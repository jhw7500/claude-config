import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


CLAUDE_COMMAND = "$HOME/.claude/hooks/task-nudge.sh"
CODEX_COMMAND = "/usr/bin/python3 $HOME/.local/share/claude-config/hooks/task-nudge-codex.py"


def _tree_snapshot(root):
    snapshot = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            payload = path.read_bytes()
        elif stat.S_ISLNK(metadata.st_mode):
            payload = os.readlink(path)
        else:
            payload = None
        snapshot[path.relative_to(root)] = (metadata.st_mode, payload)
    return snapshot


def test_merge_hook_config_preserves_unrelated_entries(installer):
    original = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-me"}]}
            ]
        },
        "theme": "dark",
    }
    merged = installer.merge_hook_config(
        original,
        matcher="apply_patch|Edit|Write",
        command=CODEX_COMMAND,
        legacy_commands=(),
        home=Path("/home/test"),
    )
    assert merged["theme"] == "dark"
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "keep-me"
    assert merged["hooks"]["PreToolUse"][1] == {
        "matcher": "apply_patch|Edit|Write",
        "hooks": [{"type": "command", "command": CODEX_COMMAND}],
    }
    assert original["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-me"}]}
    ]


def test_merge_hook_config_migrates_one_legacy_command_without_duplicate(installer):
    original = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": CLAUDE_COMMAND}]}
            ]
        }
    }
    once = installer.merge_hook_config(
        original,
        matcher="Edit|Write|NotebookEdit",
        command=CLAUDE_COMMAND,
        legacy_commands=(CLAUDE_COMMAND,),
        home=Path("/home/test"),
    )
    twice = installer.merge_hook_config(
        once,
        matcher="Edit|Write|NotebookEdit",
        command=CLAUDE_COMMAND,
        legacy_commands=(CLAUDE_COMMAND,),
        home=Path("/home/test"),
    )
    assert once == twice
    assert once["hooks"]["PreToolUse"] == [
        {
            "matcher": "Edit|Write|NotebookEdit",
            "hooks": [{"type": "command", "command": CLAUDE_COMMAND}],
        }
    ]


def test_merge_hook_config_normalizes_only_anchored_home_prefix(installer):
    absolute = "/home/test/.claude/hooks/task-nudge.sh"
    migrated = installer.merge_hook_config(
        {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": absolute}]}]}},
        matcher="Edit|Write|NotebookEdit",
        command=CLAUDE_COMMAND,
        legacy_commands=(CLAUDE_COMMAND,),
        home=Path("/home/test"),
    )
    unrelated = installer.merge_hook_config(
        {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "/opt/home/test/.claude/hooks/task-nudge.sh"}]}]}},
        matcher="Edit|Write|NotebookEdit",
        command=CLAUDE_COMMAND,
        legacy_commands=(CLAUDE_COMMAND,),
        home=Path("/home/test"),
    )
    assert len(migrated["hooks"]["PreToolUse"]) == 1
    assert len(unrelated["hooks"]["PreToolUse"]) == 2


def test_strict_json_loader_rejects_duplicate_keys(installer, tmp_path):
    config = tmp_path / "settings.json"
    config.write_text('{"hooks": {}, "hooks": {}}', encoding="utf-8")
    with pytest.raises(installer.InstallError):
        installer.load_json_config(config)


@pytest.mark.parametrize(
    "group",
    [
        "not-an-object",
        {"matcher": "Write", "hooks": "not-a-list"},
        {"matcher": "Write", "hooks": [{"type": "command"}]},
        {"matcher": "Write", "hooks": [{"type": "mystery", "command": "keep-me"}]},
    ],
)
def test_merge_hook_config_rejects_unknown_hook_record_shapes(installer, group):
    original = {"hooks": {"PreToolUse": [group]}}
    with pytest.raises(installer.InstallError):
        installer.merge_hook_config(
            original,
            matcher="Edit|Write|NotebookEdit",
            command=CLAUDE_COMMAND,
            legacy_commands=(CLAUDE_COMMAND,),
            home=Path("/home/test"),
        )


def test_merge_hook_config_rejects_multi_hook_managed_group(installer):
    original = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write",
                    "hooks": [
                        {"type": "command", "command": CLAUDE_COMMAND},
                        {"type": "command", "command": "keep-me"},
                    ],
                }
            ]
        }
    }
    with pytest.raises(installer.InstallError):
        installer.merge_hook_config(
            original,
            matcher="Edit|Write|NotebookEdit",
            command=CLAUDE_COMMAND,
            legacy_commands=(CLAUDE_COMMAND,),
            home=Path("/home/test"),
        )


def test_merge_hook_config_rejects_duplicate_managed_groups(installer):
    original = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": CLAUDE_COMMAND}]},
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "/home/test/.claude/hooks/task-nudge.sh"}]},
            ]
        }
    }
    with pytest.raises(installer.InstallError):
        installer.merge_hook_config(
            original,
            matcher="Edit|Write|NotebookEdit",
            command=CLAUDE_COMMAND,
            legacy_commands=(CLAUDE_COMMAND,),
            home=Path("/home/test"),
        )


