from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
import signal
import shutil

import pytest

from codex_mcp_ownership import classify, cleanup, cli, model, procfs, state
from helpers import FakeClock, sample_process, write_proc_entry


class FakeSignalBackend:
    def __init__(self, *, on_open=None, on_send=None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._on_open = on_open
        self._on_send = on_send
        self._identities_by_pidfd: dict[int, model.ProcessIdentity] = {}

    def open(self, identity: model.ProcessIdentity) -> int:
        self.calls.append(("open", identity))
        pidfd = identity.pid + 1000
        self._identities_by_pidfd[pidfd] = identity
        if self._on_open is not None:
            self._on_open(identity)
        return pidfd

    def send(self, pidfd: int, signum: int) -> None:
        self.calls.append(("send", pidfd, signum))
        if self._on_send is not None:
            self._on_send(self._identities_by_pidfd[pidfd], signum)

    def close(self, pidfd: int) -> None:
        self.calls.append(("close", pidfd))


class ExactProcfs:
    def __init__(self, identities: tuple[model.ProcessIdentity, ...]) -> None:
        self.identities = {identity.pid: identity for identity in identities}

    def observe_identity(self, pid: int) -> procfs.IdentityObservation:
        identity = self.identities.get(pid)
        if identity is None:
            return procfs.IdentityObservation("missing", None)
        return procfs.IdentityObservation("live", identity)

    def rss_kib(self, identity: model.ProcessIdentity) -> int | None:
        return 64 if self.identities.get(identity.pid) == identity else None


def change_start_ticks(
    tree: procfs.LinuxProcfs,
    identity: model.ProcessIdentity,
) -> None:
    stat_path = tree.proc_root / str(identity.pid) / "stat"
    raw = stat_path.read_text(encoding="utf-8")
    right = raw.rfind(")")
    fields = raw[right + 1 :].strip().split()
    fields[19] = str(identity.start_ticks + 1)
    stat_path.write_text(
        raw[: right + 1] + " " + " ".join(fields) + "\n",
        encoding="utf-8",
    )


def remove_process(tree: procfs.LinuxProcfs, pid: int) -> None:
    process_path = tree.proc_root / str(pid)
    for child in process_path.iterdir():
        child.unlink()
    process_path.rmdir()


def decode_force_token(token: str) -> dict[str, object]:
    encoded, _digest = token.split(".", 1)
    padding = "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + padding))


def encode_force_token(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode()
    encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode()
    return f"{encoded}.{hashlib.sha256(canonical).hexdigest()}"


def encode_force_token_bytes(canonical: bytes) -> str:
    encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode()
    return f"{encoded}.{hashlib.sha256(canonical).hexdigest()}"


def ended_owner_context_without_first_loss(tmp_path, fake_proc):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    identity = tree.identity(321)
    assert identity is not None
    observed = model.ObservedTime("2026-08-29T00:00:00+00:00", identity.boot_id, 50.0)
    lease = model.SessionLease(
        1,
        "session:convergence",
        "/workspace",
        "startup",
        (identity.stable_key(),),
        "ended",
        observed,
        replace(observed, boottime=60.0),
    )
    process = model.ManagedProcess(
        1,
        identity.stable_key(),
        "user",
        "convergence",
        "/workspace",
        identity,
        None,
        (identity,),
        identity.pgid,
        frozenset({identity.stable_key()}),
        observed,
        owner_session_id=lease.session_id,
        owner_generation=model.lease_generation_digest(lease),
    )
    store = state.StateStore(tmp_path / "state")
    store.save_session(lease)
    store.save_process(process)
    return store, tree, process


def test_apply_persists_first_owner_loss_once_and_next_scan_converges(
    tmp_path, fake_proc
):
    store, tree, process = ended_owner_context_without_first_loss(tmp_path, fake_proc)
    clock = FakeClock(boot=200.0)
    signaler = FakeSignalBackend()

    first = cleanup.execute_cleanup((), store, tree, signaler, clock, apply=True)
    stored = store.load_processes()[0]
    assert first.attempted == 0
    assert stored.first_owner_gone_boot == 200.0
    events = (store.root / "events.jsonl").read_text().splitlines()
    assert sum('"event":"owner_loss_observed"' in line for line in events) == 1

    cleanup.execute_cleanup((), store, tree, signaler, clock, apply=True)
    assert store.load_processes()[0].first_owner_gone_boot == 200.0
    assert (store.root / "events.jsonl").read_text().splitlines() == events

    clock.advance(121.0)
    snapshot = classify.build_audit(store, tree, clock)
    assert snapshot.classifications[0].state == "orphan"
    assert cleanup.plan_cleanup(snapshot)[0].process_key == process.wrapper.stable_key()


def test_first_owner_loss_cas_rejects_concurrent_lease_switch(
    tmp_path, fake_proc, monkeypatch
):
    store, tree, process = ended_owner_context_without_first_loss(tmp_path, fake_proc)
    ended = store.load_sessions()[0]
    original = cleanup._cas_process_and_event
    switched = False

    def switch_lease_then_cas(*args, **kwargs):
        nonlocal switched
        if not switched:
            store.save_session(replace(ended, state="active", ended=None))
            switched = True
        return original(*args, **kwargs)

    monkeypatch.setattr(cleanup, "_cas_process_and_event", switch_lease_then_cas)
    cleanup.execute_cleanup(
        (),
        store,
        tree,
        FakeSignalBackend(),
        FakeClock(boot=200.0),
        apply=True,
    )

    assert store.load_processes()[0] == process
    assert not (store.root / "events.jsonl").exists()


def test_first_owner_loss_outbox_retries_append_failure_exactly_once(
    tmp_path, fake_proc, monkeypatch
):
    store, tree, _process = ended_owner_context_without_first_loss(tmp_path, fake_proc)
    original_append = store.append_event
    failed = False

    def fail_owner_loss_once(event, **kwargs):
        nonlocal failed
        if event.get("event") == "owner_loss_observed" and not failed:
            failed = True
            raise OSError("OUTBOX-CANARY")
        return original_append(event, **kwargs)

    monkeypatch.setattr(store, "append_event", fail_owner_loss_once)
    cleanup.execute_cleanup(
        (), store, tree, FakeSignalBackend(), FakeClock(boot=200.0), apply=True
    )
    assert store.load_processes()[0].first_owner_gone_boot == 200.0

    monkeypatch.setattr(store, "append_event", original_append)
    cleanup.execute_cleanup(
        (), store, tree, FakeSignalBackend(), FakeClock(boot=200.0), apply=True
    )

    events = [
        json.loads(line)
        for line in (store.root / "events.jsonl").read_text().splitlines()
    ]
    assert [event["event"] for event in events] == ["owner_loss_observed"]
    assert list((store.root / "event-journal").iterdir()) == []


def test_committed_state_write_reported_failure_recovers_exact_event(
    tmp_path, fake_proc, monkeypatch
) -> None:
    store, tree, _process = ended_owner_context_without_first_loss(tmp_path, fake_proc)
    original_save = store.save_process
    raised = False

    def commit_then_raise(process, **kwargs):
        nonlocal raised
        original_save(process, **kwargs)
        if process.first_owner_gone_boot is not None and not raised:
            raised = True
            raise OSError("COMMITTED-WRITE-CANARY")

    monkeypatch.setattr(store, "save_process", commit_then_raise)
    cleanup.execute_cleanup(
        (), store, tree, FakeSignalBackend(), FakeClock(boot=200.0), apply=True
    )

    assert store.load_processes()[0].first_owner_gone_boot == 200.0
    events = (store.root / "events.jsonl").read_text().splitlines()
    assert sum('"event":"owner_loss_observed"' in line for line in events) == 1
    assert list((store.root / "event-journal").iterdir()) == []


def test_build_audit_rejects_caller_held_mutable_lock_without_external_work(
    tmp_path, fake_proc
):
    store, tree, _process = ended_owner_context_without_first_loss(tmp_path, fake_proc)
    with store.locked():
        with pytest.raises(ValueError, match="mutable lock"):
            classify.build_audit(store, tree, FakeClock(boot=200.0))


def test_build_audit_held_mutable_lock_never_quarantines_corrupt_record(
    tmp_path, fake_proc
):
    root = tmp_path / "state"
    sessions = root / "sessions"
    sessions.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    sessions.chmod(0o700)
    corrupt = sessions / ("a" * 64 + ".json")
    corrupt.write_bytes(b"{corrupt}\n")
    corrupt.chmod(0o600)
    store = state.StateStore(root)
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)

    with store.locked():
        with pytest.raises(ValueError, match="mutable lock"):
            classify.build_audit(store, tree, FakeClock(boot=200.0))

    assert corrupt.exists()
    assert not (root / "corrupt").exists()


def test_apply_never_observes_procfs_or_rss_while_holding_global_lock(
    orphan_context,
):
    store, tree, clock, _snapshot, process, _lease = orphan_context
    store.save_process(replace(process, first_owner_gone_boot=100.0))
    snapshot = classify.build_audit(store, tree, clock)
    assert snapshot.classifications[0].eligible_term is True

    class GuardedProcfs:
        def __getattr__(self, name):
            target = getattr(tree, name)
            if name not in {"observe_identity", "identity", "rss_kib", "boot_id"}:
                return target

            def guarded(*args, **kwargs):
                assert not store._owns_lock()
                return target(*args, **kwargs)

            return guarded

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        GuardedProcfs(),
        FakeSignalBackend(),
        clock,
        apply=True,
    )
    assert report.attempted == 1


