import errno
import os
from pathlib import Path
import stat
import subprocess

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


@pytest.mark.parametrize("rules", (
    "/.review/verdict.json\n/.review/lock\n/.review/inbox/\n",
    ".review/*\n!.review/attempts/\n",
    ".review/**\n!.review/attempts/round-1/A/attempt-1.raw\n",
))
def test_ignore_guard_rejects_partial_namespace_exclusion(git_repo, rules):
    """Checking only verdict.json would permit Git-visible raw attempts."""
    (git_repo / ".gitignore").write_text(rules, encoding="utf-8")
    with pytest.raises(SchemaError, match="^VERDICT_NOT_IGNORED$"):
        review_store.check_ignored(git_repo)
    assert not (git_repo / ".review").exists()


@pytest.mark.parametrize("pattern", (".review/", "/.review/", ".review", "/.review"))
@pytest.mark.parametrize("review_exists", (False, True))
def test_ignore_guard_accepts_whole_parent_despite_child_negations(
    git_repo, pattern, review_exists,
):
    """A safe parent exclusion must not be rejected because children are negated."""
    (git_repo / ".gitignore").write_text(
        f"{pattern}\n!.review/attempts/\n!.review/inbox/\n", encoding="utf-8",
    )
    if review_exists:
        (git_repo / ".review").mkdir(mode=0o700)
    review_store.check_ignored(git_repo)


def test_ignore_guard_rejects_tracked_descendant_under_excluded_parent(git_repo):
    """An ignored verdict does not prove that other private artifacts are untracked."""
    review = git_repo / ".review"
    review.mkdir(mode=0o700)
    tracked = review / "already-tracked.txt"
    tracked.write_text("synthetic fixture", encoding="utf-8")
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "add", "-f", "--",
         ".review/already-tracked.txt"], check=True,
    )
    with pytest.raises(SchemaError, match="^VERDICT_NOT_IGNORED$"):
        review_store.check_ignored(git_repo)
    assert tracked.read_text(encoding="utf-8") == "synthetic fixture"


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


@pytest.mark.parametrize("hold_original", (False, True))
def test_atomic_create_pins_source_independently_of_temporary_names(
    git_repo, monkeypatch, hold_original
):
    """A mutable staging pathname must never select the report's source inode."""
    token = "source-substitution"
    temporary_name = f".tmp.{os.getpid()}.{token}"
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            real_link = os.link
            held_original_fd = -1
            foreign = git_repo / ".review/inbox" / temporary_name

            def substitute_then_link(source, target, **kwargs):
                nonlocal held_original_fd
                if hold_original:
                    held_original_fd = os.open(source, os.O_RDONLY, dir_fd=inbox_fd)
                if foreign.exists():
                    foreign.unlink()
                foreign.write_bytes(b"foreign source")
                foreign.chmod(0o600)
                return real_link(source, target, **kwargs)

            monkeypatch.setattr(review_store.secrets, "token_hex", lambda _count: token)
            monkeypatch.setattr(os, "link", substitute_then_link)
            atomic_create_bytes(
                inbox_fd, "A.json", b"expected", maximum=1024,
                exists="EXISTS", unsafe="UNSAFE", exact_mode=0o600,
            )
        finally:
            if held_original_fd >= 0:
                os.close(held_original_fd)
            os.close(inbox_fd)
    inbox = git_repo / ".review/inbox"
    assert (inbox / "A.json").read_bytes() == b"expected"
    assert foreign.read_bytes() == b"foreign source"


