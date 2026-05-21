#!/usr/bin/env python3
"""Timestamp hook for Claude Code.

Modes:
  prompt  — UserPromptSubmit. Records start time and prints arrival timestamp.
  stop    — Stop. Prints completion timestamp + elapsed since prompt.

Output: JSON {"systemMessage": "..."} consumed by Claude Code to display
in the UI.

State: /tmp/claude-ts-<session_id> stores epoch float of prompt arrival.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path


def emit(msg: str) -> None:
    sys.stdout.write(json.dumps({"systemMessage": msg}))


def read_payload() -> dict:
    try:
        if sys.stdin.isatty():
            return {}
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def fmt_elapsed(secs: int) -> str:
    mins, s = divmod(secs, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f"{hrs}h {mins}m {s}s"
    if mins:
        return f"{mins}m {s}s"
    return f"{s}s"


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = read_payload()
    session_id = payload.get("session_id", "default")
    state_file = Path(f"/tmp/claude-ts-{session_id}")

    now = time.time()
    ts = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")

    if mode == "prompt":
        try:
            state_file.write_text(str(now))
        except Exception:
            pass
        emit(f"🕐 prompt @ {ts}")
    elif mode == "stop":
        elapsed = ""
        try:
            start = float(state_file.read_text())
            secs = int(now - start)
            elapsed = f" (took {fmt_elapsed(secs)})"
            state_file.unlink(missing_ok=True)
        except Exception:
            pass
        emit(f"✅ done @ {ts}{elapsed}")


if __name__ == "__main__":
    main()
