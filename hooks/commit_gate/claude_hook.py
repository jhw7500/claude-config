#!/usr/bin/python3
"""Claude PreToolUse adapter for the commit-time gate."""

from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from commit_gate.hook_io import adapter_main  # type: ignore
else:
    from .hook_io import adapter_main


if __name__ == "__main__":
    raise SystemExit(adapter_main("claude"))
