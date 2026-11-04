#!/usr/bin/python3
"""Resolve the repository-adjacent backlog checkpoint package and run its CLI."""

from pathlib import Path
import sys


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent))

from scripts.backlog_checkpoint.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