def test_render_json_config_is_stable_and_preserves_key_order(installer):
    value = {"z-local": True, "hooks": {}, "theme": "dark"}
    rendered = installer.render_json_config(value)
    assert rendered == json.dumps(value, ensure_ascii=False, indent=2).encode() + b"\n"


def test_nonempty_override_is_active_agents_target(installer, home):
    codex = home / ".codex"
    codex.mkdir()
    (codex / "AGENTS.md").write_text("base\n", encoding="utf-8")
    override = codex / "AGENTS.override.md"
    override.write_text("override\n", encoding="utf-8")
    assert installer.select_agents_path(home) == override


def test_empty_override_falls_back_to_agents_md(installer, home):
    codex = home / ".codex"
    codex.mkdir()
    (codex / "AGENTS.override.md").write_text(" \n\t", encoding="utf-8")
    assert installer.select_agents_path(home) == codex / "AGENTS.md"


def test_agents_marker_append_preserves_existing_bytes_and_adds_one_newline(installer):
    original = "prefix\n<!-- local block -->\nsuffix"
    merged = installer.merge_agents_block(original, "managed policy\n")
    assert merged.startswith(original + "\n")
    assert merged.count(installer.AGENTS_START) == 1
    assert merged.count(installer.AGENTS_END) == 1
    assert merged.endswith(installer.AGENTS_END + "\n")


def test_agents_marker_replace_preserves_bytes_outside_pair(installer):
    original = (
        "prefix-without-normalization\n"
        "<!-- claude-config:task-nudge:START -->old\r\n"
        "<!-- claude-config:task-nudge:END -->suffix-without-newline"
    )
    merged = installer.merge_agents_block(original, "new policy\n")
    assert merged.startswith("prefix-without-normalization\n" + installer.AGENTS_START)
    assert merged.endswith(installer.AGENTS_END + "suffix-without-newline")
    assert "old\r\n" not in merged


@pytest.mark.parametrize(
    "text",
    [
        "<!-- claude-config:task-nudge:START -->\n",
        "<!-- claude-config:task-nudge:END -->\n",
        "<!-- claude-config:task-nudge:END -->\n<!-- claude-config:task-nudge:START -->\n",
        (
            "<!-- claude-config:task-nudge:START -->\na\n"
            "<!-- claude-config:task-nudge:START -->\nb\n"
            "<!-- claude-config:task-nudge:END -->\n"
        ),
        (
            "<!-- claude-config:task-nudge:START -->\na\n"
            "<!-- claude-config:task-nudge:END -->\nb\n"
            "<!-- claude-config:task-nudge:END -->\n"
        ),
    ],
)
def test_malformed_agents_markers_are_rejected(installer, text):
    with pytest.raises(installer.InstallError):
        installer.merge_agents_block(text, "managed policy\n")


def test_agents_policy_matches_approved_task_three_precedence(installer):
    policy = installer.agents_policy_block()
    command = (
        '/usr/bin/python3 $HOME/.local/share/claude-config/hooks/'
        'task-nudge-codex.py --manual-check --cwd "$PWD"'
    )
    assert command in policy
    assert "[TASK-NUDGE]" in policy and "이미 결정" in policy
    order = [
        "(1) 이미 결정됨·제외 작업",
        "(2) backlog(unknown 포함)",
        "(3) 즉시 작업의 unknown",
        "(4) 등록 저장소의 즉시 작업",
        "(5) 미등록 저장소의 즉시 작업",
    ]
    assert [policy.index(clause) for clause in order] == sorted(policy.index(clause) for clause in order)
    evidence = [
        "사용자의 장기·반복·여러 세션 명시",
        "기존 GitHub Issue·승인된 계획·Handoff",
        "여러 구현 단계와 검증이 필요한 아키텍처 작업",
    ]
    assert all(policy.count(clause) == 1 for clause in evidence)
    approvals = ["GitHub Issue 생성", "Project/Repository 등록", "Formal 또는 Temporary Task 시작"]
    assert all(policy.count(clause) >= 1 for clause in approvals)
    assert "각각 별도의 명시적 사용자 승인" in policy
    assert policy.index("backlog(unknown 포함)") < policy.index("즉시 작업의 unknown")


def _source_repo(tmp_path, *, canary="source-canary"):
    source = tmp_path / "source-repo"
    hooks = source / "hooks"
    hooks.mkdir(parents=True)
    files = {
        "task_nudge.py": f"# core {canary}\n",
        "task-nudge-claude.py": f"# claude {canary}\n",
        "task-nudge-codex.py": f"# codex {canary}\n",
        "task-nudge.sh": f"#!/bin/sh\n# shim {canary}\n",
    }
    for name, body in files.items():
        (hooks / name).write_text(body, encoding="utf-8")
    return source, files


