#!/usr/bin/python3
"""Install the shared task-nudge runtime without partial target updates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
import argparse
import json
import os
import re
import secrets
import stat
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
AGENTS_START = "<!-- claude-config:task-nudge:START -->"
AGENTS_END = "<!-- claude-config:task-nudge:END -->"
CLAUDE_MATCHER = "Edit|Write|NotebookEdit"
CLAUDE_COMMAND = "$HOME/.claude/hooks/task-nudge.sh"
CODEX_MATCHER = "apply_patch|Edit|Write"
CODEX_COMMAND = "/usr/bin/python3 $HOME/.local/share/claude-config/hooks/task-nudge-codex.py"


@dataclass(frozen=True)
class PlannedWrite:
    path: Path
    data: bytes
    mode: int
    backup: bool
    allow_legacy_symlink: bool = False


@dataclass(frozen=True)
class _TargetSnapshot:
    exists: bool
    data: bytes = b""
    mode: int = 0
    link_target: str | None = None


class InstallError(Exception):
    """A bounded installer failure safe to surface without file contents."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise InstallError("invalid JSON: duplicate key")
        value[key] = item
    return value


def load_json_config(path: Path) -> dict[str, object]:
    """Load one optional configuration object with duplicate-key rejection."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise InstallError("cannot read JSON configuration") from error
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except InstallError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise InstallError("invalid JSON configuration") from error
    if not isinstance(value, dict):
        raise InstallError("JSON configuration must be an object")
    return value


def render_json_config(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def managed_group(matcher: str, command: str) -> dict[str, object]:
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    }


def normalize_command(command: object, home: Path) -> str | None:
    """Normalize HOME spellings only at a shell-token path boundary."""
    if not isinstance(command, str):
        return None
    home_text = str(home)
    if not home_text or home_text == "/":
        return command
    pattern = re.compile(r"(?:(?<=^)|(?<=\s))" + re.escape(home_text) + r"(?=/|$)")
    return pattern.sub("$HOME", command)


def _validated_pre_tool_groups(config: dict[str, object]) -> list[dict[str, object]]:
    hooks = config.get("hooks")
    if hooks is None:
        hooks = {}
        config["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise InstallError("hooks must be an object")
    groups = hooks.get("PreToolUse")
    if groups is None:
        groups = []
        hooks["PreToolUse"] = groups
    if not isinstance(groups, list):
        raise InstallError("PreToolUse hooks must be a list")
    for group in groups:
        if not isinstance(group, dict):
            raise InstallError("hook group must be an object")
        matcher = group.get("matcher")
        if matcher is not None and not isinstance(matcher, str):
            raise InstallError("hook matcher must be a string")
        records = group.get("hooks")
        if not isinstance(records, list) or not records:
            raise InstallError("hook records must be a non-empty list")
        for record in records:
            if (
                not isinstance(record, dict)
                or set(record) != {"type", "command"}
                or record.get("type") != "command"
                or not isinstance(record.get("command"), str)
            ):
                raise InstallError("unknown hook record shape")
    return groups


def merge_hook_config(
    original: dict[str, object],
    *,
    matcher: str,
    command: str,
    legacy_commands: tuple[str, ...],
    home: Path,
) -> dict[str, object]:
    """Add or replace exactly one managed PreToolUse command group."""
    if not isinstance(original, dict):
        raise InstallError("JSON configuration must be an object")
    merged = deepcopy(original)
    groups = _validated_pre_tool_groups(merged)
    managed_commands = {
        normalized
        for candidate in (command, *legacy_commands)
        if (normalized := normalize_command(candidate, home)) is not None
    }
    found: list[int] = []
    for index, group in enumerate(groups):
        records = group["hooks"]
        assert isinstance(records, list)
        matching = [
            record
            for record in records
            if normalize_command(record["command"], home) in managed_commands
        ]
        if matching and len(records) != 1:
            raise InstallError("managed command appears in a multi-hook group")
        if matching:
            if set(group) != {"matcher", "hooks"}:
                raise InstallError("managed hook group has unknown fields")
            found.append(index)
    if len(found) > 1:
        raise InstallError("multiple managed hook groups conflict")
    replacement = managed_group(matcher, command)
    if found:
        groups[found[0]] = replacement
    else:
        groups.append(replacement)
    return merged


def select_agents_path(home: Path) -> Path:
    """Select the active global Codex instruction file."""
    codex = home / ".codex"
    override = codex / "AGENTS.override.md"
    try:
        raw = override.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as error:
        raise InstallError("cannot read AGENTS override") from error
    try:
        nonempty = bool(raw.decode("utf-8").strip())
    except UnicodeDecodeError as error:
        raise InstallError("AGENTS override is not UTF-8") from error
    return override if nonempty else codex / "AGENTS.md"


def merge_agents_block(original: str, policy: str) -> str:
    """Append or replace one exact managed pair without changing outside bytes."""
    start_count = original.count(AGENTS_START)
    end_count = original.count(AGENTS_END)
    if start_count != end_count or start_count not in {0, 1}:
        raise InstallError("malformed AGENTS task-nudge markers")
    body = policy.rstrip("\n")
    block = AGENTS_START + "\n" + body + "\n" + AGENTS_END
    if start_count == 0:
        separator = "" if not original or original.endswith("\n") else "\n"
        return original + separator + block + "\n"
    start = original.index(AGENTS_START)
    end = original.index(AGENTS_END)
    if end < start:
        raise InstallError("reversed AGENTS task-nudge markers")
    return original[:start] + block + original[end + len(AGENTS_END) :]


def agents_policy_block() -> str:
    """Return the fallback policy kept semantically aligned with Task 3."""
    return (
        "첫 실질 변경 전에, 현재 세션에서 native [TASK-NUDGE]를 이미 받았거나 Task 선택을 이미 결정했다면 반복하지 않는다. "
        "그렇지 않으면 다음 stateless checker를 한 번 실행한다(설치/credential/Project Control mutation은 하지 않는다):\n"
        '/usr/bin/python3 $HOME/.local/share/claude-config/hooks/task-nudge-codex.py --manual-check --cwd "$PWD"\n'
        "반환된 repository_slug와 registration_status에 다음 우선순서를 적용해 하나만 추천한다: "
        "(1) 이미 결정됨·제외 작업(조회/Q&A·단순 문서/설정·subagent)은 Task 없이 진행; "
        "(2) backlog(unknown 포함)는 등록 상태와 무관하게 GitHub Issue만 제안하고 Task/Claim을 시작하지 않음; "
        "(3) 즉시 작업의 unknown이면 등록 여부를 가정하지 말고 현재 변경을 중단하고 bounded 오류의 복구 필요만 알림; "
        "(4) 등록 저장소의 즉시 작업은 기존 Issue 또는 반복 증거면 Formal Issue Task, 현재 세션의 제한 작업이면 Temporary Task, "
        "조정 비용보다 작으면 Task 없음; "
        "(5) 미등록 저장소의 즉시 작업은 반복 증거가 있을 때만 Project/Repository 등록만 먼저 제안하고, 아니면 Task 없음.\n"
        "반복·다중 세션 증거는 다음 셋뿐이다: 사용자의 장기·반복·여러 세션 명시, "
        "기존 GitHub Issue·승인된 계획·Handoff, 여러 구현 단계와 검증이 필요한 아키텍처 작업. "
        "파일 수나 저장소 안에 있다는 사실은 증거가 아니다.\n"
        "GitHub Issue 생성, Project/Repository 등록, Formal 또는 Temporary Task 시작은 각각 별도의 명시적 사용자 승인 후에만 한다. "
        "앞 단계 승인은 다음 단계를 승인하지 않는다.\n"
    )


def _validate_absolute_directory(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise InstallError(f"{label} must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError(f"{label} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise InstallError(f"{label} must be a regular directory")


def _read_source(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise InstallError("required source is not a regular file")
        data = path.read_bytes()
    except InstallError:
        raise
    except OSError as error:
        raise InstallError("cannot read required source") from error
    if not data:
        raise InstallError("required source is empty")
    return data


def _validate_ancestors(path: Path) -> None:
    current = path.parent
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise InstallError("cannot inspect target parent") from error
        else:
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise InstallError("target parent is not a regular directory")
        if current == current.parent:
            return
        current = current.parent


def _inspect_target(plan: PlannedWrite) -> _TargetSnapshot:
    if not plan.path.is_absolute():
        raise InstallError("planned target must be absolute")
    if plan.mode not in {0o600, 0o700}:
        raise InstallError("planned target mode is not owner-only")
    _validate_ancestors(plan.path)
    try:
        metadata = plan.path.lstat()
    except FileNotFoundError:
        return _TargetSnapshot(False)
    except OSError as error:
        raise InstallError("cannot inspect planned target") from error
    if stat.S_ISLNK(metadata.st_mode):
        is_legacy_shim = (
            plan.allow_legacy_symlink
            and plan.path.name == "task-nudge.sh"
            and plan.path.parent.name == "hooks"
            and plan.path.parent.parent.name == ".claude"
        )
        if not is_legacy_shim:
            raise InstallError("planned target is an unsafe symlink")
        try:
            target_metadata = plan.path.stat()
            if not stat.S_ISREG(target_metadata.st_mode):
                raise InstallError("legacy shim symlink target is not regular")
            data = plan.path.read_bytes()
            link_target = os.readlink(plan.path)
        except InstallError:
            raise
        except OSError as error:
            raise InstallError("cannot read legacy shim symlink") from error
        return _TargetSnapshot(True, data, stat.S_IMODE(target_metadata.st_mode), link_target)
    if not stat.S_ISREG(metadata.st_mode):
        raise InstallError("planned target is not a regular file")
    try:
        data = plan.path.read_bytes()
    except OSError as error:
        raise InstallError("cannot read planned target") from error
    return _TargetSnapshot(True, data, stat.S_IMODE(metadata.st_mode))


def _read_agents(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as error:
        raise InstallError("cannot read active AGENTS file") from error
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InstallError("active AGENTS file is not UTF-8") from error


def build_plan(repo: Path, home: Path) -> list[PlannedWrite]:
    """Validate every input and return the complete task-nudge write set."""
    repo = Path(repo)
    home = Path(home)
    _validate_absolute_directory(repo, "repository")
    _validate_absolute_directory(home, "HOME")
    _validate_absolute_directory(repo / "hooks", "source hooks directory")

    source_names = (
        "task_nudge.py",
        "task-nudge-claude.py",
        "task-nudge-codex.py",
        "task-nudge.sh",
    )
    sources = {name: _read_source(repo / "hooks" / name) for name in source_names}

    claude_config = home / ".claude" / "settings.json"
    codex_config = home / ".codex" / "hooks.json"
    for config_path in (claude_config, codex_config):
        _inspect_target(PlannedWrite(config_path, b"", 0o600, True))
    claude_value = merge_hook_config(
        load_json_config(claude_config),
        matcher=CLAUDE_MATCHER,
        command=CLAUDE_COMMAND,
        legacy_commands=(CLAUDE_COMMAND,),
        home=home,
    )
    codex_value = merge_hook_config(
        load_json_config(codex_config),
        matcher=CODEX_MATCHER,
        command=CODEX_COMMAND,
        legacy_commands=(),
        home=home,
    )

    override = home / ".codex" / "AGENTS.override.md"
    if override.exists() or override.is_symlink():
        _inspect_target(PlannedWrite(override, b"", 0o600, True))
    agents_path = select_agents_path(home)
    _inspect_target(PlannedWrite(agents_path, b"", 0o600, True))
    agents_data = merge_agents_block(_read_agents(agents_path), agents_policy_block()).encode("utf-8")

    installed = home / ".local" / "share" / "claude-config" / "hooks"
    plans = [
        PlannedWrite(installed / "task_nudge.py", sources["task_nudge.py"], 0o600, True),
        PlannedWrite(installed / "task-nudge-claude.py", sources["task-nudge-claude.py"], 0o600, True),
        PlannedWrite(installed / "task-nudge-codex.py", sources["task-nudge-codex.py"], 0o600, True),
        PlannedWrite(
            home / ".claude" / "hooks" / "task-nudge.sh",
            sources["task-nudge.sh"],
            0o700,
            True,
            allow_legacy_symlink=True,
        ),
        PlannedWrite(claude_config, render_json_config(claude_value), 0o600, True),
        PlannedWrite(codex_config, render_json_config(codex_value), 0o600, True),
        PlannedWrite(agents_path, agents_data, 0o600, True),
    ]
    seen: set[Path] = set()
    for plan in plans:
        if plan.path in seen:
            raise InstallError("duplicate planned target")
        seen.add(plan.path)
        _inspect_target(plan)
    return plans


def _make_private_parents(paths: list[Path]) -> list[Path]:
    missing: set[Path] = set()
    for path in paths:
        current = path.parent
        while not current.exists():
            missing.add(current)
            current = current.parent
    created: list[Path] = []
    try:
        for directory in sorted(missing, key=lambda item: (len(item.parts), os.fspath(item))):
            directory.mkdir(mode=0o700)
            created.append(directory)
            metadata = directory.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise InstallError("private target directory is unsafe")
            directory.chmod(0o700)
        return created
    except FileExistsError as error:
        failure: BaseException = InstallError("private target directory changed during install")
        failure.__cause__ = error
    except OSError as error:
        failure = InstallError("cannot create private target directory")
        failure.__cause__ = error
    except BaseException as error:
        failure = error
    for directory in sorted(created, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    raise failure


def _stage_regular(path: Path, data: bytes, mode: int, prefix: str) -> Path:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise InstallError("cannot stage transactional file") from error


def _stage_restore(path: Path, snapshot: _TargetSnapshot) -> Path:
    if snapshot.link_target is None:
        return _stage_regular(path, snapshot.data, snapshot.mode, ".task-nudge-restore.")
    for _ in range(128):
        temporary = path.parent / (".task-nudge-restore." + secrets.token_hex(12))
        try:
            os.symlink(snapshot.link_target, temporary)
            return temporary
        except FileExistsError:
            continue
        except OSError as error:
            raise InstallError("cannot stage transaction restore") from error
    raise InstallError("cannot allocate transaction restore name")


def _write_backup(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise InstallError("cannot create transaction backup") from error


def apply_transaction(
    writes: list[PlannedWrite],
    *,
    replace: Callable[[os.PathLike[str], os.PathLike[str]], object] = os.replace,
    stamp: str | None = None,
) -> list[Path]:
    """Atomically apply a preflighted set and roll it back on any failure."""
    stamp = stamp or datetime.now().strftime("%Y%m%d%H%M%S")
    if re.fullmatch(r"[0-9]{14}", stamp) is None:
        raise InstallError("invalid backup timestamp")
    ordered = sorted(writes, key=lambda plan: os.fspath(plan.path))
    if len({plan.path for plan in ordered}) != len(ordered):
        raise InstallError("duplicate planned target")

    snapshots = {plan.path: _inspect_target(plan) for plan in ordered}
    changed = [
        plan
        for plan in ordered
        if not (
            snapshots[plan.path].exists
            and snapshots[plan.path].link_target is None
            and snapshots[plan.path].data == plan.data
            and snapshots[plan.path].mode == plan.mode
        )
    ]
    if not changed:
        return []
    backups = {
        plan.path: plan.path.with_name(plan.path.name + ".bak." + stamp)
        for plan in changed
        if plan.backup and snapshots[plan.path].exists
    }
    for backup in backups.values():
        if backup.exists() or backup.is_symlink():
            raise InstallError("transaction backup already exists")

    created_dirs: list[Path] = []
    staged: dict[Path, Path] = {}
    restores: dict[Path, Path] = {}
    created_backups: list[Path] = []
    replaced: list[PlannedWrite] = []
    rollback_failed = False
    try:
        created_dirs = _make_private_parents([plan.path for plan in changed])
        for plan in changed:
            staged[plan.path] = _stage_regular(plan.path, plan.data, plan.mode, ".task-nudge-stage.")
            snapshot = snapshots[plan.path]
            if snapshot.exists:
                restores[plan.path] = _stage_restore(plan.path, snapshot)
        for plan in changed:
            backup = backups.get(plan.path)
            if backup is not None:
                _write_backup(backup, snapshots[plan.path].data)
                created_backups.append(backup)
        for plan in changed:
            try:
                replace(staged[plan.path], plan.path)
            except BaseException:
                if not staged[plan.path].exists():
                    replaced.append(plan)
                raise
            replaced.append(plan)
        return [plan.path for plan in changed]
    except BaseException as error:
        for plan in reversed(replaced):
            snapshot = snapshots[plan.path]
            try:
                if snapshot.exists:
                    os.replace(restores[plan.path], plan.path)
                else:
                    plan.path.unlink(missing_ok=True)
            except OSError:
                rollback_failed = True
        if not rollback_failed:
            for backup in reversed(created_backups):
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    rollback_failed = True
        message = "transaction failed and rollback was incomplete" if rollback_failed else "transaction failed"
        raise InstallError(message) from error
    finally:
        for temporary in (*staged.values(), *restores.values()):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        for directory in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass


def _cli_home(value: str | None) -> Path:
    raw = value if value is not None else os.environ.get("HOME")
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        raise InstallError("HOME must be an absolute path")
    return Path(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install transactional task-nudge runtime")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--home")
    arguments = parser.parse_args(argv)
    repo = Path(arguments.repo)
    if not repo.is_absolute():
        print("install-task-nudge: --repo must be absolute", file=sys.stderr)
        return 2
    try:
        plans = build_plan(repo, _cli_home(arguments.home))
        changed = apply_transaction(plans)
    except InstallError as error:
        print(f"install-task-nudge: {error}", file=sys.stderr)
        return 1
    print(f"task-nudge installed ({len(changed)} changed); review and trust the Codex hook with /hooks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
