"""Private descriptor-anchored storage primitives for tribunal review artifacts."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
import stat
import subprocess
from typing import Iterator

from .model import SchemaError


def repository_root(cwd: Path) -> Path:
    try:
        raw = os.fspath(cwd)
        result = subprocess.run(
            ["/usr/bin/git", "-C", raw, "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 16 * 1024:
            raise SchemaError("NOT_GIT_REPOSITORY")
        root = Path(result.stdout.decode("utf-8", "strict").rstrip("\n")).resolve(
            strict=True
        )
        physical = Path(raw).resolve(strict=True)
        if not physical.is_relative_to(root):
            raise SchemaError("NOT_GIT_REPOSITORY")
        return root
    except SchemaError:
        raise
    except (OSError, UnicodeError, subprocess.SubprocessError, ValueError):
        raise SchemaError("NOT_GIT_REPOSITORY") from None


def check_ignored(root: Path) -> None:
    try:
        result = subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(root),
                "check-ignore",
                "-q",
                "--",
                ".review/verdict.json",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise SchemaError("VERDICT_NOT_IGNORED") from None
    if result.returncode != 0:
        raise SchemaError("VERDICT_NOT_IGNORED")


def preflight_review_directory(root: Path) -> None:
    root_fd = -1
    review_fd = -1
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            review_fd = os.open(
                ".review",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return
        except OSError:
            raise SchemaError("REVIEW_DIRECTORY_UNSAFE") from None
        safe_directory(review_fd, "REVIEW_DIRECTORY_UNSAFE")
    finally:
        for fd in (review_fd, root_fd):
            if fd >= 0:
                os.close(fd)


def safe_directory(fd: int, code: str) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise SchemaError(code)


def safe_file(
    fd: int, code: str, *, exact_mode: int | None = None
) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or (
            exact_mode is not None
            and stat.S_IMODE(info.st_mode) != exact_mode
        )
    ):
        raise SchemaError(code)
    return info


def open_directory(parent_fd: int, name: str, *, create: bool, code: str) -> int:
    created = False
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        except OSError:
            raise SchemaError(code) from None
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
    except OSError:
        raise SchemaError(code) from None
    try:
        if created:
            os.fchmod(fd, 0o700)
        safe_directory(fd, code)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def locked_review(root: Path, *, create: bool) -> Iterator[int]:
    root_fd = -1
    review_fd = -1
    lock_fd = -1
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid():
            raise SchemaError("REPOSITORY_DIRECTORY_UNSAFE")
        review_fd = open_directory(
            root_fd, ".review", create=create, code="REVIEW_DIRECTORY_UNSAFE"
        )
        flags = os.O_RDWR | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT
        try:
            lock_fd = os.open("lock", flags, 0o600, dir_fd=review_fd)
            safe_file(lock_fd, "LOCK_FILE_UNSAFE")
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SchemaError("STORE_LOCKED") from None
        except SchemaError:
            raise
        except OSError:
            raise SchemaError("LOCK_FILE_UNSAFE") from None
        yield review_fd
    finally:
        for fd in (lock_fd, review_fd, root_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def read_named_file(
    parent_fd: int,
    name: str,
    *,
    maximum: int,
    missing: str,
    unsafe: str,
    exact_mode: int | None = None,
) -> bytes:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
        )
    except FileNotFoundError:
        raise SchemaError(missing) from None
    except OSError:
        raise SchemaError(unsafe) from None
    try:
        info = safe_file(fd, unsafe, exact_mode=exact_mode)
        if info.st_size > maximum:
            raise SchemaError("FILE_TOO_LARGE")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, min(65536, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise SchemaError("FILE_TOO_LARGE")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_private_temporary(
    parent_fd: int,
    raw: bytes,
    *,
    unsafe: str,
    exact_mode: int,
    write_failed: str | None = None,
) -> tuple[str, os.stat_result]:
    temporary = f".tmp.{os.getpid()}.{secrets.token_hex(8)}"
    fd = -1
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(fd, exact_mode)
        info = safe_file(fd, unsafe, exact_mode=exact_mode)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
        return temporary, info
    except SchemaError:
        _remove_temporary(parent_fd, temporary)
        raise
    except OSError:
        _remove_temporary(parent_fd, temporary)
        raise SchemaError(write_failed or unsafe) from None
    finally:
        if fd >= 0:
            os.close(fd)


def _remove_temporary(parent_fd: int, temporary: str) -> None:
    try:
        os.unlink(temporary, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _existing_target_is_safe(
    parent_fd: int, name: str, *, unsafe: str, exact_mode: int | None = None
) -> bool:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
        )
    except FileNotFoundError:
        return False
    except OSError:
        raise SchemaError(unsafe) from None
    try:
        safe_file(fd, unsafe, exact_mode=exact_mode)
        return True
    finally:
        os.close(fd)


def atomic_replace_bytes(
    parent_fd: int,
    name: str,
    raw: bytes,
    *,
    maximum: int,
    too_large: str,
    unsafe: str,
    exact_mode: int = 0o600,
    write_failed: str | None = None,
) -> None:
    if len(raw) > maximum:
        raise SchemaError(too_large)
    temporary = ""
    try:
        safe_directory(parent_fd, unsafe)
        _existing_target_is_safe(parent_fd, name, unsafe=unsafe)
    except SchemaError:
        raise
    except OSError:
        raise SchemaError(unsafe) from None
    try:
        temporary, _info = _write_private_temporary(
            parent_fd,
            raw,
            unsafe=unsafe,
            exact_mode=exact_mode,
            write_failed=write_failed,
        )
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary = ""
        os.fsync(parent_fd)
    except SchemaError:
        raise
    except OSError:
        raise SchemaError(write_failed or unsafe) from None
    finally:
        if temporary:
            _remove_temporary(parent_fd, temporary)


def _unlink_created_target(
    parent_fd: int, name: str, published: os.stat_result
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) == (
            published.st_dev,
            published.st_ino,
        ):
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except OSError:
        pass


def atomic_create_bytes(
    parent_fd: int,
    name: str,
    raw: bytes,
    *,
    maximum: int,
    exists: str,
    unsafe: str,
    exact_mode: int = 0o600,
) -> str:
    if len(raw) > maximum:
        raise SchemaError(unsafe)
    temporary = ""
    source: os.stat_result | None = None
    published: os.stat_result | None = None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        safe_directory(parent_fd, unsafe)
        if _existing_target_is_safe(parent_fd, name, unsafe=unsafe):
            raise SchemaError(exists)
        temporary, source = _write_private_temporary(
            parent_fd, raw, unsafe=unsafe, exact_mode=exact_mode
        )
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            if _existing_target_is_safe(parent_fd, name, unsafe=unsafe):
                raise SchemaError(exists) from None
            raise SchemaError(unsafe) from None
        published_fd = -1
        try:
            published_fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
            )
            published = safe_file(published_fd, unsafe, exact_mode=exact_mode)
        finally:
            if published_fd >= 0:
                os.close(published_fd)
        if source is None or (published.st_dev, published.st_ino) != (
            source.st_dev,
            source.st_ino,
        ):
            raise SchemaError(unsafe)
        os.unlink(temporary, dir_fd=parent_fd)
        temporary = ""
        os.fsync(parent_fd)
        fd = -1
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
            )
            final = safe_file(fd, unsafe, exact_mode=exact_mode)
            if published is None or (final.st_dev, final.st_ino) != (
                published.st_dev,
                published.st_ino,
            ):
                raise SchemaError(unsafe)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(fd, min(65536, maximum + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > maximum:
                    raise SchemaError(unsafe)
            if hashlib.sha256(b"".join(chunks)).hexdigest() != digest:
                raise SchemaError(unsafe)
        finally:
            if fd >= 0:
                os.close(fd)
        return digest
    except SchemaError:
        if published is not None:
            _unlink_created_target(parent_fd, name, published)
        raise
    except OSError:
        if published is not None:
            _unlink_created_target(parent_fd, name, published)
        raise SchemaError(unsafe) from None
    finally:
        if temporary:
            _remove_temporary(parent_fd, temporary)
