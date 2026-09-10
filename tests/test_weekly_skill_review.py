"""Review every active source once without moving or rewriting live observations."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"


def observation(path):
    path.mkdir(parents=True)
    (path / "log.md").write_text("### Observation 1: pending\n**Status:** OPEN\n")
    return path


def inventory(anchor, canonical, projects):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "skill-review-workspaces.py"),
         str(anchor), str(canonical), str(projects)],
        capture_output=True, text=True,
    )


def test_inventory_deduplicates_aliases_and_includes_other_projects(tmp_path):
    canonical = tmp_path / "canonical"
    projects = tmp_path / "projects"
    anchor = observation(canonical / "wlan")
    other = observation(projects / "personal ops" / "skill-observations")
    alias = projects / "wlan" / "skill-observations"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(anchor, target_is_directory=True)
    observation(anchor / "archive" / "old")
    observation(canonical / "skill-updates" / "2026-09-01")
    observation(projects / "old" / "skill-observations.merged-backup")

    result = inventory(anchor, canonical, projects)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == sorted([str(anchor), str(other)])


def test_inventory_missing_anchor_fails_instead_of_claiming_empty(tmp_path):
    result = inventory(tmp_path / "missing", tmp_path / "canonical", tmp_path / "projects")
    assert result.returncode != 0
    assert "log.md" in result.stderr


@pytest.mark.parametrize("cli_exit", [0, 23])
def test_launcher_passes_all_sources_and_preserves_cli_exit(tmp_path, cli_exit):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for source in SCRIPTS.glob("*skill-review*"):
        if source.is_file():
            shutil.copy2(source, scripts / source.name)
    test_home = tmp_path / "home"
    cli = test_home / ".local" / "bin" / "claude"
    cli.parent.mkdir(parents=True)
    capture = tmp_path / "args.json"
    cli.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "with open(os.environ['REVIEW_TEST_CAPTURE'], 'w') as stream:\n"
        "    json.dump(sys.argv[1:], stream)\n"
        "sys.exit(int(os.environ['REVIEW_TEST_EXIT']))\n"
    )
    cli.chmod(0o755)
    anchor = observation(repo / "skill-observations" / "wlan")
    other = observation(test_home / ".claude" / "projects" / "personal ops" / "skill-observations")
    jhw_commands = tmp_path / "notion skills"
    jhw_commands.mkdir()
    command_link = test_home / ".claude" / "commands" / "jhw"
    command_link.parent.mkdir(parents=True)
    command_link.symlink_to(jhw_commands, target_is_directory=True)
    before = {p: (p / "log.md").read_bytes() for p in [anchor, other]}
    env = dict(os.environ, HOME=str(test_home), REVIEW_TEST_CAPTURE=str(capture),
               REVIEW_TEST_EXIT=str(cli_exit))

    result = subprocess.run(["bash", str(scripts / "weekly-skill-review.sh"), str(anchor)],
                            env=env, capture_output=True, text=True)

    assert result.returncode == cli_exit, result.stderr
    args = json.loads(capture.read_text())
    prompt = args[args.index("-p") + 1]
    allowed = [args[i + 1] for i, arg in enumerate(args) if arg == "--add-dir"]
    for source in [anchor, other]:
        assert str(source) in prompt
        assert str(source) in allowed
        assert (source / "log.md").read_bytes() == before[source]
    assert str(jhw_commands) in allowed
    assert "STAGED" in prompt and "ACTIONED" in prompt
    assert f"exit={cli_exit}" in (test_home / ".claude/logs/weekly-skill-review.log").read_text()
