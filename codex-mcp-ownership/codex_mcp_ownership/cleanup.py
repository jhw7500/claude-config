from __future__ import annotations

import base64
import binascii
from dataclasses import replace
import hashlib
import hmac
import json
import math
import os
import signal
from typing import Protocol

from . import classify
from .clock import Clock
from .model import (
    AuditSnapshot,
    Classification,
    CleanupAction,
    CleanupOutcome,
    CleanupReport,
    ManagedProcess,
    ProcessIdentity,
    SessionLease,
)
from .procfs import IdentityObservation, LinuxProcfs
from .state import StateStore, UnsafeStatePath


SHUTDOWN_GRACE_SECONDS = 10.0
FORCE_TOKEN_TTL_SECONDS = 300.0
_FORCE_TOKEN_SCHEMA_VERSION = 1
_FORCE_TOKEN_KEYS = {
    "boot_id",
    "expires_boot",
    "identity_keys",
    "issued_boot",
    "reason_codes",
    "schema_version",
    "term_sent_boot",
}


class SignalBackend(Protocol):
    def open(self, identity: ProcessIdentity) -> int:
        """Open a pidfd for an exact process identity."""

    def send(self, pidfd: int, signum: int) -> None:
        """Send one signal through an opened pidfd."""

    def close(self, pidfd: int) -> None:
        """Close an opened pidfd."""


class PidfdUnavailable(RuntimeError):
    """The running kernel or Python does not expose safe pidfd signaling."""


class InvalidForceConfirmation(ValueError):
    """A force confirmation does not match current stubborn evidence."""


class PidfdSignalBackend:
    def __init__(self) -> None:
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if not callable(pidfd_open) or not callable(pidfd_send_signal):
            raise PidfdUnavailable("pidfd signaling is unavailable")
        self._pidfd_open = pidfd_open
        self._pidfd_send_signal = pidfd_send_signal

    def open(self, identity: ProcessIdentity) -> int:
        return self._pidfd_open(identity.pid, 0)

    def send(self, pidfd: int, signum: int) -> None:
        self._pidfd_send_signal(pidfd, signum, None, 0)

    def close(self, pidfd: int) -> None:
        os.close(pidfd)


def _has_exact_term_evidence(classification: Classification) -> bool:
    term_sent_boot = classification.process.term_sent_boot
    live_keys = [identity.stable_key() for identity in classification.live_identities]
    return bool(
        classification.state == "stubborn"
        and not classification.eligible_term
        and not isinstance(term_sent_boot, bool)
        and isinstance(term_sent_boot, (int, float))
        and math.isfinite(float(term_sent_boot))
        and float(term_sent_boot) >= 0
        and live_keys
        and len(live_keys) == len(set(live_keys))
        and classification.process.term_sent_keys == frozenset(live_keys)
    )


def plan_cleanup(
    snapshot: AuditSnapshot,
    force: bool = False,
) -> tuple[CleanupAction, ...]:
    actions: list[CleanupAction] = []
    for classification in snapshot.classifications:
        automatic = (
            not force
            and classification.state == "orphan"
            and classification.eligible_term
        )
        forced = force and _has_exact_term_evidence(classification)
        if not (automatic or forced):
            continue
        process_key = classification.process.wrapper.stable_key()
        for identity in classification.live_identities:
            actions.append(
                CleanupAction(
                    process_key=process_key,
                    identity=identity,
                    classification_state=classification.state,
                    reason_codes=classification.reason_codes,
                    force=force,
                )
            )
    return tuple(sorted(actions, key=lambda action: action.identity.stable_key()))


