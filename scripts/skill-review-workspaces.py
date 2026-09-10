#!/usr/bin/env python3
"""Print canonical active observation workspaces, without reading log bodies."""

from pathlib import Path
import sys


def workspaces(anchor: Path, canonical_root: Path, projects_root: Path) -> list[Path]:
    anchor = anchor.resolve(strict=True)
    if not (anchor / "log.md").is_file():
        raise ValueError(f"missing observation log.md: {anchor}")
    candidates = [anchor]
    if canonical_root.exists():
        candidates.extend(path for path in canonical_root.iterdir()
                          if path.name not in {"archive", "skill-updates"}
                          and "merged-backup" not in path.name)
    if projects_root.exists():
        candidates.extend(path / "skill-observations" for path in projects_root.iterdir())
    result = sorted({path.resolve(strict=True) for path in candidates
                     if (path / "log.md").is_file()})
    if any("\n" in str(path) or "\r" in str(path) for path in result):
        raise ValueError("workspace path contains a line separator")
    return result


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: skill-review-workspaces.py <anchor> <canonical-root> <projects-root>")
    try:
        print("\n".join(map(str, workspaces(*(Path(arg) for arg in sys.argv[1:])))))
    except (OSError, ValueError) as exc:
        sys.exit(f"observation log.md inventory failed: {exc}")
