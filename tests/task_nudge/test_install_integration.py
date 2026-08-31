import json
import stat

import pytest

from conftest import REPO


CLAUDE_COMMAND = "$HOME/.claude/hooks/task-nudge.sh"
CODEX_COMMAND = "/usr/bin/python3 $HOME/.local/share/claude-config/hooks/task-nudge-codex.py"
START = "<!-- claude-config:task-nudge:START -->"
END = "<!-- claude-config:task-nudge:END -->"


def _task_targets(home):
    return [
        home / ".local" / "share" / "claude-config" / "hooks" / "task_nudge.py",
        home / ".local" / "share" / "claude-config" / "hooks" / "task-nudge-claude.py",
        home / ".local" / "share" / "claude-config" / "hooks" / "task-nudge-codex.py",
        home / ".claude" / "hooks" / "task-nudge.sh",
        home / ".claude" / "settings.json",
        home / ".codex" / "hooks.json",
        home / ".codex" / "AGENTS.md",
    ]


def _pretool_entries(path):
    return json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]


def test_fresh_install_adds_neutral_hooks_codex_config_and_agents(home, run_install):
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    neutral = home / ".local" / "share" / "claude-config" / "hooks"
    assert (neutral / "task_nudge.py").is_file()
    assert (neutral / "task-nudge-claude.py").is_file()
    assert (neutral / "task-nudge-codex.py").is_file()
    assert all((path.stat().st_mode & 0o077) == 0 for path in neutral.iterdir())
    shim = home / ".claude" / "hooks" / "task-nudge.sh"
    assert shim.is_file() and not shim.is_symlink()
    assert shim.read_bytes() == (REPO / "hooks" / "task-nudge.sh").read_bytes()
    assert stat.S_IMODE(shim.stat().st_mode) == 0o700
    codex = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    groups = codex["hooks"]["PreToolUse"]
    assert groups == [{"matcher": "apply_patch|Edit|Write", "hooks": [{"type": "command", "command": CODEX_COMMAND}]}]
    claude_groups = _pretool_entries(home / ".claude" / "settings.json")
    assert [group for group in claude_groups if group["hooks"] == [{"type": "command", "command": CLAUDE_COMMAND}]] == [
        {"matcher": "Edit|Write|NotebookEdit", "hooks": [{"type": "command", "command": CLAUDE_COMMAND}]}
    ]
    agents = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.count(START) == agents.count(END) == 1
    assert "/hooks" in result.stdout


def test_install_preserves_unrelated_codex_and_agents_content(home, run_install):
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "keep"}]}]}, "ui": "keep"}),
        encoding="utf-8",
    )
    (codex / "AGENTS.md").write_text("keep-before\n", encoding="utf-8")
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    merged = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
    assert merged["ui"] == "keep"
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "keep"
    assert "keep-before" in (codex / "AGENTS.md").read_text(encoding="utf-8")


def test_identical_reinstall_is_byte_stable_and_adds_no_backups(home, run_install):
    first = run_install(home)
    assert first.returncode == 0, first.stderr
    targets = _task_targets(home)[4:]
    before = {path: path.read_bytes() for path in targets}
    backup_counts = {path: len(list(path.parent.glob(f"{path.name}.bak.*"))) for path in targets}
    second = run_install(home)
    assert second.returncode == 0, second.stderr
    assert {path: path.read_bytes() for path in targets} == before
    assert {path: len(list(path.parent.glob(f"{path.name}.bak.*"))) for path in targets} == backup_counts


def test_existing_settings_gets_distinct_generic_and_task_nudge_backups(home, run_install):
    claude = home / ".claude"
    claude.mkdir()
    settings = claude / "settings.json"
    settings.write_text('{"hooks": {}}\n', encoding="utf-8")
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    generic = list(claude.glob("settings.json.bak.[0-9]*"))
    task_nudge = list(claude.glob("settings.json.bak.task-nudge.[0-9]*"))
    assert len(generic) == len(task_nudge) == 1
    assert generic[0].read_text(encoding="utf-8") == '{"hooks": {}}\n'
    assert task_nudge[0] != generic[0]
    before = settings.read_bytes()
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    assert settings.read_bytes() == before
    assert list(claude.glob("settings.json.bak.[0-9]*")) == generic
    assert list(claude.glob("settings.json.bak.task-nudge.[0-9]*")) == task_nudge


