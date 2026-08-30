from __future__ import annotations

from dataclasses import replace
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import threading

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


def test_relative_root_is_anchored_before_process_chdir(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    make_private_directory(first)
    make_private_directory(second)
    monkeypatch.chdir(first)
    store = state.StateStore(Path("state"))
    monkeypatch.chdir(second)
    store.save_session(sample_lease())
    assert (first / "state" / "sessions").is_dir()
    assert not (second / "state").exists()


def test_intermediate_symlink_is_rejected_before_root_creation(tmp_path):
    real_parent = tmp_path / "real"
    make_private_directory(real_parent)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(state.UnsafeStatePath):
        state.StateStore(linked_parent / "state").save_session(sample_lease())
    assert not (real_parent / "state").exists()


def test_mutation_uses_pinned_root_after_path_is_rebound(tmp_path):
    root = tmp_path / "state"
    displaced = tmp_path / "displaced"
    store = state.StateStore(root)
    first = sample_lease()
    second = sample_lease("session:second")
    store.save_session(first)
    with store.locked():
        root.rename(displaced)
        make_private_directory(root)
        store.save_session(second)
    displaced_store = state.StateStore(displaced, read_only=True)
    assert displaced_store.load_sessions() == (first, second)
    assert state.StateStore(root, read_only=True).load_sessions() == ()


def test_session_filename_is_hash_not_untrusted_id(tmp_path):
    lease = sample_lease()
    store = state.StateStore(tmp_path / "state")
    store.save_session(lease)
    files = list((store.root / "sessions").iterdir())
    assert [path.name for path in files] == [
        hashlib.sha256(lease.session_id.encode()).hexdigest() + ".json"
    ]
    assert lease.session_id not in files[0].name
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert files[0].read_bytes() == (
        json.dumps(
            lease.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
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
def test_invalid_session_id_is_rejected_before_any_path_is_created(
    tmp_path, session_id
):
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
        state.StateStore(root).append_event(
            {"schema_version": 1, "event": "spawn", field: "canary"}
        )
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


def _build_unsafe_store(
    tmp_path: Path, unsafe_kind: str
) -> tuple[state.StateStore, Path]:
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
    monkeypatch.setattr(
        state.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("stop")),
    )
    with pytest.raises(OSError, match="stop"):
        store.save_session(updated)
    assert target.read_bytes() == before
    assert [path for path in target.parent.iterdir() if path.name != target.name] == []


def test_atomic_json_write_failure_removes_private_temporary_file(
    tmp_path, monkeypatch
):
    store = state.StateStore(tmp_path / "state")
    monkeypatch.setattr(
        state,
        "_write_all",
        lambda *args: (_ for _ in ()).throw(OSError("short write")),
    )
    with pytest.raises(OSError, match="short write"):
        store.save_session(sample_lease())
    assert list((store.root / "sessions").iterdir()) == []


def test_atomic_json_temp_collision_preserves_existing_sentinel(tmp_path, monkeypatch):
    store = state.StateStore(tmp_path / "state")
    store.save_session(sample_lease())
    sentinel = store.root / "sessions" / ".tmp-collision"
    write_private_file(sentinel, b"sentinel")
    monkeypatch.setattr(state.secrets, "token_hex", lambda size: "collision")
    with pytest.raises(FileExistsError):
        store.save_session(sample_lease("session:second"))
    assert sentinel.read_bytes() == b"sentinel"


def test_atomic_json_cleanup_preserves_temp_after_inode_gains_second_link(
    tmp_path, monkeypatch
):
    store = state.StateStore(tmp_path / "state")
    store.save_session(sample_lease())
    temporary = store.root / "sessions" / ".tmp-owned"
    second_link = store.root / "sessions" / ".tmp-second-link"
    monkeypatch.setattr(state.secrets, "token_hex", lambda size: "owned")

    def link_then_fail(fd, data):
        os.link(temporary, second_link)
        raise OSError("injected write failure")

    monkeypatch.setattr(state, "_write_all", link_then_fail)
    with pytest.raises(OSError, match="injected write failure"):
        store.save_session(sample_lease("session:second"))
    assert temporary.exists()
    assert second_link.exists()


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


def test_shared_store_serializes_different_threads(tmp_path):
    store = state.StateStore(tmp_path / "state", lock_timeout=1.0)
    owner_entered = threading.Event()
    release_owner = threading.Event()
    contender_started = threading.Event()
    contender_done = threading.Event()
    errors: list[BaseException] = []

    def owner():
        try:
            with store.locked():
                owner_entered.set()
                release_owner.wait(1.0)
        except BaseException as error:
            errors.append(error)

    def contender():
        try:
            contender_started.set()
            store.save_session(sample_lease())
        except BaseException as error:
            errors.append(error)
        finally:
            contender_done.set()

    owner_thread = threading.Thread(target=owner)
    contender_thread = threading.Thread(target=contender)
    owner_thread.start()
    assert owner_entered.wait(1.0)
    contender_thread.start()
    assert contender_started.wait(1.0)
    assert not contender_done.wait(0.05)
    release_owner.set()
    owner_thread.join(1.0)
    contender_thread.join(1.0)
    assert contender_done.is_set()
    assert errors == []
    assert store.load_sessions() == (sample_lease(),)


def test_unlock_error_does_not_leave_stale_cross_thread_ownership(
    tmp_path, monkeypatch
):
    store = state.StateStore(tmp_path / "state", lock_timeout=0.25)
    original_flock = state.fcntl.flock
    fail_unlock = True

    def injected_flock(fd, operation):
        nonlocal fail_unlock
        result = original_flock(fd, operation)
        if operation == fcntl.LOCK_UN and fail_unlock:
            fail_unlock = False
            raise OSError("injected unlock error")
        return result

    monkeypatch.setattr(state.fcntl, "flock", injected_flock)
    with pytest.raises(OSError, match="injected unlock error"):
        with store.locked():
            pass

    errors: list[BaseException] = []

    def retry():
        try:
            store.save_session(sample_lease())
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=retry)
    worker.start()
    worker.join(1.0)
    assert not worker.is_alive()
    assert errors == []


def test_real_unlock_rejects_same_thread_callback_until_release_cleanup(
    tmp_path, monkeypatch
):
    store = state.StateStore(tmp_path / "state", lock_timeout=1.0)
    original_flock = state.fcntl.flock
    callback_done = False
    callback_errors: list[BaseException] = []
    contender_errors: list[BaseException] = []
    contender_started = threading.Event()
    contender_done = threading.Event()
    contender_was_blocked: list[bool] = []
    contender_thread: list[threading.Thread] = []

    callback_lease = sample_lease("session:release-callback")
    contender_lease = sample_lease("session:release-contender")

    def contend():
        try:
            contender_started.set()
            store.save_session(contender_lease)
        except BaseException as error:
            contender_errors.append(error)
        finally:
            contender_done.set()

    def injected_flock(fd, operation):
        nonlocal callback_done
        result = original_flock(fd, operation)
        if operation == fcntl.LOCK_UN and not callback_done:
            callback_done = True
            try:
                store.save_session(callback_lease)
            except BaseException as error:
                callback_errors.append(error)
            worker = threading.Thread(target=contend)
            contender_thread.append(worker)
            worker.start()
            assert contender_started.wait(1.0)
            contender_was_blocked.append(not contender_done.wait(0.05))
        return result

    monkeypatch.setattr(state.fcntl, "flock", injected_flock)
    with store.locked():
        pass

    contender_thread[0].join(1.0)
    assert contender_was_blocked == [True]
    assert contender_done.is_set()
    assert contender_errors == []
    assert len(callback_errors) == 1
    assert isinstance(callback_errors[0], RuntimeError)

    final_lease = sample_lease("session:after-release")
    store.save_session(final_lease)
    assert set(store.load_sessions()) == {contender_lease, final_lease}


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf, -0.01])
def test_lock_timeout_must_be_finite_and_nonnegative(tmp_path, timeout):
    with pytest.raises(ValueError):
        state.StateStore(tmp_path / "state", lock_timeout=timeout)


