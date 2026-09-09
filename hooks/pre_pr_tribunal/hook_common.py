"""Strict bounded payload handling shared by tribunal hook adapters."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

from .gate import GateCode, evaluate_gate
from .model import SchemaError, _load_json


MAX_STDIN_BYTES = 1024 * 1024
_AMBIGUITY_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


@dataclass(frozen=True)
class HookRequest:
    cwd: Path
    command: str


def _alias(value: dict[str, object], snake: str, camel: str) -> object:
    present = [name for name in (snake, camel) if name in value]
    if len(present) != 1:
        raise ValueError
    return value[present[0]]


def decode_request(raw: bytes, *, runtime: str) -> HookRequest | None:
    try:
        value = _load_json(
            raw, limit=MAX_STDIN_BYTES, too_large="HOOK_PAYLOAD_TOO_LARGE"
        )
        if not isinstance(value, dict):
            return None
        event = _alias(value, "hook_event_name", "hookEventName")
        tool = _alias(value, "tool_name", "toolName")
        tool_input = _alias(value, "tool_input", "toolInput")
        cwd = value.get("cwd")
        if (
            event != "PreToolUse"
            or not isinstance(tool, str)
            or not isinstance(tool_input, dict)
            or not isinstance(cwd, str)
        ):
            return None
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        if runtime == "claude" and tool != "Bash":
            return None
        if runtime != "claude" and runtime != "codex":
            return None
        return HookRequest(Path(cwd), command)
    except (SchemaError, OSError, TypeError, ValueError):
        return None
    except Exception:
        return None


def deny_output(
    code: GateCode, ambiguity_reason: str | None = None
) -> dict[str, object]:
    bounded_reason = (
        ambiguity_reason
        if code is GateCode.COMMAND_AMBIGUOUS
        and isinstance(ambiguity_reason, str)
        and _AMBIGUITY_REASON.fullmatch(ambiguity_reason) is not None
        else None
    )
    marker = f"[PRE-PR-TRIBUNAL:{code.value}"
    if bounded_reason is not None:
        marker += f":{bounded_reason}"
    marker += "]"
    if code is GateCode.COMMAND_AMBIGUOUS:
        guidance = (
            "PR 생성 명령 형태가 허용된 canonical 형식과 다릅니다. "
            "shell context와 target 옵션을 수정하세요."
        )
    else:
        guidance = "현재 diff에서 pre-pr-tribunal Skill을 다시 실행하세요."
    reason = f"{marker} PR 생성이 차단되었습니다. {guidance}"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _write_deny(code: GateCode, ambiguity_reason: str | None = None) -> None:
    try:
        output = json.dumps(
            deny_output(code, ambiguity_reason),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        sys.stdout.write(output + "\n")
    except Exception:
        pass


def adapter_main(runtime: str) -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    except Exception:
        return 0
    if len(raw) > MAX_STDIN_BYTES:
        _write_deny(GateCode.COMMAND_AMBIGUOUS)
        return 0
    request = decode_request(raw, runtime=runtime)
    if request is None:
        return 0
    try:
        decision = evaluate_gate(request.cwd, request.command)
        code = decision.code
        block = decision.block
        ambiguity_reason = decision.reason
    except Exception:
        code = GateCode.VERDICT_INVALID
        block = True
        ambiguity_reason = None
    if not block:
        return 0
    _write_deny(code, ambiguity_reason)
    return 0