def test_build_plan_uses_explicit_repo_and_exact_targets(installer, tmp_path, home):
    source, files = _source_repo(tmp_path, canary="explicit-repo")
    plans = installer.build_plan(source, home)
    by_path = {plan.path: plan for plan in plans}
    installed = home / ".local" / "share" / "claude-config" / "hooks"
    expected = {
        installed / "task_nudge.py": (files["task_nudge.py"].encode(), 0o600),
        installed / "task-nudge-claude.py": (files["task-nudge-claude.py"].encode(), 0o600),
        installed / "task-nudge-codex.py": (files["task-nudge-codex.py"].encode(), 0o600),
        home / ".claude" / "hooks" / "task-nudge.sh": (files["task-nudge.sh"].encode(), 0o700),
    }
    assert set(expected).issubset(by_path)
    for path, (data, mode) in expected.items():
        assert by_path[path].data == data
        assert by_path[path].mode == mode
    assert by_path[home / ".claude" / "settings.json"].mode == 0o600
    assert by_path[home / ".codex" / "hooks.json"].mode == 0o600
    agents = by_path[home / ".codex" / "AGENTS.md"]
    assert agents.mode == 0o600 and installer.AGENTS_START.encode() in agents.data
    assert len(plans) == 7


def test_build_plan_preserves_configs_and_uses_active_agents(installer, tmp_path, home):
    source, _ = _source_repo(tmp_path)
    claude = home / ".claude"
    codex = home / ".codex"
    claude.mkdir()
    codex.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({"theme": "dark", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "keep"}]}]}}),
        encoding="utf-8",
    )
    (codex / "hooks.json").write_text(json.dumps({"local": [1, 2]}), encoding="utf-8")
    (codex / "AGENTS.md").write_text("base\n", encoding="utf-8")
    override = codex / "AGENTS.override.md"
    override.write_text("override\n", encoding="utf-8")
    plans = {plan.path: plan for plan in installer.build_plan(source, home)}
    claude_json = json.loads(plans[claude / "settings.json"].data)
    codex_json = json.loads(plans[codex / "hooks.json"].data)
    assert claude_json["theme"] == "dark"
    assert claude_json["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "keep"
    assert codex_json["local"] == [1, 2]
    assert override in plans and codex / "AGENTS.md" not in plans
    assert plans[override].data.startswith(b"override\n")


@pytest.mark.parametrize("missing", ["task_nudge.py", "task-nudge-claude.py", "task-nudge-codex.py", "task-nudge.sh"])
def test_build_plan_rejects_each_missing_source_without_writes(installer, tmp_path, home, missing):
    source, _ = _source_repo(tmp_path)
    (source / "hooks" / missing).unlink()
    before = sorted(home.rglob("*"))
    with pytest.raises(installer.InstallError):
        installer.build_plan(source, home)
    assert sorted(home.rglob("*")) == before


def test_build_plan_rejects_symlinked_source_without_writes(installer, tmp_path, home):
    source, _ = _source_repo(tmp_path)
    source_file = source / "hooks" / "task_nudge.py"
    outside = tmp_path / "outside-source"
    outside.write_bytes(source_file.read_bytes())
    source_file.unlink()
    source_file.symlink_to(outside)
    before = sorted(home.rglob("*"))
    with pytest.raises(installer.InstallError):
        installer.build_plan(source, home)
    assert sorted(home.rglob("*")) == before


def test_build_plan_rejects_invalid_target_json_and_agents_before_writes(installer, tmp_path, home):
    source, _ = _source_repo(tmp_path)
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    (home / ".claude" / "settings.json").write_text('{"hooks": {}, "hooks": {}}', encoding="utf-8")
    (home / ".codex" / "AGENTS.md").write_text(installer.AGENTS_START + "\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    with pytest.raises(installer.InstallError):
        installer.build_plan(source, home)
    assert {path: path.read_bytes() for path in home.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("target", ["claude", "codex", "agents"])
def test_build_plan_independently_preflights_every_config_target(installer, tmp_path, home, target):
    source, _ = _source_repo(tmp_path)
    claude = home / ".claude"
    codex = home / ".codex"
    claude.mkdir()
    codex.mkdir()
    if target == "claude":
        (claude / "settings.json").write_text('{"a": 1, "a": 2}', encoding="utf-8")
    elif target == "codex":
        (codex / "hooks.json").write_text('{"a": 1, "a": 2}', encoding="utf-8")
    else:
        (codex / "AGENTS.md").write_text(installer.AGENTS_END + "\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    with pytest.raises(installer.InstallError):
        installer.build_plan(source, home)
    assert {path: path.read_bytes() for path in home.rglob("*") if path.is_file()} == before


def test_apply_rejects_existing_target_content_change_since_build_without_artifacts(
    installer, tmp_path, home
):
    source, _ = _source_repo(tmp_path)
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"theme":"before"}\n', encoding="utf-8")
    settings.chmod(0o600)
    plans = installer.build_plan(source, home)

    settings.write_text('{"theme":"concurrent"}\n', encoding="utf-8")
    before_apply = _tree_snapshot(home)
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(plans, stamp="20260831010101")

    assert _tree_snapshot(home) == before_apply


def test_apply_rejects_target_that_appeared_since_build_without_artifacts(
    installer, tmp_path, home
):
    source, _ = _source_repo(tmp_path)
    plans = installer.build_plan(source, home)
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir()
    hooks.write_text('{"concurrent":true}\n', encoding="utf-8")
    hooks.chmod(0o600)

    before_apply = _tree_snapshot(home)
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(plans, stamp="20260831010101")

    assert _tree_snapshot(home) == before_apply


def test_apply_rejects_target_identity_change_since_build_without_artifacts(
    installer, tmp_path, home
):
    source, _ = _source_repo(tmp_path)
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"theme":"before"}\n', encoding="utf-8")
    settings.chmod(0o600)
    plans = installer.build_plan(source, home)

    original = settings.read_bytes()
    settings.rename(tmp_path / "held-original-settings")
    settings.write_bytes(original)
    settings.chmod(0o600)
    before_apply = _tree_snapshot(home)
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(plans, stamp="20260831010101")

    assert _tree_snapshot(home) == before_apply


@pytest.mark.parametrize("initial_override", ["absent", "empty"])
def test_apply_rejects_inactive_override_becoming_nonempty_since_build_without_artifacts(
    installer, tmp_path, home, initial_override
):
    source, _ = _source_repo(tmp_path)
    codex = home / ".codex"
    codex.mkdir()
    agents = codex / "AGENTS.md"
    override = codex / "AGENTS.override.md"
    agents.write_bytes(b"base agents\n")
    agents.chmod(0o600)
    if initial_override == "empty":
        override.write_bytes(b"")
        override.chmod(0o600)
    plans = installer.build_plan(source, home)

    override.write_bytes(b"concurrent active override\n")
    override.chmod(0o600)
    agents_before = agents.read_bytes()
    override_before = override.read_bytes()
    before_apply = _tree_snapshot(home)
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(plans, stamp="20260831010101")

    assert agents.read_bytes() == agents_before
    assert override.read_bytes() == override_before
    assert _tree_snapshot(home) == before_apply


@pytest.mark.parametrize(
    ("initial_override", "transition"),
    [
        ("absent", "appear-empty"),
        ("empty", "disappear"),
        ("empty", "change-bytes"),
        ("empty", "change-mode"),
        ("empty", "replace-identity"),
        ("empty", "replace-type"),
        ("nonempty", "become-empty"),
    ],
)
def test_apply_rejects_agents_selector_state_drift_without_artifacts(
    installer, tmp_path, home, initial_override, transition
):
    source, _ = _source_repo(tmp_path)
    codex = home / ".codex"
    codex.mkdir()
    agents = codex / "AGENTS.md"
    override = codex / "AGENTS.override.md"
    agents.write_bytes(b"base agents\n")
    agents.chmod(0o600)
    if initial_override == "empty":
        override.write_bytes(b"")
        override.chmod(0o600)
    elif initial_override == "nonempty":
        override.write_bytes(b"active override\n")
        override.chmod(0o600)
    plans = installer.build_plan(source, home)

    if transition == "appear-empty":
        override.write_bytes(b"")
        override.chmod(0o600)
    elif transition == "disappear":
        override.unlink()
    elif transition == "change-bytes":
        override.write_bytes(b" \n")
    elif transition == "change-mode":
        override.chmod(0o640)
    elif transition == "replace-identity":
        override.rename(tmp_path / "held-original-override")
        override.write_bytes(b"")
        override.chmod(0o600)
    elif transition == "replace-type":
        override.rename(tmp_path / "held-regular-override")
        override.symlink_to(tmp_path / "held-regular-override")
    elif transition == "become-empty":
        override.write_bytes(b"")
    else:
        raise AssertionError("unhandled selector transition")

    before_apply = _tree_snapshot(home)
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(plans, stamp="20260831010101")

    assert _tree_snapshot(home) == before_apply


@pytest.mark.parametrize("selector_state", ["absent", "empty", "nonempty"])
def test_apply_accepts_stable_agents_selector_and_is_idempotent(
    installer, tmp_path, home, selector_state
):
    source, _ = _source_repo(tmp_path)
    codex = home / ".codex"
    codex.mkdir()
    agents = codex / "AGENTS.md"
    override = codex / "AGENTS.override.md"
    agents.write_bytes(b"base agents\n")
    agents.chmod(0o600)
    if selector_state == "empty":
        override.write_bytes(b"")
        override.chmod(0o600)
    elif selector_state == "nonempty":
        override.write_bytes(b"active override\n")
        override.chmod(0o600)

    plans = installer.build_plan(source, home)
    active = override if selector_state == "nonempty" else agents
    changed = installer.apply_transaction(plans, stamp="20260831010101")
    assert active in changed
    assert installer.AGENTS_START.encode() in active.read_bytes()
    if selector_state == "nonempty":
        assert installer.AGENTS_START.encode() not in agents.read_bytes()
    elif selector_state == "empty":
        assert override.read_bytes() == b""
    else:
        assert not override.exists()

    second = installer.build_plan(source, home)
    assert installer.apply_transaction(second, stamp="20260831010102") == []


def test_transaction_rolls_back_updated_and_created_targets(installer, tmp_path):
    existing = tmp_path / "a-settings.json"
    created = tmp_path / "b-hooks.json"
    trigger = tmp_path / "c-AGENTS.md"
    existing.write_bytes(b"before\n")
    existing.chmod(0o640)
    writes = [
        installer.PlannedWrite(existing, b"after\n", 0o600, True),
        installer.PlannedWrite(created, b"created\n", 0o600, True),
        installer.PlannedWrite(trigger, b"trigger\n", 0o600, True),
    ]
    calls = 0
    real_replace = installer.os.replace

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected replace failure")
        real_replace(source, target)

    with pytest.raises(installer.InstallError) as caught:
        installer.apply_transaction(writes, replace=fail_second, stamp="20260831010101")
    assert existing.read_bytes() == b"before\n"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640
    assert not created.exists()
    assert not trigger.exists()
    assert list(tmp_path.glob("*.bak.*")) == []
    assert "before" not in str(caught.value) and "after" not in str(caught.value)


def test_identical_regular_plan_creates_no_backup(installer, tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"same\n")
    target.chmod(0o600)
    changed = installer.apply_transaction(
        [installer.PlannedWrite(target, b"same\n", 0o600, True)],
        stamp="20260831010101",
    )
    assert changed == []
    assert list(tmp_path.glob("AGENTS.md.bak.*")) == []


def test_mode_difference_is_a_real_change_with_private_backup(installer, tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes(b"same\n")
    target.chmod(0o640)
    installer.apply_transaction(
        [installer.PlannedWrite(target, b"same\n", 0o600, True)],
        stamp="20260831010101",
    )
    backup = tmp_path / "settings.json.bak.task-nudge.20260831010101"
    assert target.read_bytes() == backup.read_bytes() == b"same\n"
    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_transaction_stages_all_owner_only_files_before_ordered_replace(installer, tmp_path):
    later = tmp_path / "z" / "later"
    first = tmp_path / "a" / "first"
    observed = []
    real_replace = installer.os.replace

    def inspect_replace(source, target):
        observed.append((Path(target), stat.S_IMODE(Path(source).stat().st_mode)))
        assert later.parent.exists() and first.parent.exists()
        real_replace(source, target)

    installer.apply_transaction(
        [
            installer.PlannedWrite(later, b"later", 0o700, True),
            installer.PlannedWrite(first, b"first", 0o600, True),
        ],
        replace=inspect_replace,
        stamp="20260831010101",
    )
    assert observed == [(first, 0o600), (later, 0o700)]
    assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(later.parent.stat().st_mode) == 0o700


def test_backup_collision_fails_before_any_replacement(installer, tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    for target in (first, second):
        target.write_bytes(b"old")
        target.chmod(0o600)
    collision = tmp_path / "b.bak.task-nudge.20260831010101"
    collision.write_bytes(b"do-not-touch")
    calls = []

    def record_replace(source, target):
        calls.append((source, target))
        os.replace(source, target)

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [
                installer.PlannedWrite(first, b"new-a", 0o600, True),
                installer.PlannedWrite(second, b"new-b", 0o600, True),
            ],
            replace=record_replace,
            stamp="20260831010101",
        )
    assert calls == []
    assert first.read_bytes() == second.read_bytes() == b"old"
    assert collision.read_bytes() == b"do-not-touch"


def test_arbitrary_symlink_target_fails_closed(installer, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    target = tmp_path / "hooks.json"
    target.symlink_to(outside)
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"new", 0o600, True)],
            stamp="20260831010101",
        )
    assert target.is_symlink() and outside.read_bytes() == b"outside"


def test_legacy_symlink_exception_cannot_be_reused_for_an_arbitrary_target(installer, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    target = tmp_path / "task-nudge.sh"
    target.symlink_to(outside)
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"new", 0o700, True, allow_legacy_symlink=True)],
            stamp="20260831010101",
        )
    assert target.is_symlink() and outside.read_bytes() == b"outside"


