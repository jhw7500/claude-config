from __future__ import annotations

import os
from pathlib import Path

from codex_mcp_ownership import procfs


class FakeClock:
    def __init__(self, wall: str = "2026-08-29T00:00:00+00:00", boot: float = 0.0) -> None:
        self._wall = wall
        self._boot = boot

    def wall_iso(self) -> str:
        return self._wall

    def boottime(self) -> float:
        return self._boot

    def advance(self, seconds: float) -> None:
        self._boot += seconds


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
        stat_path.write_text(raw[: right + 1] + " " + " ".join(fields) + "\n", encoding="utf-8")

    def rss_kib(self, identity):
        return procfs.LinuxProcfs(self.root, self.boot_id_path).rss_kib(identity)
