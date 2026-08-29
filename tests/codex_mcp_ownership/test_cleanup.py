from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
import signal

import pytest

from codex_mcp_ownership import classify, cleanup, model, procfs, state
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


def test_build_audit_uses_held_store_pinned_root(tmp_path, fake_proc):
    store, tree, _process = ended_owner_context_without_first_loss(tmp_path, fake_proc)
    displaced = tmp_path / "displaced-state"
    with store.locked():
        store.root.rename(displaced)
        store.root.mkdir(mode=0o700)
        snapshot = classify.build_audit(store, tree, FakeClock(boot=200.0))
    assert len(snapshot.classifications) == 1


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
    events = [
        json.loads(line)
        for line in (store.root / "events.jsonl").read_text().splitlines()
    ]
    assert events == [
        {
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
    ]


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

    original_authority = cleanup._current_authority

    def mutate_after_authority(*args, **kwargs):
        nonlocal changed
        result = original_authority(*args, **kwargs)
        if not changed:
            change_start_ticks(tree, process.wrapper)
            changed = True
        return result

    monkeypatch.setattr(cleanup, "_current_authority", mutate_after_authority)
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
    event = json.loads((store.root / "events.jsonl").read_text())
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
    assert store.load_processes()[0] == process


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
    assert store.load_processes()[0] == process
    event = json.loads((store.root / "events.jsonl").read_text())
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
    assert store.load_processes()[0] == process
    assert not (store.root / "events.jsonl").exists()


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
    event = json.loads((store.root / "events.jsonl").read_text())
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
    assert store.load_processes()[0] == process
    event = json.loads((store.root / "events.jsonl").read_text())
    assert event["event"] == "cleanup_signal_indeterminate"
    assert expected_reason in event["reason_codes"]
    fresh = classify.build_audit(store, tree, clock)
    assert fresh.classifications[0].state == "orphan"
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
    original_authority = cleanup._current_authority
    rebound = False

    def rebind_then_check(*args, **kwargs):
        nonlocal rebound
        if not rebound:
            store.root.rename(displaced)
            store.root.mkdir(mode=0o700)
            rebound = True
        return original_authority(*args, **kwargs)

    monkeypatch.setattr(cleanup, "_current_authority", rebind_then_check)
    signaler = FakeSignalBackend()
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        tree,
        signaler,
        clock,
        apply=True,
    )

    assert signaler.calls == []
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
    event = json.loads((store.root / "events.jsonl").read_text())
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
    assert store.load_processes()[0].term_sent_boot is None
    assert store.load_processes()[0].term_sent_keys == frozenset()
    event = json.loads((store.root / "events.jsonl").read_text())
    assert event["event"] == "cleanup_signal_indeterminate"
    assert "partial_signal_delivery" in event["reason_codes"]
    assert failure_reason in event["reason_codes"]
    assert "sigterm_survived" in event["reason_codes"]
    assert "identity_unavailable_after_signal" not in event["reason_codes"]