def test_replace_that_completes_then_raises_is_rolled_back(installer, tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes(b"old")
    target.chmod(0o640)

    def replace_then_raise(source, destination):
        os.replace(source, destination)
        raise OSError("post-replace failure")

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"new", 0o600, True)],
            replace=replace_then_raise,
            stamp="20260831010101",
        )
    assert target.read_bytes() == b"old"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not list(tmp_path.glob("*.bak.*"))


def test_legacy_shim_symlink_migrates_once_to_regular_file(installer, tmp_path, home):
    source, _ = _source_repo(tmp_path)
    shim = home / ".claude" / "hooks" / "task-nudge.sh"
    shim.parent.mkdir(parents=True)
    legacy = tmp_path / "legacy-shim"
    legacy.write_bytes(b"legacy\n")
    shim.symlink_to(legacy)
    installer.apply_transaction(installer.build_plan(source, home), stamp="20260831010101")
    assert shim.is_file() and not shim.is_symlink()
    assert stat.S_IMODE(shim.stat().st_mode) == 0o700
    backup = shim.parent / "task-nudge.sh.bak.task-nudge.20260831010101"
    assert backup.read_bytes() == b"legacy\n" and stat.S_IMODE(backup.stat().st_mode) == 0o600
    before = sorted(shim.parent.glob("task-nudge.sh.bak.*"))
    installer.apply_transaction(installer.build_plan(source, home), stamp="20260831020202")
    assert sorted(shim.parent.glob("task-nudge.sh.bak.*")) == before


