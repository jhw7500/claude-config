"""전체 stdin-JSON 훅의 top-level 비객체 payload 가드 회귀 테스트.

회귀: PR #44 재리뷰 + PR #45 — delegate-nudge/계측에서 고친 결함(`[1,2,3]` 류
비객체 JSON에 `payload.get()` AttributeError → exit 1, 훅 규약 위반)이 기존
stdin-JSON 훅 6종에 동일하게 상속돼 있었다. 모든 훅은 어떤 입력에도 exit 0.
"""

import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parents[1] / "hooks"

STDIN_JSON_HOOKS = [
    "general-continuation-hook.py",
    "handoff-checkpoint-hook.py",
    "notion-continuous-exec-hook.py",
    "notion-recall-trigger-hook.py",
    "post-action-tool-report-hook.py",
    "post-info-tool-continuation-hook.py",
    "delegate-nudge-hook.py",
    "control-char-guard-hook.py",
    "bg-task-progress-hook.py",
    # json.load(sys.stdin) 계열 — PR #48 리뷰(Claude MEDIUM·Codex P2)에서 누락 지적
    "agent-name-delivery-hook.py",
    "carl-hook.py",
]

NON_DICT_PAYLOADS = ["[1,2,3]", '"문자열"', "42", "null", "true"]


@pytest.mark.parametrize("hook", STDIN_JSON_HOOKS)
@pytest.mark.parametrize("raw", NON_DICT_PAYLOADS)
def test_non_dict_top_level_json_exits_quietly(hook, raw):
    result = subprocess.run(
        [sys.executable, str(HOOKS_DIR / hook)],
        input=raw, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, f"{hook}: {result.stderr}"
    assert result.stdout.strip() == ""
