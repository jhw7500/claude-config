"""No-follow, descriptor-relative checkpoint storage."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
from pathlib import Path

from .codec import (
    MAX_MANIFEST_BYTES,
    CheckpointError,
    CheckpointIndeterminateError,
    _HEX32,
    _HEX64,
    _reject_constant,
    _reject_duplicate_keys,
    _relative_ref,
    canonical_bytes,
    decode_snapshot,
    encode_snapshot,
    sha256_bytes,
)


MAX_RAW_LOG_BYTES = 16 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", None)
_DIRECTORY = getattr(os, "O_DIRECTORY", None)


def _require_secure_io() -> None:
    if (
        not isinstance(_NOFOLLOW, int)
        or _NOFOLLOW == 0
        or not isinstance(_DIRECTORY, int)
        or _DIRECTORY == 0
    ):
        raise CheckpointError("required no-follow directory I/O is unsupported")
    required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink, os.link, os.rename)
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        raise CheckpointError("required descriptor-relative I/O is unsupported")
    required_nofollow = (os.stat, os.link)
    if any(function not in os.supports_follow_symlinks for function in required_nofollow):
        raise CheckpointError("required no-follow metadata I/O is unsupported")


def _check_owned_directory(fd: int, *, private: bool) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise CheckpointError("directory must be owned by the current user")
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        raise CheckpointError("state directory must have mode 0700")


def _open_directory(path: Path, *, private: bool) -> int:
    _require_secure_io()
    try:
        fd = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError as exc:
        raise CheckpointError(f"cannot open safe directory: {path}") from exc
    try:
        _check_owned_directory(fd, private=private)
    except Exception:
        os.close(fd)
        raise
    return fd


def _open_or_create_directory(parent_fd: int, name: str) -> int:
    _require_secure_io()
    if not name or "/" in name or name in {".", ".."}:
        raise CheckpointError("unsafe directory component")
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise CheckpointError("cannot create private state directory") from exc
    try:
        fd = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise CheckpointError("cannot open private state directory") from exc
    try:
        if created:
            os.fchmod(fd, 0o700)
        _check_owned_directory(fd, private=True)
        os.fsync(fd)
        os.fsync(parent_fd)
    except OSError as exc:
        os.close(fd)
        raise CheckpointError("cannot durably create private state directory") from exc
    except Exception:
        os.close(fd)
        raise
    return fd


def _open_private_directory(parent_fd: int, name: str) -> int:
    _require_secure_io()
    if not name or "/" in name or name in {".", ".."}:
        raise CheckpointError("unsafe directory component")
    try:
        fd = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise CheckpointError("cannot open private state directory") from exc
    try:
        _check_owned_directory(fd, private=True)
    except Exception:
        os.close(fd)
        raise
    return fd


def _check_owned_file(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise CheckpointError("file must be a current-user-owned 0600 regular file")


def _read_file_at(directory_fd: int, name: str, limit: int) -> bytes:
    _require_secure_io()
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_fd)
    except OSError as exc:
        raise CheckpointError("cannot open safe regular file") from exc
    try:
        _check_owned_file(fd)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise CheckpointError("file exceeds its size limit")
        return data
    finally:
        os.close(fd)


def _existing_file_bytes(directory_fd: int, name: str, limit: int) -> bytes | None:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CheckpointError("cannot inspect file") from exc
    return _read_file_at(directory_fd, name, limit)


def _atomic_replace(directory_fd: int, name: str, data: bytes) -> None:
    _require_secure_io()
    temporary = f".{name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    fd = -1
    published = False
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(fd, 0o600)
        _check_owned_file(fd)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise CheckpointError("short write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        published = True
        os.fsync(directory_fd)
    except CheckpointError:
        raise
    except OSError as exc:
        if published:
            raise CheckpointIndeterminateError(
                "snapshot publish durability is indeterminate"
            ) from exc
        raise CheckpointError("atomic file replacement failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _open_lock(directory_fd: int, name: str) -> int:
    _require_secure_io()
    try:
        fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | _NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise CheckpointError("cannot open snapshot lock") from exc
    try:
        os.fchmod(fd, 0o600)
        _check_owned_file(fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except Exception:
        os.close(fd)
        raise
    return fd


def _lock_directory(directory_fd: int) -> int:
    lock_fd = -1
    try:
        lock_fd = os.dup(directory_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as exc:
        if lock_fd >= 0:
            os.close(lock_fd)
        raise CheckpointError("cannot lock snapshot directory") from exc
    return lock_fd


def read_snapshot(path: str | os.PathLike[str]) -> dict:
    _require_secure_io()
    snapshot_path = Path(path)
    if not snapshot_path.name or snapshot_path.name in {".", ".."}:
        raise CheckpointError("invalid snapshot path")
    directory_fd = _open_directory(snapshot_path.parent, private=False)
    try:
        return decode_snapshot(_read_file_at(directory_fd, snapshot_path.name, 12 * 1024))
    finally:
        os.close(directory_fd)


def write_snapshot(
    path: str | os.PathLike[str], snapshot: dict, expected_generation: int | None
) -> dict:
    _require_secure_io()
    snapshot_path = Path(path)
    directory_fd = _open_directory(snapshot_path.parent, private=False)
    lock_fd = -1
    try:
        lock_fd = _lock_directory(directory_fd)
        current_bytes = _existing_file_bytes(directory_fd, snapshot_path.name, 12 * 1024)
        stored = dict(snapshot)
        if current_bytes is None:
            if expected_generation is not None or snapshot.get("generation") != 0:
                raise CheckpointError("new snapshot requires expected_generation=None and generation 0")
            stored["generation"] = 0
        else:
            current = decode_snapshot(current_bytes)
            if type(expected_generation) is not int or current["generation"] != expected_generation:
                raise CheckpointError("stale snapshot generation")
            stored["generation"] = expected_generation + 1
        encoded = encode_snapshot(stored)
        _atomic_replace(directory_fd, snapshot_path.name, encoded)
        return stored
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(directory_fd)


def _state_directories(
    state_root: Path, run_id: str, *, create: bool
) -> tuple[int, int, int, int]:
    if not state_root.is_absolute() or _HEX32.fullmatch(run_id) is None:
        raise CheckpointError("state_root must be absolute and run_id lowercase hex")
    root_fd = _open_directory(state_root, private=True)
    backlog_fd = run_fd = evidence_fd = -1
    try:
        opener = _open_or_create_directory if create else _open_private_directory
        backlog_fd = opener(root_fd, "backlog-drain")
        run_fd = opener(backlog_fd, run_id)
        evidence_fd = opener(run_fd, "evidence")
        return root_fd, backlog_fd, run_fd, evidence_fd
    except Exception:
        for fd in (evidence_fd, run_fd, backlog_fd, root_fd):
            if fd >= 0:
                os.close(fd)
        raise


def _close_directories(fds: tuple[int, int, int, int]) -> None:
    for fd in reversed(fds):
        os.close(fd)


def write_evidence(
    state_root: str | os.PathLike[str], run_id: str, value: dict
) -> dict:
    _require_secure_io()
    if not isinstance(value, dict):
        raise CheckpointError("evidence must be a JSON object")
    data = canonical_bytes(value)
    if len(data) > MAX_MANIFEST_BYTES:
        raise CheckpointError("evidence manifest exceeds 1 MiB")
    digest = sha256_bytes(data)
    name = f"{digest}.json"
    fds = _state_directories(Path(state_root), run_id, create=True)
    try:
        existing = _existing_file_bytes(fds[3], name, MAX_MANIFEST_BYTES)
        if existing is None:
            temporary = f".{name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
            fd = -1
            try:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o600,
                    dir_fd=fds[3],
                )
                os.fchmod(fd, 0o600)
                _check_owned_file(fd)
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise CheckpointError("short evidence write")
                    view = view[written:]
                os.fsync(fd)
                os.close(fd)
                fd = -1
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=fds[3],
                        dst_dir_fd=fds[3],
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    pass
                try:
                    os.fsync(fds[3])
                except OSError as exc:
                    raise CheckpointIndeterminateError(
                        "evidence publish durability is indeterminate"
                    ) from exc
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temporary, dir_fd=fds[3])
                except FileNotFoundError:
                    pass
            existing = _read_file_at(fds[3], name, MAX_MANIFEST_BYTES)
        if existing != data:
            raise CheckpointError("immutable evidence content does not match its digest name")
    finally:
        _close_directories(fds)
    return {"ref": f"evidence/{name}", "sha256": digest, "size": len(data)}


def _validate_read_reference(reference: object) -> tuple[str, str, int]:
    if not isinstance(reference, dict) or set(reference) != {"ref", "sha256", "size"}:
        raise CheckpointError("evidence reference has missing or unknown keys")
    ref = _relative_ref(reference["ref"], "evidence.ref")
    digest = reference["sha256"]
    size = reference["size"]
    if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
        raise CheckpointError("evidence digest is invalid")
    if type(size) is not int or size < 0 or size > MAX_MANIFEST_BYTES:
        raise CheckpointError("evidence size is invalid")
    parts = ref.split("/")
    if len(parts) != 2 or parts[0] != "evidence" or parts[1] != f"{digest}.json":
        raise CheckpointError("evidence reference must be below evidence/")
    return parts[1], digest, size


def _read_relative_file(root_fd: int, reference: str, limit: int) -> bytes:
    parts = reference.split("/")
    directory_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = _open_private_directory(directory_fd, part)
            os.close(directory_fd)
            directory_fd = next_fd
        return _read_file_at(directory_fd, parts[-1], limit)
    finally:
        os.close(directory_fd)


def _validate_raw_log_references(value: object, run_fd: int) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        for key, item in current.items():
            if key not in {"raw_log", "raw_log_ref"}:
                stack.append(item)
                continue
            if not isinstance(item, dict) or set(item) != {"ref", "sha256", "size"}:
                raise CheckpointError("raw log reference has missing or unknown keys")
            reference = _relative_ref(item["ref"], "raw_log.ref")
            digest = item["sha256"]
            size = item["size"]
            if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
                raise CheckpointError("raw log digest is invalid")
            if type(size) is not int or size < 0 or size > MAX_RAW_LOG_BYTES:
                raise CheckpointError("raw log size is invalid")
            data = _read_relative_file(run_fd, reference, MAX_RAW_LOG_BYTES)
            if len(data) != size or sha256_bytes(data) != digest:
                raise CheckpointError("raw log size or digest does not match")


def read_evidence(
    state_root: str | os.PathLike[str], run_id: str, reference: dict
) -> dict:
    _require_secure_io()
    name, digest, size = _validate_read_reference(reference)
    fds = _state_directories(Path(state_root), run_id, create=False)
    try:
        data = _read_file_at(fds[3], name, MAX_MANIFEST_BYTES)
        if len(data) != size or sha256_bytes(data) != digest:
            raise CheckpointError("evidence size or digest does not match")
        try:
            value = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except CheckpointError:
            raise
        except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
            raise CheckpointError("evidence JSON is invalid") from exc
        if not isinstance(value, dict) or canonical_bytes(value) != data:
            raise CheckpointError("evidence is not a canonical JSON object")
        _validate_raw_log_references(value, fds[2])
        return value
    finally:
        _close_directories(fds)
