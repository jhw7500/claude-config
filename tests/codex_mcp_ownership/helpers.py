from __future__ import annotations

import os
from pathlib import Path

from codex_mcp_ownership import model, procfs


class FakeClock:
    def __init__(
        self, wall: str = "2026-08-29T00:00:00+00:00", boot: float = 0.0
    ) -> None:
        self._wall = wall
        self._boot = boot

    def wall_iso(self) -> str:
        return self._wall

    def boottime(self) -> float:
        return self._boot

    def advance(self, seconds: float) -> None:
        self._boot += seconds


def sample_identity() -> model.ProcessIdentity:
    return model.ProcessIdentity(
        boot_id="test-boot-id",
        pid=321,
        ppid=1,
        pgid=321,
        start_ticks=424242,
        exe_dev=8,
        exe_ino=12345,
        exe_name="node",
    )


def sample_lease(session_id: str = "session:test_1") -> model.SessionLease:
    identity = sample_identity()
    observed = model.ObservedTime("2026-08-29T00:00:00+00:00", identity.boot_id, 12.5)
    return model.SessionLease(
        1,
        session_id,
        "/workspace",
        "SessionStart",
        (identity.stable_key(),),
        "active",
        observed,
    )


def sample_process() -> model.ManagedProcess:
    identity = sample_identity()
    observed = model.ObservedTime("2026-08-29T00:00:00+00:00", identity.boot_id, 12.5)
    lease = sample_lease()
    return model.ManagedProcess(
        1,
        identity.stable_key(),
        "user",
        "example",
        "/workspace",
        identity,
        None,
        (identity,),
        identity.pgid,
        frozenset({identity.stable_key()}),
        observed,
        owner_session_id="session:test_1",
        owner_generation=model.lease_generation_digest(lease),
    )


def make_private_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)


def write_private_file(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def write_proc_entry(
    root: Path,
    pid: int,
    stat_line: str,
    exe_target: Path,
    status_text: str = "VmRSS: 0 kB\n",
) -> None:
    base = root / str(pid)
    base.mkdir(parents=True, exist_ok=True)
    (base / "stat").write_text(stat_line, encoding="utf-8")
    (base / "status").write_text(status_text, encoding="utf-8")
    if (base / "exe").exists() or (base / "exe").is_symlink():
        (base / "exe").unlink()
    os.symlink(exe_target, base / "exe")


class FakeProcTree:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir()
        self.boot_id_path = self.root / "boot_id"
        self.boot_id_path.write_text("test-boot-id\n", encoding="utf-8")
        self.exe = self.root / "node"
        self.exe.write_text("node", encoding="utf-8")
        write_proc_entry(
            self.root,
            321,
            "321 (node worker) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 424242 0 0\n",
            self.exe,
            "Name:\tnode\nVmRSS:\t128 kB\n",
        )

    def identity(self, pid: int):
        return procfs.LinuxProcfs(self.root, self.boot_id_path).identity(pid)

    def write_start_ticks(self, pid: int, value: int) -> None:
        stat_path = self.root / str(pid) / "stat"
        raw = stat_path.read_text(encoding="utf-8")
        right = raw.rfind(")")
        fields = raw[right + 1 :].strip().split()
        fields[19] = str(value)
        stat_path.write_text(
            raw[: right + 1] + " " + " ".join(fields) + "\n", encoding="utf-8"
        )

    def write_ppid(self, pid: int, value: int) -> None:
        """Reparent the fake process — what the kernel does when a parent exits."""
        stat_path = self.root / str(pid) / "stat"
        raw = stat_path.read_text(encoding="utf-8")
        right = raw.rfind(")")
        fields = raw[right + 1 :].strip().split()
        fields[1] = str(value)
        stat_path.write_text(
            raw[: right + 1] + " " + " ".join(fields) + "\n", encoding="utf-8"
        )

    def rss_kib(self, identity):
        return procfs.LinuxProcfs(self.root, self.boot_id_path).rss_kib(identity)
