from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codex_mcp_ownership import classify, model, procfs, state
from helpers import FakeClock, make_private_directory, write_private_file, write_proc_entry


@pytest.fixture
def host_identity() -> model.ProcessIdentity:
    return model.ProcessIdentity(
        boot_id="test-boot-id",
        pid=100,
        ppid=1,
        pgid=100,
        start_ticks=1000,
        exe_dev=8,
        exe_ino=100,
        exe_name="codex",
    )


@pytest.fixture
def process(host_identity: model.ProcessIdentity) -> model.ManagedProcess:
    wrapper = replace(
        host_identity,
        pid=321,
        ppid=100,
        pgid=321,
        start_ticks=3210,
        exe_ino=321,
        exe_name="node",
    )
    return model.ManagedProcess(
        schema_version=1,
        record_id="managed-example",
        scope="user",
        server="example",
        cwd="/workspace/project",
        wrapper=wrapper,
        child=None,
        members=(wrapper,),
        pgid=wrapper.pgid,
        host_keys=frozenset({host_identity.stable_key()}),
        spawned=model.ObservedTime(
            "2026-08-29T00:00:00+00:00",
            "test-boot-id",
            100.0,
        ),
    )


@pytest.fixture
def matching_lease(host_identity: model.ProcessIdentity) -> model.SessionLease:
    return model.SessionLease(
        schema_version=1,
        session_id="thr_test",
        cwd="/workspace/project/./",
        source="SessionStart",
        host_keys=(host_identity.stable_key(),),
        state="active",
        observed=model.ObservedTime(
            "2026-08-29T00:00:05+00:00",
            "test-boot-id",
            105.0,
        ),
    )


def test_unique_matching_lease_becomes_owner(process, matching_lease):
    association = classify.associate_owner(
        process,
        (matching_lease,),
        now_boot=105.0,
    )
    assert association.kind == "session"
    assert association.session_id == matching_lease.session_id


def test_two_matching_leases_are_unknown(process, matching_lease):
    other = replace(matching_lease, session_id="thr_other")
    association = classify.associate_owner(
        process,
        (matching_lease, other),
        now_boot=105.0,
    )
    assert association.kind == "unknown"
    assert association.reason_codes == ("multiple_matching_sessions",)


@pytest.mark.parametrize(
    "lease_change",
    [
        {"cwd": "/workspace/other"},
        {"host_keys": ("not-the-exact-host-key",)},
        {
            "observed": model.ObservedTime(
                "2026-08-29T00:01:00+00:00",
                "test-boot-id",
                130.000001,
            )
        },
        {"state": "ended"},
    ],
    ids=("cwd-mismatch", "host-mismatch", "outside-window", "ended-lease"),
)
def test_incomplete_lease_evidence_is_unknown(process, matching_lease, lease_change):
    association = classify.associate_owner(
        process,
        (replace(matching_lease, **lease_change),),
        now_boot=105.0,
    )
    assert association.kind == "unknown"
    assert association.reason_codes == ("no_matching_session",)


def test_association_window_includes_exact_thirty_second_boundary(process, matching_lease):
    boundary = replace(
        matching_lease,
        observed=replace(matching_lease.observed, boottime=130.0),
    )
    association = classify.associate_owner(process, (boundary,), now_boot=130.0)
    assert association.kind == "session"


def test_explicit_shared_owner_does_not_depend_on_session_match(process, matching_lease):
    shared = replace(process, shared_owner="user:shared-example")
    association = classify.associate_owner(shared, (matching_lease,), now_boot=105.0)
    assert association.kind == "shared"
    assert association.shared_owner == "user:shared-example"


class ClassificationScenario:
    def __init__(self, fake_proc, process, matching_lease, host_identity):
        self.fake_proc = fake_proc
        self.process = process
        self.matching_lease = matching_lease
        self.host_identity = host_identity
        write_proc_entry(
            fake_proc.root,
            host_identity.pid,
            "100 (codex) S 1 100 100 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1000 0 0\n",
            fake_proc.exe,
        )
        tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
        live_host = tree.identity(host_identity.pid)
        live_wrapper = tree.identity(process.wrapper.pid)
        assert live_host is not None
        assert live_wrapper is not None
        self.host_identity = live_host
        self.process = replace(
            process,
            wrapper=live_wrapper,
            members=(live_wrapper,),
            host_keys=frozenset({live_host.stable_key()}),
            owner_session_id=matching_lease.session_id,
        )
        self.matching_lease = replace(
            matching_lease,
            host_keys=(live_host.stable_key(),),
        )
        self.procfs = tree
        self.now_boot = 300.0

    def classify(self, owner_state: str, host_live: bool, elapsed: float):
        lease = replace(
            self.matching_lease,
            state=owner_state,
            ended=(
                None
                if owner_state == "active"
                else replace(self.matching_lease.observed, boottime=self.now_boot)
            ),
        )
        first_gone = None if elapsed == 0.0 else self.now_boot - elapsed
        managed = replace(
            self.process,
            first_owner_gone_boot=(None if owner_state == "active" else first_gone),
        )
        host_path = self.fake_proc.root / str(self.host_identity.pid)
        if host_live and not host_path.exists():
            raise AssertionError("scenario host cannot be restored with the same identity")
        if not host_live and host_path.exists():
            for child in host_path.iterdir():
                child.unlink()
            host_path.rmdir()
        return classify.classify_process(
            managed,
            (lease,),
            self.procfs,
            self.now_boot,
        )


