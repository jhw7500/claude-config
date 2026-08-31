import fcntl
import os

import pytest

from codex_mcp_ownership import deadline_io


class SequenceClock:
    def __init__(self, values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


def test_expired_budget_starts_no_boundary(tmp_path, monkeypatch):
    calls = []
    original = deadline_io.os.mkdir

    def record(*args, **kwargs):
        calls.append("mkdir")
        return original(*args, **kwargs)

    monkeypatch.setattr(deadline_io.os, "mkdir", record)
    io = deadline_io.DeadlineIO(deadline_io.DeadlineBudget(0.5, lambda: 1.0))
    with pytest.raises(deadline_io.OperationDeadlineExceeded):
        io.mkdir(os.fspath(tmp_path / "never-created"), 0o700)
    assert calls == []


def test_open_fd_closes_handle_when_deadline_crosses_after_open(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"{}\n")
    opened = []
    original = deadline_io.os.open

    def capture(*args, **kwargs):
        fd = original(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(deadline_io.os, "open", capture)
    io = deadline_io.DeadlineIO(
        deadline_io.DeadlineBudget(0.5, SequenceClock([0.0, 1.0]))
    )
    with pytest.raises(deadline_io.OperationDeadlineExceeded):
        io.open_fd(os.fspath(target), os.O_RDONLY)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_dup_fd_closes_duplicate_when_deadline_crosses_after_dup(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"{}\n")
    source_fd = os.open(target, os.O_RDONLY)
    duplicated = []
    original = deadline_io.os.dup

    def capture(fd):
        duplicate = original(fd)
        duplicated.append(duplicate)
        return duplicate

    monkeypatch.setattr(deadline_io.os, "dup", capture)
    io = deadline_io.DeadlineIO(
        deadline_io.DeadlineBudget(0.5, SequenceClock([0.0, 1.0]))
    )
    try:
        with pytest.raises(deadline_io.OperationDeadlineExceeded):
            io.dup_fd(source_fd)
        assert len(duplicated) == 1
        with pytest.raises(OSError):
            os.fstat(duplicated[0])
    finally:
        os.close(source_fd)


def test_flock_is_released_when_deadline_crosses_after_acquire(tmp_path):
    target = tmp_path / "state.lock"
    target.write_bytes(b"")
    first = os.open(target, os.O_RDWR)
    second = os.open(target, os.O_RDWR)
    io = deadline_io.DeadlineIO(
        deadline_io.DeadlineBudget(0.5, SequenceClock([0.0, 1.0]))
    )
    try:
        with pytest.raises(deadline_io.OperationDeadlineExceeded):
            io.flock_exclusive_nonblocking(first)
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(second, fcntl.LOCK_UN)
    finally:
        os.close(second)
        os.close(first)


def test_expired_lseek_starts_no_boundary(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"{}\n")
    fd = os.open(target, os.O_RDONLY)
    calls = []
    original = deadline_io.os.lseek

    def record(*args):
        calls.append("lseek")
        return original(*args)

    monkeypatch.setattr(deadline_io.os, "lseek", record)
    io = deadline_io.DeadlineIO(deadline_io.DeadlineBudget(0.5, lambda: 1.0))
    try:
        with pytest.raises(deadline_io.OperationDeadlineExceeded):
            io.lseek(fd, 0, os.SEEK_SET)
        assert calls == []
    finally:
        os.close(fd)


def test_directory_names_stops_before_next_entry_after_expiry(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        io = deadline_io.DeadlineIO(
            deadline_io.DeadlineBudget(
                0.5,
                SequenceClock([0.0, 0.0, 0.0, 1.0]),
            )
        )
        with pytest.raises(deadline_io.OperationDeadlineExceeded):
            io.directory_names(fd, 8)
    finally:
        os.close(fd)


def test_directory_names_rejects_capacity_overflow(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        io = deadline_io.DeadlineIO(deadline_io.DeadlineBudget(None, lambda: 0.0))
        with pytest.raises(deadline_io.DirectoryCapacityExceeded):
            io.directory_names(fd, 2)
    finally:
        os.close(fd)
