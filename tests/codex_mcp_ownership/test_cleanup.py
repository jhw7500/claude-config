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


class DryRunStore:
    def __init__(self) -> None:
        self.lock_count = 0
        self.mutation_count = 0

    def locked(self):
        self.lock_count += 1
        raise AssertionError("dry run must not acquire the state lock")


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
    stubborn_process = replace(process, term_sent_boot=280.0)
    store.save_process(stubborn_process)
    snapshot = classify.build_audit(store, tree, clock)
    classification = snapshot.classifications[0]
    assert classification.state == "stubborn"
    return store, tree, clock, snapshot, stubborn_process, lease, classification


def test_dry_run_never_locks_store_or_opens_pidfd() -> None:
    snapshot = snapshot_for("orphan", eligible_term=True)
    signaler = FakeSignalBackend()
    store = DryRunStore()
    identity = snapshot.classifications[0].live_identities[0]

    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(snapshot),
        store,
        ExactProcfs((identity,)),
        signaler,
        FakeClock(boot=300.0),
        apply=False,
    )

    assert report.attempted == 0
    assert report.before_count == 1
    assert report.before_rss_kib == 64
    assert report.after_count == 1
    assert report.after_rss_kib == 64
    assert signaler.calls == []
    assert store.lock_count == 0
    assert store.mutation_count == 0


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
            assert store._owns_lock()
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
    assert report.before_count == 1
    assert report.before_rss_kib == 128
    assert report.after_count == 1
    assert report.after_rss_kib == 128
    assert store.load_processes()[0] == replace(process, term_sent_boot=300.0)
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
    original_build_audit = cleanup.classify.build_audit
    changed = False

    def mutate_after_reaudit(*args, **kwargs):
        nonlocal changed
        result = original_build_audit(*args, **kwargs)
        if not changed:
            change_start_ticks(tree, process.wrapper)
            changed = True
        return result

    monkeypatch.setattr(cleanup.classify, "build_audit", mutate_after_reaudit)
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
    }
    assert payload["schema_version"] == 1
    assert payload["boot_id"] == "test-boot-id"
    assert payload["identity_keys"] == sorted(
        identity.stable_key() for identity in classification.live_identities
    )
    assert payload["reason_codes"] == list(classification.reason_codes)
    assert payload["issued_boot"] == 300.0
    assert payload["expires_boot"] == 600.0
    assert "=" not in token


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
    expanded = replace(process, child=child, members=(process.wrapper, child))
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
    store.save_process(replace(process, child=child, members=(process.wrapper, child)))
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
