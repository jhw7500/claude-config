import os
from pathlib import Path
import stat

import pytest

from pre_pr_tribunal.model import SchemaError
from pre_pr_tribunal.review_store import (
    atomic_create_bytes,
    atomic_replace_bytes,
    locked_review,
    open_directory,
)


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


@pytest.mark.parametrize("operation", ("create", "replace"))
@pytest.mark.parametrize("failure_stage", ("fchmod", "write", "fsync"))
def test_temporary_write_failure_removes_its_private_temp_inode(
    git_repo, monkeypatch, operation, failure_stage
):
    """Returning before handoff must not strand a private `.tmp.*` inode."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            inbox = git_repo / ".review/inbox"
            target = inbox / "A.json"
            if operation == "replace":
                target.write_bytes(b"keep")
                target.chmod(0o600)

            def fail_stage(*args, **kwargs):
                raise OSError(f"injected {failure_stage} failure")

            monkeypatch.setattr(os, failure_stage, fail_stage)
            expected_code = "UNSAFE" if operation == "create" else "WRITE_FAILED"
            with pytest.raises(SchemaError, match=f"^{expected_code}$"):
                if operation == "create":
                    atomic_create_bytes(
                        inbox_fd, "A.json", b"new", maximum=1024,
                        exists="EXISTS", unsafe="UNSAFE", exact_mode=0o600,
                    )
                else:
                    atomic_replace_bytes(
                        inbox_fd, "A.json", b"new", maximum=1024,
                        too_large="TOO_LARGE", unsafe="UNSAFE", exact_mode=0o600,
                        write_failed="WRITE_FAILED",
                    )
        finally:
            os.close(inbox_fd)
    if operation == "create":
        assert not target.exists()
    else:
        assert target.read_bytes() == b"keep"
    assert _temporary_artifacts(inbox) == ()


def test_atomic_create_removes_verified_substituted_publish_on_rejection(
    git_repo, monkeypatch
):
    """A substituted temp inode must not survive as this call's rejected target."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            real_link = os.link
            held_original_fd = -1

            def substitute_then_link(
                source, target, *, src_dir_fd=None, dst_dir_fd=None,
                follow_symlinks=True,
            ):
                nonlocal held_original_fd
                held_original_fd = os.open(
                    source, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_dir_fd
                )
                os.unlink(source, dir_fd=src_dir_fd)
                replacement_fd = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=src_dir_fd,
                )
                try:
                    os.fchmod(replacement_fd, 0o600)
                    os.write(replacement_fd, b"substituted")
                finally:
                    os.close(replacement_fd)
                return real_link(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            monkeypatch.setattr(os, "link", substitute_then_link)
            with pytest.raises(SchemaError, match="^UNSAFE$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"expected", maximum=1024,
                    exists="EXISTS", unsafe="UNSAFE", exact_mode=0o600,
                )
        finally:
            if held_original_fd >= 0:
                os.close(held_original_fd)
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
