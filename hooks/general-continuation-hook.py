#!/usr/bin/env python3
"""
General Continuation Hook (UserPromptSubmit)

사용자가 "중단 없이 연속 실행" 의도를 표현하는 키워드를 쓰면 LLM이 중간에
"진행할까요?" 같은 재확인으로 멈추지 않고 작업을 이어가도록 system-reminder를
주입한다. Notion 저장 외 일반 작업 전반에 적용된다.

중요: 중간 진행 상황 "보고"는 금지하지 않는다. 보고는 짧게 유지하되 재확인으로
멈추는 것만 차단한다.
"""
import json
import os
import re
import sys
import time


ESCAPE_PREFIXES = ("#noreminder", "#nr", "#raw", "#silent", "#조용히")

DEBUG_LOG = os.path.expanduser("~/.claude/logs/hook-debug.log")


def debug_log(tag: str, info: "dict[str, object]") -> None:
    enabled = os.environ.get("CLAUDE_HOOK_DEBUG") == "1" or os.path.exists(
        os.path.expanduser("~/.claude/logs/hook-debug.on")
    )
    if not enabled:
        return
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG), exist_ok=True)
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tag": tag,
                **info,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def is_escape_prefixed(text: str) -> bool:
    """사용자가 명시적으로 훅 주입 스킵을 요청했는지 확인."""
    lowered = text.lstrip().lower()
    return any(lowered.startswith(p) for p in ESCAPE_PREFIXES)


TRIGGER_PATTERNS = [
    # 한국어
    r'한\s*번에',
    r'끝까지',
    r'쭉\s*(?:진행|실행|해)',
    r'중간에?\s*(?:멈추지|서지|끊지|끊기지|서서|끊어)\s*말',
    r'연속\s*(?:으로|실행|진행)',
    r'마저\s*(?:해|진행|끝내)',
    r'계속\s*(?:해|진행)',
    r'다\s*(?:진행|실행|끝내)',
    r'재확인\s*없이',
    r'자동(?:으로)?\s*(?:진행|실행)',
    r'일괄\s*(?:진행|처리|실행)',
    # 영어
    r'\b(?:all\s+at\s+once)\b',
    r"\b(?:don'?t\s+stop)\b",
    r'\b(?:keep\s+going)\b',
    r'\b(?:full\s+auto)\b',
    r'\b(?:in\s+one\s+shot)\b',
    r'\b(?:end\s+to\s+end)\b',
    r'\b(?:without\s+stopping)\b',
]


REMINDER = """<system-reminder>
[CONTINUOUS-EXEC] "중단 없이 연속 실행" 의도 감지됨.

작업 완료까지 **한 턴 안에서 이어 실행**. 독립 tool은 **병렬**로.
단계별 1~2문장 진행 보고는 유지, "진행할까요?" 재확인 없이 진행.
사용자 결정이 꼭 필요한 분기점에서만 응답 마무리.

세부 규칙: `~/.claude/CLAUDE.md` > "공통 작업 규칙" / "진행상황 보고"
</system-reminder>"""


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):  # 비객체 top-level JSON — 조용히 무시 (훅 규약: 모든 경로 exit 0)
        return 0

    # 실증: Claude Code는 'prompt' 키 사용. user_prompt/message는 레거시 fallback.
    prompt = (
        payload.get("prompt")
        or payload.get("user_prompt")
        or payload.get("message")
        or ""
    )
    if not isinstance(prompt, str):
        return 0

    debug_log("general", {"payload_keys": sorted(list(payload.keys())), "prompt_len": len(prompt)})

    text = prompt.strip()
    if not text:
        return 0

    # Escape hatch: 사용자가 #noreminder 등 prefix를 쓰면 훅 주입 스킵
    if is_escape_prefixed(text):
        return 0

    # 길이 상한: 지나치게 긴 메시지는 의도 모호하므로 제외
    if len(text) > 400:
        return 0

    for pat in TRIGGER_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            sys.stdout.write(REMINDER)
            sys.stdout.flush()
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
