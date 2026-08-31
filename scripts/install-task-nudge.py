#!/usr/bin/python3
"""Install the shared task-nudge runtime without partial target updates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
import argparse
import errno
import json
import os
import re
import secrets
import stat
import sys


REPO = Path(__file__).resolve().parents[1]
AGENTS_START = "<!-- claude-config:task-nudge:START -->"
AGENTS_END = "<!-- claude-config:task-nudge:END -->"
CLAUDE_MATCHER = "Edit|Write|NotebookEdit"
CLAUDE_COMMAND = "$HOME/.claude/hooks/task-nudge.sh"
CODEX_MATCHER = "apply_patch|Edit|Write"
CODEX_COMMAND = "/usr/bin/python3 $HOME/.local/share/claude-config/hooks/task-nudge-codex.py"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_SYSTEM_REPLACE = os.replace


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
    identity: tuple[int, int, int] | None = None
    link_identity: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class _ParentHandle:
    path: Path
    descriptor: int


@dataclass(frozen=True)
class _OwnedEntry:
    name: str
    identity: tuple[int, int, int]


@dataclass(frozen=True)
class _CreatedDirectory:
    parent_descriptor: int
    name: str
    identity: tuple[int, int, int]


class InstallError(Exception):
    """A bounded installer failure safe to surface without file contents."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise InstallError("invalid JSON: duplicate key")
        value[key] = item
    return value


