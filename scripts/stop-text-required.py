#!/usr/bin/env python3
"""Stop hook — 마지막 assistant turn이 비어 있거나 도구만 호출하고 텍스트 0줄이면 차단.

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

설치 위치: ~/.claude/settings.json hooks.Stop.[].hooks
"""
from __future__ import annotations

import json
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


def evaluate(messages: list) -> tuple[str, str]:
    """('ok' | 'tool_only' | 'empty', tool_use_id) 판정.

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

    if last_tool >= 0:
        after = blocks[last_tool + 1:]
        if has_nonempty_text(after):
            return "ok", ""
        # 산출이 전혀 없으면 '침묵' 분기, thinking 등만 있으면 'text 0줄' 분기
        return ("empty", tool_id) if not after else ("tool_only", "")

    # turn에 tool_use가 없는 경우 — 기존 분기 B 조건 유지
    if not has_nonempty_text(blocks):
        if isinstance(last, dict) and last.get("role") == "user":
            result_id = user_tool_result_id(last)
            if result_id:
                return "empty", result_id
    return "ok", ""


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

    messages = read_transcript_messages(payload)
    if not messages:
        return 0

    user_text = last_user_text(messages).lower()
    if any(hint in user_text for hint in EXPLICIT_STOP_HINTS):
        return 0  # 사용자가 명시적으로 종료 요청 — 통과

    verdict, tool_id = evaluate(messages)

    # transcript는 content block 단위로 append되므로 마지막 text block이 아직
    # 기록되지 않았을 수 있다. 파일 기반일 때만 재읽기로 확정한다.
    if verdict != "ok" and not isinstance(payload.get("messages"), list):
        for _ in range(SETTLE_ATTEMPTS):
            time.sleep(SETTLE_DELAY_SEC)
            retry = read_transcript_messages(payload)
            if not retry:
                break
            messages = retry
            verdict, tool_id = evaluate(retry)
            if verdict == "ok":
                return 0

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
            f"STOP_HOOK_BLOCK: 직전 도구({tool_label}) 결과 수신 후 "
            "assistant turn이 텍스트 0줄, action 0개로 비어 있습니다.\n"
            "AskUserQuestion 답변/도구 결과를 받았으면 같은 흐름에서 "
            "(a) 다음 질문, (b) 분석/액션 텍스트, (c) 후속 도구 호출 "
            "중 하나 이상을 출력해야 합니다.\n"
            "참조: ~/.claude/CLAUDE.md '정보 수집 → 조기 종료 방지' 2026-05-14 사례.\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
