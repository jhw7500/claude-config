"""The Codex AGENTS block, the Claude skill and install.sh point at the same installed outbox script."""

import pytest


COMMAND = "/usr/bin/python3 $HOME/.claude/scripts/artifact-outbox.py"


def test_codex_block_and_claude_skill_use_the_installed_command(installer):
    block = installer.artifact_outbox_policy_block()
    skill = (installer.REPO / "skills/artifact-publish/SKILL.md").read_text(encoding="utf-8")
    assert f"{COMMAND} export" in block
    assert "`artifact-publish` skill" in block and "`next_step`" in block
    assert COMMAND in skill


def test_artifact_outbox_block_is_idempotent_and_separate(installer):
    before = "keep-before\n"
    once = installer.merge_artifact_outbox_block(before, installer.artifact_outbox_policy_block())
    twice = installer.merge_artifact_outbox_block(once, installer.artifact_outbox_policy_block())
    assert twice == once
    assert once.startswith(before)
    assert once.count(installer.ARTIFACT_OUTBOX_START) == 1
    assert once.count(installer.ARTIFACT_OUTBOX_END) == 1
    assert installer.AGENTS_START not in once and installer.CHANGE_EVIDENCE_START not in once


@pytest.mark.parametrize("other", ["task-nudge", "change-evidence"])
@pytest.mark.parametrize("artifact_outside", [True, False], ids=["artifact-outside", "artifact-inside"])
def test_artifact_outbox_block_nested_with_another_fails_closed(installer, other, artifact_outside):
    artifact = ("<!-- claude-config:artifact-outbox:START -->\n", "<!-- claude-config:artifact-outbox:END -->\n")
    managed = (f"<!-- claude-config:{other}:START -->\n", f"<!-- claude-config:{other}:END -->\n")
    outer, inner = (artifact, managed) if artifact_outside else (managed, artifact)
    with pytest.raises(installer.InstallError, match="AGENTS managed blocks overlap"):
        installer.merge_guidance_blocks(outer[0] + inner[0] + inner[1] + outer[1])


def test_any_pair_of_three_blocks_is_checked_for_overlap(installer):
    # The task-nudge block is separate; the overlap is between the second and third blocks.
    original = (
        "<!-- claude-config:task-nudge:START -->\n<!-- claude-config:task-nudge:END -->\n"
        "<!-- claude-config:change-evidence:START -->\n"
        "<!-- claude-config:artifact-outbox:START -->\n<!-- claude-config:artifact-outbox:END -->\n"
        "<!-- claude-config:change-evidence:END -->\n"
    )
    with pytest.raises(installer.InstallError, match="AGENTS managed blocks overlap"):
        installer.merge_guidance_blocks(original)


def test_full_install_links_the_script_and_writes_the_block_once(installer, home, run_install):
    first = run_install(home)
    assert first.returncode == 0, first.stderr
    assert (home / ".claude/scripts/artifact-outbox.py").resolve() == installer.REPO / "scripts/artifact-outbox.py"
    agents = home / ".codex/AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    assert text.count(installer.ARTIFACT_OUTBOX_START) == 1
    assert text.count(installer.ARTIFACT_OUTBOX_END) == 1
    assert f"{COMMAND} export" in text
    second = run_install(home)
    assert second.returncode == 0, second.stderr
    assert agents.read_text(encoding="utf-8") == text