def state_tree(root) -> tuple[tuple[str, bytes | None], ...]:
    return tuple(
        (str(path.relative_to(root)), path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    )


def cleanup_events(store: state.StateStore) -> list[dict[str, object]]:
    path = store.root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def cleanup_event(store: state.StateStore, name: str) -> dict[str, object]:
    matches = [event for event in cleanup_events(store) if event["event"] == name]
    assert len(matches) == 1
    return matches[0]


def snapshot_for(
    state_name: str,
    *,
    eligible_term: bool = False,
) -> model.AuditSnapshot:
    process = sample_process()
    identity = process.wrapper
    classification = model.Classification(
        process=process,
        state=state_name,
        reason_codes=(f"{state_name}_reason",),
        live_identities=() if state_name == "gone" else (identity,),
        grace_deadline_boot=None,
        eligible_term=eligible_term,
    )
    return model.AuditSnapshot(
        schema_version=1,
        generated=model.ObservedTime(
            "2026-08-29T00:00:00+00:00",
            identity.boot_id,
            300.0,
        ),
        classifications=(classification,),
        state_counts=((state_name, 1),),
        process_count=len(classification.live_identities),
        rss_kib=64 if classification.live_identities else 0,
        ownership_coverage=(),
        corrupt_count=0,
    )


@pytest.fixture
def orphan_context(tmp_path, fake_proc):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    identity = tree.identity(321)
    assert identity is not None
    observed = model.ObservedTime(
        "2026-08-29T00:00:00+00:00",
        identity.boot_id,
        12.5,
    )
    lease = model.SessionLease(
        schema_version=1,
        session_id="session:test_1",
        cwd="/workspace",
        source="SessionStart",
        host_keys=(identity.stable_key(),),
        state="ended",
        observed=observed,
        ended=replace(observed, boottime=20.0),
    )
    process = model.ManagedProcess(
        schema_version=1,
        record_id="managed-example",
        scope="user",
        server="example",
        cwd="/workspace",
        wrapper=identity,
        child=None,
        members=(identity,),
        pgid=identity.pgid,
        host_keys=frozenset({identity.stable_key()}),
        spawned=observed,
        owner_session_id=lease.session_id,
        owner_generation=model.lease_generation_digest(lease),
        first_owner_gone_boot=100.0,
    )
    store = state.StateStore(tmp_path / "state")
    store.save_session(lease)
    store.save_process(process)
    clock = FakeClock(boot=300.0)
    snapshot = classify.build_audit(store, tree, clock)
    assert snapshot.classifications[0].state == "orphan"
    return store, tree, clock, snapshot, process, lease


@pytest.fixture
def stubborn_context(orphan_context):
    store, tree, clock, _snapshot, process, lease = orphan_context
    stubborn_process = replace(
        process,
        term_sent_boot=280.0,
        term_sent_keys=frozenset({process.wrapper.stable_key()}),
    )
    store.save_process(stubborn_process)
    snapshot = classify.build_audit(store, tree, clock)
    classification = snapshot.classifications[0]
    assert classification.state == "stubborn"
    return store, tree, clock, snapshot, stubborn_process, lease, classification


def test_dry_run_never_locks_store_or_opens_pidfd(tmp_path, monkeypatch) -> None:
    snapshot = snapshot_for("orphan", eligible_term=True)
    signaler = FakeSignalBackend()
    identity = snapshot.classifications[0].live_identities[0]
    second = replace(identity, pid=322, start_ticks=424243)
    unrelated = replace(
        snapshot.classifications[0].process,
        record_id="shared-record",
        wrapper=second,
        members=(second,),
        pgid=second.pgid,
        shared_owner="user:shared",
    )
    store = state.StateStore(tmp_path / "state")
    store.save_process(snapshot.classifications[0].process)
    store.save_process(unrelated)
    before = state_tree(store.root)

    def forbidden_lock():
        raise AssertionError("dry run must not acquire the state lock")

    monkeypatch.setattr(store, "locked", forbidden_lock)

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        ExactProcfs((identity, second)),
        signaler,
        FakeClock(boot=300.0),
        apply=False,
    )

    assert report.attempted == 0
    assert report.before_count == 2
    assert report.before_rss_kib == 128
    assert report.after_count == 2
    assert report.after_rss_kib == 128
    assert signaler.calls == []
    assert state_tree(store.root) == before


def test_no_action_preview_reports_all_exact_managed_processes(
    tmp_path,
    monkeypatch,
) -> None:
    process = sample_process()
    identity = process.wrapper
    second = replace(identity, pid=322, start_ticks=424243)
    unrelated = replace(
        process,
        record_id="shared-record",
        wrapper=second,
        members=(second,),
        pgid=second.pgid,
        shared_owner="user:shared",
    )
    store = state.StateStore(tmp_path / "state")
    store.save_process(process)
    store.save_process(unrelated)
    before = state_tree(store.root)
    monkeypatch.setattr(
        store,
        "locked",
        lambda: (_ for _ in ()).throw(
            AssertionError("dry run must not acquire the state lock")
        ),
    )

    report = cleanup.execute_cleanup(
        (),
        store,
        ExactProcfs((identity, second)),
        FakeSignalBackend(),
        FakeClock(boot=300.0),
        apply=False,
    )

    assert (report.before_count, report.before_rss_kib) == (2, 128)
    assert (report.after_count, report.after_rss_kib) == (2, 128)
    assert report.outcomes == ()
    assert state_tree(store.root) == before


@pytest.mark.parametrize(
    "state_name",
    ["active", "shared", "exiting", "unknown", "gone"],
)
def test_non_orphans_never_become_automatic_actions(state_name: str) -> None:
    assert cleanup.plan_cleanup(snapshot_for(state_name)) == ()


def test_orphan_without_explicit_term_eligibility_is_not_actionable() -> None:
    snapshot = snapshot_for("orphan", eligible_term=False)
    assert cleanup.plan_cleanup(snapshot) == ()


def test_automatic_plan_contains_only_exact_live_orphan_identities() -> None:
    snapshot = snapshot_for("orphan", eligible_term=True)
    classification = snapshot.classifications[0]
    second = replace(classification.live_identities[0], pid=322, start_ticks=424243)
    snapshot = replace(
        snapshot,
        classifications=(
            replace(
                classification,
                live_identities=(second, classification.live_identities[0]),
            ),
        ),
    )

    actions = cleanup.plan_cleanup(snapshot)

    assert [action.identity.stable_key() for action in actions] == sorted(
        identity.stable_key()
        for identity in (classification.live_identities[0], second)
    )
    assert all(action.classification_state == "orphan" for action in actions)
    assert all(action.force is False for action in actions)


def test_apply_reaudits_under_lock_and_persists_term_survivor(orphan_context) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context

    class LockCheckingBackend(FakeSignalBackend):
        def open(self, identity: model.ProcessIdentity) -> int:
            assert not store._owns_lock()
            return super().open(identity)

    signaler = LockCheckingBackend()
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 1
    assert report.survived == 1
    assert report.terminated == 0
    assert report.skipped == 0
    assert dict(report.before_state_counts)["orphan"] == 1
    assert sum(dict(report.after_state_counts).values()) == 1
    assert report.before_classifications[0].state == "orphan"
    assert report.after_classifications[0].state in {"exiting", "orphan", "stubborn"}
    assert report.outcomes[0].status == "survived"
    assert report.outcomes[0].reason == "sigterm_survived"
    assert report.before_count == 1
    assert report.before_rss_kib == 128
    assert report.after_count == 1
    assert report.after_rss_kib == 128
    assert store.load_processes()[0] == replace(
        process,
        term_sent_boot=300.0,
        term_sent_keys=frozenset({process.wrapper.stable_key()}),
    )
    events = cleanup_events(store)
    assert [event["event"] for event in events] == [
        "cleanup_term_pending",
        "cleanup_term_dispatch",
        "cleanup_term_delivery_receipt",
        "cleanup_term_sent",
    ]
    final = dict(events[-1])
    event_id = final.pop("event_id")
    assert len(event_id) == 64
    assert final == {
        "event": "cleanup_term_sent",
        "observed_wall": "2026-08-29T00:00:00+00:00",
        "process_key": process.wrapper.stable_key(),
        "reason_codes": [
            "owner_session_ended",
            "owner_grace_elapsed",
            "sigterm_survived",
        ],
        "schema_version": 1,
        "scope": "user",
        "server": "example",
        "state": "exiting",
    }


def test_concurrent_stale_supervisor_update_cannot_erase_term_receipt(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context

    def supervisor_refresh(_identity, _signum) -> None:
        current = store.load_processes()[0]
        store.save_process(
            replace(
                current,
                owner_reason_codes=current.owner_reason_codes + ("supervisor_refresh",),
            ),
            maintenance=False,
        )

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        FakeSignalBackend(on_send=supervisor_refresh),
        clock,
        apply=True,
    )

    assert report.attempted == 1
    assert report.outcomes[0].reason == "sigterm_survived"
    persisted = store.load_processes()[0]
    assert persisted.term_sent_boot == 300.0
    assert persisted.term_sent_keys
    followup = classify.build_audit(store, tree, clock)
    assert followup.classifications[0].state == "exiting"
    assert followup.classifications[0].eligible_term is False
    events = cleanup_events(store)
    assert events[-1]["event"] == "cleanup_term_sent"


def test_persistence_conflict_is_journaled_against_separate_intent(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context
    classification = snapshot.classifications[0]
    intent = model.SignalIntent(
        1,
        process.wrapper.stable_key(),
        process.owner_generation,
        tuple(
            sorted(identity.stable_key() for identity in classification.live_identities)
        ),
        "term",
        "pending",
        (),
    )
    store.save_signal_intent(intent)
    authority = cleanup.capture_authorized_audit(
        store,
        tree,
        clock,
        expected_root_binding=store.root_binding(),
    )
    conflicted = replace(intent, status="conflict")

    revision = cleanup._persist_intent_status(
        store,
        authority,
        authority.revision,
        intent,
        conflicted,
        {
            "schema_version": 1,
            "event": "cleanup_state_persistence_conflict",
            "observed_wall": process.spawned.wall_iso,
            "server": process.server,
            "scope": process.scope,
            "process_key": process.wrapper.stable_key(),
            "state": "unknown",
            "reason_codes": ["state_persistence_conflict"],
        },
    )

    assert revision is not None
    persisted = store.load_signal_intent(process.wrapper.stable_key())
    assert persisted is not None
    assert persisted.status == "conflict"
    assert list((store.root / "event-journal").iterdir()) == []
    events = (store.root / "events.jsonl").read_text().splitlines()
    assert (
        sum('"event":"cleanup_state_persistence_conflict"' in line for line in events)
        == 1
    )


def test_apply_metrics_include_unrelated_exact_shared_process(orphan_context) -> None:
    store, tree, clock, _snapshot, process, _lease = orphan_context
    write_proc_entry(
        tree.proc_root,
        322,
        "322 (shared) S 1 322 322 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 3220 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t32 kB\n",
    )
    shared_identity = tree.identity(322)
    assert shared_identity is not None
    shared = replace(
        process,
        record_id="shared-record",
        wrapper=shared_identity,
        child=None,
        members=(shared_identity,),
        pgid=shared_identity.pgid,
        owner_session_id=None,
        shared_owner="user:shared",
        first_owner_gone_boot=None,
    )
    store.save_process(shared)
    snapshot = classify.build_audit(store, tree, clock)
    assert {item.state for item in snapshot.classifications} == {"orphan", "shared"}

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        FakeSignalBackend(),
        clock,
        apply=True,
    )

    assert report.attempted == 1
    assert (report.before_count, report.before_rss_kib) == (2, 160)
    assert (report.after_count, report.after_rss_kib) == (2, 160)
    stored = {item.record_id: item for item in store.load_processes()}
    assert stored["shared-record"] == shared


