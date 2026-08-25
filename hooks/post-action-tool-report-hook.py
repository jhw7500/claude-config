#!/usr/bin/env python3
"""
Post-Action-Tool Report Hook (PostToolUse)

action 도구(jhw_record/jhw_note/jhw_delete, notion-create-pages/update-page,
notion-create-database 등) 호출 직후 결과 URL/ID/title을 사용자에게 텍스트로
보고하지 않고 응답을 종료하는 패턴을 차단한다.

배경: MCP tool result(JSON)는 Claude 컨텍스트에만 들어오고 사용자 화면엔
자동 표시되지 않음. 호출 직후 응답을 종료하면 사용자는 저장됐는지/실패했는지/
URL이 뭔지 알 수 없어 다시 입력해서 확인해야 한다. 본 세션(2026-05-07)에서
3회 반복 위반.

CLAUDE.md "정보 수집 → 조기 종료 방지" 자가 점검 #3 (결과 요약 텍스트 보고)을
도구 호출 직후 다시 강제하는 구조적 안전장치.

PostToolUse hookSpecificOutput.additionalContext 스펙 사용.
"""
import json
import os
import sys
import time

DEBUG_LOG = os.path.expanduser("~/.claude/logs/hook-debug.log")

# action 도구 매처 — settings.json matcher와 일관성 유지.
# matcher가 정규식이라 1차 필터되지만, 스크립트에서 한 번 더 검증해
# 매처 변경 시 안전망 역할.
ACTION_TOOL_SUBSTRINGS = (
    # jhw-notion action
    "mcp__jhw-notion__jhw_record",
    "mcp__jhw-notion__jhw_note",
    "mcp__jhw-notion__jhw_delete",
    "mcp__jhw-notion__jhw_start",
    "mcp__jhw-notion__jhw_close",
    "mcp__jhw-notion__jhw_report_export",
    # notion native action
    "mcp__notion__notion-create-pages",
    "mcp__notion__notion-update-page",
    "mcp__notion__notion-create-database",
    "mcp__notion__notion-update-data-source",
    "mcp__notion__notion-create-comment",
    "mcp__notion__notion-duplicate-page",
    "mcp__notion__notion-move-pages",
)

REMINDER = """[ACTION-RESULT-REPORT] action 도구 호출 완료. 결과를 사용자에게 텍스트로 보고할 차례.

MCP tool result(JSON URL/ID 등)는 Claude 컨텍스트에만 들어오고 사용자 화면엔 자동 표시되지 않는다.
다음을 응답에 반드시 포함하기 전까지 응답 종료 금지:

**결과 보고 형식 (표 형식 의무)**:
| # | DB | 제목 | URL |
|---|---|---|---|
| 1 | <db명> | <제목> | [열기](<URL>) |

여러 항목 저장 시 모두 한 표에 합친다. 단일 항목이라도 표 또는 1줄 명시 텍스트로 보고.

**금지 패턴**:
- 도구 호출 후 텍스트 보고 없이 응답 종료
- "저장했습니다" 등 모호한 보고 (URL/ID 누락)
- raw JSON만 노출

**자가 점검 (응답 종료 직전)**:
- 이 응답에 결과 URL이 텍스트로 포함됐는가? 없으면 종료 금지.

세부 규칙: ~/.claude/CLAUDE.md > "정보 수집 → 조기 종료 방지" 자가 점검 #3 / @CLAUDE-notion.md
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
    if not isinstance(payload, dict):  # 비객체 top-level JSON — 조용히 무시 (훅 규약: 모든 경로 exit 0)
        return 0

    tool_name = payload.get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name:
        return 0

    debug_log("post-action-tool", {"tool": tool_name})

    # 2차 필터 — matcher 정규식이 변경/오인 매칭해도 여기서 차단
    if not any(p in tool_name for p in ACTION_TOOL_SUBSTRINGS):
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
