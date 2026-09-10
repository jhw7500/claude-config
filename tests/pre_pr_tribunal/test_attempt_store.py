"""Exact private evidence and descriptor-verified retention behavior."""

import hashlib
import json
import os
import stat

import pytest

from pre_pr_tribunal.model import MAX_REPORT_BYTES, Reviewer, SchemaError
from pre_pr_tribunal.review_store import locked_review


def append(review_fd, sequence=1, *, reviewer=Reviewer.C, raw=b'{"schema":1\r',
           reason_code="JSON_INVALID"):
    from pre_pr_tribunal.attempt_store import append_attempt_evidence

    return append_attempt_evidence(
        review_fd, round_number=1, reviewer=reviewer, sequence=sequence,
        reason_code=reason_code, raw=raw,
    )


@pytest.mark.parametrize("mask", (0o000, 0o022, 0o077))
def test_failed_attempt_preserves_exact_bytes_and_private_metadata(git_repo, mask):
    raw = b'{"schema":1\r'
    previous = os.umask(mask)
    try:
        with locked_review(git_repo, create=True) as review_fd:
            evidence = append(review_fd, raw=raw)
    finally:
        os.umask(previous)
    raw_path = git_repo / evidence.raw_path
    meta_path = git_repo / evidence.metadata_path
    assert raw_path.read_bytes() == raw
    for path in (raw_path, meta_path):
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode)
        assert info.st_uid == os.geteuid()
        assert stat.S_IMODE(info.st_mode) == 0o600
    for path in (raw_path.parent, raw_path.parent.parent, raw_path.parent.parent.parent):
        assert stat.S_IMODE(path.lstat().st_mode) == 0o700
    assert json.loads(meta_path.read_bytes()) == {
        "attempt": 1, "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "reason_code": "JSON_INVALID", "reviewer": "C", "round": 1,
    }


def test_timeout_stores_metadata_without_a_raw_file(git_repo):
    with locked_review(git_repo, create=True) as review_fd:
        evidence = append(review_fd, raw=None, reason_code="REVIEWER_TIMEOUT")
    assert evidence.raw_path is None and evidence.raw_sha256 is None
    path = git_repo / evidence.metadata_path
    assert set(path.parent.iterdir()) == {path}
    assert json.loads(path.read_bytes()) == {
        "attempt": 1, "raw_sha256": None, "reason_code": "REVIEWER_TIMEOUT",
        "reviewer": "C", "round": 1,
    }


def test_rotation_retains_only_latest_three_complete_attempts_and_preserves_peers(git_repo):
    with locked_review(git_repo, create=True) as review_fd:
        peer = append(review_fd, reviewer=Reviewer.A)
        attempts = [append(review_fd, sequence=n, raw=str(n).encode()) for n in range(1, 6)]
    directory = (git_repo / attempts[-1].metadata_path).parent
    assert {path.name for path in directory.iterdir()} == {
        f"attempt-{n}.{suffix}" for n in (3, 4, 5) for suffix in ("raw", "meta.json")
    }
    assert (git_repo / peer.raw_path).read_bytes() == b'{"schema":1\r'
    for n in (3, 4, 5):
        assert (directory / f"attempt-{n}.raw").read_bytes() == str(n).encode()


def test_sequence_reuse_preserves_existing_pair(git_repo):
    with locked_review(git_repo, create=True) as review_fd:
        evidence = append(review_fd)
        before = (git_repo / evidence.metadata_path).read_bytes()
        with pytest.raises(SchemaError, match="^ATTEMPT_EVIDENCE_UNSAFE$"):
            append(review_fd, raw=b"replacement")
    assert (git_repo / evidence.raw_path).read_bytes() == b'{"schema":1\r'
    assert (git_repo / evidence.metadata_path).read_bytes() == before


def test_legacy_attempt_preservation_is_idempotent_and_recovers_missing_canonical(git_repo):
    from pre_pr_tribunal.attempt_store import preserve_legacy_report
    with locked_review(git_repo, create=True) as review_fd:
        args = dict(round_number=1, reviewer=Reviewer.A, raw=b"{", reason_code="JSON_INVALID")
        assert preserve_legacy_report(review_fd, **args) == (b"{", "JSON_INVALID")
        path = git_repo / ".review/attempts/round-1/A/attempt-1.raw"
        before = path.stat()
        assert preserve_legacy_report(review_fd, **args) == (b"{", "JSON_INVALID")
        assert path.stat().st_ino == before.st_ino
        assert preserve_legacy_report(review_fd, round_number=1, reviewer=Reviewer.A,
                                       raw=None, reason_code=None) == (b"{", "JSON_INVALID")


@pytest.mark.parametrize("damage", ("different_bytes", "different_reason", "partial", "mode", "digest", "symlink"))
def test_legacy_attempt_recovery_refuses_unproven_or_different_evidence(git_repo, damage):
    from pre_pr_tribunal.attempt_store import preserve_legacy_report
    with locked_review(git_repo, create=True) as review_fd:
        args = dict(round_number=1, reviewer=Reviewer.A, raw=b"{", reason_code="JSON_INVALID")
        preserve_legacy_report(review_fd, **args)
        raw_path = git_repo / ".review/attempts/round-1/A/attempt-1.raw"
        meta_path = raw_path.with_name("attempt-1.meta.json")
        if damage == "different_bytes":
            args["raw"] = b"other"
        elif damage == "different_reason":
            args["reason_code"] = "REPORT_SCHEMA_INVALID"
        elif damage == "partial":
            meta_path.unlink()
        elif damage == "mode":
            raw_path.chmod(0o644)
        elif damage == "digest":
            raw_path.write_bytes(b"other")
        else:
            raw_path.unlink()
            raw_path.symlink_to(git_repo / "tracked.txt")
        with pytest.raises(SchemaError, match="^ATTEMPT_EVIDENCE_UNSAFE$"):
            preserve_legacy_report(review_fd, **args)
        assert raw_path.is_symlink() if damage == "symlink" else raw_path.exists()


