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


def test_mkdir_private_normalizes_mode_under_restrictive_umask(tmp_path):
    target = tmp_path / "private"
    io = deadline_io.DeadlineIO(deadline_io.DeadlineBudget(None, lambda: 0.0))
    previous_umask = os.umask(0o777)
    anchor_fd = None
    try:
        anchor_fd = io.mkdir_private(os.fspath(target), 0o700)
    finally:
        os.umask(previous_umask)
    try:
        value = os.stat(target, follow_symlinks=False)
        anchored = os.fstat(anchor_fd)
        assert value.st_mode & 0o777 == 0o700
        assert (anchored.st_dev, anchored.st_ino) == (value.st_dev, value.st_ino)
    finally:
        os.close(anchor_fd)


def test_mkdir_private_finishes_anchored_mode_before_reporting_expiry(
    tmp_path, monkeypatch
):
    target = tmp_path / "private"
    now = [0.0]
    boundaries = []
    anchors = []
    original_mkdir = deadline_io.os.mkdir
    original_open = deadline_io.os.open
    original_chmod = deadline_io.os.chmod
    original_close = deadline_io.os.close

    def mkdir_then_expire(*args, **kwargs):
        result = original_mkdir(*args, **kwargs)
        boundaries.append("mkdir")
        now[0] = 1.0
        return result

    def capture_anchor(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        boundaries.append("anchor")
        anchors.append(fd)
        return fd

    def capture_chmod(*args, **kwargs):
        boundaries.append("chmod")
        return original_chmod(*args, **kwargs)

    def capture_close(fd):
        boundaries.append("close")
        return original_close(fd)

    monkeypatch.setattr(deadline_io.os, "mkdir", mkdir_then_expire)
    monkeypatch.setattr(deadline_io.os, "open", capture_anchor)
    monkeypatch.setattr(deadline_io.os, "chmod", capture_chmod)
    monkeypatch.setattr(deadline_io.os, "close", capture_close)
    io = deadline_io.DeadlineIO(deadline_io.DeadlineBudget(0.5, lambda: now[0]))

    with pytest.raises(deadline_io.OperationDeadlineExceeded):
        io.mkdir_private(os.fspath(target), 0o700)

    assert target.stat().st_mode & 0o777 == 0o700
    assert boundaries == ["mkdir", "anchor", "chmod", "close"]
    assert len(anchors) == 1
    with pytest.raises(OSError):
        os.fstat(anchors[0])


def test_mkdir_private_closes_anchor_when_mode_finalization_fails(
    tmp_path, monkeypatch
):
    target = tmp_path / "private"
    anchors = []
    original_open = deadline_io.os.open

    def capture_anchor(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        anchors.append(fd)
        return fd

    monkeypatch.setattr(deadline_io.os, "open", capture_anchor)
    monkeypatch.setattr(
        deadline_io.os,
        "chmod",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("chmod failed")),
    )
    io = deadline_io.DeadlineIO(deadline_io.DeadlineBudget(None, lambda: 0.0))

    with pytest.raises(OSError, match="chmod failed"):
        io.mkdir_private(os.fspath(target), 0o700)

    assert len(anchors) == 1
    with pytest.raises(OSError):
        os.fstat(anchors[0])


def test_rename_noreplace_reports_expiry_after_typed_boundary(monkeypatch):
    now = [0.0]
    calls = []

    def rename_then_expire(source_fd, source, destination_fd, destination):
        calls.append((source_fd, source, destination_fd, destination))
        now[0] = 1.0

    monkeypatch.setattr(
        deadline_io,
        "_rename_noreplace",
        rename_then_expire,
        raising=False,
    )
    io = deadline_io.DeadlineIO(deadline_io.DeadlineBudget(0.5, lambda: now[0]))

    with pytest.raises(deadline_io.OperationDeadlineExceeded):
        io.rename_noreplace(
            "source",
            "destination",
            source_dir_fd=11,
            destination_dir_fd=22,
        )

    assert calls == [(11, "source", 22, "destination")]


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
