#!/usr/bin/env python3
"""Codex hook and stateless manual-check adapters for task-nudge."""

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


def _unknown(reason: str) -> core.RegistrationResult:
    return core.RegistrationResult(core.RegistrationStatus.UNKNOWN, None, reason)


def _write_hook(result: core.RegistrationResult) -> None:
    sys.stdout.write(json.dumps({"systemMessage": core.render_nudge_message(result)}, ensure_ascii=False) + "\n")


def _write_manual(result: core.RegistrationResult) -> None:
    projection = {
        "repository_slug": result.repository_slug,
        "registration_status": result.status.value,
    }
    if result.reason is not None:
        projection["reason"] = result.reason
    sys.stdout.write(json.dumps(projection, ensure_ascii=False, sort_keys=True) + "\n")


def _manual_result(cwd: str | None) -> core.RegistrationResult:
    if not isinstance(cwd, str) or not cwd:
        return _unknown("HOOK_INPUT_INVALID")
    try:
        identity = core.resolve_repository(Path(cwd))
    except core.NudgeError as error:
        return _unknown(error.reason)
    try:
        return core.query_registration(identity, _home())
    except Exception:
        return _unknown("PORTFOLIO_UNAVAILABLE")


def main(argv: list[str]) -> int:
    if argv[:1] == ["--manual-check"]:
        cwd = argv[2] if len(argv) == 3 and argv[1] == "--cwd" else None
        _write_manual(_manual_result(cwd))
        return 0
    try:
        payload = _read_payload()
        if _suppressed(payload):
            return 0
        event = core.parse_codex_event(payload)
        result = core.evaluate_event(event, _home(), os.environ)
        if result is None:
            return 0
    except core.NudgeError as error:
        result = _unknown(error.reason)
    except Exception:
        result = _unknown("PORTFOLIO_UNAVAILABLE")
    _write_hook(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
