#!/usr/bin/python3
"""Install shared backlog-drain Skill and helper links into a selected HOME."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys
from typing import Sequence


LIB = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(LIB))

from runtime_hook_installer import (  # noqa: E402
    InstallError,
    PlannedSymlink,
    apply_transaction,
    capture_source_directory,
    capture_source_file,
    inspect_target,
)


def _directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise InstallError(f"{label} must be absolute")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InstallError(f"{label} is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise InstallError(f"{label} is unavailable")
    if label == "HOME" and metadata.st_uid != os.geteuid():
        raise InstallError("HOME must be owned by the current user")
    return resolved


def build_plan(repo: Path, home: Path) -> list[PlannedSymlink]:
    repo = _directory(repo, "repository")
    home = _directory(home, "HOME")
    skill = repo / "skills/backlog-drain"
    references = skill / "references"
    helper = repo / "scripts/backlog-checkpoint.py"
    skill_guards = (
        capture_source_directory(skill),
        capture_source_directory(references),
        capture_source_file(skill / "SKILL.md"),
        capture_source_file(references / "protocol.md"),
    )
    helper_guards = (capture_source_file(helper),)
    entries: list[PlannedSymlink] = []
    for runtime in (".claude", ".codex"):
        for target, source, guards in (
            (home / runtime / "skills/backlog-drain", skill, skill_guards),
            (home / runtime / "scripts/backlog-checkpoint.py", helper, helper_guards),
        ):
            planned = PlannedSymlink(target, source)
            entries.append(
                PlannedSymlink(
                    target,
                    source,
                    precondition=inspect_target(planned),
                    source_preconditions=guards,
                )
            )
    return entries


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install backlog-drain assets")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--home", required=True)
    arguments = parser.parse_args(argv)
    try:
        changed = apply_transaction(
            build_plan(Path(arguments.repo), Path(arguments.home)),
            namespace="backlog-drain",
        )
    except InstallError as exc:
        print(f"install-backlog-drain: {exc}", file=sys.stderr)
        return 1
    print(f"backlog-drain installed ({len(changed)} changed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
