from __future__ import annotations

from dataclasses import replace
import math
import os
from pathlib import Path
import time
from typing import Callable

from .clock import Clock
from .model import (
    Association,
    AuditSnapshot,
    Classification,
    ManagedProcess,
    ObservedTime,
    ProcessIdentity,
    SessionLease,
    lease_generation_digest,
)
from .procfs import IdentityObservation, LinuxProcfs
from .state import OperationDeadlineExceeded, StateCorruption, StateStore


ASSOCIATION_WINDOW_SECONDS = 30.0
OWNER_GRACE_SECONDS = 120.0
_TERM_SURVIVOR_SECONDS = 10.0
_STATE_ORDER = (
    "active",
    "shared",
    "exiting",
    "orphan",
    "unknown",
    "stubborn",
    "gone",
)


def _deadline_check(
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise OperationDeadlineExceeded("audit deadline exhausted")


def _owner_host_keys(
    process: ManagedProcess,
    lease: SessionLease,
) -> frozenset[str]:
    return process.host_keys & frozenset(lease.host_keys)


def _lease_has_owner_evidence(process: ManagedProcess, lease: SessionLease) -> bool:
    same_host = bool(_owner_host_keys(process, lease))
    same_cwd = os.path.realpath(process.cwd) == os.path.realpath(lease.cwd)
    within_window = (
        abs(process.spawned.boottime - lease.observed.boottime)
        <= ASSOCIATION_WINDOW_SECONDS
    )
    return same_host and same_cwd and within_window


def _lease_matches_recorded_generation(
    process: ManagedProcess,
    lease: SessionLease,
) -> bool:
    return bool(
        process.owner_generation is not None
        and process.owner_generation == lease_generation_digest(lease)
    )


def _active_owner_candidates(
    process: ManagedProcess,
    leases: tuple[SessionLease, ...],
) -> tuple[SessionLease, ...]:
    return tuple(
        lease
        for lease in leases
        if lease.state == "active" and _lease_has_owner_evidence(process, lease)
    )


def associate_owner(
    process: ManagedProcess,
    leases: tuple[SessionLease, ...],
    now_boot: float,
) -> Association:
    del now_boot
    if process.shared_owner is not None:
        return Association(
            kind="shared",
            session_id=None,
            shared_owner=process.shared_owner,
            reason_codes=("explicit_shared_owner",),
        )
    matches = _active_owner_candidates(process, leases)
    if len(matches) == 1:
        return Association(
            kind="session",
            session_id=matches[0].session_id,
            shared_owner=None,
            reason_codes=("unique_matching_session",),
            owner_generation=lease_generation_digest(matches[0]),
        )
    if len(matches) > 1:
        return Association(
            kind="unknown",
            session_id=None,
            shared_owner=None,
            reason_codes=("multiple_matching_sessions",),
        )
    return Association(
        kind="unknown",
        session_id=None,
        shared_owner=None,
        reason_codes=("no_matching_session",),
    )


def _recorded_identities(process: ManagedProcess) -> tuple[ProcessIdentity, ...]:
    identities = (process.wrapper,) + (
        () if process.child is None else (process.child,)
    )
    identities += process.members
    unique = {identity.stable_key(): identity for identity in identities}
    return tuple(unique[key] for key in sorted(unique))


def _observe_process(
    process: ManagedProcess,
    procfs: LinuxProcfs,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[tuple[ProcessIdentity, ...], str | None]:
    live: list[ProcessIdentity] = []
    for expected in _recorded_identities(process):
        _deadline_check(deadline, monotonic)
        observation = _observe_identity(procfs, expected.pid)
        _deadline_check(deadline, monotonic)
        if observation.kind == "unavailable":
            return (), "process_identity_unavailable"
        if observation.kind == "missing":
            continue
        if observation.identity != expected:
            return (), "process_identity_mismatch"
        live.append(expected)
    return tuple(live), None


def _observe_identity(procfs: LinuxProcfs, pid: int) -> IdentityObservation:
    observer = getattr(procfs, "observe_identity", None)
    if callable(observer):
        try:
            observation = observer(pid)
        except (OSError, ValueError):
            return IdentityObservation("unavailable", None)
        if isinstance(observation, IdentityObservation):
            return observation
        return IdentityObservation("unavailable", None)
    try:
        identity = procfs.identity(pid)
    except (OSError, ValueError):
        return IdentityObservation("unavailable", None)
    if identity is None:
        return IdentityObservation("unavailable", None)
    return IdentityObservation("live", identity)


def _host_identity_state(
    procfs: LinuxProcfs,
    owner_host_keys: frozenset[str],
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    _deadline_check(deadline, monotonic)
    proc_root = getattr(procfs, "proc_root", None)
    if not isinstance(proc_root, Path):
        return "unavailable"
    try:
        pids = sorted(
            int(entry.name) for entry in proc_root.iterdir() if entry.name.isdecimal()
        )
    except OSError:
        return "unavailable"
    unavailable = False
    for pid in pids:
        _deadline_check(deadline, monotonic)
        observation = _observe_identity(procfs, pid)
        _deadline_check(deadline, monotonic)
        if observation.kind == "unavailable":
            unavailable = True
            continue
        if (
            observation.kind == "live"
            and observation.identity is not None
            and observation.identity.stable_key() in owner_host_keys
        ):
            return "live"
    return "unavailable" if unavailable else "missing"


def _classification(
    process: ManagedProcess,
    state: str,
    reason_codes: tuple[str, ...],
    live_identities: tuple[ProcessIdentity, ...],
    grace_deadline_boot: float | None = None,
    eligible_term: bool = False,
) -> Classification:
    return Classification(
        process=process,
        state=state,
        reason_codes=reason_codes,
        live_identities=live_identities,
        grace_deadline_boot=grace_deadline_boot,
        eligible_term=eligible_term,
    )


def _valid_boot_time(value: object, now_boot: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    converted = float(value)
    return (
        math.isfinite(converted)
        and converted >= 0
        and (now_boot is None or converted <= now_boot)
    )


def classify_process(
    process: ManagedProcess,
    leases: tuple[SessionLease, ...],
    procfs: LinuxProcfs,
    now_boot: float,
    grace_seconds: float = OWNER_GRACE_SECONDS,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Classification:
    _deadline_check(deadline, monotonic)
    if (
        not _valid_boot_time(now_boot)
        or not math.isfinite(grace_seconds)
        or grace_seconds < 0
        or not math.isfinite(now_boot + grace_seconds)
    ):
        return _classification(
            process,
            "unknown",
            ("invalid_classification_time",),
            (),
        )
    if not _valid_boot_time(process.spawned.boottime, now_boot):
        return _classification(
            process,
            "unknown",
            ("invalid_lifecycle_time",),
            (),
        )
    live_identities, identity_error = _observe_process(
        process,
        procfs,
        deadline,
        monotonic,
    )
    if identity_error is not None:
        return _classification(process, "unknown", (identity_error,), ())
    if not live_identities:
        return _classification(process, "gone", ("process_gone",), ())
    if process.shared_owner is not None:
        return _classification(
            process,
            "shared",
            ("explicit_shared_owner",),
            live_identities,
        )
    if process.owner_session_id is None:
        return _classification(
            process,
            "unknown",
            process.owner_reason_codes or ("unmanaged",),
            live_identities,
        )

    owner_leases = tuple(
        lease for lease in leases if lease.session_id == process.owner_session_id
    )
    if not owner_leases:
        return _classification(
            process,
            "unknown",
            ("owner_lease_missing",),
            live_identities,
        )
    if len(owner_leases) > 1:
        return _classification(
            process,
            "unknown",
            ("multiple_owner_leases",),
            live_identities,
        )
    owner = owner_leases[0]
    live_boot_ids = {identity.boot_id for identity in live_identities}
    if (
        owner.observed.boot_id != process.spawned.boot_id
        or owner.observed.boot_id not in live_boot_ids
        or (owner.ended is not None and owner.ended.boot_id != owner.observed.boot_id)
    ):
        return _classification(
            process,
            "unknown",
            ("owner_boot_mismatch",),
            live_identities,
        )
    if not _valid_boot_time(owner.observed.boottime, now_boot):
        return _classification(
            process,
            "unknown",
            ("invalid_lifecycle_time",),
            live_identities,
        )
    owner_time_floor = max(process.spawned.boottime, owner.observed.boottime)
    if owner.state == "ended":
        if (
            owner.ended is None
            or not _valid_boot_time(owner.ended.boottime, now_boot)
            or owner.ended.boottime < owner_time_floor
        ):
            return _classification(
                process,
                "unknown",
                ("invalid_lifecycle_time",),
                live_identities,
            )
        owner_time_floor = max(owner_time_floor, owner.ended.boottime)
    elif owner.ended is not None:
        return _classification(
            process,
            "unknown",
            ("invalid_lifecycle_time",),
            live_identities,
        )
    if not _lease_matches_recorded_generation(process, owner):
        return _classification(
            process,
            "unknown",
            ("owner_generation_mismatch",),
            live_identities,
        )
    if not _lease_has_owner_evidence(process, owner):
        return _classification(
            process,
            "unknown",
            ("owner_evidence_mismatch",),
            live_identities,
        )

    first_gone = process.first_owner_gone_boot
    term_sent = process.term_sent_boot
    term_sent_keys = process.term_sent_keys
    force_receipt_reasons = tuple(
        reason
        for reason in process.owner_reason_codes
        if reason
        in {
            "signal_force_pending",
            "signal_force_conflict",
            "signal_force_delivered",
        }
    )
    if force_receipt_reasons:
        return _classification(
            process,
            "unknown",
            force_receipt_reasons,
            live_identities,
        )
    if "signal_term_pending" in process.owner_reason_codes:
        return _classification(
            process,
            "unknown",
            ("state_persistence_pending",),
            live_identities,
        )
    if (term_sent is None) != (not term_sent_keys):
        return _classification(
            process,
            "unknown",
            ("invalid_lifecycle_time",),
            live_identities,
        )
    if first_gone is not None and (
        not _valid_boot_time(first_gone, now_boot) or first_gone < owner_time_floor
    ):
        return _classification(
            process,
            "unknown",
            ("invalid_lifecycle_time",),
            live_identities,
        )
    grace_deadline: float | None = None
    if first_gone is not None:
        grace_deadline = first_gone + grace_seconds
        if not math.isfinite(grace_deadline):
            return _classification(
                process,
                "unknown",
                ("invalid_lifecycle_time",),
                live_identities,
            )
    if term_sent is not None and (
        first_gone is None
        or not _valid_boot_time(term_sent, now_boot)
        or grace_deadline is None
        or term_sent < grace_deadline
    ):
        return _classification(
            process,
            "unknown",
            ("invalid_lifecycle_time",),
            live_identities,
        )
    active_candidates = _active_owner_candidates(process, leases)
    if owner.state == "active" and (
        len(active_candidates) != 1
        or active_candidates[0].session_id != owner.session_id
    ):
        return _classification(
            process,
            "unknown",
            ("ambiguous_active_owner",),
            live_identities,
        )
    if owner.state == "ended" and active_candidates:
        return _classification(
            process,
            "unknown",
            ("competing_active_owner",),
            live_identities,
        )

    owner_loss_reason: str | None = None
    if owner.state == "ended":
        owner_loss_reason = "owner_session_ended"
    else:
        host_state = _host_identity_state(
            procfs,
            _owner_host_keys(process, owner),
            deadline,
            monotonic,
        )
        if host_state == "unavailable":
            return _classification(
                process,
                "unknown",
                ("owner_identity_unavailable",),
                live_identities,
            )
        if host_state == "live":
            if first_gone is not None or term_sent is not None:
                return _classification(
                    process,
                    "unknown",
                    ("invalid_lifecycle_time",),
                    live_identities,
                )
            return _classification(
                process,
                "active",
                ("owner_active",),
                live_identities,
            )
        owner_loss_reason = "owner_host_gone"

    if first_gone is None:
        first_gone = now_boot
        process = replace(process, first_owner_gone_boot=first_gone)
        grace_deadline = first_gone + grace_seconds
        if not math.isfinite(grace_deadline):
            return _classification(
                process,
                "unknown",
                ("invalid_lifecycle_time",),
                live_identities,
            )
    assert grace_deadline is not None
    if term_sent is not None:
        live_keys = frozenset(identity.stable_key() for identity in live_identities)
        if live_keys and live_keys.issubset(term_sent_keys):
            term_sent_keys = live_keys
            process = replace(process, term_sent_keys=live_keys)
        if live_keys != term_sent_keys:
            process = replace(
                process,
                term_sent_boot=None,
                term_sent_keys=frozenset(),
            )
            return _classification(
                process,
                "orphan",
                (owner_loss_reason, "term_evidence_membership_changed"),
                live_identities,
                grace_deadline,
                eligible_term=True,
            )
        if now_boot - term_sent >= _TERM_SURVIVOR_SECONDS:
            return _classification(
                process,
                "stubborn",
                (owner_loss_reason, "term_survivor"),
                live_identities,
                grace_deadline,
            )
        return _classification(
            process,
            "exiting",
            (owner_loss_reason, "term_shutdown_grace"),
            live_identities,
            grace_deadline,
        )
    if now_boot < grace_deadline:
        return _classification(
            process,
            "exiting",
            (owner_loss_reason, "owner_grace_active"),
            live_identities,
            grace_deadline,
        )
    return _classification(
        process,
        "orphan",
        (owner_loss_reason, "owner_grace_elapsed"),
        live_identities,
        grace_deadline,
        eligible_term=True,
    )


def _unknown_after_corruption(
    process: ManagedProcess,
    procfs: LinuxProcfs,
    reason: str,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Classification:
    live_identities, _ = _observe_process(process, procfs, deadline, monotonic)
    return _classification(process, "unknown", (reason,), live_identities)


def _audit_boot_id(
    procfs: LinuxProcfs,
    processes: tuple[ManagedProcess, ...],
    leases: tuple[SessionLease, ...],
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    _deadline_check(deadline, monotonic)
    read_boot_id = getattr(procfs, "_boot_id", None)
    if callable(read_boot_id):
        try:
            boot_id = read_boot_id()
        except OSError:
            boot_id = None
        if isinstance(boot_id, str) and boot_id:
            _deadline_check(deadline, monotonic)
            return boot_id
    if processes:
        return processes[0].wrapper.boot_id
    if leases:
        return leases[0].observed.boot_id
    return ""


def _audit_rss_observation(
    procfs: LinuxProcfs,
    identity: ProcessIdentity,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bool, int]:
    try:
        _deadline_check(deadline, monotonic)
        rss_kib = procfs.rss_kib(identity)
        _deadline_check(deadline, monotonic)
        observation = _observe_identity(procfs, identity.pid)
        _deadline_check(deadline, monotonic)
    except (OSError, ValueError):
        return False, 0
    if (
        isinstance(rss_kib, bool)
        or not isinstance(rss_kib, int)
        or rss_kib < 0
        or observation.kind != "live"
        or observation.identity != identity
    ):
        return False, 0
    return True, rss_kib


def build_audit(
    store: StateStore,
    procfs: LinuxProcfs,
    clock: Clock,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> AuditSnapshot:
    _deadline_check(deadline, monotonic)
    if store._owns_lock():
        raise ValueError("audit cannot run under a mutable lock")
    audit_store = StateStore(
        store.root,
        read_only=True,
        lock_timeout=store.lock_timeout,
    )
    corrupt_count = 0
    sessions_corrupt = False
    try:
        leases = audit_store.load_sessions(deadline=deadline, monotonic=monotonic)
    except StateCorruption:
        leases = ()
        sessions_corrupt = True
        corrupt_count += 1
    try:
        processes = audit_store.load_processes(
            deadline=deadline,
            monotonic=monotonic,
        )
    except StateCorruption:
        processes = ()
        corrupt_count += 1

    return build_audit_from_records(
        processes,
        leases,
        procfs,
        clock,
        corrupt_count=corrupt_count,
        sessions_corrupt=sessions_corrupt,
        deadline=deadline,
        monotonic=monotonic,
    )


def build_audit_from_records(
    processes: tuple[ManagedProcess, ...],
    leases: tuple[SessionLease, ...],
    procfs: LinuxProcfs,
    clock: Clock,
    *,
    corrupt_count: int = 0,
    sessions_corrupt: bool = False,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> AuditSnapshot:
    _deadline_check(deadline, monotonic)
    now_boot = clock.boottime()
    _deadline_check(deadline, monotonic)
    entries: list[tuple[ManagedProcess, Classification]] = []
    for process in processes:
        _deadline_check(deadline, monotonic)
        item = (
            _unknown_after_corruption(
                process,
                procfs,
                "corrupt_session_state",
                deadline,
                monotonic,
            )
            if sessions_corrupt
            else classify_process(
                process,
                leases,
                procfs,
                now_boot,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        _deadline_check(deadline, monotonic)
        entries.append((process, item))
    initial_entries = tuple(
        sorted(entries, key=lambda entry: entry[0].wrapper.stable_key())
    )
    observations: dict[str, tuple[bool, int]] = {}
    unique_live: dict[str, ProcessIdentity] = {}
    rss_kib = 0
    classifications_list: list[Classification] = []
    for stored_process, item in initial_entries:
        _deadline_check(deadline, monotonic)
        verified: list[ProcessIdentity] = []
        unavailable = False
        for identity in item.live_identities:
            _deadline_check(deadline, monotonic)
            key = identity.stable_key()
            observation = observations.get(key)
            if observation is None:
                observation = _audit_rss_observation(
                    procfs,
                    identity,
                    deadline,
                    monotonic,
                )
                observations[key] = observation
            exact, rss = observation
            if not exact:
                unavailable = True
                continue
            verified.append(identity)
            if key not in unique_live:
                unique_live[key] = identity
                rss_kib += rss
        if unavailable:
            reason_codes = item.reason_codes
            if "audit_identity_unavailable" not in reason_codes:
                reason_codes += ("audit_identity_unavailable",)
            item = replace(
                item,
                process=stored_process,
                state="unknown",
                reason_codes=reason_codes,
                live_identities=tuple(verified),
                eligible_term=False,
            )
        classifications_list.append(item)
        _deadline_check(deadline, monotonic)
    classifications = tuple(classifications_list)

    counts = {state: 0 for state in _STATE_ORDER}
    for item in classifications:
        counts[item.state] += 1

    managed = sum(
        any(identity.stable_key() in unique_live for identity in item.live_identities)
        for item in classifications
    )
    unknown = sum(
        any(identity.stable_key() in unique_live for identity in item.live_identities)
        and item.state == "unknown"
        for item in classifications
    )
    owned_or_shared = managed - unknown
    generated = ObservedTime(
        clock.wall_iso(),
        _audit_boot_id(procfs, processes, leases, deadline, monotonic),
        now_boot,
    )
    _deadline_check(deadline, monotonic)
    return AuditSnapshot(
        schema_version=1,
        generated=generated,
        classifications=classifications,
        state_counts=tuple((state, counts[state]) for state in _STATE_ORDER),
        process_count=len(unique_live),
        rss_kib=rss_kib,
        ownership_coverage=(
            ("managed", managed),
            ("owned_or_shared", owned_or_shared),
            ("unknown", unknown),
        ),
        corrupt_count=corrupt_count,
    )
