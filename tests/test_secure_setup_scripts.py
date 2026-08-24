import importlib.util
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


SOURCE_ROOT = Path(__file__).parents[1]
READER = SOURCE_ROOT / "scripts" / "lib" / "secure_env_reader.py"
CANARY = "CANARY_SECRET_MUST_NOT_APPEAR"
SECRET_CONTENT = "\n".join(
    [
        f"BRAVE_API_KEY={CANARY}",
        "MORPH_API_KEY=morph-test-value",
        "SLACK_BOT_TOKEN=xoxb-test-value",
        "SLACK_APP_TOKEN=xapp-test-value",
        f"SLACK_CHANNEL_ID={CANARY}",
        "SLACK_ALLOWED_USER_ID=user-test-value",
        "",
    ]
)


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
    env = dict(os.environ)
    env["HOME"] = str(test_home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    for key in (
        "BRAVE_API_KEY",
        "MORPH_API_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_CHANNEL_ID",
        "SLACK_ALLOWED_USER_ID",
    ):
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


def run_setup(
    repo: Path,
    env: dict[str, str],
    script_name: str,
) -> subprocess.CompletedProcess[str]:
    args = ["bash", str(repo / "scripts" / script_name), "--dry-run"]
    if script_name == "setup-mcp.sh":
        args.insert(-1, "--no-internal")
    return subprocess.run(args, text=True, capture_output=True, check=False, env=env)


@pytest.mark.parametrize("script_name", ["setup-mcp.sh", "setup-slack-bridge.sh"])
def test_setup_rejects_group_or_world_readable_secret_file(tmp_path, script_name):
    repo, env = make_fixture(tmp_path, script_name)
    secret_file = write_secret_file(repo, mode=0o644)
    sourced_marker = tmp_path / "unsafe-file-was-sourced"
    with secret_file.open("a", encoding="utf-8") as stream:
        stream.write(f"touch {shlex.quote(str(sourced_marker))}\n")

    result = run_setup(repo, env, script_name)

    assert result.returncode != 0
    assert "unsafe secret file" in result.stderr.lower()
    assert not sourced_marker.exists()


@pytest.mark.parametrize("script_name", ["setup-mcp.sh", "setup-slack-bridge.sh"])
def test_setup_rejects_symlinked_secret_file(tmp_path, script_name):
    repo, env = make_fixture(tmp_path, script_name)
    target = tmp_path / "actual-secrets.env"
    target.write_text(SECRET_CONTENT, encoding="utf-8")
    target.chmod(0o600)
    (repo / "secrets.local.env").symlink_to(target)

    result = run_setup(repo, env, script_name)

    assert result.returncode != 0
    assert "unsafe secret file" in result.stderr.lower()


@pytest.mark.parametrize("script_name", ["setup-mcp.sh", "setup-slack-bridge.sh"])
def test_setup_rejects_hardlinked_secret_file(tmp_path, script_name):
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
    original = b"BRAVE_API_KEY=original-value\n"
    secret_file.write_bytes(original)
    replacement.write_bytes(b"BRAVE_API_KEY=replacement-value\n")
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


def test_setup_mcp_dry_run_redacts_expanded_secret_values(tmp_path):
    repo, env = make_fixture(tmp_path, "setup-mcp.sh")
    write_secret_file(repo)

    result = run_setup(repo, env, "setup-mcp.sh")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert CANARY not in combined
    assert "BRAVE_API_KEY=<redacted>" in result.stdout


def test_setup_slack_dry_run_does_not_log_channel_id(tmp_path):
    repo, env = make_fixture(tmp_path, "setup-slack-bridge.sh")
    write_secret_file(repo)

    result = run_setup(repo, env, "setup-slack-bridge.sh")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert CANARY not in combined
    assert "SLACK_* 4개 확인됨" in result.stdout
