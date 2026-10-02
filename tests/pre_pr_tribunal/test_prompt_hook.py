"""Natural-language and explicit Tribunal request routing reminders."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from pre_pr_tribunal.prompt_hook import classify_request


HOOK = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/prompt_hook.py"
CONTINUATION_HOOK = Path(__file__).resolve().parents[2] / "hooks/general-continuation-hook.py"


def run_hook(runtime: str, payload: object) -> str:
    result = subprocess.run(
        [sys.executable, str(HOOK), runtime],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_natural_and_explicit_requests_get_distinct_one_round_guidance(runtime):
    natural = run_hook(runtime, {"prompt": "심사하되 50으로 하자"})
    explicit = run_hook(runtime, {"prompt": "$pre-pr-tribunal 진행"})
    assert natural != explicit
    if runtime == "codex":
        natural_output = json.loads(natural)["hookSpecificOutput"]
        explicit_output = json.loads(explicit)["hookSpecificOutput"]
        assert natural_output["hookEventName"] == "UserPromptSubmit"
        assert explicit_output["hookEventName"] == "UserPromptSubmit"
        natural = natural_output["additionalContext"]
        explicit = explicit_output["additionalContext"]
    assert "자연어 심사 요청" in natural
    assert "명시적 pre-pr-tribunal 호출" in explicit
    for message in (natural, explicit):
        assert "policy-preview" in message
        assert "최대 1라운드" in message
        assert "FAIL/INCONCLUSIVE" in message
        assert "새 명시적 사용자 요청" in message


@pytest.mark.parametrize(
    "prompt",
    ["심사를 해줘", "심사 좀 해줘", "트리뷰날을 진행해줘", "트리뷰날 한 번 돌려줘"],
)
def test_common_affirmative_phrases_are_natural_requests(prompt):
    assert classify_request(prompt) == "natural"


@pytest.mark.parametrize(
    ("prompt", "kind"),
    [
        ("심사해줘. 실패하면 자동 수정하지 마", "natural"),
        ("$pre-pr-tribunal 진행하되 자동 재심사하지 마", "explicit"),
        ("please run $pre-pr-tribunal", "explicit"),
        ("이번에는 $pre-pr-tribunal 진행해줘", "explicit"),
        ("심사해줘. " + "검토 범위 설명 " * 50, "natural"),
    ],
)
def test_affirmative_review_request_survives_unrelated_limits(prompt, kind):
    assert classify_request(prompt) == kind
    assert run_hook("codex", {"prompt": prompt})


@pytest.mark.parametrize(
    "prompt",
    [
        "트리뷰날에 A와 B 모델이 다른 이유는?",
        "코드 리뷰해줘",
        "#noreminder 심사해줘",
        "$pre-pr-tribunal-extra",
        "심사하지 마",
        "심사를 하지 마",
        "트리뷰날 하지 말아줘",
        "심사하면 얼마나 걸려?",
        "심사해야 할까?",
        "$pre-pr-tribunal 하지 마",
        "심사해줘. 아니, 하지 마",
        "트리뷰날을 취소해줘",
        "please don't run $pre-pr-tribunal",
        "명시적 $pre-pr-tribunal과 자연어 심사의 차이는?",
    ],
)
def test_discussion_or_unrelated_review_does_not_route_to_tribunal(prompt):
    assert classify_request(prompt) is None
    assert run_hook("codex", {"prompt": prompt}) == ""


@pytest.mark.parametrize("payload", [[1, 2], {"prompt": 12}, {"prompt": "심사해줘", "is_subagent": True}])
def test_invalid_or_child_prompt_is_silent(payload):
    assert run_hook("codex", payload) == ""


def test_prior_keep_going_request_does_not_override_fail_stop():
    result = subprocess.run(
        [sys.executable, str(CONTINUATION_HOOK)],
        input=json.dumps({"prompt": "심사 끝까지 계속해"}, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    assert "[CONTINUOUS-EXEC]" in result.stdout
    assert "한 요청당 한 결정 라운드" in result.stdout
    assert "자동 수정·커밋·재심사는 하지 않는다" in result.stdout