def test_cli_uses_temporary_home_and_never_invokes_launcher(installer, tmp_path, home):
    source, _ = _source_repo(tmp_path, canary="cli-source")
    launcher = home / ".local" / "bin" / "jhw-control-host"
    launcher.parent.mkdir(parents=True)
    sentinel = home / "launcher-was-called"
    launcher.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    launcher.chmod(0o700)
    env = dict(os.environ, HOME=str(home), TMPDIR=str(home / "scratch"))
    result = subprocess.run(
        [sys.executable, str(installer.REPO / "scripts" / "install-task-nudge.py"), "--repo", str(source)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    assert "/hooks" in result.stdout and "trust" in result.stdout.lower()
    assert not sentinel.exists()
    installed = home / ".local" / "share" / "claude-config" / "hooks" / "task_nudge.py"
    assert installed.read_text(encoding="utf-8") == "# core cli-source\n"


def test_cli_rejects_relative_repo_without_target_writes(installer, home):
    env = dict(os.environ, HOME=str(home), TMPDIR=str(home / "scratch"))
    result = subprocess.run(
        [sys.executable, str(installer.REPO / "scripts" / "install-task-nudge.py"), "--repo", "relative"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert not (home / ".claude").exists() and not (home / ".codex").exists()


def test_cli_source_validation_failure_is_no_write(installer, tmp_path, home):
    source, _ = _source_repo(tmp_path)
    (source / "hooks" / "task-nudge-codex.py").unlink()
    env = dict(os.environ, HOME=str(home), TMPDIR=str(home / "scratch"))
    before = sorted(home.rglob("*"))
    result = subprocess.run(
        [sys.executable, str(installer.REPO / "scripts" / "install-task-nudge.py"), "--repo", str(source)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert sorted(home.rglob("*")) == before


def test_raced_backup_collision_is_not_unlinked_or_replaced(installer, tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes(b"original")
    target.chmod(0o600)
    raced_backup = tmp_path / "settings.json.bak.task-nudge.20260831010101"
    replace_calls = []

    def race(phase, path):
        if phase == "before_backup_create":
            path.write_bytes(b"raced-owner-data")

    def replace(source, destination):
        replace_calls.append((source, destination))
        os.replace(source, destination)

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"updated", 0o600, True)],
            replace=replace,
            stamp="20260831010101",
            phase_hook=race,
        )
    assert target.read_bytes() == b"original"
    assert raced_backup.read_bytes() == b"raced-owner-data"
    assert replace_calls == []


def test_source_swap_between_validation_and_open_fails_closed(installer, tmp_path, home):
    source, _ = _source_repo(tmp_path)
    victim = source / "hooks" / "task_nudge.py"
    outside = tmp_path / "outside-source"
    outside.write_bytes(b"outside-source-data")

    def swap(path):
        if path == victim:
            path.unlink()
            path.symlink_to(outside)

    with pytest.raises(installer.InstallError):
        installer.build_plan(source, home, before_source_open=swap)
    assert victim.is_symlink()
    assert outside.read_bytes() == b"outside-source-data"
    assert not (home / ".local" / "share" / "claude-config").exists()


def test_source_parent_swap_cannot_redirect_descriptor_read(installer, tmp_path, home):
    source, files = _source_repo(tmp_path, canary="validated-source")
    hooks = source / "hooks"
    moved_hooks = source / "validated-hooks"
    outside_hooks = tmp_path / "outside-hooks"
    outside_hooks.mkdir()
    for name in files:
        (outside_hooks / name).write_text("outside-substitution\n", encoding="utf-8")
    swapped = False

    def swap(path):
        nonlocal swapped
        if not swapped:
            hooks.rename(moved_hooks)
            hooks.symlink_to(outside_hooks, target_is_directory=True)
            swapped = True

    plans = installer.build_plan(source, home, before_source_open=swap)
    installed_core = home / ".local" / "share" / "claude-config" / "hooks" / "task_nudge.py"
    by_path = {plan.path: plan.data for plan in plans}
    assert by_path[installed_core] == b"# core validated-source\n"
    assert (outside_hooks / "task_nudge.py").read_bytes() == b"outside-substitution\n"


def test_existing_target_swap_before_backup_is_preserved(installer, tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes(b"original")
    target.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")

    def swap(phase, path):
        if phase == "before_backup_revalidate":
            path.unlink()
            path.symlink_to(outside)

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"updated", 0o600, True)],
            stamp="20260831010101",
            phase_hook=swap,
        )
    assert target.is_symlink() and target.resolve() == outside
    assert outside.read_bytes() == b"outside"
    assert not (tmp_path / "settings.json.bak.task-nudge.20260831010101").exists()


def test_existing_regular_swap_before_replace_is_preserved(installer, tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes(b"original")
    target.chmod(0o600)

    def swap(phase, path):
        if phase == "before_replace_revalidate":
            path.unlink()
            path.write_bytes(b"substituted")
            path.chmod(0o600)

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"updated", 0o600, True)],
            stamp="20260831010101",
            phase_hook=swap,
        )
    assert target.read_bytes() == b"substituted"
    assert not (tmp_path / "settings.json.bak.task-nudge.20260831010101").exists()


