import os
from pathlib import Path
import stat

import pytest

from pre_pr_tribunal.model import SchemaError
from pre_pr_tribunal.review_store import atomic_create_bytes, locked_review, open_directory


def _temporary_artifacts(directory: Path) -> tuple[Path, ...]:
    return tuple(directory.glob(".tmp.*"))


def test_atomic_create_publishes_complete_exact_private_file(git_repo):
    """Removing fchmod, bounded writes, or final verification breaks this result."""
    payload = b'{"raw":"first\\nsecond"}\n'
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="FILE_UNSAFE")
        try:
            digest = atomic_create_bytes(
                inbox_fd,
                "A.json",
                payload,
                maximum=1024,
                exists="REPORT_FILE_EXISTS",
                unsafe="FILE_UNSAFE",
                exact_mode=0o600,
            )
        finally:
            os.close(inbox_fd)
    target = git_repo / ".review/inbox/A.json"
    assert target.read_bytes() == payload
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert len(digest) == 64
    assert _temporary_artifacts(target.parent) == ()


def test_atomic_create_refuses_existing_target_without_changing_bytes(git_repo):
    """Replacing an existing report would destroy an exact reviewer artifact."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="FILE_UNSAFE")
        os.close(inbox_fd)
    target = git_repo / ".review/inbox/A.json"
    target.write_bytes(b"keep")
    target.chmod(0o600)
    with locked_review(git_repo, create=False) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=False, code="FILE_UNSAFE")
        try:
            with pytest.raises(SchemaError, match="^REPORT_FILE_EXISTS$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"replace", maximum=1024,
                    exists="REPORT_FILE_EXISTS", unsafe="FILE_UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    assert target.read_bytes() == b"keep"
    assert _temporary_artifacts(target.parent) == ()


@pytest.mark.parametrize("target_kind", ("symlink", "fifo"))
def test_atomic_create_rejects_unsafe_existing_target_without_temporary_residue(
    git_repo, tmp_path, target_kind
):
    """Following a link or accepting a non-regular report is an unsafe publish."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="FILE_UNSAFE")
    os.close(inbox_fd)
    inbox = git_repo / ".review/inbox"
    target = inbox / "A.json"
    if target_kind == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_bytes(b"outside")
        target.symlink_to(outside)
    else:
        os.mkfifo(target, mode=0o600)
    with locked_review(git_repo, create=False) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=False, code="FILE_UNSAFE")
        try:
            with pytest.raises(SchemaError, match="^FILE_UNSAFE$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"new", maximum=1024,
                    exists="REPORT_FILE_EXISTS", unsafe="FILE_UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    assert target.exists() or target.is_symlink()
    assert _temporary_artifacts(inbox) == ()


def test_atomic_create_rejects_oversized_bytes_without_publishing(git_repo):
    """Skipping the byte limit would let unbounded raw reports reach disk."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="FILE_UNSAFE")
        try:
            with pytest.raises(SchemaError, match="^FILE_UNSAFE$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"012345", maximum=5,
                    exists="REPORT_FILE_EXISTS", unsafe="FILE_UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    inbox = git_repo / ".review/inbox"
    assert not (inbox / "A.json").exists()
    assert _temporary_artifacts(inbox) == ()


@pytest.mark.parametrize("umask", (0o000, 0o022, 0o077))
def test_atomic_create_normalizes_file_mode_under_every_common_umask(git_repo, umask):
    """Relying on the caller's umask would make reviewer reports non-deterministic."""
    previous_umask = os.umask(umask)
    try:
        with locked_review(git_repo, create=True) as review_fd:
            inbox_fd = open_directory(review_fd, "inbox", create=True, code="FILE_UNSAFE")
            try:
                atomic_create_bytes(
                    inbox_fd, "A.json", b"mode", maximum=1024,
                    exists="REPORT_FILE_EXISTS", unsafe="FILE_UNSAFE", exact_mode=0o600,
                )
            finally:
                os.close(inbox_fd)
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE((git_repo / ".review/inbox/A.json").stat().st_mode) == 0o600


def test_atomic_create_rolls_back_when_publish_fails(git_repo, monkeypatch):
    """A failed no-replace publish must not leave a partial report or temp inode."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="FILE_UNSAFE")
        try:
            def fail_link(*args, **kwargs):
                raise OSError("injected link failure")

            monkeypatch.setattr(os, "link", fail_link)
            with pytest.raises(SchemaError, match="^FILE_UNSAFE$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"no publish", maximum=1024,
                    exists="REPORT_FILE_EXISTS", unsafe="FILE_UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    inbox = git_repo / ".review/inbox"
    assert not (inbox / "A.json").exists()
    assert _temporary_artifacts(inbox) == ()


def test_atomic_create_rejects_wrong_parent_descriptor_mode_without_publishing(git_repo):
    """Dropping the descriptor safety recheck permits writes after chmod races."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="FILE_UNSAFE")
        try:
            os.fchmod(inbox_fd, 0o755)
            with pytest.raises(SchemaError, match="^FILE_UNSAFE$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"unsafe", maximum=1024,
                    exists="REPORT_FILE_EXISTS", unsafe="FILE_UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    inbox = git_repo / ".review/inbox"
    assert not (inbox / "A.json").exists()
    assert _temporary_artifacts(inbox) == ()
