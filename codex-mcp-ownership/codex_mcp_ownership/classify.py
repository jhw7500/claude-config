from __future__ import annotations

from dataclasses import replace
import math
import os
from pathlib import Path

from .clock import Clock
from .model import (
    Association,
    AuditSnapshot,
    Classification,
    ManagedProcess,
    ObservedTime,
    ProcessIdentity,
    SessionLease,
)
from .procfs import LinuxProcfs
from .state import StateCorruption, StateStore


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


def _lease_matches(process: ManagedProcess, lease: SessionLease) -> bool:
    same_host = bool(process.host_keys & frozenset(lease.host_keys))
    same_cwd = os.path.realpath(process.cwd) == os.path.realpath(lease.cwd)
    within_window = (
        abs(process.spawned.boottime - lease.observed.boottime)
        <= ASSOCIATION_WINDOW_SECONDS
    )
    return same_host and same_cwd and within_window and lease.state == "active"


def _lease_has_owner_evidence(process: ManagedProcess, lease: SessionLease) -> bool:
    return (
        bool(process.host_keys & frozenset(lease.host_keys))
        and os.path.realpath(process.cwd) == os.path.realpath(lease.cwd)
        and abs(process.spawned.boottime - lease.observed.boottime)
        <= ASSOCIATION_WINDOW_SECONDS
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
    matches = tuple(lease for lease in leases if _lease_matches(process, lease))
    if len(matches) == 1:
        return Association(
            kind="session",
            session_id=matches[0].session_id,
            shared_owner=None,
            reason_codes=("unique_matching_session",),
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
    identities = (process.wrapper,) + (() if process.child is None else (process.child,))
    identities += process.members
    unique = {identity.stable_key(): identity for identity in identities}
    return tuple(unique[key] for key in sorted(unique))


def _observe_process(
    process: ManagedProcess,
    procfs: LinuxProcfs,
) -> tuple[tuple[ProcessIdentity, ...], str | None]:
    live: list[ProcessIdentity] = []
    for expected in _recorded_identities(process):
        try:
            current = procfs.identity(expected.pid)
        except (OSError, ValueError):
            return (), "process_identity_unavailable"
        if current is None:
            continue
        if current != expected:
            return (), "process_identity_mismatch"
        live.append(expected)
    return tuple(live), None


def _live_host_keys(procfs: LinuxProcfs) -> frozenset[str] | None:
    proc_root = getattr(procfs, "proc_root", None)
    if not isinstance(proc_root, Path):
        return None
    try:
        pids = sorted(
            int(entry.name) for entry in proc_root.iterdir() if entry.name.isdecimal()
        )
    except OSError:
        return None
    keys: set[str] = set()
    for pid in pids:
        try:
            identity = procfs.identity(pid)
        except (OSError, ValueError):
            return None
        if identity is not None:
            keys.add(identity.stable_key())
    return frozenset(keys)


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


def classify_process(
    process: ManagedProcess,
    leases: tuple[SessionLease, ...],
    procfs: LinuxProcfs,
    now_boot: float,
    grace_seconds: float = OWNER_GRACE_SECONDS,
) -> Classification:
    if (
        not math.isfinite(now_boot)
        or not math.isfinite(grace_seconds)
        or grace_seconds < 0
    ):
        return _classification(
            process,
            "unknown",
            ("invalid_classification_time",),
            (),
        )
    live_identities, identity_error = _observe_process(process, procfs)
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
        return _classification(process, "unknown", ("unmanaged",), live_identities)

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
    if not _lease_has_owner_evidence(process, owner):
        return _classification(
            process,
            "unknown",
            ("owner_evidence_mismatch",),
            live_identities,
        )

    owner_loss_reason: str | None = None
    if owner.state == "ended":
        owner_loss_reason = "owner_session_ended"
    else:
        host_keys = _live_host_keys(procfs)
        if host_keys is None:
            return _classification(
                process,
                "unknown",
                ("owner_identity_unavailable",),
                live_identities,
            )
        if process.host_keys & host_keys:
            return _classification(
                process,
                "active",
                ("owner_active",),
                live_identities,
            )
        owner_loss_reason = "owner_host_gone"

    first_gone = process.first_owner_gone_boot
    if first_gone is None:
        first_gone = now_boot
        process = replace(process, first_owner_gone_boot=first_gone)
    if first_gone > now_boot:
        return _classification(
            process,
            "unknown",
            ("owner_loss_time_in_future",),
            live_identities,
        )
    grace_deadline = first_gone + grace_seconds
    if process.term_sent_boot is not None:
        if process.term_sent_boot > now_boot:
            return _classification(
                process,
                "unknown",
                ("term_time_in_future",),
                live_identities,
                grace_deadline,
            )
        if now_boot - process.term_sent_boot >= _TERM_SURVIVOR_SECONDS:
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
) -> Classification:
    live_identities, _ = _observe_process(process, procfs)
    return _classification(process, "unknown", (reason,), live_identities)


def _audit_boot_id(
    procfs: LinuxProcfs,
    processes: tuple[ManagedProcess, ...],
    leases: tuple[SessionLease, ...],
) -> str:
    read_boot_id = getattr(procfs, "_boot_id", None)
    if callable(read_boot_id):
        try:
            boot_id = read_boot_id()
        except OSError:
            boot_id = None
        if isinstance(boot_id, str) and boot_id:
            return boot_id
    if processes:
        return processes[0].wrapper.boot_id
    if leases:
        return leases[0].observed.boot_id
    return ""


def build_audit(store: StateStore, procfs: LinuxProcfs, clock: Clock) -> AuditSnapshot:
    audit_store = StateStore(
        store.root,
        read_only=True,
        lock_timeout=store.lock_timeout,
    )
    corrupt_count = 0
    sessions_corrupt = False
    try:
        leases = audit_store.load_sessions()
    except StateCorruption:
        leases = ()
        sessions_corrupt = True
        corrupt_count += 1
    try:
        processes = audit_store.load_processes()
    except StateCorruption:
        processes = ()
        corrupt_count += 1

    now_boot = clock.boottime()
    classifications = tuple(
        sorted(
            (
                _unknown_after_corruption(
                    process,
                    procfs,
                    "corrupt_session_state",
                )
                if sessions_corrupt
                else classify_process(process, leases, procfs, now_boot)
                for process in processes
            ),
            key=lambda item: item.process.wrapper.stable_key(),
        )
    )
    counts = {state: 0 for state in _STATE_ORDER}
    for item in classifications:
        counts[item.state] += 1

    unique_live: dict[str, ProcessIdentity] = {}
    rss_kib = 0
    for item in classifications:
        for identity in item.live_identities:
            key = identity.stable_key()
            if key in unique_live:
                continue
            rss = procfs.rss_kib(identity)
            try:
                still_live = procfs.identity(identity.pid) == identity
            except (OSError, ValueError):
                still_live = False
            if not still_live:
                continue
            unique_live[key] = identity
            if rss is not None:
                rss_kib += rss

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
        _audit_boot_id(procfs, processes, leases),
        now_boot,
    )
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