@pytest.mark.parametrize("replacement_type", ["symlink", "directory"])
def test_missing_target_type_swap_before_replace_is_preserved(installer, tmp_path, replacement_type):
    target = tmp_path / "hooks.json"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")

    def swap(phase, path):
        if phase != "before_replace_revalidate":
            return
        if replacement_type == "symlink":
            path.symlink_to(outside)
        else:
            path.mkdir()

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"created", 0o600, True)],
            stamp="20260831010101",
            phase_hook=swap,
        )
    if replacement_type == "symlink":
        assert target.is_symlink() and target.resolve() == outside
    else:
        assert target.is_dir()
    assert outside.read_bytes() == b"outside"


def test_destination_parent_swap_cannot_redirect_production_replace(installer, tmp_path):
    parent = tmp_path / "target-parent"
    parent.mkdir()
    target = parent / "settings.json"
    target.write_bytes(b"original")
    target.chmod(0o600)
    moved_parent = tmp_path / "validated-parent"
    outside_parent = tmp_path / "outside-parent"
    outside_parent.mkdir()
    outside_target = outside_parent / "settings.json"
    outside_target.write_bytes(b"substituted")

    def swap(phase, path):
        if phase == "before_replace_revalidate":
            parent.rename(moved_parent)
            parent.symlink_to(outside_parent, target_is_directory=True)

    installer.apply_transaction(
        [installer.PlannedWrite(target, b"updated", 0o600, True)],
        stamp="20260831010101",
        phase_hook=swap,
    )
    assert (moved_parent / "settings.json").read_bytes() == b"updated"
    assert outside_target.read_bytes() == b"substituted"


