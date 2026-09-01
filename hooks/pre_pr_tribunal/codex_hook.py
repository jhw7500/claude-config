#!/usr/bin/python3
"""Codex PreToolUse adapter for tribunal PR gating."""

from pathlib import Path
import sys

if __package__ in {None, ""}:
    package_parent = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(package_parent))
    from pre_pr_tribunal.hook_common import adapter_main  # type: ignore
else:
    from .hook_common import adapter_main


if __name__ == "__main__":
    raise SystemExit(adapter_main("codex"))
