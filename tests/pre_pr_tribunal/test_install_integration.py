import json
import os
from pathlib import Path
import stat
import subprocess

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "install.sh"
TASK_CLAUDE = "$HOME/.claude/hooks/task-nudge.sh"
TASK_CODEX = "/usr/bin/python3 $HOME/.local/share/claude-config/hooks/task-nudge-codex.py"
TRIBUNAL_CLAUDE = "/usr/bin/python3 $HOME/.local/share/claude-config/pre_pr_tribunal/claude_hook.py"
TRIBUNAL_CODEX = "/usr/bin/python3 $HOME/.local/share/claude-config/pre_pr_tribunal/codex_hook.py"
SKILL_SOURCE = REPO / "skills/pre-pr-tribunal"
SKILL_TARGETS = (
    Path(".claude/skills/pre-pr-tribunal"),
    Path(".codex/skills/pre-pr-tribunal"),
)


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


def _backup_paths(home: Path) -> dict[str, list[Path]]:
    settings = home / ".claude/settings.json"
    hooks = home / ".codex/hooks.json"
    return {
        "generic-settings": sorted(settings.parent.glob("settings.json.bak.[0-9]*")),
        "task-settings": sorted(settings.parent.glob("settings.json.bak.task-nudge.*")),
        "tribunal-settings": sorted(
            settings.parent.glob("settings.json.bak.pre-pr-tribunal.*")
        ),
        "task-codex": sorted(hooks.parent.glob("hooks.json.bak.task-nudge.*")),
        "tribunal-codex": sorted(
            hooks.parent.glob("hooks.json.bak.pre-pr-tribunal.*")
        ),
    }


def _backup_snapshot(home: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for paths in _backup_paths(home).values()
        for path in paths
    }


def _tribunal_snapshot(home: Path) -> dict[Path, tuple[object, ...]]:
    package = home / ".local/share/claude-config/pre_pr_tribunal"
    paths = [
        *sorted(package.glob("*.py")),
        home / ".claude/settings.json",
        home / ".codex/hooks.json",
        *(home / relative for relative in SKILL_TARGETS),
    ]
    result = {}
    for path in paths:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            result[path] = ("absent",)
        else:
            if stat.S_ISLNK(metadata.st_mode):
                result[path] = ("symlink", os.readlink(path))
            elif stat.S_ISREG(metadata.st_mode):
                result[path] = (
                    "regular",
                    stat.S_IMODE(metadata.st_mode),
                    path.read_bytes(),
                )
            elif stat.S_ISDIR(metadata.st_mode):
                result[path] = (
                    "directory",
                    tuple(sorted(child.name for child in path.iterdir())),
                )
            else:
                result[path] = ("other", stat.S_IFMT(metadata.st_mode))
    return result


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
    assert "[install] Pre-PR tribunal: Claude/Codex Skill + blocking hook" in first.stdout
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
    backups_by_namespace = _backup_paths(home)
    assert {name: len(paths) for name, paths in backups_by_namespace.items()} == {
        "generic-settings": 1,
        "task-settings": 1,
        "tribunal-settings": 1,
        "task-codex": 1,
        "tribunal-codex": 1,
    }
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for paths in backups_by_namespace.values()
        for path in paths
    )

    source_names = sorted(path.name for path in (REPO / "hooks/pre_pr_tribunal").glob("*.py"))
    installed = home / ".local/share/claude-config/pre_pr_tribunal"
    assert sorted(path.name for path in installed.iterdir()) == source_names
    for name in source_names:
        target = installed / name
        assert target.is_file() and not target.is_symlink()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_bytes() == (REPO / "hooks/pre_pr_tribunal" / name).read_bytes()

    for relative in SKILL_TARGETS:
        target = home / relative
        assert target.is_symlink()
        assert target.readlink() == SKILL_SOURCE

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
    backups = _backup_snapshot(home)
    second = _run_install(home)
    assert second.returncode == 0, second.stderr
    assert {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in tracked} == before
    assert _backup_snapshot(home) == backups
    assert _backup_paths(home) == backups_by_namespace
    for relative in SKILL_TARGETS:
        target = home / relative
        assert target.is_symlink() and target.readlink() == SKILL_SOURCE


def test_full_install_preserves_every_tribunal_target_on_conflicting_skill_directory(
    home,
):
    first = _run_install(home)
    assert first.returncode == 0, first.stderr
    conflict = home / ".codex/skills/pre-pr-tribunal"
    conflict.unlink()
    conflict.mkdir()
    (conflict / "sentinel").write_text("keep\n", encoding="utf-8")
    before = _tribunal_snapshot(home)
    backups = _backup_snapshot(home)

    second = _run_install(home)

    assert second.returncode != 0
    assert _tribunal_snapshot(home) == before
    assert _backup_snapshot(home) == backups
    assert (conflict / "sentinel").read_text(encoding="utf-8") == "keep\n"


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
