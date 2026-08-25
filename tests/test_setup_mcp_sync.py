import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SOURCE_ROOT = Path(__file__).parents[1]
SYNC_HELPER = SOURCE_ROOT / "scripts" / "lib" / "mcp_config_sync.py"
CANARY = "CANARY_EXISTING_CREDENTIAL_MUST_NOT_APPEAR"
SAFE_TEST_ENV = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "TMPDIR",
    "TZ",
    "USER",
)


def make_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    repo = tmp_path / "repo"
    shutil.copytree(SOURCE_ROOT / "scripts", repo / "scripts")
    (repo / "manifest").mkdir()
    (repo / "manifest" / "mcp.json").write_text(
        json.dumps(
            {
                "probe": {
                    "scope": "user",
                    "command": "new-command",
                    "args": ["--safe"],
                    "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
                }
            }
        ),
        encoding="utf-8",
    )

    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    config_dir.chmod(0o700)
    config_path = config_dir / ".claude.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "probe": {
                        "type": "stdio",
                        "command": "old-command",
                        "args": ["--token", CANARY],
                        "env": {"PROBE_TOKEN": CANARY},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "claude-calls.log"
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['CLAUDE_CALLS_FILE']).write_text('called\\n', encoding='utf-8')\n"
        "raise SystemExit(99)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    env = {key: os.environ[key] for key in SAFE_TEST_ENV if key in os.environ}
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CLAUDE_PROJECT_DIR"] = str(repo)
    env["CLAUDE_CALLS_FILE"] = str(call_log)
    env["PROBE_TOKEN"] = CANARY
    return repo, env, config_path, call_log


def run_setup(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "scripts" / "setup-mcp.sh"), *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=5,
    )


