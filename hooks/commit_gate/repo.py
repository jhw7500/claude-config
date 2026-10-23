"""Repository helpers shared by the commit gate and its grant CLI."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

UNBORN = "UNBORN"
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_JHW7500_ORIGIN = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"jhw7500/[^/\s]+?(?:\.git)?/?"
)


def git(directory: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def repo_root(directory: Path) -> Optional[Path]:
    top = git(directory, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def head(directory: Path) -> str:
    """The 40/64-hex HEAD commit, or ``"UNBORN"`` when there is none."""
    value = git(directory, "rev-parse", "--verify", "-q", "HEAD")
    return value if value and _SHA.fullmatch(value) else UNBORN


def origin_is_jhw7500(directory: Path) -> bool:
    url = git(directory, "remote", "get-url", "origin")
    return bool(url) and _JHW7500_ORIGIN.fullmatch(url) is not None
