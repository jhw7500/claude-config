#!/usr/bin/env python3
"""
Notion Continuous Execution Hook (UserPromptSubmit)

사용자가 `/jhw:review`, `/jhw:import`, `/jhw:record` 등 Notion 저장 스킬의
후보 테이블에 승인(전체 저장, N번만, OK 등)을 했을 때, LLM이 중간 단계에서
멈추지 않고 한 assistant 턴에서 저장을 완료하도록 system-reminder를 주입한다.

- 입력(stdin): Claude Code UserPromptSubmit hook JSON
- 출력(stdout): 승인 패턴 감지 시 system-reminder 텍스트

승인 키워드 패턴 매칭:
  - "OK", "ok", "네", "예", "응", "좋아"
  - "전체", "전부", "모두", "전체 저장", "모두 저장"
  - "1번", "1,2", "1,2,3번만", "1-3" 등 숫자 기반 선택
  - "번호 지정", "N번 빼", "N번 제외"

매칭되지 않으면 stdout은 비어있고 훅은 조용히 종료한다.
"""
import json
import os
import re
import sys
import time


ESCAPE_PREFIXES = ("#noreminder", "#nr", "#raw", "#silent", "#조용히")

DEBUG_LOG = os.path.expanduser("~/.claude/logs/hook-debug.log")


def debug_log(tag: str, info: "dict[str, object]") -> None:
    """CLAUDE_HOOK_DEBUG=1 또는 ~/.claude/logs/hook-debug.on 파일이 있을 때만 기록."""
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


APPROVAL_PATTERNS = [
    # 짧은 긍정 응답
    r'^(ok|okay|네|예|응|좋아|좋아요|확인|진행|승인|저장|yes|y)\.?$',
    # 전체/전부 저장
    r'(전체|전부|모두)\s*(저장|가져오기|기록|승인)',
    r'^(전체|전부|모두)\.?$',
    # 숫자 기반 선택 ("1번만", "1,2", "1,2,3번만 저장", "1-3")
    r'^\s*\d+(\s*[,\-~]\s*\d+)*\s*(번)?\s*(만)?\s*(저장|기록|가져오기)?\s*\.?$',
    # 제외 표현 ("2번 빼", "3번 제외")
    r'\d+\s*번?\s*(빼|제외|빼고)',
]


REMINDER = """<system-reminder>
[NOTION-CONTINUOUS-EXEC] 승인 응답 감지됨.

Notion 저장/불러오기 스킬(`/jhw:review`, `/jhw:import`, `/jhw:record`,
`/jhw:note`, `/jhw:start`, `/jhw:close`, `/jhw:delete`) 맥락이라면:
저장 완료까지 **한 턴 안에서 끊지 말고 연속 실행**. 재확인으로 멈추지 말 것.
단계별 1~2문장 진행 보고는 유지. 독립 tool은 **병렬**.

세부 규칙: `~/.claude/CLAUDE.md` > "Notion 저장 스킬 흐름 규칙"
맥락이 아니라면 이 reminder는 무시.
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

    # Record payload keys for spec verification (env-gated)
    debug_log("notion", {"payload_keys": sorted(list(payload.keys())), "prompt_len": len(prompt)})

    text = prompt.strip()
    if not text:
        return 0

    # Escape hatch: 사용자가 #noreminder 등 prefix를 쓰면 훅 주입 스킵
    if is_escape_prefixed(text):
        return 0

    # 너무 긴 메시지는 일반 요청일 가능성이 높으므로 스킵
    if len(text) > 80:
        return 0

    lowered = text.lower()
    for pat in APPROVAL_PATTERNS:
        if re.search(pat, lowered):
            sys.stdout.write(REMINDER)
            sys.stdout.flush()
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
