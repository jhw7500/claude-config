import json
import os
from pathlib import Path
import stat
import subprocess

from conftest import REPO


INSTALL = REPO / "install.sh"
TASK_CLAUDE = "$HOME/.claude/hooks/task-nudge.sh"
TASK_CODEX = "/usr/bin/python3 $HOME/.local/share/claude-config/hooks/task-nudge-codex.py"
TRIBUNAL_CLAUDE = "/usr/bin/python3 $HOME/.local/share/claude-config/pre_pr_tribunal/claude_hook.py"
TRIBUNAL_CODEX = "/usr/bin/python3 $HOME/.local/share/claude-config/pre_pr_tribunal/codex_hook.py"


def _run_install(home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(INSTALL)],
        env=dict(os.environ, HOME=str(home)),
        text=True,
        capture_output=True,
        check=False,
    )


def _groups(path: Path) -> list[dict[str, object]]:
    return json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]


def _matching(groups: list[dict[str, object]], command: str) -> list[dict[str, object]]:
    return [group for group in groups if group.get("hooks") == [{"type": "command", "command": command}]]


def _transaction_backups(home: Path) -> list[Path]:
    return sorted(path for path in home.rglob("*.bak.*") if ".bak.pre-pr-tribunal." in path.name)


def test_full_install_keeps_task_nudge_and_tribunal_hooks_coexisting_once(home):
    claude = home / ".claude"
    codex = home / ".codex"
    claude.mkdir()
    codex.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({"keepClaude": 1, "hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "keep-claude"}]}]}}),
        encoding="utf-8",
    )
    (codex / "hooks.json").write_text(
        json.dumps({"keepCodex": 2, "hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "keep-codex"}]}]}}),
        encoding="utf-8",
    )

    first = _run_install(home)
    assert first.returncode == 0, first.stderr
    assert "[install] Pre-PR tribunal: Claude/Codex blocking hook" in first.stdout
    assert "Codex에서 /hooks" in first.stdout

    claude_data = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    codex_data = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
    assert claude_data["keepClaude"] == 1 and codex_data["keepCodex"] == 2
    assert _matching(claude_data["hooks"]["PreToolUse"], TASK_CLAUDE) == [
        {"matcher": "Edit|Write|NotebookEdit", "hooks": [{"type": "command", "command": TASK_CLAUDE}]}
    ]
    assert _matching(claude_data["hooks"]["PreToolUse"], TRIBUNAL_CLAUDE) == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": TRIBUNAL_CLAUDE}]}
    ]
    assert _matching(codex_data["hooks"]["PreToolUse"], TASK_CODEX) == [
        {"matcher": "apply_patch|Edit|Write", "hooks": [{"type": "command", "command": TASK_CODEX}]}
    ]
    assert _matching(codex_data["hooks"]["PreToolUse"], TRIBUNAL_CODEX) == [
        {"hooks": [{"type": "command", "command": TRIBUNAL_CODEX}]}
    ]

    source_names = sorted(path.name for path in (REPO / "hooks/pre_pr_tribunal").glob("*.py"))
    installed = home / ".local/share/claude-config/pre_pr_tribunal"
    assert sorted(path.name for path in installed.iterdir()) == source_names
    for name in source_names:
        target = installed / name
        assert target.is_file() and not target.is_symlink()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_bytes() == (REPO / "hooks/pre_pr_tribunal" / name).read_bytes()

    hook_result = subprocess.run(
        ["/usr/bin/python3", str(installed / "claude_hook.py")],
        input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "printf safe"}, "cwd": str(home)}),
        text=True,
        capture_output=True,
        check=False,
        env=dict(os.environ, HOME=str(home)),
    )
    assert hook_result.returncode == 0, hook_result.stderr

    tracked = [*(installed / name for name in source_names), claude / "settings.json", codex / "hooks.json"]
    before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in tracked}
    backups = _transaction_backups(home)
    second = _run_install(home)
    assert second.returncode == 0, second.stderr
    assert {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in tracked} == before
    assert _transaction_backups(home) == backups


def test_install_stops_after_tribunal_failure_without_partial_tribunal_targets(home, tmp_path):
    package = home / ".local/share/claude-config/pre_pr_tribunal"
    package.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("outside\n", encoding="utf-8")
    (package / "model.py").symlink_to(outside)

    result = _run_install(home)

    assert result.returncode != 0
    assert "[install] Pre-PR tribunal:" not in result.stdout
    assert "완료. 적용" not in result.stdout
    task_groups = _groups(home / ".claude/settings.json")
    assert len(_matching(task_groups, TASK_CLAUDE)) == 1
    assert (package / "model.py").is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert sorted(package.iterdir()) == [package / "model.py"]
    assert not list(home.rglob(".pre-pr-tribunal-*"))


def test_review_state_ignore_rule_and_comment_appear_exactly_once():
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count("# pre-PR tribunal verdict와 reviewer evidence는 local state다.") == 1
    assert lines.count(".review/") == 1
