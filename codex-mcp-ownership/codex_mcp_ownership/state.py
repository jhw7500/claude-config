from __future__ import annotations

from contextlib import contextmanager
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
from .model import ManagedProcess, SessionLease


EVENT_LOG_MAX_BYTES = 1_048_576
EVENT_LOG_BACKUPS = 3
EVENT_LOG_RETENTION_SECONDS = 2_592_000
TRANSACTION_RETENTION = 3
EVENT_LOG_BACKUP_FILENAMES = tuple(
    f"events.jsonl.{number}" for number in range(1, EVENT_LOG_BACKUPS + 1)
)

INSTALL_STATE_FILENAME = "install-state.json"
INSTALL_STATE_TRANSACTION_FIELD = "transaction_id"
INSTALL_STATE_FIELDS = frozenset({INSTALL_STATE_TRANSACTION_FIELD})
INSTALL_STATE_LEGACY_FILENAMES = ("install_state.json", "install.json")

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
}

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_HEX_DIGEST_LENGTH = 64
_Record = TypeVar("_Record", SessionLease, ManagedProcess)


class UnsafeStatePath(RuntimeError):
    """A state path does not satisfy the private-file trust boundary."""


class StateLockTimeout(TimeoutError):
    """The state flock could not be acquired before its deadline."""


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


def session_key(session_id: str) -> str:
    validated = model.validate_session_id(session_id)
    return hashlib.sha256(validated.encode("utf-8")).hexdigest()


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


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 131072)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, data: bytes) -> None:
    position = 0
    while position < len(data):
        written = os.write(fd, data[position:])
        if written <= 0:
            raise OSError(errno.EIO, "short state write")
        position += written


