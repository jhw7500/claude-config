#!/usr/bin/env python3
"""
Post-Info-Tool Continuation Hook (PostToolUse)

정보 수집 도구(ToolSearch, notion-search/fetch, jhw_search 등) 호출 직후
응답 종료를 방지하기 위해 system-reminder를 inject한다.

CLAUDE.md "정보 수집 → 조기 종료 방지" 룰을 도구 호출 직후에 다시 강제하는
구조적 안전장치. 다단계 흐름(예: 사용자 승인 → 저장, /jhw:review)에서
정보만 수집하고 "Tool loaded.", 결과 요약만 출력한 채 응답 종료하는
LLM 관성을 차단.

PostToolUse hookSpecificOutput.additionalContext 스펙 사용.
"""
import json
import os
import sys
import time

DEBUG_LOG = os.path.expanduser("~/.claude/logs/hook-debug.log")

# 정보 수집 도구 매처 — settings.json matcher와 일관성 유지.
# matcher가 정규식이라 1차 필터되지만, 스크립트에서 한 번 더 검증해
# 매처 변경 시 안전망 역할.
INFO_TOOL_SUBSTRINGS = (
    "ToolSearch",
    "WebSearch",
    "WebFetch",
    # Notion 검색/조회
    "mcp__notion__notion-search",
    "mcp__notion__notion-fetch",
    "mcp__notion__notion-get-comments",
    # jhw-notion 검색/조회
    "mcp__jhw-notion__jhw_search",
    "mcp__jhw-notion__jhw_context",
    "mcp__jhw-notion__jhw_history",
    "mcp__jhw-notion__jhw_status",
    "mcp__jhw-notion__jhw_retrieve",
    # context7 docs
    "mcp__plugin_context7_context7__query-docs",
    "mcp__plugin_context7_context7__resolve-library-id",
)

REMINDER = """[INFO-TOOL-CONTINUATION] 정보 수집 도구 호출 완료.

이 응답이 다단계 흐름(승인→저장, /jhw:review, /jhw:record, 다단계 분석/실행 등)이라면:
- **같은 응답 안에서 후속 action 도구 호출까지 이어가라**
- action 도구 예: create-pages, update-page, jhw_record, jhw_note, Edit, Write, Bash 등
- "Tool loaded." 또는 결과 요약만 출력하고 응답 종료 금지

응답 종료 직전 필수 자가 점검:
1. 사용자 요청이 다단계인가? (승인/저장/생성/수정/실행 action 포함)
2. 그 action 도구 호출이 이번 응답에 포함됐는가?
3. 둘 다 yes인데 action 호출이 없으면 응답 종료 금지 — 같은 응답 안에서 action까지 이어간다.

세부 규칙: ~/.claude/CLAUDE.md > "정보 수집 → 조기 종료 방지" / "Notion 저장 스킬 흐름 규칙"
"""


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


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool_name = payload.get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name:
        return 0

    debug_log("post-info-tool", {"tool": tool_name})

    # 2차 필터 — matcher 정규식이 변경/오인 매칭해도 여기서 차단
    if not any(p in tool_name for p in INFO_TOOL_SUBSTRINGS):
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": REMINDER,
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
