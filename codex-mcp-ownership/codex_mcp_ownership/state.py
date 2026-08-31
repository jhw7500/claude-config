from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import errno
import fcntl as fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Callable, Iterator, TypeVar

from . import deadline_io as deadline_io
from . import model
from .deadline_io import (
    DeadlineBudget,
    DeadlineIO,
    DirectoryCapacityExceeded,
    OperationDeadlineExceeded,
)
from .model import ManagedProcess, SessionLease, SignalIntent
from .transition_truth import (
    RecoveryContradiction,
    RecoveryDecision,
    RecoveryEvidence,
    decide_recovery,
    derive_transition_id,
)


EVENT_LOG_MAX_BYTES = 1_048_576
EVENT_LOG_BACKUPS = 3
EVENT_LOG_RETENTION_SECONDS = 2_592_000
EVENT_RECEIPT_RETENTION = 1_024
TRANSITION_JOURNAL_LIMIT = 256
LEGACY_OUTBOX_DRAIN_LIMIT = 64
LEGACY_OUTBOX_CAPACITY = 256
TRANSACTION_RETENTION = 3
STATE_DIRECTORY_MAX_ENTRIES = 4_096
STATE_RECORD_MAX_BYTES = 1_048_576
STATE_JSON_MAX_DEPTH = 64
STATE_JSON_MAX_NODES = 100_000
EVENT_LOG_BACKUP_FILENAMES = tuple(
    f"events.jsonl.{number}" for number in range(1, EVENT_LOG_BACKUPS + 1)
)

INSTALL_STATE_FILENAME = "install-state.json"
INSTALL_STATE_TRANSACTION_FIELD = "transaction_id"
INSTALL_STATE_FIELDS = frozenset({INSTALL_STATE_TRANSACTION_FIELD})
INSTALL_STATE_LEGACY_FILENAMES = ("install_state.json", "install.json")
LEDGER_REVISION_FILENAME = "ledger-revision.json"

EVENT_FIELDS = {
    "schema_version",
    "event",
    "observed_wall",
    "server",
    "scope",
    "session_id",
    "process_key",
    "state",
    "reason_codes",
    "rss_kib",
    "event_id",
}

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_HEX_DIGEST_LENGTH = 64
_ATOMIC_TEMP_PATTERN = re.compile(r"\.tmp-[0-9a-f]{32}\Z")
_ATOMIC_TEMP_RECONCILE_LIMIT = 64
_ATOMIC_JSON_DIRECTORIES = (
    "sessions",
    "processes",
    "signal-intents",
    "force-receipts",
    "event-journal",
    "event-receipts",
)
_Record = TypeVar("_Record", SessionLease, ManagedProcess, SignalIntent)


class UnsafeStatePath(RuntimeError):
    """A state path does not satisfy the private-file trust boundary."""


class StateLockTimeout(TimeoutError):
    """The state flock could not be acquired before its deadline."""


class PostEffectStateError(RuntimeError):
    """The irreversible effect returned before durable completion failed."""

    def __init__(self, message: str, *, record_persisted: bool) -> None:
        super().__init__(message)
        self.record_persisted = record_persisted


class ReadOnlyStateError(PermissionError):
    """A mutation was attempted through an audit-only store."""


class StateCorruption(RuntimeError):
    """A private state file is well-formed at the filesystem layer but invalid."""

    def __init__(
        self,
        path: Path,
        digest: str,
        *,
        quarantine_path: Path | None = None,
    ) -> None:
        super().__init__(f"corrupt state record: {path}")
        self.path = path
        self.digest = digest
        self.quarantine_path = quarantine_path


@dataclass(frozen=True)
class RootBinding:
    root_token: tuple[int, int]
    parent_token: tuple[int, int]
    name: str


def _directory_names_with_io(
    io: DeadlineIO,
    directory_fd: int,
    path: Path,
    limit: int,
) -> tuple[str, ...]:
    try:
        return io.directory_names(directory_fd, limit)
    except DirectoryCapacityExceeded as error:
        raise StateCorruption(
            path,
            hashlib.sha256(b"directory_capacity").hexdigest(),
        ) from error


def session_key(session_id: str) -> str:
    validated = model.validate_session_id(session_id)
    return hashlib.sha256(validated.encode("utf-8")).hexdigest()