def test_replacement_rename_failure_preserves_one_complete_private_staging_link(
    git_repo, monkeypatch
):
    """A failed rename cannot authorize deleting a mutable staging pathname."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            target = git_repo / ".review/inbox/A.json"
            target.write_bytes(b"keep")
            target.chmod(0o600)

            def fail_replace(*args, **kwargs):
                raise OSError("injected rename failure")

            monkeypatch.setattr(os, "replace", fail_replace)
            with pytest.raises(SchemaError, match="^VERDICT_WRITE_FAILED$"):
                atomic_replace_bytes(
                    inbox_fd, "A.json", b"new", maximum=1024,
                    too_large="TOO_LARGE", unsafe="VERDICT_FILE_UNSAFE", exact_mode=0o600,
                    write_failed="VERDICT_WRITE_FAILED",
                )
        finally:
            os.close(inbox_fd)
    assert target.read_bytes() == b"keep"
    artifacts = _temporary_artifacts(target.parent)
    assert len(artifacts) == 1
    assert artifacts[0].read_bytes() == b"new"
    assert stat.S_IMODE(artifacts[0].stat().st_mode) == 0o600
    assert artifacts[0].stat().st_uid == os.geteuid()
    assert set(target.parent.iterdir()) == {target, artifacts[0]}


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


def test_atomic_create_preserves_bounded_destination_when_open_fails_after_link(
    git_repo, monkeypatch
):
    """Post-link uncertainty must preserve the completed name without unsafe rollback."""
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
    target = inbox / "A.json"
    assert target.read_bytes() == b"expected"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_uid == os.geteuid()
    assert set(inbox.iterdir()) == {target}
    assert _temporary_artifacts(inbox) == ()


def test_temporary_name_collision_preserves_unowned_inode(git_repo, monkeypatch):
    """Exclusive staging publication must preserve an existing foreign name."""
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
                atomic_replace_bytes(
                    inbox_fd, "A.json", b"expected", maximum=1024,
                    too_large="TOO_LARGE", unsafe="UNSAFE", exact_mode=0o600,
                    write_failed="WRITE_FAILED",
                )
        finally:
            os.close(inbox_fd)
    assert foreign.read_bytes() == b"foreign temporary"
    assert set(foreign.parent.iterdir()) == {foreign}


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
                    real_close(fd)
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


def test_cleanup_boundary_substitution_preserves_foreign_name(git_repo, monkeypatch):
    """A name swapped at cleanup must be preserved rather than pathname-unlinked."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        try:
            real_fsync = os.fsync

            def fail_directory_fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    target = git_repo / ".review/inbox/A.json"
                    target.unlink()
                    target.write_bytes(b"foreign cleanup")
                    target.chmod(0o600)
                    raise OSError("injected directory fsync failure")
                return real_fsync(fd)

            monkeypatch.setattr(os, "fsync", fail_directory_fsync)
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


def test_atomic_create_preserves_names_substituted_after_cleanup_verification(
    git_repo, monkeypatch
):
    """Deleting a checked quarantine pathname can delete its later replacement."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        inbox = git_repo / ".review/inbox"
        real_close = os.close
        real_fsync = os.fsync
        real_unlink = os.unlink
        substituted = []
        deletion_requests = []

        def substitute_after_verified_descriptor_close(fd):
            path = Path(os.readlink(f"/proc/self/fd/{fd}"))
            real_close(fd)
            if path.parent == inbox and path.name.startswith(".cleanup."):
                # Reaches this point only after a successful quarantine rename
                # and its descriptor verification. Anonymous staging eliminates
                # the mutable cleanup name and therefore this attack boundary.
                real_unlink(path)
                path.write_bytes(b"foreign after verification")
                path.chmod(0o600)
                substituted.append(path)

        def fail_directory_fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected post-publication failure")
            return real_fsync(fd)

        def record_unlink(name, *args, **kwargs):
            deletion_requests.append(name)
            return real_unlink(name, *args, **kwargs)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "close", substitute_after_verified_descriptor_close)
                patch.setattr(os, "fsync", fail_directory_fsync)
                patch.setattr(os, "unlink", record_unlink)
                with pytest.raises(SchemaError, match="^UNSAFE$"):
                    atomic_create_bytes(
                        inbox_fd, "A.json", b"expected", maximum=1024,
                        exists="EXISTS", unsafe="UNSAFE", exact_mode=0o600,
                    )
        finally:
            real_close(inbox_fd)
    for path in substituted:
        assert path.exists(), "a successfully quarantined name was replaced, then deleted"
        assert path.read_bytes() == b"foreign after verification"
    assert deletion_requests == [], "mutable pathnames cannot establish deletion ownership"


@pytest.mark.parametrize("reuse_descriptor", (False, True))
def test_replacement_close_failure_relinquishes_descriptor_once(
    git_repo, monkeypatch, reuse_descriptor
):
    """Retrying a released descriptor changes the code or closes an unrelated file."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        target = git_repo / ".review/inbox/A.json"
        real_close = os.close
        real_open = os.open
        owned_fd = -1
        released_fd = -1
        unrelated_fd = -1
        close_attempts = 0

        def record_temporary_open(name, flags, *args, **kwargs):
            nonlocal owned_fd
            fd = real_open(name, flags, *args, **kwargs)
            if flags & os.O_TMPFILE == os.O_TMPFILE or flags & os.O_EXCL:
                owned_fd = fd
            return fd

        def fail_replacement_close(fd):
            nonlocal released_fd, unrelated_fd, close_attempts
            if fd == released_fd:
                close_attempts += 1
                return real_close(fd)
            if fd == owned_fd:
                released_fd = fd
                close_attempts += 1
                real_close(fd)
                if reuse_descriptor:
                    unrelated_fd = os.open("/dev/null", os.O_RDONLY)
                    assert unrelated_fd == released_fd
                raise OSError("injected close failure after descriptor release")
            return real_close(fd)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "open", record_temporary_open)
                patch.setattr(os, "close", fail_replacement_close)
                with pytest.raises(SchemaError) as failure:
                    atomic_replace_bytes(
                        inbox_fd, "A.json", b"new", maximum=1024,
                        too_large="TOO_LARGE", unsafe="VERDICT_FILE_UNSAFE",
                        write_failed="VERDICT_WRITE_FAILED", exact_mode=0o600,
                    )
            assert str(failure.value) == "VERDICT_WRITE_FAILED"
            assert close_attempts == 1
            if reuse_descriptor:
                assert stat.S_ISCHR(os.fstat(unrelated_fd).st_mode)
            assert target.read_bytes() == b"new"
        finally:
            if unrelated_fd >= 0:
                try:
                    real_close(unrelated_fd)
                except OSError:
                    pass
            real_close(inbox_fd)


