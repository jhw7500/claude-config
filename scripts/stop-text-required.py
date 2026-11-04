#!/usr/bin/env python3
"""Stop hook — 결과 보고가 없거나 최종 설명 본문이 대부분 영어이면 차단.

목적: 모델이 도구 결과 수신 후 텍스트 응답 없이 turn을 종료하는 패턴 방지.

원인 사례:
- 2026-05-08: jhw-notion p1-3b M0 spike에서 v5 header curl 결과 수신 후
  텍스트 응답 0줄로 turn 종료 → 사용자 인터럽트로 재개.
- 2026-05-14: pim-package-jhw sync-from-gitlab.sh brainstorming 중
  AskUserQuestion 답변 수신 직후 텍스트 0줄 + 후속 action 0개로 turn 종료
  → 사용자 인터럽트로 재개. 빈 assistant turn은 기존 has_tool/has_text 검사를
  모두 통과하므로 추가 분기로 별도 차단 필요.

동작:
- stdin으로 Claude Code가 hook payload(JSON) 전달
- payload.transcript_path 또는 payload.messages 에서 마지막 assistant turn 추출
- turn 단위 판정: transcript는 content block 하나당 JSONL record 하나로 기록되므로
  (thinking / text / tool_use 분리) 마지막 record 하나가 아니라 마지막 실제 user
  prompt 이후의 assistant record 전부를 하나의 turn으로 합쳐 본다.
- 통과 조건: turn의 **마지막 tool_use 이후**에 비어 있지 않은 text block이 있을 것.
  도입 텍스트만 내고 도구 호출로 끝내는 turn은 통과하지 않는다 (결과 보고 강제).
- 차단 조건 (둘 중 하나):
  (A) 마지막 tool_use 뒤에 thinking 등은 있으나 text가 0줄
  (B) 마지막 tool_use 뒤에 assistant 산출이 전혀 없음
      (도구 결과 수신 후 침묵 — 특히 AskUserQuestion)
- settle-retry: 차단 조건이 성립해도 transcript를 다시 읽어 재판정한다. Stop 훅이
  마지막 text block record보다 먼저 파일을 읽으면 직전 tool_use record가 turn의
  끝으로 보이기 때문이다 (2026-08-24 실측 오탐 2건).
- 차단 시 exit 2 + stderr 경고 → 모델 컨텍스트에 system-reminder로 노출
- 한국어 검사: 코드·URL·경로를 뺀 한글/영문 80자 이상 본문에서 한글이 10% 미만이면
  한국어 재작성을 요청한다. CLAUDE_STOP_KOREAN=0은 이 검사만 끈다.
- last_assistant_message가 있으면 최종 텍스트로 사용한다. 없는 파일 기반 언어 검사는
  처음에 짧은 본문만 보여도 전체 settle 시간 동안 다시 읽는다.

설치 위치: ~/.claude/settings.json hooks.Stop.[].hooks
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

EXPLICIT_STOP_HINTS = ("종료", "exit", "quit", "그만", "끝", "stop session")


def transcript_record_message(record: Any) -> dict | None:
    """JSONL envelope에서 주 대화의 user/assistant 메시지만 반환한다."""
    if not isinstance(record, dict):
        return None
    if record.get("isSidechain") is True or record.get("isApiErrorMessage") is True:
        return None
    message = record.get("message")
    if isinstance(message, dict) and message.get("role") in ("user", "assistant"):
        return message
    return None


def read_transcript_messages(payload: dict) -> list | None:
    """payload에서 messages 리스트를 끄집어낸다. 여러 스키마 호환."""
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        return msgs
    transcript_path = payload.get("transcript_path") or payload.get("transcriptPath")
    if not transcript_path:
        return None

    path = Path(str(transcript_path))
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        messages: list[dict] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                return None
            message = transcript_record_message(record)
            if message is not None:
                messages.append(message)
        return messages or None

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("messages")
        if isinstance(inner, list):
            return inner
        message = transcript_record_message(data)
        if message is not None:
            return [message]
    return None


def last_assistant_turn(messages: list) -> dict | None:
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return msg
    return None


def last_assistant_index(messages: list) -> int:
    """마지막 assistant turn의 인덱스. 없으면 -1."""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return i
    return -1


def user_tool_result_id(turn: dict) -> str | None:
    """user turn에 tool_result block이 있으면 그 tool_use_id를 반환."""
    content = turn.get("content")
    if not isinstance(content, list):
        return None
    for c in content:
        if isinstance(c, dict) and c.get("type") == "tool_result":
            tid = c.get("tool_use_id")
            if isinstance(tid, str):
                return tid
    return None


def find_tool_use_name(messages: list, tool_use_id: str) -> str:
    """tool_use_id에 해당하는 도구 이름을 assistant turn에서 찾는다."""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if (
                isinstance(c, dict)
                and c.get("type") in ("tool_use", "server_tool_use")
                and c.get("id") == tool_use_id
            ):
                name = c.get("name")
                if isinstance(name, str):
                    return name
    return ""


def last_user_text(messages: list) -> str:
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for c in content:
                if isinstance(c, dict) and c.get("type") in ("text", "input_text"):
                    text = c.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts)
    return ""


def assistant_has_text(turn: dict) -> bool:
    """assistant turn에 1자 이상의 text content가 있는지."""
    content = turn.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("text", "output_text"):
                text = c.get("text")
                if isinstance(text, str) and text.strip():
                    return True
    return False


def assistant_has_tool_use(turn: dict) -> bool:
    content = turn.get("content")
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("tool_use", "server_tool_use"):
                return True
    return False


SETTLE_ATTEMPTS = 8
SETTLE_DELAY_SEC = 0.1
KOREAN_MIN_LETTERS = 80
KOREAN_MIN_PERCENT = 10


def language_prose(text: str) -> str:
    """코드·명령·URL·경로·명확한 식별자를 제외하고 설명 문단을 남긴다."""
    lines: list[str] = []
    list_indents: list[int] = []
    fence_char = ""
    fence_length = 0
    fence_indent = 0
    paragraph_open = False
    for raw_line in text.splitlines():
        line = re.sub(r"^ {0,3}(?:> ?)+", "", raw_line.expandtabs(4))
        indentation = len(line) - len(line.lstrip(" "))
        if fence_char and fence_indent and line.strip() and indentation < fence_indent:
            # 목록 컨테이너가 끝나면 그 안의 닫히지 않은 fence도 끝난다.
            fence_char = ""
        if fence_char:
            fence = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", line[fence_indent:])
            if (
                fence
                and fence[1][0] == fence_char
                and len(fence[1]) >= fence_length
                and not fence[2].strip()
            ):
                fence_char = ""
            continue
        if not line.strip():
            lines.append("")
            paragraph_open = False
            continue
        while list_indents and indentation < list_indents[-1]:
            list_indents.pop()
        content_indent = list_indents[-1] if list_indents else 0
        # 들여쓴 코드는 문단을 끊지 못한다. 목록의 본문 시작 열을 기준으로 센다.
        if indentation >= content_indent + 4 and not paragraph_open:
            continue
        marker = re.match(r"( {0,3})([-+*]|\d+[.)])( +)", line[content_indent:])
        if marker:
            padding = len(marker[3]) if len(marker[3]) <= 4 else 1
            content_indent += len(marker[1]) + len(marker[2]) + padding
            list_indents.append(content_indent)
            paragraph_open = False
        candidate = line[content_indent:]
        if candidate.startswith("    ") and not paragraph_open:
            continue
        fence = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", candidate)
        if fence:
            fence_char, fence_length = fence[1][0], len(fence[1])
            fence_indent = content_indent
            paragraph_open = False
            continue
        command = re.match(
            r"(?:\$ +)?(?:rtk(?: +proxy)? +)?"
            r"(?:python(?:3(?:\.\d+)?)?|pytest|git|bash|sh|node|npm|npx|pnpm|yarn|uv|ruff|shellcheck|curl) +(.+)$",
            candidate.strip(),
        )
        if command and re.search(r"(?:^|\s)(?:--?\w|\S*[\\/]\S*|\S+\.(?:py|sh|[mc]?js)\b)", command[1]):
            continue
        lines.append(line)
        paragraph_open = True

    prose = re.sub(r"(`+).*?\1", " ", "\n".join(lines), flags=re.S)
    prose = re.sub(r"\b(?:https?://|www\.)[^\s<>`]+", " ", prose, flags=re.I)
    prose = re.sub(
        r"\b(?:[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+"
        r"|[A-Za-z][A-Za-z0-9]*[a-z][A-Z][A-Za-z0-9]*"
        r"|[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){2,})\b"
        r"|(?<!\w)--?[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*",
        " ", prose,
    )
    return re.sub(
        r"(?<![\w])(?:"
        r"(?:[A-Za-z]:[\\/]|\$\{HOME\}/|\$HOME/|~?/|\.{1,2}/)[^\s<>\"'`()\[\]{}]+"
        r"|(?:[\w.@+-]+[\\/])+[\w.@+\\/:~-]*"
        r"|[\w@+-]+(?:\.[\w@+-]+)+(?::\d+)?"
        r")",
        " ", prose,
    )


def needs_korean_rewrite(text: str) -> bool:
    """짧거나 기술 표기뿐인 답변은 면제하고 대부분 영어인 본문만 차단한다."""
    prose = language_prose(text)
    korean = len(re.findall(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]", prose))
    latin = len(re.findall(r"[A-Za-z]", prose))
    total = korean + latin
    return total >= KOREAN_MIN_LETTERS and korean * 100 < total * KOREAN_MIN_PERCENT


def current_assistant_turn(messages: list) -> tuple[list, dict | None]:
    """마지막 실제 user prompt 이후의 assistant record 전부와 마지막 message를 반환.

    turn 경계는 tool_result를 담지 않은 user message로 끊는다. tool_result user
    record는 같은 turn의 일부다.
    """
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if (
            isinstance(msg, dict)
            and msg.get("role") == "user"
            and user_tool_result_id(msg) is None
        ):
            start = i + 1
            break
    turn = [m for m in messages[start:] if isinstance(m, dict) and m.get("role") == "assistant"]
    return turn, (messages[-1] if messages else None)


def turn_blocks(turn: list) -> list:
    """turn의 assistant record들을 순서 그대로 content block 리스트로 편다."""
    blocks: list = []
    for msg in turn:
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                blocks.append({"type": "text", "text": content})
            continue
        if isinstance(content, list):
            blocks.extend(b for b in content if isinstance(b, dict))
    return blocks


def has_nonempty_text(blocks: list) -> bool:
    for b in blocks:
        if b.get("type") in ("text", "output_text"):
            text = b.get("text")
            if isinstance(text, str) and text.strip():
                return True
    return False


def evaluate(
    messages: list, *, check_korean: bool = True, final_text: str | None = None,
) -> tuple[str, str]:
    """('ok' | 'tool_only' | 'empty' | 'non_korean', tool_use_id) 판정.

    통과하려면 turn의 마지막 tool_use **이후**에 비어 있지 않은 text가 있어야 한다.
    """
    turn, last = current_assistant_turn(messages)
    blocks = turn_blocks(turn)

    last_tool = -1
    tool_id = ""
    for i, b in enumerate(blocks):
        if b.get("type") in ("tool_use", "server_tool_use"):
            last_tool = i
            tool_id = b.get("id") if isinstance(b.get("id"), str) else ""

    after = blocks[last_tool + 1:]
    if final_text is not None:
        # Stop의 공식 최종 텍스트는 transcript의 지연/부분 기록보다 우선한다.
        after = [{"type": "text", "text": final_text}]
    if has_nonempty_text(after):
        text = "\n".join(
            b["text"] for b in after
            if b.get("type") in ("text", "output_text") and isinstance(b.get("text"), str)
        )
        if check_korean and needs_korean_rewrite(text):
            return "non_korean", ""
        return "ok", ""

    if last_tool >= 0:
        # 산출이 전혀 없으면 '침묵' 분기, thinking 등만 있으면 'text 0줄' 분기
        return ("empty", tool_id) if not after else ("tool_only", "")

    # turn에 tool_use가 없는 경우 — 기존 분기 B 조건 유지
    if not has_nonempty_text(blocks):
        if isinstance(last, dict) and last.get("role") == "user":
            result_id = user_tool_result_id(last)
            if result_id:
                return "empty", result_id
    return "ok", ""


def settle(
    read_messages: Any,
    messages: list,
    verdict: str,
    tool_id: str,
    attempts: int = SETTLE_ATTEMPTS,
    delay: float = SETTLE_DELAY_SEC,
    sleep: Any = time.sleep,
    *,
    check_korean: bool = True,
    wait_for_final: bool = False,
) -> tuple[str, str, list]:
    """차단 판정을 transcript 재읽기로 확정한다.

    Stop 훅이 마지막 text block record보다 먼저 파일을 읽을 수 있으므로 차단
    조건이 성립해도 곧바로 확정하지 않는다. read_messages/sleep을 주입받아
    타이밍에 의존하지 않고 재시도 경로를 단위 테스트할 수 있게 한다.
    wait_for_final은 짧은 초기 본문을 최종 답변으로 오인하지 않도록 전체 횟수를 읽는다.
    """
    readable = True
    for _ in range(attempts):
        sleep(delay)
        retry = read_messages()
        if not retry:
            # 최종 record를 쓰는 도중이면 마지막 줄이 부분 JSON이라 파싱에 실패한다.
            # 일시적 상태이므로 남은 시도를 계속한다.
            readable = False
            continue
        readable = True
        messages = retry
        verdict, tool_id = evaluate(retry, check_korean=check_korean)
        if verdict == "ok" and not wait_for_final:
            return "ok", "", messages
    if not readable:
        # 끝까지 읽지 못했다 — malformed transcript와 동일하게 fail-open
        return "ok", "", messages
    return verdict, tool_id, messages


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload: Any = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    # 무한 루프 방지: 이미 한 번 차단했으면 그대로 통과 (stop_hook_active=True)
    if payload.get("stop_hook_active") is True:
        return 0

    check_korean = (
        os.environ.get("CLAUDE_STOP_KOREAN") != "0"
        and payload.get("hook_event_name", "Stop") == "Stop"
    )
    final_text = payload.get("last_assistant_message")
    if not isinstance(final_text, str):
        final_text = None
    messages = read_transcript_messages(payload)
    if not messages:
        if final_text is None:
            return 0
        messages = [{"role": "assistant", "content": final_text}]

    user_text = last_user_text(messages).lower()
    if any(hint in user_text for hint in EXPLICIT_STOP_HINTS):
        return 0  # 사용자가 명시적으로 종료 요청 — 통과

    verdict, tool_id = evaluate(messages, check_korean=check_korean, final_text=final_text)

    # transcript는 content block 단위로 append되므로 마지막 text block이 아직
    # 기록되지 않았을 수 있다. 파일 기반일 때만 재읽기로 확정한다.
    if (
        not isinstance(payload.get("messages"), list)
        and final_text is None
        and (verdict != "ok" or check_korean)
    ):
        verdict, tool_id, messages = settle(
            lambda: read_transcript_messages(payload), messages, verdict, tool_id,
            check_korean=check_korean, wait_for_final=check_korean,
        )

    if verdict == "non_korean":
        sys.stderr.write(
            "STOP_HOOK_KOREAN: 최종 답변의 설명 본문에서 한국어 비율이 낮습니다.\n"
            "직전 답변을 한국어로 다시 작성하세요. 코드·명령·경로·식별자는 원문을 유지하고, "
            "결과와 설명을 한국어로 전달하세요.\n"
        )
        return 2

    if verdict == "tool_only":
        sys.stderr.write(
            "STOP_HOOK_BLOCK: 마지막 assistant turn에 tool_use는 있고 텍스트 응답이 0줄입니다.\n"
            "도구 결과 수신 후 응답 종료 직전 자가점검을 통과하지 못했습니다.\n"
            "한 줄 이상의 결과 보고 또는 다음 action(도구 호출)을 같은 흐름에서 이어가세요.\n"
            "참조: ~/.claude/CLAUDE.md '정보 수집 → 조기 종료 방지' 2026-05-08 사례.\n"
        )
        return 2  # 차단

    # 추가 분기 (2026-05-14): turn에 assistant 산출이 전혀 없고
    # 직전 user turn이 도구 결과(특히 AskUserQuestion)인 경우 차단.
    if verdict == "empty":
        tool_label = find_tool_use_name(messages, tool_id) or "previous tool"
        sys.stderr.write(
            f"STOP_HOOK_BLOCK: 마지막 도구({tool_label}) 호출 이후 assistant 산출이 "
            "전혀 없습니다 — 결과 보고 없이 turn이 끝났습니다.\n"
            "AskUserQuestion 답변/도구 결과를 받았으면 같은 흐름에서 "
            "(a) 다음 질문, (b) 분석/액션 텍스트, (c) 후속 도구 호출 "
            "중 하나 이상을 출력해야 합니다.\n"
            "참조: ~/.claude/CLAUDE.md '정보 수집 → 조기 종료 방지' 2026-05-14 사례.\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