def issue_force_token(classification: Classification, clock: Clock) -> str:
    if not isinstance(classification, Classification) or not _has_exact_term_evidence(
        classification
    ):
        raise InvalidForceConfirmation("force confirmation requires stubborn evidence")
    identity_keys = sorted(
        identity.stable_key() for identity in classification.live_identities
    )
    boot_ids = {identity.boot_id for identity in classification.live_identities}
    if (
        not identity_keys
        or len(identity_keys) != len(set(identity_keys))
        or len(boot_ids) != 1
    ):
        raise InvalidForceConfirmation(
            "force confirmation requires exact live identities"
        )
    issued_boot = _finite_boot_time(clock.boottime(), "issued_boot")
    term_sent_boot = _finite_boot_time(
        classification.process.term_sent_boot,
        "term_sent_boot",
    )
    if issued_boot < term_sent_boot:
        raise InvalidForceConfirmation("force confirmation predates TERM evidence")
    expires_boot = issued_boot + FORCE_TOKEN_TTL_SECONDS
    if not math.isfinite(expires_boot):
        raise InvalidForceConfirmation("force confirmation window is invalid")
    payload = {
        "schema_version": _FORCE_TOKEN_SCHEMA_VERSION,
        "boot_id": next(iter(boot_ids)),
        "identity_keys": identity_keys,
        "reason_codes": list(classification.reason_codes),
        "term_sent_boot": term_sent_boot,
        "issued_boot": issued_boot,
        "expires_boot": expires_boot,
    }
    canonical = _canonical_json(payload)
    encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"{encoded}.{digest}"


def select_force_actions(
    snapshot: AuditSnapshot,
    token: str | None,
    clock: Clock,
) -> tuple[CleanupAction, ...]:
    """Select the one stubborn classification addressed by a force token."""
    payload = _decode_force_token(token, clock.boottime())
    matches: list[Classification] = []
    for classification in snapshot.classifications:
        if classification.state != "stubborn":
            continue
        try:
            boot_id, identity_keys, reasons, term_sent = _force_evidence(classification)
        except InvalidForceConfirmation:
            continue
        if (
            payload["boot_id"] == snapshot.generated.boot_id == boot_id
            and payload["identity_keys"] == identity_keys
            and payload["reason_codes"] == reasons
            and payload["term_sent_boot"] == term_sent
        ):
            matches.append(classification)
    if len(matches) != 1:
        raise InvalidForceConfirmation(
            "force confirmation must select one current classification"
        )
    selected_key = matches[0].process.wrapper.stable_key()
    return tuple(
        action
        for action in plan_cleanup(snapshot, force=True)
        if action.process_key == selected_key
    )


def _state_snapshot(
    store: StateStore,
    expected_root_token: tuple[int, int] | None = None,
) -> tuple[tuple[int, int], tuple[ManagedProcess, ...], tuple[SessionLease, ...]]:
    with store.locked(expected_root_token=expected_root_token):
        token = store.root_token()
        leases = store.load_sessions()
        processes = store.load_processes()
    return token, processes, leases


def _current_authority(
    store: StateStore,
    root_token: tuple[int, int],
    expected: ManagedProcess,
    expected_lease: SessionLease | None,
) -> bool:
    try:
        lock = store.locked(expected_root_token=root_token)
        with lock:
            if store.root_token() != root_token:
                return False
            current = store.load_process(expected.wrapper.stable_key())
            if current != expected:
                return False
            if expected.owner_session_id is None:
                return expected_lease is None
            return store.load_session(expected.owner_session_id) == expected_lease
    except (FileNotFoundError, UnsafeStatePath):
        return False


def _cas_process_and_event(
    store: StateStore,
    root_token: tuple[int, int],
    expected: ManagedProcess,
    updated: ManagedProcess,
    event: dict[str, object] | None,
) -> bool:
    try:
        lock = store.locked(expected_root_token=root_token)
        with lock:
            if store.root_token() != root_token:
                return False
            current = store.load_process(expected.wrapper.stable_key())
            if current != expected:
                return False
            if updated != expected:
                store.save_process(updated, maintenance=False)
            if event is not None:
                store.append_event(event, maintenance=False)
            return True
    except (FileNotFoundError, UnsafeStatePath):
        return False


def _owner_loss_event(
    classification: Classification,
    observed_wall: str,
) -> dict[str, object]:
    process = classification.process
    return {
        "schema_version": 1,
        "event": "owner_loss_observed",
        "observed_wall": observed_wall,
        "server": process.server,
        "scope": process.scope,
        "process_key": process.wrapper.stable_key(),
        "state": "exiting",
        "reason_codes": list(classification.reason_codes),
    }