def test_lock_timeout_is_converted_exactly_once(tmp_path):
    class Timeout:
        def __init__(self):
            self.calls = 0

        def __float__(self):
            self.calls += 1
            return 0.25

    timeout = Timeout()
    store = state.StateStore(tmp_path / "state", lock_timeout=timeout)
    assert store.lock_timeout == 0.25
    assert timeout.calls == 1


def _assert_fifo_call_finishes_without_peer(call, fifo: Path) -> None:
    finished = threading.Event()
    errors: list[BaseException] = []

    def invoke():
        try:
            call()
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=invoke)
    worker.start()
    prompt = finished.wait(0.1)
    writer_fd = None
    if not prompt:
        writer_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        worker.join(1.0)
    finally:
        if writer_fd is not None:
            os.close(writer_fd)
    assert prompt
    assert len(errors) == 1
    assert isinstance(errors[0], state.UnsafeStatePath)


def test_fifo_record_is_rejected_without_blocking(tmp_path):
    root = tmp_path / "state"
    processes = root / "processes"
    make_private_directory(processes)
    fifo = processes / "unsafe.json"
    os.mkfifo(fifo, mode=0o600)
    _assert_fifo_call_finishes_without_peer(
        state.StateStore(root, read_only=True).load_processes,
        fifo,
    )


