import pytest


CONTRACT = (
    "https://github.com/jhw7500/automation/blob/"
    "0d97a63891ba4473a3a189eae643f8059b76eb56/"
    "docs/change-evidence-contract-v1.md"
)
TRIGGER = "커밋 생성 또는 PR 본문 생성·갱신을 실제로 준비할 때만 적용한다."
EXCLUSION = "조회·설명·코드 편집만 하는 작업에는 적용하지 않는다."
SHARED_RULES = (
    "이미 해당 작성 스킬을 적용했다면 중복 호출하지 않는다.",
    "빠졌다면 실행 전에 누락을 알리고 정본 스킬로 보완한다.",
    "검증하지 않은 결과나 Issue/PR 관계를 만들지 않는다.",
    "스킬이 설치되지 않았다면 배포 누락을 알리고 계약을 충족한 척하지 않는다.",
    "Issue·PR·commit을 자동 생성하지 않는다.",
    "Task 선택은 task-nudge, PR 생성 허가는 pre-PR Tribunal가 담당한다.",
    "PR은 pre-PR Tribunal의 canonical command로만 생성하고, "
    "`<PR>`의 생성 명령(`--repo`·변수 인자 포함)은 실행하지 않는다.",
    "PR 본문은 `<PR>`의 작성·검증 단계를 통과한 파일을 그대로 넘긴다.",
    "일반 git 명령을 추가로 차단하거나 Tribunal 재심사를 호출하지 않는다.",
)


def _neutral(policy, host, commit_skill, pr_skill):
    # Host and skill names are the only intended differences between the two.
    return (
        policy.replace(f"{host}에서는", "<HOST>에서는")
        .replace(commit_skill, "<COMMIT>")
        .replace(pr_skill, "<PR>")
    )


def test_claude_and_codex_share_bounded_activation(installer):
    guidance = (installer.REPO / "claude-md/global-guidance.md").read_text(encoding="utf-8")
    heading = "## Change Evidence 작성 활성화 (전역)\n\n"
    start = guidance.index(heading) + len(heading)
    claude = _neutral(guidance[start:guidance.index("\n---", start)], "Claude", "/jhw:commit", "/jhw:pr")
    codex = _neutral(installer.change_evidence_policy_block(), "Codex", "$jhw-commit", "$jhw-pr")
    assert claude == codex
    assert "<HOST>에서는 `<COMMIT>`(직접/PR용 커밋) 또는 `<PR>`(PR 본문 작성·검증)" in claude
    assert claude.startswith(TRIGGER + " " + EXCLUSION + "\n")
    assert f"[Change Evidence Contract v1 정본]({CONTRACT})" in claude
    for rule in SHARED_RULES:
        assert rule in claude


def test_readme_and_installer_pin_the_same_contract(installer):
    readme = (installer.REPO / "README.md").read_text(encoding="utf-8")
    assert installer.CHANGE_EVIDENCE_CONTRACT == CONTRACT
    assert CONTRACT in readme


def test_change_evidence_block_is_idempotent_and_separate(installer):
    before = "keep-before\n"
    once = installer.merge_change_evidence_block(before, installer.change_evidence_policy_block())
    twice = installer.merge_change_evidence_block(once, installer.change_evidence_policy_block())
    assert twice == once
    assert once.startswith(before)
    assert once.count(installer.CHANGE_EVIDENCE_START) == 1
    assert once.count(installer.CHANGE_EVIDENCE_END) == 1
    assert installer.AGENTS_START not in once


@pytest.mark.parametrize(
    "malformed",
    [
        "<!-- claude-config:change-evidence:START -->\n",
        "<!-- claude-config:change-evidence:END -->\n",
        "<!-- claude-config:change-evidence:END -->\n<!-- claude-config:change-evidence:START -->\n",
    ],
)
def test_malformed_change_evidence_block_is_rejected(installer, malformed):
    with pytest.raises(installer.InstallError):
        installer.merge_change_evidence_block(malformed, installer.change_evidence_policy_block())


