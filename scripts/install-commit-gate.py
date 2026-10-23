#!/usr/bin/python3
"""Install the commit-time gate package and its Claude/Codex hooks atomically."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence
import argparse
import os
import stat
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]
LIB = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(LIB))

from runtime_hook_installer import (  # noqa: E402
    InstallError,
    PlannedSymlink,
    PlannedWrite,
    TargetSnapshot,
    apply_transaction as _apply_transaction,
    inspect_target,
    merge_pre_tool_hook,
    read_python_source_group,
    read_regular_source,
    render_json_config,
    strict_json_object,
)


PACKAGE_NAMES = (
    "__init__.py",
    "claude_hook.py",
    "cli.py",
    "codex_hook.py",
    "commit_scan.py",
    "gate.py",
    "grant.py",
    "hook_io.py",
    "repo.py",
)
VENDOR_NAMES = (
    "check_commit_message.py",
    "commit_title_rules.py",
    "validate_change_evidence.py",
)
VENDOR_DATA_NAMES = ("SOURCE.json",)
EMPTY_MARKER = "__init__.py"
CLAUDE_MATCHER = "Bash"
CLAUDE_COMMAND = (
    "/usr/bin/python3 $HOME/.local/share/claude-config/commit_gate/claude_hook.py"
)
CODEX_COMMAND = (
    "/usr/bin/python3 $HOME/.local/share/claude-config/commit_gate/codex_hook.py"
)


def _real_directory(path: Path, *, label: str, private: bool = False) -> Path:
    if not path.is_absolute():
        raise InstallError(f"{label} must be absolute")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InstallError(f"{label} is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise InstallError(f"{label} is unavailable")
    if private and metadata.st_uid != os.geteuid():
        raise InstallError(f"{label} must be owner-private")
    inspect_target(PlannedWrite(resolved / ".commit-gate-home-probe", b"", 0o600, False))
    return resolved


def _read_empty_marker(path: Path) -> bytes:
    """The package marker must be a regular, empty file (not a link or a payload)."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError("required Python source is missing") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
        raise InstallError("package marker must be an empty regular file")
    return b""


def check_vendor(repo: Path) -> None:
    """Refuse to install when the vendored rule copies drift from SOURCE.json."""
    script = repo / "scripts" / "sync-commit-gate-vendor.py"
    vendor = repo / "hooks" / "commit_gate" / "vendor"
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-I", str(script), "--check", "--vendor-dir", str(vendor)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError("vendor check could not run") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise InstallError(
            "vendor copies drifted from SOURCE.json: " + (detail[0] if detail else "check failed")
        )


def read_validated_sources(
    repo: Path,
    *,
    before_source_open: Callable[[Path], None] | None = None,
    before_source_recheck: Callable[[Path], None] | None = None,
) -> dict[str, bytes]:
    """Capture the closed package list; keys are paths relative to the package."""
    package = repo / "hooks" / "commit_gate"
    # read_python_source_group rejects empty sources, and the package marker
    # __init__.py is intentionally empty, so the closed list is read per file.
    top = {
        name: read_regular_source(package / name, before_open=before_source_open)
        for name in PACKAGE_NAMES
        if name != EMPTY_MARKER
    }
    top[EMPTY_MARKER] = _read_empty_marker(package / EMPTY_MARKER)
    if before_source_recheck is not None:
        before_source_recheck(package)
    vendor = read_python_source_group(
        package / "vendor",
        required_names=VENDOR_NAMES,
        before_open=before_source_open,
        before_recheck=before_source_recheck,
    )
    sources = {name: top[name] for name in PACKAGE_NAMES}
    sources.update({f"vendor/{name}": vendor[name] for name in VENDOR_NAMES})
    for name in VENDOR_DATA_NAMES:
        sources[f"vendor/{name}"] = read_regular_source(
            package / "vendor" / name, before_open=before_source_open
        )
    return sources


def _config_snapshots(home: Path) -> tuple[TargetSnapshot, TargetSnapshot]:
    return (
        inspect_target(PlannedWrite(home / ".claude" / "settings.json", b"", 0o600, True)),
        inspect_target(PlannedWrite(home / ".codex" / "hooks.json", b"", 0o600, True)),
    )


def merge_runtime_configs(
    home: Path,
    *,
    snapshots: tuple[TargetSnapshot, TargetSnapshot] | None = None,
) -> tuple[bytes, bytes]:
    claude_snapshot, codex_snapshot = snapshots or _config_snapshots(home)
    claude = strict_json_object(claude_snapshot.data) if claude_snapshot.exists else {}
    codex = strict_json_object(codex_snapshot.data) if codex_snapshot.exists else {}
    claude = merge_pre_tool_hook(
        claude,
        matcher=CLAUDE_MATCHER,
        command=CLAUDE_COMMAND,
        legacy_commands=(CLAUDE_COMMAND,),
        home=home,
    )
    codex = merge_pre_tool_hook(
        codex,
        matcher=None,
        command=CODEX_COMMAND,
        legacy_commands=(CODEX_COMMAND,),
        home=home,
    )
    return render_json_config(claude), render_json_config(codex)


def build_plan(
    repo: Path,
    home: Path,
    *,
    before_source_open: Callable[[Path], None] | None = None,
    before_source_recheck: Callable[[Path], None] | None = None,
) -> list[PlannedWrite | PlannedSymlink]:
    repo = _real_directory(Path(repo), label="repository")
    home = _real_directory(Path(home), label="HOME", private=True)
    check_vendor(repo)
    sources = read_validated_sources(
        repo,
        before_source_open=before_source_open,
        before_source_recheck=before_source_recheck,
    )
    snapshots = _config_snapshots(home)
    configs = merge_runtime_configs(home, snapshots=snapshots)
    package = home / ".local" / "share" / "claude-config" / "commit_gate"
    plans: list[PlannedWrite | PlannedSymlink] = []
    for name in sorted(sources):
        path = package / name
        plans.append(
            PlannedWrite(
                path,
                sources[name],
                0o600,
                True,
                precondition=inspect_target(PlannedWrite(path, b"", 0o600, True)),
            )
        )
    plans.append(
        PlannedWrite(
            home / ".claude" / "settings.json", configs[0], 0o600, True,
            precondition=snapshots[0],
        )
    )
    plans.append(
        PlannedWrite(
            home / ".codex" / "hooks.json", configs[1], 0o600, True,
            precondition=snapshots[1],
        )
    )
    return plans


def apply_transaction(
    entries: list[PlannedWrite | PlannedSymlink],
    **kwargs,
) -> list[Path]:
    return _apply_transaction(entries, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install commit-time gate runtime")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--home")
    arguments = parser.parse_args(argv)
    repo = Path(arguments.repo)
    home_raw = arguments.home if arguments.home is not None else os.environ.get("HOME", "")
    home = Path(home_raw)
    if not repo.is_absolute() or not home.is_absolute():
        print("install-commit-gate: repo and HOME must be absolute", file=sys.stderr)
        return 2
    try:
        changed = apply_transaction(build_plan(repo, home), namespace="commit-gate")
    except InstallError as error:
        print(f"install-commit-gate: {error}", file=sys.stderr)
        return 1
    print(
        f"commit-gate installed ({len(changed)} changed); "
        "review Codex hook trust with /hooks"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