def execute_cleanup(
    actions: tuple[CleanupAction, ...],
    store: StateStore,
    procfs: LinuxProcfs,
    signaler: SignalBackend,
    clock: Clock,
    apply: bool = False,
    confirm_token: str | None = None,
) -> CleanupReport:
    if not apply:
        before = classify.build_audit(store, procfs, clock)
        before_count, before_rss_kib = _fresh_metrics(before, procfs)
        after = classify.build_audit(store, procfs, clock)
        after_count, after_rss_kib = _fresh_metrics(after, procfs)
        return CleanupReport(
            before_count=before_count,
            before_rss_kib=before_rss_kib,
            after_count=after_count,
            after_rss_kib=after_rss_kib,
            attempted=0,
            terminated=0,
            survived=0,
            skipped=0,
            outcomes=(),
            before_state_counts=before.state_counts,
            after_state_counts=after.state_counts,
            before_classifications=before.classifications,
            after_classifications=after.classifications,
        )

    has_force = any(action.force for action in actions)
    if has_force and not all(action.force for action in actions):
        raise InvalidForceConfirmation("automatic and force actions cannot be mixed")
    force_payload = None
    if has_force:
        force_payload = _decode_force_token(confirm_token, clock.boottime())
        try:
            current_boot_id = procfs.boot_id()
        except OSError as error:
            raise InvalidForceConfirmation("current boot ID is unavailable") from error
        if force_payload["boot_id"] != current_boot_id:
            raise InvalidForceConfirmation("force confirmation boot ID changed")

    root_token, stored_processes, leases = _state_snapshot(store)
    before = classify.build_audit_from_records(
        stored_processes,
        leases,
        procfs,
        clock,
    )
    before_count, before_rss_kib = _fresh_metrics(before, procfs)
    lease_by_id = {lease.session_id: lease for lease in leases}
    stored_by_key = {
        process.wrapper.stable_key(): process for process in stored_processes
    }
    current = {
        item.process.wrapper.stable_key(): item for item in before.classifications
    }
    grouped: dict[str, list[CleanupAction]] = {}
    for action in actions:
        grouped.setdefault(action.process_key, []).append(action)
    if force_payload is not None:
        _validate_current_force_actions(
            grouped,
            current,
            force_payload,
            before,
            clock.boottime(),
        )

    for process_key, classification in current.items():
        original = stored_by_key.get(process_key)
        proposed = classification.process
        if original is not None and original != proposed:
            first_loss = bool(
                original.first_owner_gone_boot is None
                and proposed.first_owner_gone_boot is not None
                and classification.state == "exiting"
            )
            if _cas_process_and_event(
                store,
                root_token,
                original,
                proposed,
                (
                    _owner_loss_event(classification, before.generated.wall_iso)
                    if first_loss
                    else None
                ),
            ):
                stored_by_key[process_key] = proposed

    outcomes: list[CleanupOutcome] = []
    attempted = 0
    for process_key, process_actions in grouped.items():
        classification = current.get(process_key)
        survived_term = False
        terminated_term = False
        indeterminate_signal = False
        term_sent_boot: float | None = None
        term_time_floor = before.generated.boottime
        term_time_valid = True
        delivered_keys: set[str] = set()
        survived_keys: set[str] = set()
        group_outcomes: list[CleanupOutcome] = []
        seen: set[str] = set()
        for action in process_actions:
            identity_key = action.identity.stable_key()
            if identity_key in seen:
                outcome = CleanupOutcome(action, "skipped", "duplicate_action")
                outcomes.append(outcome)
                group_outcomes.append(outcome)
                continue
            seen.add(identity_key)
            if action.force:
                matches = _matches_force_action(action, classification)
                signum = signal.SIGKILL
            else:
                matches = _matches_automatic_action(action, classification)
                signum = signal.SIGTERM
            if not matches:
                outcome = CleanupOutcome(action, "skipped", "classification_changed")
                outcomes.append(outcome)
                group_outcomes.append(outcome)
                continue
            expected_process = classification.process
            expected_lease = (
                None
                if expected_process.owner_session_id is None
                else lease_by_id.get(expected_process.owner_session_id)
            )
            if not _current_authority(
                store,
                root_token,
                expected_process,
                expected_lease,
            ):
                outcome = CleanupOutcome(
                    action,
                    "skipped",
                    "state_authority_changed",
                )
                outcomes.append(outcome)
                group_outcomes.append(outcome)
                continue
            outcome, was_attempted = _signal_exact(
                action,
                procfs,
                signaler,
                signum,
            )
            attempted += int(was_attempted)
            if outcome.status == "survived" and not action.force:
                observed, time_reason = _observed_boot_time(clock, term_time_floor)
                if time_reason is None:
                    assert observed is not None
                    term_sent_boot = observed
                    term_time_floor = observed
                else:
                    term_time_valid = False
                    outcome = replace(outcome, reason=time_reason)
            outcomes.append(outcome)
            group_outcomes.append(outcome)
            survived_term |= outcome.status == "survived"
            terminated_term |= outcome.status == "terminated"
            if outcome.status in {"survived", "terminated"}:
                delivered_keys.add(identity_key)
            if outcome.status == "survived":
                survived_keys.add(identity_key)
            indeterminate_signal |= outcome.reason in {
                "identity_unavailable_after_signal",
                "identity_changed_after_signal",
            }
        forced = process_actions[0].force
        current_keys = (
            set()
            if classification is None
            else {identity.stable_key() for identity in classification.live_identities}
        )
        complete_delivery = bool(current_keys) and delivered_keys == current_keys
        if (
            survived_term
            and classification is not None
            and not forced
            and complete_delivery
            and term_time_valid
            and term_sent_boot is not None
        ):
            updated = replace(
                classification.process,
                term_sent_boot=term_sent_boot,
                term_sent_keys=frozenset(survived_keys),
            )
            _cas_process_and_event(
                store,
                root_token,
                classification.process,
                updated,
                {
                    "schema_version": 1,
                    "event": "cleanup_term_sent",
                    "observed_wall": before.generated.wall_iso,
                    "server": updated.server,
                    "scope": updated.scope,
                    "process_key": process_key,
                    "state": "exiting",
                    "reason_codes": list(
                        classification.reason_codes + ("sigterm_survived",)
                    ),
                },
            )
        elif (
            survived_term
            and classification is not None
            and forced
            and complete_delivery
        ):
            _cas_process_and_event(
                store,
                root_token,
                classification.process,
                classification.process,
                {
                    "schema_version": 1,
                    "event": "cleanup_force_sent",
                    "observed_wall": before.generated.wall_iso,
                    "server": classification.process.server,
                    "scope": classification.process.scope,
                    "process_key": process_key,
                    "state": "stubborn",
                    "reason_codes": list(
                        classification.reason_codes + ("sigkill_survived",)
                    ),
                },
            )
        elif (
            terminated_term
            and classification is not None
            and _all_current_identities_terminated(
                process_key,
                classification,
                outcomes,
            )
        ):
            _cas_process_and_event(
                store,
                root_token,
                classification.process,
                classification.process,
                {
                    "schema_version": 1,
                    "event": (
                        "cleanup_force_terminated" if forced else "cleanup_terminated"
                    ),
                    "observed_wall": before.generated.wall_iso,
                    "server": classification.process.server,
                    "scope": classification.process.scope,
                    "process_key": process_key,
                    "state": "gone",
                    "reason_codes": list(
                        classification.reason_codes
                        + (
                            ("sigkill_terminated",)
                            if forced
                            else ("sigterm_terminated",)
                        )
                    ),
                },
            )
        elif (
            terminated_term
            or indeterminate_signal
            or (survived_term and not complete_delivery)
            or (survived_term and not forced and not term_time_valid)
        ) and classification is not None:
            _cas_process_and_event(
                store,
                root_token,
                classification.process,
                classification.process,
                {
                    "schema_version": 1,
                    "event": (
                        "cleanup_force_indeterminate"
                        if forced
                        else "cleanup_signal_indeterminate"
                    ),
                    "observed_wall": before.generated.wall_iso,
                    "server": classification.process.server,
                    "scope": classification.process.scope,
                    "process_key": process_key,
                    "state": "unknown",
                    "reason_codes": list(
                        classification.reason_codes
                        + (
                            (
                                "partial_signal_delivery"
                                if not complete_delivery
                                else "signal_outcome_indeterminate"
                            ),
                        )
                        + tuple(sorted({outcome.reason for outcome in group_outcomes}))
                    ),
                },
            )
    try:
        if store.root_token() != root_token:
            raise UnsafeStatePath("state root identity changed")
        after_token, after_processes, after_leases = _state_snapshot(
            store, expected_root_token=root_token
        )
        if after_token != root_token:
            raise UnsafeStatePath("state root identity changed")
    except (FileNotFoundError, UnsafeStatePath):
        after_processes = tuple(stored_by_key.values())
        after_leases = leases
    after = classify.build_audit_from_records(
        after_processes,
        after_leases,
        procfs,
        clock,
    )
    after_count, after_rss_kib = _fresh_metrics(after, procfs)
    rendered = tuple(outcomes)
    return CleanupReport(
        before_count=before_count,
        before_rss_kib=before_rss_kib,
        after_count=after_count,
        after_rss_kib=after_rss_kib,
        attempted=attempted,
        terminated=sum(item.status == "terminated" for item in rendered),
        survived=sum(item.status == "survived" for item in rendered),
        skipped=sum(item.status == "skipped" for item in rendered),
        outcomes=rendered,
        before_state_counts=before.state_counts,
        after_state_counts=after.state_counts,
        before_classifications=before.classifications,
        after_classifications=after.classifications,
    )


