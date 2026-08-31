import re

import pytest


_STAGE_ANCHORS = {
    "suppressed": ("(1) 이미 결정",),
    "backlog": ("(2) backlog",),
    "unknown": ("(3) 즉시 작업의 unknown",),
    "registered": ("(4) registered(등록) 저장소", "(4) 등록 저장소"),
    "unregistered": ("(5) unregistered(미등록) 저장소", "(5) 미등록 저장소"),
}
_STAGE_NAMES = tuple(_STAGE_ANCHORS)
_NO_PRECLAIM = {
    "claude-global": ("Task/Claim", "선점하지 않는다"),
    "native-hook": ("Task/Claim", "선점하거나 시작하지 않음"),
    "codex-agents": ("Task/Claim", "시작하지 않음"),
}


@pytest.fixture
def guidance_surfaces(core, installer):
    guidance = (installer.REPO / "claude-md" / "global-guidance.md").read_text(encoding="utf-8")
    start = guidance.index("9. **Task 등록 권유")
    end = guidance.index("\n---", start)
    result = core.RegistrationResult(
        core.RegistrationStatus.REGISTERED,
        "jhw7500/claude-config",
    )
    return {
        "claude-global": guidance[start:end],
        "native-hook": core.render_nudge_message(result),
        "codex-agents": installer.agents_policy_block(),
    }


def _stage_sections(text):
    """Return policy branches only after proving the approved precedence."""
    positions = []
    for stage, alternatives in _STAGE_ANCHORS.items():
        position = next((text.find(anchor) for anchor in alternatives if text.find(anchor) >= 0), -1)
        assert position >= 0, f"missing {stage} stage"
        positions.append(position)
    assert positions == sorted(positions), "policy stages are reordered"
    final_boundary = text.find("\n", positions[-1])
    if final_boundary < 0:
        final_boundary = len(text)
    boundaries = positions[1:] + [final_boundary]
    return {
        stage: text[start:end]
        for stage, start, end in zip(_STAGE_NAMES, positions, boundaries)
    }


def _assert_unknown_stops_pending_classification(section):
    assert "unknown" in section
    assert "현재 변경을 중단" in section
    assert any(
        phrase in section
        for phrase in ("등록 여부를 가정하지 않고", "등록 여부를 가정하지 말고")
    )
    assert "Formal Issue Task" not in section
    assert "Temporary Task" not in section


def _assert_backlog_does_not_preclaim(name, section):
    assert "Issue만" in section
    task_claim, prohibition = _NO_PRECLAIM[name]
    assert task_claim in section
    assert prohibition in section
    assert "Task/Claim 시작" not in section


def _assert_unregistered_registration_only(section):
    assert "반복" in section
    assert "Project/Repository 등록" in section
    assert "먼저 제안" in section
    _assert_no_task_choice(section)
    assert "GitHub Issue" not in section
    assert "Formal Issue Task" not in section
    assert "Temporary Task" not in section
    assert "Task/Claim" not in section
    assert "Task 시작" not in section


def _assert_no_task_choice(section):
    assert "Task 없이" in section or "Task 없음" in section


def test_all_guidance_surfaces_follow_the_ordered_task_policy(guidance_surfaces):
    for name, text in guidance_surfaces.items():
        sections = _stage_sections(text)
        assert "이미 결정" in sections["suppressed"]
        assert "subagent" in sections["suppressed"]
        assert "Task 없이" in sections["suppressed"]

        _assert_backlog_does_not_preclaim(name, sections["backlog"])
        _assert_unknown_stops_pending_classification(sections["unknown"])

        registered = sections["registered"]
        assert "Formal Issue Task" in registered
        assert "Temporary Task" in registered
        _assert_no_task_choice(registered)
        assert "Project/Repository 등록" not in registered

        _assert_unregistered_registration_only(sections["unregistered"])


def test_stage_contract_rejects_a_reordered_policy(guidance_surfaces):
    text = guidance_surfaces["native-hook"]
    backlog_start = text.index("(2) backlog")
    unknown_start = text.index("(3) 즉시 작업의 unknown")
    registered_start = text.index("(4) 등록 저장소")
    reordered = (
        text[:backlog_start]
        + text[unknown_start:registered_start]
        + text[backlog_start:unknown_start]
        + text[registered_start:]
    )

    with pytest.raises(AssertionError, match="reordered"):
        _stage_sections(reordered)


def test_unregistered_contract_rejects_issue_or_task_claim_mutation(guidance_surfaces):
    unregistered = _stage_sections(guidance_surfaces["native-hook"])["unregistered"]
    contradictory = unregistered + " GitHub Issue 생성과 Task/Claim 시작을 제안."

    with pytest.raises(AssertionError):
        _assert_unregistered_registration_only(contradictory)


