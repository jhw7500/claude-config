import os
from pathlib import Path
import stat

import pytest

from pre_pr_tribunal.model import SchemaError
import pre_pr_tribunal.review_store as review_store
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


def test_atomic_create_preserves_unproven_substituted_publish_on_rejection(
    git_repo, monkeypatch
):
    """A substituted source inode is unsafe but is not ours to delete."""
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
    assert (inbox / "A.json").read_bytes() == b"substituted"
    assert tuple(item.read_bytes() for item in _temporary_artifacts(inbox)) == (
        b"substituted",
    )


def test_temporary_cleanup_failure_surfaces_unsafe_and_retains_evidence(
    git_repo, monkeypatch
):
    """Ignoring failed cleanup hides residue behind the primary write error."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            real_unlink = os.unlink

            def fail_fchmod(*args, **kwargs):
                raise OSError("injected fchmod failure")

            def fail_temp_unlink(name, *args, **kwargs):
                if str(name).startswith((".tmp.", ".cleanup.")):
                    raise OSError("injected cleanup failure")
                return real_unlink(name, *args, **kwargs)

            monkeypatch.setattr(os, "fchmod", fail_fchmod)
            monkeypatch.setattr(os, "unlink", fail_temp_unlink)
            with pytest.raises(SchemaError, match="^UNSAFE$"):
                atomic_replace_bytes(
                    inbox_fd, "A.json", b"new", maximum=1024,
                    too_large="TOO_LARGE", unsafe="UNSAFE", exact_mode=0o600,
                    write_failed="WRITE_FAILED",
                )
        finally:
            os.close(inbox_fd)
    artifacts = tuple((git_repo / ".review/inbox").glob(".cleanup.*"))
    assert len(artifacts) == 1


def test_atomic_create_preserves_destination_substituted_after_link(
    git_repo, monkeypatch
):
    """A destination replaced after link is unproven and must not be rollback-deleted."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            real_link = os.link

            def link_then_substitute(source, target, **kwargs):
                result = real_link(source, target, **kwargs)
                os.unlink(target, dir_fd=kwargs["dst_dir_fd"])
                foreign_fd = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=kwargs["dst_dir_fd"],
                )
                try:
                    os.write(foreign_fd, b"foreign destination")
                finally:
                    os.close(foreign_fd)
                return result

            monkeypatch.setattr(os, "link", link_then_substitute)
            with pytest.raises(SchemaError, match="^UNSAFE$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"expected", maximum=1024,
                    exists="EXISTS", unsafe="UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    inbox = git_repo / ".review/inbox"
    assert (inbox / "A.json").read_bytes() == b"foreign destination"
    assert _temporary_artifacts(inbox) == ()


def test_atomic_create_rolls_back_when_destination_open_fails_after_link(
    git_repo, monkeypatch
):
    """A successful link must be rollback-owned before destination validation."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            real_link = os.link
            real_open = os.open
            linked = False
            destination_open_failed = False

            def record_link(*args, **kwargs):
                nonlocal linked
                result = real_link(*args, **kwargs)
                linked = True
                return result

            def fail_destination_open(name, flags, *args, **kwargs):
                nonlocal destination_open_failed
                if linked and name == "A.json" and not destination_open_failed:
                    destination_open_failed = True
                    raise OSError("injected destination open failure")
                return real_open(name, flags, *args, **kwargs)

            monkeypatch.setattr(os, "link", record_link)
            monkeypatch.setattr(os, "open", fail_destination_open)
            with pytest.raises(SchemaError, match="^UNSAFE$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"expected", maximum=1024,
                    exists="EXISTS", unsafe="UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    inbox = git_repo / ".review/inbox"
    assert not (inbox / "A.json").exists()
    assert _temporary_artifacts(inbox) == ()


def test_temporary_name_collision_preserves_unowned_inode(git_repo, monkeypatch):
    """An O_EXCL collision proves this call never owned the temporary name."""
    token = "collision"
    temporary = f".tmp.{os.getpid()}.{token}"
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            foreign = git_repo / ".review/inbox" / temporary
            foreign.write_bytes(b"foreign temporary")
            foreign.chmod(0o600)
            monkeypatch.setattr(review_store.secrets, "token_hex", lambda _count: token)
            with pytest.raises(SchemaError, match="^UNSAFE$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"expected", maximum=1024,
                    exists="EXISTS", unsafe="UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    assert foreign.read_bytes() == b"foreign temporary"


@pytest.mark.parametrize("cleanup_stage", ("fstat", "close"))
def test_temporary_cleanup_descriptor_failures_map_to_unsafe(
    git_repo, monkeypatch, cleanup_stage
):
    """Raw cleanup descriptor failures must not escape the stable unsafe boundary."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            real_fstat = os.fstat
            real_close = os.close
            fstat_calls = 0
            close_failed = False

            def fail_fchmod(*args, **kwargs):
                raise OSError("injected primary failure")

            def fail_cleanup_fstat(fd):
                nonlocal fstat_calls
                fstat_calls += 1
                if cleanup_stage == "fstat" and fstat_calls == 3:
                    raise OSError("injected cleanup fstat failure")
                return real_fstat(fd)

            def fail_cleanup_close(fd):
                nonlocal close_failed
                if cleanup_stage == "close" and not close_failed:
                    close_failed = True
                    raise OSError("injected cleanup close failure")
                return real_close(fd)

            monkeypatch.setattr(os, "fchmod", fail_fchmod)
            monkeypatch.setattr(os, "fstat", fail_cleanup_fstat)
            monkeypatch.setattr(os, "close", fail_cleanup_close)
            with pytest.raises(SchemaError, match="^UNSAFE$"):
                atomic_replace_bytes(
                    inbox_fd, "A.json", b"new", maximum=1024,
                    too_large="TOO_LARGE", unsafe="UNSAFE", exact_mode=0o600,
                    write_failed="WRITE_FAILED",
                )
        finally:
            os.close(inbox_fd)


def test_atomic_create_source_substitution_without_held_inode_preserves_foreign(
    git_repo, monkeypatch
):
    """Closing the source early lets inode reuse turn a foreign publish into ours."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            real_link = os.link

            def substitute_then_link(source, target, **kwargs):
                os.unlink(source, dir_fd=kwargs["src_dir_fd"])
                foreign_fd = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=kwargs["src_dir_fd"],
                )
                try:
                    os.write(foreign_fd, b"foreign source")
                finally:
                    os.close(foreign_fd)
                return real_link(source, target, **kwargs)

            monkeypatch.setattr(os, "link", substitute_then_link)
            with pytest.raises(SchemaError, match="^UNSAFE$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"expected", maximum=1024,
                    exists="EXISTS", unsafe="UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    inbox = git_repo / ".review/inbox"
    assert (inbox / "A.json").read_bytes() == b"foreign source"
    assert tuple(item.read_bytes() for item in _temporary_artifacts(inbox)) == (
        b"foreign source",
    )


def test_cleanup_boundary_substitution_preserves_foreign_name(git_repo, monkeypatch):
    """A name swapped at cleanup must be preserved rather than pathname-unlinked."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            real_fsync = os.fsync
            fsync_calls = 0

            def fail_directory_fsync(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("injected directory fsync failure")
                return real_fsync(fd)

            def substitute_before_cleanup(*args, **kwargs):
                source = args[1]
                if source == "A.json":
                    os.unlink(source, dir_fd=args[0])
                    foreign_fd = os.open(
                        source,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=args[0],
                    )
                    try:
                        os.write(foreign_fd, b"foreign cleanup")
                    finally:
                        os.close(foreign_fd)
                raise OSError("cleanup exchange unavailable")

            monkeypatch.setattr(os, "fsync", fail_directory_fsync)
            monkeypatch.setattr(
                review_store, "_rename_noreplace", substitute_before_cleanup, raising=False
            )
            with pytest.raises(SchemaError, match="^UNSAFE$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"expected", maximum=1024,
                    exists="EXISTS", unsafe="UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    assert (git_repo / ".review/inbox/A.json").read_bytes() == b"foreign cleanup"


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