def _parse_json_config(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except InstallError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise InstallError("invalid JSON configuration") from error
    if not isinstance(value, dict):
        raise InstallError("JSON configuration must be an object")
    return value


def load_json_config(path: Path) -> dict[str, object]:
    """Load one optional configuration object with duplicate-key rejection."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise InstallError("cannot read JSON configuration") from error
    return _parse_json_config(raw)


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


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _path_components(path: Path) -> tuple[str, ...]:
    if not path.is_absolute() or ".." in path.parts:
        raise InstallError("filesystem path must be absolute and normalized")
    return tuple(part for part in path.parts[1:] if part not in {"", "."})


def _open_absolute_directory(path: Path, *, missing_ok: bool = False) -> int | None:
    descriptor = -1
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        for component in _path_components(path):
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if missing_ok:
                    os.close(descriptor)
                    return None
                raise
            os.close(descriptor)
            descriptor = child
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise InstallError("filesystem component is not a directory")
        return descriptor
    except InstallError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise InstallError("cannot open filesystem directory safely") from error


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    before_open: Callable[[], None] | None = None,
    missing_ok: bool = False,
) -> tuple[bytes, os.stat_result] | None:
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise InstallError("required file is missing")
    except OSError as error:
        raise InstallError("cannot inspect file safely") from error
    if before_open is not None:
        before_open()
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or _identity(before) != _identity(after)
        ):
            raise InstallError("file identity changed during validation")
        return _read_descriptor(descriptor), after
    except InstallError:
        raise
    except OSError as error:
        raise InstallError("cannot open regular file safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_absolute_directory(path: Path, label: str) -> None:
    descriptor = -1
    try:
        descriptor = _open_absolute_directory(path)
        assert descriptor is not None
    except InstallError as error:
        raise InstallError(f"{label} is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_source(
    hooks_descriptor: int,
    name: str,
    path: Path,
    before_source_open: Callable[[Path], None] | None,
) -> bytes:
    callback = None
    if before_source_open is not None:
        callback = lambda: before_source_open(path)
    result = _read_regular_at(hooks_descriptor, name, before_open=callback)
    assert result is not None
    data, _ = result
    if not data:
        raise InstallError("required source is empty")
    return data


def _read_optional_regular_path(path: Path) -> bytes | None:
    parent_descriptor = _open_absolute_directory(path.parent, missing_ok=True)
    if parent_descriptor is None:
        return None
    try:
        result = _read_regular_at(parent_descriptor, path.name, missing_ok=True)
        return None if result is None else result[0]
    finally:
        os.close(parent_descriptor)


def _inspect_target(plan: PlannedWrite) -> _TargetSnapshot:
    if not plan.path.is_absolute():
        raise InstallError("planned target must be absolute")
    if plan.mode not in {0o600, 0o700}:
        raise InstallError("planned target mode is not owner-only")
    handle = _open_parent(plan.path, create=False, created_directories=[])
    if handle is None:
        return _TargetSnapshot(False)
    try:
        return _snapshot_at(handle, plan)
    finally:
        os.close(handle.descriptor)


def build_plan(
    repo: Path,
    home: Path,
    *,
    before_source_open: Callable[[Path], None] | None = None,
) -> list[PlannedWrite]:
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
    hooks_descriptor = _open_absolute_directory(repo / "hooks")
    assert hooks_descriptor is not None
    try:
        sources = {
            name: _read_source(
                hooks_descriptor,
                name,
                repo / "hooks" / name,
                before_source_open,
            )
            for name in source_names
        }
    finally:
        os.close(hooks_descriptor)

    claude_config = home / ".claude" / "settings.json"
    codex_config = home / ".codex" / "hooks.json"
    for config_path in (claude_config, codex_config):
        _inspect_target(PlannedWrite(config_path, b"", 0o600, True))
    claude_raw = _read_optional_regular_path(claude_config)
    codex_raw = _read_optional_regular_path(codex_config)
    claude_value = merge_hook_config(
        {} if claude_raw is None else _parse_json_config(claude_raw),
        matcher=CLAUDE_MATCHER,
        command=CLAUDE_COMMAND,
        legacy_commands=(CLAUDE_COMMAND,),
        home=home,
    )
    codex_value = merge_hook_config(
        {} if codex_raw is None else _parse_json_config(codex_raw),
        matcher=CODEX_MATCHER,
        command=CODEX_COMMAND,
        legacy_commands=(),
        home=home,
    )

    override = home / ".codex" / "AGENTS.override.md"
    override_raw = _read_optional_regular_path(override)
    try:
        override_nonempty = override_raw is not None and bool(override_raw.decode("utf-8").strip())
    except UnicodeDecodeError as error:
        raise InstallError("AGENTS override is not UTF-8") from error
    agents_path = override if override_nonempty else home / ".codex" / "AGENTS.md"
    _inspect_target(PlannedWrite(agents_path, b"", 0o600, True))
    agents_raw = override_raw if override_nonempty else _read_optional_regular_path(agents_path)
    try:
        agents_original = "" if agents_raw is None else agents_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InstallError("active AGENTS file is not UTF-8") from error
    agents_data = merge_agents_block(agents_original, agents_policy_block()).encode("utf-8")

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


def _open_parent(
    path: Path,
    *,
    create: bool,
    created_directories: list[_CreatedDirectory],
) -> _ParentHandle | None:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in _path_components(path.parent):
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                os.mkdir(component, 0o700, dir_fd=descriptor)
                metadata = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                created_directories.append(
                    _CreatedDirectory(os.dup(descriptor), component, _identity(metadata))
                )
                os.fsync(descriptor)
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.fchmod(child, 0o700)
                os.fsync(child)
            os.close(descriptor)
            descriptor = child
        return _ParentHandle(path.parent, descriptor)
    except BaseException as error:
        os.close(descriptor)
        if isinstance(error, InstallError):
            raise
        raise InstallError("cannot open target parent safely") from error


def _read_regular_absolute(path: Path) -> tuple[bytes, os.stat_result]:
    parent_descriptor = _open_absolute_directory(path.parent)
    assert parent_descriptor is not None
    try:
        result = _read_regular_at(parent_descriptor, path.name)
        assert result is not None
        return result
    finally:
        os.close(parent_descriptor)


def _snapshot_at(handle: _ParentHandle, plan: PlannedWrite) -> _TargetSnapshot:
    try:
        metadata = os.stat(plan.path.name, dir_fd=handle.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return _TargetSnapshot(False)
    except OSError as error:
        raise InstallError("cannot inspect planned target safely") from error
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
            link_target = os.readlink(plan.path.name, dir_fd=handle.descriptor)
            candidate = Path(link_target)
            if not candidate.is_absolute():
                candidate = Path(os.path.normpath(os.fspath(handle.path / candidate)))
            data, target_metadata = _read_regular_absolute(candidate)
        except InstallError:
            raise
        except OSError as error:
            raise InstallError("cannot read legacy shim symlink safely") from error
        return _TargetSnapshot(
            True,
            data,
            stat.S_IMODE(target_metadata.st_mode),
            link_target,
            _identity(metadata),
            _identity(target_metadata),
        )
    result = _read_regular_at(handle.descriptor, plan.path.name)
    if result is None:
        raise InstallError("planned target changed during validation")
    data, opened_metadata = result
    return _TargetSnapshot(
        True,
        data,
        stat.S_IMODE(opened_metadata.st_mode),
        None,
        _identity(opened_metadata),
    )


def _snapshot_matches(left: _TargetSnapshot, right: _TargetSnapshot) -> bool:
    return left == right


def _entry_identity(handle: _ParentHandle, name: str) -> tuple[int, int, int] | None:
    try:
        return _identity(os.stat(name, dir_fd=handle.descriptor, follow_symlinks=False))
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallError("cannot inspect transaction entry") from error


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short transactional write")
        view = view[written:]


def _unlink_owned(handle: _ParentHandle, entry: _OwnedEntry) -> None:
    identity = _entry_identity(handle, entry.name)
    if identity is None:
        return
    if identity != entry.identity:
        raise InstallError("transaction entry ownership changed")
    os.unlink(entry.name, dir_fd=handle.descriptor)
    os.fsync(handle.descriptor)


def _stage_regular_at(
    handle: _ParentHandle,
    data: bytes,
    mode: int,
    prefix: str,
) -> _OwnedEntry:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    for _ in range(128):
        name = prefix + secrets.token_hex(12)
        descriptor = -1
        owned: _OwnedEntry | None = None
        try:
            descriptor = os.open(name, flags, mode, dir_fd=handle.descriptor)
            os.fchmod(descriptor, mode)
            owned = _OwnedEntry(name, _identity(os.fstat(descriptor)))
            _write_all(descriptor, data)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.fsync(handle.descriptor)
            return owned
        except FileExistsError:
            continue
        except BaseException as error:
            if descriptor >= 0:
                os.close(descriptor)
            if owned is not None:
                try:
                    _unlink_owned(handle, owned)
                except BaseException:
                    pass
            if isinstance(error, InstallError):
                raise
            raise InstallError("cannot stage transactional file") from error
    raise InstallError("cannot allocate transactional stage name")


def _stage_restore_at(handle: _ParentHandle, snapshot: _TargetSnapshot) -> _OwnedEntry:
    if snapshot.link_target is None:
        return _stage_regular_at(
            handle,
            snapshot.data,
            snapshot.mode,
            ".task-nudge-restore.",
        )
    for _ in range(128):
        name = ".task-nudge-restore." + secrets.token_hex(12)
        try:
            os.symlink(snapshot.link_target, name, dir_fd=handle.descriptor)
            identity = _entry_identity(handle, name)
            assert identity is not None
            owned = _OwnedEntry(name, identity)
            os.fsync(handle.descriptor)
            return owned
        except FileExistsError:
            continue
        except BaseException as error:
            if "owned" in locals():
                try:
                    _unlink_owned(handle, owned)
                except BaseException:
                    pass
            if isinstance(error, InstallError):
                raise
            raise InstallError("cannot stage transaction restore") from error
    raise InstallError("cannot allocate transaction restore name")


def _write_backup_at(handle: _ParentHandle, name: str, data: bytes) -> _OwnedEntry:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = -1
    owned: _OwnedEntry | None = None
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=handle.descriptor)
        os.fchmod(descriptor, 0o600)
        owned = _OwnedEntry(name, _identity(os.fstat(descriptor)))
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.fsync(handle.descriptor)
        return owned
    except BaseException as error:
        if descriptor >= 0:
            os.close(descriptor)
        if owned is not None:
            try:
                _unlink_owned(handle, owned)
            except BaseException:
                pass
        if isinstance(error, InstallError):
            raise
        raise InstallError("cannot create transaction backup") from error


def _remove_created_directories(
    created: list[_CreatedDirectory],
    *,
    require_empty: bool = False,
) -> bool:
    failed = False
    for directory in reversed(created):
        try:
            current = os.stat(
                directory.name,
                dir_fd=directory.parent_descriptor,
                follow_symlinks=False,
            )
            if _identity(current) != directory.identity:
                failed = True
                continue
            os.rmdir(directory.name, dir_fd=directory.parent_descriptor)
            os.fsync(directory.parent_descriptor)
        except FileNotFoundError:
            pass
        except OSError as error:
            if require_empty or error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                failed = True
        finally:
            os.close(directory.parent_descriptor)
    return failed


def apply_transaction(
    writes: list[PlannedWrite],
    *,
    replace: Callable[[os.PathLike[str], os.PathLike[str]], object] = _SYSTEM_REPLACE,
    stamp: str | None = None,
    phase_hook: Callable[[str, Path], None] | None = None,
) -> list[Path]:
    """Apply a descriptor-anchored write set and roll it back on failure."""
    stamp = stamp or datetime.now().strftime("%Y%m%d%H%M%S")
    if re.fullmatch(r"[0-9]{14}", stamp) is None:
        raise InstallError("invalid backup timestamp")
    ordered = sorted(writes, key=lambda plan: os.fspath(plan.path))
    if len({plan.path for plan in ordered}) != len(ordered):
        raise InstallError("duplicate planned target")
    for plan in ordered:
        if not plan.path.is_absolute() or plan.mode not in {0o600, 0o700}:
            raise InstallError("invalid planned target")

    snapshots: dict[Path, _TargetSnapshot] = {}
    for plan in ordered:
        handle = _open_parent(plan.path, create=False, created_directories=[])
        if handle is None:
            snapshots[plan.path] = _TargetSnapshot(False)
            continue
        try:
            snapshots[plan.path] = _snapshot_at(handle, plan)
        finally:
            os.close(handle.descriptor)
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

    backup_names = {
        plan.path: plan.path.name + ".bak." + stamp
        for plan in changed
        if plan.backup and snapshots[plan.path].exists
    }
    for plan in changed:
        backup_name = backup_names.get(plan.path)
        if backup_name is None:
            continue
        handle = _open_parent(plan.path, create=False, created_directories=[])
        assert handle is not None
        try:
            if _entry_identity(handle, backup_name) is not None:
                raise InstallError("transaction backup already exists")
        finally:
            os.close(handle.descriptor)

    created_directories: list[_CreatedDirectory] = []
    handles: dict[Path, _ParentHandle] = {}
    staged: dict[Path, _OwnedEntry] = {}
    restores: dict[Path, _OwnedEntry] = {}
    created_backups: dict[Path, _OwnedEntry] = {}
    replaced: list[PlannedWrite] = []
    rollback_failed = False
    try:
        for plan in changed:
            handle = _open_parent(
                plan.path,
                create=True,
                created_directories=created_directories,
            )
            assert handle is not None
            handles[plan.path] = handle
            if not _snapshot_matches(_snapshot_at(handle, plan), snapshots[plan.path]):
                raise InstallError("planned target changed after preflight")
        for plan in changed:
            handle = handles[plan.path]
            staged[plan.path] = _stage_regular_at(
                handle,
                plan.data,
                plan.mode,
                ".task-nudge-stage.",
            )
            if snapshots[plan.path].exists:
                restores[plan.path] = _stage_restore_at(handle, snapshots[plan.path])
        for plan in changed:
            backup_name = backup_names.get(plan.path)
            if backup_name is None:
                continue
            handle = handles[plan.path]
            if phase_hook is not None:
                phase_hook("before_backup_revalidate", plan.path)
            if not _snapshot_matches(_snapshot_at(handle, plan), snapshots[plan.path]):
                raise InstallError("planned target changed before backup")
            if phase_hook is not None:
                phase_hook("before_backup_create", plan.path.with_name(backup_name))
            created_backups[plan.path] = _write_backup_at(
                handle,
                backup_name,
                snapshots[plan.path].data,
            )
        for plan in changed:
            handle = handles[plan.path]
            if phase_hook is not None:
                phase_hook("before_replace_revalidate", plan.path)
            if not _snapshot_matches(_snapshot_at(handle, plan), snapshots[plan.path]):
                raise InstallError("planned target changed before replacement")
            stage = staged[plan.path]
            try:
                if replace is _SYSTEM_REPLACE:
                    _SYSTEM_REPLACE(
                        stage.name,
                        plan.path.name,
                        src_dir_fd=handle.descriptor,
                        dst_dir_fd=handle.descriptor,
                    )
                else:
                    replace(handle.path / stage.name, plan.path)
            except BaseException:
                if _entry_identity(handle, stage.name) is None:
                    replaced.append(plan)
                raise
            replaced.append(plan)
            os.fsync(handle.descriptor)
        for plan, restore in list(restores.items()):
            _unlink_owned(handles[plan], restore)
            del restores[plan]
        return [plan.path for plan in changed]
    except BaseException as error:
        for plan in reversed(replaced):
            handle = handles[plan.path]
            snapshot = snapshots[plan.path]
            try:
                current_identity = _entry_identity(handle, plan.path.name)
                if current_identity != staged[plan.path].identity:
                    raise InstallError("replacement ownership changed before rollback")
                if snapshot.exists:
                    restore = restores.get(plan.path)
                    if restore is None or _entry_identity(handle, restore.name) != restore.identity:
                        restore = _stage_restore_at(handle, snapshot)
                    _SYSTEM_REPLACE(
                        restore.name,
                        plan.path.name,
                        src_dir_fd=handle.descriptor,
                        dst_dir_fd=handle.descriptor,
                    )
                    os.fsync(handle.descriptor)
                    restores.pop(plan.path, None)
                else:
                    os.unlink(plan.path.name, dir_fd=handle.descriptor)
                    os.fsync(handle.descriptor)
            except BaseException:
                rollback_failed = True
        for plan, backup in reversed(list(created_backups.items())):
            try:
                _unlink_owned(handles[plan], backup)
            except BaseException:
                rollback_failed = True
        for mapping in (staged, restores):
            for plan, entry in list(mapping.items()):
                try:
                    _unlink_owned(handles[plan], entry)
                except BaseException:
                    rollback_failed = True
        if _remove_created_directories(created_directories, require_empty=True):
            rollback_failed = True
        created_directories = []
        message = "transaction failed and rollback was incomplete" if rollback_failed else "transaction failed"
        raise InstallError(message) from error
    finally:
        for handle in handles.values():
            try:
                os.close(handle.descriptor)
            except OSError:
                pass
        if created_directories:
            _remove_created_directories(created_directories)


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
