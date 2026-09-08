#!/usr/bin/python3
"""Install the shared pre-PR tribunal package and runtime hooks atomically."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Sequence
import argparse
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
    SourcePrecondition,
    TargetSnapshot,
    apply_transaction as _apply_transaction,
    capture_source_directory,
    capture_source_file,
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
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


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


def preflight_skill_sources(
    skill: Path,
    *,
    before_source_open: Callable[[Path], None] | None = None,
    before_source_recheck: Callable[[Path], None] | None = None,
) -> tuple[SourcePrecondition, ...]:
    """Validate SKILL.md and every references/*.md as immutable guards."""
    references = skill / "references"
    skill_directory = capture_source_directory(skill)
    reference_directory = capture_source_directory(references)
    if not {"SKILL.md", "references"}.issubset(skill_directory.entry_names or ()):
        raise InstallError("required Skill source is missing")
    markdown_names = tuple(
        name for name in (reference_directory.entry_names or ()) if name.endswith(".md")
    )
    if not set(SKILL_REQUIRED_NAMES).issubset(markdown_names):
        raise InstallError("required Skill reference is missing")
    files = (skill / "SKILL.md", *(references / name for name in markdown_names))
    captured: list[SourcePrecondition] = [skill_directory, reference_directory]
    for path in files:
        if before_source_open is not None:
            before_source_open(path)
        guard = capture_source_file(path)
        if guard.content_sha256 == _EMPTY_SHA256:
            raise InstallError("required Skill source is empty")
        captured.append(guard)
    result = tuple(captured)
    if before_source_recheck is not None:
        before_source_recheck(skill)
    rechecked = (
        capture_source_directory(skill),
        capture_source_directory(references),
        *(capture_source_file(path) for path in files),
    )
    if rechecked != result:
        raise InstallError("Skill source changed during validation")
    return result


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
    skill_preconditions = preflight_skill_sources(
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
                source_preconditions=skill_preconditions,
            )
        )
    return plans


def apply_transaction(
    entries: list[PlannedWrite | PlannedSymlink],
    **kwargs,
) -> list[Path]:
    """Require one complete, identical Claude/Codex tribunal Skill pair."""
    managed: list[tuple[PlannedSymlink, Path, str]] = []
    for entry in entries:
        if not isinstance(entry, PlannedSymlink):
            continue
        if not (
            entry.path.name == "pre-pr-tribunal"
            and entry.path.parent.name == "skills"
            and entry.path.parent.parent.name in {".claude", ".codex"}
        ):
            continue
        runtime = entry.path.parent.parent.name
        home = entry.path.parent.parent.parent
        managed.append((entry, home, runtime))
        if (
            not isinstance(entry.source_preconditions, tuple)
            or not entry.source_preconditions
            or not all(
                isinstance(precondition, SourcePrecondition)
                for precondition in entry.source_preconditions
            )
        ):
            raise InstallError("managed Skill source preconditions required")
        by_path = {
            precondition.path: precondition
            for precondition in entry.source_preconditions
        }
        if len(by_path) != len(entry.source_preconditions):
            raise InstallError("managed Skill source preconditions conflict")
        if not entry.target.is_absolute():
            raise InstallError("managed Skill links require the same guarded source")
        if any(
            precondition.path != entry.target
            and entry.target not in precondition.path.parents
            for precondition in entry.source_preconditions
        ):
            raise InstallError("managed Skill source preconditions conflict")
        root = by_path.get(entry.target)
        references_path = entry.target / "references"
        references = by_path.get(references_path)
        if (
            root is None
            or root.entry_names is None
            or not {"SKILL.md", "references"}.issubset(root.entry_names)
            or references is None
            or references.entry_names is None
        ):
            raise InstallError("managed Skill source metadata required")
        markdown_names = tuple(
            name for name in references.entry_names if name.endswith(".md")
        )
        required_files = {
            entry.target / "SKILL.md",
            *(references_path / name for name in markdown_names),
        }
        expected_paths = {entry.target, references_path, *required_files}
        if (
            not set(SKILL_REQUIRED_NAMES).issubset(markdown_names)
            or set(by_path) != expected_paths
            or any(
                path not in by_path
                or by_path[path].content_sha256 is None
                or by_path[path].content_sha256 == _EMPTY_SHA256
                for path in required_files
            )
        ):
            if any(
                by_path.get(path) is not None
                and by_path[path].content_sha256 == _EMPTY_SHA256
                for path in required_files
            ):
                raise InstallError("managed Skill source file is empty")
            raise InstallError("managed Skill source metadata required")
    if len(managed) != 2:
        raise InstallError("managed Skill exact runtime pair required")
    homes = {home for _entry, home, _runtime in managed}
    runtimes = {runtime for _entry, _home, runtime in managed}
    home = managed[0][1]
    expected_targets = {
        home / ".claude/skills/pre-pr-tribunal",
        home / ".codex/skills/pre-pr-tribunal",
    }
    if (
        len(homes) != 1
        or runtimes != {".claude", ".codex"}
        or {entry.path for entry, _home, _runtime in managed} != expected_targets
    ):
        raise InstallError("managed Skill exact runtime pair required")
    first, second = (entry for entry, _home, _runtime in managed)
    if (
        first.target != second.target
        or first.source_preconditions != second.source_preconditions
    ):
        raise InstallError("managed Skill links require the same guarded source")
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
