from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
from typing import Literal

from .model import ProcessIdentity


class ProcfsFormatError(ValueError):
    """Raised when procfs content cannot establish an exact identity."""


@dataclass(frozen=True)
class ProcStat:
    pid: int
    ppid: int
    pgid: int
    start_ticks: int


@dataclass(frozen=True)
class IdentityObservation:
    kind: Literal["live", "missing", "unavailable"]
    identity: ProcessIdentity | None


def parse_stat(raw: str) -> ProcStat:
    left = raw.find("(")
    right = raw.rfind(")")
    if left <= 0 or right <= left:
        raise ProcfsFormatError("invalid proc stat framing")
    fields = raw[right + 1 :].strip().split()
    if len(fields) < 20:
        raise ProcfsFormatError("incomplete proc stat")
    try:
        pid_text = raw[:left].strip()
        if not pid_text.isascii() or not pid_text.isdecimal():
            raise ValueError("invalid pid")
        pid = int(pid_text)
        if pid < 1:
            raise ValueError("invalid pid")
        return ProcStat(
            pid=pid,
            ppid=int(fields[1]),
            pgid=int(fields[2]),
            start_ticks=int(fields[19]),
        )
    except ValueError as error:
        raise ProcfsFormatError("non-integer proc stat field") from error


class LinuxProcfs:
    def __init__(
        self,
        proc_root: Path | str = "/proc",
        boot_id_path: Path | str = "/proc/sys/kernel/random/boot_id",
    ) -> None:
        self.proc_root = Path(proc_root)
        self.boot_id_path = Path(boot_id_path)

    @staticmethod
    def _read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def _boot_id(self) -> str | None:
        try:
            boot_id = self._read_text(self.boot_id_path).strip()
        except OSError:
            return None
        return boot_id or None

    def boot_id(self) -> str | None:
        """Return the current kernel boot ID without inspecting a process."""
        return self._boot_id()

    def identity(self, pid: int) -> ProcessIdentity | None:
        if type(pid) is not int or pid < 1:
            return None
        base = self.proc_root / str(pid)
        try:
            boot_id = self._boot_id()
            if boot_id is None:
                return None
            before = parse_stat(self._read_text(base / "stat"))
            exe_stat = (base / "exe").stat()
            exe_target = os.readlink(base / "exe")
            after = parse_stat(self._read_text(base / "stat"))
        except FileNotFoundError:
            return None
        except ProcessLookupError:
            return None
        except OSError:
            return None
        if before != after or before.pid != pid or after.pid != pid:
            return None
        return ProcessIdentity(
            boot_id=boot_id,
            pid=pid,
            ppid=before.ppid,
            pgid=before.pgid,
            start_ticks=before.start_ticks,
            exe_dev=exe_stat.st_dev,
            exe_ino=exe_stat.st_ino,
            exe_name=Path(exe_target).name,
        )

    def observe_identity(self, pid: int) -> IdentityObservation:
        if type(pid) is not int or pid < 1:
            return IdentityObservation("unavailable", None)
        base = self.proc_root / str(pid)
        try:
            boot_id = self._read_text(self.boot_id_path).strip()
        except OSError:
            return IdentityObservation("unavailable", None)
        if not boot_id:
            return IdentityObservation("unavailable", None)
        try:
            before = parse_stat(self._read_text(base / "stat"))
        except (FileNotFoundError, ProcessLookupError):
            return IdentityObservation("missing", None)
        except (OSError, ProcfsFormatError):
            return IdentityObservation("unavailable", None)
        try:
            exe_stat = (base / "exe").stat()
            exe_target = os.readlink(base / "exe")
        except (OSError, ProcfsFormatError):
            return IdentityObservation("unavailable", None)
        try:
            after = parse_stat(self._read_text(base / "stat"))
        except (FileNotFoundError, ProcessLookupError):
            return IdentityObservation("missing", None)
        except (OSError, ProcfsFormatError):
            return IdentityObservation("unavailable", None)
        if before != after or before.pid != pid or after.pid != pid:
            return IdentityObservation("unavailable", None)
        return IdentityObservation(
            "live",
            ProcessIdentity(
                boot_id=boot_id,
                pid=pid,
                ppid=before.ppid,
                pgid=before.pgid,
                start_ticks=before.start_ticks,
                exe_dev=exe_stat.st_dev,
                exe_ino=exe_stat.st_ino,
                exe_name=Path(exe_target).name,
            ),
        )

    def ancestor_chain(self, pid: int) -> tuple[ProcessIdentity, ...]:
        chain: list[ProcessIdentity] = []
        visited: set[int] = set()
        next_pid = pid
        for _ in range(128):
            if next_pid in visited:
                break
            current = self.identity(next_pid)
            if current is None:
                break
            chain.append(current)
            visited.add(current.pid)
            if current.ppid < 1:
                break
            next_pid = current.ppid
        return tuple(chain)

    def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]:
        if type(pgid) is not int:
            return ()
        try:
            entries = sorted(
                (entry for entry in self.proc_root.iterdir() if entry.name.isdecimal()),
                key=lambda entry: int(entry.name),
            )
        except OSError:
            return ()
        members: list[ProcessIdentity] = []
        for entry in entries:
            identity = self.identity(int(entry.name))
            if identity is not None and identity.pgid == pgid:
                members.append(identity)
        return tuple(members)

    def rss_kib(self, identity: ProcessIdentity) -> int | None:
        if not isinstance(identity, ProcessIdentity):
            return None
        if self.identity(identity.pid) != identity:
            return None
        try:
            status = self._read_text(self.proc_root / str(identity.pid) / "status")
        except OSError:
            return None
        match = re.search(r"^VmRSS:\s*(\d+)\s+kB\s*$", status, re.MULTILINE)
        if match is None:
            return None
        if self.identity(identity.pid) != identity:
            return None
        return int(match.group(1))

    def open_pidfd(self, identity: ProcessIdentity) -> int:
        if not isinstance(identity, ProcessIdentity) or self.identity(identity.pid) != identity:
            raise ProcessLookupError(errno.ESRCH, "process identity is no longer live")
        pidfd_open = getattr(os, "pidfd_open", None)
        if pidfd_open is None:
            raise OSError(errno.ENOSYS, "pidfd_open is unavailable")
        descriptor = pidfd_open(identity.pid, 0)
        try:
            if self.identity(identity.pid) == identity:
                return descriptor
            raise ProcessLookupError(errno.ESRCH, "process identity changed while opening pidfd")
        except BaseException:
            os.close(descriptor)
            raise
