"""Content-addressed, immutable private evidence using review-store descriptors."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat

from . import evidence, review_store
from .model import SchemaError


@contextmanager
def _review_directory(root: Path, *, create: bool):
    if create:
        with review_store.locked_review(root, create=True) as fd:
            yield fd
        return
    # Immutable reads need no lock: each exact byte string is checked against
    # its external digest. This also permits verification inside a verdict lock.
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(root_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise SchemaError('EVIDENCE_DIRECTORY_UNSAFE')
        review_fd = review_store.open_directory(root_fd, '.review', create=False,
                                                code='EVIDENCE_DIRECTORY_UNSAFE')
        try:
            yield review_fd
        finally:
            os.close(review_fd)
    finally:
        os.close(root_fd)


@contextmanager
def _directory(cwd: Path, kind: str, *, create: bool):
    root = review_store.repository_root(cwd)
    review_store.check_ignored(root)
    with _review_directory(root, create=create) as review_fd:
        evidence_fd = review_store.open_directory(review_fd, 'evidence', create=create,
                                                   code='EVIDENCE_DIRECTORY_UNSAFE')
        try:
            kind_fd = review_store.open_directory(evidence_fd, kind, create=create,
                                                  code='EVIDENCE_DIRECTORY_UNSAFE')
            try:
                yield kind_fd
            finally:
                os.close(kind_fd)
        finally:
            os.close(evidence_fd)


def _read_at(fd, name, digest, maximum):
    raw = review_store.read_named_file(fd, name, maximum=maximum,
                                      missing='EVIDENCE_MISSING', unsafe='EVIDENCE_FILE_UNSAFE',
                                      exact_mode=0o600)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise SchemaError('EVIDENCE_DIGEST_MISMATCH')
    return raw


def _put(cwd, kind, suffix, raw, maximum):
    digest = hashlib.sha256(raw).hexdigest()
    name = digest+suffix
    with _directory(cwd, kind, create=True) as fd:
        try:
            review_store.atomic_create_bytes(fd, name, raw, maximum=maximum,
                                             exists='EVIDENCE_EXISTS', unsafe='EVIDENCE_FILE_UNSAFE',
                                             exact_mode=0o600)
        except SchemaError as error:
            if error.code != 'EVIDENCE_EXISTS':
                raise
            if _read_at(fd, name, digest, maximum) != raw:
                raise SchemaError('EVIDENCE_DIGEST_MISMATCH') from None
    return digest


def _read(cwd, kind, suffix, digest, maximum):
    evidence._digest(digest)
    with _directory(cwd, kind, create=False) as fd:
        return _read_at(fd, digest+suffix, digest, maximum)


@evidence.bounded_errors
def put_capture(cwd: Path, stdout: bytes, stderr: bytes) -> str:
    return _put(cwd, 'captures', '.bin', evidence.encode_capture(stdout, stderr),
                evidence.MAX_CAPTURE_BYTES+evidence.CAPTURE_OVERHEAD)


@evidence.bounded_errors
def read_capture(cwd: Path, digest: str) -> bytes:
    raw = _read(cwd, 'captures', '.bin', digest,
                evidence.MAX_CAPTURE_BYTES+evidence.CAPTURE_OVERHEAD)
    evidence.decode_capture(raw)
    return raw


@evidence.bounded_errors
def put_receipt(cwd: Path, receipt: dict) -> str:
    raw = evidence.canonical_json(receipt)
    evidence.parse_receipt(raw)
    evidence.validate_entry_capture(receipt['entry'], read_capture(cwd, receipt['entry']['capture_sha256']))
    return _put(cwd, 'receipts', '.json', raw, evidence.MAX_BUNDLE_BYTES)


@evidence.bounded_errors
def read_receipt(cwd: Path, digest: str) -> dict:
    return evidence.parse_receipt(_read(cwd, 'receipts', '.json', digest, evidence.MAX_BUNDLE_BYTES))


@evidence.bounded_errors
def put_bundle(cwd: Path, bundle: dict) -> str:
    raw = evidence.canonical_json(bundle)
    evidence.parse_bundle(raw)
    for entry in bundle['entries']:
        evidence.validate_entry_capture(entry, read_capture(cwd, entry['capture_sha256']))
    return _put(cwd, 'bundles', '.json', raw, evidence.MAX_BUNDLE_BYTES)


@evidence.bounded_errors
def read_bundle(cwd: Path, digest: str) -> dict:
    return evidence.parse_bundle(_read(cwd, 'bundles', '.json', digest, evidence.MAX_BUNDLE_BYTES))


@evidence.bounded_errors
def verify_bundle(cwd: Path, digest: str, expected_binding: dict) -> dict:
    bundle = read_bundle(cwd, digest)
    evidence.validate_binding(bundle['binding'], expected_binding)
    eligible, rejected = [], []
    for entry in bundle['entries']:
        evidence.validate_entry_capture(entry, read_capture(cwd, entry['capture_sha256']))
        if entry['freshness'] == 'always-fresh':
            rejected.append({'id': entry['id'], 'reason': 'EVIDENCE_ALWAYS_FRESH'})
        else:
            eligible.append(entry)
    return {'bundle': bundle, 'eligible': eligible, 'rejected': rejected}
