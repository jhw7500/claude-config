from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from typing import Callable, TypeVar


_T = TypeVar("_T")


class OperationDeadlineExceeded(RuntimeError):
    """The absolute deadline expired before the next bounded operation."""


class DirectoryCapacityExceeded(RuntimeError):
    """A bounded directory contains more entries than the caller permits."""


@dataclass(frozen=True)
class DeadlineBudget:
    deadline: float | None
    monotonic: Callable[[], float]

    def expired(self) -> bool:
        return self.deadline is not None and self.monotonic() >= self.deadline

    def check(self) -> None:
        if self.expired():
            raise OperationDeadlineExceeded("operation deadline exhausted")

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise OperationDeadlineExceeded("operation deadline exhausted")
        return remaining


class DeadlineIO:
    def __init__(self, budget: DeadlineBudget) -> None:
        self.budget = budget

    def _call(self, operation: Callable[..., _T], *args, **kwargs) -> _T:
        self.budget.check()
        result = operation(*args, **kwargs)
        self.budget.check()
        return result

    def open_fd(
        self,
        name: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        self.budget.check()
        fd = os.open(name, flags, mode, dir_fd=dir_fd)
        try:
            self.budget.check()
        except Exception:
            try:
                self.close_fd(fd)
            except OSError:
                pass
            raise
        return fd

    def dup_fd(self, fd: int) -> int:
        self.budget.check()
        duplicate = os.dup(fd)
        try:
            self.budget.check()
        except Exception:
            try:
                self.close_fd(duplicate)
            except OSError:
                pass
            raise
        return duplicate

    def read(self, fd: int, size: int) -> bytes:
        return self._call(os.read, fd, size)

    def write(self, fd: int, data: bytes) -> int:
        return self._call(os.write, fd, data)

    def lseek(self, fd: int, offset: int, whence: int) -> int:
        return self._call(os.lseek, fd, offset, whence)

    def flock_exclusive_nonblocking(self, fd: int) -> None:
        self.budget.check()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.budget.check()
        except Exception:
            try:
                self.unlock_fd(fd)
            except OSError:
                pass
            raise

    def fstat(self, fd: int) -> os.stat_result:
        return self._call(os.fstat, fd)

    def stat(
        self,
        name: str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = False,
    ) -> os.stat_result:
        return self._call(
            os.stat,
            name,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def mkdir(
        self,
        name: str,
        mode: int,
        *,
        dir_fd: int | None = None,
    ) -> None:
        self._call(os.mkdir, name, mode, dir_fd=dir_fd)

    def mkdir_private(
        self,
        name: str,
        mode: int,
        *,
        dir_fd: int | None = None,
    ) -> int:
        """Create and mode-normalize one Linux directory as a single boundary."""
        path_flag = getattr(os, "O_PATH", None)
        if path_flag is None:
            raise OSError("O_PATH is unavailable")
        self.budget.check()
        anchor_fd: int | None = None
        try:
            os.mkdir(name, mode, dir_fd=dir_fd)
            flags = path_flag | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            anchor_fd = os.open(name, flags, dir_fd=dir_fd)
            os.chmod(f"/proc/self/fd/{anchor_fd}", mode)
            self.budget.check()
        except Exception:
            if anchor_fd is not None:
                try:
                    os.close(anchor_fd)
                except OSError:
                    pass
            raise
        return anchor_fd

    def fchmod(self, fd: int, mode: int) -> None:
        self._call(os.fchmod, fd, mode)

    def fsync(self, fd: int) -> None:
        self._call(os.fsync, fd)

    def replace(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        self._call(
            os.replace,
            source,
            destination,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=destination_dir_fd,
        )

    def unlink(self, name: str, *, dir_fd: int) -> None:
        self._call(os.unlink, name, dir_fd=dir_fd)

    def directory_names(self, directory_fd: int, limit: int) -> tuple[str, ...]:
        self.budget.check()
        entries = os.scandir(directory_fd)
        try:
            self.budget.check()
            names = []
            iterator = iter(entries)
            while True:
                self.budget.check()
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                self.budget.check()
                if len(names) >= limit:
                    raise DirectoryCapacityExceeded("directory capacity exceeded")
                names.append(entry.name)
            self.budget.check()
            return tuple(sorted(names))
        finally:
            try:
                entries.close()
            except OSError:
                pass

    def close_fd(self, fd: int) -> None:
        os.close(fd)

    def unlock_fd(self, fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