@pytest.mark.parametrize(
    "original",
    [
        "<!-- claude-config:task-nudge:START -->\n"
        "<!-- claude-config:change-evidence:START -->\n"
        "<!-- claude-config:change-evidence:END -->\n"
        "<!-- claude-config:task-nudge:END -->\n",
        "<!-- claude-config:change-evidence:START -->\n"
        "<!-- claude-config:task-nudge:START -->\n"
        "<!-- claude-config:task-nudge:END -->\n"
        "<!-- claude-config:change-evidence:END -->\n",
        "<!-- claude-config:task-nudge:START -->\n"
        "<!-- claude-config:change-evidence:START -->\n"
        "<!-- claude-config:task-nudge:END -->\n",
    ],
)
def test_nested_or_partial_managed_blocks_fail_closed(installer, original):
    with pytest.raises(installer.InstallError):
        installer.merge_guidance_blocks(original)


def test_full_install_publishes_codex_guidance_once(home, run_install):
    first = run_install(home)
    assert first.returncode == 0, first.stderr
    claude = (home / ".claude/global-guidance.md").read_text(encoding="utf-8")
    assert CONTRACT in claude and TRIGGER in claude
    assert "@global-guidance.md" in (home / ".claude/CLAUDE.md").read_text(encoding="utf-8")
    agents = home / ".codex/AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    assert text.count("<!-- claude-config:change-evidence:START -->") == 1
    assert text.count("<!-- claude-config:change-evidence:END -->") == 1
    assert CONTRACT in text
    second = run_install(home)
    assert second.returncode == 0, second.stderr
    assert agents.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("trailing_newline", [True, False], ids=["newline", "no-newline"])
def test_install_upgrades_task_nudge_only_host(installer, home, run_install, trailing_newline):
    # A host installed before this change carries only the task-nudge block.
    codex = home / ".codex"
    codex.mkdir()
    previous = installer.merge_agents_block("user-notes\n", installer.agents_policy_block())
    if not trailing_newline:
        previous = previous.rstrip("\n")
    agents = codex / "AGENTS.md"
    agents.write_text(previous, encoding="utf-8")
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    text = agents.read_text(encoding="utf-8")
    assert text.startswith(previous)
    assert text[len(previous):].lstrip("\n").startswith(installer.CHANGE_EVIDENCE_START)
    assert text.count(installer.AGENTS_START) == 1
    assert text.count(installer.CHANGE_EVIDENCE_START) == 1


def test_manual_block_removal_is_undone_by_next_install(installer, home, run_install):
    # README: withdrawal must also revert the installer, or the next install re-adds the block.
    first = run_install(home)
    assert first.returncode == 0, first.stderr
    agents = home / ".codex/AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    start = text.index(installer.CHANGE_EVIDENCE_START)
    end = text.index(installer.CHANGE_EVIDENCE_END) + len(installer.CHANGE_EVIDENCE_END) + 1
    agents.write_text(text[:start] + text[end:], encoding="utf-8")
    assert installer.CHANGE_EVIDENCE_START not in agents.read_text(encoding="utf-8")
    second = run_install(home)
    assert second.returncode == 0, second.stderr
    assert agents.read_text(encoding="utf-8").count(installer.CHANGE_EVIDENCE_START) == 1


@pytest.mark.parametrize(
    ("seed", "message"),
    [
        ("user-notes\n<!-- claude-config:change-evidence:START -->\n", "malformed AGENTS change-evidence markers"),
        (
            "<!-- claude-config:task-nudge:START -->\n"
            "<!-- claude-config:change-evidence:START -->\n"
            "<!-- claude-config:change-evidence:END -->\n"
            "<!-- claude-config:task-nudge:END -->\n",
            "AGENTS managed blocks overlap",
        ),
    ],
    ids=["unpaired-marker", "nested-blocks"],
)
def test_install_rejects_bad_managed_blocks_without_writing_agents(home, run_install, seed, message):
    codex = home / ".codex"
    codex.mkdir()
    agents = codex / "AGENTS.md"
    agents.write_text(seed, encoding="utf-8")
    result = run_install(home)
    assert result.returncode != 0
    assert message in result.stderr
    assert agents.read_bytes() == seed.encode("utf-8")
    assert not (codex / "hooks.json").exists()
