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
EXPLICIT_SKILL = re.compile(r"(?<![\w-])[$/]pre-pr-tribunal(?![\w-])", re.IGNORECASE)
EXPLICIT_ACTION_BEFORE = re.compile(
    r"^\s*(?:(?:please|can you)\s+)?"
    r"(?:run|invoke|use|start|go)\s+(?:the\s+)?$", re.IGNORECASE
)
EXPLICIT_ACTION_AFTER = re.compile(
    r"^\s*(?:(?:진행|실행|호출|시작)"
    r"(?:하되|하자|해\s*(?:줘|주세요|줄래)|해(?=$|[.!?。])|(?=$|[.!?。]))"
    r"|돌려(?:\s*(?:줘|주세요)|(?=$|[.!?。]))"
    r"|해\s*(?:줘|주세요|줄래)|부탁해)",
    re.IGNORECASE,
)
EXPLICIT_DISCUSSION_AFTER = re.compile(
    r"^\s*(?:[:：]\s*)?(?:(?:은|는|이|가|의|에\s*대해)\s*)?"
    r"(?:(?:실행|진행|사용|호출)\s*(?:방법|상황|뜻|차이)"
    r"|(?:방법|사용법|뜻|차이|설명|알려|어떻게|왜|무엇|뭐)"
    r"|(?:동작\s*원리|결과)\s*(?:설명|왜|뭐|무엇)"
    r"|(?:what|why|how|explain|describe|usage|meaning|difference)\b)",
    re.IGNORECASE,
)
EXPLICIT_REJECTION_AFTER = re.compile(
    r"^\s*(?:(?:은|는|을|를)\s*)?"
    r"(?:말고|대신|없이|빼고|제외)(?=\s|$)"
    r"|^\s*(?:필요\s*없|안\s*돌려|not\b|skip\b|later\b|without\b|instead\b)",
    re.IGNORECASE,
)
REVIEW_NOUN = r"(?:심사|트리뷰날)(?:를|을)?"
NATURAL_REVIEW = re.compile(
    REVIEW_NOUN
    + r"(?:\s*(?:좀|한\s*번))?\s*"
    + r"(?:하자|하되|해\s*(?:줘|주세요|줄래)|해(?=$|[.!])|"
    + r"시작해(?:줘)?|진행해(?:줘)?|돌려\s*(?:줘|주세요)|부탁해)",
    re.IGNORECASE,
)
NEGATED_REVIEW = re.compile(
    r"(?<!\w)(?:심사|트리뷰날)(?:[은는을를])?\s*"
    r"(?:하지\s*마|하지\s*말|하지|안\s*해|취소|중지|그만)|"
    r"(?<![\w-])[$/]pre-pr-tribunal(?![\w-])\s*"
    r"(?:(?:은|는|을|를|아직|절대|지금은?|이번(?:엔|에는)|오늘은?)\s*){0,2}"
    r"(?:(?:진행|실행|호출|시작)(?:[은는을를])?\s*)?"
    r"(?:하지\s*마|하지\s*말|하지|안\s*해|취소(?:해줘)?|중지|그만)|"
    r"\b(?:do\s+not|don't|never|rather\s+not)\s+(?:ever\s+)?"
    r"(?:run|invoke|use|start)\s+(?:the\s+)?[$/]pre-pr-tribunal\b|"
    r"(?:^|[.!?。]\s*|아니[,\s]+)"
    r"(?:(?:오늘은|지금은|이번(?:엔|에는)|아직|절대)\s*)?"
    r"(?:하지\s*마|취소(?:해줘)?|그만)\s*[.!?。]?$",
    re.IGNORECASE,
)


def classify_request(prompt: str) -> str | None:
    """Classify only explicit invocations and review-action requests."""
    if not isinstance(prompt, str) or len(prompt) > MAX_INPUT_BYTES:
        return None
    text = prompt.strip()
    if not text or text.lower().startswith(ESCAPE_PREFIXES):
        return None
    if NEGATED_REVIEW.search(text):
        return None
    skill = EXPLICIT_SKILL.search(text)
    after_skill = text[skill.end():] if skill else ""
    if skill and EXPLICIT_REJECTION_AFTER.match(after_skill):
        return None
    if skill and (
        EXPLICIT_ACTION_BEFORE.search(text[:skill.start()])
        or EXPLICIT_ACTION_AFTER.match(after_skill)
        or (skill.start() == 0 and not (
            EXPLICIT_DISCUSSION_AFTER.match(after_skill)
            or text.endswith("?")
        ))
    ):
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
