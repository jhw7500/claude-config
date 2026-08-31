#!/usr/bin/env python3
"""Claude PreToolUse adapter for the shared task-nudge engine."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import task_nudge as core


MAX_HOOK_INPUT_BYTES = 1024 * 1024


def _read_payload() -> object:
    raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(raw) > MAX_HOOK_INPUT_BYTES:
        raise core.NudgeError("HOOK_INPUT_INVALID")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=core._unique_object)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise core.NudgeError("HOOK_INPUT_INVALID") from error
    if not isinstance(payload, dict):
        raise core.NudgeError("HOOK_INPUT_INVALID")
    return payload


def _suppressed(payload: object) -> bool:
    return isinstance(payload, dict) and (
        payload.get("is_subagent") is True or payload.get("already_decided") is True
    )


def _home() -> Path:
    value = os.environ.get("HOME")
    return Path(value) if isinstance(value, str) and Path(value).is_absolute() else Path("/nonexistent")


def main() -> int:
    try:
        payload = _read_payload()
        if _suppressed(payload):
            return 0
        event = core.parse_claude_event(payload)
        result = core.evaluate_event(event, _home(), os.environ)
        if result is None:
            return 0
    except core.NudgeError as error:
        result = core.RegistrationResult(core.RegistrationStatus.UNKNOWN, None, error.reason)
    except Exception:
        result = core.RegistrationResult(core.RegistrationStatus.UNKNOWN, None, "PORTFOLIO_UNAVAILABLE")
    sys.stdout.write(core.render_nudge_message(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
