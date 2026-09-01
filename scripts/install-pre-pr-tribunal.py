#!/usr/bin/python3
"""Install the shared pre-PR tribunal package and runtime hooks atomically."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Sequence
import argparse
import hashlib
import os
import stat
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
    render_json_config,
    strict_json_object,
)


PACKAGE_SOURCE = REPO / "hooks" / "pre_pr_tribunal"
SKILL_SOURCE = REPO / "skills" / "pre-pr-tribunal"
SKILL_REQUIRED_NAMES = (
    "reviewer-a.md",
    "reviewer-b.md",
    "reviewer-c.md",
    "report-schema.md",
)

SkillSourceSnapshot = tuple[tuple[str, tuple[int, int, int], str], ...]


class TribunalPlan(list[PlannedWrite | PlannedSymlink]):
    """A transaction plan bound to the exact preflighted Skill tree."""

    def __init__(
        self,
        entries: Sequence[PlannedWrite | PlannedSymlink],
        *,
        skill_source: Path,
        skill_snapshot: SkillSourceSnapshot,
    ) -> None:
        super().__init__(entries)
        self.skill_source = skill_source
        self.skill_snapshot = skill_snapshot


PACKAGE_NAMES = (
    "__init__.py",
    "claude_hook.py",
    "cli.py",
    "codex_hook.py",
    "gate.py",
    "git_state.py",
    "hook_common.py",
    "model.py",
    "shell_scan.py",
    "verdict_store.py",
)
CLAUDE_MATCHER = "Bash"
CLAUDE_COMMAND = (
    "/usr/bin/python3 $HOME/.local/share/claude-config/"
    "pre_pr_tribunal/claude_hook.py"
)
CODEX_COMMAND = (
    "/usr/bin/python3 $HOME/.local/share/claude-config/"
    "pre_pr_tribunal/codex_hook.py"
)


def _real_directory(path: Path, *, label: str, private: bool = False) -> Path:
    if not path.is_absolute():
        raise InstallError(f"{label} must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError(f"{label} is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise InstallError(f"{label} is unavailable")
    if private and metadata.st_uid != os.geteuid():
        raise InstallError(f"{label} must be owner-private")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InstallError(f"{label} is unavailable") from error
    inspect_target(PlannedWrite(resolved / ".pre-pr-home-probe", b"", 0o600, False))
    return resolved


def read_validated_sources(
    repo: Path,
    *,
    before_source_open: Callable[[Path], None] | None = None,
    before_source_recheck: Callable[[Path], None] | None = None,
) -> dict[str, bytes]:
    """Capture the deterministic required top-level package sources."""
    package = repo / "hooks" / "pre_pr_tribunal"
    return read_python_source_group(
        package,
        required_names=PACKAGE_NAMES,
        before_open=before_source_open,
        before_recheck=before_source_recheck,
    )


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _open_absolute_directory_no_symlinks(path: Path) -> int:
    """Open every absolute directory component without following symlinks."""
    if not path.is_absolute():
        raise InstallError("Skill source path must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        for component in path.parts[1:]:
            if component in {"", ".", ".."}:
                raise InstallError("Skill source path is invalid")
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise InstallError("Skill source directory is unavailable")
        return descriptor
    except InstallError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise InstallError("cannot open Skill source directory safely") from error


def _read_markdown_at(
    descriptor: int,
    name: str,
    path: Path,
    before_open: Callable[[Path], None] | None,
) -> tuple[bytes, tuple[int, int, int]]:
    try:
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except OSError as error:
        raise InstallError("required Skill source is missing") from error
    if not stat.S_ISREG(before.st_mode):
        raise InstallError("Skill source is not a regular file")
    if before_open is not None:
        before_open(path)
    source = -1
    try:
        source = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=descriptor,
        )
        after = os.fstat(source)
        if not stat.S_ISREG(after.st_mode) or _identity(before) != _identity(after):
            raise InstallError("Skill source identity changed during validation")
        chunks: list[bytes] = []
        while chunk := os.read(source, 64 * 1024):
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise InstallError("required Skill source is empty")
        return data, _identity(after)
    except InstallError:
        raise
    except OSError as error:
        raise InstallError("cannot read Skill source safely") from error
    finally:
        if source >= 0:
            os.close(source)


def preflight_skill_sources(
    skill: Path,
    *,
    before_source_open: Callable[[Path], None] | None = None,
    before_source_recheck: Callable[[Path], None] | None = None,
) -> SkillSourceSnapshot:
    """Validate SKILL.md and every references/*.md through retained directories."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    skill_descriptor = -1
    references_descriptor = -1
    reopened_skill_descriptor = -1
    try:
        skill_descriptor = _open_absolute_directory_no_symlinks(skill)
        skill_identity = _identity(os.fstat(skill_descriptor))
        skill_data, skill_file_identity = _read_markdown_at(
            skill_descriptor,
            "SKILL.md",
            skill / "SKILL.md",
            before_source_open,
        )
        references_descriptor = os.open(
            "references",
            flags,
            dir_fd=skill_descriptor,
        )
        reference_identity = _identity(os.fstat(references_descriptor))
        names = tuple(
            sorted(name for name in os.listdir(references_descriptor) if name.endswith(".md"))
        )
        if not set(SKILL_REQUIRED_NAMES).issubset(names):
            raise InstallError("required Skill reference is missing")
        references: list[tuple[str, tuple[int, int, int], str]] = []
        for name in names:
            data, identity = _read_markdown_at(
                references_descriptor,
                name,
                skill / "references" / name,
                before_source_open,
            )
            references.append(
                (
                    f"references/{name}",
                    identity,
                    hashlib.sha256(data).hexdigest(),
                )
            )
        if before_source_recheck is not None:
            before_source_recheck(skill)
        if tuple(
            sorted(name for name in os.listdir(references_descriptor) if name.endswith(".md"))
        ) != names:
            raise InstallError("Skill source set changed during validation")
        reopened_skill_descriptor = _open_absolute_directory_no_symlinks(skill)
        current_references = os.stat(
            "references",
            dir_fd=skill_descriptor,
            follow_symlinks=False,
        )
        if (
            _identity(os.fstat(reopened_skill_descriptor)) != skill_identity
            or _identity(current_references) != reference_identity
        ):
            raise InstallError("Skill source parent changed during validation")
        return (
            (".", skill_identity, ""),
            ("references", reference_identity, ""),
            (
                "SKILL.md",
                skill_file_identity,
                hashlib.sha256(skill_data).hexdigest(),
            ),
            *references,
        )
    except InstallError:
        raise
    except OSError as error:
        raise InstallError("cannot preflight Skill sources safely") from error
    finally:
        if reopened_skill_descriptor >= 0:
            os.close(reopened_skill_descriptor)
        if references_descriptor >= 0:
            os.close(references_descriptor)
        if skill_descriptor >= 0:
            os.close(skill_descriptor)


def _config_snapshots(home: Path) -> tuple[TargetSnapshot, TargetSnapshot]:
    claude = home / ".claude" / "settings.json"
    codex = home / ".codex" / "hooks.json"
    return (
        inspect_target(PlannedWrite(claude, b"", 0o600, True)),
        inspect_target(PlannedWrite(codex, b"", 0o600, True)),
    )


def merge_runtime_configs(
    home: Path,
    claude_command: str,
    codex_command: str,
    *,
    snapshots: tuple[TargetSnapshot, TargetSnapshot] | None = None,
) -> tuple[bytes, bytes]:
    """Merge both runtime groups from one immutable pair of snapshots."""
    claude_snapshot, codex_snapshot = snapshots or _config_snapshots(home)
    claude = strict_json_object(claude_snapshot.data) if claude_snapshot.exists else {}
    codex = strict_json_object(codex_snapshot.data) if codex_snapshot.exists else {}
    claude = merge_pre_tool_hook(
        claude,
        matcher=CLAUDE_MATCHER,
        command=claude_command,
        legacy_commands=(claude_command,),
        home=home,
    )
    codex = merge_pre_tool_hook(
        codex,
        matcher=None,
        command=codex_command,
        legacy_commands=(codex_command,),
        home=home,
    )
    return render_json_config(claude), render_json_config(codex)


def plan_package_and_configs(
    repo: Path,
    home: Path,
    sources: Mapping[str, bytes],
    configs: tuple[bytes, bytes],
    *,
    config_snapshots: tuple[TargetSnapshot, TargetSnapshot] | None = None,
) -> list[PlannedWrite]:
    """Preflight every target, then bind captured data to those snapshots."""
    del repo
    package = home / ".local" / "share" / "claude-config" / "pre_pr_tribunal"
    source_names = tuple(sorted(sources))
    package_paths = [package / name for name in source_names]
    package_snapshots = [
        inspect_target(PlannedWrite(path, b"", 0o600, True))
        for path in package_paths
    ]
    claude_path = home / ".claude" / "settings.json"
    codex_path = home / ".codex" / "hooks.json"
    claude_snapshot, codex_snapshot = config_snapshots or _config_snapshots(home)
    plans = [
        PlannedWrite(
            path,
            sources[name],
            0o600,
            True,
            precondition=snapshot,
        )
        for name, path, snapshot in zip(
            source_names,
            package_paths,
            package_snapshots,
            strict=True,
        )
    ]
    plans.extend(
        [
            PlannedWrite(
                claude_path,
                configs[0],
                0o600,
                True,
                precondition=claude_snapshot,
            ),
            PlannedWrite(
                codex_path,
                configs[1],
                0o600,
                True,
                precondition=codex_snapshot,
            ),
        ]
    )
    return plans


def build_plan(
    repo: Path,
    home: Path,
    *,
    before_source_open: Callable[[Path], None] | None = None,
    before_source_recheck: Callable[[Path], None] | None = None,
) -> list[PlannedWrite | PlannedSymlink]:
    """Capture and validate the complete tribunal transaction plan."""
    repo = _real_directory(Path(repo), label="repository")
    home = _real_directory(Path(home), label="HOME", private=True)
    sources = read_validated_sources(
        repo,
        before_source_open=before_source_open,
        before_source_recheck=before_source_recheck,
    )
    skill_source = repo / "skills" / "pre-pr-tribunal"
    skill_snapshot = preflight_skill_sources(
        skill_source,
        before_source_open=before_source_open,
        before_source_recheck=before_source_recheck,
    )
    snapshots = _config_snapshots(home)
    configs = merge_runtime_configs(
        home,
        CLAUDE_COMMAND,
        CODEX_COMMAND,
        snapshots=snapshots,
    )
    plans: list[PlannedWrite | PlannedSymlink] = plan_package_and_configs(
        repo,
        home,
        sources,
        configs,
        config_snapshots=snapshots,
    )
    for target in (
        home / ".claude" / "skills" / "pre-pr-tribunal",
        home / ".codex" / "skills" / "pre-pr-tribunal",
    ):
        link = PlannedSymlink(target, skill_source)
        plans.append(
            PlannedSymlink(
                target,
                skill_source,
                precondition=inspect_target(link),
            )
        )
    return TribunalPlan(
        plans,
        skill_source=skill_source,
        skill_snapshot=skill_snapshot,
    )


def apply_transaction(
    entries: list[PlannedWrite | PlannedSymlink],
    **kwargs,
) -> list[Path]:
    """Revalidate a bound Skill source before applying any target mutation."""
    if isinstance(entries, TribunalPlan):
        current = preflight_skill_sources(entries.skill_source)
        if current != entries.skill_snapshot:
            raise InstallError("Skill source changed since plan was built")
    return _apply_transaction(entries, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install pre-PR tribunal runtime")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--home")
    arguments = parser.parse_args(argv)
    repo = Path(arguments.repo)
    home_raw = arguments.home if arguments.home is not None else os.environ.get("HOME", "")
    home = Path(home_raw)
    if not repo.is_absolute() or not home.is_absolute():
        print(
            "install-pre-pr-tribunal: repo and HOME must be absolute",
            file=sys.stderr,
        )
        return 2
    try:
        changed = apply_transaction(
            build_plan(repo, home),
            namespace="pre-pr-tribunal",
        )
    except InstallError as error:
        print(f"install-pre-pr-tribunal: {error}", file=sys.stderr)
        return 1
    print(
        f"pre-pr-tribunal installed ({len(changed)} changed); "
        "review Codex hook trust with /hooks"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