def test_unix_socket_record_is_rejected_as_unsafe_path(tmp_path):
    root = tmp_path / "state"
    processes = root / "processes"
    make_private_directory(processes)
    socket_path = processes / "unsafe.json"
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(socket_path))
        socket_path.chmod(0o600)
        with pytest.raises(state.UnsafeStatePath):
            state.StateStore(root, read_only=True).load_processes()
    finally:
        server.close()


def test_fifo_event_log_is_rejected_without_blocking(tmp_path):
    store = state.StateStore(tmp_path / "state")
    store.save_session(sample_lease())
    fifo = store.root / "events.jsonl"
    os.mkfifo(fifo, mode=0o600)
    _assert_fifo_call_finishes_without_peer(
        lambda: store.append_event({"schema_version": 1, "event": "scan"}),
        fifo,
    )


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
    before = {
        path.name: (path.stat().st_ino, path.read_bytes())
        for path in store.root.glob("events.jsonl*")
    }
    assert state.StateStore(store.root, read_only=True).load_sessions() == ()
    after = {
        path.name: (path.stat().st_ino, path.read_bytes())
        for path in store.root.glob("events.jsonl*")
    }
    assert after == before
    store.append_event({"schema_version": 1, "event": "scan-again"})
    assert not old_backup.exists()


def test_mutation_removes_all_noncanonical_numeric_event_backups(tmp_path):
    store = state.StateStore(tmp_path / "state")
    store.append_event({"schema_version": 1, "event": "scan"})
    aliases = ["events.jsonl.0", "events.jsonl.00", "events.jsonl.01", "events.jsonl.4"]
    for name in aliases:
        write_private_file(store.root / name, b"alias\n")
    store.append_event({"schema_version": 1, "event": "scan-again"})
    assert all(not (store.root / name).exists() for name in aliases)
    assert {path.name for path in store.root.glob("events.jsonl.*")} <= set(
        state.EVENT_LOG_BACKUP_FILENAMES
    )


def test_mutation_prunes_completed_transactions_but_preserves_installed_reference(
    tmp_path,
):
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
        store.root / state.INSTALL_STATE_FILENAME,
        json.dumps({state.INSTALL_STATE_TRANSACTION_FIELD: "txn-1"}).encode() + b"\n",
    )
    store.append_event({"schema_version": 1, "event": "mutate"})
    assert {path.name for path in transactions.iterdir()} == {"txn-1", "txn-3", "txn-4"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"transaction_id": 1},
        {"transaction_id": ""},
        {"transaction_id": "txn-1", "extra": True},
    ],
)
def test_malformed_canonical_install_state_fails_before_transaction_pruning(
    tmp_path, payload
):
    store = state.StateStore(tmp_path / "state")
    store.save_session(sample_lease())
    transactions = store.root / "transactions"
    make_private_directory(transactions)
    for index in range(4):
        transaction = transactions / f"txn-{index}"
        make_private_directory(transaction)
        os.utime(transaction, (index + 1, index + 1))
    write_private_file(
        store.root / state.INSTALL_STATE_FILENAME,
        json.dumps(payload).encode() + b"\n",
    )
    with pytest.raises(state.StateCorruption):
        store.append_event({"schema_version": 1, "event": "mutate"})
    assert {path.name for path in transactions.iterdir()} == {
        "txn-0",
        "txn-1",
        "txn-2",
        "txn-3",
    }


