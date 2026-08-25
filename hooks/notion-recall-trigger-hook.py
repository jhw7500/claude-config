#!/usr/bin/env python3
"""
Notion Recall Trigger Hook (UserPromptSubmit)

프롬프트에 조회 마커('노션참고'/'노션 참고'/'@notion')가 있으면, 모델이 되묻거나
추측하기 전에 먼저 관련 노션 기록을 조회하도록 [NOTION-RECALL] system-reminder를 주입한다.

- 입력(stdin): Claude Code UserPromptSubmit hook JSON
- 출력(stdout): 마커 감지 시 reminder 텍스트. 미감지 시 빈 출력.

Design Ref: docs/superpowers/specs/2026-07-06-notion-recall-on-demand-design.md §4.1
"""
import json
import re
import sys

ESCAPE_PREFIXES = ("#noreminder", "#nr", "#raw", "#silent", "#조용히")

# '노션참고' / '노션 참고' / '@notion' (case-insensitive)
MARKER_RE = re.compile(r"(@notion|노션\s*참고)", re.IGNORECASE)

REMINDER = """<system-reminder>
[NOTION-RECALL] 노션 참고 요청 감지됨.

지금 프롬프트의 핵심 '주제'를 뽑아, 되묻거나 추측하기 전에 먼저 관련 노션 기록
(결정·근거 / 재사용 지식 / 외부문서)을 조회하고 그 내용을 근거로 작업하라.

- 우선 `mcp__jhw-notion__jhw_retrieve` 호출 (topic=핵심 주제, 식별되면 project 지정).
  도구가 없으면 `mcp__notion__notion-search` → 관련 후보 `mcp__notion__notion-fetch`.
- 독립 조회는 병렬로.
- 조회 근거(제목/URL)를 1줄로 보고한 뒤 작업을 이어간다.
- 조회로도 불충분하면 그때 사용자에게 무엇이 불명확한지 물어라.

맥락상 '노션에 저장'(기록)·'노션 MCP 코드 수정' 요청이면 이 reminder는 무시.
</system-reminder>"""


def is_escape_prefixed(text: str) -> bool:
    lowered = text.lstrip().lower()
    return any(lowered.startswith(p) for p in ESCAPE_PREFIXES)


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

    prompt = (
        payload.get("prompt")
        or payload.get("user_prompt")
        or payload.get("message")
        or ""
    )
    if not isinstance(prompt, str):
        return 0

    text = prompt.strip()
    if not text or is_escape_prefixed(text):
        return 0

    if MARKER_RE.search(text):
        sys.stdout.write(REMINDER)
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