def _is_hex_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != _HEX_DIGEST_LENGTH:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _canonical_json(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("state value is not canonical JSON") from error
    return rendered.encode("utf-8") + b"\n"


def _mode(value: os.stat_result) -> int:
    return stat.S_IMODE(value.st_mode)


def _validate_directory(value: os.stat_result, path: Path) -> None:
    if not stat.S_ISDIR(value.st_mode):
        raise UnsafeStatePath(f"not a regular directory: {path}")
    if value.st_uid != os.getuid():
        raise UnsafeStatePath(f"state directory has a different owner: {path}")
    if _mode(value) != _DIRECTORY_MODE:
        raise UnsafeStatePath(f"state directory mode is not 0700: {path}")


def _validate_file(value: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise UnsafeStatePath(f"not a regular file: {path}")
    if value.st_uid != os.getuid():
        raise UnsafeStatePath(f"state file has a different owner: {path}")
    if value.st_nlink != 1:
        raise UnsafeStatePath(f"state file link count is not one: {path}")
    if _mode(value) != _FILE_MODE:
        raise UnsafeStatePath(f"state file mode is not 0600: {path}")


def _read_all_with_io(
    io: DeadlineIO,
    fd: int,
    *,
    max_bytes: int | None = None,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = io.read(fd, 131072)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise ValueError("file exceeds scan limit")
        chunks.append(chunk)


def _read_state_record_with_io(io: DeadlineIO, fd: int) -> bytes:
    chunks: list[bytes] = []
    remaining = STATE_RECORD_MAX_BYTES + 1
    while remaining:
        chunk = io.read(fd, min(131072, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > STATE_RECORD_MAX_BYTES:
        raise ValueError("state record exceeds maximum size")
    return raw


def _validate_json_resources(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > STATE_JSON_MAX_NODES or depth > STATE_JSON_MAX_DEPTH:
            raise ValueError("state JSON exceeds resource limits")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _parse_state_record(
    raw: bytes,
    parser: Callable[[object], _Record],
) -> _Record:
    if len(raw) > STATE_RECORD_MAX_BYTES:
        raise ValueError("state record exceeds maximum size")
    decoded = json.loads(raw.decode("utf-8"))
    _validate_json_resources(decoded)
    return parser(decoded)


def _write_all_with_io(io: DeadlineIO, fd: int, data: bytes) -> None:
    position = 0
    while position < len(data):
        written = io.write(fd, data[position:])
        if written <= 0:
            raise OSError(errno.EIO, "short state write")
        position += written


def _close_fd_after_failure(io: DeadlineIO, fd: int) -> None:
    try:
        io.close_fd(fd)
    except OSError:
        pass


class StateStore:
    def __init__(
        self, root: Path, read_only: bool = False, lock_timeout: float = 2.0
    ) -> None:
        converted_timeout = float(lock_timeout)
        if not math.isfinite(converted_timeout) or converted_timeout < 0:
            raise ValueError("lock_timeout must be non-negative")
        self.root = Path(os.path.abspath(os.fspath(root)))
        self.read_only = read_only
        self.lock_timeout = converted_timeout
        self._lock_condition = threading.Condition()
        self._lock_owner: int | None = None
        self._lock_releasing = False
        self._lock_fd: int | None = None
        self._pinned_root_fd: int | None = None
        self._lock_io: DeadlineIO | None = None
        self._lock_depth = 0
        self._effect_transition_ids: set[str] = set()
        self._operation_io = threading.local()

    def _require_mutable(self) -> None:
        if self.read_only:
            raise ReadOnlyStateError("read-only state store cannot mutate")

    def _owns_lock(self) -> bool:
        with self._lock_condition:
            return (
                self._lock_owner == threading.get_ident()
                and not self._lock_releasing
                and self._lock_depth > 0
                and self._pinned_root_fd is not None
                and self._lock_io is not None
            )

    def _operation_gateway(
        self,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> DeadlineIO:
        owner = threading.get_ident()
        with self._lock_condition:
            if self._lock_owner == owner and not self._lock_releasing:
                if self._lock_io is None or self._lock_depth < 1:
                    raise RuntimeError("invalid state-lock I/O gateway ownership")
                active_budget = self._lock_io.budget
                if deadline is not None and (
                    active_budget.deadline is None
                    or active_budget.monotonic is not monotonic
                    or deadline < active_budget.deadline
                ):
                    raise OperationDeadlineExceeded(
                        "nested operation deadline cannot narrow active I/O gateway"
                    )
                return self._lock_io
        return DeadlineIO(DeadlineBudget(deadline, monotonic))

    def _root_exists(self, *, io: DeadlineIO) -> bool:
        try:
            fd = self._open_root(io=io)
        except FileNotFoundError:
            return False
        io.close_fd(fd)
        return True

    def root_token(self) -> tuple[int, int]:
        """Return the device/inode identity of the current pinned state root."""
        io = self._operation_gateway(None, time.monotonic)
        fd = self._open_root(io=io)
        try:
            value = io.fstat(fd)
            return value.st_dev, value.st_ino
        finally:
            io.close_fd(fd)

    def _open_lexical_parent(self) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent = self.root.parent
        parts = parent.parts
        if not parent.is_absolute() or not parts:
            raise UnsafeStatePath("state root parent must be absolute")
        try:
            current_fd = os.open(parts[0], flags)
        except OSError as error:
            raise UnsafeStatePath("cannot open filesystem root") from error
        transferred = False
        try:
            for component in parts[1:]:
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except OSError as error:
                    raise UnsafeStatePath(
                        "cannot safely open state root parent"
                    ) from error
                os.close(current_fd)
                current_fd = next_fd
            value = os.fstat(current_fd)
            if not stat.S_ISDIR(value.st_mode):
                raise UnsafeStatePath("state root parent is not a directory")
            transferred = True
            return current_fd
        finally:
            if not transferred:
                os.close(current_fd)

    def _raw_root_binding(self) -> RootBinding:
        parent_fd = self._open_lexical_parent()
        try:
            parent = os.fstat(parent_fd)
            try:
                root = os.stat(
                    self.root.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise UnsafeStatePath(
                    "state root lexical binding is unavailable"
                ) from error
            _validate_directory(root, self.root)
            return RootBinding(
                (root.st_dev, root.st_ino),
                (parent.st_dev, parent.st_ino),
                self.root.name,
            )
        finally:
            os.close(parent_fd)

    def root_binding(self) -> RootBinding:
        """Capture the exact root and its immediate lexical parent/name binding."""
        io = self._operation_gateway(None, time.monotonic)
        return self._root_binding_with_io(io=io)

    def validate_root_binding(self, expected: RootBinding) -> None:
        if (
            not isinstance(expected, RootBinding)
            or self._raw_root_binding() != expected
        ):
            raise UnsafeStatePath("state root lexical binding changed")

    def _open_lexical_parent_with_io(self, *, io: DeadlineIO) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent = self.root.parent
        parts = parent.parts
        if not parent.is_absolute() or not parts:
            raise UnsafeStatePath("state root parent must be absolute")
        try:
            current_fd = io.open_fd(parts[0], flags)
        except OSError as error:
            raise UnsafeStatePath("cannot open filesystem root") from error
        transferred = False
        try:
            for component in parts[1:]:
                try:
                    next_fd = io.open_fd(component, flags, dir_fd=current_fd)
                except OSError as error:
                    raise UnsafeStatePath(
                        "cannot safely open state root parent"
                    ) from error
                io.close_fd(current_fd)
                current_fd = next_fd
            value = io.fstat(current_fd)
            if not stat.S_ISDIR(value.st_mode):
                raise UnsafeStatePath("state root parent is not a directory")
            transferred = True
            return current_fd
        finally:
            if not transferred:
                io.close_fd(current_fd)

    def _root_binding_with_io(self, *, io: DeadlineIO) -> RootBinding:
        parent_fd = self._open_lexical_parent_with_io(io=io)
        try:
            parent = io.fstat(parent_fd)
            try:
                root = io.stat(self.root.name, dir_fd=parent_fd)
            except OSError as error:
                raise UnsafeStatePath(
                    "state root lexical binding is unavailable"
                ) from error
            _validate_directory(root, self.root)
            return RootBinding(
                (root.st_dev, root.st_ino),
                (parent.st_dev, parent.st_ino),
                self.root.name,
            )
        finally:
            io.close_fd(parent_fd)

    def _validate_root_binding_with_io(
        self, expected: RootBinding, *, io: DeadlineIO
    ) -> None:
        if (
            not isinstance(expected, RootBinding)
            or self._root_binding_with_io(io=io) != expected
        ):
            raise UnsafeStatePath("state root lexical binding changed")

    def lexical_root_token(self) -> tuple[int, int]:
        """Return the inode currently bound to the configured root pathname."""
        io = self._operation_gateway(None, time.monotonic)
        value = io.stat(os.fspath(self.root))
        _validate_directory(value, self.root)
        return value.st_dev, value.st_ino

    def _walk_root(self, *, create: bool, io: DeadlineIO) -> int:
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        parts = self.root.parts
        if not self.root.is_absolute() or not parts:
            raise UnsafeStatePath("state root must be an absolute lexical path")
        try:
            current_fd = io.open_fd(parts[0], flags)
        except OSError as error:
            raise UnsafeStatePath("cannot open filesystem root") from error
        walked = Path(parts[0])
        transferred = False
        try:
            for index, component in enumerate(parts[1:], start=1):
                final = index == len(parts) - 1
                walked /= component
                try:
                    next_fd = io.open_fd(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not final or not create:
                        raise
                    anchor_fd: int | None = None
                    try:
                        anchor_fd = io.mkdir_private(
                            component,
                            _DIRECTORY_MODE,
                            dir_fd=current_fd,
                        )
                    except FileExistsError:
                        pass
                    next_fd: int | None = None
                    try:
                        next_fd = io.open_fd(component, flags, dir_fd=current_fd)
                    except OSError as error:
                        if anchor_fd is not None:
                            closing_fd = anchor_fd
                            anchor_fd = None
                            _close_fd_after_failure(io, closing_fd)
                        raise UnsafeStatePath(
                            f"cannot safely open state path component: {walked}"
                        ) from error
                    except Exception:
                        if anchor_fd is not None:
                            closing_fd = anchor_fd
                            anchor_fd = None
                            _close_fd_after_failure(io, closing_fd)
                        raise
                    try:
                        opened = io.fstat(next_fd)
                        _validate_directory(opened, walked)
                        if anchor_fd is not None:
                            anchored = io.fstat(anchor_fd)
                            _validate_directory(anchored, walked)
                            if (opened.st_dev, opened.st_ino) != (
                                anchored.st_dev,
                                anchored.st_ino,
                            ):
                                raise UnsafeStatePath(
                                    f"state directory changed during open: {walked}"
                                )
                    except Exception:
                        if anchor_fd is not None:
                            closing_fd = anchor_fd
                            anchor_fd = None
                            _close_fd_after_failure(io, closing_fd)
                        if next_fd is not None:
                            closing_fd = next_fd
                            next_fd = None
                            _close_fd_after_failure(io, closing_fd)
                        raise
                    if anchor_fd is not None:
                        closing_fd = anchor_fd
                        anchor_fd = None
                        try:
                            io.close_fd(closing_fd)
                        except Exception:
                            if next_fd is not None:
                                closing_fd = next_fd
                                next_fd = None
                                _close_fd_after_failure(io, closing_fd)
                            raise
                    try:
                        io.fsync(current_fd)
                    except Exception:
                        if next_fd is not None:
                            closing_fd = next_fd
                            next_fd = None
                            _close_fd_after_failure(io, closing_fd)
                        raise
                except OSError as error:
                    raise UnsafeStatePath(
                        f"cannot safely open state path component: {walked}"
                    ) from error
                assert next_fd is not None
                io.close_fd(current_fd)
                current_fd = next_fd
            _validate_directory(io.fstat(current_fd), self.root)
            transferred = True
            return current_fd
        finally:
            if not transferred:
                io.close_fd(current_fd)

    def _open_root(self, *, create: bool = False, io: DeadlineIO) -> int:
        owner = threading.get_ident()
        with self._lock_condition:
            if (
                self._lock_owner == owner
                and not self._lock_releasing
                and self._pinned_root_fd is not None
            ):
                fd = io.dup_fd(self._pinned_root_fd)
                try:
                    _validate_directory(io.fstat(fd), self.root)
                except Exception:
                    io.close_fd(fd)
                    raise
                return fd
        return self._walk_root(create=create, io=io)

    def _open_private_file(
        self,
        directory_fd: int,
        name: str,
        path: Path,
        access_flags: int = os.O_RDONLY,
        *,
        io: DeadlineIO,
    ) -> int:
        try:
            before = io.stat(name, dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise UnsafeStatePath(
                f"cannot safely inspect state file: {path}"
            ) from error
        _validate_file(before, path)
        flags = access_flags | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = io.open_fd(name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise UnsafeStatePath(f"cannot safely open state file: {path}") from error
        try:
            after = io.fstat(fd)
            _validate_file(after, path)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise UnsafeStatePath(f"state file changed during open: {path}")
        except Exception:
            io.close_fd(fd)
            raise
        return fd

    def _open_directory(
        self,
        root_fd: int,
        name: str,
        *,
        create: bool,
        io: DeadlineIO,
    ) -> int | None:
        path = self.root / name
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = io.open_fd(name, flags, dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                return None
            if not self._owns_lock():
                raise RuntimeError("private directory creation requires the state lock")
            anchor_fd: int | None = None
            try:
                anchor_fd = io.mkdir_private(
                    name,
                    _DIRECTORY_MODE,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                pass
            try:
                fd = io.open_fd(name, flags, dir_fd=root_fd)
            except Exception:
                if anchor_fd is not None:
                    io.close_fd(anchor_fd)
                raise
            try:
                opened = io.fstat(fd)
                _validate_directory(opened, path)
                if anchor_fd is not None:
                    anchored = io.fstat(anchor_fd)
                    _validate_directory(anchored, path)
                    if (opened.st_dev, opened.st_ino) != (
                        anchored.st_dev,
                        anchored.st_ino,
                    ):
                        raise UnsafeStatePath(
                            f"state directory changed during open: {path}"
                        )
                if anchor_fd is not None:
                    closing_fd = anchor_fd
                    anchor_fd = None
                    io.close_fd(closing_fd)
                io.fsync(root_fd)
            except Exception:
                if anchor_fd is not None:
                    io.close_fd(anchor_fd)
                io.close_fd(fd)
                raise
        except OSError as error:
            raise UnsafeStatePath(
                f"cannot safely open state directory: {path}"
            ) from error
        try:
            _validate_directory(io.fstat(fd), path)
        except Exception:
            io.close_fd(fd)
            raise
        return fd

    def _create_lock_file(self, root_fd: int, *, io: DeadlineIO) -> int:
        common = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = io.open_fd(
                "state.lock",
                common | os.O_CREAT | os.O_EXCL,
                _FILE_MODE,
                dir_fd=root_fd,
            )
        except FileExistsError:
            fd = self._open_private_file(
                root_fd,
                "state.lock",
                self.root / "state.lock",
                os.O_RDWR,
                io=io,
            )
        else:
            try:
                io.fchmod(fd, _FILE_MODE)
                io.fsync(fd)
                io.fsync(root_fd)
            except Exception:
                io.close_fd(fd)
                raise
        try:
            _validate_file(io.fstat(fd), self.root / "state.lock")
        except Exception:
            io.close_fd(fd)
            raise
        return fd

    def _validate_lock_binding(
        self, lock_fd: int, root_fd: int, *, io: DeadlineIO
    ) -> None:
        opened = io.fstat(lock_fd)
        _validate_file(opened, self.root / "state.lock")
        named_fd = self._open_private_file(
            root_fd, "state.lock", self.root / "state.lock", io=io
        )
        try:
            named = io.fstat(named_fd)
        finally:
            io.close_fd(named_fd)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise UnsafeStatePath("state.lock was replaced during lock acquisition")

    @contextmanager
    def _locked_with_io(
        self,
        *,
        io: DeadlineIO,
        expected_root_token: tuple[int, int] | None = None,
        remaining_timeout: float | None = None,
    ) -> Iterator[StateStore]:
        stack = getattr(self._operation_io, "stack", None)
        if stack is None:
            stack = []
            self._operation_io.stack = stack
        if stack and stack[-1] is not io:
            raise RuntimeError("reentrant state-lock I/O gateway changed")
        stack.append(io)
        try:
            if expected_root_token is None and remaining_timeout is None:
                lock = self.locked()
            elif remaining_timeout is None:
                lock = self.locked(expected_root_token=expected_root_token)
            elif expected_root_token is None:
                lock = self.locked(remaining_timeout=remaining_timeout)
            else:
                lock = self.locked(
                    expected_root_token=expected_root_token,
                    remaining_timeout=remaining_timeout,
                )
            with lock:
                yield self
        finally:
            if not stack or stack.pop() is not io:
                raise RuntimeError("invalid operation I/O gateway ownership")

    @contextmanager
    def locked(
        self,
        *,
        expected_root_token: tuple[int, int] | None = None,
        remaining_timeout: float | None = None,
    ) -> Iterator[StateStore]:
        self._require_mutable()
        stack = getattr(self._operation_io, "stack", None)
        if stack:
            io = stack[-1]
            registered = False
        else:
            io = DeadlineIO(DeadlineBudget(None, time.monotonic))
            if stack is None:
                stack = []
                self._operation_io.stack = stack
            stack.append(io)
            registered = True
        try:
            with self._locked_core(
                io=io,
                expected_root_token=expected_root_token,
                remaining_timeout=remaining_timeout,
            ):
                yield self
        finally:
            if registered and (not stack or stack.pop() is not io):
                raise RuntimeError("invalid operation I/O gateway ownership")

    @contextmanager
    def _locked_core(
        self,
        *,
        io: DeadlineIO,
        expected_root_token: tuple[int, int] | None = None,
        remaining_timeout: float | None = None,
    ) -> Iterator[StateStore]:
        owner = threading.get_ident()
        timeout = self.lock_timeout
        if remaining_timeout is not None:
            converted = float(remaining_timeout)
            if not math.isfinite(converted) or converted < 0:
                raise ValueError("remaining_timeout must be non-negative")
            timeout = min(timeout, converted)
        lock_deadline = time.monotonic() + timeout
        nested = False
        with self._lock_condition:
            if self._lock_owner == owner:
                if self._lock_releasing:
                    raise RuntimeError("state lock is releasing")
                if self._pinned_root_fd is None or self._lock_depth < 1:
                    raise RuntimeError("invalid reentrant state-lock ownership")
                if self._lock_io is not io:
                    raise RuntimeError("reentrant state-lock I/O gateway changed")
                if expected_root_token is not None:
                    pinned = io.fstat(self._pinned_root_fd)
                    if (pinned.st_dev, pinned.st_ino) != expected_root_token:
                        raise UnsafeStatePath("state root identity changed")
                self._lock_depth += 1
                nested = True
            else:
                while self._lock_owner is not None:
                    lock_remaining = lock_deadline - time.monotonic()
                    if lock_remaining <= 0:
                        raise StateLockTimeout("timed out waiting for state-lock owner")
                    operation_remaining = io.budget.remaining()
                    wait_for = (
                        lock_remaining
                        if operation_remaining is None
                        else min(lock_remaining, operation_remaining)
                    )
                    self._lock_condition.wait(wait_for)
                self._lock_owner = owner
                self._lock_depth = 1
                self._lock_io = io
        if nested:
            try:
                yield self
            finally:
                with self._lock_condition:
                    if (
                        self._lock_owner != owner
                        or self._lock_depth <= 1
                        or self._lock_io is not io
                    ):
                        raise RuntimeError("invalid nested state-lock release")
                    self._lock_depth -= 1
            return
        root_fd: int | None = None
        lock_fd: int | None = None
        flocked = False
        try:
            root_fd = self._open_root(
                create=expected_root_token is None,
                io=io,
            )
            if expected_root_token is not None:
                root_value = io.fstat(root_fd)
                if (root_value.st_dev, root_value.st_ino) != expected_root_token:
                    raise UnsafeStatePath("state root identity changed")
            lock_fd = self._create_lock_file(root_fd, io=io)
            while True:
                try:
                    io.flock_exclusive_nonblocking(lock_fd)
                    flocked = True
                    self._validate_lock_binding(lock_fd, root_fd, io=io)
                    break
                except BlockingIOError as error:
                    lock_remaining = lock_deadline - time.monotonic()
                    if lock_remaining <= 0:
                        raise StateLockTimeout(
                            "timed out acquiring state.lock"
                        ) from error
                    operation_remaining = io.budget.remaining()
                    sleep_for = (
                        min(0.01, lock_remaining)
                        if operation_remaining is None
                        else min(0.01, lock_remaining, operation_remaining)
                    )
                    time.sleep(sleep_for)
            with self._lock_condition:
                if (
                    self._lock_owner != owner
                    or self._lock_depth != 1
                    or self._lock_io is not io
                ):
                    raise RuntimeError(
                        "state-lock ownership changed during acquisition"
                    )
                self._lock_fd = lock_fd
                self._pinned_root_fd = root_fd
            try:
                yield self
            finally:
                with self._lock_condition:
                    if (
                        self._lock_owner != owner
                        or self._lock_depth != 1
                        or self._lock_io is not io
                    ):
                        raise RuntimeError(
                            "outer state lock exited with nested ownership"
                        )
                    self._lock_releasing = True
        finally:
            try:
                if flocked and lock_fd is not None:
                    io.unlock_fd(lock_fd)
            finally:
                try:
                    if lock_fd is not None:
                        io.close_fd(lock_fd)
                finally:
                    try:
                        if root_fd is not None:
                            io.close_fd(root_fd)
                    finally:
                        with self._lock_condition:
                            if self._lock_owner == owner:
                                self._lock_depth = 0
                                self._lock_fd = None
                                self._pinned_root_fd = None
                                self._lock_io = None
                                self._lock_releasing = False
                                self._lock_owner = None
                                self._lock_condition.notify_all()

    def _atomic_json(
        self,
        directory_fd: int,
        directory: Path,
        name: str,
        value: object,
        *,
        io: DeadlineIO,
    ) -> None:
        if not self._owns_lock():
            raise RuntimeError("atomic state writes require the state lock")
        io.budget.check()
        data = _canonical_json(value)
        io.budget.check()
        temporary = f".tmp-{secrets.token_hex(16)}"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd: int | None = None
        created: os.stat_result | None = None
        close_finalizer_failed = False
        try:
            fd = io.open_fd(temporary, flags, _FILE_MODE, dir_fd=directory_fd)
            created = io.fstat(fd)
            io.fchmod(fd, _FILE_MODE)
            _write_all_with_io(io, fd, data)
            io.fsync(fd)
            closing_fd = fd
            fd = None
            try:
                io.close_fd(closing_fd)
            except Exception:
                close_finalizer_failed = True
                raise
            try:
                target_fd = self._open_private_file(
                    directory_fd,
                    name,
                    directory / name,
                    io=io,
                )
            except FileNotFoundError:
                target_fd = None
            if target_fd is not None:
                io.close_fd(target_fd)
            io.replace(
                temporary,
                name,
                source_dir_fd=directory_fd,
                destination_dir_fd=directory_fd,
            )
            io.fsync(directory_fd)
        except OperationDeadlineExceeded:
            if fd is not None:
                closing_fd = fd
                fd = None
                try:
                    io.close_fd(closing_fd)
                except OSError:
                    pass
            raise
        except Exception:
            if fd is not None:
                closing_fd = fd
                fd = None
                try:
                    io.close_fd(closing_fd)
                except OSError:
                    pass
            if (
                not close_finalizer_failed
                and not io.budget.expired()
                and created is not None
            ):
                try:
                    current = io.stat(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    current = None
                if (
                    current is not None
                    and (current.st_dev, current.st_ino)
                    == (
                        created.st_dev,
                        created.st_ino,
                    )
                    and stat.S_ISREG(current.st_mode)
                    and current.st_uid == os.getuid()
                    and _mode(current) == _FILE_MODE
                    and current.st_nlink == 1
                ):
                    io.unlink(temporary, dir_fd=directory_fd)
            raise

    def _maintenance_locked(self, *, io: DeadlineIO) -> None:
        io.budget.check()
        if io.budget.deadline is not None:
            # Time-bounded writers enforce the authoritative receipt invariant;
            # unrelated archival and transaction housekeeping remains deferrable.
            self._prune_event_receipts_locked(io=io)
            return
        self._prune_event_backups_locked(io=io)
        self._prune_event_receipts_locked(io=io)
        self._prune_transactions_locked(io=io)

    def ledger_revision(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> int:
        io = self._operation_gateway(deadline, monotonic)
        io.budget.check()
        if self._owns_lock() or self.read_only:
            return self._ledger_revision_locked(io=io)
        with self._locked_with_io(io=io):
            return self._ledger_revision_locked(io=io)

    def _ledger_revision_locked(self, *, io: DeadlineIO) -> int:
        io.budget.check()
        root_fd = self._open_root(io=io)
        try:
            try:
                fd = self._open_private_file(
                    root_fd,
                    LEDGER_REVISION_FILENAME,
                    self.root / LEDGER_REVISION_FILENAME,
                    io=io,
                )
            except FileNotFoundError:
                return 0
            try:
                raw = _read_state_record_with_io(io, fd)
            finally:
                io.close_fd(fd)
        finally:
            io.close_fd(root_fd)

        try:
            payload = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(payload, dict)
                or set(payload) != {"schema_version", "revision"}
                or payload["schema_version"] != 1
                or type(payload["revision"]) is not int
                or payload["revision"] < 0
            ):
                raise ValueError("invalid ledger revision")
            return payload["revision"]
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as error:
            raise StateCorruption(
                self.root / LEDGER_REVISION_FILENAME,
                hashlib.sha256(raw).hexdigest(),
            ) from error

    def _bump_ledger_revision_locked(self, *, io: DeadlineIO) -> int:
        if not self._owns_lock():
            raise RuntimeError("ledger revision update requires the state lock")
        io.budget.check()
        revision = self._ledger_revision_locked(io=io) + 1
        io.budget.check()
        root_fd = self._open_root(io=io)
        try:
            self._atomic_json(
                root_fd,
                self.root,
                LEDGER_REVISION_FILENAME,
                {"schema_version": 1, "revision": revision},
                io=io,
            )
        finally:
            io.close_fd(root_fd)
        return revision

    def _save_record_locked(
        self,
        kind: str,
        key: str,
        payload: dict[str, object],
        *,
        maintenance: bool,
        io: DeadlineIO,
    ) -> int:
        if not self._owns_lock():
            raise RuntimeError("state record write requires the state lock")
        if maintenance:
            self._maintenance_locked(io=io)
        io.budget.check()
        root_fd = self._open_root(io=io)
        try:
            directory_fd = self._open_directory(root_fd, kind, create=True, io=io)
            assert directory_fd is not None
            try:
                revision = self._bump_ledger_revision_locked(io=io)
                self._atomic_json(
                    directory_fd,
                    self.root / kind,
                    key + ".json",
                    payload,
                    io=io,
                )
                return revision
            finally:
                io.close_fd(directory_fd)
        finally:
            io.close_fd(root_fd)

    def save_session(self, lease: SessionLease, *, maintenance: bool = True) -> None:
        if not isinstance(lease, SessionLease):
            raise TypeError("lease must be a SessionLease")
        key = session_key(lease.session_id)
        payload = lease.to_dict()
        self._require_mutable()
        io = self._operation_gateway(None, time.monotonic)
        with self._locked_with_io(io=io):
            self._recover_before_write_locked(io=io)
            self._save_record_locked(
                "sessions",
                key,
                payload,
                maintenance=maintenance,
                io=io,
            )

    def save_process(
        self, process: ManagedProcess, *, maintenance: bool = True
    ) -> None:
        if not isinstance(process, ManagedProcess):
            raise TypeError("process must be a ManagedProcess")
        key = process.wrapper.stable_key()
        payload = process.to_dict()
        self._require_mutable()
        io = self._operation_gateway(None, time.monotonic)
        with self._locked_with_io(io=io):
            self._recover_before_write_locked(io=io)
            self._save_record_locked(
                "processes",
                key,
                payload,
                maintenance=maintenance,
                io=io,
            )

    def remove_process(self, process: ManagedProcess | str) -> None:
        key = (
            process.wrapper.stable_key()
            if isinstance(process, ManagedProcess)
            else process
        )
        if not isinstance(key, str) or len(key) != _HEX_DIGEST_LENGTH:
            raise ValueError("invalid process key")
        try:
            int(key, 16)
        except ValueError as error:
            raise ValueError("invalid process key") from error
        self._require_mutable()
        io = self._operation_gateway(None, time.monotonic)
        if not self._root_exists(io=io):
            return
        with self._locked_with_io(io=io):
            self._recover_before_write_locked(io=io)
            self._maintenance_locked(io=io)
            self._bump_ledger_revision_locked(io=io)
            root_fd = self._open_root(io=io)
            try:
                directory_fd = self._open_directory(
                    root_fd, "processes", create=False, io=io
                )
                if directory_fd is None:
                    return
                try:
                    name = key + ".json"
                    try:
                        fd = self._open_private_file(
                            directory_fd,
                            name,
                            self.root / "processes" / name,
                            io=io,
                        )
                    except FileNotFoundError:
                        return
                    io.close_fd(fd)
                    io.unlink(name, dir_fd=directory_fd)
                    io.fsync(directory_fd)
                finally:
                    io.close_fd(directory_fd)
            finally:
                io.close_fd(root_fd)

    def load_sessions(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[SessionLease, ...]:
        io = self._operation_gateway(deadline, monotonic)
        return self._load_sessions_with_io(io=io)

    def _load_sessions_with_io(self, *, io: DeadlineIO) -> tuple[SessionLease, ...]:
        return self._load_records_with_io(
            "sessions",
            SessionLease.from_dict,
            lambda value: session_key(value.session_id),
            io=io,
        )

    def sessions_digest(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> str:
        io = self._operation_gateway(deadline, monotonic)
        return self._sessions_digest_with_io(io=io)

    def _sessions_digest_with_io(self, *, io: DeadlineIO) -> str:
        io.budget.check()
        leases = self._load_sessions_with_io(io=io)
        io.budget.check()
        payload = [
            lease.to_dict()
            for lease in sorted(leases, key=lambda item: item.session_id)
        ]
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(rendered).hexdigest()

    def load_session(
        self,
        session_id: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> SessionLease | None:
        """Load only the SHA-256-addressed lease for ``session_id``."""
        io = self._operation_gateway(deadline, monotonic)
        key = session_key(session_id)
        value = self._load_exact_record_with_io(
            "sessions",
            key,
            SessionLease.from_dict,
            lambda item: session_key(item.session_id),
            io=io,
        )
        if value is not None and value.session_id != session_id:
            raise StateCorruption(self.root / "sessions" / f"{key}.json", key)
        return value

    def load_process(self, process_key: str) -> ManagedProcess | None:
        io = self._operation_gateway(None, time.monotonic)
        process = self._load_raw_process_with_io(process_key, io=io)
        if process is None:
            return None
        return self._overlay_signal_intent(process, io=io)

    def load_raw_process(
        self,
        process_key: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> ManagedProcess | None:
        io = self._operation_gateway(deadline, monotonic)
        return self._load_raw_process_with_io(process_key, io=io)

    def _load_raw_process_with_io(
        self, process_key: str, *, io: DeadlineIO
    ) -> ManagedProcess | None:
        if not isinstance(process_key, str) or len(process_key) != _HEX_DIGEST_LENGTH:
            raise ValueError("invalid process key")
        try:
            int(process_key, 16)
        except ValueError as error:
            raise ValueError("invalid process key") from error
        process = self._load_exact_record_with_io(
            "processes",
            process_key,
            ManagedProcess.from_dict,
            lambda item: item.wrapper.stable_key(),
            io=io,
        )
        if process is None:
            return None
        assert isinstance(process, ManagedProcess)
        return process

    def load_signal_intent(
        self,
        process_key: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> SignalIntent | None:
        io = self._operation_gateway(deadline, monotonic)
        return self._load_signal_intent_with_io(process_key, action="term", io=io)

    def _load_signal_intent_with_io(
        self,
        process_key: str,
        *,
        action: str,
        io: DeadlineIO,
    ) -> SignalIntent | None:
        if not _is_hex_digest(process_key):
            raise ValueError("invalid process key")
        kind = "force-receipts" if action == "force" else "signal-intents"
        value = self._load_exact_record_with_io(
            kind,
            process_key,
            SignalIntent.from_dict,
            lambda item: item.process_key,
            io=io,
        )
        assert value is None or isinstance(value, SignalIntent)
        return value

    def load_force_intent(
        self,
        process_key: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> SignalIntent | None:
        io = self._operation_gateway(deadline, monotonic)
        return self._load_signal_intent_with_io(process_key, action="force", io=io)

    def load_signal_intents(
        self,
        action: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[SignalIntent, ...]:
        io = self._operation_gateway(deadline, monotonic)
        return self._load_signal_intents_with_io(action, io=io)

    def _load_signal_intents_with_io(
        self, action: str, *, io: DeadlineIO
    ) -> tuple[SignalIntent, ...]:
        if action not in {"term", "force"}:
            raise ValueError("invalid signal intent action")
        kind = "force-receipts" if action == "force" else "signal-intents"
        values = self._load_records_with_io(
            kind,
            SignalIntent.from_dict,
            lambda item: item.process_key,
            io=io,
        )
        intents = tuple(value for value in values if isinstance(value, SignalIntent))
        if any(intent.action != action for intent in intents):
            raise StateCorruption(
                self.root / kind,
                hashlib.sha256(b"intent_action_mismatch").hexdigest(),
            )
        return intents

    def save_signal_intent(self, intent: SignalIntent) -> int:
        if not isinstance(intent, SignalIntent):
            raise TypeError("intent must be a SignalIntent")
        directory_name = (
            "force-receipts" if intent.action == "force" else "signal-intents"
        )
        self._require_mutable()
        io = self._operation_gateway(None, time.monotonic)
        with self._locked_with_io(io=io):
            self._recover_before_write_locked(io=io)
            existing = self._load_signal_intent_with_io(
                intent.process_key,
                action=intent.action,
                io=io,
            )
            merged = self._merge_signal_intent(existing, intent)
            if merged == existing:
                return self._ledger_revision_locked(io=io)
            return self._save_record_locked(
                directory_name,
                intent.process_key,
                merged.to_dict(),
                maintenance=False,
                io=io,
            )

    @staticmethod
    def _merge_signal_intent(
        existing: SignalIntent | None,
        proposed: SignalIntent,
    ) -> SignalIntent:
        if existing is None:
            return proposed
        same_authority = bool(
            existing.process_key == proposed.process_key
            and existing.owner_generation == proposed.owner_generation
            and existing.identity_keys == proposed.identity_keys
            and existing.action == proposed.action
        )
        if not same_authority:
            if existing.action == "force" and existing.delivered_keys:
                raise ValueError("delivered force receipt cannot be replaced")
            return proposed
        delivered = tuple(
            sorted(set(existing.delivered_keys) | set(proposed.delivered_keys))
        )
        dispatch = tuple(
            sorted(
                set(existing.dispatch_keys)
                | set(proposed.dispatch_keys)
                | set(delivered)
            )
        )
        if existing.status in {"delivered", "conflict"}:
            status = existing.status
        else:
            status = proposed.status
        term_sent_boot = None
        if status == "delivered" and proposed.action == "term":
            term_sent_boot = (
                existing.term_sent_boot
                if existing.term_sent_boot is not None
                else proposed.term_sent_boot
            )
        return replace(
            existing,
            status=status,
            delivered_keys=delivered,
            term_sent_boot=term_sent_boot,
            dispatch_keys=dispatch,
        )

    def remove_signal_intent(self, process_key: str, *, action: str = "term") -> int:
        if not _is_hex_digest(process_key):
            raise ValueError("invalid process key")
        if action not in {"term", "force"}:
            raise ValueError("invalid signal intent action")
        directory_name = "force-receipts" if action == "force" else "signal-intents"
        self._require_mutable()
        io = self._operation_gateway(None, time.monotonic)
        with self._locked_with_io(io=io):
            self._recover_before_write_locked(io=io)
            root_fd = self._open_root(io=io)
            try:
                directory_fd = self._open_directory(
                    root_fd, directory_name, create=False, io=io
                )
                if directory_fd is None:
                    return self._ledger_revision_locked(io=io)
                try:
                    revision = self._bump_ledger_revision_locked(io=io)
                    try:
                        io.unlink(process_key + ".json", dir_fd=directory_fd)
                    except FileNotFoundError:
                        return revision
                    io.fsync(directory_fd)
                    return revision
                finally:
                    io.close_fd(directory_fd)
            finally:
                io.close_fd(root_fd)

    def _overlay_signal_intent(
        self, process: ManagedProcess, *, io: DeadlineIO
    ) -> ManagedProcess:
        intent = self._load_signal_intent_with_io(
            process.wrapper.stable_key(), action="term", io=io
        )
        recorded_keys = {
            process.wrapper.stable_key(),
            *(member.stable_key() for member in process.members),
        }
        if process.child is not None:
            recorded_keys.add(process.child.stable_key())
        if (
            intent is not None
            and intent.action == "term"
            and process.owner_generation is not None
            and intent.owner_generation == process.owner_generation
            and frozenset(intent.identity_keys).issubset(recorded_keys)
        ):
            keys = frozenset(
                intent.delivered_keys
                if intent.status == "delivered"
                else intent.identity_keys
            )
            if intent.status == "delivered" and intent.term_sent_boot is not None:
                process = replace(
                    process,
                    term_sent_boot=intent.term_sent_boot,
                    term_sent_keys=keys,
                    owner_reason_codes=tuple(
                        reason
                        for reason in process.owner_reason_codes
                        if reason != "signal_term_pending"
                    ),
                )
            else:
                process = replace(
                    process,
                    term_sent_boot=None,
                    term_sent_keys=keys,
                    owner_reason_codes=tuple(
                        dict.fromkeys(
                            process.owner_reason_codes + ("signal_term_pending",)
                        )
                    ),
                )
        force = self._load_signal_intent_with_io(
            process.wrapper.stable_key(), action="force", io=io
        )
        if (
            force is None
            or process.owner_generation is None
            or force.owner_generation != process.owner_generation
        ):
            return process
        force_reason = {
            "pending": "signal_force_pending",
            "conflict": "signal_force_conflict",
            "delivered": "signal_force_delivered",
        }[force.status]
        return replace(
            process,
            owner_reason_codes=tuple(
                dict.fromkeys(process.owner_reason_codes + (force_reason,))
            ),
        )

    def _load_exact_record_with_io(
        self,
        kind: str,
        key: str,
        parser: Callable[[object], _Record],
        key_for: Callable[[_Record], str],
        *,
        io: DeadlineIO,
    ) -> _Record | None:
        io.budget.check()
        if not self._root_exists(io=io):
            return None
        if self.read_only:
            return self._load_exact_record_locked_or_read_only(
                kind,
                key,
                parser,
                key_for,
                io=io,
            )
        with self._locked_with_io(io=io):
            return self._load_exact_record_locked_or_read_only(
                kind,
                key,
                parser,
                key_for,
                io=io,
            )

    def _load_exact_record_locked_or_read_only(
        self,
        kind: str,
        key: str,
        parser: Callable[[object], _Record],
        key_for: Callable[[_Record], str],
        *,
        io: DeadlineIO,
    ) -> _Record | None:
        io.budget.check()
        root_fd = self._open_root(io=io)
        try:
            directory_fd = self._open_directory(root_fd, kind, create=False, io=io)
            if directory_fd is None:
                return None
            try:
                name = key + ".json"
                path = self.root / kind / name
                try:
                    fd = self._open_private_file(directory_fd, name, path, io=io)
                except FileNotFoundError:
                    return None
                try:
                    raw = _read_state_record_with_io(io, fd)
                except (ValueError, OverflowError, RecursionError):
                    io.lseek(fd, 0, os.SEEK_SET)
                    raw = io.read(fd, STATE_RECORD_MAX_BYTES + 1)
                finally:
                    io.close_fd(fd)
                digest = hashlib.sha256(raw).hexdigest()
                try:
                    record = _parse_state_record(raw, parser)
                    if key_for(record) != key:
                        raise ValueError(
                            "state filename does not match record identity"
                        )
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    TypeError,
                    OverflowError,
                    RecursionError,
                ):
                    quarantine = None
                    if not self.read_only:
                        quarantine = self._quarantine_locked(
                            root_fd,
                            directory_fd,
                            kind,
                            name,
                            raw,
                            io=io,
                        )
                    raise StateCorruption(
                        path,
                        digest,
                        quarantine_path=quarantine,
                    ) from None
                return record
            finally:
                io.close_fd(directory_fd)
        finally:
            io.close_fd(root_fd)

    def load_processes(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[ManagedProcess, ...]:
        io = self._operation_gateway(deadline, monotonic)
        return tuple(
            self._overlay_signal_intent(process, io=io)
            for process in self._load_records_with_io(
                "processes",
                ManagedProcess.from_dict,
                lambda value: value.wrapper.stable_key(),
                io=io,
            )
        )

    def load_raw_processes(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[ManagedProcess, ...]:
        io = self._operation_gateway(deadline, monotonic)
        return self._load_raw_processes_with_io(io=io)

    def _load_raw_processes_with_io(
        self, *, io: DeadlineIO
    ) -> tuple[ManagedProcess, ...]:
        records = self._load_records_with_io(
            "processes",
            ManagedProcess.from_dict,
            lambda value: value.wrapper.stable_key(),
            io=io,
        )
        return tuple(records)

    def _load_records_with_io(
        self,
        kind: str,
        parser: Callable[[object], _Record],
        key_for: Callable[[_Record], str],
        *,
        io: DeadlineIO,
    ) -> tuple[_Record, ...]:
        io.budget.check()
        if not self._root_exists(io=io):
            return ()
        if self.read_only:
            return self._load_records_locked_or_read_only(
                kind,
                parser,
                key_for,
                io=io,
            )
        with self._locked_with_io(io=io):
            return self._load_records_locked_or_read_only(
                kind,
                parser,
                key_for,
                io=io,
            )

    def _load_records_locked_or_read_only(
        self,
        kind: str,
        parser: Callable[[object], _Record],
        key_for: Callable[[_Record], str],
        *,
        io: DeadlineIO,
    ) -> tuple[_Record, ...]:
        io.budget.check()
        root_fd = self._open_root(io=io)
        try:
            directory_fd = self._open_directory(root_fd, kind, create=False, io=io)
            if directory_fd is None:
                return ()
            try:
                records: list[_Record] = []
                names = _directory_names_with_io(
                    io,
                    directory_fd,
                    self.root / kind,
                    STATE_DIRECTORY_MAX_ENTRIES,
                )
                for name in names:
                    io.budget.check()
                    path = self.root / kind / name
                    fd = self._open_private_file(directory_fd, name, path, io=io)
                    try:
                        raw = _read_state_record_with_io(io, fd)
                    except (ValueError, OverflowError, RecursionError):
                        io.lseek(fd, 0, os.SEEK_SET)
                        raw = io.read(fd, STATE_RECORD_MAX_BYTES + 1)
                    finally:
                        io.close_fd(fd)
                    digest = hashlib.sha256(raw).hexdigest()
                    try:
                        if not name.endswith(".json"):
                            raise ValueError("unexpected state filename")
                        record = _parse_state_record(raw, parser)
                        if name != key_for(record) + ".json":
                            raise ValueError(
                                "state filename does not match record identity"
                            )
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        ValueError,
                        TypeError,
                        OverflowError,
                        RecursionError,
                    ):
                        quarantine = None
                        if not self.read_only:
                            quarantine = self._quarantine_locked(
                                root_fd,
                                directory_fd,
                                kind,
                                name,
                                raw,
                                io=io,
                            )
                        raise StateCorruption(
                            path, digest, quarantine_path=quarantine
                        ) from None
                    records.append(record)
                    io.budget.check()
                return tuple(records)
            finally:
                io.close_fd(directory_fd)
        finally:
            io.close_fd(root_fd)

    def _quarantine_locked(
        self,
        root_fd: int,
        source_fd: int,
        kind: str,
        name: str,
        raw: bytes,
        *,
        io: DeadlineIO,
    ) -> Path:
        if not self._owns_lock():
            raise RuntimeError("quarantine requires the state lock")
        digest = hashlib.sha256(raw).hexdigest()
        quarantine_fd = self._open_directory(root_fd, "corrupt", create=True, io=io)
        assert quarantine_fd is not None
        try:
            destination = ""
            for _attempt in range(16):
                candidate = f"{kind}-{digest}-{secrets.token_hex(8)}.json"
                try:
                    io.rename_noreplace(
                        name,
                        candidate,
                        source_dir_fd=source_fd,
                        destination_dir_fd=quarantine_fd,
                    )
                except FileExistsError:
                    continue
                destination = candidate
                break
            if not destination:
                raise StateCorruption(self.root / kind / name, digest)
            io.fsync(source_fd)
            io.fsync(quarantine_fd)
            destination_path = self.root / "corrupt" / destination
            return destination_path
        finally:
            io.close_fd(quarantine_fd)

    def append_event(
        self,
        event: dict[str, object],
        *,
        maintenance: bool = True,
        remaining_timeout: float | None = None,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(event, dict):
            raise ValueError("event must be a record")
        unknown = set(event) - EVENT_FIELDS
        if unknown:
            raise ValueError("event contains forbidden fields")
        if "session_id" in event:
            model.validate_session_id(event["session_id"])
        if "event_id" in event and not _is_hex_digest(event["event_id"]):
            raise ValueError("invalid event ID")
        record = _canonical_json(event)
        if len(record) > EVENT_LOG_MAX_BYTES:
            raise ValueError("event record exceeds maximum log size")
        io = self._operation_gateway(deadline, monotonic)
        io.budget.check()
        self._require_mutable()
        with self._locked_with_io(io=io, remaining_timeout=remaining_timeout):
            self._recover_before_write_locked(io=io)
            if maintenance:
                self._maintenance_locked(io=io)
            io.budget.check()
            root_fd = self._open_root(io=io)
            try:
                event_id = event.get("event_id")
                if isinstance(event_id, str):
                    created = self._write_event_receipt_locked(
                        root_fd,
                        event_id,
                        event,
                        self._ledger_revision_locked(io=io),
                        io=io,
                    )
                    if not created:
                        return
                self._append_event_locked(
                    root_fd,
                    record,
                    io=io,
                )
            finally:
                io.close_fd(root_fd)

    def transition(
        self,
        record_kind: str,
        record_key: str,
        expected: SessionLease | ManagedProcess | SignalIntent | None,
        updated: SessionLease | ManagedProcess | SignalIntent,
        event: dict[str, object],
        *,
        expected_revision: int | None = None,
        expected_sessions_digest: str | None = None,
        expected_root_binding: RootBinding | None = None,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        before_effect: Callable[[], None] | None = None,
        effect: Callable[[], None] | None = None,
    ) -> int:
        """Commit one exact raw-state mutation and its logical event receipt."""
        io = self._operation_gateway(deadline, monotonic)
        budget = io.budget
        budget.check()
        self._require_mutable()
        root_token = (
            None if expected_root_binding is None else expected_root_binding.root_token
        )
        with self._locked_with_io(
            io=io,
            expected_root_token=root_token,
        ):
            self._recover_before_write_locked(io=io)
            budget.check()
            self._validate_transition_authority_locked(
                record_kind,
                record_key,
                expected,
                expected_revision,
                expected_sessions_digest,
                expected_root_binding,
                io=io,
            )
            if isinstance(updated, SignalIntent):
                existing_intent = (
                    expected if isinstance(expected, SignalIntent) else None
                )
                if self._merge_signal_intent(existing_intent, updated) != updated:
                    raise UnsafeStatePath("signal intent transition is not monotonic")
            event_id, journal = self._build_transition_journal(
                record_kind,
                record_key,
                expected,
                updated,
                event,
                io=io,
            )
            budget.check()
            self._write_transition_journal_locked(
                event_id,
                journal,
                io=io,
            )
            budget.check()
            self._validate_transition_authority_locked(
                record_kind,
                record_key,
                expected,
                expected_revision,
                expected_sessions_digest,
                expected_root_binding,
                io=io,
            )
            effect_completed = False
            try:
                if before_effect is not None:
                    before_effect()
                    budget.check()
                if effect is not None:
                    self._effect_transition_ids.add(event_id)
                    try:
                        if expected_root_binding is not None:
                            self.validate_root_binding(expected_root_binding)
                        effect()
                        effect_completed = True
                    finally:
                        self._effect_transition_ids.discard(event_id)
            except Exception as error:
                if isinstance(error, OperationDeadlineExceeded):
                    raise
                if budget.expired():
                    raise
                self._recover_known_transition_locked(
                    event_id,
                    journal,
                    io=io,
                )
                raise
            if effect_completed:
                try:
                    budget.check()
                except OperationDeadlineExceeded as error:
                    raise PostEffectStateError(
                        "effect completed after operation deadline",
                        record_persisted=False,
                    ) from error
            try:
                revision = self._write_transition_record_locked(
                    record_kind,
                    record_key,
                    updated,
                    io=io,
                )
            except OperationDeadlineExceeded as error:
                if effect_completed:
                    raise PostEffectStateError(
                        "effect completed before state persistence deadline",
                        record_persisted=False,
                    ) from error
                raise
            except Exception:
                recovery_failed = False
                try:
                    self._recover_known_transition_locked(
                        event_id,
                        journal,
                        io=io,
                    )
                except Exception:
                    recovery_failed = True
                if (
                    not recovery_failed
                    and self._transition_record_locked(record_kind, record_key, io=io)
                    == updated
                ):
                    if effect_completed:
                        raise PostEffectStateError(
                            "effect completed before state write reported failure",
                            record_persisted=True,
                        )
                    return self._ledger_revision_locked(io=io)
                if effect_completed:
                    raise PostEffectStateError(
                        "effect completed before state persistence failed",
                        record_persisted=False,
                    )
                raise
            try:
                self._mark_transition_committed_locked(
                    event_id,
                    io=io,
                )
            except Exception as error:
                if isinstance(error, OperationDeadlineExceeded):
                    if effect_completed:
                        raise PostEffectStateError(
                            "effect completed before journal commit deadline",
                            record_persisted=True,
                        ) from error
                    raise
                recovery_failed = False
                try:
                    self._recover_known_transition_locked(
                        event_id,
                        journal,
                        io=io,
                    )
                except Exception:
                    recovery_failed = True
                record_persisted = False
                try:
                    record_persisted = (
                        self._transition_record_locked(record_kind, record_key, io=io)
                        == updated
                    )
                except Exception:
                    recovery_failed = True
                if effect_completed:
                    raise PostEffectStateError(
                        "effect completed before journal commit failed",
                        record_persisted=record_persisted,
                    ) from error
                if not recovery_failed and record_persisted:
                    return self._ledger_revision_locked(io=io)
                raise
            try:
                self._recover_known_transition_locked(
                    event_id,
                    journal,
                    io=io,
                )
            except Exception as error:
                if effect_completed:
                    raise PostEffectStateError(
                        "effect completed before event receipt materialization failed",
                        record_persisted=True,
                    ) from error
                if not isinstance(error, OSError):
                    raise
            return revision

    def _validate_transition_authority_locked(
        self,
        record_kind: str,
        record_key: str,
        expected: SessionLease | ManagedProcess | SignalIntent | None,
        expected_revision: int | None,
        expected_sessions_digest: str | None,
        expected_root_binding: RootBinding | None,
        *,
        io: DeadlineIO,
    ) -> None:
        if expected_root_binding is not None:
            self._validate_root_binding_with_io(expected_root_binding, io=io)
        if (
            expected_revision is not None
            and self._ledger_revision_locked(io=io) != expected_revision
        ):
            raise UnsafeStatePath("authorized ledger revision changed")
        if expected_sessions_digest is not None and (
            self._sessions_digest_with_io(io=io) != expected_sessions_digest
        ):
            raise UnsafeStatePath("authorized session set changed")
        current = self._transition_record_locked(
            record_kind,
            record_key,
            io=io,
        )
        if current != expected:
            raise UnsafeStatePath("transition raw state changed")

    def _build_transition_journal(
        self,
        record_kind: str,
        record_key: str,
        expected: SessionLease | ManagedProcess | SignalIntent | None,
        updated: SessionLease | ManagedProcess | SignalIntent,
        event: dict[str, object],
        *,
        io: DeadlineIO,
    ) -> tuple[str, dict[str, object]]:
        io.budget.check()
        if record_kind not in {
            "sessions",
            "processes",
            "signal-intents",
            "force-receipts",
        }:
            raise ValueError("invalid transition record kind")
        event_payload = dict(event)
        event_payload.pop("event_id", None)
        expected_payload = None if expected is None else expected.to_dict()
        updated_payload = updated.to_dict()
        io.budget.check()
        expected_digest = hashlib.sha256(_canonical_json(expected_payload)).hexdigest()
        io.budget.check()
        updated_digest = hashlib.sha256(_canonical_json(updated_payload)).hexdigest()
        io.budget.check()
        if expected_digest == updated_digest:
            raise ValueError("transition must change raw state")
        event_id = derive_transition_id(
            record_kind,
            record_key,
            expected_digest,
            updated_digest,
            event_payload,
        )
        io.budget.check()
        event_payload["event_id"] = event_id
        return event_id, {
            "schema_version": 1,
            "phase": "prepared",
            "record_kind": record_kind,
            "record_key": record_key,
            "expected_digest": expected_digest,
            "updated_digest": updated_digest,
            "event_id": event_id,
            "event": event_payload,
        }

    def _write_transition_record_locked(
        self,
        record_kind: str,
        record_key: str,
        updated: SessionLease | ManagedProcess | SignalIntent,
        *,
        io: DeadlineIO,
    ) -> int:
        if record_kind == "sessions" and isinstance(updated, SessionLease):
            if session_key(updated.session_id) != record_key:
                raise ValueError("session transition key mismatch")
        elif record_kind == "processes" and isinstance(updated, ManagedProcess):
            if updated.wrapper.stable_key() != record_key:
                raise ValueError("process transition key mismatch")
        elif record_kind == "signal-intents" and isinstance(updated, SignalIntent):
            if updated.action != "term" or updated.process_key != record_key:
                raise ValueError("TERM transition key mismatch")
        elif record_kind == "force-receipts" and isinstance(updated, SignalIntent):
            if updated.action != "force" or updated.process_key != record_key:
                raise ValueError("force transition key mismatch")
        else:
            raise ValueError("transition record type mismatch")
        return self._save_record_locked(
            record_kind,
            record_key,
            updated.to_dict(),
            maintenance=False,
            io=io,
        )

    def _transition_record_locked(
        self,
        kind: str,
        key: str,
        *,
        io: DeadlineIO,
    ) -> SessionLease | ManagedProcess | SignalIntent | None:
        if kind == "sessions":
            records = self._load_records_locked_or_read_only(
                "sessions",
                SessionLease.from_dict,
                lambda value: session_key(value.session_id),
                io=io,
            )
            return next(
                (item for item in records if session_key(item.session_id) == key),
                None,
            )
        if kind == "processes":
            return self._load_exact_record_locked_or_read_only(
                "processes",
                key,
                ManagedProcess.from_dict,
                lambda item: item.wrapper.stable_key(),
                io=io,
            )
        if kind == "signal-intents":
            return self._load_exact_record_locked_or_read_only(
                "signal-intents",
                key,
                SignalIntent.from_dict,
                lambda item: item.process_key,
                io=io,
            )
        if kind == "force-receipts":
            return self._load_exact_record_locked_or_read_only(
                "force-receipts",
                key,
                SignalIntent.from_dict,
                lambda item: item.process_key,
                io=io,
            )
        raise UnsafeStatePath("invalid journal record kind")

    def _write_transition_journal_locked(
        self,
        event_id: str,
        journal: dict[str, object],
        *,
        io: DeadlineIO,
    ) -> None:
        if not self._owns_lock():
            raise RuntimeError("transition journal write requires the state lock")
        io.budget.check()
        root_fd = self._open_root(io=io)
        try:
            directory_fd = self._open_directory(
                root_fd, "event-journal", create=True, io=io
            )
            assert directory_fd is not None
            try:
                names = _directory_names_with_io(
                    io,
                    directory_fd,
                    self.root / "event-journal",
                    TRANSITION_JOURNAL_LIMIT,
                )
                if (
                    len(names) >= TRANSITION_JOURNAL_LIMIT
                    and (event_id + ".json") not in names
                ):
                    raise StateCorruption(
                        self.root / "event-journal",
                        hashlib.sha256(b"journal_capacity").hexdigest(),
                    )
                io.budget.check()
                self._atomic_json(
                    directory_fd,
                    self.root / "event-journal",
                    event_id + ".json",
                    journal,
                    io=io,
                )
            finally:
                io.close_fd(directory_fd)
        finally:
            io.close_fd(root_fd)

    def _mark_transition_committed_locked(
        self,
        event_id: str,
        *,
        io: DeadlineIO,
    ) -> None:
        journal = self._load_journal_locked(event_id, io=io)
        if journal is None:
            return
        journal["phase"] = "committed"
        root_fd = self._open_root(io=io)
        try:
            directory_fd = self._open_directory(
                root_fd, "event-journal", create=False, io=io
            )
            assert directory_fd is not None
            try:
                self._atomic_json(
                    directory_fd,
                    self.root / "event-journal",
                    event_id + ".json",
                    journal,
                    io=io,
                )
            finally:
                io.close_fd(directory_fd)
        finally:
            io.close_fd(root_fd)

    def recover_transition_events(
        self,
        limit: int = 64,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(limit) is not int or limit < 0 or limit > TRANSITION_JOURNAL_LIMIT:
            raise ValueError("invalid transition recovery limit")
        io = self._operation_gateway(deadline, monotonic)
        io.budget.check()
        self._require_mutable()
        with self._locked_with_io(io=io):
            self._recover_transition_events_locked(
                limit=limit,
                io=io,
            )

    def _recover_before_write_locked(self, *, io: DeadlineIO) -> None:
        self._reconcile_atomic_temps_locked(io=io)
        self._recover_transition_events_locked(
            limit=TRANSITION_JOURNAL_LIMIT,
            io=io,
        )
        self._prune_event_receipts_locked(io=io)
        self._recover_legacy_outbox_locked(
            LEGACY_OUTBOX_DRAIN_LIMIT,
            io=io,
        )
        self._prune_event_receipts_locked(io=io)

    def _reconcile_atomic_temps_locked(self, *, io: DeadlineIO) -> None:
        if not self._owns_lock():
            raise RuntimeError("atomic temp reconciliation requires the state lock")
        root_fd = self._open_root(io=io)
        opened: list[int] = []
        try:
            locations: list[tuple[str, int, Path, int]] = [
                ("", root_fd, self.root, STATE_DIRECTORY_MAX_ENTRIES)
            ]
            for directory_name in _ATOMIC_JSON_DIRECTORIES:
                directory_fd = self._open_directory(
                    root_fd,
                    directory_name,
                    create=False,
                    io=io,
                )
                if directory_fd is None:
                    continue
                opened.append(directory_fd)
                limit = (
                    TRANSITION_JOURNAL_LIMIT
                    if directory_name == "event-journal"
                    else STATE_DIRECTORY_MAX_ENTRIES
                )
                locations.append(
                    (
                        directory_name,
                        directory_fd,
                        self.root / directory_name,
                        limit,
                    )
                )

            candidates: list[tuple[str, str, int, Path]] = []
            malformed_path: Path | None = None
            for directory_name, directory_fd, path, limit in locations:
                names = _directory_names_with_io(io, directory_fd, path, limit)
                for name in names:
                    if not name.startswith(".tmp-"):
                        continue
                    if _ATOMIC_TEMP_PATTERN.fullmatch(name) is None:
                        malformed_path = path
                        break
                    candidates.append((directory_name, name, directory_fd, path))
                if malformed_path is not None:
                    break
            if malformed_path is not None:
                raise StateCorruption(
                    malformed_path,
                    hashlib.sha256(b"invalid_atomic_temp").hexdigest(),
                )

            validated: list[tuple[str, int, Path]] = []
            for _directory_name, name, directory_fd, path in sorted(candidates)[
                :_ATOMIC_TEMP_RECONCILE_LIMIT
            ]:
                try:
                    value = io.stat(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISREG(value.st_mode)
                    or value.st_uid != os.getuid()
                    or _mode(value) != _FILE_MODE
                    or value.st_nlink != 1
                ):
                    raise StateCorruption(
                        path,
                        hashlib.sha256(b"unsafe_atomic_temp").hexdigest(),
                    )
                validated.append((name, directory_fd, path))

            changed: set[int] = set()
            for name, directory_fd, _path in validated:
                try:
                    io.unlink(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    continue
                changed.add(directory_fd)
            for directory_fd in sorted(changed):
                io.fsync(directory_fd)
        finally:
            for directory_fd in reversed(opened):
                io.close_fd(directory_fd)
            io.close_fd(root_fd)

    def _recover_transition_events_locked(
        self,
        *,
        limit: int,
        io: DeadlineIO,
    ) -> None:
        io.budget.check()
        root_fd = self._open_root(io=io)
        try:
            directory_fd = self._open_directory(
                root_fd, "event-journal", create=False, io=io
            )
            if directory_fd is None:
                return
            try:
                names = _directory_names_with_io(
                    io,
                    directory_fd,
                    self.root / "event-journal",
                    TRANSITION_JOURNAL_LIMIT,
                )
                for name in names[:limit]:
                    io.budget.check()
                    if not name.endswith(".json") or not _is_hex_digest(name[:-5]):
                        raise UnsafeStatePath("invalid event journal entry")
                    event_id = name[:-5]
                    if event_id in self._effect_transition_ids:
                        continue
                    journal = self._load_journal_locked(event_id, io=io)
                    if journal is None:
                        continue
                    self._recover_one_transition_locked(
                        root_fd,
                        directory_fd,
                        event_id,
                        journal,
                        io=io,
                    )
                    io.budget.check()
            finally:
                io.close_fd(directory_fd)
        finally:
            io.close_fd(root_fd)

    def _recover_one_transition_locked(
        self,
        root_fd: int,
        directory_fd: int,
        event_id: str,
        journal: dict[str, object],
        *,
        io: DeadlineIO,
    ) -> bool:
        """Recover one known journal without enumerating unrelated state."""
        io.budget.check()
        current_digest = self._transition_record_digest_locked(
            journal["record_kind"],
            journal["record_key"],
            io=io,
        )
        io.budget.check()
        has_receipt = self._event_receipt_exists_locked(
            root_fd,
            event_id,
            journal["event"],
            io=io,
        )
        try:
            decision = decide_recovery(
                RecoveryEvidence(
                    phase=journal["phase"],
                    current_digest=current_digest,
                    expected_digest=journal["expected_digest"],
                    updated_digest=journal["updated_digest"],
                    has_matching_receipt=has_receipt,
                )
            )
        except RecoveryContradiction as error:
            raise StateCorruption(
                self.root / "event-journal" / (event_id + ".json"),
                hashlib.sha256(_canonical_json(journal)).hexdigest(),
            ) from error
        if decision is RecoveryDecision.FINALIZE_UPDATED:
            created = self._write_event_receipt_locked(
                root_fd,
                event_id,
                journal["event"],
                self._ledger_revision_locked(io=io),
                io=io,
            )
            if created:
                self._append_event_locked(
                    root_fd,
                    _canonical_json(journal["event"]),
                    io=io,
                )
            self._prune_event_receipts_locked(io=io)
        elif decision is RecoveryDecision.ALREADY_RECEIPTED:
            created = False
        elif decision is RecoveryDecision.DISCARD_PREPARED:
            created = False
        io.unlink(event_id + ".json", dir_fd=directory_fd)
        io.fsync(directory_fd)
        return decision is not RecoveryDecision.DISCARD_PREPARED

    def _recover_known_transition_locked(
        self,
        event_id: str,
        journal: dict[str, object],
        *,
        io: DeadlineIO,
    ) -> bool:
        """Finish the active transition without a post-deadline journal scan."""
        root_fd = self._open_root(io=io)
        try:
            directory_fd = self._open_directory(
                root_fd, "event-journal", create=False, io=io
            )
            if directory_fd is None:
                raise StateCorruption(
                    self.root / "event-journal",
                    hashlib.sha256(b"missing_active_journal").hexdigest(),
                )
            try:
                return self._recover_one_transition_locked(
                    root_fd,
                    directory_fd,
                    event_id,
                    journal,
                    io=io,
                )
            finally:
                io.close_fd(directory_fd)
        finally:
            io.close_fd(root_fd)

    def _load_journal_locked(
        self,
        event_id: str,
        *,
        io: DeadlineIO,
    ) -> dict[str, object] | None:
        io.budget.check()
        root_fd = self._open_root(io=io)
        try:
            directory_fd = self._open_directory(
                root_fd, "event-journal", create=False, io=io
            )
            if directory_fd is None:
                return None
            try:
                try:
                    fd = self._open_private_file(
                        directory_fd,
                        event_id + ".json",
                        self.root / "event-journal" / (event_id + ".json"),
                        io=io,
                    )
                except FileNotFoundError:
                    return None
                try:
                    raw = _read_state_record_with_io(io, fd)
                finally:
                    io.close_fd(fd)
            finally:
                io.close_fd(directory_fd)
        finally:
            io.close_fd(root_fd)
        try:
            payload = json.loads(raw.decode("utf-8"))
            _validate_json_resources(payload)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as error:
            raise StateCorruption(
                self.root / "event-journal" / (event_id + ".json"),
                hashlib.sha256(raw).hexdigest(),
            ) from error
        required = {
            "schema_version",
            "phase",
            "record_kind",
            "record_key",
            "expected_digest",
            "updated_digest",
            "event_id",
            "event",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or payload["schema_version"] != 1
            or payload["phase"] not in {"prepared", "committed"}
            or payload["event_id"] != event_id
            or payload["record_kind"]
            not in {"sessions", "processes", "signal-intents", "force-receipts"}
            or not _is_hex_digest(payload["record_key"])
            or not _is_hex_digest(payload["expected_digest"])
            or not _is_hex_digest(payload["updated_digest"])
            or not isinstance(payload["event"], dict)
            or payload["event"].get("event_id") != event_id
        ):
            raise StateCorruption(
                self.root / "event-journal" / (event_id + ".json"),
                hashlib.sha256(raw).hexdigest(),
            )
        event_without_id = dict(payload["event"])
        event_without_id.pop("event_id", None)
        derived = derive_transition_id(
            payload["record_kind"],
            payload["record_key"],
            payload["expected_digest"],
            payload["updated_digest"],
            event_without_id,
        )
        if derived != event_id:
            raise StateCorruption(
                self.root / "event-journal" / (event_id + ".json"),
                hashlib.sha256(raw).hexdigest(),
            )
        return payload

    def _transition_record_digest_locked(
        self,
        kind: str,
        key: str,
        *,
        io: DeadlineIO,
    ) -> str:
        io.budget.check()
        current = self._transition_record_locked(kind, key, io=io)
        payload = None if current is None else current.to_dict()
        io.budget.check()
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def _load_event_receipt_locked(
        self,
        root_fd: int,
        event_id: str,
        *,
        io: DeadlineIO,
    ) -> dict[str, object] | None:
        io.budget.check()
        if not _is_hex_digest(event_id):
            raise UnsafeStatePath("invalid event receipt ID")
        directory_fd = self._open_directory(
            root_fd, "event-receipts", create=False, io=io
        )
        if directory_fd is None:
            return None
        try:
            try:
                fd = self._open_private_file(
                    directory_fd,
                    event_id + ".json",
                    self.root / "event-receipts" / (event_id + ".json"),
                    io=io,
                )
            except FileNotFoundError:
                return None
            try:
                raw = _read_state_record_with_io(io, fd)
            finally:
                io.close_fd(fd)
        finally:
            io.close_fd(directory_fd)
        try:
            payload = json.loads(raw.decode("utf-8"))
            _validate_json_resources(payload)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as error:
            raise StateCorruption(
                self.root / "event-receipts" / (event_id + ".json"),
                hashlib.sha256(raw).hexdigest(),
            ) from error
        required = {
            "schema_version",
            "transition_id",
            "event_id",
            "committed_revision",
            "event",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or payload["schema_version"] != 1
            or payload["transition_id"] != event_id
            or payload["event_id"] != event_id
            or type(payload["committed_revision"]) is not int
            or payload["committed_revision"] < 0
            or not isinstance(payload["event"], dict)
            or payload["event"].get("event_id") != event_id
        ):
            raise StateCorruption(
                self.root / "event-receipts" / (event_id + ".json"),
                hashlib.sha256(raw).hexdigest(),
            )
        return payload

    def _event_receipt_exists_locked(
        self,
        root_fd: int,
        event_id: str,
        expected_event: object | None = None,
        *,
        io: DeadlineIO,
    ) -> bool:
        receipt = self._load_event_receipt_locked(
            root_fd,
            event_id,
            io=io,
        )
        if receipt is None:
            return False
        if expected_event is not None and receipt["event"] != expected_event:
            raise StateCorruption(
                self.root / "event-receipts" / (event_id + ".json"),
                hashlib.sha256(_canonical_json(receipt)).hexdigest(),
            )
        return True

    def _write_event_receipt_locked(
        self,
        root_fd: int,
        event_id: str,
        event: object,
        committed_revision: int,
        *,
        io: DeadlineIO,
    ) -> bool:
        io.budget.check()
        if not _is_hex_digest(event_id) or not isinstance(event, dict):
            raise StateCorruption(
                self.root / "event-receipts",
                hashlib.sha256(b"invalid_event_receipt").hexdigest(),
            )
        if event.get("event_id") != event_id:
            raise StateCorruption(
                self.root / "event-receipts" / (event_id + ".json"),
                hashlib.sha256(_canonical_json(event)).hexdigest(),
            )
        current = self._load_event_receipt_locked(
            root_fd,
            event_id,
            io=io,
        )
        if current is not None:
            if current["event"] != event:
                raise StateCorruption(
                    self.root / "event-receipts" / (event_id + ".json"),
                    hashlib.sha256(_canonical_json(current)).hexdigest(),
                )
            return False
        self._prune_event_receipts_locked(io=io, reserve=1)
        io.budget.check()
        directory_fd = self._open_directory(
            root_fd, "event-receipts", create=True, io=io
        )
        assert directory_fd is not None
        try:
            receipt = {
                "schema_version": 1,
                "transition_id": event_id,
                "event_id": event_id,
                "committed_revision": committed_revision,
                "event": event,
            }
            self._atomic_json(
                directory_fd,
                self.root / "event-receipts",
                event_id + ".json",
                receipt,
                io=io,
            )
            io.budget.check()
            return True
        finally:
            io.close_fd(directory_fd)

    def _recover_legacy_outbox_locked(
        self,
        limit: int,
        *,
        io: DeadlineIO,
    ) -> None:
        io.budget.check()
        root_fd = self._open_root(io=io)
        try:
            directory_fd = self._open_directory(root_fd, "outbox", create=False, io=io)
            if directory_fd is None:
                return
            try:
                names = _directory_names_with_io(
                    io,
                    directory_fd,
                    self.root / "outbox",
                    LEGACY_OUTBOX_CAPACITY,
                )
                for name in names[:limit]:
                    io.budget.check()
                    if not name.endswith(".json"):
                        raise UnsafeStatePath("invalid outbox entry")
                    event_id = name[:-5]
                    if not _is_hex_digest(event_id):
                        raise UnsafeStatePath("invalid outbox event ID")
                    fd = self._open_private_file(
                        directory_fd,
                        name,
                        self.root / "outbox" / name,
                        io=io,
                    )
                    try:
                        raw = _read_state_record_with_io(io, fd)
                    finally:
                        io.close_fd(fd)
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                        _validate_json_resources(payload)
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        ValueError,
                        TypeError,
                        OverflowError,
                        RecursionError,
                    ) as error:
                        raise StateCorruption(
                            self.root / "outbox" / name,
                            hashlib.sha256(raw).hexdigest(),
                        ) from error
                    if (
                        not isinstance(payload, dict)
                        or payload.get("event_id") != event_id
                    ):
                        raise StateCorruption(
                            self.root / "outbox" / name,
                            hashlib.sha256(raw).hexdigest(),
                        )
                    created = self._write_event_receipt_locked(
                        root_fd,
                        event_id,
                        payload,
                        self._ledger_revision_locked(io=io),
                        io=io,
                    )
                    if created:
                        self._append_event_locked(
                            root_fd,
                            _canonical_json(payload),
                            io=io,
                        )
                    io.unlink(name, dir_fd=directory_fd)
                    io.fsync(directory_fd)
            finally:
                io.close_fd(directory_fd)
        finally:
            io.close_fd(root_fd)

    def _append_event_locked(
        self,
        root_fd: int,
        record: bytes,
        *,
        io: DeadlineIO,
    ) -> None:
        io.budget.check()
        name = "events.jsonl"
        path = self.root / name
        size = 0
        try:
            current_fd = self._open_private_file(root_fd, name, path, io=io)
        except FileNotFoundError:
            current_fd = None
        if current_fd is not None:
            try:
                size = io.fstat(current_fd).st_size
            finally:
                io.close_fd(current_fd)
        io.budget.check()
        if size and size + len(record) > EVENT_LOG_MAX_BYTES:
            self._rotate_events_locked(root_fd, io=io)
            io.budget.check()
        try:
            fd = self._open_private_file(
                root_fd,
                name,
                path,
                os.O_WRONLY | os.O_APPEND,
                io=io,
            )
            created = False
        except FileNotFoundError:
            flags = os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK
            flags |= os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = io.open_fd(name, flags, _FILE_MODE, dir_fd=root_fd)
            created = True
        try:
            if created:
                io.fchmod(fd, _FILE_MODE)
            value = io.fstat(fd)
            _validate_file(value, path)
            _write_all_with_io(io, fd, record)
            io.fsync(fd)
        except Exception:
            _close_fd_after_failure(io, fd)
            raise
        else:
            io.close_fd(fd)
        io.fsync(root_fd)

    def _validate_named_file_if_present(
        self,
        directory_fd: int,
        name: str,
        *,
        io: DeadlineIO,
    ) -> bool:
        try:
            fd = self._open_private_file(directory_fd, name, self.root / name, io=io)
        except FileNotFoundError:
            return False
        io.close_fd(fd)
        io.budget.check()
        return True

    def _rotate_events_locked(
        self,
        root_fd: int,
        *,
        io: DeadlineIO,
    ) -> None:
        io.budget.check()
        oldest = f"events.jsonl.{EVENT_LOG_BACKUPS}"
        if self._validate_named_file_if_present(
            root_fd,
            oldest,
            io=io,
        ):
            io.unlink(oldest, dir_fd=root_fd)
        for number in range(EVENT_LOG_BACKUPS - 1, 0, -1):
            io.budget.check()
            source = f"events.jsonl.{number}"
            destination = f"events.jsonl.{number + 1}"
            if not self._validate_named_file_if_present(
                root_fd,
                source,
                io=io,
            ):
                continue
            self._validate_named_file_if_present(
                root_fd,
                destination,
                io=io,
            )
            io.replace(
                source,
                destination,
                source_dir_fd=root_fd,
                destination_dir_fd=root_fd,
            )
        if self._validate_named_file_if_present(
            root_fd,
            "events.jsonl",
            io=io,
        ):
            self._validate_named_file_if_present(
                root_fd,
                "events.jsonl.1",
                io=io,
            )
            io.replace(
                "events.jsonl",
                "events.jsonl.1",
                source_dir_fd=root_fd,
                destination_dir_fd=root_fd,
            )
        io.fsync(root_fd)

    def _prune_event_backups_locked(self, *, io: DeadlineIO) -> None:
        if not self._root_exists(io=io):
            return
        root_fd = self._open_root(io=io)
        cutoff = time.time() - EVENT_LOG_RETENTION_SECONDS
        try:
            candidates: list[tuple[str, os.stat_result]] = []
            names = _directory_names_with_io(
                io, root_fd, self.root, STATE_DIRECTORY_MAX_ENTRIES
            )
            for name in names:
                if not name.startswith("events.jsonl."):
                    continue
                suffix = name.removeprefix("events.jsonl.")
                if not suffix.isdigit():
                    continue
                fd = self._open_private_file(root_fd, name, self.root / name, io=io)
                try:
                    value = io.fstat(fd)
                finally:
                    io.close_fd(fd)
                candidates.append((name, value))
            canonical = set(EVENT_LOG_BACKUP_FILENAMES)
            changed = False
            for name, value in candidates:
                if name not in canonical or value.st_mtime < cutoff:
                    io.unlink(name, dir_fd=root_fd)
                    changed = True
            if changed:
                io.fsync(root_fd)
        finally:
            io.close_fd(root_fd)

    def _prune_event_receipts_locked(
        self,
        *,
        io: DeadlineIO,
        reserve: int = 0,
    ) -> None:
        if reserve not in {0, 1}:
            raise ValueError("invalid event receipt reservation")
        io.budget.check()
        exists = self._root_exists(io=io)
        io.budget.check()
        if not exists:
            return
        root_fd = self._open_root(io=io)
        try:
            receipt_fd = self._open_directory(
                root_fd, "event-receipts", create=False, io=io
            )
            if receipt_fd is None:
                return
            journal_fd = self._open_directory(
                root_fd, "event-journal", create=False, io=io
            )
            try:
                protected: set[str] = set()
                if journal_fd is not None:
                    journal_names = _directory_names_with_io(
                        io,
                        journal_fd,
                        self.root / "event-journal",
                        TRANSITION_JOURNAL_LIMIT,
                    )
                    for name in journal_names:
                        if not name.endswith(".json") or not _is_hex_digest(name[:-5]):
                            raise UnsafeStatePath("invalid event journal entry")
                        protected.add(name[:-5])
                receipt_names = _directory_names_with_io(
                    io,
                    receipt_fd,
                    self.root / "event-receipts",
                    STATE_DIRECTORY_MAX_ENTRIES,
                )
                candidates: list[tuple[int, str]] = []
                for name in receipt_names:
                    io.budget.check()
                    if not name.endswith(".json") or not _is_hex_digest(name[:-5]):
                        raise UnsafeStatePath("invalid event receipt entry")
                    receipt = self._load_event_receipt_locked(
                        root_fd,
                        name[:-5],
                        io=io,
                    )
                    if receipt is None:
                        raise StateCorruption(
                            self.root / "event-receipts" / name,
                            hashlib.sha256(b"missing_event_receipt").hexdigest(),
                        )
                    fd = self._open_private_file(
                        receipt_fd,
                        name,
                        self.root / "event-receipts" / name,
                        io=io,
                    )
                    try:
                        value = io.fstat(fd)
                        candidates.append((value.st_mtime_ns, name))
                    finally:
                        io.close_fd(fd)
                    io.budget.check()
                excess = max(
                    0,
                    len(candidates) + reserve - EVENT_RECEIPT_RETENTION,
                )
                changed = False
                for _mtime, name in sorted(candidates):
                    io.budget.check()
                    if excess == 0:
                        break
                    if name[:-5] in protected:
                        continue
                    io.unlink(name, dir_fd=receipt_fd)
                    excess -= 1
                    changed = True
                if excess:
                    raise StateCorruption(
                        self.root / "event-receipts",
                        hashlib.sha256(b"receipt_capacity").hexdigest(),
                    )
                if changed:
                    io.fsync(receipt_fd)
            finally:
                if journal_fd is not None:
                    io.close_fd(journal_fd)
                io.close_fd(receipt_fd)
        finally:
            io.close_fd(root_fd)

    def _install_transaction_reference(
        self, root_fd: int, *, io: DeadlineIO
    ) -> str | None:
        def read_private(name: str) -> tuple[bytes, str] | None:
            try:
                fd = self._open_private_file(root_fd, name, self.root / name, io=io)
            except FileNotFoundError:
                return None
            try:
                raw = _read_all_with_io(io, fd)
            finally:
                io.close_fd(fd)
            return raw, hashlib.sha256(raw).hexdigest()

        for alias in INSTALL_STATE_LEGACY_FILENAMES:
            legacy = read_private(alias)
            if legacy is not None:
                _, digest = legacy
                raise StateCorruption(self.root / alias, digest)

        canonical = read_private(INSTALL_STATE_FILENAME)
        if canonical is None:
            return None
        raw, digest = canonical
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise StateCorruption(self.root / INSTALL_STATE_FILENAME, digest) from None
        if not isinstance(value, dict) or set(value) != INSTALL_STATE_FIELDS:
            raise StateCorruption(self.root / INSTALL_STATE_FILENAME, digest)
        reference = value[INSTALL_STATE_TRANSACTION_FIELD]
        if not isinstance(reference, str) or not reference:
            raise StateCorruption(self.root / INSTALL_STATE_FILENAME, digest)
        return reference

    def _prune_transactions_locked(self, *, io: DeadlineIO) -> None:
        if not self._root_exists(io=io):
            return
        root_fd = self._open_root(io=io)
        try:
            reference = self._install_transaction_reference(root_fd, io=io)
            transactions_fd = self._open_directory(
                root_fd, "transactions", create=False, io=io
            )
            if transactions_fd is None:
                return
            try:
                candidates: list[tuple[int, str]] = []
                names = _directory_names_with_io(
                    io,
                    transactions_fd,
                    self.root / "transactions",
                    STATE_DIRECTORY_MAX_ENTRIES,
                )
                for name in names:
                    value = io.stat(name, dir_fd=transactions_fd)
                    _validate_directory(value, self.root / "transactions" / name)
                    candidates.append((value.st_mtime_ns, name))
                if len(candidates) <= TRANSACTION_RETENTION:
                    return
                candidates.sort(reverse=True)
                keep: set[str] = set()
                if reference is not None and any(
                    name == reference for _, name in candidates
                ):
                    keep.add(reference)
                for _, name in candidates:
                    if len(keep) >= TRANSACTION_RETENTION:
                        break
                    keep.add(name)
                for _, name in candidates:
                    if name not in keep:
                        self._remove_private_tree(
                            transactions_fd,
                            name,
                            self.root / "transactions" / name,
                            io=io,
                        )
                io.fsync(transactions_fd)
            finally:
                io.close_fd(transactions_fd)
        finally:
            io.close_fd(root_fd)

    def _remove_private_tree(
        self,
        parent_fd: int,
        name: str,
        path: Path,
        *,
        io: DeadlineIO,
    ) -> None:
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = io.open_fd(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise UnsafeStatePath(f"cannot safely prune transaction: {path}") from error
        try:
            _validate_directory(io.fstat(directory_fd), path)
            children = _directory_names_with_io(
                io, directory_fd, path, STATE_DIRECTORY_MAX_ENTRIES
            )
            for child in children:
                child_path = path / child
                value = io.stat(child, dir_fd=directory_fd)
                if stat.S_ISDIR(value.st_mode):
                    _validate_directory(value, child_path)
                    self._remove_private_tree(directory_fd, child, child_path, io=io)
                else:
                    _validate_file(value, child_path)
                    io.unlink(child, dir_fd=directory_fd)
            io.fsync(directory_fd)
        finally:
            io.close_fd(directory_fd)
        io.budget.check()
        os.rmdir(name, dir_fd=parent_fd)
        io.budget.check()
