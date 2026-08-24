#!/usr/bin/env python3
"""
Control-Char Guard Hook (PostToolUse)

Edit/Write/MultiEdit/NotebookEdit 로 **방금 기록한 내용**에 raw 제어문자(C0/DEL)가
섞여 들어갔는지 검사하고, 있으면 위치와 함께 즉시 경고한다.

배경: 정규식 character class 안에 raw 제어문자가 두 차례 삽입돼 python 바이트
검사로 추적해야 했던 사고(2026-08-24 /insights 리포트, board-service.ts).
눈에 보이지 않고 일반 grep 으로도 걸리지 않아 발견이 늦는다. "조심하자"는 규칙이
아니라 기록 시점에 걸러내는 구조적 장치가 필요한 부류의 결함이다.

검사 대상은 **이번 호출이 새로 쓴 텍스트**(content/new_string/new_source)다.
파일 전체가 아니므로 기존 파일에 원래 있던 제어문자로는 발화하지 않는다
(= 매 편집마다 같은 경고가 반복되는 노이즈가 없다).

허용: \t(09) \n(0a) \r(0d).
금지: 00-08, 0b, 0c, 0e-1f, 7f.

PostToolUse hookSpecificOutput.additionalContext 스펙 사용.
킬스위치: CLAUDE_SKIP_CTRLCHAR_GUARD=1
"""
import json
import os
import sys
import time

DEBUG_LOG = os.path.expanduser("~/.claude/logs/hook-debug.log")

# 2차 필터 — matcher 정규식이 변경/오인 매칭해도 여기서 차단
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

FORBIDDEN = frozenset(
    [chr(c) for c in range(0x00, 0x09)]
    + [chr(0x0B), chr(0x0C)]
    + [chr(c) for c in range(0x0E, 0x20)]
    + [chr(0x7F)]
)

# 위치 탐색은 참고 정보다. 거대 파일에서 훅이 오래 붙잡히지 않도록 상한을 둔다.
MAX_LOCATE_BYTES = 5 * 1024 * 1024
MAX_REPORT = 10

REMINDER = """[CTRL-CHAR-GUARD] 방금 {tool} 로 기록한 내용에 raw 제어문자가 들어 있다: {chars}
대상 파일: {path}
{where}
정규식 character class·문자열 리터럴에 보이지 않는 제어문자가 섞이면 grep 으로 걸리지 않고
동작만 조용히 어긋난다. 다음 중 하나를 이번 응답 안에서 처리하기 전까지 종료 금지:

1. 의도한 것이 아니면 해당 위치를 이스케이프 표기로 교체한다 (`\\t`, `\\x1b`, `\\x00` 등).
2. 의도한 것이면 왜 raw 바이트여야 하는지 1줄로 밝히고 넘어간다.

재확인: grep -nP '[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f]' {path}
킬스위치: CLAUDE_SKIP_CTRLCHAR_GUARD=1"""


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


def written_texts(tool_name: str, tool_input: dict) -> "list[str]":
    """이번 호출이 새로 기록한 텍스트만 모은다 (기존 파일 내용은 보지 않는다)."""
    out = []

    def take(value):
        if isinstance(value, str):
            out.append(value)

    if tool_name == "Write":
        take(tool_input.get("content"))
    elif tool_name == "Edit":
        take(tool_input.get("new_string"))
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, dict):
                    take(edit.get("new_string"))
    elif tool_name == "NotebookEdit":
        take(tool_input.get("new_source"))
    return out


def found_chars(texts: "list[str]") -> "list[str]":
    hits = set()
    for text in texts:
        hits.update(ch for ch in text if ch in FORBIDDEN)
    return sorted(hits)


def locate(path: str) -> "list[tuple[int, int, str]]":
    """파일에서 제어문자의 (줄, 열)을 찾는다. 실패하면 빈 목록 — 경고 자체는 유지된다."""
    hits = []
    try:
        if os.path.getsize(path) > MAX_LOCATE_BYTES:
            return hits
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                for col, ch in enumerate(line, 1):
                    if ch in FORBIDDEN:
                        hits.append((lineno, col, ch))
                        if len(hits) >= MAX_REPORT:
                            return hits
    except OSError:
        pass
    return hits


def format_where(path: str) -> str:
    hits = locate(path)
    if not hits:
        return "위치: 파일에서 특정하지 못함 (읽기 실패·크기 초과). 위 grep 으로 직접 확인하라."
    lines = [
        "  %s:%d  (열 %d) %s" % (os.path.basename(path), lineno, col, repr(ch))
        for lineno, col, ch in hits
    ]
    if len(hits) >= MAX_REPORT:
        lines.append("  ... (%d개까지만 표시)" % MAX_REPORT)
    return "위치:\n" + "\n".join(lines)


def main() -> int:
    if os.environ.get("CLAUDE_SKIP_CTRLCHAR_GUARD") == "1":
        return 0
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    tool_name = payload.get("tool_name", "")
    if not isinstance(tool_name, str) or tool_name not in EDIT_TOOLS:
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    chars = found_chars(written_texts(tool_name, tool_input))
    debug_log("ctrl-char-guard", {"tool": tool_name, "hits": len(chars)})
    if not chars:
        return 0

    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not isinstance(path, str):
        path = ""

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": REMINDER.format(
                tool=tool_name,
                chars=", ".join(repr(c) for c in chars),
                path=path or "(경로 불명)",
                where=format_where(path) if path else "",
            ),
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