class StateStore:
    def __init__(self, root: Path, read_only: bool = False, lock_timeout: float = 2.0) -> None:
        converted_timeout = float(lock_timeout)
        if not math.isfinite(converted_timeout) or converted_timeout < 0:
            raise ValueError("lock_timeout must be non-negative")
        self.root = Path(os.path.abspath(os.fspath(root)))
        self.read_only = read_only
        self.lock_timeout = converted_timeout
        self._lock_condition = threading.Condition()
        self._lock_owner: int | None = None
        self._lock_fd: int | None = None
        self._pinned_root_fd: int | None = None
        self._lock_depth = 0

    def _require_mutable(self) -> None:
        if self.read_only:
            raise ReadOnlyStateError("read-only state store cannot mutate")

    def _owns_lock(self) -> bool:
        with self._lock_condition:
            return (
                self._lock_owner == threading.get_ident()
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

    def _walk_root(self, *, create: bool) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
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
            if self._lock_owner == owner and self._pinned_root_fd is not None:
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
            raise UnsafeStatePath(f"cannot safely inspect state file: {path}") from error
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
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
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
            raise UnsafeStatePath(f"cannot safely open state directory: {path}") from error
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
        named_fd = self._open_private_file(root_fd, "state.lock", self.root / "state.lock")
        try:
            named = os.fstat(named_fd)
        finally:
            os.close(named_fd)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise UnsafeStatePath("state.lock was replaced during lock acquisition")

    @contextmanager
    def locked(self) -> Iterator[StateStore]:
        self._require_mutable()
        owner = threading.get_ident()
        deadline = time.monotonic() + self.lock_timeout
        nested = False
        with self._lock_condition:
            if self._lock_owner == owner:
                if self._pinned_root_fd is None or self._lock_depth < 1:
                    raise RuntimeError("invalid reentrant state-lock ownership")
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
            root_fd = self._open_root(create=True)
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
                        raise StateLockTimeout("timed out acquiring state.lock") from error
                    time.sleep(min(0.01, remaining))
            with self._lock_condition:
                if self._lock_owner != owner or self._lock_depth != 1:
                    raise RuntimeError("state-lock ownership changed during acquisition")
                self._lock_fd = lock_fd
                self._pinned_root_fd = root_fd
            try:
                yield self
            finally:
                with self._lock_condition:
                    if self._lock_owner != owner or self._lock_depth != 1:
                        raise RuntimeError("outer state lock exited with nested ownership")
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
                                self._lock_owner = None
                                self._lock_condition.notify_all()

    def _atomic_json(self, directory_fd: int, directory: Path, name: str, value: object) -> None:
        if not self._owns_lock():
            raise RuntimeError("atomic state writes require the state lock")
        data = _canonical_json(value)
        target = directory / name
        try:
            existing = self._open_private_file(directory_fd, name, target)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            os.close(existing)
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
            fd = os.open(temporary, flags, _FILE_MODE, dir_fd=directory_fd)
            created = os.fstat(fd)
            try:
                os.fchmod(fd, _FILE_MODE)
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        except Exception:
            if created is not None:
                try:
                    current = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if current is not None and (current.st_dev, current.st_ino) == (
                    created.st_dev,
                    created.st_ino,
                ) and (
                    stat.S_ISREG(current.st_mode)
                    and current.st_uid == os.getuid()
                    and current.st_nlink == 1
                ):
                    os.unlink(temporary, dir_fd=directory_fd)
            raise

    def _maintenance_locked(self) -> None:
        self._prune_event_backups_locked()
        self._prune_transactions_locked()

    def save_session(self, lease: SessionLease) -> None:
        if not isinstance(lease, SessionLease):
            raise TypeError("lease must be a SessionLease")
        key = session_key(lease.session_id)
        payload = lease.to_dict()
        self._require_mutable()
        with self.locked():
            self._maintenance_locked()
            root_fd = self._open_root()
            try:
                directory_fd = self._open_directory(root_fd, "sessions", create=True)
                assert directory_fd is not None
                try:
                    self._atomic_json(directory_fd, self.root / "sessions", key + ".json", payload)
                finally:
                    os.close(directory_fd)
            finally:
                os.close(root_fd)

    def save_process(self, process: ManagedProcess) -> None:
        if not isinstance(process, ManagedProcess):
            raise TypeError("process must be a ManagedProcess")
        key = process.wrapper.stable_key()
        payload = process.to_dict()
        self._require_mutable()
        with self.locked():
            self._maintenance_locked()
            root_fd = self._open_root()
            try:
                directory_fd = self._open_directory(root_fd, "processes", create=True)
                assert directory_fd is not None
                try:
                    self._atomic_json(directory_fd, self.root / "processes", key + ".json", payload)
                finally:
                    os.close(directory_fd)
            finally:
                os.close(root_fd)

    def remove_process(self, process: ManagedProcess | str) -> None:
        key = process.wrapper.stable_key() if isinstance(process, ManagedProcess) else process
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
            self._maintenance_locked()
            root_fd = self._open_root()
            try:
                directory_fd = self._open_directory(root_fd, "processes", create=False)
                if directory_fd is None:
                    return
                try:
                    name = key + ".json"
                    try:
                        fd = self._open_private_file(directory_fd, name, self.root / "processes" / name)
                    except FileNotFoundError:
                        return
                    os.close(fd)
                    os.unlink(name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                os.close(root_fd)

    def load_sessions(self) -> tuple[SessionLease, ...]:
        return self._load_records(
            "sessions",
            SessionLease.from_dict,
            lambda value: session_key(value.session_id),
        )

    def load_processes(self) -> tuple[ManagedProcess, ...]:
        return self._load_records(
            "processes",
            ManagedProcess.from_dict,
            lambda value: value.wrapper.stable_key(),
        )

    def _load_records(
        self,
        kind: str,
        parser: Callable[[object], _Record],
        key_for: Callable[[_Record], str],
    ) -> tuple[_Record, ...]:
        if not self._root_exists():
            return ()
        if self.read_only:
            return self._load_records_locked_or_read_only(kind, parser, key_for)
        with self.locked():
            return self._load_records_locked_or_read_only(kind, parser, key_for)

    def _load_records_locked_or_read_only(
        self,
        kind: str,
        parser: Callable[[object], _Record],
        key_for: Callable[[_Record], str],
    ) -> tuple[_Record, ...]:
        root_fd = self._open_root()
        try:
            directory_fd = self._open_directory(root_fd, kind, create=False)
            if directory_fd is None:
                return ()
            try:
                records: list[_Record] = []
                for name in sorted(os.listdir(directory_fd)):
                    path = self.root / kind / name
                    fd = self._open_private_file(directory_fd, name, path)
                    try:
                        raw = _read_all(fd)
                    finally:
                        os.close(fd)
                    digest = hashlib.sha256(raw).hexdigest()
                    try:
                        if not name.endswith(".json"):
                            raise ValueError("unexpected state filename")
                        decoded = json.loads(raw.decode("utf-8"))
                        record = parser(decoded)
                        if name != key_for(record) + ".json":
                            raise ValueError("state filename does not match record identity")
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                        quarantine = None
                        if not self.read_only:
                            quarantine = self._quarantine_locked(root_fd, directory_fd, kind, name, raw)
                        raise StateCorruption(path, digest, quarantine_path=quarantine) from None
                    records.append(record)
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
        destination = f"{kind}-{digest}.json"
        destination_path = self.root / "corrupt" / destination
        try:
            try:
                existing_fd = self._open_private_file(quarantine_fd, destination, destination_path)
            except FileNotFoundError:
                existing_fd = None
            if existing_fd is None:
                os.replace(name, destination, src_dir_fd=source_fd, dst_dir_fd=quarantine_fd)
            else:
                try:
                    existing_raw = _read_all(existing_fd)
                finally:
                    os.close(existing_fd)
                if existing_raw != raw:
                    raise StateCorruption(self.root / kind / name, digest)
                os.unlink(name, dir_fd=source_fd)
            os.fsync(source_fd)
            os.fsync(quarantine_fd)
            return destination_path
        finally:
            os.close(quarantine_fd)

    def append_event(self, event: dict[str, object]) -> None:
        if not isinstance(event, dict):
            raise ValueError("event must be a record")
        unknown = set(event) - EVENT_FIELDS
        if unknown:
            raise ValueError("event contains forbidden fields")
        if "session_id" in event:
            model.validate_session_id(event["session_id"])
        record = _canonical_json(event)
        if len(record) > EVENT_LOG_MAX_BYTES:
            raise ValueError("event record exceeds maximum log size")
        self._require_mutable()
        with self.locked():
            self._maintenance_locked()
            root_fd = self._open_root()
            try:
                self._append_event_locked(root_fd, record)
            finally:
                os.close(root_fd)

    def _append_event_locked(self, root_fd: int, record: bytes) -> None:
        name = "events.jsonl"
        path = self.root / name
        size = 0
        try:
            current_fd = self._open_private_file(root_fd, name, path)
        except FileNotFoundError:
            current_fd = None
        if current_fd is not None:
            try:
                size = os.fstat(current_fd).st_size
            finally:
                os.close(current_fd)
        if size and size + len(record) > EVENT_LOG_MAX_BYTES:
            self._rotate_events_locked(root_fd)
        try:
            fd = self._open_private_file(root_fd, name, path, os.O_WRONLY | os.O_APPEND)
        except FileNotFoundError:
            flags = os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK
            flags |= os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, _FILE_MODE, dir_fd=root_fd)
            os.fchmod(fd, _FILE_MODE)
        try:
            _validate_file(os.fstat(fd), path)
            _write_all(fd, record)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(root_fd)

    def _validate_named_file_if_present(self, directory_fd: int, name: str) -> bool:
        try:
            fd = self._open_private_file(directory_fd, name, self.root / name)
        except FileNotFoundError:
            return False
        os.close(fd)
        return True

    def _rotate_events_locked(self, root_fd: int) -> None:
        oldest = f"events.jsonl.{EVENT_LOG_BACKUPS}"
        if self._validate_named_file_if_present(root_fd, oldest):
            os.unlink(oldest, dir_fd=root_fd)
        for number in range(EVENT_LOG_BACKUPS - 1, 0, -1):
            source = f"events.jsonl.{number}"
            destination = f"events.jsonl.{number + 1}"
            if not self._validate_named_file_if_present(root_fd, source):
                continue
            self._validate_named_file_if_present(root_fd, destination)
            os.replace(source, destination, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        if self._validate_named_file_if_present(root_fd, "events.jsonl"):
            self._validate_named_file_if_present(root_fd, "events.jsonl.1")
            os.replace("events.jsonl", "events.jsonl.1", src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)

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
            transactions_fd = self._open_directory(root_fd, "transactions", create=False)
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
                if reference is not None and any(name == reference for _, name in candidates):
                    keep.add(reference)
                for _, name in candidates:
                    if len(keep) >= TRANSACTION_RETENTION:
                        break
                    keep.add(name)
                for _, name in candidates:
                    if name not in keep:
                        self._remove_private_tree(transactions_fd, name, self.root / "transactions" / name)
                os.fsync(transactions_fd)
            finally:
                os.close(transactions_fd)
        finally:
            os.close(root_fd)

    def _remove_private_tree(self, parent_fd: int, name: str, path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
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