def _all_current_identities_terminated(
    process_key: str,
    classification: Classification,
    outcomes: list[CleanupOutcome],
) -> bool:
    current_keys = {
        identity.stable_key() for identity in classification.live_identities
    }
    terminated_keys = {
        outcome.action.identity.stable_key()
        for outcome in outcomes
        if outcome.action.process_key == process_key and outcome.status == "terminated"
    }
    return bool(current_keys) and terminated_keys == current_keys


def _canonical_json(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise InvalidForceConfirmation(
            "force confirmation is not canonical JSON"
        ) from error
    return rendered.encode("utf-8")


def _finite_boot_time(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidForceConfirmation(f"{field} must be a finite boot time")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise InvalidForceConfirmation(f"{field} must be a finite boot time")
    return converted


def _observed_boot_time(
    clock: Clock,
    floor: float,
) -> tuple[float | None, str | None]:
    try:
        observed = clock.boottime()
    except (OSError, ValueError):
        return None, "post_signal_time_unavailable"
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return None, "post_signal_time_unavailable"
    converted = float(observed)
    if not math.isfinite(converted) or converted < 0:
        return None, "post_signal_time_unavailable"
    if converted < floor:
        return None, "post_signal_time_regressed"
    return converted, None


def _decode_force_token(
    token: str | None,
    now_boot_value: object,
) -> dict[str, object]:
    if not isinstance(token, str) or token.count(".") != 1:
        raise InvalidForceConfirmation("force confirmation is required")
    encoded, provided_digest = token.split(".")
    if not encoded or len(provided_digest) != 64:
        raise InvalidForceConfirmation("invalid force confirmation framing")
    try:
        int(provided_digest, 16)
        canonical = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as error:
        raise InvalidForceConfirmation("invalid force confirmation encoding") from error
    expected_digest = hashlib.sha256(canonical).hexdigest()
    if not hmac.compare_digest(provided_digest, expected_digest):
        raise InvalidForceConfirmation("force confirmation digest mismatch")
    try:
        payload = json.loads(canonical.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidForceConfirmation("invalid force confirmation payload") from error
    if not isinstance(payload, dict) or set(payload) != _FORCE_TOKEN_KEYS:
        raise InvalidForceConfirmation("invalid force confirmation schema")
    if _canonical_json(payload) != canonical:
        raise InvalidForceConfirmation("force confirmation is not canonical")
    canonical_encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(encoded, canonical_encoded):
        raise InvalidForceConfirmation("force confirmation encoding is not canonical")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise InvalidForceConfirmation("unsupported force confirmation schema")
    boot_id = payload["boot_id"]
    if not isinstance(boot_id, str) or not boot_id:
        raise InvalidForceConfirmation("invalid force confirmation boot ID")
    identity_keys = payload["identity_keys"]
    if (
        not isinstance(identity_keys, list)
        or not identity_keys
        or not all(isinstance(key, str) and len(key) == 64 for key in identity_keys)
        or any(_not_hex_digest(key) for key in identity_keys)
        or identity_keys != sorted(set(identity_keys))
    ):
        raise InvalidForceConfirmation("invalid force confirmation identities")
    reason_codes = payload["reason_codes"]
    if not isinstance(reason_codes, list) or not all(
        isinstance(reason, str) for reason in reason_codes
    ):
        raise InvalidForceConfirmation("invalid force confirmation reasons")
    issued_boot = _finite_boot_time(payload["issued_boot"], "issued_boot")
    expires_boot = _finite_boot_time(payload["expires_boot"], "expires_boot")
    term_sent_boot = _finite_boot_time(payload["term_sent_boot"], "term_sent_boot")
    now_boot = _finite_boot_time(now_boot_value, "current_boot")
    if expires_boot != issued_boot + FORCE_TOKEN_TTL_SECONDS:
        raise InvalidForceConfirmation("invalid force confirmation window")
    if now_boot < issued_boot or now_boot > expires_boot:
        raise InvalidForceConfirmation("force confirmation expired or not yet valid")
    if term_sent_boot > issued_boot:
        raise InvalidForceConfirmation("force confirmation predates TERM evidence")
    return payload


def _not_hex_digest(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return True
    return False


def _force_evidence(
    classification: Classification,
) -> tuple[str, list[str], list[str], float]:
    if not _has_exact_term_evidence(classification):
        raise InvalidForceConfirmation("current stubborn TERM evidence is incomplete")
    identities = list(classification.live_identities)
    boot_ids = {identity.boot_id for identity in identities}
    if not identities or len(boot_ids) != 1:
        raise InvalidForceConfirmation("current stubborn evidence is incomplete")
    return (
        next(iter(boot_ids)),
        sorted(identity.stable_key() for identity in identities),
        list(classification.reason_codes),
        _finite_boot_time(classification.process.term_sent_boot, "term_sent_boot"),
    )


def _validate_current_force_actions(
    grouped: dict[str, list[CleanupAction]],
    current: dict[str, Classification],
    payload: dict[str, object],
    snapshot: AuditSnapshot,
    now_boot_value: object,
) -> None:
    now_boot = _finite_boot_time(now_boot_value, "current_boot")
    issued_boot = _finite_boot_time(payload["issued_boot"], "issued_boot")
    expires_boot = _finite_boot_time(payload["expires_boot"], "expires_boot")
    if now_boot < issued_boot or now_boot > expires_boot:
        raise InvalidForceConfirmation("force confirmation expired or not yet valid")
    if len(grouped) != 1:
        raise InvalidForceConfirmation(
            "one force confirmation covers one classification"
        )
    process_key, actions = next(iter(grouped.items()))
    classification = current.get(process_key)
    if classification is None or classification.state != "stubborn":
        raise InvalidForceConfirmation("current classification is not stubborn")
    if not actions or not all(
        _matches_force_action(action, classification) for action in actions
    ):
        raise InvalidForceConfirmation("force action does not match current evidence")
    boot_id, identity_keys, reason_codes, term_sent_boot = _force_evidence(
        classification
    )
    if (
        payload["boot_id"] != snapshot.generated.boot_id
        or payload["boot_id"] != boot_id
        or payload["identity_keys"] != identity_keys
        or payload["reason_codes"] != reason_codes
        or payload["term_sent_boot"] != term_sent_boot
    ):
        raise InvalidForceConfirmation("force confirmation evidence changed")


def _fresh_metrics(
    snapshot: AuditSnapshot,
    procfs: LinuxProcfs,
) -> tuple[int, int]:
    identities: dict[str, ProcessIdentity] = {}
    for classification in snapshot.classifications:
        process = classification.process
        recorded = (process.wrapper,) + (
            () if process.child is None else (process.child,)
        )
        recorded += process.members
        for identity in recorded:
            identities[identity.stable_key()] = identity
    return _fresh_identity_metrics(tuple(identities.values()), procfs)


def _fresh_identity_metrics(
    identities: tuple[ProcessIdentity, ...],
    procfs: LinuxProcfs,
) -> tuple[int, int]:
    count = 0
    total_rss_kib = 0
    for identity in sorted(identities, key=lambda item: item.stable_key()):
        before = _observe_identity(procfs, identity.pid)
        if before.kind != "live" or before.identity != identity:
            continue
        try:
            rss_kib = procfs.rss_kib(identity)
        except (OSError, ValueError):
            continue
        after = _observe_identity(procfs, identity.pid)
        if (
            isinstance(rss_kib, bool)
            or not isinstance(rss_kib, int)
            or rss_kib < 0
            or after.kind != "live"
            or after.identity != identity
        ):
            continue
        count += 1
        total_rss_kib += rss_kib
    return count, total_rss_kib


def _matches_automatic_action(
    action: CleanupAction,
    classification: Classification | None,
) -> bool:
    return bool(
        classification is not None
        and not action.force
        and action.classification_state == "orphan"
        and classification.state == "orphan"
        and classification.eligible_term
        and action.reason_codes == classification.reason_codes
        and action.identity in classification.live_identities
    )


def _matches_force_action(
    action: CleanupAction,
    classification: Classification | None,
) -> bool:
    return bool(
        classification is not None
        and action.force
        and action.classification_state == "stubborn"
        and _has_exact_term_evidence(classification)
        and action.reason_codes == classification.reason_codes
        and action.identity in classification.live_identities
    )


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
    identity_reader = getattr(procfs, "identity", None)
    if not callable(identity_reader):
        return IdentityObservation("unavailable", None)
    try:
        identity = identity_reader(pid)
    except (OSError, ValueError):
        return IdentityObservation("unavailable", None)
    if not isinstance(identity, ProcessIdentity):
        return IdentityObservation("unavailable", None)
    return IdentityObservation("live", identity)


def _mismatch_reason(
    observation: IdentityObservation,
    *,
    stage: str,
) -> str | None:
    if observation.kind == "live" and observation.identity is not None:
        return None
    suffix = "" if stage == "before_pidfd" else f"_{stage}"
    if observation.kind == "missing":
        return f"identity_gone{suffix}"
    return f"identity_unavailable{suffix}"


def _signal_exact(
    action: CleanupAction,
    procfs: LinuxProcfs,
    signaler: SignalBackend,
    signum: int,
) -> tuple[CleanupOutcome, bool]:
    before = _observe_identity(procfs, action.identity.pid)
    reason = _mismatch_reason(before, stage="before_pidfd")
    if reason is not None:
        return CleanupOutcome(action, "skipped", reason), False
    if before.identity != action.identity:
        return CleanupOutcome(action, "skipped", "identity_changed"), False
    try:
        pidfd = signaler.open(action.identity)
    except (PidfdUnavailable, OSError):
        return CleanupOutcome(action, "skipped", "pidfd_unavailable"), False
    attempted = False
    sent = False
    pending: CleanupOutcome | None = None
    close_failed = False
    try:
        after_open = _observe_identity(procfs, action.identity.pid)
        reason = _mismatch_reason(after_open, stage="after_pidfd")
        if reason is not None:
            pending = CleanupOutcome(action, "skipped", reason)
        elif after_open.identity != action.identity:
            pending = CleanupOutcome(
                action,
                "skipped",
                "identity_changed_after_pidfd",
            )
        else:
            attempted = True
            try:
                signaler.send(pidfd, signum)
                sent = True
            except OSError:
                pending = CleanupOutcome(action, "skipped", "signal_failed")
    finally:
        try:
            signaler.close(pidfd)
        except OSError:
            close_failed = True

    if pending is not None:
        return pending, attempted
    if not sent:
        return CleanupOutcome(action, "skipped", "signal_failed"), attempted

    after_signal = _observe_identity(procfs, action.identity.pid)
    close_suffix = "_pidfd_close_failed" if close_failed else ""
    if after_signal.kind == "missing":
        reason = "sigkill_terminated" if action.force else "sigterm_terminated"
        return CleanupOutcome(action, "terminated", reason + close_suffix), attempted
    if after_signal.kind != "live" or after_signal.identity is None:
        return CleanupOutcome(
            action, "skipped", "identity_unavailable_after_signal"
        ), attempted
    if after_signal.identity != action.identity:
        return CleanupOutcome(
            action, "skipped", "identity_changed_after_signal"
        ), attempted
    reason = "sigkill_survived" if action.force else "sigterm_survived"
    return CleanupOutcome(action, "survived", reason + close_suffix), attempted