@pytest.fixture
def scenario(fake_proc, process, matching_lease, host_identity):
    return ClassificationScenario(fake_proc, process, matching_lease, host_identity)


@pytest.mark.parametrize(
    ("owner_state", "host_live", "elapsed", "expected"),
    [
        ("active", True, 999.0, "active"),
        ("ended", False, 0.0, "exiting"),
        ("ended", False, 119.9, "exiting"),
        ("ended", False, 120.0, "orphan"),
    ],
)
def test_owner_lifecycle(owner_state, host_live, elapsed, expected, scenario):
    result = scenario.classify(owner_state, host_live, elapsed)
    assert result.state == expected
    if owner_state == "ended" and elapsed == 0.0:
        assert result.process.first_owner_gone_boot == 300.0


def test_live_identity_with_disappeared_active_host_starts_owner_grace(scenario):
    result = scenario.classify("active", False, 0.0)
    assert result.state == "exiting"
    assert result.process.first_owner_gone_boot == 300.0
    assert result.grace_deadline_boot == 420.0


def test_explicit_shared_process_is_never_term_eligible(scenario):
    shared = replace(scenario.process, shared_owner="user:shared-example")
    result = classify.classify_process(
        shared,
        (scenario.matching_lease,),
        scenario.procfs,
        scenario.now_boot,
    )
    assert result.state == "shared"
    assert result.eligible_term is False


def test_unmanaged_process_is_unknown(scenario):
    unmanaged = replace(scenario.process, owner_session_id=None)
    result = classify.classify_process(
        unmanaged,
        (scenario.matching_lease,),
        scenario.procfs,
        scenario.now_boot,
    )
    assert result.state == "unknown"
    assert result.reason_codes == ("unmanaged",)


def test_reused_pid_is_unknown_and_not_live(scenario):
    scenario.fake_proc.write_start_ticks(
        scenario.process.wrapper.pid,
        scenario.process.wrapper.start_ticks + 1,
    )
    result = classify.classify_process(
        scenario.process,
        (scenario.matching_lease,),
        scenario.procfs,
        scenario.now_boot,
    )
    assert result.state == "unknown"
    assert result.reason_codes == ("process_identity_mismatch",)
    assert result.live_identities == ()
    assert result.eligible_term is False


def test_owner_boot_mismatch_is_unknown(scenario):
    mismatched = replace(
        scenario.matching_lease,
        observed=replace(scenario.matching_lease.observed, boot_id="previous-boot"),
    )
    result = classify.classify_process(
        scenario.process,
        (mismatched,),
        scenario.procfs,
        scenario.now_boot,
    )
    assert result.state == "unknown"
    assert result.reason_codes == ("owner_boot_mismatch",)


@pytest.mark.parametrize(
    ("now_boot", "grace_seconds"),
    [(float("nan"), 120.0), (300.0, float("inf")), (300.0, -1.0)],
)
def test_invalid_classification_time_is_unknown(
    scenario,
    now_boot,
    grace_seconds,
):
    result = classify.classify_process(
        scenario.process,
        (scenario.matching_lease,),
        scenario.procfs,
        now_boot,
        grace_seconds,
    )
    assert result.state == "unknown"
    assert result.reason_codes == ("invalid_classification_time",)
    assert result.eligible_term is False


def test_process_with_no_remaining_identity_is_gone(scenario):
    process_path = scenario.fake_proc.root / str(scenario.process.wrapper.pid)
    for child in process_path.iterdir():
        child.unlink()
    process_path.rmdir()
    result = classify.classify_process(
        scenario.process,
        (scenario.matching_lease,),
        scenario.procfs,
        scenario.now_boot,
    )
    assert result.state == "gone"
    assert result.live_identities == ()


@pytest.mark.parametrize(
    ("term_elapsed", "expected"),
    [(9.999, "exiting"), (10.0, "stubborn")],
)
def test_term_survivor_threshold_is_inclusive(scenario, term_elapsed, expected):
    ended = replace(
        scenario.matching_lease,
        state="ended",
        ended=replace(scenario.matching_lease.observed, boottime=100.0),
    )
    managed = replace(
        scenario.process,
        first_owner_gone_boot=100.0,
        term_sent_boot=scenario.now_boot - term_elapsed,
    )
    result = classify.classify_process(
        managed,
        (ended,),
        scenario.procfs,
        scenario.now_boot,
    )
    assert result.state == expected
    assert result.eligible_term is False


