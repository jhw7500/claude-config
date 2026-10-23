"""Hook payload decoding and output for the commit gate (independent of tribunal)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

try:
    from . import gate
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from commit_gate import gate

MAX_STDIN_BYTES = 1024 * 1024
_SHELL_TOOLS = {"claude": ("Bash",), "codex": ("exec_command", "Bash", "shell", "local_shell")}


def _get(value: dict, snake: str, camel: str) -> object:
    return value.get(snake, value.get(camel))


def decode_request(raw: bytes, *, runtime: str) -> Optional[tuple[Path, str]]:
    """(cwd, command) for a shell tool call, else None (the gate does not apply)."""
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    event = _get(value, "hook_event_name", "hookEventName")
    tool = _get(value, "tool_name", "toolName")
    tool_input = _get(value, "tool_input", "toolInput")
    cwd = value.get("cwd")
    if event != "PreToolUse" or not isinstance(tool, str) or not isinstance(cwd, str):
        return None
    if tool not in _SHELL_TOOLS.get(runtime, ()) or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command", tool_input.get("cmd"))
    if not isinstance(command, str):
        return None
    return Path(cwd), command


def deny_output(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def adapter_main(runtime: str) -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
        if len(raw) > MAX_STDIN_BYTES:
            return 0
        request = decode_request(raw, runtime=runtime)
        if request is None:
            return 0
        cwd, command = request
        decision = gate.evaluate(
            command, cwd=cwd, runtime=runtime, home=gate.default_home(), now=time.time()
        )
    except Exception as error:  # noqa: BLE001 - fail open
        print(f"[commit-gate] hook error: {type(error).__name__}", file=sys.stderr)
        return 0
    if not decision.allow:
        sys.stdout.write(
            json.dumps(deny_output(decision.reason), ensure_ascii=False, separators=(",", ":")) + "\n"
        )
    return 0
