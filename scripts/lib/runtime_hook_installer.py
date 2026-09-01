"""Secure descriptor-anchored transactions for local runtime-hook installers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence, TypeAlias
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
_SYSTEM_REPLACE = os.replace
_RENAME_NOREPLACE = 1
_RETAINED_SOURCE_PARENT: ContextVar[tuple[Path, int] | None] = ContextVar(
    "runtime_hook_source_parent",
    default=None,
)
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEAT2.restype = ctypes.c_int


@dataclass(frozen=True)
class TargetSnapshot:
    exists: bool
    data: bytes = b""
    mode: int = 0
    link_target: str | None = None
    identity: tuple[int, int, int] | None = None
    link_identity: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class SelectorPrecondition:
    path: Path
    snapshot: TargetSnapshot


@dataclass(frozen=True)
class SourcePrecondition:
    path: Path
    identity: tuple[int, int, int]
    content_sha256: str | None = None
    entry_names: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PlannedWrite:
    path: Path
    data: bytes
    mode: int
    backup: bool
    allow_legacy_symlink: bool = False
    precondition: TargetSnapshot | None = None
    selector_preconditions: Sequence[SelectorPrecondition] = ()


@dataclass(frozen=True)
class PlannedSymlink:
    path: Path
    target: Path
    precondition: TargetSnapshot | None = None
    source_preconditions: tuple[SourcePrecondition, ...] = ()


PlannedEntry: TypeAlias = PlannedWrite | PlannedSymlink


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


@dataclass(frozen=True)
class _SourceHandle:
    precondition: SourcePrecondition
    descriptor: int


class InstallError(Exception):
    """A bounded installer failure safe to surface without target contents."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise InstallError("invalid JSON: duplicate key")
        value[key] = item
    return value


def strict_json_object(raw: bytes) -> dict[str, object]:
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


def _managed_group(matcher: str | None, command: str) -> dict[str, object]:
    group: dict[str, object] = {
        "hooks": [{"type": "command", "command": command}],
    }
    if matcher is not None:
        group["matcher"] = matcher
    return group


def _normalize_command(command: object, home: Path) -> str | None:
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