def load_sync_helper():
    spec = importlib.util.spec_from_file_location("test_mcp_config_sync", SYNC_HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_preview_reports_field_level_drift_without_mutation(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 2, result.stdout + result.stderr
    assert "[DRIFT] user/probe: command,args,env" in result.stdout
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


def test_preview_blocks_literal_credential_before_any_cli_call(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"]["PROBE_TOKEN"] = CANARY
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user/probe: env.PROBE_TOKEN must use a placeholder" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


def test_apply_replaces_drift_atomically_without_claude_child(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["unmanagedTopLevel"] = {"keep": True}
    config["mcpServers"]["unmanaged"] = {"type": "stdio", "command": "keep-me"}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "[DRIFT] user/probe: command,args,env" in result.stdout
    assert "[APPLIED] user/probe" in result.stdout
    assert CANARY not in combined
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["mcpServers"]["probe"] == {
        "type": "stdio",
        "command": "new-command",
        "args": ["--safe"],
        "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
    }
    assert updated["mcpServers"]["unmanaged"] == {
        "type": "stdio",
        "command": "keep-me",
    }
    assert updated["unmanagedTopLevel"] == {"keep": True}
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("case", "expected_field"),
    [
        ("type", "type"),
        ("command", "command"),
        ("args-order", "args"),
        ("env-add", "env"),
        ("env-delete", "env"),
        ("extra", "extra"),
    ],
)
def test_preview_reports_each_drift_field_independently(
    tmp_path,
    case,
    expected_field,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["args"] = ["first", "second"]
    manifest["probe"]["env"]["SAFE_SETTING"] = "${SAFE_SETTING:-default}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    actual = {
        "type": "stdio",
        "command": "new-command",
        "args": ["first", "second"],
        "env": {
            "PROBE_TOKEN": "${PROBE_TOKEN}",
            "SAFE_SETTING": "${SAFE_SETTING:-default}",
        },
    }
    if case == "type":
        actual["type"] = "sse"
    elif case == "command":
        actual["command"] = "other-command"
    elif case == "args-order":
        actual["args"] = ["second", "first"]
    elif case == "env-add":
        actual["env"]["EXTRA_SETTING"] = "${EXTRA_SETTING}"
    elif case == "env-delete":
        del actual["env"]["SAFE_SETTING"]
    elif case == "extra":
        actual["unexpected"] = True
    config = {"mcpServers": {"probe": actual}}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env)

    assert result.returncode == 2, result.stdout + result.stderr
    assert f"[DRIFT] user/probe: {expected_field}" in result.stdout
    assert CANARY not in result.stdout + result.stderr
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("command", "${PROBE_TOKEN}", "command may not reference credential variables"),
        ("args", ["--api-key", CANARY], "args may not contain credential flags"),
        ("command", CANARY, "command matches a credential environment value"),
        ("args", [CANARY], "args match a credential environment value"),
        ("command", f"prefix-{CANARY}", "command matches a credential environment value"),
        ("args", [f"--header=Bearer {CANARY}"], "args may not contain credential flags"),
    ],
)
def test_preview_blocks_credentials_in_command_or_args(
    tmp_path,
    field,
    value,
    diagnostic,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"[BLOCKED] user/probe: {diagnostic}" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


@pytest.mark.parametrize(
    "args",
    [
        [f"--auth={CANARY}"],
        ["--authorization", CANARY],
        [f"--bearer={CANARY}"],
        [f"--access-key={CANARY}"],
        ["--private-key", CANARY],
        ["--header", f"Bearer {CANARY}"],
        ["-H", f"Authorization: Bearer {CANARY}"],
        [f"--headers=Authorization: Bearer {CANARY}"],
        [f"-HAuthorization: Bearer {CANARY}"],
        [f"-H=Authorization: Bearer {CANARY}"],
        ["-e", f"PROBE_TOKEN={CANARY}"],
        [f"-ePROBE_TOKEN={CANARY}"],
        [f"--env=PROBE_TOKEN={CANARY}"],
        ["--wrapper-option", f"PROBE_TOKEN={CANARY}"],
    ],
)
def test_preview_blocks_extended_credential_flags_without_inherited_secret(
    tmp_path,
    args,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["args"] = args
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user/probe: args may not contain credential flags" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


def test_preview_blocks_credential_assignment_in_command_without_inherited_secret(
    tmp_path,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["command"] = f"PROBE_TOKEN={CANARY}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "command may not contain credential carriers" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("command", CANARY, "command matches a credential environment value"),
        ("args", [f"prefix-{CANARY}"], "args match a credential environment value"),
    ],
)
def test_preview_blocks_literal_from_ambient_credential_not_declared_by_entry(
    tmp_path,
    field,
    value,
    diagnostic,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    env["OTHER_API_KEY"] = CANARY
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {}
    manifest["probe"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"[BLOCKED] user/probe: {diagnostic}" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize("shadow_scope", ["local", "project"])
def test_preview_blocks_higher_precedence_scope_shadow(tmp_path, shadow_scope):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    desired = {
        "type": "stdio",
        "command": "new-command",
        "args": ["--safe"],
        "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
    }
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mcpServers"]["probe"] = desired
    config_path.write_text(json.dumps(config), encoding="utf-8")
    if shadow_scope == "local":
        config["projects"] = {
            str(repo): {
                "mcpServers": {
                    "probe": {
                        "type": "stdio",
                        "command": "shadow-command",
                        "args": [CANARY],
                    }
                }
            }
        }
    else:
        (repo / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "probe": {
                            "type": "stdio",
                            "command": "shadow-command",
                            "args": [CANARY],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"[SHADOWED] user/probe: {shadow_scope}" in result.stderr
    assert CANARY not in combined
    assert not call_log.exists()


def test_preview_detects_local_shadow_under_an_opaque_claude_project_key(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    desired = {
        "type": "stdio",
        "command": "new-command",
        "args": ["--safe"],
        "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
    }
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mcpServers"]["probe"] = desired
    config["projects"] = {
        "/opaque/claude-project-key": {
            "mcpServers": {
                "probe": {
                    "type": "stdio",
                    "command": "shadow-command",
                    "args": [CANARY],
                }
            }
        }
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[SHADOWED] user/probe: local" in result.stderr
    assert "/opaque/claude-project-key" not in combined
    assert CANARY not in combined
    assert not call_log.exists()


def test_explicit_apply_migrates_legacy_local_entry_to_user_scope(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config = {
        "projects": {
            "/opaque/claude-project-key": {
                "mcpServers": {
                    "probe": {
                        "type": "stdio",
                        "command": "legacy-command",
                        "args": [CANARY],
                        "env": {"PROBE_TOKEN": CANARY},
                    },
                    "unmanaged": {"type": "stdio", "command": "keep-me"},
                }
            }
        },
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env, "--apply", "--migrate-local")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "[MIGRATED] local/probe -> user/probe" in result.stdout
    assert "[APPLIED] user/probe" in result.stdout
    assert "/opaque/claude-project-key" not in combined
    assert CANARY not in combined
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["mcpServers"]["probe"] == {
        "type": "stdio",
        "command": "new-command",
        "args": ["--safe"],
        "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
    }
    local_servers = updated["projects"]["/opaque/claude-project-key"]["mcpServers"]
    assert "probe" not in local_servers
    assert local_servers["unmanaged"] == {"type": "stdio", "command": "keep-me"}
    assert not call_log.exists()


def test_migrate_local_still_blocks_project_scope_shadow(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["projects"] = {
        "/opaque/claude-project-key": {
            "mcpServers": {"probe": {"command": "legacy-local", "args": [CANARY]}}
        }
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (repo / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"probe": {"command": "project-shadow", "args": [CANARY]}}}
        ),
        encoding="utf-8",
    )
    original = config_path.read_bytes()

    result = run_setup(repo, env, "--apply", "--migrate-local")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[SHADOWED] user/probe: project" in result.stderr
    assert "local" not in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


def test_migrate_local_requires_explicit_apply(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    original = config_path.read_bytes()

    result = run_setup(repo, env, "--migrate-local")

    assert result.returncode == 64
    assert "--migrate-local requires --apply" in result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize("preview_flag", ["--check", "--dry-run"])
def test_migrate_local_rejects_preview_mode(tmp_path, preview_flag):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    original = config_path.read_bytes()

    result = run_setup(repo, env, preview_flag, "--migrate-local")

    assert result.returncode == 64
    assert "--migrate-local requires --apply" in result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


def test_apply_rejects_symlinked_user_config_without_leaking_contents(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    target = tmp_path / "actual-claude.json"
    target.write_bytes(config_path.read_bytes())
    original_target = target.read_bytes()
    config_path.unlink()
    config_path.symlink_to(target)

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user configuration is not a private regular file" in result.stderr
    assert CANARY not in combined
    assert target.read_bytes() == original_target
    assert not call_log.exists()


def test_preview_rejects_fifo_user_config_without_blocking(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config_path.unlink()
    os.mkfifo(config_path, mode=0o600)

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user configuration is not a private regular file" in result.stderr
    assert CANARY not in combined
    assert not call_log.exists()


@pytest.mark.parametrize("unsafe_kind", ["world-readable", "hard-linked"])
def test_apply_rejects_unsafe_user_config_metadata(tmp_path, unsafe_kind):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    if unsafe_kind == "world-readable":
        config_path.chmod(0o644)
    else:
        os.link(config_path, tmp_path / "second-config-link")
    original = config_path.read_bytes()

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user configuration is not a private regular file" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


def test_apply_rejects_symlinked_lock_without_touching_its_target(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    lock_target = tmp_path / "lock-target"
    lock_target.write_text(CANARY, encoding="utf-8")
    lock_path = config_path.with_name(".claude.json.mcp-sync.lock")
    lock_path.symlink_to(lock_target)
    original_config = config_path.read_bytes()

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] MCP synchronization lock could not be secured" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert lock_target.read_text(encoding="utf-8") == CANARY
    assert not call_log.exists()


def test_apply_rejects_group_or_world_writable_config_directory(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config_dir = config_path.parent
    config_dir.chmod(0o777)
    original = config_path.read_bytes()

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user configuration directory is not secure" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not list(config_dir.glob(".claude.json.mcp-sync.*"))
    assert not call_log.exists()


def test_apply_rejects_symlinked_config_directory(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config_dir = config_path.parent
    target_dir = tmp_path / "actual-config-directory"
    config_dir.rename(target_dir)
    config_dir.symlink_to(target_dir, target_is_directory=True)
    target_config = target_dir / ".claude.json"
    original = target_config.read_bytes()

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user configuration directory is not secure" in result.stderr
    assert CANARY not in combined
    assert target_config.read_bytes() == original
    assert not list(target_dir.glob(".claude.json.mcp-sync.*"))
    assert not call_log.exists()


def test_apply_creates_missing_user_config_with_private_mode(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config_path.unlink()

    result = run_setup(repo, env, "--apply")

    assert result.returncode == 0, result.stdout + result.stderr
    assert config_path.stat().st_mode & 0o777 == 0o600
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["mcpServers"]["probe"]["env"] == {
        "PROBE_TOKEN": "${PROBE_TOKEN}"
    }
    assert CANARY not in result.stdout + result.stderr
    assert not call_log.exists()


def test_atomic_apply_aborts_if_config_changes_before_commit(tmp_path, monkeypatch):
    helper = load_sync_helper()
    config_path = tmp_path / ".claude.json"
    initial = {"mcpServers": {}, "unmanaged": {"keep": True}}
    config_path.write_text(json.dumps(initial), encoding="utf-8")
    config_path.chmod(0o600)
    expected_raw = config_path.read_bytes()
    directory_metadata = tmp_path.stat(follow_symlinks=False)
    expected_directory = (directory_metadata.st_dev, directory_metadata.st_ino)
    concurrent = {"mcpServers": {}, "unmanaged": {"concurrent": True}}
    concurrent_raw = json.dumps(concurrent).encode("utf-8")
    original_read = helper._read_user_config
    calls = 0

    def interleaved_read(path, directory_fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(concurrent_raw)
            path.chmod(0o600)
        return original_read(path, directory_fd)

    monkeypatch.setattr(helper, "_read_user_config", interleaved_read)
    result = helper._atomic_apply(
        config_path,
        expected_raw,
        {"probe": {"type": "stdio", "command": "safe", "args": [], "env": {}}},
        set(),
        expected_directory,
    )

    assert result == "changed"
    assert config_path.read_bytes() == concurrent_raw
    leftovers = [
        path
        for path in tmp_path.glob(".claude.json.mcp-sync.*")
        if path.name != ".claude.json.mcp-sync.lock"
    ]
    assert not leftovers


def test_atomic_apply_rejects_config_directory_retarget_after_preview(tmp_path):
    helper = load_sync_helper()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_dir.chmod(0o700)
    config_path = config_dir / ".claude.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    config_path.chmod(0o600)
    expected_raw = config_path.read_bytes()
    metadata = config_dir.stat(follow_symlinks=False)
    expected_directory = (metadata.st_dev, metadata.st_ino)
    original_dir = tmp_path / "original-config"
    config_dir.rename(original_dir)
    config_dir.mkdir()
    config_dir.chmod(0o700)
    retargeted_config = config_dir / ".claude.json"
    retargeted_config.write_bytes(expected_raw)
    retargeted_config.chmod(0o600)
    desired = {"type": "stdio", "command": "safe", "args": [], "env": {}}

    result = helper._atomic_apply(
        retargeted_config,
        expected_raw,
        {"probe": desired},
        set(),
        expected_directory,
    )

    assert result == "changed"
    assert retargeted_config.read_bytes() == expected_raw
    assert (original_dir / ".claude.json").read_bytes() == expected_raw
    assert not list(config_dir.glob(".claude.json.mcp-sync.*"))


def test_atomic_apply_reports_durability_uncertain_after_committed_replace(
    tmp_path,
    monkeypatch,
):
    helper = load_sync_helper()
    config_path = tmp_path / ".claude.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    config_path.chmod(0o600)
    expected_raw = config_path.read_bytes()
    directory_metadata = tmp_path.stat(follow_symlinks=False)
    expected_directory = (directory_metadata.st_dev, directory_metadata.st_ino)
    desired = {"type": "stdio", "command": "safe", "args": [], "env": {}}
    original_fsync = helper.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory fsync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(helper.os, "fsync", fail_directory_fsync)
    result = helper._atomic_apply(
        config_path,
        expected_raw,
        {"probe": desired},
        set(),
        expected_directory,
    )

    assert result == "durability-uncertain"
    assert json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"][
        "probe"
    ] == desired


def test_check_does_not_report_committed_but_unconfirmed_apply_as_not_updated(
    tmp_path,
    monkeypatch,
    capsys,
):
    repo, env, config_path, _call_log = make_fixture(tmp_path)
    helper = load_sync_helper()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        helper,
        "_atomic_apply",
        lambda *_args, **_kwargs: "durability-uncertain",
    )
    original = config_path.read_bytes()

    result = helper.check(
        repo / "manifest" / "mcp.json",
        with_internal=False,
        apply=True,
    )
    captured = capsys.readouterr()

    assert result == 1
    assert "[APPLIED_UNCONFIRMED]" in captured.err
    assert "was not updated" not in captured.err
    assert CANARY not in captured.out + captured.err
    assert config_path.read_bytes() == original


def test_check_disables_shell_xtrace_before_processing_private_environment(tmp_path):
    repo, env, _config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {"BRAVE_API_KEY": "${BRAVE_API_KEY}"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    secret_file = repo / "secrets.local.env"
    secret_file.write_text(f"BRAVE_API_KEY={CANARY}\n", encoding="utf-8")
    secret_file.chmod(0o600)

    result = subprocess.run(
        ["bash", "-x", str(repo / "scripts" / "setup-mcp.sh")],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert CANARY not in combined
    assert not call_log.exists()


def test_repository_manifest_passes_secret_safe_planning(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    shutil.copy2(SOURCE_ROOT / "manifest" / "mcp.json", repo / "manifest" / "mcp.json")
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    result = run_setup(repo, env, "--no-internal")

    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert "[MISSING] user/morph-mcp" in result.stdout
    assert "cts-email" not in combined
    assert "cts-ta" not in combined
    assert "jhw-notion" not in combined
    assert "ssh-mcp" not in combined
    assert "[BLOCKED]" not in combined
    assert not call_log.exists()


def test_preview_blocks_literal_default_in_credential_placeholder(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"]["PROBE_TOKEN"] = f"${{PROBE_TOKEN:-{CANARY}}}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user/probe: env.PROBE_TOKEN credential placeholder may not have a default" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


def test_check_returns_in_sync_without_cli_calls(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mcpServers"]["probe"] = {
        "type": "stdio",
        "command": "new-command",
        "args": ["--safe"],
        "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "[IN_SYNC] user/probe"
    assert not call_log.exists()


def test_apply_adds_missing_entry_without_claude_child(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "[MISSING] user/probe" in result.stdout
    assert "[APPLIED] user/probe" in result.stdout
    assert CANARY not in combined
    assert not call_log.exists()


@pytest.mark.parametrize("preview_flag", ["--check", "--dry-run"])
def test_setup_rejects_conflicting_preview_and_apply_modes(tmp_path, preview_flag):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    original_config = config_path.read_bytes()

    result = run_setup(repo, env, preview_flag, "--apply")

    assert result.returncode == 64
    assert "conflicting mode options" in result.stderr
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


def test_setup_mcp_does_not_read_repo_secret_file(tmp_path):
    repo, env, _config_path, call_log = make_fixture(tmp_path)
    secret_file = repo / "secrets.local.env"
    secret_file.write_text(f"BRAVE_API_KEY={CANARY}\n", encoding="utf-8")
    secret_file.chmod(0o644)

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert "unsafe secret file" not in combined.lower()
    assert CANARY not in combined
    assert not call_log.exists()


def test_apply_does_not_spawn_claude_with_repo_secret_values(tmp_path):
    repo, env, _config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {"BRAVE_API_KEY": "${BRAVE_API_KEY}"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    secret_file = repo / "secrets.local.env"
    secret_file.write_text(f"BRAVE_API_KEY={CANARY}\n", encoding="utf-8")
    secret_file.chmod(0o600)
    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert CANARY not in combined
    assert not call_log.exists()