def test_success_fsyncs_regular_files_and_parent_directories(installer, tmp_path, monkeypatch):
    observed = []
    real_fsync = installer.os.fsync

    def observe(descriptor):
        observed.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(installer.os, "fsync", observe)
    target = tmp_path / "nested" / "hooks.json"
    installer.apply_transaction(
        [installer.PlannedWrite(target, b"created", 0o600, True)],
        stamp="20260831010101",
    )
    assert stat.S_IFREG in observed
    assert stat.S_IFDIR in observed


def test_rollback_fsyncs_parent_directories(installer, tmp_path, monkeypatch):
    existing = tmp_path / "a-settings.json"
    created = tmp_path / "b-hooks.json"
    trigger = tmp_path / "c-trigger"
    existing.write_bytes(b"original")
    existing.chmod(0o640)
    observed = []
    real_fsync = installer.os.fsync
    real_replace = installer.os.replace
    replacements = 0

    def observe(descriptor):
        observed.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    def fail_third(source, destination):
        nonlocal replacements
        replacements += 1
        if replacements == 3:
            raise OSError("injected")
        real_replace(source, destination)

    monkeypatch.setattr(installer.os, "fsync", observe)
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [
                installer.PlannedWrite(existing, b"updated", 0o600, True),
                installer.PlannedWrite(created, b"created", 0o600, True),
                installer.PlannedWrite(trigger, b"trigger", 0o600, True),
            ],
            replace=fail_third,
            stamp="20260831010101",
        )
    assert existing.read_bytes() == b"original"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640
    assert not created.exists() and not trigger.exists()
    assert stat.S_IFDIR in observed


def test_directory_fsync_failure_is_bounded_and_rolls_back(installer, tmp_path, monkeypatch):
    target = tmp_path / "hooks.json"
    calls = 0
    real_fsync = installer.os.fsync

    def fail_first_directory(descriptor):
        nonlocal calls
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and calls == 0:
            calls += 1
            raise OSError("directory fsync injection")
        real_fsync(descriptor)

    monkeypatch.setattr(installer.os, "fsync", fail_first_directory)
    with pytest.raises(installer.InstallError) as caught:
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"created", 0o600, True)],
            stamp="20260831010101",
        )
    assert not target.exists()
    assert "directory fsync injection" not in str(caught.value)


