import os
from pathlib import Path
import stat

import pytest


def test_matcherless_group_omits_matcher_and_preserves_unrelated(common_installer):
    original = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "keep"}],
                }
            ]
        },
        "theme": "dark",
    }
    merged = common_installer.merge_pre_tool_hook(
        original,
        matcher=None,
        command=(
            "/usr/bin/python3 "
            "$HOME/.local/share/claude-config/pre_pr_tribunal/codex_hook.py"
        ),
        legacy_commands=(),
        home=Path("/home/test"),
    )
    assert merged["theme"] == "dark"
    assert merged["hooks"]["PreToolUse"][-1] == {
        "hooks": [
            {
                "type": "command",
                "command": (
                    "/usr/bin/python3 "
                    "$HOME/.local/share/claude-config/pre_pr_tribunal/codex_hook.py"
                ),
            }
        ]
    }
    assert original["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "keep"


def test_matcherless_group_replaces_one_known_legacy_matcher(common_installer):
    command = "$HOME/.local/share/claude-config/pre_pr_tribunal/codex_hook.py"
    original = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "old matcher",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }

    merged = common_installer.merge_pre_tool_hook(
        original,
        matcher=None,
        command=command,
        legacy_commands=(),
        home=Path("/home/test"),
    )

    assert merged == {
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": command}]}
            ]
        }
    }


def test_managed_group_with_unknown_fields_is_rejected(common_installer):
    command = "$HOME/.local/share/claude-config/pre_pr_tribunal/codex_hook.py"
    original = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "old matcher",
                    "hooks": [{"type": "command", "command": command}],
                    "extra": True,
                }
            ]
        }
    }

    with pytest.raises(common_installer.InstallError, match="unknown fields"):
        common_installer.merge_pre_tool_hook(
            original,
            matcher=None,
            command=command,
            legacy_commands=(),
            home=Path("/home/test"),
        )


def test_exact_symlink_is_idempotent_and_wrong_target_is_rejected(
    common_installer, tmp_path
):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "home" / ".codex" / "skills" / "pre-pr-tribunal"
    entry = common_installer.PlannedSymlink(path=target, target=source)
    assert common_installer.apply_transaction(
        [entry], namespace="pre-pr-tribunal"
    ) == [target]
    assert target.is_symlink() and target.readlink() == source
    assert common_installer.apply_transaction(
        [entry], namespace="pre-pr-tribunal"
    ) == []
    target.unlink()
    target.symlink_to(tmp_path / "other")
    with pytest.raises(common_installer.InstallError, match="symlink target conflicts"):
        common_installer.apply_transaction([entry], namespace="pre-pr-tribunal")
    assert target.readlink() == tmp_path / "other"


def test_invalid_namespace_fails_before_creating_target_parents(
    common_installer, tmp_path
):
    target = tmp_path / "new" / "tree" / "settings.json"

    with pytest.raises(common_installer.InstallError, match="invalid namespace"):
        common_installer.apply_transaction(
            [common_installer.PlannedWrite(target, b"new", 0o600, False)],
            namespace="../unsafe",
        )

    assert not (tmp_path / "new").exists()


def test_namespace_controls_backup_name(common_installer, tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes(b"old")
    target.chmod(0o640)

    assert common_installer.apply_transaction(
        [common_installer.PlannedWrite(target, b"new", 0o600, True)],
        namespace="pre-pr-tribunal",
        stamp="20260901000000",
    ) == [target]

    backup = tmp_path / "settings.json.bak.pre-pr-tribunal.20260901000000"
    assert target.read_bytes() == b"new"
    assert backup.read_bytes() == b"old"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_mixed_transaction_rolls_back_files_symlink_modes_and_new_tree(
    common_installer, tmp_path
):
    first = tmp_path / "b" / "first.json"
    second = tmp_path / "c" / "second.json"
    for parent in (first.parent, second.parent):
        parent.mkdir()
    first.write_bytes(b"first-original")
    first.chmod(0o640)
    second.write_bytes(b"second-original")
    second.chmod(0o600)

    source = tmp_path / "skill-source"
    source.mkdir()
    link = tmp_path / "a-new-tree" / "skills" / "pre-pr-tribunal"
    entries = [
        common_installer.PlannedWrite(first, b"first-new", 0o600, True),
        common_installer.PlannedWrite(second, b"second-new", 0o700, True),
        common_installer.PlannedSymlink(link, source),
    ]

    def fail_after_second_claim(phase, path):
        if phase == "after_target_claim" and path == second:
            raise RuntimeError("injected failure")

    with pytest.raises(common_installer.InstallError, match="transaction failed"):
        common_installer.apply_transaction(
            entries,
            namespace="pre-pr-tribunal",
            stamp="20260901000000",
            phase_hook=fail_after_second_claim,
        )

    assert first.read_bytes() == b"first-original"
    assert second.read_bytes() == b"second-original"
    assert stat.S_IMODE(first.stat().st_mode) == 0o640
    assert stat.S_IMODE(second.stat().st_mode) == 0o600
    assert not os.path.lexists(link)
    assert source.is_dir()
    assert not (tmp_path / "a-new-tree").exists()
    assert list(tmp_path.rglob(".pre-pr-tribunal-*")) == []
    assert list(tmp_path.rglob("*.bak.pre-pr-tribunal.*")) == []