def _state_tree(root: Path) -> tuple[tuple[str, bytes | None], ...]:
    if not root.exists():
        return ()
    return tuple(
        (str(path.relative_to(root)), path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    )


def test_audit_is_read_only_with_writable_store_and_counts_exact_live_rss(
    tmp_path,
    scenario,
):
    root = tmp_path / "state"
    store = state.StateStore(root)
    store.save_session(scenario.matching_lease)
    store.save_process(scenario.process)
    before = _state_tree(root)

    snapshot = classify.build_audit(
        store,
        scenario.procfs,
        FakeClock(boot=scenario.now_boot),
    )

    assert _state_tree(root) == before
    assert snapshot.generated.boot_id == "test-boot-id"
    assert snapshot.process_count == 1
    assert snapshot.rss_kib == 128
    assert snapshot.state_counts == (
        ("active", 1),
        ("shared", 0),
        ("exiting", 0),
        ("orphan", 0),
        ("unknown", 0),
        ("stubborn", 0),
        ("gone", 0),
    )
    assert snapshot.ownership_coverage == (
        ("managed", 1),
        ("owned_or_shared", 1),
        ("unknown", 0),
    )
    assert snapshot.corrupt_count == 0


def test_audit_does_not_create_missing_state_root(tmp_path, fake_proc):
    root = tmp_path / "missing-state"
    snapshot = classify.build_audit(
        state.StateStore(root),
        procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path),
        FakeClock(boot=10.0),
    )
    assert snapshot.process_count == 0
    assert not root.exists()


def test_corrupt_session_state_makes_valid_process_unknown_without_quarantine(
    tmp_path,
    scenario,
):
    root = tmp_path / "state"
    store = state.StateStore(root)
    store.save_process(scenario.process)
    sessions = root / "sessions"
    make_private_directory(sessions)
    corrupt = sessions / ("a" * 64 + ".json")
    write_private_file(corrupt, b"{not-json}\n")
    before = _state_tree(root)

    snapshot = classify.build_audit(
        store,
        scenario.procfs,
        FakeClock(boot=scenario.now_boot),
    )

    assert _state_tree(root) == before
    assert snapshot.corrupt_count == 1
    assert len(snapshot.classifications) == 1
    assert snapshot.classifications[0].state == "unknown"
    assert snapshot.classifications[0].reason_codes == ("corrupt_session_state",)
    assert snapshot.classifications[0].eligible_term is False
    assert not (root / "corrupt").exists()


def test_corrupt_process_state_is_counted_without_mutation_or_synthetic_identity(
    tmp_path,
    scenario,
):
    root = tmp_path / "state"
    store = state.StateStore(root)
    store.save_session(scenario.matching_lease)
    store.save_process(scenario.process)
    corrupt = root / "processes" / ("0" * 64 + ".json")
    write_private_file(corrupt, b"{not-json}\n")
    before = _state_tree(root)

    snapshot = classify.build_audit(
        store,
        scenario.procfs,
        FakeClock(boot=scenario.now_boot),
    )

    assert _state_tree(root) == before
    assert snapshot.corrupt_count == 1
    assert snapshot.classifications == ()
    assert snapshot.process_count == 0
    assert not (root / "corrupt").exists()


def test_audit_excludes_identity_that_changes_during_rss_revalidation(
    tmp_path,
    scenario,
):
    root = tmp_path / "state"
    store = state.StateStore(root)
    store.save_session(scenario.matching_lease)
    store.save_process(scenario.process)

    class ReusedDuringRss:
        proc_root = scenario.procfs.proc_root

        def _boot_id(self):
            return scenario.procfs._boot_id()

        def identity(self, pid):
            return scenario.procfs.identity(pid)

        def rss_kib(self, identity):
            scenario.fake_proc.write_start_ticks(identity.pid, identity.start_ticks + 1)
            return scenario.procfs.rss_kib(identity)

    snapshot = classify.build_audit(
        store,
        ReusedDuringRss(),
        FakeClock(boot=scenario.now_boot),
    )

    assert snapshot.process_count == 0
    assert snapshot.rss_kib == 0


def test_audit_classification_order_is_stable_by_process_identity(tmp_path, scenario):
    root = tmp_path / "state"
    store = state.StateStore(root)
    store.save_session(scenario.matching_lease)
    second_identity = replace(
        scenario.process.wrapper,
        pid=400,
        start_ticks=4000,
        exe_ino=400,
    )
    second = replace(
        scenario.process,
        record_id="second",
        wrapper=second_identity,
        members=(second_identity,),
    )
    store.save_process(second)
    store.save_process(scenario.process)

    snapshot = classify.build_audit(
        store,
        scenario.procfs,
        FakeClock(boot=scenario.now_boot),
    )

    keys = [item.process.wrapper.stable_key() for item in snapshot.classifications]
    assert keys == sorted(keys)