@pytest.mark.parametrize("operation", ("create", "replace"))
def test_anonymous_staging_unsupported_fails_closed_without_named_fallback(
    git_repo, monkeypatch, operation
):
    """A named fallback would reintroduce deletion races on unsupported filesystems."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        real_open = os.open

        def reject_anonymous_open(name, flags, *args, **kwargs):
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                raise OSError(errno.EOPNOTSUPP, "anonymous staging unavailable")
            return real_open(name, flags, *args, **kwargs)

        try:
            monkeypatch.setattr(os, "open", reject_anonymous_open)
            expected = "UNSAFE" if operation == "create" else "WRITE_FAILED"
            with pytest.raises(SchemaError, match=f"^{expected}$"):
                if operation == "create":
                    atomic_create_bytes(
                        inbox_fd, "A.json", b"expected", maximum=1024,
                        exists="EXISTS", unsafe="UNSAFE", exact_mode=0o600,
                    )
                else:
                    atomic_replace_bytes(
                        inbox_fd, "A.json", b"expected", maximum=1024,
                        too_large="TOO_LARGE", unsafe="UNSAFE", exact_mode=0o600,
                        write_failed="WRITE_FAILED",
                    )
        finally:
            os.close(inbox_fd)
    assert tuple((git_repo / ".review/inbox").iterdir()) == ()


def test_atomic_replace_rejects_substituted_staging_source(git_repo, monkeypatch):
    """Renaming a substituted staging name must not report successful persistence."""
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="UNSAFE")
        real_replace = os.replace

        def substitute_then_replace(source, target, **kwargs):
            source_path = git_repo / ".review/inbox" / source
            source_path.unlink()
            source_path.write_bytes(b"foreign replacement")
            source_path.chmod(0o600)
            return real_replace(source, target, **kwargs)

        try:
            monkeypatch.setattr(os, "replace", substitute_then_replace)
            with pytest.raises(SchemaError, match="^UNSAFE$"):
                atomic_replace_bytes(
                    inbox_fd, "A.json", b"expected", maximum=1024,
                    too_large="TOO_LARGE", unsafe="UNSAFE", exact_mode=0o600,
                    write_failed="WRITE_FAILED",
                )
        finally:
            os.close(inbox_fd)
    target = git_repo / ".review/inbox/A.json"
    assert target.read_bytes() == b"foreign replacement"
    assert set(target.parent.iterdir()) == {target}
def test_atomic_create_reports_publication_io_failure_separately_from_unsafe(
    git_repo, monkeypatch
):
    with locked_review(git_repo, create=True) as review_fd:
        def fail_write(*args, **kwargs):
            raise OSError("injected write failure")

        monkeypatch.setattr(os, "write", fail_write)
        with pytest.raises(SchemaError, match="^REPORT_WRITE_FAILED$"):
            atomic_create_bytes(
                review_fd, "A.json", b"expected", maximum=1024,
                exists="REPORT_FILE_EXISTS", unsafe="FILE_UNSAFE",
                exact_mode=0o600, write_failed="REPORT_WRITE_FAILED",
            )
    assert not (git_repo / ".review/A.json").exists()