@pytest.mark.parametrize("kind", ("symlink", "fifo", "mode", "owner", "metadata", "digest"))
@pytest.mark.parametrize("suffix", ("raw", "meta.json"))
def test_rotation_rejects_unproven_pair_without_unlinking(git_repo, monkeypatch, kind, suffix):
    with locked_review(git_repo, create=True) as review_fd:
        first = append(review_fd)
        append(review_fd, sequence=2)
        append(review_fd, sequence=3)
        directory = (git_repo / first.metadata_path).parent
        victim = directory / f"attempt-1.{suffix}"
        if kind in ("symlink", "fifo"):
            victim.unlink()
            if kind == "symlink":
                victim.symlink_to(git_repo / "tracked.txt")
            else:
                os.mkfifo(victim, 0o600)
        elif kind == "mode":
            victim.chmod(0o640)
        elif kind == "metadata":
            path = directory / "attempt-1.meta.json"
            value = json.loads(path.read_bytes())
            value["reviewer"] = "B"
            path.write_text(json.dumps(value))
        elif kind == "digest":
            (directory / "attempt-1.raw").write_bytes(b"changed")
        else:
            target = victim.stat()
            real_fstat = os.fstat

            def wrong_owner(fd):
                info = real_fstat(fd)
                if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                    fields = list(info)
                    fields[4] = info.st_uid + 1
                    return os.stat_result(fields)
                return info

            monkeypatch.setattr(os, "fstat", wrong_owner)
        names = {path.name for path in directory.iterdir()}
        with pytest.raises(SchemaError, match="^ATTEMPT_EVIDENCE_UNSAFE$"):
            append(review_fd, sequence=4)
        assert names.issubset({path.name for path in directory.iterdir()})
        assert (git_repo / "tracked.txt").read_bytes() == b"feature\n"


def test_incomplete_attempt_does_not_authorize_rotation(git_repo):
    with locked_review(git_repo, create=True) as review_fd:
        first = append(review_fd)
        (git_repo / first.metadata_path).unlink()
        with pytest.raises(SchemaError, match="^ATTEMPT_EVIDENCE_UNSAFE$"):
            append(review_fd, sequence=2)
    assert (git_repo / first.raw_path).read_bytes() == b'{"schema":1\r'


@pytest.mark.parametrize("size", (MAX_REPORT_BYTES, MAX_REPORT_BYTES + 1))
def test_evidence_preserves_bounded_oversize_sentinel_bytes(git_repo, size):
    raw = b"x" * size
    with locked_review(git_repo, create=True) as review_fd:
        evidence = append(review_fd, raw=raw, reason_code="REPORT_TOO_LARGE")
    assert (git_repo / evidence.raw_path).read_bytes() == raw


def test_evidence_rejects_unbounded_input_without_truncating(git_repo):
    with locked_review(git_repo, create=True) as review_fd:
        with pytest.raises(SchemaError, match="^REPORT_TOO_LARGE$"):
            append(review_fd, raw=b"x" * (MAX_REPORT_BYTES + 2))
    assert not (git_repo / ".review/attempts").exists()


def test_failure_before_rotation_preserves_all_existing_names(git_repo, monkeypatch):
    from pre_pr_tribunal import attempt_store

    with locked_review(git_repo, create=True) as review_fd:
        first = append(review_fd)
        append(review_fd, sequence=2)
        append(review_fd, sequence=3)
        directory = (git_repo / first.metadata_path).parent
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        real_create = attempt_store.atomic_create_bytes

        def fail_new_metadata(parent_fd, name, raw, **kwargs):
            if name == "attempt-4.meta.json":
                raise SchemaError("ATTEMPT_EVIDENCE_UNSAFE")
            return real_create(parent_fd, name, raw, **kwargs)

        monkeypatch.setattr(attempt_store, "atomic_create_bytes", fail_new_metadata)
        with pytest.raises(SchemaError, match="^ATTEMPT_EVIDENCE_UNSAFE$"):
            append(review_fd, sequence=4)
    assert {name: (directory / name).read_bytes() for name in before} == before


def test_rotation_rechecks_an_old_name_substituted_during_new_publication(git_repo, monkeypatch):
    from pre_pr_tribunal import attempt_store

    with locked_review(git_repo, create=True) as review_fd:
        first = append(review_fd)
        append(review_fd, sequence=2)
        append(review_fd, sequence=3)
        victim = git_repo / first.raw_path
        real_create = attempt_store.atomic_create_bytes

        def publish_then_substitute(parent_fd, name, raw, **kwargs):
            result = real_create(parent_fd, name, raw, **kwargs)
            if name == "attempt-4.meta.json":
                victim.unlink()
                victim.symlink_to(git_repo / "tracked.txt")
            return result

        monkeypatch.setattr(attempt_store, "atomic_create_bytes", publish_then_substitute)
        with pytest.raises(SchemaError, match="^ATTEMPT_EVIDENCE_UNSAFE$"):
            append(review_fd, sequence=4)
    assert victim.is_symlink()
    assert (git_repo / first.metadata_path).is_file()
    assert (git_repo / "tracked.txt").read_bytes() == b"feature\n"
