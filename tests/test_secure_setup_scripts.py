import os
import shutil
import subprocess
from pathlib import Path

import pytest


SOURCE_ROOT = Path(__file__).parents[1]
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
    write_secret_file(repo, mode=0o644)

    result = run_setup(repo, env, script_name)

    assert result.returncode != 0
    assert "unsafe secret file" in result.stderr.lower()


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


@pytest.mark.parametrize("script_name", ["setup-mcp.sh", "setup-slack-bridge.sh"])
def test_setup_rejects_owner_mismatch(tmp_path, script_name):
    repo, env = make_fixture(tmp_path, script_name)
    write_secret_file(repo)
    fake_id = tmp_path / "bin" / "id"
    fake_id.write_text("#!/bin/sh\nprintf '999999\\n'\n", encoding="utf-8")
    fake_id.chmod(0o755)

    result = run_setup(repo, env, script_name)

    assert result.returncode != 0
    assert "unsafe secret file" in result.stderr.lower()


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
