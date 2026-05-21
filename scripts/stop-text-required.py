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
- 차단 조건 (둘 중 하나):
  (A) 마지막 assistant turn에 tool_use는 있지만 text content가 없음
  (B) 마지막 assistant turn이 완전히 비어 있고 직전 user turn이 도구 결과
      (특히 AskUserQuestion)인 경우
- 차단 시 exit 2 + stderr 경고 → 모델 컨텍스트에 system-reminder로 노출

설치 위치: ~/.claude/settings.json hooks.Stop.[].hooks
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

EXPLICIT_STOP_HINTS = ("종료", "exit", "quit", "그만", "끝", "stop session")


def read_transcript_messages(payload: dict) -> list | None:
    """payload에서 messages 리스트를 끄집어낸다. 여러 스키마 호환."""
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        return msgs
    transcript_path = payload.get("transcript_path") or payload.get("transcriptPath")
    if transcript_path and Path(str(transcript_path)).is_file():
        try:
            data = json.loads(Path(str(transcript_path)).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("messages")
            if isinstance(inner, list):
                return inner
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

    turn_idx = last_assistant_index(messages)
    if turn_idx < 0:
        return 0
    turn = messages[turn_idx]

    has_tool = assistant_has_tool_use(turn)
    has_text = assistant_has_text(turn)

    if has_tool and not has_text:
        sys.stderr.write(
            "STOP_HOOK_BLOCK: 마지막 assistant turn에 tool_use는 있고 텍스트 응답이 0줄입니다.\n"
            "도구 결과 수신 후 응답 종료 직전 자가점검을 통과하지 못했습니다.\n"
            "한 줄 이상의 결과 보고 또는 다음 action(도구 호출)을 같은 흐름에서 이어가세요.\n"
            "참조: ~/.claude/CLAUDE.md '정보 수집 → 조기 종료 방지' 2026-05-08 사례.\n"
        )
        return 2  # 차단

    # 추가 분기 (2026-05-14): 마지막 assistant turn이 완전히 비어 있고
    # 직전 user turn이 도구 결과(특히 AskUserQuestion)인 경우 차단.
    if not has_tool and not has_text and turn_idx > 0:
        prev = messages[turn_idx - 1]
        if isinstance(prev, dict) and prev.get("role") == "user":
            tool_id = user_tool_result_id(prev)
            if tool_id:
                tool_name = find_tool_use_name(messages, tool_id)
                tool_label = tool_name or "previous tool"
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