def test_apply_skips_action_when_locked_reaudit_is_no_longer_orphan(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, lease = orphan_context
    store.save_session(replace(lease, state="active", ended=None))
    signaler = FakeSignalBackend()

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 0
    assert report.skipped == 1
    assert report.outcomes[0].reason == "classification_changed"
    assert signaler.calls == []
    assert store.load_processes()[0] == process
    assert not (store.root / "events.jsonl").exists()


def test_pid_reuse_before_pidfd_open_sends_no_signal(
    orphan_context,
    monkeypatch,
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context
    changed = False

    original_observe = cleanup._observe_identity

    def mutate_before_observe(*args, **kwargs):
        nonlocal changed
        if not changed:
            change_start_ticks(tree, process.wrapper)
            changed = True
        return original_observe(*args, **kwargs)

    monkeypatch.setattr(cleanup, "_observe_identity", mutate_before_observe)
    signaler = FakeSignalBackend()

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 0
    assert report.skipped == 1
    assert report.outcomes[0].reason == "identity_changed"
    assert signaler.calls == []
    assert store.load_processes()[0] == process
    assert not (store.root / "events.jsonl").exists()


def test_pid_reuse_after_pidfd_open_closes_once_and_sends_no_signal(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context
    signaler = FakeSignalBackend(
        on_open=lambda identity: change_start_ticks(tree, identity)
    )

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 0
    assert report.skipped == 1
    assert report.outcomes[0].reason == "identity_changed_after_pidfd"
    assert signaler.calls == [
        ("open", process.wrapper),
        ("close", process.wrapper.pid + 1000),
    ]
    assert store.load_processes()[0] == process
    assert not (store.root / "events.jsonl").exists()


def test_lease_switch_after_pidfd_preparation_prevents_term(orphan_context) -> None:
    store, tree, clock, snapshot, _process, lease = orphan_context

    def activate_owner(_identity):
        store.save_session(replace(lease, state="active", ended=None))

    signaler = FakeSignalBackend(on_open=activate_owner)
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert [call for call in signaler.calls if call[0] == "send"] == []
    assert report.attempted == 0
    assert report.outcomes[0].reason == "state_authority_changed"


def test_root_rebind_after_pidfd_preparation_prevents_term(
    orphan_context, tmp_path
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    displaced = tmp_path / "prepared-root"

    def rebind(_identity):
        store.root.rename(displaced)
        store.root.mkdir(mode=0o700)

    signaler = FakeSignalBackend(on_open=rebind)
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert [call for call in signaler.calls if call[0] == "send"] == []
    assert report.attempted == 0
    assert report.outcomes[0].reason == "state_authority_changed"
    assert list(store.root.iterdir()) == []


def test_audited_root_replacement_with_same_records_has_no_authority(
    orphan_context, tmp_path
) -> None:
    store, tree, clock, _snapshot, _process, _lease = orphan_context
    authority = cleanup.capture_authorized_audit(
        store,
        tree,
        clock,
        expected_root_binding=store.root_binding(),
    )
    snapshot = authority.snapshot
    records = state_tree(store.root)
    displaced = tmp_path / "audited-root"
    store.root.rename(displaced)
    store.root.mkdir(mode=0o700)
    for relative, payload in records:
        target = store.root / relative
        if payload is None:
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(payload)
            target.chmod(0o600)
    signaler = FakeSignalBackend()

    with pytest.raises(state.UnsafeStatePath):
        cleanup.execute_cleanup(
            cleanup.plan_cleanup(snapshot),
            store,
            tree,
            signaler,
            clock,
            apply=True,
            authority=authority,
        )

    assert signaler.calls == []
    assert state_tree(store.root) == records


def test_lexical_rebind_during_intent_save_prevents_delivery(
    orphan_context, tmp_path, monkeypatch
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    displaced = tmp_path / "intent-root"
    original = store.transition
    rebound = False

    def save_then_rebind(*args, **kwargs):
        nonlocal rebound
        revision = original(*args, **kwargs)
        updated = args[3]
        if (
            not rebound
            and isinstance(updated, model.SignalIntent)
            and updated.action == "term"
            and updated.status == "pending"
            and not updated.delivered_keys
        ):
            store.root.rename(displaced)
            store.root.mkdir(mode=0o700)
            rebound = True
        return revision

    monkeypatch.setattr(store, "transition", save_then_rebind)
    signaler = FakeSignalBackend()
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert [call for call in signaler.calls if call[0] == "send"] == []
    assert report.outcomes[0].reason == "state_authority_changed"
    assert list(store.root.iterdir()) == []


def test_new_competing_lease_after_pidfd_preparation_prevents_delivery(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, lease = orphan_context
    competitor = replace(
        lease,
        session_id="competing-session",
        state="active",
        ended=None,
    )
    signaler = FakeSignalBackend(
        on_open=lambda _identity: store.save_session(competitor)
    )
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert [call for call in signaler.calls if call[0] == "send"] == []
    assert report.outcomes[0].reason == "state_authority_changed"
    assert store.load_processes()[0].wrapper == process.wrapper


def test_authority_loss_after_signal_reports_after_state_unavailable(
    orphan_context, tmp_path
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    displaced = tmp_path / "signaled-root"

    def rebind_after_send(_identity, _signum) -> None:
        store.root.rename(displaced)
        store.root.mkdir(mode=0o700)

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        FakeSignalBackend(on_send=rebind_after_send),
        clock,
        apply=True,
    )

    assert report.attempted == 1
    assert report.after_state_available is False
    assert report.authority_lost is True
    assert report.after_classifications == ()
    assert report.after_state_counts == ()
    assert report.after_count == 0
    assert report.after_rss_kib == 0


def test_partial_subtree_reuse_skips_only_changed_identity(orphan_context) -> None:
    store, tree, clock, _snapshot, process, _lease = orphan_context
    write_proc_entry(
        tree.proc_root,
        322,
        "322 (child) S 321 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 3220 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t32 kB\n",
    )
    write_proc_entry(
        tree.proc_root,
        323,
        "323 (worker) S 322 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 3230 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t64 kB\n",
    )
    child = tree.identity(322)
    worker = tree.identity(323)
    assert child is not None
    assert worker is not None
    expanded = replace(
        process,
        child=child,
        members=(process.wrapper, child, worker),
    )
    store.save_process(expanded)
    snapshot = classify.build_audit(store, tree, clock)
    signaler = FakeSignalBackend(
        on_open=lambda identity: (
            change_start_ticks(tree, identity) if identity.pid == child.pid else None
        )
    )

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    sent_pids = {
        signaler._identities_by_pidfd[call[1]].pid
        for call in signaler.calls
        if call[0] == "send"
    }
    assert sent_pids == {process.wrapper.pid, worker.pid}
    assert report.attempted == 2
    assert report.survived == 2
    assert report.skipped == 1
    assert report.before_count == 3
    assert report.before_rss_kib == 224
    assert report.after_count == 2
    assert report.after_rss_kib == 192
    assert store.load_processes()[0].term_sent_boot is None


def test_sigterm_process_disappearance_is_immediately_terminated(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context
    signaler = FakeSignalBackend(
        on_send=lambda identity, _signum: remove_process(tree, identity.pid)
    )

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 1
    assert report.terminated == 1
    assert report.survived == 0
    assert report.outcomes[0].reason == "sigterm_terminated"
    assert report.after_count == 0
    assert report.after_rss_kib == 0
    assert store.load_processes()[0].term_sent_boot is None
    event = cleanup_event(store, "cleanup_terminated")
    assert event["event"] == "cleanup_terminated"
    assert event["state"] == "gone"


def test_pidfd_backend_uses_only_pidfd_operations(monkeypatch) -> None:
    identity = sample_process().wrapper
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        cleanup.os,
        "pidfd_open",
        lambda pid, flags: calls.append(("open", pid, flags)) or 91,
    )
    monkeypatch.setattr(
        cleanup.signal,
        "pidfd_send_signal",
        lambda pidfd, signum, siginfo, flags: calls.append(
            ("send", pidfd, signum, siginfo, flags)
        ),
    )
    monkeypatch.setattr(
        cleanup.os, "close", lambda pidfd: calls.append(("close", pidfd))
    )

    backend = cleanup.PidfdSignalBackend()
    pidfd = backend.open(identity)
    backend.send(pidfd, signal.SIGTERM)
    backend.close(pidfd)

    assert calls == [
        ("open", identity.pid, 0),
        ("send", 91, signal.SIGTERM, None, 0),
        ("close", 91),
    ]


@pytest.mark.parametrize("missing", ["pidfd_open", "pidfd_send_signal"])
def test_pidfd_backend_requires_open_and_send_support(
    monkeypatch, missing: str
) -> None:
    monkeypatch.setattr(
        cleanup.os, "pidfd_open", lambda _pid, _flags: 91, raising=False
    )
    monkeypatch.setattr(
        cleanup.signal,
        "pidfd_send_signal",
        lambda _pidfd, _signum, _siginfo, _flags: None,
        raising=False,
    )
    owner = cleanup.os if missing == "pidfd_open" else cleanup.signal
    monkeypatch.delattr(owner, missing)

    with pytest.raises(cleanup.PidfdUnavailable):
        cleanup.PidfdSignalBackend()


def test_pidfd_unavailable_is_diagnostic_only(orphan_context) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context

    class UnavailableBackend(FakeSignalBackend):
        def open(self, identity: model.ProcessIdentity) -> int:
            self.calls.append(("open", identity))
            raise cleanup.PidfdUnavailable("unsupported")

    signaler = UnavailableBackend()
    before = state_tree(store.root)
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 0
    assert report.skipped == 1
    assert report.outcomes[0].reason == "pidfd_unavailable"
    assert report.before_classifications[0].state == "orphan"
    assert report.after_classifications[0].state == "orphan"
    assert signaler.calls == [("open", process.wrapper)]
    assert state_tree(store.root) == before


def test_force_plan_contains_only_stubborn_exact_identities(stubborn_context) -> None:
    _store, _tree, _clock, snapshot, process, _lease, classification = stubborn_context

    actions = cleanup.plan_cleanup(snapshot, force=True)

    assert len(actions) == 1
    assert actions[0].process_key == process.wrapper.stable_key()
    assert actions[0].identity == classification.live_identities[0]
    assert actions[0].classification_state == "stubborn"
    assert actions[0].reason_codes == classification.reason_codes
    assert actions[0].force is True
    assert cleanup.plan_cleanup(snapshot) == ()
    assert (
        cleanup.plan_cleanup(snapshot_for("orphan", eligible_term=True), force=True)
        == ()
    )


def test_force_plan_and_token_reject_fabricated_stubborn_without_term_keys() -> None:
    snapshot = snapshot_for("stubborn")
    classification = snapshot.classifications[0]
    assert classification.process.term_sent_boot is None
    assert classification.process.term_sent_keys == frozenset()

    assert cleanup.plan_cleanup(snapshot, force=True) == ()
    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.issue_force_token(classification, FakeClock(boot=300.0))


def test_force_token_is_canonical_exact_evidence(stubborn_context) -> None:
    _store, _tree, clock, _snapshot, _process, _lease, classification = stubborn_context

    token = cleanup.issue_force_token(classification, clock)
    payload = decode_force_token(token)

    assert set(payload) == {
        "boot_id",
        "expires_boot",
        "identity_keys",
        "issued_boot",
        "reason_codes",
        "schema_version",
        "term_sent_boot",
    }
    assert payload["schema_version"] == 1
    assert payload["boot_id"] == "test-boot-id"
    assert payload["identity_keys"] == sorted(
        identity.stable_key() for identity in classification.live_identities
    )
    assert payload["reason_codes"] == list(classification.reason_codes)
    assert payload["term_sent_boot"] == classification.process.term_sent_boot
    assert payload["issued_boot"] == 300.0
    assert payload["expires_boot"] == 600.0
    assert "=" not in token


@pytest.mark.parametrize(
    "canonical",
    [
        ("[" * 1100 + "0" + "]" * 1100).encode(),
        b'{"schema_version":' + b"9" * 5000 + b"}",
        b'"' + b"x" * 20_000 + b'"',
    ],
    ids=("deep", "huge-number", "oversized"),
)
def test_force_token_resource_failures_are_invalid_confirmation(canonical) -> None:
    token = encode_force_token_bytes(canonical)
    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup._decode_force_token(token, 300.0)


def test_force_token_invalid_unicode_scalar_is_invalid_confirmation() -> None:
    canonical = json.dumps(
        {
            "boot_id": "test-boot-id",
            "expires_boot": 600.0,
            "identity_keys": ["0" * 64],
            "issued_boot": 300.0,
            "reason_codes": ["\ud800"],
            "schema_version": 1,
            "term_sent_boot": 280.0,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    token = encode_force_token_bytes(canonical)
    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup._decode_force_token(token, 300.0)


def test_force_numeric_conversion_overflow_is_invalid_confirmation() -> None:
    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup._finite_boot_time(10**10000, "issued_boot")


def test_fallback_deadline_crossed_in_second_audit_starts_no_backend(
    orphan_context, monkeypatch
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    now = [0.0]
    original = classify.build_audit_from_records

    def expire_during_audit(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 1.0
        return result

    monkeypatch.setattr(classify, "build_audit_from_records", expire_during_audit)
    signaler = FakeSignalBackend()
    with pytest.raises(cleanup.CleanupDeadlineExceeded):
        cleanup.execute_cleanup(
            cleanup.plan_cleanup(snapshot),
            store,
            tree,
            signaler,
            clock,
            apply=True,
            deadline=0.5,
            monotonic=lambda: now[0],
        )
    assert signaler.calls == []


def test_fallback_deadline_crossed_after_first_loss_cas_starts_no_backend(
    tmp_path, fake_proc, monkeypatch
) -> None:
    store, tree, _process = ended_owner_context_without_first_loss(tmp_path, fake_proc)
    now = [0.0]
    original = cleanup._cas_process_and_event

    def expire_after_cas(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 1.0
        return result

    monkeypatch.setattr(cleanup, "_cas_process_and_event", expire_after_cas)
    signaler = FakeSignalBackend()
    with pytest.raises(cleanup.CleanupDeadlineExceeded):
        cleanup.execute_cleanup(
            (),
            store,
            tree,
            signaler,
            FakeClock(boot=200.0),
            apply=True,
            deadline=0.5,
            monotonic=lambda: now[0],
        )
    assert signaler.calls == []


def test_fallback_deadline_crossed_during_pidfd_open_closes_without_send(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    now = [0.0]
    signaler = FakeSignalBackend(on_open=lambda _identity: now.__setitem__(0, 1.0))
    with pytest.raises(cleanup.CleanupDeadlineExceeded):
        cleanup.execute_cleanup(
            cleanup.plan_cleanup(snapshot),
            store,
            tree,
            signaler,
            clock,
            apply=True,
            deadline=0.5,
            monotonic=lambda: now[0],
        )
    assert [call[0] for call in signaler.calls] == ["open", "close"]


def test_deadline_crossed_after_identity_observation_starts_no_pidfd(
    orphan_context,
    monkeypatch,
) -> None:
    _store, tree, _clock, snapshot, _process, _lease = orphan_context
    action = cleanup.plan_cleanup(snapshot)[0]
    now = [0.0]
    original = cleanup._observe_identity

    def expire_after_observation(proc_tree, pid):
        result = original(proc_tree, pid)
        now[0] = 1.0
        return result

    monkeypatch.setattr(cleanup, "_observe_identity", expire_after_observation)
    signaler = FakeSignalBackend()

    with pytest.raises(cleanup.CleanupDeadlineExceeded):
        cleanup._prepare_exact_signal(
            action,
            tree,
            signaler,
            deadline=0.5,
            monotonic=lambda: now[0],
        )

    assert signaler.calls == []


def test_deadline_crossed_during_rss_starts_no_second_observation(
    orphan_context,
) -> None:
    _store, _tree, _clock, snapshot, process, _lease = orphan_context
    now = [0.0]

    class ExpiringMetricsProcfs(ExactProcfs):
        def __init__(self):
            super().__init__((process.wrapper,))
            self.observations = 0

        def observe_identity(self, pid):
            self.observations += 1
            return super().observe_identity(pid)

        def rss_kib(self, identity):
            result = super().rss_kib(identity)
            now[0] = 1.0
            return result

    measured = ExpiringMetricsProcfs()
    with pytest.raises(cleanup.CleanupDeadlineExceeded):
        cleanup._fresh_metrics(
            snapshot,
            measured,
            deadline=0.5,
            monotonic=lambda: now[0],
        )

    assert measured.observations == 1


def test_post_signal_deadline_crossing_persists_receipt_without_second_pidfd(
    orphan_context,
    monkeypatch,
) -> None:
    store, tree, clock, _snapshot, process, _lease = orphan_context
    write_proc_entry(
        tree.proc_root,
        654,
        "654 (second) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 6540 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t48 kB\n",
    )
    second = tree.identity(654)
    assert second is not None
    store.save_process(replace(process, members=(process.wrapper, second)))
    snapshot = classify.build_audit(store, tree, clock)
    actions = cleanup.plan_cleanup(snapshot)
    now = [0.0]
    delivered = [False]
    original_observe = tree.observe_identity

    def expire_on_post_signal(pid):
        result = original_observe(pid)
        if delivered[0]:
            now[0] = 1.0
        return result

    monkeypatch.setattr(tree, "observe_identity", expire_on_post_signal)

    class BoundaryClock:
        def wall_iso(self):
            return clock.wall_iso()

        def boottime(self):
            if now[0] >= 0.5:
                raise AssertionError("no clock boundary after deadline")
            return clock.boottime()

    signaler = FakeSignalBackend(
        on_send=lambda _identity, _signum: delivered.__setitem__(0, True)
    )
    report = cleanup.execute_cleanup(
        actions,
        store,
        tree,
        signaler,
        BoundaryClock(),
        apply=True,
        deadline=0.5,
        monotonic=lambda: now[0],
    )

    assert report.attempted == 1
    assert report.after_state_available is False
    assert [call[0] for call in signaler.calls].count("open") == 1
    assert store.load_signal_intent(process.wrapper.stable_key()) is not None


def test_fallback_deadline_crossed_during_term_intent_starts_no_send(
    orphan_context, monkeypatch
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    now = [0.0]
    original = store.transition

    def expire_after_save(*args, **kwargs):
        result = original(*args, **kwargs)
        updated = args[3]
        if (
            isinstance(updated, model.SignalIntent)
            and updated.action == "term"
            and updated.status == "pending"
            and not updated.delivered_keys
        ):
            now[0] = 1.0
        return result

    monkeypatch.setattr(store, "transition", expire_after_save)
    signaler = FakeSignalBackend()
    with pytest.raises(cleanup.CleanupDeadlineExceeded):
        cleanup.execute_cleanup(
            cleanup.plan_cleanup(snapshot),
            store,
            tree,
            signaler,
            clock,
            apply=True,
            deadline=0.5,
            monotonic=lambda: now[0],
        )
    assert [call[0] for call in signaler.calls] == ["open", "close"]
    assert "signal_term_pending" in store.load_processes()[0].owner_reason_codes


def test_each_force_token_selects_only_its_stubborn_classification(
    stubborn_context,
) -> None:
    _store, _tree, clock, snapshot, _process, _lease, first = stubborn_context
    second_identity = replace(first.live_identities[0], pid=654, start_ticks=6540)
    second_process = replace(
        first.process,
        wrapper=second_identity,
        child=None,
        members=(second_identity,),
        host_keys=frozenset({second_identity.stable_key()}),
        term_sent_keys=frozenset({second_identity.stable_key()}),
    )
    second = replace(
        first,
        process=second_process,
        live_identities=(second_identity,),
    )
    combined = replace(snapshot, classifications=(first, second))

    first_actions = cleanup.select_force_actions(
        combined,
        cleanup.issue_force_token(first, clock),
        clock,
    )
    second_actions = cleanup.select_force_actions(
        combined,
        cleanup.issue_force_token(second, clock),
        clock,
    )

    assert {action.process_key for action in first_actions} == {
        first.process.wrapper.stable_key()
    }
    assert {action.process_key for action in second_actions} == {
        second.process.wrapper.stable_key()
    }


def test_force_expiry_after_first_identity_preserves_partial_truth(
    stubborn_context,
) -> None:
    store, tree, clock, _snapshot, process, _lease, _classification = stubborn_context
    write_proc_entry(
        tree.proc_root,
        654,
        "654 (second) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 6540 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t48 kB\n",
    )
    second = tree.identity(654)
    assert second is not None
    process = replace(
        process,
        members=(process.wrapper, second),
        term_sent_keys=frozenset({process.wrapper.stable_key(), second.stable_key()}),
    )
    store.save_process(process)
    snapshot = classify.build_audit(store, tree, clock)
    classification = snapshot.classifications[0]
    assert classification.state == "stubborn"
    token = cleanup.issue_force_token(classification, clock)
    actions = cleanup.plan_cleanup(snapshot, force=True)
    signaler = FakeSignalBackend(
        on_send=lambda _identity, _signum: clock.advance(
            cleanup.FORCE_TOKEN_TTL_SECONDS + 1.0
        )
    )

    report = cleanup.execute_cleanup(
        actions,
        store,
        tree,
        signaler,
        clock,
        apply=True,
        confirm_token=token,
    )

    assert report.partial_force is True
    assert report.attempted == 1
    assert report.survived == 1
    assert report.skipped == 1
    assert [item.reason for item in report.outcomes] == [
        "sigkill_survived",
        "partial_force_authority_expired",
    ]
    assert [call[0] for call in signaler.calls].count("send") == 1
    events = (store.root / "events.jsonl").read_text().splitlines()
    assert sum('"event":"cleanup_force_partial"' in line for line in events) == 1


def test_two_stubborn_tokens_each_signal_only_their_exact_target(
    stubborn_context,
) -> None:
    store, tree, clock, _snapshot, first_process, first_lease, _first = stubborn_context
    write_proc_entry(
        tree.proc_root,
        654,
        "654 (second) S 1 654 654 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 6540 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t48 kB\n",
    )
    second_identity = tree.identity(654)
    assert second_identity is not None
    second_lease = replace(first_lease, session_id="session:second-stubborn")
    second_process = replace(
        first_process,
        record_id=second_identity.stable_key(),
        wrapper=second_identity,
        child=None,
        members=(second_identity,),
        owner_session_id=second_lease.session_id,
        term_sent_keys=frozenset({second_identity.stable_key()}),
    )
    store.save_session(second_lease)
    store.save_process(second_process)
    combined = classify.build_audit(store, tree, clock)
    stubborn = [item for item in combined.classifications if item.state == "stubborn"]
    assert len(stubborn) == 2

    for selected in stubborn:
        token = cleanup.issue_force_token(selected, clock)
        actions = cleanup.select_force_actions(combined, token, clock)
        signaler = FakeSignalBackend()
        report = cleanup.execute_cleanup(
            actions,
            store,
            tree,
            signaler,
            clock,
            apply=True,
            confirm_token=token,
        )
        assert report.attempted == 1
        assert [call[1] for call in signaler.calls if call[0] == "open"] == [
            selected.live_identities[0]
        ]


def test_valid_force_confirmation_sends_only_sigkill(stubborn_context) -> None:
    store, tree, clock, snapshot, process, _lease, classification = stubborn_context
    action = cleanup.plan_cleanup(snapshot, force=True)[0]
    token = cleanup.issue_force_token(classification, clock)
    signaler = FakeSignalBackend()

    report = cleanup.execute_cleanup(
        (action,),
        store,
        tree,
        signaler,
        clock,
        apply=True,
        confirm_token=token,
    )

    assert report.attempted == 1
    assert report.survived == 1
    assert [call for call in signaler.calls if call[0] == "send"] == [
        ("send", process.wrapper.pid + 1000, signal.SIGKILL)
    ]
    assert store.load_raw_processes()[0] == process
    receipt = store.load_force_intent(process.wrapper.stable_key())
    assert receipt is not None
    assert receipt.status == "delivered"
    assert (
        cleanup.plan_cleanup(classify.build_audit(store, tree, clock), force=True) == ()
    )


def test_force_expiry_during_pidfd_preparation_prevents_sigkill(
    stubborn_context,
) -> None:
    store, tree, clock, snapshot, _process, _lease, classification = stubborn_context
    actions = cleanup.plan_cleanup(snapshot, force=True)
    token = cleanup.issue_force_token(classification, clock)
    clock.advance(cleanup.FORCE_TOKEN_TTL_SECONDS - 0.1)
    signaler = FakeSignalBackend(on_open=lambda _identity: clock.advance(1.0))

    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            actions,
            store,
            tree,
            signaler,
            clock,
            apply=True,
            confirm_token=token,
        )

    assert [call for call in signaler.calls if call[0] == "send"] == []
    assert [call for call in signaler.calls if call[0] == "close"]


@pytest.mark.parametrize("token_kind", ["missing", "expired", "bad_digest"])
def test_invalid_force_confirmation_has_zero_opens_signals_or_mutations(
    stubborn_context,
    token_kind: str,
) -> None:
    store, tree, clock, snapshot, _process, _lease, classification = stubborn_context
    action = cleanup.plan_cleanup(snapshot, force=True)[0]
    token = cleanup.issue_force_token(classification, clock)
    if token_kind == "missing":
        token = None
    elif token_kind == "expired":
        clock.advance(cleanup.FORCE_TOKEN_TTL_SECONDS + 0.001)
    else:
        token = token[:-1] + ("0" if token[-1] != "0" else "1")
    signaler = FakeSignalBackend()
    before = state_tree(store.root)

    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            (action,),
            store,
            tree,
            signaler,
            clock,
            apply=True,
            confirm_token=token,
        )

    assert signaler.calls == []
    assert state_tree(store.root) == before


def test_force_token_is_rejected_when_current_classification_changed(
    stubborn_context,
) -> None:
    store, tree, clock, snapshot, _process, lease, classification = stubborn_context
    action = cleanup.plan_cleanup(snapshot, force=True)[0]
    token = cleanup.issue_force_token(classification, clock)
    store.save_session(replace(lease, state="active", ended=None))
    before = state_tree(store.root)
    signaler = FakeSignalBackend()

    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            (action,),
            store,
            tree,
            signaler,
            clock,
            apply=True,
            confirm_token=token,
        )

    assert signaler.calls == []
    assert state_tree(store.root) == before


def test_force_token_cannot_replay_across_new_term_delivery_time(
    stubborn_context,
) -> None:
    store, tree, clock, snapshot, process, _lease, classification = stubborn_context
    actions = cleanup.plan_cleanup(snapshot, force=True)
    token = cleanup.issue_force_token(classification, clock)
    store.save_process(replace(process, term_sent_boot=290.0))
    refreshed = classify.build_audit(store, tree, clock).classifications[0]
    assert refreshed.state == "stubborn"
    assert refreshed.live_identities == classification.live_identities
    assert refreshed.reason_codes == classification.reason_codes
    signaler = FakeSignalBackend()
    before = state_tree(store.root)

    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            actions,
            store,
            tree,
            signaler,
            clock,
            apply=True,
            confirm_token=token,
        )

    assert signaler.calls == []
    assert state_tree(store.root) == before


def test_dry_run_does_not_parse_or_consume_force_token(stubborn_context) -> None:
    store, tree, clock, snapshot, _process, _lease, _classification = stubborn_context
    signaler = FakeSignalBackend()
    before = state_tree(store.root)

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot, force=True),
        store,
        tree,
        signaler,
        clock,
        apply=False,
        confirm_token="not-a-token",
    )

    assert report.attempted == 0
    assert signaler.calls == []
    assert state_tree(store.root) == before


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", 2),
        ("expires_boot", 600.001),
        ("issued_boot", 301.0),
        ("boot_id", "different-boot"),
        ("reason_codes", ["different_reason"]),
        ("term_sent_boot", 279.0),
    ],
)
def test_well_digested_but_malformed_or_mismatched_force_payload_is_rejected(
    stubborn_context,
    field: str,
    replacement: object,
) -> None:
    store, tree, clock, snapshot, _process, _lease, classification = stubborn_context
    action = cleanup.plan_cleanup(snapshot, force=True)[0]
    payload = decode_force_token(cleanup.issue_force_token(classification, clock))
    payload[field] = replacement
    signaler = FakeSignalBackend()
    before = state_tree(store.root)

    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            (action,),
            store,
            tree,
            signaler,
            clock,
            apply=True,
            confirm_token=encode_force_token(payload),
        )

    assert signaler.calls == []
    assert state_tree(store.root) == before


def test_force_pid_reuse_after_token_issue_is_rejected_before_pidfd_open(
    stubborn_context,
) -> None:
    store, tree, clock, snapshot, process, _lease, classification = stubborn_context
    action = cleanup.plan_cleanup(snapshot, force=True)[0]
    token = cleanup.issue_force_token(classification, clock)
    change_start_ticks(tree, process.wrapper)
    signaler = FakeSignalBackend()
    before = state_tree(store.root)

    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            (action,),
            store,
            tree,
            signaler,
            clock,
            apply=True,
            confirm_token=token,
        )

    assert signaler.calls == []
    assert state_tree(store.root) == before


def test_force_pid_reuse_after_pidfd_open_closes_once_and_sends_no_signal(
    stubborn_context,
) -> None:
    store, tree, clock, snapshot, process, _lease, classification = stubborn_context
    action = cleanup.plan_cleanup(snapshot, force=True)[0]
    token = cleanup.issue_force_token(classification, clock)
    signaler = FakeSignalBackend(
        on_open=lambda identity: change_start_ticks(tree, identity)
    )

    report = cleanup.execute_cleanup(
        (action,),
        store,
        tree,
        signaler,
        clock,
        apply=True,
        confirm_token=token,
    )

    assert report.attempted == 0
    assert report.skipped == 1
    assert report.outcomes[0].reason == "identity_changed_after_pidfd"
    assert signaler.calls == [
        ("open", process.wrapper),
        ("close", process.wrapper.pid + 1000),
    ]
    assert not (store.root / "events.jsonl").exists()


def test_post_signal_unavailable_is_conservative_and_records_no_term_time(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context

    class BecomesUnavailable:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.proc_root = wrapped.proc_root
            self.unavailable = False

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def observe_identity(self, pid):
            if self.unavailable:
                return procfs.IdentityObservation("unavailable", None)
            return self.wrapped.observe_identity(pid)

    changing_procfs = BecomesUnavailable(tree)
    signaler = FakeSignalBackend(
        on_send=lambda _identity, _signum: setattr(changing_procfs, "unavailable", True)
    )

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        changing_procfs,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 1
    assert report.skipped == 1
    assert report.outcomes[0].reason == "identity_unavailable_after_signal"
    persisted = store.load_processes()[0]
    assert persisted.term_sent_boot is None
    assert "signal_term_pending" in persisted.owner_reason_codes
    event = cleanup_event(store, "cleanup_signal_indeterminate")
    assert event["event"] == "cleanup_signal_indeterminate"
    assert event["state"] == "unknown"


def test_signal_failure_closes_pidfd_once_without_lifecycle_mutation(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context

    class SendFailure(FakeSignalBackend):
        def send(self, pidfd: int, signum: int) -> None:
            super().send(pidfd, signum)
            raise PermissionError("denied")

    signaler = SendFailure()
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 1
    assert report.skipped == 1
    assert report.outcomes[0].reason == "signal_failed"
    assert signaler.calls == [
        ("open", process.wrapper),
        ("send", process.wrapper.pid + 1000, signal.SIGTERM),
        ("close", process.wrapper.pid + 1000),
    ]
    assert store.load_raw_processes()[0] == process
    intent = store.load_signal_intent(process.wrapper.stable_key())
    assert intent is not None
    assert intent.status == "conflict"
    assert cleanup_event(store, "cleanup_signal_indeterminate")["state"] == "unknown"


def test_force_partial_subtree_survival_is_not_falsely_evented_as_gone(
    stubborn_context,
) -> None:
    store, tree, clock, _snapshot, process, _lease, _classification = stubborn_context
    write_proc_entry(
        tree.proc_root,
        322,
        "322 (child) S 321 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 3220 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t32 kB\n",
    )
    child = tree.identity(322)
    assert child is not None
    expanded = replace(
        process,
        child=child,
        members=(process.wrapper, child),
        term_sent_keys=frozenset({process.wrapper.stable_key(), child.stable_key()}),
    )
    store.save_process(expanded)
    snapshot = classify.build_audit(store, tree, clock)
    classification = snapshot.classifications[0]
    assert classification.state == "stubborn"
    token = cleanup.issue_force_token(classification, clock)
    actions = cleanup.plan_cleanup(snapshot, force=True)
    signaler = FakeSignalBackend(
        on_send=lambda identity, _signum: (
            remove_process(tree, identity.pid) if identity.pid == child.pid else None
        )
    )

    report = cleanup.execute_cleanup(
        actions,
        store,
        tree,
        signaler,
        clock,
        apply=True,
        confirm_token=token,
    )

    assert report.attempted == 2
    assert report.terminated == 1
    assert report.survived == 1
    event = cleanup_event(store, "cleanup_force_sent")
    assert event["event"] == "cleanup_force_sent"
    assert event["state"] == "stubborn"


def test_force_batch_is_fully_validated_before_any_pidfd_open(stubborn_context) -> None:
    store, tree, clock, _snapshot, process, _lease, _classification = stubborn_context
    write_proc_entry(
        tree.proc_root,
        322,
        "322 (child) S 321 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 3220 0 0\n",
        tree.proc_root / "node",
    )
    child = tree.identity(322)
    assert child is not None
    store.save_process(
        replace(
            process,
            child=child,
            members=(process.wrapper, child),
            term_sent_keys=frozenset(
                {process.wrapper.stable_key(), child.stable_key()}
            ),
        )
    )
    snapshot = classify.build_audit(store, tree, clock)
    classification = snapshot.classifications[0]
    actions = list(cleanup.plan_cleanup(snapshot, force=True))
    actions[-1] = replace(actions[-1], reason_codes=("forged_reason",))
    signaler = FakeSignalBackend()
    before = state_tree(store.root)

    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            tuple(actions),
            store,
            tree,
            signaler,
            clock,
            apply=True,
            confirm_token=cleanup.issue_force_token(classification, clock),
        )

    assert signaler.calls == []
    assert state_tree(store.root) == before


def test_pidfd_close_error_after_successful_term_does_not_skip_survival_record(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context

    class CloseFailure(FakeSignalBackend):
        def close(self, pidfd: int) -> None:
            super().close(pidfd)
            raise OSError("close failed")

    signaler = CloseFailure()
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 1
    assert report.survived == 1
    assert report.outcomes[0].reason == "sigterm_survived_pidfd_close_failed"
    assert signaler.calls.count(("close", process.wrapper.pid + 1000)) == 1
    assert store.load_processes()[0].term_sent_boot == 300.0


def test_identity_only_procfs_contract_still_revalidates_exact_action(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context

    class IdentityOnlyProcfs:
        proc_root = tree.proc_root

        def _boot_id(self):
            return tree._boot_id()

        def identity(self, pid):
            return tree.identity(pid)

        def rss_kib(self, identity):
            return tree.rss_kib(identity)

    signaler = FakeSignalBackend()
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        IdentityOnlyProcfs(),
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 1
    assert report.survived == 1
    assert [call for call in signaler.calls if call[0] == "send"] == [
        ("send", process.wrapper.pid + 1000, signal.SIGTERM)
    ]


def test_force_expiry_during_locked_metrics_recheck_prevents_pidfd_open(
    stubborn_context,
) -> None:
    store, tree, clock, snapshot, _process, _lease, classification = stubborn_context
    action = cleanup.plan_cleanup(snapshot, force=True)[0]
    token = cleanup.issue_force_token(classification, clock)

    class AdvanceDuringRss:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.proc_root = wrapped.proc_root
            self.advanced = False

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def rss_kib(self, identity):
            result = self.wrapped.rss_kib(identity)
            if not self.advanced:
                self.advanced = True
                clock.advance(cleanup.FORCE_TOKEN_TTL_SECONDS + 0.001)
            return result

    signaler = FakeSignalBackend()
    before = state_tree(store.root)

    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            (action,),
            store,
            AdvanceDuringRss(tree),
            signaler,
            clock,
            apply=True,
            confirm_token=token,
        )

    assert signaler.calls == []
    assert state_tree(store.root) == before


def test_disappearance_after_pidfd_open_closes_once_without_signal(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context
    signaler = FakeSignalBackend(
        on_open=lambda identity: remove_process(tree, identity.pid)
    )

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 0
    assert report.skipped == 1
    assert report.outcomes[0].reason == "identity_gone_after_pidfd"
    assert signaler.calls == [
        ("open", process.wrapper),
        ("close", process.wrapper.pid + 1000),
    ]
    assert not (store.root / "events.jsonl").exists()


def test_term_sent_time_is_observed_after_signal_not_from_reaudit(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    signaler = FakeSignalBackend(on_send=lambda _identity, _signum: clock.advance(5.0))

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.survived == 1
    assert store.load_processes()[0].term_sent_boot == 305.0


@pytest.mark.parametrize(
    ("invalid_observation", "expected_reason"),
    [
        (OSError("clock unavailable"), "post_signal_time_unavailable"),
        (float("nan"), "post_signal_time_unavailable"),
        (float("inf"), "post_signal_time_unavailable"),
        (-1.0, "post_signal_time_unavailable"),
        (299.999, "post_signal_time_regressed"),
    ],
    ids=("oserror", "nan", "infinity", "negative", "regression"),
)
def test_invalid_post_term_clock_never_advances_lifecycle_or_force_eligibility(
    orphan_context,
    invalid_observation,
    expected_reason,
) -> None:
    store, tree, _clock, snapshot, process, _lease = orphan_context

    class DelayedInvalidClock:
        def __init__(self) -> None:
            self.current = 300.0
            self.invalid_pending = False

        def wall_iso(self) -> str:
            return "2026-08-29T00:00:00+00:00"

        def boottime(self) -> float:
            if self.invalid_pending:
                self.invalid_pending = False
                if isinstance(invalid_observation, BaseException):
                    raise invalid_observation
                return invalid_observation
            return self.current

        def signal_completed_after_delay(self, _identity, _signum) -> None:
            self.current += 20.0
            self.invalid_pending = True

    clock = DelayedInvalidClock()
    signaler = FakeSignalBackend(on_send=clock.signal_completed_after_delay)

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.attempted == 1
    assert report.survived == 1
    assert report.outcomes[0].reason == expected_reason
    persisted = store.load_processes()[0]
    assert persisted.term_sent_boot is None
    assert "signal_term_pending" in persisted.owner_reason_codes
    event = cleanup_event(store, "cleanup_signal_indeterminate")
    assert event["event"] == "cleanup_signal_indeterminate"
    assert expected_reason in event["reason_codes"]
    fresh = classify.build_audit(store, tree, clock)
    assert fresh.classifications[0].state == "unknown"
    assert cleanup.plan_cleanup(fresh, force=True) == ()


def test_new_member_requires_complete_current_set_term_before_force(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context
    first_signaler = FakeSignalBackend()
    cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        first_signaler,
        clock,
        apply=True,
    )
    first_term = store.load_processes()[0]
    assert first_term.term_sent_keys == frozenset({process.wrapper.stable_key()})
    clock.advance(10.0)
    prior_stubborn = classify.build_audit(store, tree, clock)
    prior_classification = prior_stubborn.classifications[0]
    assert prior_classification.state == "stubborn"
    stale_actions = cleanup.plan_cleanup(prior_stubborn, force=True)
    stale_token = cleanup.issue_force_token(prior_classification, clock)

    write_proc_entry(
        tree.proc_root,
        322,
        "322 (child) S 321 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 3220 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t32 kB\n",
    )
    child = tree.identity(322)
    assert child is not None
    store.save_process(
        replace(
            first_term,
            child=child,
            members=(process.wrapper, child),
        )
    )
    changed = classify.build_audit(store, tree, clock)
    changed_classification = changed.classifications[0]
    assert changed_classification.state == "orphan"
    assert changed_classification.eligible_term is True
    assert cleanup.plan_cleanup(changed, force=True) == ()
    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.issue_force_token(changed_classification, clock)
    rejected_signaler = FakeSignalBackend()
    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            stale_actions,
            store,
            tree,
            rejected_signaler,
            clock,
            apply=True,
            confirm_token=stale_token,
        )
    assert rejected_signaler.calls == []

    term_signaler = FakeSignalBackend()
    term_report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(changed),
        store,
        tree,
        term_signaler,
        clock,
        apply=True,
    )
    assert term_report.survived == 2, term_report
    current_keys = frozenset({process.wrapper.stable_key(), child.stable_key()})
    refreshed = store.load_processes()[0]
    assert refreshed.term_sent_boot == 310.0
    assert refreshed.term_sent_keys == current_keys

    clock.advance(10.0)
    stubborn = classify.build_audit(store, tree, clock)
    stubborn_classification = stubborn.classifications[0]
    assert stubborn_classification.state == "stubborn"
    force_actions = cleanup.plan_cleanup(stubborn, force=True)
    assert {action.identity.stable_key() for action in force_actions} == current_keys
    token = cleanup.issue_force_token(stubborn_classification, clock)
    force_signaler = FakeSignalBackend()
    force_report = cleanup.execute_cleanup(
        force_actions,
        store,
        tree,
        force_signaler,
        clock,
        apply=True,
        confirm_token=token,
    )
    assert force_report.survived == 2
    assert [call[2] for call in force_signaler.calls if call[0] == "send"] == [
        signal.SIGKILL,
        signal.SIGKILL,
    ]


def test_root_rebind_before_signal_skips_without_cross_ledger_mutation(
    orphan_context, tmp_path, monkeypatch
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    displaced = tmp_path / "displaced-state"
    rebound = False

    def rebind_after_open(_identity):
        nonlocal rebound
        if not rebound:
            store.root.rename(displaced)
            store.root.mkdir(mode=0o700)
            rebound = True

    signaler = FakeSignalBackend(on_open=rebind_after_open)
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert [call for call in signaler.calls if call[0] == "send"] == []
    assert report.outcomes[0].reason == "state_authority_changed"
    assert list(store.root.iterdir()) == []
    assert not (store.root / "events.jsonl").exists()
    assert state.StateStore(displaced).load_processes()


def test_partial_term_delivery_does_not_make_unsignaled_identity_stubborn(
    orphan_context,
) -> None:
    store, tree, clock, _snapshot, process, _lease = orphan_context
    write_proc_entry(
        tree.proc_root,
        322,
        "322 (child) S 321 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 3220 0 0\n",
        tree.proc_root / "node",
    )
    child = tree.identity(322)
    assert child is not None
    expanded = replace(process, child=child, members=(process.wrapper, child))
    store.save_process(expanded)
    snapshot = classify.build_audit(store, tree, clock)

    class PartiallyUnavailable(FakeSignalBackend):
        def open(self, identity: model.ProcessIdentity) -> int:
            if identity == child:
                self.calls.append(("open", identity))
                raise cleanup.PidfdUnavailable("child unavailable")
            return super().open(identity)

    signaler = PartiallyUnavailable()
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert report.survived == 1
    assert report.skipped == 1
    assert store.load_processes()[0].term_sent_boot is None
    event = cleanup_event(store, "cleanup_signal_indeterminate")
    assert event["event"] == "cleanup_signal_indeterminate"
    assert event["state"] == "unknown"
    assert "partial_signal_delivery" in event["reason_codes"]
    assert "pidfd_unavailable" in event["reason_codes"]
    assert "sigterm_survived" in event["reason_codes"]
    assert "identity_unavailable_after_signal" not in event["reason_codes"]


@pytest.mark.parametrize(
    "failure_reason",
    ["signal_failed", "identity_changed_after_signal"],
)
def test_partial_term_event_reports_the_actual_failed_identity_outcome(
    orphan_context,
    failure_reason,
) -> None:
    store, tree, clock, _snapshot, process, _lease = orphan_context
    write_proc_entry(
        tree.proc_root,
        322,
        "322 (child) S 321 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 3220 0 0\n",
        tree.proc_root / "node",
    )
    child = tree.identity(322)
    assert child is not None
    expanded = replace(process, child=child, members=(process.wrapper, child))
    store.save_process(expanded)
    snapshot = classify.build_audit(store, tree, clock)

    class PartialFailure(FakeSignalBackend):
        def send(self, pidfd: int, signum: int) -> None:
            identity = self._identities_by_pidfd[pidfd]
            super().send(pidfd, signum)
            if identity != child:
                return
            if failure_reason == "signal_failed":
                raise PermissionError("denied")
            change_start_ticks(tree, identity)

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        PartialFailure(),
        clock,
        apply=True,
    )

    assert report.survived == 1
    assert report.skipped == 1
    assert failure_reason in {outcome.reason for outcome in report.outcomes}
    persisted = store.load_processes()[0]
    assert persisted.term_sent_boot is None
    assert "signal_term_pending" in persisted.owner_reason_codes
    assert persisted.term_sent_keys == frozenset(
        identity.stable_key()
        for identity in snapshot.classifications[0].live_identities
    )
    event = cleanup_event(store, "cleanup_signal_indeterminate")
    assert event["event"] == "cleanup_signal_indeterminate"
    assert "partial_signal_delivery" in event["reason_codes"]
    assert failure_reason in event["reason_codes"]
    assert "sigterm_survived" in event["reason_codes"]
    assert "identity_unavailable_after_signal" not in event["reason_codes"]


def test_cli_apply_never_recaptures_a_replacement_initial_root(
    orphan_context, tmp_path, monkeypatch, capsys
) -> None:
    store, tree, clock, _snapshot, _process, _lease = orphan_context
    displaced = tmp_path / "initial-audited-root"
    original_capture = cli.capture_authorized_audit
    replaced = False

    def replace_before_authorized_capture(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            store.root.rename(displaced)
            shutil.copytree(displaced, store.root)
            replaced = True
        return original_capture(*args, **kwargs)

    signaler = FakeSignalBackend()
    monkeypatch.setattr(cli, "_runtime", lambda: (store, tree, clock))
    monkeypatch.setattr(
        cli,
        "capture_authorized_audit",
        replace_before_authorized_capture,
    )
    monkeypatch.setattr(cli, "PidfdSignalBackend", lambda: signaler)

    result = cli._cleanup_command(True, False, None)

    assert result != 0
    assert [call for call in signaler.calls if call[0] == "send"] == []
    assert state_tree(store.root) == state_tree(displaced)
    captured = capsys.readouterr()
    assert "state unavailable" in captured.err


def test_first_loss_cas_rejects_a_competing_full_session_set_revision(
    tmp_path, fake_proc, monkeypatch
) -> None:
    store, tree, process = ended_owner_context_without_first_loss(tmp_path, fake_proc)
    owner = store.load_sessions()[0]
    competitor = replace(
        owner,
        session_id="session:competing",
        state="active",
        ended=None,
    )
    original = cleanup._cas_process_and_event
    inserted = False

    def insert_competitor_then_cas(*args, **kwargs):
        nonlocal inserted
        if not inserted:
            store.save_session(competitor)
            inserted = True
        return original(*args, **kwargs)

    monkeypatch.setattr(cleanup, "_cas_process_and_event", insert_competitor_then_cas)

    report = cleanup.execute_cleanup(
        (),
        store,
        tree,
        FakeSignalBackend(),
        FakeClock(boot=200.0),
        apply=True,
    )

    assert report.authority_lost is True
    assert store.load_process(process.wrapper.stable_key()) == process
    assert not (store.root / "events.jsonl").exists()


def test_delivered_term_receipt_projects_over_the_exact_live_subset(
    orphan_context,
) -> None:
    store, tree, clock, _snapshot, process, _lease = orphan_context
    missing = replace(process.wrapper, pid=654, start_ticks=6540)
    expanded = replace(process, members=(process.wrapper, missing))
    store.save_process(expanded)
    store.save_signal_intent(
        model.SignalIntent(
            1,
            process.wrapper.stable_key(),
            process.owner_generation,
            (process.wrapper.stable_key(),),
            "term",
            "delivered",
            (process.wrapper.stable_key(),),
            290.0,
        )
    )

    snapshot = classify.build_audit(store, tree, clock)

    assert snapshot.classifications[0].state == "stubborn"
    assert snapshot.classifications[0].eligible_term is False
    assert cleanup.plan_cleanup(snapshot) == ()


@pytest.mark.parametrize("status", ["pending", "conflict", "delivered"])
def test_force_receipt_terminal_and_ambiguous_states_block_replanning(
    stubborn_context, status
) -> None:
    store, tree, clock, _snapshot, process, _lease, classification = stubborn_context
    keys = tuple(
        sorted(identity.stable_key() for identity in classification.live_identities)
    )
    store.save_signal_intent(
        model.SignalIntent(
            1,
            process.wrapper.stable_key(),
            process.owner_generation,
            keys,
            "force",
            status,
            keys if status == "delivered" else (),
        )
    )

    snapshot = classify.build_audit(store, tree, clock)

    assert cleanup.plan_cleanup(snapshot, force=True) == ()


def test_force_rejects_a_caller_supplied_subset_before_any_pidfd(
    stubborn_context,
) -> None:
    store, tree, clock, _snapshot, process, _lease, _classification = stubborn_context
    write_proc_entry(
        tree.proc_root,
        654,
        "654 (second) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 6540 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t48 kB\n",
    )
    second = tree.identity(654)
    assert second is not None
    expanded = replace(
        process,
        members=(process.wrapper, second),
        term_sent_keys=frozenset({process.wrapper.stable_key(), second.stable_key()}),
    )
    store.save_process(expanded)
    snapshot = classify.build_audit(store, tree, clock)
    classification = snapshot.classifications[0]
    token = cleanup.issue_force_token(classification, clock)
    actions = cleanup.plan_cleanup(snapshot, force=True)
    signaler = FakeSignalBackend()

    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            actions[:1],
            store,
            tree,
            signaler,
            clock,
            apply=True,
            confirm_token=token,
        )

    assert signaler.calls == []


def test_force_ttl_is_rechecked_after_pending_intent_persistence(
    stubborn_context, monkeypatch
) -> None:
    store, tree, clock, snapshot, _process, _lease, classification = stubborn_context
    token = cleanup.issue_force_token(classification, clock)
    original = store.transition

    def expire_after_pending(*args, **kwargs):
        revision = original(*args, **kwargs)
        updated = args[3]
        if (
            isinstance(updated, model.SignalIntent)
            and updated.action == "force"
            and updated.status == "pending"
            and not updated.delivered_keys
        ):
            clock.advance(cleanup.FORCE_TOKEN_TTL_SECONDS + 1.0)
        return revision

    monkeypatch.setattr(store, "transition", expire_after_pending)
    signaler = FakeSignalBackend()

    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            cleanup.plan_cleanup(snapshot, force=True),
            store,
            tree,
            signaler,
            clock,
            apply=True,
            confirm_token=token,
        )

    assert [call for call in signaler.calls if call[0] == "send"] == []


def test_malformed_transition_journal_blocks_apply_before_backend_use(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    journal = store.root / "event-journal"
    journal.mkdir(mode=0o700)
    malformed = journal / (("a" * 64) + ".json")
    malformed.write_bytes(b'{"schema_version":1,"phase":"prepared"}\n')
    malformed.chmod(0o600)
    signaler = FakeSignalBackend()

    with pytest.raises((state.StateCorruption, state.UnsafeStatePath)):
        cleanup.execute_cleanup(
            cleanup.plan_cleanup(snapshot),
            store,
            tree,
            signaler,
            clock,
            apply=True,
        )

    assert signaler.calls == []


def test_deadline_stops_classification_before_the_next_observation(
    orphan_context,
) -> None:
    store, tree, clock, _snapshot, process, lease = orphan_context
    now = [0.0]

    class ExpiringProcfs(ExactProcfs):
        def __init__(self):
            super().__init__((process.wrapper,))
            self.observations = 0

        def observe_identity(self, pid):
            if now[0] >= 0.5:
                raise AssertionError("observation started after deadline")
            self.observations += 1
            result = super().observe_identity(pid)
            now[0] = 1.0
            return result

    expiring = ExpiringProcfs()

    with pytest.raises(cleanup.CleanupDeadlineExceeded):
        classify.build_audit_from_records(
            (process,),
            (lease,),
            expiring,
            clock,
            deadline=0.5,
            monotonic=lambda: now[0],
        )

    assert expiring.observations == 1


def test_force_deadline_after_first_delivery_is_partial_and_accounts_for_all(
    stubborn_context,
) -> None:
    store, tree, clock, _snapshot, process, _lease, _classification = stubborn_context
    write_proc_entry(
        tree.proc_root,
        654,
        "654 (second) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 6540 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t48 kB\n",
    )
    second = tree.identity(654)
    assert second is not None
    expanded = replace(
        process,
        members=(process.wrapper, second),
        term_sent_keys=frozenset({process.wrapper.stable_key(), second.stable_key()}),
    )
    store.save_process(expanded)
    snapshot = classify.build_audit(store, tree, clock)
    classification = snapshot.classifications[0]
    token = cleanup.issue_force_token(classification, clock)
    actions = cleanup.plan_cleanup(snapshot, force=True)
    now = [0.0]
    signaler = FakeSignalBackend(
        on_send=lambda _identity, _signum: now.__setitem__(0, 1.0)
    )

    report = cleanup.execute_cleanup(
        actions,
        store,
        tree,
        signaler,
        clock,
        apply=True,
        confirm_token=token,
        deadline=0.5,
        monotonic=lambda: now[0],
    )

    assert report.partial_force is True
    assert report.attempted == 1
    assert len(report.outcomes) == len(actions)
    assert report.skipped == len(actions) - 1
    assert [call[0] for call in signaler.calls].count("send") == 1


def _expand_stubborn_process(store, tree, clock, process):
    write_proc_entry(
        tree.proc_root,
        654,
        "654 (second) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 6540 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t48 kB\n",
    )
    second = tree.identity(654)
    assert second is not None
    expanded = replace(
        process,
        members=(process.wrapper, second),
        term_sent_keys=frozenset({process.wrapper.stable_key(), second.stable_key()}),
    )
    store.save_process(expanded)
    snapshot = classify.build_audit(store, tree, clock)
    assert snapshot.classifications[0].state == "stubborn"
    return snapshot, second


def test_final_lexical_binding_check_prevents_signal_at_effect_seam(
    orphan_context, tmp_path, monkeypatch
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    displaced = tmp_path / "final-effect-root"
    original_transition = store.transition
    rebound = False

    def rebind_after_all_effect_preconditions(*args, **kwargs):
        nonlocal rebound
        if kwargs.get("effect") is not None:
            original_before = kwargs["before_effect"]

            def before_then_rebind():
                nonlocal rebound
                original_before()
                if not rebound:
                    store.root.rename(displaced)
                    store.root.mkdir(mode=0o700)
                    rebound = True

            kwargs["before_effect"] = before_then_rebind
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(store, "transition", rebind_after_all_effect_preconditions)
    signaler = FakeSignalBackend()

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert rebound is True
    assert [call for call in signaler.calls if call[0] == "send"] == []
    assert report.authority_lost is True
    assert report.after_state_available is False
    assert list(store.root.iterdir()) == []


def test_successful_singleton_dispatch_is_not_collapsed_to_signal_failure(
    orphan_context, monkeypatch
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context
    original_write = store._write_transition_record_locked
    failed = False

    def fail_delivered_receipt(kind, key, updated, **kwargs):
        nonlocal failed
        if (
            not failed
            and isinstance(updated, model.SignalIntent)
            and updated.delivered_keys
        ):
            failed = True
            raise OSError("POST-EFFECT-RECEIPT-FAILURE")
        return original_write(kind, key, updated, **kwargs)

    monkeypatch.setattr(
        store, "_write_transition_record_locked", fail_delivered_receipt
    )
    signaler = FakeSignalBackend()

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert [call[0] for call in signaler.calls].count("send") == 1
    assert report.attempted == 1
    assert report.outcomes[0].status == "survived"
    assert report.outcomes[0].reason == "signal_delivery_indeterminate"
    assert report.authority_lost is True
    assert report.after_state_available is False
    intent = store.load_signal_intent(process.wrapper.stable_key())
    assert intent is not None
    no_replay_keys = set(intent.delivered_keys) | set(
        getattr(intent, "dispatch_keys", ())
    )
    assert process.wrapper.stable_key() in no_replay_keys


def test_post_effect_deadline_returns_indeterminate_singleton_report(
    orphan_context, monkeypatch
) -> None:
    store, tree, clock, snapshot, process, _lease = orphan_context
    signaler = FakeSignalBackend()
    original_mark = store._mark_transition_committed_locked
    now = [0.0]
    expired = False

    def expire_before_post_effect_commit(event_id, **kwargs):
        nonlocal expired
        if not expired and any(call[0] == "send" for call in signaler.calls):
            now[0] = 1.0
            expired = True
        return original_mark(event_id, **kwargs)

    monkeypatch.setattr(
        store,
        "_mark_transition_committed_locked",
        expire_before_post_effect_commit,
    )

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
        deadline=0.5,
        monotonic=lambda: now[0],
    )

    assert expired is True
    assert [call[0] for call in signaler.calls].count("send") == 1
    assert report.attempted == 1
    assert report.outcomes[0].status == "survived"
    assert report.outcomes[0].reason == "signal_delivery_indeterminate"
    assert report.partial_force is False
    assert report.authority_lost is True
    assert report.after_state_available is False
    intent = store.load_signal_intent(process.wrapper.stable_key())
    assert intent is not None
    assert process.wrapper.stable_key() in (
        set(intent.delivered_keys) | set(intent.dispatch_keys)
    )


def test_successful_multi_dispatch_failure_accounts_partial_force_truth(
    stubborn_context, monkeypatch
) -> None:
    store, tree, clock, _snapshot, process, _lease, _classification = stubborn_context
    snapshot, _second = _expand_stubborn_process(store, tree, clock, process)
    classification = snapshot.classifications[0]
    token = cleanup.issue_force_token(classification, clock)
    actions = cleanup.plan_cleanup(snapshot, force=True)
    original_write = store._write_transition_record_locked
    failed = False

    def fail_first_delivered_receipt(kind, key, updated, **kwargs):
        nonlocal failed
        if (
            not failed
            and isinstance(updated, model.SignalIntent)
            and updated.action == "force"
            and updated.delivered_keys
        ):
            failed = True
            raise OSError("POST-EFFECT-FORCE-RECEIPT-FAILURE")
        return original_write(kind, key, updated, **kwargs)

    monkeypatch.setattr(
        store,
        "_write_transition_record_locked",
        fail_first_delivered_receipt,
    )
    signaler = FakeSignalBackend()

    report = cleanup.execute_cleanup(
        actions,
        store,
        tree,
        signaler,
        clock,
        apply=True,
        confirm_token=token,
    )

    assert [call[0] for call in signaler.calls].count("send") == 1
    assert report.attempted == 1
    assert len(report.outcomes) == len(actions)
    assert report.outcomes[0].status == "survived"
    assert report.outcomes[0].reason == "signal_delivery_indeterminate"
    assert report.outcomes[1].status == "skipped"
    assert report.partial_force is True
    assert report.authority_lost is True
    assert report.after_state_available is False


def test_deadline_during_second_delivery_journal_returns_accounted_report(
    stubborn_context, monkeypatch
) -> None:
    store, tree, clock, _snapshot, process, _lease, _classification = stubborn_context
    snapshot, _second = _expand_stubborn_process(store, tree, clock, process)
    classification = snapshot.classifications[0]
    token = cleanup.issue_force_token(classification, clock)
    actions = cleanup.plan_cleanup(snapshot, force=True)
    now = [0.0]
    signaler = FakeSignalBackend()
    original_build = store._build_transition_journal
    expired = False

    def expire_after_second_delivery_construction(*args, **kwargs):
        nonlocal expired
        result = original_build(*args, **kwargs)
        updated = args[3]
        dispatch_keys = getattr(updated, "dispatch_keys", updated.delivered_keys)
        if (
            not expired
            and isinstance(updated, model.SignalIntent)
            and updated.action == "force"
            and len(dispatch_keys) == 2
            and [call[0] for call in signaler.calls].count("send") == 1
        ):
            now[0] = 1.0
            expired = True
        return result

    monkeypatch.setattr(
        store,
        "_build_transition_journal",
        expire_after_second_delivery_construction,
    )

    report = cleanup.execute_cleanup(
        actions,
        store,
        tree,
        signaler,
        clock,
        apply=True,
        confirm_token=token,
        deadline=0.5,
        monotonic=lambda: now[0],
    )

    assert expired is True
    assert [call[0] for call in signaler.calls].count("send") == 1
    assert report.attempted == 1
    assert len(report.outcomes) == len(actions)
    assert report.partial_force is True
    assert report.after_state_available is False


def test_unrelated_corrupt_receipt_blocks_signal_before_backend_use(
    orphan_context,
) -> None:
    store, tree, clock, snapshot, _process, _lease = orphan_context
    receipts = store.root / "event-receipts"
    receipts.mkdir(mode=0o700)
    corrupt = receipts / (("a" * 64) + ".json")
    corrupt.write_bytes(b'{"schema_version":1}\n')
    corrupt.chmod(0o600)
    signaler = FakeSignalBackend()

    with pytest.raises(state.StateCorruption):
        cleanup.execute_cleanup(
            cleanup.plan_cleanup(snapshot),
            store,
            tree,
            signaler,
            clock,
            apply=True,
        )

    assert signaler.calls == []


@pytest.mark.parametrize("identity_count", [1, 2])
def test_force_delivery_event_is_partial_only_for_strict_subsets(
    stubborn_context, identity_count
) -> None:
    store, tree, clock, snapshot, process, _lease, _classification = stubborn_context
    if identity_count == 2:
        snapshot, _second = _expand_stubborn_process(store, tree, clock, process)
    classification = snapshot.classifications[0]
    token = cleanup.issue_force_token(classification, clock)
    actions = cleanup.plan_cleanup(snapshot, force=True)

    report = cleanup.execute_cleanup(
        actions,
        store,
        tree,
        FakeSignalBackend(),
        clock,
        apply=True,
        confirm_token=token,
    )

    assert report.partial_force is False
    events = [
        json.loads(line)
        for line in (store.root / "events.jsonl").read_text().splitlines()
    ]
    names = [event["event"] for event in events]
    assert names.count("cleanup_force_partial") == identity_count - 1
    assert names.count("cleanup_force_delivery_receipt") == 1
