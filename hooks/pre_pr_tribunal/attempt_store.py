"""Bounded, private failure evidence owned by one locked reviewer slot."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat

from . import model as m
from .model import Reviewer, SchemaError
from .review_store import atomic_create_bytes, open_directory, safe_directory, safe_file

MAX_RETAINED_ATTEMPTS = 3
# Includes the CLI's single over-limit sentinel byte. Never truncate evidence.
MAX_ATTEMPT_RAW_BYTES = m.MAX_REPORT_BYTES + 1
OPERATIONAL_FAILURE_CODES = frozenset((
    "DISPATCH_FAILED", "REVIEWER_FAILED", "REVIEWER_TIMEOUT",
))
REPORT_RETRYABLE_CODES = frozenset((
    "BEHAVIOR_EVIDENCE_REQUIRED", "CLAIM_EVIDENCE_REQUIRED", "CLAIM_ID_DUPLICATE",
    "CLAIM_SCHEMA_INVALID", "EVIDENCE_SECRET_DETECTED", "EXECUTION_ID_DUPLICATE",
    "EXECUTION_ID_INVALID", "EXECUTION_REFERENCE_INVALID", "EXECUTION_SCHEMA_INVALID",
    "FINDING_ID_DUPLICATE", "FINDING_ID_INVALID", "FINDING_REVIEWER_MISMATCH",
    "JSON_DUPLICATE_KEY", "JSON_INVALID", "PATH_INVALID", "PRIOR_DECISION_RESPONSE_INVALID",
    "REPLACEMENT_FINDING_REQUIRED", "REPORT_NOT_TERMINAL", "REPORT_REVIEWER_MISMATCH",
    "REPORT_ROUND_MISMATCH", "REPORT_SCHEMA_INVALID", "REPORT_SNAPSHOT_MISMATCH",
    "REPORT_TOO_LARGE", "TEXT_INVALID", "TEXT_TOO_LARGE",
))
LEGACY_FAILURE_CODES = frozenset(("LEGACY_PROVENANCE_UNAVAILABLE",))
_RAW_EVIDENCE_CODES = REPORT_RETRYABLE_CODES | LEGACY_FAILURE_CODES
_UNSAFE = "ATTEMPT_EVIDENCE_UNSAFE"
_NAME = re.compile(r"attempt-([1-9][0-9]*)\.(raw|meta\.json)\Z")
_META_LIMIT = 4096


@dataclass(frozen=True)
class AttemptEvidence:
    reviewer: Reviewer
    round: int
    sequence: int
    reason_code: str
    raw_sha256: str | None
    raw_path: str | None
    metadata_path: str


def _private_directory(parent_fd: int, name: str, stack: ExitStack) -> int:
    fd = open_directory(parent_fd, name, create=True, code=_UNSAFE)
    stack.callback(os.close, fd)
    if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
        raise SchemaError(_UNSAFE)
    os.fsync(parent_fd)
    return fd


def _read_pinned(parent_fd: int, name: str, maximum: int, stack: ExitStack):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    stack.callback(os.close, fd)
    info = safe_file(fd, _UNSAFE, exact_mode=0o600)
    if info.st_size > maximum:
        raise SchemaError(_UNSAFE)
    raw = bytearray()
    while len(raw) <= maximum:
        chunk = os.read(fd, min(65536, maximum + 1 - len(raw)))
        if not chunk:
            return fd, bytes(raw)
        raw.extend(chunk)
    raise SchemaError(_UNSAFE)


def _verify_named(parent_fd: int, name: str, fd: int) -> None:
    opened = safe_file(fd, _UNSAFE, exact_mode=0o600)
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_uid != os.geteuid()
        or stat.S_IMODE(named.st_mode) != 0o600
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise SchemaError(_UNSAFE)


def _verified_attempt(parent_fd: int, sequence: int, reviewer: Reviewer,
                      round_number: int, stack: ExitStack) -> dict[str, int]:
    meta_name = f"attempt-{sequence}.meta.json"
    meta_fd, raw_meta = _read_pinned(parent_fd, meta_name, _META_LIMIT, stack)
    metadata = m._load_json(raw_meta, limit=_META_LIMIT, too_large=_UNSAFE)
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"attempt", "raw_sha256", "reason_code", "reviewer", "round"}
        or type(metadata["attempt"]) is not int
        or metadata["attempt"] != sequence
        or type(metadata["round"]) is not int
        or metadata["round"] != round_number
        or metadata["reviewer"] != reviewer.value
        or not isinstance(metadata["reason_code"], str)
    ):
        raise SchemaError(_UNSAFE)
    files = {meta_name: meta_fd}
    digest = metadata["raw_sha256"]
    if digest is None:
        if metadata["reason_code"] not in OPERATIONAL_FAILURE_CODES:
            raise SchemaError(_UNSAFE)
    else:
        if metadata["reason_code"] not in _RAW_EVIDENCE_CODES:
            raise SchemaError(_UNSAFE)
        raw_name = f"attempt-{sequence}.raw"
        raw_fd, raw = _read_pinned(parent_fd, raw_name, MAX_ATTEMPT_RAW_BYTES, stack)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise SchemaError(_UNSAFE)
        files[raw_name] = raw_fd
    for name, fd in files.items():
        _verify_named(parent_fd, name, fd)
    return files


def _existing_attempts(parent_fd: int, reviewer: Reviewer, round_number: int) -> list[int]:
    names: set[str] = set()
    sequences: set[int] = set()
    with os.scandir(parent_fd) as entries:
        for entry in entries:
            match = _NAME.fullmatch(entry.name)
            if match is None or len(names) >= MAX_RETAINED_ATTEMPTS * 2:
                raise SchemaError(_UNSAFE)
            names.add(entry.name)
            sequences.add(int(match[1]))
    if len(sequences) > MAX_RETAINED_ATTEMPTS:
        raise SchemaError(_UNSAFE)
    proven: set[str] = set()
    with ExitStack() as stack:
        for sequence in sorted(sequences):
            proven.update(_verified_attempt(parent_fd, sequence, reviewer, round_number, stack))
    if names != proven:
        raise SchemaError(_UNSAFE)
    return sorted(sequences)


def _rotate_attempt(parent_fd: int, sequence: int, reviewer: Reviewer, round_number: int):
    with ExitStack() as stack:
        files = _verified_attempt(parent_fd, sequence, reviewer, round_number, stack)
        # Hold both descriptors through deletion and recheck each named inode.
        # Same-UID writers must honor the review lock; POSIX has no unlink-by-inode.
        for name, fd in reversed(tuple(files.items())):
            safe_directory(parent_fd, _UNSAFE)
            _verify_named(parent_fd, name, fd)
            os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)


def append_attempt_evidence(
    review_fd: int, *, round_number: int, reviewer: Reviewer, sequence: int,
    reason_code: str, raw: bytes | None,
) -> AttemptEvidence:
    """Publish an immutable attempt, then retain the newest three complete records.

    Caller holds the review lock. Partial publication/rotation is an integrity
    failure: preserve remaining names and require explicit recovery.
    """
    if (
        not isinstance(reviewer, Reviewer)
        or type(round_number) is not int or not 1 <= round_number <= 3
        or type(sequence) is not int or sequence < 1
        or not isinstance(reason_code, str)
        or (raw is not None and not isinstance(raw, bytes))
        or reason_code not in (OPERATIONAL_FAILURE_CODES if raw is None else _RAW_EVIDENCE_CODES)
    ):
        raise SchemaError(_UNSAFE)
    if raw is not None and len(raw) > MAX_ATTEMPT_RAW_BYTES:
        raise SchemaError("REPORT_TOO_LARGE")
    digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
    relative = f".review/attempts/round-{round_number}/{reviewer.value}"
    raw_name = f"attempt-{sequence}.raw"
    meta_name = f"attempt-{sequence}.meta.json"
    metadata = json.dumps({
        "attempt": sequence, "raw_sha256": digest, "reason_code": reason_code,
        "reviewer": reviewer.value, "round": round_number,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    try:
        safe_directory(review_fd, _UNSAFE)
        with ExitStack() as stack:
            attempts_fd = _private_directory(review_fd, "attempts", stack)
            round_fd = _private_directory(attempts_fd, f"round-{round_number}", stack)
            slot_fd = _private_directory(round_fd, reviewer.value, stack)
            previous = _existing_attempts(slot_fd, reviewer, round_number)
            if previous and sequence <= previous[-1]:
                raise SchemaError(_UNSAFE)
            if raw is not None:
                atomic_create_bytes(slot_fd, raw_name, raw, maximum=MAX_ATTEMPT_RAW_BYTES,
                                    exists=_UNSAFE, unsafe=_UNSAFE, exact_mode=0o600)
            atomic_create_bytes(slot_fd, meta_name, metadata, maximum=_META_LIMIT,
                                exists=_UNSAFE, unsafe=_UNSAFE, exact_mode=0o600)
            for oldest in previous[:max(0, len(previous) + 1 - MAX_RETAINED_ATTEMPTS)]:
                _rotate_attempt(slot_fd, oldest, reviewer, round_number)
    except (OSError, ValueError, SchemaError):
        raise SchemaError(_UNSAFE) from None
    return AttemptEvidence(reviewer, round_number, sequence, reason_code, digest,
                           f"{relative}/{raw_name}" if raw is not None else None,
                           f"{relative}/{meta_name}")


def preserve_legacy_report(
    review_fd: int, *, round_number: int, reviewer: Reviewer,
    raw: bytes | None, reason_code: str | None,
) -> tuple[bytes, str] | None:
    """Preserve attempt one, or authenticate its complete evidence on migration retry.

    This proves only the pending attempt's bytes, never authority to seal a report.
    A partial record or a different canonical response requires explicit recovery.
    """
    try:
        with ExitStack() as stack:
            attempts_fd = _private_directory(review_fd, "attempts", stack)
            round_fd = _private_directory(attempts_fd, f"round-{round_number}", stack)
            slot_fd = _private_directory(round_fd, reviewer.value, stack)
            previous = _existing_attempts(slot_fd, reviewer, round_number)
            if previous:
                if previous != [1]:
                    raise SchemaError(_UNSAFE)
                _verified_attempt(slot_fd, 1, reviewer, round_number, stack)
                _, metadata_raw = _read_pinned(slot_fd, "attempt-1.meta.json", _META_LIMIT, stack)
                metadata = m._load_json(metadata_raw, limit=_META_LIMIT, too_large=_UNSAFE)
                _, preserved = _read_pinned(slot_fd, "attempt-1.raw", MAX_ATTEMPT_RAW_BYTES, stack)
                reason = metadata["reason_code"]
                if raw is not None and (preserved != raw or reason != reason_code):
                    raise SchemaError(_UNSAFE)
                return preserved, reason
        if raw is None:
            return None
        append_attempt_evidence(review_fd, round_number=round_number, reviewer=reviewer,
                                sequence=1, raw=raw, reason_code=reason_code)
        return raw, reason_code
    except (OSError, ValueError, SchemaError):
        raise SchemaError(_UNSAFE) from None
