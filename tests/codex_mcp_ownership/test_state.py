from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

import pytest

from codex_mcp_ownership import model, state
from helpers import (
    make_private_directory,
    sample_lease,
    sample_process,
    write_private_file,
)


def test_read_only_store_does_not_create_root(tmp_path):
    root = tmp_path / "missing"
    store = state.StateStore(root, read_only=True)
    assert store.load_sessions() == ()
    assert not root.exists()


def test_session_filename_is_hash_not_untrusted_id(tmp_path):
    lease = sample_lease()
    store = state.StateStore(tmp_path / "state")
    store.save_session(lease)
    files = list((store.root / "sessions").iterdir())
    assert [path.name for path in files] == [hashlib.sha256(lease.session_id.encode()).hexdigest() + ".json"]
    assert lease.session_id not in files[0].name
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert files[0].read_bytes() == (
        json.dumps(lease.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        + b"\n"
    )


@pytest.mark.parametrize("umask", [0, 0o777])
def test_created_state_paths_have_deterministic_private_modes(tmp_path, umask):
    root = tmp_path / "state"
    previous_umask = os.umask(umask)
    try:
        state.StateStore(root).save_session(sample_lease())
    finally:
        os.umask(previous_umask)
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "sessions").stat().st_mode & 0o777 == 0o700
    assert (root / "state.lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("session_id", ["bad/session", "bad\x01session", "", "x" * 129])
def test_invalid_session_id_is_rejected_before_any_path_is_created(tmp_path, session_id):
    root = tmp_path / "state"
    with pytest.raises(ValueError):
        state.StateStore(root).save_session(sample_lease(session_id))
    assert not root.exists()


def test_sessions_and_processes_round_trip_and_process_removal_is_idempotent(tmp_path):
    store = state.StateStore(tmp_path / "state")
    lease = sample_lease()
    process = sample_process()
    store.save_session(lease)
    store.save_process(process)
    assert store.load_sessions() == (lease,)
    assert store.load_processes() == (process,)
    store.remove_process(process.wrapper.stable_key())
    store.remove_process(process)
    assert store.load_processes() == ()


def test_read_only_store_rejects_mutation_without_creating_root(tmp_path):
    root = tmp_path / "state"
    store = state.StateStore(root, read_only=True)
    with pytest.raises(state.ReadOnlyStateError):
        store.append_event({"schema_version": 1, "event": "audit"})
    assert not root.exists()


@pytest.mark.parametrize(
    "field",
    ["command", "args", "env", "transcript_path", "credential", "unknown"],
)
def test_event_log_rejects_sensitive_and_unknown_fields(tmp_path, field):
    root = tmp_path / "state"
    with pytest.raises(ValueError):
        state.StateStore(root).append_event({"schema_version": 1, "event": "spawn", field: "canary"})
    assert not root.exists()


def test_event_log_accepts_only_redacted_allowlisted_fields(tmp_path):
    store = state.StateStore(tmp_path / "state")
    event = {
        "schema_version": 1,
        "event": "spawn",
        "observed_wall": "2026-08-29T00:00:00+00:00",
        "server": "example",
        "scope": "user",
        "session_id": "session:test_1",
        "process_key": "a" * 64,
        "state": "active",
        "reason_codes": ["managed"],
        "rss_kib": 128,
    }
    store.append_event(event)
    assert json.loads((store.root / "events.jsonl").read_text()) == event
    assert (store.root / "events.jsonl").stat().st_mode & 0o777 == 0o600


def _build_unsafe_store(tmp_path: Path, unsafe_kind: str) -> tuple[state.StateStore, Path]:
    root = tmp_path / "state"
    processes = root / "processes"
    make_private_directory(processes)
    target = tmp_path / "sentinel"
    write_private_file(target, b"sentinel")
    record = processes / "unsafe.json"
    if unsafe_kind == "symlink":
        record.symlink_to(target)
    elif unsafe_kind == "hardlink":
        os.link(target, record)
    else:
        write_private_file(record, b"sentinel")
        record.chmod(0o644)
        target = record
    return state.StateStore(root, read_only=True), target


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "mode"])
def test_unsafe_state_file_is_rejected(tmp_path, unsafe_kind):
    store, target = _build_unsafe_store(tmp_path, unsafe_kind)
    with pytest.raises(state.UnsafeStatePath):
        store.load_processes()
    assert target.read_bytes() == b"sentinel"


@pytest.mark.parametrize("unsafe_kind", ["symlink", "mode", "file"])
def test_unsafe_state_root_is_rejected_without_mutation(tmp_path, unsafe_kind):
    target = tmp_path / "target"
    if unsafe_kind == "file":
        write_private_file(target, b"sentinel")
        root = target
    else:
        make_private_directory(target)
        root = tmp_path / "state"
        if unsafe_kind == "symlink":
            root.symlink_to(target, target_is_directory=True)
        else:
            target.chmod(0o755)
            root = target
    with pytest.raises(state.UnsafeStatePath):
        state.StateStore(root, read_only=True).load_sessions()


def test_read_only_corruption_is_reported_without_quarantine(tmp_path):
    root = tmp_path / "state"
    sessions = root / "sessions"
    make_private_directory(sessions)
    corrupt = sessions / ("a" * 64 + ".json")
    write_private_file(corrupt, b"{not-json}\n")
    before = corrupt.stat()
    with pytest.raises(state.StateCorruption) as error:
        state.StateStore(root, read_only=True).load_sessions()
    assert error.value.path == corrupt
    assert error.value.digest == hashlib.sha256(b"{not-json}\n").hexdigest()
    assert corrupt.read_bytes() == b"{not-json}\n"
    assert corrupt.stat().st_ino == before.st_ino
    assert not (root / "corrupt").exists()