def merge_pre_tool_hook(
    original: dict[str, object],
    *,
    matcher: str | None,
    command: str,
    legacy_commands: Sequence[str],
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
        if (normalized := _normalize_command(candidate, home)) is not None
    }
    found: list[int] = []
    for index, group in enumerate(groups):
        records = group["hooks"]
        assert isinstance(records, list)
        matching = [
            record
            for record in records
            if _normalize_command(record["command"], home) in managed_commands
        ]
        if matching and len(records) != 1:
            raise InstallError("managed command appears in a multi-hook group")
        if matching:
            if set(group) not in ({"matcher", "hooks"}, {"hooks"}):
                raise InstallError("managed hook group has unknown fields")
            found.append(index)
    if len(found) > 1:
        raise InstallError("multiple managed hook groups conflict")
    replacement = _managed_group(matcher, command)
    if found:
        groups[found[0]] = replacement
    else:
        groups.append(replacement)
    return merged


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _rename_noreplace(
    source_descriptor: int,
    source_name: str,
    target_descriptor: int,
    target_name: str,
) -> None:
    if _RENAMEAT2 is None:
        raise InstallError("atomic no-replace rename is unavailable")
    result = _RENAMEAT2(
        source_descriptor,
        os.fsencode(source_name),
        target_descriptor,
        os.fsencode(target_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


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
    if not stat.S_ISREG(before.st_mode):
        raise InstallError("file is not a regular file")
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


@contextmanager
def _retain_source_parent(path: Path):
    """Retain one safely opened source parent across a bounded read group."""
    descriptor = _open_absolute_directory(path)
    assert descriptor is not None
    token = _RETAINED_SOURCE_PARENT.set((path, descriptor))
    try:
        yield descriptor
    finally:
        _RETAINED_SOURCE_PARENT.reset(token)
        os.close(descriptor)


def read_regular_source(
    path: Path,
    before_open: Callable[[Path], None] | None = None,
) -> bytes:
    """Read one non-empty regular source without following symlinks."""
    if not path.is_absolute():
        raise InstallError("source path must be absolute")
    retained = _RETAINED_SOURCE_PARENT.get()
    owns_descriptor = retained is None or retained[0] != path.parent
    if owns_descriptor:
        parent_descriptor = _open_absolute_directory(path.parent)
        assert parent_descriptor is not None
    else:
        parent_descriptor = retained[1]
    try:
        callback = None if before_open is None else lambda: before_open(path)
        result = _read_regular_at(parent_descriptor, path.name, before_open=callback)
        assert result is not None
        data, _ = result
        if not data:
            raise InstallError("required source is empty")
        return data
    finally:
        if owns_descriptor:
            os.close(parent_descriptor)


def _python_source_names(descriptor: int) -> tuple[str, ...]:
    try:
        names = os.listdir(descriptor)
    except OSError as error:
        raise InstallError("cannot enumerate source directory safely") from error
    return tuple(sorted(name for name in names if name.endswith(".py")))


def read_python_source_group(
    directory: Path,
    *,
    required_names: Sequence[str] = (),
    before_open: Callable[[Path], None] | None = None,
    before_recheck: Callable[[Path], None] | None = None,
) -> dict[str, bytes]:
    """Capture every top-level Python source through one retained directory."""
    if not directory.is_absolute():
        raise InstallError("source directory must be absolute")
    required = tuple(required_names)
    if len(set(required)) != len(required) or any(
        not isinstance(name, str)
        or not name.endswith(".py")
        or Path(name).name != name
        for name in required
    ):
        raise InstallError("invalid required Python source name")
    with _retain_source_parent(directory) as descriptor:
        names = _python_source_names(descriptor)
        if not names:
            raise InstallError("required Python source set is empty")
        if not set(required).issubset(names):
            raise InstallError("required Python source is missing")
        captured = {
            name: read_regular_source(
                directory / name,
                before_open=before_open,
            )
            for name in names
        }
        if before_recheck is not None:
            before_recheck(directory)
        if _python_source_names(descriptor) != names:
            raise InstallError("Python source set changed during validation")
        return captured


def capture_source_directory(path: Path) -> SourcePrecondition:
    """Capture one component-safe directory identity and complete name set."""
    if not path.is_absolute():
        raise InstallError("source precondition path must be absolute")
    descriptor = _open_absolute_directory(path)
    assert descriptor is not None
    try:
        return SourcePrecondition(
            path=path,
            identity=_identity(os.fstat(descriptor)),
            entry_names=tuple(sorted(os.listdir(descriptor))),
        )
    except OSError as error:
        raise InstallError("cannot capture source directory safely") from error
    finally:
        os.close(descriptor)


def capture_source_file(path: Path) -> SourcePrecondition:
    """Capture one component-safe regular-file identity and content digest."""
    if not path.is_absolute():
        raise InstallError("source precondition path must be absolute")
    descriptor = _open_absolute_directory(path.parent)
    assert descriptor is not None
    try:
        result = _read_regular_at(descriptor, path.name)
        if result is None:
            raise InstallError("required source file is missing")
        data, metadata = result
        return SourcePrecondition(
            path=path,
            identity=_identity(metadata),
            content_sha256=hashlib.sha256(data).hexdigest(),
        )
    finally:
        os.close(descriptor)


def _validate_source_precondition_shape(precondition: SourcePrecondition) -> None:
    if (
        not isinstance(precondition, SourcePrecondition)
        or not precondition.path.is_absolute()
    ):
        raise InstallError("invalid source precondition")
    if (
        not isinstance(precondition.identity, tuple)
        or len(precondition.identity) != 3
        or any(not isinstance(value, int) for value in precondition.identity)
    ):
        raise InstallError("invalid source precondition")
    is_file = precondition.content_sha256 is not None
    is_directory = precondition.entry_names is not None
    if is_file == is_directory:
        raise InstallError("invalid source precondition")
    if is_file and (
        not isinstance(precondition.content_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", precondition.content_sha256) is None
    ):
        raise InstallError("invalid source precondition")
    if is_directory and (
        not isinstance(precondition.entry_names, tuple)
        or any(
            not isinstance(name, str) or not name or Path(name).name != name
            for name in precondition.entry_names
        )
        or tuple(sorted(precondition.entry_names)) != precondition.entry_names
        or len(set(precondition.entry_names)) != len(precondition.entry_names)
    ):
        raise InstallError("invalid source precondition")


def _collect_source_preconditions(
    entries: Sequence[PlannedEntry],
) -> tuple[SourcePrecondition, ...]:
    collected: dict[Path, SourcePrecondition] = {}
    for entry in entries:
        if not isinstance(entry, PlannedSymlink):
            continue
        if not isinstance(entry.source_preconditions, tuple):
            raise InstallError("source preconditions must be immutable")
        for precondition in entry.source_preconditions:
            _validate_source_precondition_shape(precondition)
            previous = collected.get(precondition.path)
            if previous is not None and previous != precondition:
                raise InstallError("conflicting source preconditions")
            collected[precondition.path] = precondition
    return tuple(collected[path] for path in sorted(collected, key=os.fspath))


def _open_source_handles(
    preconditions: Sequence[SourcePrecondition],
) -> tuple[_SourceHandle, ...]:
    handles: list[_SourceHandle] = []
    try:
        for precondition in preconditions:
            directory = (
                precondition.path
                if precondition.entry_names is not None
                else precondition.path.parent
            )
            descriptor = _open_absolute_directory(directory)
            assert descriptor is not None
            handles.append(_SourceHandle(precondition, descriptor))
        _validate_source_handles(handles)
        return tuple(handles)
    except BaseException:
        for handle in handles:
            os.close(handle.descriptor)
        raise


def _validate_source_handles(handles: Sequence[_SourceHandle]) -> None:
    """Revalidate retained source descriptors and their canonical anchors."""
    for handle in handles:
        precondition = handle.precondition
        if precondition.entry_names is not None:
            reopened = _open_absolute_directory(precondition.path)
            assert reopened is not None
            try:
                if _identity(os.fstat(reopened)) != precondition.identity:
                    raise InstallError("source directory identity changed")
            finally:
                os.close(reopened)
            try:
                names = tuple(sorted(os.listdir(handle.descriptor)))
            except OSError as error:
                raise InstallError("cannot revalidate source directory") from error
            if names != precondition.entry_names:
                raise InstallError("source directory name set changed")
            continue

        parent = _ParentHandle(precondition.path.parent, handle.descriptor)
        _require_parent_anchor(parent)
        result = _read_regular_at(handle.descriptor, precondition.path.name)
        if result is None:
            raise InstallError("source file changed")
        data, metadata = result
        if (
            _identity(metadata) != precondition.identity
            or hashlib.sha256(data).hexdigest() != precondition.content_sha256
        ):
            raise InstallError("source file changed")


def _close_source_handles(handles: Sequence[_SourceHandle]) -> None:
    for handle in handles:
        try:
            os.close(handle.descriptor)
        except OSError:
            pass


def _transaction_phase(
    handles: Sequence[_SourceHandle],
    phase_hook: Callable[[str, Path], None] | None,
    phase: str,
    path: Path,
) -> None:
    _validate_source_handles(handles)
    if phase_hook is not None:
        phase_hook(phase, path)
    _validate_source_handles(handles)


def inspect_target(entry: PlannedEntry) -> TargetSnapshot:
    if not entry.path.is_absolute():
        raise InstallError("planned target must be absolute")
    if isinstance(entry, PlannedWrite) and entry.mode not in {0o600, 0o700}:
        raise InstallError("planned target mode is not owner-only")
    if isinstance(entry, PlannedSymlink) and not entry.target.is_absolute():
        raise InstallError("planned symlink target must be absolute")
    handle = _open_parent(entry.path, create=False, created_directories=[])
    if handle is None:
        return TargetSnapshot(False)
    try:
        return _snapshot_at(handle, entry)
    finally:
        os.close(handle.descriptor)


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


def _require_parent_anchor(handle: _ParentHandle) -> None:
    """Prove the canonical parent path still names the retained directory."""
    reopened = _open_absolute_directory(handle.path)
    assert reopened is not None
    try:
        if _identity(os.fstat(reopened)) != _identity(os.fstat(handle.descriptor)):
            raise InstallError("planned target parent changed during transaction")
    except OSError as error:
        raise InstallError("cannot validate planned target parent") from error
    finally:
        os.close(reopened)


def _read_regular_absolute(path: Path) -> tuple[bytes, os.stat_result]:
    parent_descriptor = _open_absolute_directory(path.parent)
    assert parent_descriptor is not None
    try:
        result = _read_regular_at(parent_descriptor, path.name)
        assert result is not None
        return result
    finally:
        os.close(parent_descriptor)


def _snapshot_at(
    handle: _ParentHandle,
    plan: PlannedEntry,
    *,
    entry_name: str | None = None,
) -> TargetSnapshot:
    name = plan.path.name if entry_name is None else entry_name
    try:
        metadata = os.stat(name, dir_fd=handle.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return TargetSnapshot(False)
    except OSError as error:
        raise InstallError("cannot inspect planned target safely") from error
    if stat.S_ISLNK(metadata.st_mode):
        try:
            link_target = os.readlink(name, dir_fd=handle.descriptor)
        except OSError as error:
            raise InstallError("cannot inspect planned symlink safely") from error
        if isinstance(plan, PlannedSymlink):
            if link_target != os.fspath(plan.target):
                raise InstallError("symlink target conflicts with planned target")
            return TargetSnapshot(
                True,
                link_target=link_target,
                identity=_identity(metadata),
            )
        is_legacy_shim = (
            plan.allow_legacy_symlink
            and plan.path.name == "task-nudge.sh"
            and plan.path.parent.name == "hooks"
            and plan.path.parent.parent.name == ".claude"
        )
        if not is_legacy_shim:
            raise InstallError("planned target is an unsafe symlink")
        try:
            candidate = Path(link_target)
            if not candidate.is_absolute():
                candidate = Path(os.path.normpath(os.fspath(handle.path / candidate)))
            data, target_metadata = _read_regular_absolute(candidate)
        except InstallError:
            raise
        except OSError as error:
            raise InstallError("cannot read legacy shim symlink safely") from error
        return TargetSnapshot(
            True,
            data,
            stat.S_IMODE(target_metadata.st_mode),
            link_target,
            _identity(metadata),
            _identity(target_metadata),
        )
    if isinstance(plan, PlannedSymlink):
        raise InstallError("symlink target conflicts with planned target")
    result = _read_regular_at(handle.descriptor, name)
    if result is None:
        raise InstallError("planned target changed during validation")
    data, opened_metadata = result
    return TargetSnapshot(
        True,
        data,
        stat.S_IMODE(opened_metadata.st_mode),
        None,
        _identity(opened_metadata),
    )


def snapshot_matches(left: TargetSnapshot, right: TargetSnapshot) -> bool:
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


def _stage_symlink_at(
    handle: _ParentHandle,
    target: Path,
    prefix: str,
) -> _OwnedEntry:
    for _ in range(128):
        name = prefix + secrets.token_hex(12)
        owned: _OwnedEntry | None = None
        try:
            os.symlink(os.fspath(target), name, dir_fd=handle.descriptor)
            identity = _entry_identity(handle, name)
            assert identity is not None
            owned = _OwnedEntry(name, identity)
            os.fsync(handle.descriptor)
            return owned
        except FileExistsError:
            continue
        except BaseException as error:
            if owned is not None:
                try:
                    _unlink_owned(handle, owned)
                except BaseException:
                    pass
            if isinstance(error, InstallError):
                raise
            raise InstallError("cannot stage transactional symlink") from error
    raise InstallError("cannot allocate transactional stage name")


def _stage_restore_at(
    handle: _ParentHandle,
    snapshot: TargetSnapshot,
    prefix: str,
) -> _OwnedEntry:
    if snapshot.link_target is None:
        return _stage_regular_at(
            handle,
            snapshot.data,
            snapshot.mode,
            prefix,
        )
    for _ in range(128):
        name = prefix + secrets.token_hex(12)
        owned: _OwnedEntry | None = None
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
            if owned is not None:
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


def _claim_target_at(
    handle: _ParentHandle,
    target_name: str,
    prefix: str,
) -> _OwnedEntry:
    for _ in range(128):
        quarantine_name = prefix + secrets.token_hex(12)
        try:
            _rename_noreplace(
                handle.descriptor,
                target_name,
                handle.descriptor,
                quarantine_name,
            )
        except OSError as error:
            if error.errno == errno.EEXIST:
                continue
            raise InstallError("cannot atomically claim target") from error
        identity = _entry_identity(handle, quarantine_name)
        if identity is None:
            raise InstallError("claimed target disappeared")
        return _OwnedEntry(quarantine_name, identity)
    raise InstallError("cannot allocate target quarantine")


def _restore_claim_at(
    handle: _ParentHandle,
    quarantine: _OwnedEntry,
    target_name: str,
) -> None:
    if _entry_identity(handle, quarantine.name) != quarantine.identity:
        raise InstallError("target quarantine ownership changed")
    _rename_noreplace(
        handle.descriptor,
        quarantine.name,
        handle.descriptor,
        target_name,
    )
    os.fsync(handle.descriptor)


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


def _apply_transaction_with_source_handles(
    entries: list[PlannedEntry],
    *,
    namespace: str,
    replace: Callable | None = None,
    stamp: str | None = None,
    phase_hook: Callable[[str, Path], None] | None = None,
    source_handles: Sequence[_SourceHandle] = (),
) -> list[Path]:
    """Apply a descriptor-anchored write set and roll it back on failure."""
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", namespace) is None:
        raise InstallError("invalid namespace")
    stamp = stamp or datetime.now().strftime("%Y%m%d%H%M%S")
    if re.fullmatch(r"[0-9]{14}", stamp) is None:
        raise InstallError("invalid backup timestamp")
    ordered = sorted(entries, key=lambda plan: os.fspath(plan.path))
    if len({plan.path for plan in ordered}) != len(ordered):
        raise InstallError("duplicate planned target")
    selector_preconditions: dict[Path, TargetSnapshot] = {}
    for plan in ordered:
        if not plan.path.is_absolute():
            raise InstallError("invalid planned target")
        if isinstance(plan, PlannedWrite) and plan.mode not in {0o600, 0o700}:
            raise InstallError("invalid planned target")
        if isinstance(plan, PlannedSymlink) and not plan.target.is_absolute():
            raise InstallError("invalid planned symlink target")
        selectors = plan.selector_preconditions if isinstance(plan, PlannedWrite) else ()
        for selector in selectors:
            if not selector.path.is_absolute():
                raise InstallError("invalid selector precondition")
            if (
                selector.path in selector_preconditions
                and selector_preconditions[selector.path] != selector.snapshot
            ):
                raise InstallError("conflicting selector preconditions")
            selector_preconditions[selector.path] = selector.snapshot

    _validate_source_handles(source_handles)

    snapshots: dict[Path, TargetSnapshot] = {}
    for plan in ordered:
        handle = _open_parent(plan.path, create=False, created_directories=[])
        if handle is None:
            current = TargetSnapshot(False)
        else:
            try:
                _require_parent_anchor(handle)
                current = _snapshot_at(handle, plan)
            finally:
                os.close(handle.descriptor)
        if plan.precondition is not None and not snapshot_matches(
            current, plan.precondition
        ):
            raise InstallError("planned target changed since plan was built")
        snapshots[plan.path] = current
    for path, expected in selector_preconditions.items():
        current = snapshots.get(path)
        if current is None:
            current = inspect_target(PlannedWrite(path, b"", 0o600, False))
        if not snapshot_matches(current, expected):
            raise InstallError("AGENTS selector changed since plan was built")
    changed = [
        plan
        for plan in ordered
        if (
            not snapshots[plan.path].exists
            or (
                isinstance(plan, PlannedWrite)
                and (
                    snapshots[plan.path].link_target is not None
                    or snapshots[plan.path].data != plan.data
                    or snapshots[plan.path].mode != plan.mode
                )
            )
        )
    ]
    if not changed:
        _validate_source_handles(source_handles)
        return []

    backup_names = {
        plan.path: plan.path.name + f".bak.{namespace}." + stamp
        for plan in changed
        if isinstance(plan, PlannedWrite)
        and plan.backup
        and snapshots[plan.path].exists
    }
    for plan in changed:
        backup_name = backup_names.get(plan.path)
        if backup_name is None:
            continue
        handle = _open_parent(plan.path, create=False, created_directories=[])
        assert handle is not None
        try:
            _require_parent_anchor(handle)
            if _entry_identity(handle, backup_name) is not None:
                raise InstallError("transaction backup already exists")
        finally:
            os.close(handle.descriptor)

    created_directories: list[_CreatedDirectory] = []
    handles: dict[Path, _ParentHandle] = {}
    staged: dict[Path, _OwnedEntry] = {}
    restores: dict[Path, _OwnedEntry] = {}
    created_backups: dict[Path, _OwnedEntry] = {}
    quarantines: dict[Path, _OwnedEntry] = {}
    replaced: list[PlannedEntry] = []
    rollback_failed = False
    replace_impl = _SYSTEM_REPLACE if replace is None else replace
    stage_prefix = f".{namespace}-stage."
    restore_prefix = f".{namespace}-restore."
    quarantine_prefix = f".{namespace}-quarantine."
    try:
        for plan in changed:
            handle = _open_parent(
                plan.path,
                create=True,
                created_directories=created_directories,
            )
            assert handle is not None
            handles[plan.path] = handle
            _require_parent_anchor(handle)
            if not snapshot_matches(_snapshot_at(handle, plan), snapshots[plan.path]):
                raise InstallError("planned target changed after preflight")
        for plan in changed:
            handle = handles[plan.path]
            _require_parent_anchor(handle)
            if isinstance(plan, PlannedWrite):
                staged[plan.path] = _stage_regular_at(
                    handle,
                    plan.data,
                    plan.mode,
                    stage_prefix,
                )
            else:
                staged[plan.path] = _stage_symlink_at(
                    handle,
                    plan.target,
                    stage_prefix,
                )
            if snapshots[plan.path].exists:
                restores[plan.path] = _stage_restore_at(
                    handle,
                    snapshots[plan.path],
                    restore_prefix,
                )
        for plan in changed:
            backup_name = backup_names.get(plan.path)
            if backup_name is None:
                continue
            handle = handles[plan.path]
            _transaction_phase(
                source_handles,
                phase_hook,
                "before_backup_revalidate",
                plan.path,
            )
            _require_parent_anchor(handle)
            if not snapshot_matches(_snapshot_at(handle, plan), snapshots[plan.path]):
                raise InstallError("planned target changed before backup")
            _transaction_phase(
                source_handles,
                phase_hook,
                "before_backup_create",
                plan.path.with_name(backup_name),
            )
            _require_parent_anchor(handle)
            created_backups[plan.path] = _write_backup_at(
                handle,
                backup_name,
                snapshots[plan.path].data,
            )
        for plan in changed:
            handle = handles[plan.path]
            _transaction_phase(
                source_handles,
                phase_hook,
                "before_replace_revalidate",
                plan.path,
            )
            _require_parent_anchor(handle)
            if not snapshot_matches(_snapshot_at(handle, plan), snapshots[plan.path]):
                raise InstallError("planned target changed before replacement")
            stage = staged[plan.path]
            if snapshots[plan.path].exists:
                _transaction_phase(
                    source_handles,
                    phase_hook,
                    "before_target_claim",
                    plan.path,
                )
                _require_parent_anchor(handle)
                quarantine = _claim_target_at(
                    handle,
                    plan.path.name,
                    quarantine_prefix,
                )
                quarantines[plan.path] = quarantine
                os.fsync(handle.descriptor)
                captured = _snapshot_at(handle, plan, entry_name=quarantine.name)
                if not snapshot_matches(captured, snapshots[plan.path]):
                    raise InstallError("atomically claimed target did not match snapshot")
            _transaction_phase(
                source_handles,
                phase_hook,
                "after_target_claim",
                plan.path,
            )
            _require_parent_anchor(handle)
            try:
                if replace is None:
                    _rename_noreplace(
                        handle.descriptor,
                        stage.name,
                        handle.descriptor,
                        plan.path.name,
                    )
                else:
                    replace_impl(handle.path / stage.name, plan.path)
            except BaseException:
                if _entry_identity(handle, stage.name) is None:
                    replaced.append(plan)
                raise
            replaced.append(plan)
            os.fsync(handle.descriptor)
            _require_parent_anchor(handle)
        for handle in handles.values():
            _require_parent_anchor(handle)
        _transaction_phase(
            source_handles,
            phase_hook,
            "before_commit_cleanup",
            changed[-1].path,
        )
        for plan, quarantine in list(quarantines.items()):
            _unlink_owned(handles[plan], quarantine)
            del quarantines[plan]
        for plan, restore in list(restores.items()):
            _unlink_owned(handles[plan], restore)
            del restores[plan]
        for handle in handles.values():
            _require_parent_anchor(handle)
        return [plan.path for plan in changed]
    except BaseException as error:
        for plan in reversed(replaced):
            handle = handles[plan.path]
            snapshot = snapshots[plan.path]
            try:
                current_identity = _entry_identity(handle, plan.path.name)
                if current_identity != staged[plan.path].identity:
                    raise InstallError("replacement ownership changed before rollback")
                _unlink_owned(
                    handle,
                    _OwnedEntry(plan.path.name, staged[plan.path].identity),
                )
                if snapshot.exists:
                    quarantine = quarantines.get(plan.path)
                    if (
                        quarantine is not None
                        and _entry_identity(handle, quarantine.name) == quarantine.identity
                    ):
                        _restore_claim_at(handle, quarantine, plan.path.name)
                        quarantines.pop(plan.path, None)
                    else:
                        restore = restores.get(plan.path)
                        if restore is None or _entry_identity(handle, restore.name) != restore.identity:
                            restore = _stage_restore_at(
                                handle,
                                snapshot,
                                restore_prefix,
                            )
                        _rename_noreplace(
                            handle.descriptor,
                            restore.name,
                            handle.descriptor,
                            plan.path.name,
                        )
                        os.fsync(handle.descriptor)
                        restores.pop(plan.path, None)
            except BaseException:
                rollback_failed = True
        for plan, quarantine in list(quarantines.items()):
            try:
                if _entry_identity(handles[plan], plan.name) is not None:
                    rollback_failed = True
                    continue
                _restore_claim_at(handles[plan], quarantine, plan.name)
                quarantines.pop(plan, None)
            except BaseException:
                rollback_failed = True
        if not rollback_failed:
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


def apply_transaction(
    entries: list[PlannedEntry],
    *,
    namespace: str,
    replace: Callable | None = None,
    stamp: str | None = None,
    phase_hook: Callable[[str, Path], None] | None = None,
) -> list[Path]:
    """Apply one transaction while retaining all attached source guards."""
    preconditions = _collect_source_preconditions(entries)
    source_handles = _open_source_handles(preconditions)
    try:
        return _apply_transaction_with_source_handles(
            entries,
            namespace=namespace,
            replace=replace,
            stamp=stamp,
            phase_hook=phase_hook,
            source_handles=source_handles,
        )
    finally:
        _close_source_handles(source_handles)
