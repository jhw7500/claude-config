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

# 새로 기록되는 텍스트가 담기는 필드. 도구명이 아니라 **필드 유무**로 판정하므로
# matcher 에 새 편집 도구를 추가하면 훅 수정 없이 그대로 커버된다(도구명 하드코딩 회피).
TEXT_FIELDS = ("content", "new_string", "new_source")

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


def written_texts(tool_input: dict) -> "list[str]":
    """이번 호출이 새로 기록한 텍스트만 모은다 (기존 파일 내용은 보지 않는다).

    도구명으로 분기하지 않고 **텍스트 필드 유무**로 판정한다. old_string 처럼
    지워지는 쪽은 대상이 아니므로 TEXT_FIELDS 에 넣지 않는다.
    """
    out = []

    def take(source):
        if not isinstance(source, dict):
            return
        for key in TEXT_FIELDS:
            value = source.get(key)
            if isinstance(value, str):
                out.append(value)

    take(tool_input)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            take(edit)
    return out


def found_chars(texts: "list[str]") -> "list[str]":
    hits = set()
    for text in texts:
        hits.update(ch for ch in text if ch in FORBIDDEN)
    return sorted(hits)


def line_col(text: str, offset: int) -> "tuple[int, int]":
    head = text[:offset]
    return head.count("\n") + 1, offset - (head.rfind("\n") + 1) + 1


def read_file(path: str) -> "str | None":
    try:
        if os.path.getsize(path) > MAX_LOCATE_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def unique_offset(content: str, text: str) -> int:
    """content 안에서 text 가 정확히 한 번 나오면 그 오프셋, 아니면 -1.

    두 번째 일치를 찾는 즉시 멈추므로 전체 count 보다 싸다.
    """
    first = content.find(text)
    if first < 0:
        return -1
    if content.find(text, first + 1) >= 0:
        return -1
    return first


def locate(path: str, texts: "list[str]") -> "list[tuple[int, int, str, bool]]":
    """**새로 기록한 텍스트 안의** 제어문자 위치만 돌려준다.

    파일에서 그 텍스트가 **정확히 한 번** 나올 때만 파일 좌표(absolute=True)로
    보고한다. 없거나 여러 번 나오면 어느 것이 이번 편집인지 알 수 없으므로 작성
    텍스트 기준 상대 좌표(absolute=False)로 폴백한다 — 틀린 절대 줄 번호를 주면
    사용자가 이번 편집과 무관한 줄을 고치게 된다. 어느 경우든 기존 파일에만 있던
    제어문자는 섞이지 않는다 — 트리거 로직과 위치 보고가 같은 범위를 본다.
    """
    content = read_file(path) if path else None
    hits = []
    for text in texts:
        base = unique_offset(content, text) if content else -1
        for offset, ch in enumerate(text):
            if ch not in FORBIDDEN:
                continue
            if base >= 0:
                lineno, col = line_col(content, base + offset)
                hits.append((lineno, col, ch, True))
            else:
                lineno, col = line_col(text, offset)
                hits.append((lineno, col, ch, False))
            if len(hits) >= MAX_REPORT:
                return hits
    return hits


def format_where(path: str, texts: "list[str]") -> str:
    hits = locate(path, texts)
    if not hits:
        return "위치: 특정하지 못함. 위 grep 으로 직접 확인하라."
    name = os.path.basename(path) if path else "(경로 불명)"
    lines = []
    for lineno, col, ch, absolute in hits:
        if absolute:
            lines.append("  %s:%d  (열 %d) %s" % (name, lineno, col, repr(ch)))
        else:
            lines.append(
                "  %s  작성 텍스트 %d번째 줄 (열 %d) %s — 파일에서 위치를 특정하지 못해(미발견 또는 중복) 상대 위치"
                % (name, lineno, col, repr(ch))
            )
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
    if not isinstance(tool_name, str) or not tool_name:
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    texts = written_texts(tool_input)
    chars = found_chars(texts)
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
                where=format_where(path, texts),
            ),
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