def test_writable_load_quarantines_corruption_by_digest_under_lock(tmp_path):
    root = tmp_path / "state"
    sessions = root / "sessions"
    make_private_directory(sessions)
    raw = b"{not-json}\n"
    corrupt = sessions / ("a" * 64 + ".json")
    write_private_file(corrupt, raw)
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(state.StateCorruption) as error:
        state.StateStore(root).load_sessions()
    assert not corrupt.exists()
    quarantined = list((root / "corrupt").iterdir())
    assert len(quarantined) == 1
    assert digest in quarantined[0].name
    assert quarantined[0].read_bytes() == raw
    assert quarantined[0].stat().st_mode & 0o777 == 0o600
    assert error.value.quarantine_path == quarantined[0]


def test_atomic_json_failure_preserves_previous_record(tmp_path, monkeypatch):
    store = state.StateStore(tmp_path / "state")
    original = sample_lease()
    store.save_session(original)
    target = next((store.root / "sessions").iterdir())
    before = target.read_bytes()
    updated = model.SessionLease(
        original.schema_version,
        original.session_id,
        original.cwd,
        original.source,
        original.host_keys,
        "ended",
        original.observed,
        original.observed,
    )
    monkeypatch.setattr(state.os, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError, match="stop"):
        store.save_session(updated)
    assert target.read_bytes() == before
    assert [path for path in target.parent.iterdir() if path.name != target.name] == []


def test_atomic_json_write_failure_removes_private_temporary_file(tmp_path, monkeypatch):
    store = state.StateStore(tmp_path / "state")
    monkeypatch.setattr(
        state,
        "_write_all",
        lambda *args: (_ for _ in ()).throw(OSError("short write")),
    )
    with pytest.raises(OSError, match="short write"):
        store.save_session(sample_lease())
    assert list((store.root / "sessions").iterdir()) == []


def test_lock_contention_times_out_without_replacing_lock_inode(tmp_path):
    store = state.StateStore(tmp_path / "state")
    store.save_session(sample_lease())
    lock_path = store.root / "state.lock"
    before = lock_path.stat()
    fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(state.StateLockTimeout):
            with state.StateStore(store.root, lock_timeout=0.01).locked():
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    after = lock_path.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_lock_path_replacement_during_acquisition_is_rejected(tmp_path, monkeypatch):
    store = state.StateStore(tmp_path / "state")
    store.save_session(sample_lease())
    lock_path = store.root / "state.lock"
    original_flock = state.fcntl.flock
    replaced = False

    def replace_during_flock(fd, operation):
        nonlocal replaced
        result = original_flock(fd, operation)
        if operation & fcntl.LOCK_EX and not replaced:
            replaced = True
            lock_path.unlink()
            write_private_file(lock_path, b"")
        return result

    monkeypatch.setattr(state.fcntl, "flock", replace_during_flock)
    with pytest.raises(state.UnsafeStatePath):
        with store.locked():
            pass


def test_event_rotation_is_bounded_private_and_preserves_newest_event(tmp_path):
    store = state.StateStore(tmp_path / "state")
    for index in range(80):
        store.append_event(
            {
                "schema_version": 1,
                "event": "scan",
                "reason_codes": [f"event-{index}-" + "x" * 65536],
            }
        )
    event_files = sorted(store.root.glob("events.jsonl*"))
    assert len(event_files) <= state.EVENT_LOG_BACKUPS + 1
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in event_files)
    assert "event-79-" in (store.root / "events.jsonl").read_text()


def test_read_only_audit_does_not_rotate_or_age_prune_event_files(tmp_path):
    store = state.StateStore(tmp_path / "state")
    store.append_event({"schema_version": 1, "event": "scan"})
    old_backup = store.root / "events.jsonl.1"
    write_private_file(old_backup, b"old\n")
    os.utime(old_backup, (1, 1))
    before = {path.name: (path.stat().st_ino, path.read_bytes()) for path in store.root.glob("events.jsonl*")}
    assert state.StateStore(store.root, read_only=True).load_sessions() == ()
    after = {path.name: (path.stat().st_ino, path.read_bytes()) for path in store.root.glob("events.jsonl*")}
    assert after == before
    store.append_event({"schema_version": 1, "event": "scan-again"})
    assert not old_backup.exists()


def test_mutation_prunes_completed_transactions_but_preserves_installed_reference(tmp_path):
    store = state.StateStore(tmp_path / "state")
    store.save_session(sample_lease())
    transactions = store.root / "transactions"
    make_private_directory(transactions)
    names = ["txn-1", "txn-2", "txn-3", "txn-4"]
    for index, name in enumerate(names, start=1):
        transaction = transactions / name
        make_private_directory(transaction)
        os.utime(transaction, (index, index))
    write_private_file(
        store.root / "install-state.json",
        json.dumps({"transaction_id": "txn-1"}).encode() + b"\n",
    )
    store.append_event({"schema_version": 1, "event": "mutate"})
    assert {path.name for path in transactions.iterdir()} == {"txn-1", "txn-3", "txn-4"}