def test_nonempty_override_receives_active_agents_block(home, run_install):
    codex = home / ".codex"
    codex.mkdir()
    (codex / "AGENTS.md").write_text("base\n", encoding="utf-8")
    override = codex / "AGENTS.override.md"
    override.write_text("override\n", encoding="utf-8")
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    assert START in override.read_text(encoding="utf-8")
    assert START not in (codex / "AGENTS.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("hooks.json", '{"hooks": {}, "hooks": {}}'),
        ("hooks.json", '{"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "' + CODEX_COMMAND + '"}]}, {"matcher": "Edit", "hooks": [{"type": "command", "command": "' + CODEX_COMMAND + '"}]}]}}'),
    ],
    ids=["duplicate-json", "contradictory-managed-hook"],
)
def test_invalid_codex_input_leaves_task_nudge_targets_unchanged(home, run_install, name, payload):
    codex = home / ".codex"
    codex.mkdir()
    target = codex / name
    target.write_text(payload, encoding="utf-8")
    before = {target: target.read_bytes()}
    result = run_install(home)
    assert result.returncode != 0
    assert {target: target.read_bytes()} == before
    assert not (home / ".local" / "share" / "claude-config" / "hooks").exists()
    assert not (home / ".claude" / "hooks" / "task-nudge.sh").exists()


def test_malformed_agents_marker_leaves_task_nudge_targets_unchanged(home, run_install):
    first = run_install(home)
    assert first.returncode == 0, first.stderr
    codex = home / ".codex"
    agents = codex / "AGENTS.md"
    agents.write_text(START + "\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in _task_targets(home)}
    result = run_install(home)
    assert result.returncode != 0
    assert {path: path.read_bytes() for path in _task_targets(home)} == before


def test_real_task_nudge_change_creates_exactly_one_backup(home, run_install):
    first = run_install(home)
    assert first.returncode == 0, first.stderr
    target = home / ".local" / "share" / "claude-config" / "hooks" / "task_nudge.py"
    target.write_text("stale task nudge\n", encoding="utf-8")
    second = run_install(home)
    assert second.returncode == 0, second.stderr
    backups = list(target.parent.glob("task_nudge.py.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "stale task nudge\n"
    third = run_install(home)
    assert third.returncode == 0, third.stderr
    assert list(target.parent.glob("task_nudge.py.bak.*")) == backups


def test_install_never_runs_provider_canary(home, run_install, tmp_path):
    launcher = home / ".local" / "bin" / "jhw-control-host"
    launcher.parent.mkdir(parents=True)
    canary = tmp_path / "provider-canary.log"
    launcher.write_text("#!/bin/sh\nprintf provider >> \"$CANARY\"\n", encoding="utf-8")
    launcher.chmod(0o700)
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    assert not canary.exists()


def test_install_output_and_targets_contain_no_trust_bypass_literal(home, run_install):
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    installed = b"\n".join(path.read_bytes() for path in _task_targets(home) if path.exists())
    forbidden = (b"trust-bypass", b"auto-trust", b"allow-all", b"managed-policy override")
    assert all(word not in result.stdout.encode() + result.stderr.encode() + installed for word in forbidden)


def test_active_agents_block_contains_all_five_policy_branches(home, run_install):
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    agents = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    branches = [
        "(1) 이미 결정됨·제외 작업",
        "(2) backlog(unknown 포함)",
        "(3) 즉시 작업의 unknown",
        "(4) 등록 저장소의 즉시 작업",
        "(5) 미등록 저장소의 즉시 작업",
    ]
    assert [agents.index(branch) for branch in branches] == sorted(agents.index(branch) for branch in branches)
