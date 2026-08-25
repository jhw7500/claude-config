import importlib.util
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


SOURCE_ROOT = Path(__file__).parents[1]
READER = SOURCE_ROOT / "scripts" / "lib" / "secure_env_reader.py"
LOADER = SOURCE_ROOT / "scripts" / "lib" / "secure-env-file.sh"
CANARY = "CANARY_SECRET_MUST_NOT_APPEAR"
SECRET_CONTENT = "\n".join(
    [
        "SLACK_BOT_TOKEN=xoxb-test-value",
        "SLACK_APP_TOKEN=xapp-test-value",
        f"SLACK_CHANNEL_ID={CANARY}",
        "SLACK_ALLOWED_USER_ID=user-test-value",
        "",
    ]
)
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
SLACK_ENV_NAMES = frozenset(
    {
        "SLACK_ALLOWED_USER_ID",
        "SLACK_APP_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_CHANNEL_ID",
    }
)


def sanitized_env(source: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    return {key: source[key] for key in SAFE_TEST_ENV if key in source}


def make_fixture(tmp_path: Path, script_name: str) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    shutil.copytree(SOURCE_ROOT / "scripts", repo / "scripts")

    if script_name == "setup-mcp.sh":
        (repo / "manifest").mkdir()
        shutil.copy2(SOURCE_ROOT / "manifest" / "mcp.json", repo / "manifest" / "mcp.json")
    else:
        (repo / "slack-bridge").mkdir()
        shutil.copy2(
            SOURCE_ROOT / "slack-bridge" / "claude-slack-bridge.service.template",
            repo / "slack-bridge" / "claude-slack-bridge.service.template",
        )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    test_home = tmp_path / "home"
    test_home.mkdir()
    test_home.chmod(0o700)
    env = sanitized_env()
    env["HOME"] = str(test_home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    for key in SLACK_ENV_NAMES:
        env.pop(key, None)
    return repo, env


def write_secret_file(repo: Path, mode: int = 0o600) -> Path:
    secret_file = repo / "secrets.local.env"
    secret_file.write_text(SECRET_CONTENT, encoding="utf-8")
    secret_file.chmod(mode)
    return secret_file


def load_reader_module():
    assert READER.exists(), "secure env reader is not implemented"
    spec = importlib.util.spec_from_file_location("secure_env_reader", READER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secure_env_reader_and_example_are_slack_only():
    reader = load_reader_module()
    assignments = reader.parse_env_assignments(
        (SOURCE_ROOT / "secrets.example.env").read_bytes(),
        environ={"HOME": "/safe/home"},
    )

    assert reader.ALLOWED_ENV_NAMES == SLACK_ENV_NAMES
    assert {name for name, _value in assignments} == SLACK_ENV_NAMES


def run_setup(
    repo: Path,
    env: dict[str, str],
    script_name: str,
) -> subprocess.CompletedProcess[str]:
    args = ["bash", str(repo / "scripts" / script_name), "--dry-run"]
    if script_name == "setup-mcp.sh":
        args.insert(-1, "--no-internal")
    return subprocess.run(args, text=True, capture_output=True, check=False, env=env)


def run_loader(
    secret_file: Path,
    body: str,
    *,
    before_load: str = "",
    env: dict[str, str] | None = None,
    errexit: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = "\n".join(
        [
            "set -e" if errexit else "set +e",
            '. "$1"',
            before_load,
            'load_private_env_file "$2"',
            body,
        ]
    )
    return subprocess.run(
        ["bash", "-c", command, "loader-test", str(LOADER), str(secret_file)],
        text=True,
        capture_output=True,
        check=False,
        env=sanitized_env() if env is None else env,
    )


def test_setup_slack_rejects_group_or_world_readable_secret_file(tmp_path):
    script_name = "setup-slack-bridge.sh"
    repo, env = make_fixture(tmp_path, script_name)
    secret_file = write_secret_file(repo, mode=0o644)
    sourced_marker = tmp_path / "unsafe-file-was-sourced"
    with secret_file.open("a", encoding="utf-8") as stream:
        stream.write(f"touch {shlex.quote(str(sourced_marker))}\n")

    result = run_setup(repo, env, script_name)

    assert result.returncode != 0
    assert "unsafe secret file" in result.stderr.lower()
    assert not sourced_marker.exists()


def test_setup_slack_rejects_symlinked_secret_file(tmp_path):
    script_name = "setup-slack-bridge.sh"
    repo, env = make_fixture(tmp_path, script_name)
    target = tmp_path / "actual-secrets.env"
    target.write_text(SECRET_CONTENT, encoding="utf-8")
    target.chmod(0o600)
    (repo / "secrets.local.env").symlink_to(target)

    result = run_setup(repo, env, script_name)

    assert result.returncode != 0
    assert "unsafe secret file" in result.stderr.lower()


def test_setup_slack_rejects_hardlinked_secret_file(tmp_path):
    script_name = "setup-slack-bridge.sh"
    repo, env = make_fixture(tmp_path, script_name)
    secret_file = write_secret_file(repo)
    os.link(secret_file, tmp_path / "second-link.env")

    result = run_setup(repo, env, script_name)

    assert result.returncode != 0
    assert "unsafe secret file" in result.stderr.lower()


def test_reader_rejects_owner_mismatch(tmp_path, monkeypatch):
    reader = load_reader_module()
    secret_file = tmp_path / "secrets.env"
    secret_file.write_text(SECRET_CONTENT, encoding="utf-8")
    secret_file.chmod(0o600)
    actual_owner = secret_file.stat().st_uid
    monkeypatch.setattr(reader.os, "getuid", lambda: actual_owner + 1)

    with pytest.raises(reader.UnsafeSecretFile):
        reader.read_private_env(secret_file)


def test_reader_pins_open_file_when_path_is_replaced(tmp_path, monkeypatch):
    reader = load_reader_module()
    secret_file = tmp_path / "secrets.env"
    replacement = tmp_path / "replacement.env"
    original = b"SLACK_BOT_TOKEN=original-value\n"
    secret_file.write_bytes(original)
    replacement.write_bytes(b"SLACK_BOT_TOKEN=replacement-value\n")
    secret_file.chmod(0o600)
    replacement.chmod(0o600)
    real_read = reader.os.read
    replaced = False

    def replace_then_read(descriptor, size):
        nonlocal replaced
        if not replaced:
            os.replace(replacement, secret_file)
            replaced = True
        return real_read(descriptor, size)

    monkeypatch.setattr(reader.os, "read", replace_then_read)

    assert reader.read_private_env(secret_file) == original


def test_loader_preserves_trailing_backslash_at_eof(tmp_path):
    secret_file = tmp_path / "secrets.env"
    secret_file.write_bytes(b"SLACK_CHANNEL_ID=abc\\")
    secret_file.chmod(0o600)

    result = run_loader(secret_file, "printf '%s' \"$SLACK_CHANNEL_ID\"")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "abc\\"


@pytest.mark.parametrize(
    "payload",
    [
        "SLACK_BOT_TOKEN=$(touch {marker})\n",
        "SLACK_BOT_TOKEN=`touch {marker}`\n",
        "SLACK_BOT_TOKEN=value;touch {marker}\n",
        "SLACK_BOT_TOKEN=value\ntouch {marker}\n",
    ],
)
def test_loader_rejects_shell_code_without_executing(tmp_path, payload):
    secret_file = tmp_path / "secrets.env"
    marker = tmp_path / "shell-code-ran"
    secret_file.write_text(
        payload.format(marker=shlex.quote(str(marker))),
        encoding="utf-8",
    )
    secret_file.chmod(0o600)

    result = run_loader(secret_file, "printf 'LOADER_COMPLETED\\n'")

    assert result.returncode != 0
    assert not marker.exists()
    assert "LOADER_COMPLETED" not in result.stdout


def test_loader_accepts_documented_assignment_syntax(tmp_path):
    secret_file = tmp_path / "secrets.env"
    secret_file.write_bytes(
        b"# data-only dotenv syntax\r\n"
        b"export SLACK_BOT_TOKEN='literal $HOME # ='\r\n"
        b'SLACK_APP_TOKEN="double ${HOME}/path with spaces and \\$literal"\r\n'
        b"SLACK_CHANNEL_ID=edit\\ file\r\n"
        b"SLACK_ALLOWED_USER_ID=0 # inline comment"
    )
    secret_file.chmod(0o600)
    env = sanitized_env()
    env["HOME"] = "/safe/home"

    result = run_loader(
        secret_file,
        "printf '%s\n' \"$SLACK_BOT_TOKEN\" \"$SLACK_APP_TOKEN\" "
        '"$SLACK_CHANNEL_ID" "$SLACK_ALLOWED_USER_ID"',
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "literal $HOME # =",
        "double /safe/home/path with spaces and $literal",
        "edit file",
        "0",
    ]


def test_loader_accepts_the_documented_example_file(tmp_path):
    secret_file = tmp_path / "secrets.env"
    secret_file.write_bytes((SOURCE_ROOT / "secrets.example.env").read_bytes())
    secret_file.chmod(0o600)
    env = sanitized_env()
    env["HOME"] = "/safe/home"

    result = run_loader(
        secret_file,
        "printf '%s|%s|%s|%s' \"$SLACK_BOT_TOKEN\" \"$SLACK_APP_TOKEN\" "
        '"$SLACK_CHANNEL_ID" "$SLACK_ALLOWED_USER_ID"',
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "|||"


def test_loader_does_not_partially_apply_an_invalid_file(tmp_path):
    secret_file = tmp_path / "secrets.env"
    secret_file.write_text(
        "SLACK_BOT_TOKEN=changed\nPYTHONPATH=/tmp/injection\n",
        encoding="utf-8",
    )
    secret_file.chmod(0o600)
    env = sanitized_env()
    env["SLACK_BOT_TOKEN"] = "original"

    result = run_loader(
        secret_file,
        'loader_status=$?\nprintf \'%s|%s\' "$loader_status" "$SLACK_BOT_TOKEN"',
        env=env,
        errexit=False,
    )

    assert result.returncode == 0
    assert result.stdout == "1|original"
    assert "unsupported environment variable" in result.stderr.lower()


@pytest.mark.parametrize(
    ("payload", "extra_env"),
    [
        (b"SLACK_BOT_TOKEN='safe\t--\tbash'\n", {}),
        (
            b"SLACK_BOT_TOKEN=$INHERITED_VALUE\n",
            {"INHERITED_VALUE": "safe\nINJECTED=value"},
        ),
    ],
)
def test_loader_rejects_control_characters_in_values(tmp_path, payload, extra_env):
    secret_file = tmp_path / "secrets.env"
    secret_file.write_bytes(payload)
    secret_file.chmod(0o600)
    env = sanitized_env()
    env.update(extra_env)

    result = run_loader(secret_file, "printf 'LOADER_COMPLETED\n'", env=env)

    assert result.returncode != 0
    assert "control characters" in result.stderr.lower()
    assert "LOADER_COMPLETED" not in result.stdout


@pytest.mark.parametrize(
    ("declaration", "original"),
    [
        ("declare -l SLACK_BOT_TOKEN=ORIGINAL", "original"),
        ("declare -u SLACK_BOT_TOKEN=original", "ORIGINAL"),
    ],
)
def test_loader_rejects_shell_variables_that_transform_values(
    tmp_path,
    declaration,
    original,
):
    secret_file = tmp_path / "secrets.env"
    secret_file.write_text("SLACK_BOT_TOKEN=MiXeD\n", encoding="utf-8")
    secret_file.chmod(0o600)

    result = run_loader(
        secret_file,
        'loader_status=$?\nprintf \'%s|%s\' "$loader_status" "$SLACK_BOT_TOKEN"',
        before_load=declaration,
        env=sanitized_env(),
        errexit=False,
    )

    assert result.returncode == 0
    assert result.stdout == f"1|{original}"
    assert "shell variable attributes" in result.stderr.lower()


def test_loader_updates_a_callers_local_environment_value(tmp_path):
    secret_file = tmp_path / "secrets.env"
    secret_file.write_text("SLACK_BOT_TOKEN=loaded\n", encoding="utf-8")
    secret_file.chmod(0o600)
    command = "\n".join(
        [
            'source "$1"',
            "caller() {",
            "  local SLACK_BOT_TOKEN=original",
            '  load_private_env_file "$1"',
            '  printf \'%s\' "$SLACK_BOT_TOKEN"',
            "}",
            'caller "$2"',
        ]
    )

    result = subprocess.run(
        ["bash", "-c", command, "loader-test", str(LOADER), str(secret_file)],
        text=True,
        capture_output=True,
        check=False,
        env=sanitized_env(),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "loaded"


def test_setup_mcp_dry_run_alias_reports_missing_without_secret_values(tmp_path):
    repo, env = make_fixture(tmp_path, "setup-mcp.sh")
    write_secret_file(repo)

    result = run_setup(repo, env, "setup-mcp.sh")

    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert CANARY not in combined
    assert "[MISSING] user/brave-search" in result.stdout


def test_setup_mcp_dry_run_blocks_credentials_in_command_or_args(tmp_path):
    repo, env = make_fixture(tmp_path, "setup-mcp.sh")
    write_secret_file(repo)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["brave-search"]["command"] = "${BRAVE_API_KEY}"
    manifest["brave-search"]["args"] = ["--token", "${BRAVE_API_KEY}"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = run_setup(repo, env, "setup-mcp.sh")

    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert CANARY not in combined
    assert "command may not reference credential variables" in result.stderr


def test_setup_slack_rejects_legacy_mcp_env_names_without_value_disclosure(tmp_path):
    repo, env = make_fixture(tmp_path, "setup-slack-bridge.sh")
    secret_file = write_secret_file(repo)
    with secret_file.open("a", encoding="utf-8") as stream:
        stream.write(f"BRAVE_API_KEY={CANARY}\n")

    result = run_setup(repo, env, "setup-slack-bridge.sh")

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "unsupported environment variable" in result.stderr.lower()
    assert CANARY not in combined


def test_setup_slack_rejects_unsupported_env_names_before_starting_child_processes(tmp_path):
    repo, env = make_fixture(tmp_path, "setup-slack-bridge.sh")
    secret_file = write_secret_file(repo)
    injection_dir = tmp_path / "python-injection"
    injection_dir.mkdir()
    marker = tmp_path / "sitecustomize-ran"
    (injection_dir / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    with secret_file.open("a", encoding="utf-8") as stream:
        stream.write(f"PYTHONPATH={injection_dir}\n")

    result = run_setup(repo, env, "setup-slack-bridge.sh")

    assert result.returncode != 0
    assert "unsupported environment variable" in result.stderr.lower()
    assert not marker.exists()


def test_setup_slack_dry_run_does_not_log_channel_id(tmp_path):
    repo, env = make_fixture(tmp_path, "setup-slack-bridge.sh")
    write_secret_file(repo)

    result = run_setup(repo, env, "setup-slack-bridge.sh")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert CANARY not in combined
    assert "SLACK_* 4개 확인됨" in result.stdout


def test_setup_slack_writes_backslashes_literally_when_xpg_echo_is_enabled(tmp_path):
    repo, env = make_fixture(tmp_path, "setup-slack-bridge.sh")
    secret_file = repo / "secrets.local.env"
    secret_file.write_text(
        "SLACK_BOT_TOKEN='token\\nINJECTED=value'\n"
        "SLACK_APP_TOKEN=app-token\n"
        "SLACK_CHANNEL_ID=channel-id\n"
        "SLACK_ALLOWED_USER_ID=user-id\n",
        encoding="utf-8",
    )
    secret_file.chmod(0o600)
    for command in ("systemctl", "uv"):
        executable = tmp_path / "bin" / command
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

    result = subprocess.run(
        ["bash", "-O", "xpg_echo", str(repo / "scripts" / "setup-slack-bridge.sh")],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    env_file = Path(env["HOME"]) / ".config" / "claude-slack-bridge.env"
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        r"SLACK_BOT_TOKEN=token\nINJECTED=value",
        "SLACK_APP_TOKEN=app-token",
        "SLACK_CHANNEL_ID=channel-id",
        "SLACK_ALLOWED_USER_ID=user-id",
    ]
