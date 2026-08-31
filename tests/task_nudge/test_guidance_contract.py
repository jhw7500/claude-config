import pytest


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


@pytest.mark.parametrize(
    "required",
    [
        "Formal Issue Task",
        "Temporary Task",
        "Task 없이",
        "GitHub Issue",
        "Project/Repository 등록",
        "unknown",
        "기존 Issue",
        "계획",
        "Handoff",
        "아키텍처",
        "별도",
        "subagent",
        "이미 결정",
    ],
)
def test_all_guidance_surfaces_share_task_policy(guidance_surfaces, required):
    for name, text in guidance_surfaces.items():
        assert required in text, f"{name} misses {required}"


def test_backlog_policy_never_preclaims(guidance_surfaces):
    for text in guidance_surfaces.values():
        assert "backlog" in text
        assert "Issue만" in text


def test_guidance_keeps_three_mutations_separately_approved(guidance_surfaces):
    for text in guidance_surfaces.values():
        assert "GitHub Issue 생성" in text
        assert "Project/Repository 등록" in text
        assert "Task 시작" in text
        assert "각각 별도" in text


def test_guidance_never_assembles_credentials_or_raw_control(guidance_surfaces):
    forbidden = ("source control.env", "jhw-control task", "GH_PROJECT_TOKEN", "NOTION_API_KEY")
    for text in guidance_surfaces.values():
        assert all(value not in text for value in forbidden)