@pytest.mark.parametrize("substitution_type", ["regular", "symlink", "directory"])
def test_post_claim_substitution_is_preserved_without_losing_original(
    installer, tmp_path, substitution_type
):
    target = tmp_path / "settings.json"
    target.write_bytes(b"original-target")
    target.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-target")

    def inject(phase, path):
        if phase != "after_target_claim":
            return
        if substitution_type == "regular":
            path.write_bytes(b"unowned-substitution")
            path.chmod(0o600)
        elif substitution_type == "symlink":
            path.symlink_to(outside)
        else:
            path.mkdir()

    with pytest.raises(installer.InstallError) as caught:
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"installed", 0o600, True)],
            stamp="20260831010101",
            phase_hook=inject,
        )
    if substitution_type == "regular":
        assert target.read_bytes() == b"unowned-substitution"
    elif substitution_type == "symlink":
        assert target.is_symlink() and os.readlink(target) == str(outside)
        assert outside.read_bytes() == b"outside-target"
    else:
        assert target.is_dir()
    quarantines = list(tmp_path.glob(".task-nudge-quarantine.*"))
    assert len(quarantines) == 1
    assert quarantines[0].is_file() and quarantines[0].read_bytes() == b"original-target"
    assert "unowned-substitution" not in str(caught.value)


def test_post_claim_substitution_preserves_legacy_symlink_quarantine(installer, tmp_path, home):
    shim = home / ".claude" / "hooks" / "task-nudge.sh"
    shim.parent.mkdir(parents=True)
    legacy = tmp_path / "legacy-shim"
    legacy.write_bytes(b"legacy")
    shim.symlink_to(legacy)

    def inject(phase, path):
        if phase == "after_target_claim":
            path.write_bytes(b"unowned")
            path.chmod(0o600)

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [
                installer.PlannedWrite(
                    shim,
                    b"installed",
                    0o700,
                    True,
                    allow_legacy_symlink=True,
                )
            ],
            stamp="20260831010101",
            phase_hook=inject,
        )
    assert shim.read_bytes() == b"unowned"
    quarantines = list(shim.parent.glob(".task-nudge-quarantine.*"))
    assert len(quarantines) == 1 and quarantines[0].is_symlink()
    assert os.readlink(quarantines[0]) == str(legacy)
    assert quarantines[0].read_bytes() == b"legacy"


def test_post_claim_substitution_of_missing_target_is_not_overwritten(installer, tmp_path):
    target = tmp_path / "hooks.json"

    def inject(phase, path):
        if phase == "after_target_claim":
            path.write_bytes(b"unowned-new-entry")
            path.chmod(0o600)

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"installed", 0o600, True)],
            stamp="20260831010101",
            phase_hook=inject,
        )
    assert target.read_bytes() == b"unowned-new-entry"
    assert not list(tmp_path.glob(".task-nudge-quarantine.*"))


def test_substitution_atomically_captured_by_claim_is_restored_and_rejected(installer, tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes(b"original-target")
    target.chmod(0o600)
    displaced_original = tmp_path / "externally-displaced-original"

    def inject(phase, path):
        if phase == "before_target_claim":
            path.rename(displaced_original)
            path.write_bytes(b"captured-substitution")
            path.chmod(0o600)

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [installer.PlannedWrite(target, b"installed", 0o600, True)],
            stamp="20260831010101",
            phase_hook=inject,
        )
    assert target.read_bytes() == b"captured-substitution"
    assert displaced_original.read_bytes() == b"original-target"
    assert not list(tmp_path.glob(".task-nudge-quarantine.*"))


def test_existing_fifo_source_is_rejected_promptly_without_writes(installer, tmp_path, home):
    source, _ = _source_repo(tmp_path)
    fifo = source / "hooks" / "task_nudge.py"
    fifo.unlink()
    os.mkfifo(fifo)
    env = dict(os.environ, HOME=str(home), TMPDIR=str(home / "scratch"))
    result = subprocess.run(
        [sys.executable, str(installer.REPO / "scripts" / "install-task-nudge.py"), "--repo", str(source)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=1.0,
    )
    assert result.returncode != 0
    assert str(fifo) not in result.stderr
    assert not (home / ".local" / "share" / "claude-config").exists()


def test_regular_source_raced_to_fifo_is_rejected_promptly(installer, tmp_path, home):
    source, _ = _source_repo(tmp_path)
    program = """
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("fifo_race_installer", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
victim = Path(sys.argv[2]) / "hooks" / "task_nudge.py"
def swap(path):
    if path == victim:
        path.unlink()
        os.mkfifo(path)
try:
    module.build_plan(Path(sys.argv[2]), Path(sys.argv[3]), before_source_open=swap)
except module.InstallError as error:
    print(str(error))
    raise SystemExit(7)
raise SystemExit(0)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(installer.REPO / "scripts" / "install-task-nudge.py"),
            str(source),
            str(home),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=1.0,
    )
    assert result.returncode == 7
    assert str(source) not in result.stdout + result.stderr
    assert not (home / ".local" / "share" / "claude-config").exists()