@pytest.mark.parametrize("legacy_name", ["install_state.json", "install.json"])
def test_competing_install_state_alias_fails_before_transaction_pruning(
    tmp_path, legacy_name
):
    store = state.StateStore(tmp_path / "state")
    store.save_session(sample_lease())
    transactions = store.root / "transactions"
    make_private_directory(transactions)
    for index in range(4):
        transaction = transactions / f"txn-{index}"
        make_private_directory(transaction)
        os.utime(transaction, (index + 1, index + 1))
    canonical = {state.INSTALL_STATE_TRANSACTION_FIELD: "txn-0"}
    write_private_file(
        store.root / state.INSTALL_STATE_FILENAME,
        json.dumps(canonical).encode() + b"\n",
    )
    conflicting = {state.INSTALL_STATE_TRANSACTION_FIELD: "txn-1"}
    write_private_file(
        store.root / legacy_name, json.dumps(conflicting).encode() + b"\n"
    )
    with pytest.raises(state.StateCorruption):
        store.append_event({"schema_version": 1, "event": "mutate"})
    assert len(list(transactions.iterdir())) == 4


@pytest.mark.parametrize("legacy_name", ["install_state.json", "install.json"])
def test_legacy_install_state_without_canonical_authority_is_corruption(
    tmp_path, legacy_name
):
    store = state.StateStore(tmp_path / "state")
    store.save_session(sample_lease())
    payload = {state.INSTALL_STATE_TRANSACTION_FIELD: "txn-0"}
    write_private_file(store.root / legacy_name, json.dumps(payload).encode() + b"\n")
    with pytest.raises(state.StateCorruption):
        store.append_event({"schema_version": 1, "event": "mutate"})


@pytest.mark.parametrize(
    ("lease_state", "ended"),
    [
        ("active", "present"),
        ("ended", None),
    ],
)
def test_session_lease_rejects_cross_field_invalid_lifecycle(lease_state, ended):
    lease = sample_lease()
    payload = lease.to_dict()
    payload["state"] = lease_state
    payload["ended"] = lease.observed.to_dict() if ended == "present" else None
    with pytest.raises(ValueError):
        model.SessionLease.from_dict(payload)


def test_oversized_record_is_capped_corruption_and_writable_load_quarantines(tmp_path):
    root = tmp_path / "state"
    sessions = root / "sessions"
    make_private_directory(sessions)
    target = sessions / ("a" * 64 + ".json")
    write_private_file(target, b"{" + b'"padding":"' + b"x" * 1_100_000 + b'"}\n')

    with pytest.raises(state.StateCorruption):
        state.StateStore(root, read_only=True).load_sessions()
    assert target.exists()

    with pytest.raises(state.StateCorruption) as error:
        state.StateStore(root).load_sessions()
    assert not target.exists()
    assert error.value.quarantine_path is not None
    assert error.value.quarantine_path.exists()


def test_common_prefix_oversized_records_quarantine_to_unique_bounded_destinations(
    tmp_path,
):
    root = tmp_path / "state"
    sessions = root / "sessions"
    make_private_directory(sessions)
    prefix = b"x" * (state.STATE_RECORD_MAX_BYTES + 1)
    first = sessions / ("a" * 64 + ".json")
    second = sessions / ("b" * 64 + ".json")
    write_private_file(first, prefix + b"first")
    write_private_file(second, prefix + b"second")
    store = state.StateStore(root)

    with pytest.raises(state.StateCorruption):
        store.load_sessions()
    with pytest.raises(state.StateCorruption):
        store.load_sessions()

    assert not first.exists()
    assert not second.exists()
    quarantined = list((root / "corrupt").iterdir())
    assert len(quarantined) == 2
    assert any(path.read_bytes().endswith(b"first") for path in quarantined)
    assert any(path.read_bytes().endswith(b"second") for path in quarantined)


