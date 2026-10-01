#!/usr/bin/python3
"""Advisory UserPromptSubmit boundary for Tribunal review requests.

This does not authenticate a user request or grant a CLI round transition.
"""

from __future__ import annotations

import json
import re
import sys


MAX_INPUT_BYTES = 64 * 1024
ESCAPE_PREFIXES = ("#noreminder", "#nr", "#raw", "#silent", "#조용히")
EXPLICIT_SKILL = re.compile(r"^\s*[$/]pre-pr-tribunal(?![\w-])", re.IGNORECASE)
REVIEW_NOUN = r"(?:심사|트리뷰날)(?:를|을)?"
NATURAL_REVIEW = re.compile(
    REVIEW_NOUN
    + r"(?:\s*(?:좀|한\s*번))?\s*"
    + r"(?:하자|하되|해\s*(?:줘|주세요|줄래)|해(?=$|[.!])|"
    + r"시작해(?:줘)?|진행해(?:줘)?|돌려\s*(?:줘|주세요)|부탁해)",
    re.IGNORECASE,
)
NEGATED_REVIEW = re.compile(
    r"(?:심사|트리뷰날)\s*(?:하지\s*마|하지\s*말|하지|안\s*해)|"
    r"(?:심사|트리뷰날).{0,24}?(?:하지\s*마|하지\s*말|취소|중지|그만)|"
    r"[$/]pre-pr-tribunal.{0,24}?(?:하지\s*마|하지\s*말|취소|중지|그만)",
    re.IGNORECASE,
)


def classify_request(prompt: str) -> str | None:
    """Classify only explicit invocations and review-action requests."""
    if not isinstance(prompt, str) or len(prompt) > 400:
        return None
    text = prompt.strip()
    if not text or text.lower().startswith(ESCAPE_PREFIXES):
        return None
    if NEGATED_REVIEW.search(text):
        return None
    if EXPLICIT_SKILL.search(text):
        return "explicit"
    if text == "심사" or NATURAL_REVIEW.search(text):
        return "natural"
    return None


def _message(kind: str) -> str:
    if kind == "explicit":
        opening = "명시적 pre-pr-tribunal 호출: Skill의 전체 증거 계약을 적용한다."
    else:
        opening = (
            "자연어 심사 요청: 일반 코드 리뷰인지 pre-PR Tribunal인지 문맥으로 구분한다. "
            "Tribunal로 해석한다면 고비용 실행 전에 범위를 명확히 한다."
        )
    return (
        f"[TRIBUNAL-INTENT] {opening} Tribunal 실행 전 policy-preview로 활성 reviewer, "
        "intensity/mode, 예상 소요 범위, 최대 1라운드와 중단 조건을 알린다. "
        "이번 사용자 요청에서는 한 결정 라운드만 수행한다. FAIL/INCONCLUSIVE 뒤에는 "
        "수정·커밋·재심사를 자동으로 이어가지 않는다. 후속 작업에는 결과를 보여 준 뒤 "
        "새 명시적 사용자 요청이 필요하다. 이전 '끝까지' 지시는 재심사 승인이 아니다."
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in {"claude", "codex"}:
        return 0
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return 0
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return 0
    if not isinstance(payload, dict) or payload.get("is_subagent") is True:
        return 0
    prompt = payload.get("prompt") or payload.get("user_prompt") or payload.get("message")
    kind = classify_request(prompt)
    if kind is None:
        return 0
    message = _message(kind)
    if argv[0] == "codex":
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": message,
            },
        }, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(f"<system-reminder>\n{message}\n</system-reminder>\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
