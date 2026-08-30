from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, replace
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Callable, Iterator, TypeVar

from . import model
from .model import ManagedProcess, SessionLease, SignalIntent


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
_Record = TypeVar("_Record", SessionLease, ManagedProcess, SignalIntent)


class UnsafeStatePath(RuntimeError):
    """A state path does not satisfy the private-file trust boundary."""


class StateLockTimeout(TimeoutError):
    """The state flock could not be acquired before its deadline."""


class OperationDeadlineExceeded(RuntimeError):
    """The absolute deadline expired before the next bounded operation."""


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


def _deadline_check(
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise OperationDeadlineExceeded("operation deadline exhausted")


def _remaining_timeout(
    deadline: float | None,
    monotonic: Callable[[], float],
) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - monotonic())


def _bounded_directory_names(
    directory_fd: int,
    path: Path,
    limit: int,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[str, ...]:
    """Enumerate a private directory with item, time, and memory bounds."""
    _deadline_check(deadline, monotonic)
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        iterator = iter(entries)
        while True:
            _deadline_check(deadline, monotonic)
            try:
                entry = next(iterator)
            except StopIteration:
                break
            _deadline_check(deadline, monotonic)
            if len(names) >= limit:
                raise StateCorruption(
                    path,
                    hashlib.sha256(b"directory_capacity").hexdigest(),
                )
            names.append(entry.name)
    _deadline_check(deadline, monotonic)
    return tuple(sorted(names))


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


def _read_all(
    fd: int,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    max_bytes: int | None = None,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        _deadline_check(deadline, monotonic)
        chunk = os.read(fd, 131072)
        _deadline_check(deadline, monotonic)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise ValueError("file exceeds scan limit")
        chunks.append(chunk)


def _read_state_record(
    fd: int,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> bytes:
    chunks: list[bytes] = []
    remaining = STATE_RECORD_MAX_BYTES + 1
    while remaining:
        _deadline_check(deadline, monotonic)
        chunk = os.read(fd, min(131072, remaining))
        _deadline_check(deadline, monotonic)
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


def _write_all(
    fd: int,
    data: bytes,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    position = 0
    while position < len(data):
        _deadline_check(deadline, monotonic)
        written = os.write(fd, data[position:])
        _deadline_check(deadline, monotonic)
        if written <= 0:
            raise OSError(errno.EIO, "short state write")
        position += written


def _rename_noreplace(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
) -> None:
    """Use Linux renameat2(RENAME_NOREPLACE) without a replace-capable gap."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        1,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


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
        self._lock_depth = 0
        self._effect_transition_ids: set[str] = set()

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
            )

    def _root_exists(self) -> bool:
        try:
            fd = self._open_root()
        except FileNotFoundError:
            return False
        os.close(fd)
        return True

    def root_token(self) -> tuple[int, int]:
        """Return the device/inode identity of the current pinned state root."""
        fd = self._open_root()
        try:
            value = os.fstat(fd)
            return value.st_dev, value.st_ino
        finally:
            os.close(fd)

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

    def root_binding(self) -> RootBinding:
        """Capture the exact root and its immediate lexical parent/name binding."""
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

    def validate_root_binding(self, expected: RootBinding) -> None:
        if not isinstance(expected, RootBinding) or self.root_binding() != expected:
            raise UnsafeStatePath("state root lexical binding changed")

    def lexical_root_token(self) -> tuple[int, int]:
        """Return the inode currently bound to the configured root pathname."""
        value = os.stat(self.root, follow_symlinks=False)
        _validate_directory(value, self.root)
        return value.st_dev, value.st_ino

    def _walk_root(self, *, create: bool) -> int:
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        parts = self.root.parts
        if not self.root.is_absolute() or not parts:
            raise UnsafeStatePath("state root must be an absolute lexical path")
        try:
            current_fd = os.open(parts[0], flags)
        except OSError as error:
            raise UnsafeStatePath("cannot open filesystem root") from error
        walked = Path(parts[0])
        transferred = False
        try:
            for index, component in enumerate(parts[1:], start=1):
                final = index == len(parts) - 1
                walked /= component
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not final or not create:
                        raise
                    try:
                        os.mkdir(component, _DIRECTORY_MODE, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    else:
                        os.chmod(
                            component,
                            _DIRECTORY_MODE,
                            dir_fd=current_fd,
                            follow_symlinks=False,
                        )
                        os.fsync(current_fd)
                    try:
                        next_fd = os.open(component, flags, dir_fd=current_fd)
                    except OSError as error:
                        raise UnsafeStatePath(
                            f"cannot safely open state path component: {walked}"
                        ) from error
                except OSError as error:
                    raise UnsafeStatePath(
                        f"cannot safely open state path component: {walked}"
                    ) from error
                os.close(current_fd)
                current_fd = next_fd
            _validate_directory(os.fstat(current_fd), self.root)
            transferred = True
            return current_fd
        finally:
            if not transferred:
                os.close(current_fd)

    def _open_root(self, *, create: bool = False) -> int:
        owner = threading.get_ident()
        with self._lock_condition:
            if (
                self._lock_owner == owner
                and not self._lock_releasing
                and self._pinned_root_fd is not None
            ):
                fd = os.dup(self._pinned_root_fd)
                _validate_directory(os.fstat(fd), self.root)
                return fd
        return self._walk_root(create=create)

    def _open_private_file(
        self,
        directory_fd: int,
        name: str,
        path: Path,
        access_flags: int = os.O_RDONLY,
    ) -> int:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
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
            fd = os.open(name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise UnsafeStatePath(f"cannot safely open state file: {path}") from error
        try:
            after = os.fstat(fd)
            _validate_file(after, path)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise UnsafeStatePath(f"state file changed during open: {path}")
        except Exception:
            os.close(fd)
            raise
        return fd

    def _open_directory(self, root_fd: int, name: str, *, create: bool) -> int | None:
        path = self.root / name
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                return None
            if not self._owns_lock():
                raise RuntimeError("private directory creation requires the state lock")
            try:
                os.mkdir(name, _DIRECTORY_MODE, dir_fd=root_fd)
            except FileExistsError:
                pass
            else:
                os.chmod(
                    name,
                    _DIRECTORY_MODE,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            fd = os.open(name, flags, dir_fd=root_fd)
            os.fsync(root_fd)
        except OSError as error:
            raise UnsafeStatePath(
                f"cannot safely open state directory: {path}"
            ) from error
        try:
            _validate_directory(os.fstat(fd), path)
        except Exception:
            os.close(fd)
            raise
        return fd

    def _create_lock_file(self, root_fd: int) -> int:
        common = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(
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
            )
        else:
            os.fchmod(fd, _FILE_MODE)
            os.fsync(fd)
            os.fsync(root_fd)
        try:
            _validate_file(os.fstat(fd), self.root / "state.lock")
        except Exception:
            os.close(fd)
            raise
        return fd

    def _validate_lock_binding(self, lock_fd: int, root_fd: int) -> None:
        opened = os.fstat(lock_fd)
        _validate_file(opened, self.root / "state.lock")
        named_fd = self._open_private_file(
            root_fd, "state.lock", self.root / "state.lock"
        )
        try:
            named = os.fstat(named_fd)
        finally:
            os.close(named_fd)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise UnsafeStatePath("state.lock was replaced during lock acquisition")

    @contextmanager
    def locked(
        self,
        *,
        expected_root_token: tuple[int, int] | None = None,
        remaining_timeout: float | None = None,
    ) -> Iterator[StateStore]:
        self._require_mutable()
        owner = threading.get_ident()
        timeout = self.lock_timeout
        if remaining_timeout is not None:
            converted = float(remaining_timeout)
            if not math.isfinite(converted) or converted < 0:
                raise ValueError("remaining_timeout must be non-negative")
            timeout = min(timeout, converted)
        deadline = time.monotonic() + timeout
        nested = False
        with self._lock_condition:
            if self._lock_owner == owner:
                if self._lock_releasing:
                    raise RuntimeError("state lock is releasing")
                if self._pinned_root_fd is None or self._lock_depth < 1:
                    raise RuntimeError("invalid reentrant state-lock ownership")
                if expected_root_token is not None:
                    pinned = os.fstat(self._pinned_root_fd)
                    if (pinned.st_dev, pinned.st_ino) != expected_root_token:
                        raise UnsafeStatePath("state root identity changed")
                self._lock_depth += 1
                nested = True
            else:
                while self._lock_owner is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StateLockTimeout("timed out waiting for state-lock owner")
                    self._lock_condition.wait(remaining)
                self._lock_owner = owner
                self._lock_depth = 1
        if nested:
            try:
                yield self
            finally:
                with self._lock_condition:
                    if self._lock_owner != owner or self._lock_depth <= 1:
                        raise RuntimeError("invalid nested state-lock release")
                    self._lock_depth -= 1
            return
        root_fd: int | None = None
        lock_fd: int | None = None
        flocked = False
        try:
            root_fd = self._open_root(create=expected_root_token is None)
            if expected_root_token is not None:
                root_value = os.fstat(root_fd)
                if (root_value.st_dev, root_value.st_ino) != expected_root_token:
                    raise UnsafeStatePath("state root identity changed")
            lock_fd = self._create_lock_file(root_fd)
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    flocked = True
                    self._validate_lock_binding(lock_fd, root_fd)
                    break
                except BlockingIOError as error:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StateLockTimeout(
                            "timed out acquiring state.lock"
                        ) from error
                    time.sleep(min(0.01, remaining))
            with self._lock_condition:
                if self._lock_owner != owner or self._lock_depth != 1:
                    raise RuntimeError(
                        "state-lock ownership changed during acquisition"
                    )
                self._lock_fd = lock_fd
                self._pinned_root_fd = root_fd
            try:
                yield self
            finally:
                with self._lock_condition:
                    if self._lock_owner != owner or self._lock_depth != 1:
                        raise RuntimeError(
                            "outer state lock exited with nested ownership"
                        )
                    self._lock_releasing = True
        finally:
            try:
                if flocked and lock_fd is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                try:
                    if lock_fd is not None:
                        os.close(lock_fd)
                finally:
                    try:
                        if root_fd is not None:
                            os.close(root_fd)
                    finally:
                        with self._lock_condition:
                            if self._lock_owner == owner:
                                self._lock_depth = 0
                                self._lock_fd = None
                                self._pinned_root_fd = None
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
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not self._owns_lock():
            raise RuntimeError("atomic state writes require the state lock")
        _deadline_check(deadline, monotonic)
        data = _canonical_json(value)
        _deadline_check(deadline, monotonic)
        target = directory / name
        try:
            _deadline_check(deadline, monotonic)
            existing = self._open_private_file(directory_fd, name, target)
            _deadline_check(deadline, monotonic)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            os.close(existing)
            _deadline_check(deadline, monotonic)
        temporary = f".tmp-{secrets.token_hex(16)}"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        created: os.stat_result | None = None
        try:
            _deadline_check(deadline, monotonic)
            fd = os.open(temporary, flags, _FILE_MODE, dir_fd=directory_fd)
            _deadline_check(deadline, monotonic)
            created = os.fstat(fd)
            _deadline_check(deadline, monotonic)
            try:
                os.fchmod(fd, _FILE_MODE)
                _deadline_check(deadline, monotonic)
                _write_all(
                    fd,
                    data,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                os.fsync(fd)
                _deadline_check(deadline, monotonic)
            finally:
                os.close(fd)
            _deadline_check(deadline, monotonic)
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            _deadline_check(deadline, monotonic)
            os.fsync(directory_fd)
            _deadline_check(deadline, monotonic)
        except Exception:
            if created is not None:
                try:
                    current = os.stat(
                        temporary, dir_fd=directory_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    current = None
                if (
                    current is not None
                    and (current.st_dev, current.st_ino)
                    == (
                        created.st_dev,
                        created.st_ino,
                    )
                    and (
                        stat.S_ISREG(current.st_mode)
                        and current.st_uid == os.getuid()
                        and current.st_nlink == 1
                    )
                ):
                    os.unlink(temporary, dir_fd=directory_fd)
            raise

    def _maintenance_locked(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        _deadline_check(deadline, monotonic)
        if deadline is not None:
            # Time-bounded writers enforce the authoritative receipt invariant;
            # unrelated archival and transaction housekeeping remains deferrable.
            self._prune_event_receipts_locked(
                deadline=deadline,
                monotonic=monotonic,
            )
            return
        self._prune_event_backups_locked()
        self._prune_event_receipts_locked()
        self._prune_transactions_locked()

    def ledger_revision(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> int:
        _deadline_check(deadline, monotonic)
        if self._owns_lock() or self.read_only:
            return self._ledger_revision_locked(
                deadline=deadline,
                monotonic=monotonic,
            )
        with self.locked(
            remaining_timeout=_remaining_timeout(deadline, monotonic),
        ):
            return self._ledger_revision_locked(
                deadline=deadline,
                monotonic=monotonic,
            )

    def _ledger_revision_locked(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> int:
        _deadline_check(deadline, monotonic)
        root_fd = self._open_root()
        _deadline_check(deadline, monotonic)
        try:
            try:
                fd = self._open_private_file(
                    root_fd,
                    LEDGER_REVISION_FILENAME,
                    self.root / LEDGER_REVISION_FILENAME,
                )
            except FileNotFoundError:
                return 0
            try:
                raw = _read_state_record(
                    fd,
                    deadline=deadline,
                    monotonic=monotonic,
                )
            finally:
                os.close(fd)
        finally:
            os.close(root_fd)

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

    def _bump_ledger_revision_locked(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> int:
        if not self._owns_lock():
            raise RuntimeError("ledger revision update requires the state lock")
        _deadline_check(deadline, monotonic)
        revision = (
            self._ledger_revision_locked(
                deadline=deadline,
                monotonic=monotonic,
            )
            + 1
        )
        _deadline_check(deadline, monotonic)
        root_fd = self._open_root()
        _deadline_check(deadline, monotonic)
        temporary = f".ledger-revision-{secrets.token_hex(16)}"
        fd: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NONBLOCK
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            _deadline_check(deadline, monotonic)
            fd = os.open(temporary, flags, _FILE_MODE, dir_fd=root_fd)
            _deadline_check(deadline, monotonic)
            os.fchmod(fd, _FILE_MODE)
            _deadline_check(deadline, monotonic)
            payload = _canonical_json({"schema_version": 1, "revision": revision})
            _deadline_check(deadline, monotonic)
            _write_all(
                fd,
                payload,
                deadline=deadline,
                monotonic=monotonic,
            )
            os.fsync(fd)
            _deadline_check(deadline, monotonic)
            os.close(fd)
            fd = None
            _deadline_check(deadline, monotonic)
            os.replace(
                temporary,
                LEDGER_REVISION_FILENAME,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            _deadline_check(deadline, monotonic)
            os.fsync(root_fd)
            _deadline_check(deadline, monotonic)
        except Exception:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(root_fd)
        return revision

    def _save_record_locked(
        self,
        kind: str,
        key: str,
        payload: dict[str, object],
        *,
        maintenance: bool,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> int:
        if not self._owns_lock():
            raise RuntimeError("state record write requires the state lock")
        if maintenance:
            self._maintenance_locked(deadline=deadline, monotonic=monotonic)
        _deadline_check(deadline, monotonic)
        root_fd = self._open_root()
        _deadline_check(deadline, monotonic)
        try:
            _deadline_check(deadline, monotonic)
            directory_fd = self._open_directory(root_fd, kind, create=True)
            _deadline_check(deadline, monotonic)
            assert directory_fd is not None
            try:
                revision = self._bump_ledger_revision_locked(
                    deadline=deadline,
                    monotonic=monotonic,
                )
                self._atomic_json(
                    directory_fd,
                    self.root / kind,
                    key + ".json",
                    payload,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                return revision
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)

    def save_session(self, lease: SessionLease, *, maintenance: bool = True) -> None:
        if not isinstance(lease, SessionLease):
            raise TypeError("lease must be a SessionLease")
        key = session_key(lease.session_id)
        payload = lease.to_dict()
        self._require_mutable()
        with self.locked():
            self._recover_before_write_locked()
            self._save_record_locked(
                "sessions",
                key,
                payload,
                maintenance=maintenance,
            )

    def save_process(
        self, process: ManagedProcess, *, maintenance: bool = True
    ) -> None:
        if not isinstance(process, ManagedProcess):
            raise TypeError("process must be a ManagedProcess")
        key = process.wrapper.stable_key()
        payload = process.to_dict()
        self._require_mutable()
        with self.locked():
            self._recover_before_write_locked()
            self._save_record_locked(
                "processes",
                key,
                payload,
                maintenance=maintenance,
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
        if not self._root_exists():
            return
        with self.locked():
            self._recover_before_write_locked()
            self._maintenance_locked()
            self._bump_ledger_revision_locked()
            root_fd = self._open_root()
            try:
                directory_fd = self._open_directory(root_fd, "processes", create=False)
                if directory_fd is None:
                    return
                try:
                    name = key + ".json"
                    try:
                        fd = self._open_private_file(
                            directory_fd, name, self.root / "processes" / name
                        )
                    except FileNotFoundError:
                        return
                    os.close(fd)
                    os.unlink(name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                os.close(root_fd)

    def load_sessions(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[SessionLease, ...]:
        return self._load_records(
            "sessions",
            SessionLease.from_dict,
            lambda value: session_key(value.session_id),
            deadline=deadline,
            monotonic=monotonic,
        )

    def sessions_digest(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> str:
        _deadline_check(deadline, monotonic)
        leases = self.load_sessions(deadline=deadline, monotonic=monotonic)
        _deadline_check(deadline, monotonic)
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
        key = session_key(session_id)
        value = self._load_exact_record(
            "sessions",
            key,
            SessionLease.from_dict,
            lambda item: session_key(item.session_id),
            deadline=deadline,
            monotonic=monotonic,
        )
        if value is not None and value.session_id != session_id:
            raise StateCorruption(self.root / "sessions" / f"{key}.json", key)
        return value

    def load_process(self, process_key: str) -> ManagedProcess | None:
        process = self.load_raw_process(process_key)
        if process is None:
            return None
        return self._overlay_signal_intent(process)

    def load_raw_process(
        self,
        process_key: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> ManagedProcess | None:
        if not isinstance(process_key, str) or len(process_key) != _HEX_DIGEST_LENGTH:
            raise ValueError("invalid process key")
        try:
            int(process_key, 16)
        except ValueError as error:
            raise ValueError("invalid process key") from error
        process = self._load_exact_record(
            "processes",
            process_key,
            ManagedProcess.from_dict,
            lambda item: item.wrapper.stable_key(),
            deadline=deadline,
            monotonic=monotonic,
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
        if not _is_hex_digest(process_key):
            raise ValueError("invalid process key")
        value = self._load_exact_record(
            "signal-intents",
            process_key,
            SignalIntent.from_dict,
            lambda item: item.process_key,
            deadline=deadline,
            monotonic=monotonic,
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
        if not _is_hex_digest(process_key):
            raise ValueError("invalid process key")
        value = self._load_exact_record(
            "force-receipts",
            process_key,
            SignalIntent.from_dict,
            lambda item: item.process_key,
            deadline=deadline,
            monotonic=monotonic,
        )
        assert value is None or isinstance(value, SignalIntent)
        return value

    def load_signal_intents(
        self,
        action: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[SignalIntent, ...]:
        if action not in {"term", "force"}:
            raise ValueError("invalid signal intent action")
        kind = "force-receipts" if action == "force" else "signal-intents"
        values = self._load_records(
            kind,
            SignalIntent.from_dict,
            lambda item: item.process_key,
            deadline=deadline,
            monotonic=monotonic,
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
        with self.locked():
            self._recover_before_write_locked()
            existing = (
                self.load_force_intent(intent.process_key)
                if intent.action == "force"
                else self.load_signal_intent(intent.process_key)
            )
            merged = self._merge_signal_intent(existing, intent)
            if merged == existing:
                return self.ledger_revision()
            return self._save_record_locked(
                directory_name,
                intent.process_key,
                merged.to_dict(),
                maintenance=False,
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
        with self.locked():
            self._recover_before_write_locked()
            root_fd = self._open_root()
            try:
                directory_fd = self._open_directory(
                    root_fd, directory_name, create=False
                )
                if directory_fd is None:
                    return self.ledger_revision()
                try:
                    revision = self._bump_ledger_revision_locked()
                    try:
                        os.unlink(process_key + ".json", dir_fd=directory_fd)
                    except FileNotFoundError:
                        return revision
                    os.fsync(directory_fd)
                    return revision
                finally:
                    os.close(directory_fd)
            finally:
                os.close(root_fd)

    def _overlay_signal_intent(self, process: ManagedProcess) -> ManagedProcess:
        intent = self.load_signal_intent(process.wrapper.stable_key())
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
        force = self.load_force_intent(process.wrapper.stable_key())
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

    def _load_exact_record(
        self,
        kind: str,
        key: str,
        parser: Callable[[object], _Record],
        key_for: Callable[[_Record], str],
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> _Record | None:
        _deadline_check(deadline, monotonic)
        if not self._root_exists():
            return None
        if self.read_only:
            return self._load_exact_record_locked_or_read_only(
                kind,
                key,
                parser,
                key_for,
                deadline=deadline,
                monotonic=monotonic,
            )
        with self.locked(
            remaining_timeout=_remaining_timeout(deadline, monotonic),
        ):
            return self._load_exact_record_locked_or_read_only(
                kind,
                key,
                parser,
                key_for,
                deadline=deadline,
                monotonic=monotonic,
            )

    def _load_exact_record_locked_or_read_only(
        self,
        kind: str,
        key: str,
        parser: Callable[[object], _Record],
        key_for: Callable[[_Record], str],
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> _Record | None:
        _deadline_check(deadline, monotonic)
        root_fd = self._open_root()
        _deadline_check(deadline, monotonic)
        try:
            directory_fd = self._open_directory(root_fd, kind, create=False)
            if directory_fd is None:
                return None
            try:
                name = key + ".json"
                path = self.root / kind / name
                try:
                    fd = self._open_private_file(directory_fd, name, path)
                except FileNotFoundError:
                    return None
                try:
                    raw = _read_state_record(
                        fd,
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                except (ValueError, OverflowError, RecursionError):
                    _deadline_check(deadline, monotonic)
                    os.lseek(fd, 0, os.SEEK_SET)
                    _deadline_check(deadline, monotonic)
                    raw = os.read(fd, STATE_RECORD_MAX_BYTES + 1)
                    _deadline_check(deadline, monotonic)
                finally:
                    os.close(fd)
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
                        )
                    raise StateCorruption(
                        path,
                        digest,
                        quarantine_path=quarantine,
                    ) from None
                return record
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)

    def load_processes(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[ManagedProcess, ...]:
        return tuple(
            self._overlay_signal_intent(process)
            for process in self.load_raw_processes(
                deadline=deadline,
                monotonic=monotonic,
            )
        )

    def load_raw_processes(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[ManagedProcess, ...]:
        records = self._load_records(
            "processes",
            ManagedProcess.from_dict,
            lambda value: value.wrapper.stable_key(),
            deadline=deadline,
            monotonic=monotonic,
        )
        return tuple(records)

    def _load_records(
        self,
        kind: str,
        parser: Callable[[object], _Record],
        key_for: Callable[[_Record], str],
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[_Record, ...]:
        _deadline_check(deadline, monotonic)
        if not self._root_exists():
            return ()
        if self.read_only:
            return self._load_records_locked_or_read_only(
                kind,
                parser,
                key_for,
                deadline=deadline,
                monotonic=monotonic,
            )
        lock = (
            self.locked()
            if deadline is None
            else self.locked(
                remaining_timeout=_remaining_timeout(deadline, monotonic),
            )
        )
        with lock:
            return self._load_records_locked_or_read_only(
                kind,
                parser,
                key_for,
                deadline=deadline,
                monotonic=monotonic,
            )

    def _load_records_locked_or_read_only(
        self,
        kind: str,
        parser: Callable[[object], _Record],
        key_for: Callable[[_Record], str],
        *,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> tuple[_Record, ...]:
        _deadline_check(deadline, monotonic)
        root_fd = self._open_root()
        try:
            directory_fd = self._open_directory(root_fd, kind, create=False)
            if directory_fd is None:
                return ()
            try:
                records: list[_Record] = []
                names = _bounded_directory_names(
                    directory_fd,
                    self.root / kind,
                    STATE_DIRECTORY_MAX_ENTRIES,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                for name in names:
                    _deadline_check(deadline, monotonic)
                    path = self.root / kind / name
                    fd = self._open_private_file(directory_fd, name, path)
                    try:
                        raw = _read_state_record(
                            fd,
                            deadline=deadline,
                            monotonic=monotonic,
                        )
                    except (ValueError, OverflowError, RecursionError):
                        _deadline_check(deadline, monotonic)
                        os.lseek(fd, 0, os.SEEK_SET)
                        _deadline_check(deadline, monotonic)
                        raw = os.read(fd, STATE_RECORD_MAX_BYTES + 1)
                        _deadline_check(deadline, monotonic)
                    finally:
                        os.close(fd)
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
                                root_fd, directory_fd, kind, name, raw
                            )
                        raise StateCorruption(
                            path, digest, quarantine_path=quarantine
                        ) from None
                    records.append(record)
                    _deadline_check(deadline, monotonic)
                return tuple(records)
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)

    def _quarantine_locked(
        self,
        root_fd: int,
        source_fd: int,
        kind: str,
        name: str,
        raw: bytes,
    ) -> Path:
        if not self._owns_lock():
            raise RuntimeError("quarantine requires the state lock")
        digest = hashlib.sha256(raw).hexdigest()
        quarantine_fd = self._open_directory(root_fd, "corrupt", create=True)
        assert quarantine_fd is not None
        try:
            destination = ""
            for _attempt in range(16):
                candidate = f"{kind}-{digest}-{secrets.token_hex(8)}.json"
                try:
                    _rename_noreplace(source_fd, name, quarantine_fd, candidate)
                except FileExistsError:
                    continue
                destination = candidate
                break
            if not destination:
                raise StateCorruption(self.root / kind / name, digest)
            os.fsync(source_fd)
            os.fsync(quarantine_fd)
            destination_path = self.root / "corrupt" / destination
            return destination_path
        finally:
            os.close(quarantine_fd)

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
        _deadline_check(deadline, monotonic)
        self._require_mutable()
        lock = (
            self.locked()
            if remaining_timeout is None and deadline is None
            else self.locked(
                remaining_timeout=(
                    remaining_timeout
                    if deadline is None
                    else min(
                        remaining_timeout
                        if remaining_timeout is not None
                        else self.lock_timeout,
                        _remaining_timeout(deadline, monotonic) or 0.0,
                    )
                )
            )
        )
        with lock:
            self._recover_before_write_locked(deadline, monotonic)
            if maintenance:
                self._maintenance_locked(
                    deadline=deadline,
                    monotonic=monotonic,
                )
            _deadline_check(deadline, monotonic)
            root_fd = self._open_root()
            try:
                event_id = event.get("event_id")
                if isinstance(event_id, str):
                    created = self._write_event_receipt_locked(
                        root_fd,
                        event_id,
                        event,
                        self.ledger_revision(
                            deadline=deadline,
                            monotonic=monotonic,
                        ),
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                    if not created:
                        return
                self._append_event_locked(
                    root_fd,
                    record,
                    deadline=deadline,
                    monotonic=monotonic,
                )
            finally:
                os.close(root_fd)

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
        _deadline_check(deadline, monotonic)
        self._require_mutable()
        root_token = (
            None if expected_root_binding is None else expected_root_binding.root_token
        )
        with self.locked(
            expected_root_token=root_token,
            remaining_timeout=_remaining_timeout(deadline, monotonic),
        ):
            self._recover_before_write_locked(deadline, monotonic)
            _deadline_check(deadline, monotonic)
            self._validate_transition_authority_locked(
                record_kind,
                record_key,
                expected,
                expected_revision,
                expected_sessions_digest,
                expected_root_binding,
                deadline,
                monotonic,
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
                deadline=deadline,
                monotonic=monotonic,
            )
            _deadline_check(deadline, monotonic)
            self._write_transition_journal_locked(
                event_id,
                journal,
                deadline=deadline,
                monotonic=monotonic,
            )
            _deadline_check(deadline, monotonic)
            self._validate_transition_authority_locked(
                record_kind,
                record_key,
                expected,
                expected_revision,
                expected_sessions_digest,
                expected_root_binding,
                deadline,
                monotonic,
            )
            effect_completed = False
            try:
                if before_effect is not None:
                    before_effect()
                    _deadline_check(deadline, monotonic)
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
                self._recover_known_transition_locked(
                    event_id,
                    journal,
                )
                raise
            if effect_completed:
                try:
                    _deadline_check(deadline, monotonic)
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
                    deadline=deadline,
                    monotonic=monotonic,
                )
            except Exception:
                recovery_failed = False
                try:
                    self._recover_known_transition_locked(
                        event_id,
                        journal,
                    )
                except Exception:
                    recovery_failed = True
                if (
                    not recovery_failed
                    and self._transition_record_locked(record_kind, record_key)
                    == updated
                ):
                    if effect_completed:
                        raise PostEffectStateError(
                            "effect completed before state write reported failure",
                            record_persisted=True,
                        )
                    return self.ledger_revision()
                if effect_completed:
                    raise PostEffectStateError(
                        "effect completed before state persistence failed",
                        record_persisted=False,
                    )
                raise
            try:
                self._mark_transition_committed_locked(
                    event_id,
                    deadline=deadline,
                    monotonic=monotonic,
                )
            except Exception as error:
                if (
                    isinstance(error, OperationDeadlineExceeded)
                    and not effect_completed
                ):
                    raise
                recovery_failed = False
                try:
                    self._recover_known_transition_locked(
                        event_id,
                        journal,
                    )
                except Exception:
                    recovery_failed = True
                record_persisted = False
                try:
                    record_persisted = (
                        self._transition_record_locked(record_kind, record_key)
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
                    return self.ledger_revision()
                raise
            try:
                self._recover_known_transition_locked(
                    event_id,
                    journal,
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
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> None:
        if expected_root_binding is not None:
            self.validate_root_binding(expected_root_binding)
        if (
            expected_revision is not None
            and self.ledger_revision(
                deadline=deadline,
                monotonic=monotonic,
            )
            != expected_revision
        ):
            raise UnsafeStatePath("authorized ledger revision changed")
        if expected_sessions_digest is not None and (
            self.sessions_digest(deadline=deadline, monotonic=monotonic)
            != expected_sessions_digest
        ):
            raise UnsafeStatePath("authorized session set changed")
        current = self._transition_record_locked(
            record_kind,
            record_key,
            deadline=deadline,
            monotonic=monotonic,
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
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[str, dict[str, object]]:
        _deadline_check(deadline, monotonic)
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
        _deadline_check(deadline, monotonic)
        expected_digest = hashlib.sha256(_canonical_json(expected_payload)).hexdigest()
        _deadline_check(deadline, monotonic)
        updated_digest = hashlib.sha256(_canonical_json(updated_payload)).hexdigest()
        _deadline_check(deadline, monotonic)
        if expected_digest == updated_digest:
            raise ValueError("transition must change raw state")
        event_id = hashlib.sha256(
            _canonical_json(
                {
                    "record_kind": record_kind,
                    "record_key": record_key,
                    "expected_digest": expected_digest,
                    "updated_digest": updated_digest,
                    "event": event_payload,
                }
            )
        ).hexdigest()
        _deadline_check(deadline, monotonic)
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
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
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
            deadline=deadline,
            monotonic=monotonic,
        )

    def _transition_record_locked(
        self,
        kind: str,
        key: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> SessionLease | ManagedProcess | SignalIntent | None:
        if kind == "sessions":
            records = self.load_sessions(deadline=deadline, monotonic=monotonic)
            return next(
                (item for item in records if session_key(item.session_id) == key),
                None,
            )
        if kind == "processes":
            return self.load_raw_process(
                key,
                deadline=deadline,
                monotonic=monotonic,
            )
        if kind == "signal-intents":
            return self.load_signal_intent(
                key,
                deadline=deadline,
                monotonic=monotonic,
            )
        if kind == "force-receipts":
            return self.load_force_intent(
                key,
                deadline=deadline,
                monotonic=monotonic,
            )
        raise UnsafeStatePath("invalid journal record kind")

    def _write_transition_journal_locked(
        self,
        event_id: str,
        journal: dict[str, object],
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not self._owns_lock():
            raise RuntimeError("transition journal write requires the state lock")
        _deadline_check(deadline, monotonic)
        root_fd = self._open_root()
        _deadline_check(deadline, monotonic)
        try:
            directory_fd = self._open_directory(root_fd, "event-journal", create=True)
            _deadline_check(deadline, monotonic)
            assert directory_fd is not None
            try:
                names = _bounded_directory_names(
                    directory_fd,
                    self.root / "event-journal",
                    TRANSITION_JOURNAL_LIMIT,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                if (
                    len(names) >= TRANSITION_JOURNAL_LIMIT
                    and (event_id + ".json") not in names
                ):
                    raise StateCorruption(
                        self.root / "event-journal",
                        hashlib.sha256(b"journal_capacity").hexdigest(),
                    )
                _deadline_check(deadline, monotonic)
                self._atomic_json(
                    directory_fd,
                    self.root / "event-journal",
                    event_id + ".json",
                    journal,
                    deadline=deadline,
                    monotonic=monotonic,
                )
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)

    def _mark_transition_committed_locked(
        self,
        event_id: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        journal = self._load_journal_locked(
            event_id,
            deadline=deadline,
            monotonic=monotonic,
        )
        if journal is None:
            return
        journal["phase"] = "committed"
        root_fd = self._open_root()
        try:
            directory_fd = self._open_directory(root_fd, "event-journal", create=False)
            assert directory_fd is not None
            try:
                self._atomic_json(
                    directory_fd,
                    self.root / "event-journal",
                    event_id + ".json",
                    journal,
                    deadline=deadline,
                    monotonic=monotonic,
                )
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)

    def recover_transition_events(
        self,
        limit: int = 64,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(limit) is not int or limit < 0 or limit > TRANSITION_JOURNAL_LIMIT:
            raise ValueError("invalid transition recovery limit")
        _deadline_check(deadline, monotonic)
        self._require_mutable()
        with self.locked(
            remaining_timeout=_remaining_timeout(deadline, monotonic),
        ):
            self._recover_transition_events_locked(
                limit=limit,
                deadline=deadline,
                monotonic=monotonic,
            )

    def _recover_before_write_locked(
        self,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._recover_transition_events_locked(
            limit=TRANSITION_JOURNAL_LIMIT,
            deadline=deadline,
            monotonic=monotonic,
        )
        self._prune_event_receipts_locked(
            deadline=deadline,
            monotonic=monotonic,
        )
        self._recover_legacy_outbox_locked(
            LEGACY_OUTBOX_DRAIN_LIMIT,
            deadline=deadline,
            monotonic=monotonic,
        )
        self._prune_event_receipts_locked(
            deadline=deadline,
            monotonic=monotonic,
        )

    def _recover_transition_events_locked(
        self,
        *,
        limit: int,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> None:
        _deadline_check(deadline, monotonic)
        root_fd = self._open_root()
        _deadline_check(deadline, monotonic)
        try:
            _deadline_check(deadline, monotonic)
            directory_fd = self._open_directory(root_fd, "event-journal", create=False)
            _deadline_check(deadline, monotonic)
            if directory_fd is None:
                return
            try:
                names = _bounded_directory_names(
                    directory_fd,
                    self.root / "event-journal",
                    TRANSITION_JOURNAL_LIMIT,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                for name in names[:limit]:
                    _deadline_check(deadline, monotonic)
                    if not name.endswith(".json") or not _is_hex_digest(name[:-5]):
                        raise UnsafeStatePath("invalid event journal entry")
                    event_id = name[:-5]
                    if event_id in self._effect_transition_ids:
                        continue
                    journal = self._load_journal_locked(
                        event_id,
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                    if journal is None:
                        continue
                    self._recover_one_transition_locked(
                        root_fd,
                        directory_fd,
                        event_id,
                        journal,
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                    _deadline_check(deadline, monotonic)
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)

    def _recover_one_transition_locked(
        self,
        root_fd: int,
        directory_fd: int,
        event_id: str,
        journal: dict[str, object],
        *,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> bool:
        """Recover one known journal without enumerating unrelated state."""
        _deadline_check(deadline, monotonic)
        current_digest = self._transition_record_digest_locked(
            journal["record_kind"],
            journal["record_key"],
            deadline=deadline,
            monotonic=monotonic,
        )
        _deadline_check(deadline, monotonic)
        has_receipt = self._event_receipt_exists_locked(
            root_fd,
            event_id,
            journal["event"],
            deadline=deadline,
            monotonic=monotonic,
        )
        committed = bool(
            journal["phase"] == "committed"
            or current_digest == journal["updated_digest"]
            or has_receipt
        )
        if committed:
            created = self._write_event_receipt_locked(
                root_fd,
                event_id,
                journal["event"],
                self.ledger_revision(
                    deadline=deadline,
                    monotonic=monotonic,
                ),
                deadline=deadline,
                monotonic=monotonic,
            )
            if created:
                self._append_event_locked(
                    root_fd,
                    _canonical_json(journal["event"]),
                    deadline=deadline,
                    monotonic=monotonic,
                )
            self._prune_event_receipts_locked(
                deadline=deadline,
                monotonic=monotonic,
            )
        elif current_digest != journal["expected_digest"]:
            raise StateCorruption(
                self.root / "event-journal" / (event_id + ".json"),
                hashlib.sha256(_canonical_json(journal)).hexdigest(),
            )
        _deadline_check(deadline, monotonic)
        os.unlink(event_id + ".json", dir_fd=directory_fd)
        _deadline_check(deadline, monotonic)
        os.fsync(directory_fd)
        _deadline_check(deadline, monotonic)
        return committed

    def _recover_known_transition_locked(
        self,
        event_id: str,
        journal: dict[str, object],
    ) -> bool:
        """Finish the active transition without a post-deadline journal scan."""
        root_fd = self._open_root()
        try:
            directory_fd = self._open_directory(root_fd, "event-journal", create=False)
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
                    deadline=None,
                    monotonic=time.monotonic,
                )
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)

    def _load_journal_locked(
        self,
        event_id: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> dict[str, object] | None:
        _deadline_check(deadline, monotonic)
        root_fd = self._open_root()
        _deadline_check(deadline, monotonic)
        try:
            _deadline_check(deadline, monotonic)
            directory_fd = self._open_directory(root_fd, "event-journal", create=False)
            _deadline_check(deadline, monotonic)
            if directory_fd is None:
                return None
            try:
                try:
                    fd = self._open_private_file(
                        directory_fd,
                        event_id + ".json",
                        self.root / "event-journal" / (event_id + ".json"),
                    )
                    _deadline_check(deadline, monotonic)
                except FileNotFoundError:
                    return None
                try:
                    raw = _read_state_record(
                        fd,
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                finally:
                    os.close(fd)
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)
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
        return payload

    def _transition_record_digest_locked(
        self,
        kind: str,
        key: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> str:
        _deadline_check(deadline, monotonic)
        if kind == "sessions":
            records = self.load_sessions(deadline=deadline, monotonic=monotonic)
            current = next(
                (item for item in records if session_key(item.session_id) == key),
                None,
            )
        elif kind == "processes":
            current = self.load_raw_process(
                key,
                deadline=deadline,
                monotonic=monotonic,
            )
        elif kind == "signal-intents":
            current = self.load_signal_intent(
                key,
                deadline=deadline,
                monotonic=monotonic,
            )
        elif kind == "force-receipts":
            current = self.load_force_intent(
                key,
                deadline=deadline,
                monotonic=monotonic,
            )
        else:
            raise UnsafeStatePath("invalid journal record kind")
        payload = None if current is None else current.to_dict()
        _deadline_check(deadline, monotonic)
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def _load_event_receipt_locked(
        self,
        root_fd: int,
        event_id: str,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> dict[str, object] | None:
        _deadline_check(deadline, monotonic)
        if not _is_hex_digest(event_id):
            raise UnsafeStatePath("invalid event receipt ID")
        directory_fd = self._open_directory(root_fd, "event-receipts", create=False)
        _deadline_check(deadline, monotonic)
        if directory_fd is None:
            return None
        try:
            try:
                fd = self._open_private_file(
                    directory_fd,
                    event_id + ".json",
                    self.root / "event-receipts" / (event_id + ".json"),
                )
                _deadline_check(deadline, monotonic)
            except FileNotFoundError:
                return None
            try:
                raw = _read_state_record(
                    fd,
                    deadline=deadline,
                    monotonic=monotonic,
                )
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
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
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> bool:
        receipt = self._load_event_receipt_locked(
            root_fd,
            event_id,
            deadline=deadline,
            monotonic=monotonic,
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
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> bool:
        _deadline_check(deadline, monotonic)
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
            deadline=deadline,
            monotonic=monotonic,
        )
        if current is not None:
            if current["event"] != event:
                raise StateCorruption(
                    self.root / "event-receipts" / (event_id + ".json"),
                    hashlib.sha256(_canonical_json(current)).hexdigest(),
                )
            return False
        self._prune_event_receipts_locked(
            deadline=deadline,
            monotonic=monotonic,
            reserve=1,
        )
        _deadline_check(deadline, monotonic)
        directory_fd = self._open_directory(root_fd, "event-receipts", create=True)
        _deadline_check(deadline, monotonic)
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
                deadline=deadline,
                monotonic=monotonic,
            )
            _deadline_check(deadline, monotonic)
            return True
        finally:
            os.close(directory_fd)

    def _recover_legacy_outbox_locked(
        self,
        limit: int,
        *,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> None:
        _deadline_check(deadline, monotonic)
        root_fd = self._open_root()
        _deadline_check(deadline, monotonic)
        try:
            _deadline_check(deadline, monotonic)
            directory_fd = self._open_directory(root_fd, "outbox", create=False)
            _deadline_check(deadline, monotonic)
            if directory_fd is None:
                return
            try:
                names = _bounded_directory_names(
                    directory_fd,
                    self.root / "outbox",
                    LEGACY_OUTBOX_CAPACITY,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                for name in names[:limit]:
                    _deadline_check(deadline, monotonic)
                    if not name.endswith(".json"):
                        raise UnsafeStatePath("invalid outbox entry")
                    event_id = name[:-5]
                    if not _is_hex_digest(event_id):
                        raise UnsafeStatePath("invalid outbox event ID")
                    fd = self._open_private_file(
                        directory_fd,
                        name,
                        self.root / "outbox" / name,
                    )
                    _deadline_check(deadline, monotonic)
                    try:
                        raw = _read_state_record(
                            fd,
                            deadline=deadline,
                            monotonic=monotonic,
                        )
                    finally:
                        os.close(fd)
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
                        self.ledger_revision(
                            deadline=deadline,
                            monotonic=monotonic,
                        ),
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                    if created:
                        self._append_event_locked(
                            root_fd,
                            _canonical_json(payload),
                            deadline=deadline,
                            monotonic=monotonic,
                        )
                    _deadline_check(deadline, monotonic)
                    os.unlink(name, dir_fd=directory_fd)
                    _deadline_check(deadline, monotonic)
                    os.fsync(directory_fd)
                    _deadline_check(deadline, monotonic)
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)

    def _append_event_locked(
        self,
        root_fd: int,
        record: bytes,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        _deadline_check(deadline, monotonic)
        name = "events.jsonl"
        path = self.root / name
        size = 0
        try:
            current_fd = self._open_private_file(root_fd, name, path)
            _deadline_check(deadline, monotonic)
        except FileNotFoundError:
            current_fd = None
        if current_fd is not None:
            try:
                _deadline_check(deadline, monotonic)
                size = os.fstat(current_fd).st_size
                _deadline_check(deadline, monotonic)
            finally:
                os.close(current_fd)
        _deadline_check(deadline, monotonic)
        if size and size + len(record) > EVENT_LOG_MAX_BYTES:
            self._rotate_events_locked(
                root_fd,
                deadline=deadline,
                monotonic=monotonic,
            )
            _deadline_check(deadline, monotonic)
        try:
            _deadline_check(deadline, monotonic)
            fd = self._open_private_file(root_fd, name, path, os.O_WRONLY | os.O_APPEND)
            _deadline_check(deadline, monotonic)
        except FileNotFoundError:
            flags = os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK
            flags |= os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            _deadline_check(deadline, monotonic)
            fd = os.open(name, flags, _FILE_MODE, dir_fd=root_fd)
            _deadline_check(deadline, monotonic)
            os.fchmod(fd, _FILE_MODE)
            _deadline_check(deadline, monotonic)
        try:
            _deadline_check(deadline, monotonic)
            value = os.fstat(fd)
            _deadline_check(deadline, monotonic)
            _validate_file(value, path)
            _write_all(
                fd,
                record,
                deadline=deadline,
                monotonic=monotonic,
            )
            _deadline_check(deadline, monotonic)
            os.fsync(fd)
            _deadline_check(deadline, monotonic)
        finally:
            os.close(fd)
        _deadline_check(deadline, monotonic)
        os.fsync(root_fd)
        _deadline_check(deadline, monotonic)

    def _validate_named_file_if_present(
        self,
        directory_fd: int,
        name: str,
        *,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> bool:
        _deadline_check(deadline, monotonic)
        try:
            fd = self._open_private_file(directory_fd, name, self.root / name)
            _deadline_check(deadline, monotonic)
        except FileNotFoundError:
            return False
        os.close(fd)
        _deadline_check(deadline, monotonic)
        return True

    def _rotate_events_locked(
        self,
        root_fd: int,
        *,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> None:
        _deadline_check(deadline, monotonic)
        oldest = f"events.jsonl.{EVENT_LOG_BACKUPS}"
        if self._validate_named_file_if_present(
            root_fd,
            oldest,
            deadline=deadline,
            monotonic=monotonic,
        ):
            _deadline_check(deadline, monotonic)
            os.unlink(oldest, dir_fd=root_fd)
            _deadline_check(deadline, monotonic)
        for number in range(EVENT_LOG_BACKUPS - 1, 0, -1):
            _deadline_check(deadline, monotonic)
            source = f"events.jsonl.{number}"
            destination = f"events.jsonl.{number + 1}"
            if not self._validate_named_file_if_present(
                root_fd,
                source,
                deadline=deadline,
                monotonic=monotonic,
            ):
                continue
            self._validate_named_file_if_present(
                root_fd,
                destination,
                deadline=deadline,
                monotonic=monotonic,
            )
            _deadline_check(deadline, monotonic)
            os.replace(source, destination, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            _deadline_check(deadline, monotonic)
        if self._validate_named_file_if_present(
            root_fd,
            "events.jsonl",
            deadline=deadline,
            monotonic=monotonic,
        ):
            self._validate_named_file_if_present(
                root_fd,
                "events.jsonl.1",
                deadline=deadline,
                monotonic=monotonic,
            )
            _deadline_check(deadline, monotonic)
            os.replace(
                "events.jsonl", "events.jsonl.1", src_dir_fd=root_fd, dst_dir_fd=root_fd
            )
            _deadline_check(deadline, monotonic)
        _deadline_check(deadline, monotonic)
        os.fsync(root_fd)
        _deadline_check(deadline, monotonic)

    def _prune_event_backups_locked(self) -> None:
        if not self._root_exists():
            return
        root_fd = self._open_root()
        cutoff = time.time() - EVENT_LOG_RETENTION_SECONDS
        try:
            candidates: list[tuple[str, os.stat_result]] = []
            for name in os.listdir(root_fd):
                if not name.startswith("events.jsonl."):
                    continue
                suffix = name.removeprefix("events.jsonl.")
                if not suffix.isdigit():
                    continue
                fd = self._open_private_file(root_fd, name, self.root / name)
                try:
                    value = os.fstat(fd)
                finally:
                    os.close(fd)
                candidates.append((name, value))
            canonical = set(EVENT_LOG_BACKUP_FILENAMES)
            changed = False
            for name, value in candidates:
                if name not in canonical or value.st_mtime < cutoff:
                    os.unlink(name, dir_fd=root_fd)
                    changed = True
            if changed:
                os.fsync(root_fd)
        finally:
            os.close(root_fd)

    def _prune_event_receipts_locked(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        reserve: int = 0,
    ) -> None:
        if reserve not in {0, 1}:
            raise ValueError("invalid event receipt reservation")
        _deadline_check(deadline, monotonic)
        exists = self._root_exists()
        _deadline_check(deadline, monotonic)
        if not exists:
            return
        _deadline_check(deadline, monotonic)
        root_fd = self._open_root()
        _deadline_check(deadline, monotonic)
        try:
            _deadline_check(deadline, monotonic)
            receipt_fd = self._open_directory(root_fd, "event-receipts", create=False)
            _deadline_check(deadline, monotonic)
            if receipt_fd is None:
                return
            _deadline_check(deadline, monotonic)
            journal_fd = self._open_directory(root_fd, "event-journal", create=False)
            _deadline_check(deadline, monotonic)
            try:
                protected: set[str] = set()
                if journal_fd is not None:
                    journal_names = _bounded_directory_names(
                        journal_fd,
                        self.root / "event-journal",
                        TRANSITION_JOURNAL_LIMIT,
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                    for name in journal_names:
                        if not name.endswith(".json") or not _is_hex_digest(name[:-5]):
                            raise UnsafeStatePath("invalid event journal entry")
                        protected.add(name[:-5])
                receipt_names = _bounded_directory_names(
                    receipt_fd,
                    self.root / "event-receipts",
                    STATE_DIRECTORY_MAX_ENTRIES,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                candidates: list[tuple[int, str]] = []
                for name in receipt_names:
                    _deadline_check(deadline, monotonic)
                    if not name.endswith(".json") or not _is_hex_digest(name[:-5]):
                        raise UnsafeStatePath("invalid event receipt entry")
                    receipt = self._load_event_receipt_locked(
                        root_fd,
                        name[:-5],
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                    if receipt is None:
                        raise StateCorruption(
                            self.root / "event-receipts" / name,
                            hashlib.sha256(b"missing_event_receipt").hexdigest(),
                        )
                    _deadline_check(deadline, monotonic)
                    fd = self._open_private_file(
                        receipt_fd,
                        name,
                        self.root / "event-receipts" / name,
                    )
                    _deadline_check(deadline, monotonic)
                    try:
                        _deadline_check(deadline, monotonic)
                        value = os.fstat(fd)
                        _deadline_check(deadline, monotonic)
                        candidates.append((value.st_mtime_ns, name))
                    finally:
                        os.close(fd)
                    _deadline_check(deadline, monotonic)
                excess = max(
                    0,
                    len(candidates) + reserve - EVENT_RECEIPT_RETENTION,
                )
                changed = False
                for _mtime, name in sorted(candidates):
                    _deadline_check(deadline, monotonic)
                    if excess == 0:
                        break
                    if name[:-5] in protected:
                        continue
                    os.unlink(name, dir_fd=receipt_fd)
                    _deadline_check(deadline, monotonic)
                    excess -= 1
                    changed = True
                if excess:
                    raise StateCorruption(
                        self.root / "event-receipts",
                        hashlib.sha256(b"receipt_capacity").hexdigest(),
                    )
                if changed:
                    _deadline_check(deadline, monotonic)
                    os.fsync(receipt_fd)
                    _deadline_check(deadline, monotonic)
            finally:
                if journal_fd is not None:
                    os.close(journal_fd)
                os.close(receipt_fd)
        finally:
            os.close(root_fd)

    def _install_transaction_reference(self, root_fd: int) -> str | None:
        def read_private(name: str) -> tuple[bytes, str] | None:
            try:
                fd = self._open_private_file(root_fd, name, self.root / name)
            except FileNotFoundError:
                return None
            try:
                raw = _read_all(fd)
            finally:
                os.close(fd)
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

    def _prune_transactions_locked(self) -> None:
        if not self._root_exists():
            return
        root_fd = self._open_root()
        try:
            reference = self._install_transaction_reference(root_fd)
            transactions_fd = self._open_directory(
                root_fd, "transactions", create=False
            )
            if transactions_fd is None:
                return
            try:
                candidates: list[tuple[int, str]] = []
                for name in os.listdir(transactions_fd):
                    value = os.stat(name, dir_fd=transactions_fd, follow_symlinks=False)
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
                            transactions_fd, name, self.root / "transactions" / name
                        )
                os.fsync(transactions_fd)
            finally:
                os.close(transactions_fd)
        finally:
            os.close(root_fd)

    def _remove_private_tree(self, parent_fd: int, name: str, path: Path) -> None:
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise UnsafeStatePath(f"cannot safely prune transaction: {path}") from error
        try:
            _validate_directory(os.fstat(directory_fd), path)
            for child in os.listdir(directory_fd):
                child_path = path / child
                value = os.stat(child, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(value.st_mode):
                    _validate_directory(value, child_path)
                    self._remove_private_tree(directory_fd, child, child_path)
                else:
                    _validate_file(value, child_path)
                    os.unlink(child, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=parent_fd)