def test_quarantine_rename_failure_leaves_single_diagnosable_source(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "state"
    store = state.StateStore(root)
    sessions = root / "sessions"
    make_private_directory(root)
    make_private_directory(sessions)
    path = sessions / ("a" * 64 + ".json")
    write_private_file(path, b"not-json\n")
    original = state.os.rename
    monkeypatch.setattr(
        state.os,
        "rename",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rename failed")),
    )

    with pytest.raises(OSError):
        store.load_sessions()

    assert path.exists()
    assert path.stat().st_nlink == 1
    monkeypatch.setattr(state.os, "rename", original)
    with pytest.raises(state.StateCorruption) as error:
        store.load_sessions()
    assert error.value.quarantine_path is not None
    assert not path.exists()


def test_prepared_event_without_state_commit_recovers_without_false_event(
    tmp_path,
) -> None:
    store = state.StateStore(tmp_path / "state")
    expected = sample_process()
    updated = replace(expected, first_owner_gone_boot=200.0)
    store.save_process(expected)
    event_id = store.prepare_transition_event(
        "processes",
        expected.wrapper.stable_key(),
        expected,
        updated,
        {
            "schema_version": 1,
            "event": "owner_loss_observed",
            "observed_wall": expected.spawned.wall_iso,
            "process_key": expected.wrapper.stable_key(),
            "state": "exiting",
            "reason_codes": ["owner_session_ended"],
        },
    )

    store.recover_transition_events()

    assert store.load_processes()[0] == expected
    assert not (store.root / "events.jsonl").exists()
    assert not (store.root / "event-journal" / f"{event_id}.json").exists()


def test_prior_v1_process_without_owner_generation_loads_conservatively(
    tmp_path,
) -> None:
    store = state.StateStore(tmp_path / "state")
    process = sample_process()
    payload = process.to_dict()
    del payload["owner_generation"]
    directory = store.root / "processes"
    make_private_directory(directory)
    write_private_file(
        directory / f"{process.wrapper.stable_key()}.json",
        json.dumps(payload, sort_keys=True).encode() + b"\n",
    )

    loaded = store.load_processes()

    assert len(loaded) == 1
    assert loaded[0].owner_session_id == process.owner_session_id
    assert loaded[0].owner_generation is None
    assert not (store.root / "corrupt").exists()


def test_committed_event_in_rotated_log_is_not_duplicated(tmp_path) -> None:
    store = state.StateStore(tmp_path / "state")
    expected = sample_process()
    updated = replace(expected, first_owner_gone_boot=200.0)
    store.save_process(expected)
    event = {
        "schema_version": 1,
        "event": "owner_loss_observed",
        "observed_wall": expected.spawned.wall_iso,
        "process_key": expected.wrapper.stable_key(),
        "state": "exiting",
        "reason_codes": ["owner_session_ended"],
    }
    event_id = store.prepare_transition_event(
        "processes", expected.wrapper.stable_key(), expected, updated, event
    )
    store.save_process(updated)
    store.mark_transition_committed(event_id)
    store.append_event(dict(event, event_id=event_id), maintenance=False)
    os.rename(store.root / "events.jsonl", store.root / "events.jsonl.1")

    store.recover_transition_events()

    assert not (store.root / "events.jsonl").exists()
    backup = (store.root / "events.jsonl.1").read_text()
    assert backup.count(event_id) == 1


def test_deeply_nested_record_is_corruption_without_recursion_traceback(tmp_path):
    root = tmp_path / "state"
    sessions = root / "sessions"
    make_private_directory(sessions)
    target = sessions / ("a" * 64 + ".json")
    write_private_file(target, ("[" * 1100 + "0" + "]" * 1100).encode())
    with pytest.raises(state.StateCorruption):
        state.StateStore(root, read_only=True).load_sessions()


def test_huge_integer_record_is_normalized_to_state_corruption(tmp_path):
    root = tmp_path / "state"
    sessions = root / "sessions"
    make_private_directory(sessions)
    target = sessions / ("a" * 64 + ".json")
    raw = sample_lease().to_dict()
    rendered = json.dumps(raw).replace("12.5", "9" * 5000).encode()
    write_private_file(target, rendered)
    with pytest.raises(state.StateCorruption):
        state.StateStore(root, read_only=True).load_sessions()