def test_backlog_contract_rejects_removed_task_claim_prohibition(guidance_surfaces):
    backlog = _stage_sections(guidance_surfaces["native-hook"])["backlog"]
    contradictory = backlog.replace("Task/Claim을 선점하거나 시작하지 않음", "Task/Claim을 시작함")

    with pytest.raises(AssertionError):
        _assert_backlog_does_not_preclaim("native-hook", contradictory)


def test_all_guidance_surfaces_keep_approvals_separate_and_non_transitive(guidance_surfaces):
    for text in guidance_surfaces.values():
        approval = text[text.index("GitHub Issue 생성") :]
        assert "GitHub Issue 생성" in approval
        assert "Project/Repository 등록" in approval
        assert "Formal 또는 Temporary Task 시작" in approval
        assert "각각 별도의 명시적 사용자 승인" in approval
        assert "앞 단계 승인은 다음 단계를 승인하지 않는다" in approval


def test_all_guidance_surfaces_suppress_subagents_and_already_decided_work(guidance_surfaces):
    for text in guidance_surfaces.values():
        sections = _stage_sections(text)
        assert "이미 결정" in sections["suppressed"]
        assert "subagent" in sections["suppressed"]
        assert "Task 없이" in sections["suppressed"]


def test_all_guidance_surfaces_define_only_the_three_recurring_evidence_classes(guidance_surfaces):
    for text in guidance_surfaces.values():
        assert "장기·반복·여러 세션" in text
        assert "기존 GitHub Issue·승인된 계획·Handoff" in text
        assert "여러 구현 단계와 검증이 필요한 아키텍처 작업" in text
        assert "파일 수나 저장소 안에 있다는 사실은 증거가 아니다" in text


@pytest.mark.parametrize("status_name", ("REGISTERED", "UNREGISTERED"))
def test_native_renderer_scrubs_malicious_slug_for_value_statuses(core, status_name):
    canaries = (
        "/tmp/TASK_NUDGE_PATH_CANARY",
        "SESSION_CANARY",
        "PROJECT_ID_CANARY",
        "REPOSITORY_ID_CANARY",
        "TASK_ID_CANARY",
        "CLAIM_ID_CANARY",
    )
    status = getattr(core.RegistrationStatus, status_name)
    result = core.RegistrationResult(
        status,
        "/".join(canaries),
    )
    message = core.render_nudge_message(result)

    assert f"상태: {status.value}" in message
    assert "저장소:" not in message
    assert all(canary not in message for canary in canaries)


def test_native_renderer_scrubs_malicious_unknown_reason(core):
    canaries = (
        "/tmp/TASK_NUDGE_PATH_CANARY",
        "RAW_CHILD_OUTPUT_CANARY",
        "SESSION_CANARY",
        "GH_PROJECT_TOKEN",
        "NOTION_API_KEY",
        "PASSWORD_CANARY",
        "SECRET_CANARY",
    )
    result = core.RegistrationResult(
        core.RegistrationStatus.UNKNOWN,
        canaries[0],
        "-".join(canaries),
    )
    message = core.render_nudge_message(result)

    assert "상태: unknown" in message
    assert "PORTFOLIO_UNAVAILABLE" in message
    assert all(canary not in message for canary in canaries)


@pytest.mark.parametrize("status_name", ("REGISTERED", "UNREGISTERED"))
def test_native_renderer_renders_a_valid_safe_slug(core, status_name):
    status = getattr(core.RegistrationStatus, status_name)
    message = core.render_nudge_message(
        core.RegistrationResult(status, "safe-owner/safe-repo")
    )

    assert message.startswith(
        f"[TASK-NUDGE] 저장소: safe-owner/safe-repo / 상태: {status.value}"
    )


_FORBIDDEN_GUIDANCE_PATTERNS = (
    re.compile(r"\b(?:project|repository|task|claim)[_-]id\b", re.IGNORECASE),
    re.compile(
        r"\b(?:gh[_-]?project[_-]?token|notion[_-]?api[_-]?key|api[_-]?key|"
        r"access[_-]?key|private[_-]?key|password|secret|token)\b",
        re.IGNORECASE,
    ),
)


def test_guidance_never_assembles_credentials_raw_control_or_internal_values(guidance_surfaces):
    forbidden = (
        "source control.env",
        "jhw-control task",
        "GH_PROJECT_TOKEN",
        "NOTION_API_KEY",
        "RAW_CHILD_OUTPUT_CANARY",
        "/tmp/TASK_NUDGE_PATH_CANARY",
        "SESSION_CANARY",
        "PROJECT_ID_CANARY",
        "REPOSITORY_ID_CANARY",
        "TASK_ID_CANARY",
        "CLAIM_ID_CANARY",
    )
    for text in guidance_surfaces.values():
        assert all(value.casefold() not in text.casefold() for value in forbidden)
        assert all(pattern.search(text) is None for pattern in _FORBIDDEN_GUIDANCE_PATTERNS)

    claude_guidance = guidance_surfaces["claude-global"]
    assert '"$HOME/.local/bin/jhw-control-host" preflight' in claude_guidance
    assert 'jhw-control-host" portfolio status' not in claude_guidance
