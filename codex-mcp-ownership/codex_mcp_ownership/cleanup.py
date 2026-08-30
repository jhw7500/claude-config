from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import math
import os
import signal
import time
from typing import Callable, Protocol

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
    SignalIntent,
    lease_generation_digest,
)
from .procfs import IdentityObservation, LinuxProcfs
from .state import (
    OperationDeadlineExceeded,
    RootBinding,
    StateStore,
    UnsafeStatePath,
)


SHUTDOWN_GRACE_SECONDS = 10.0
FORCE_TOKEN_TTL_SECONDS = 300.0
_FORCE_TOKEN_SCHEMA_VERSION = 1
_FORCE_TOKEN_MAX_ENCODED_BYTES = 16_384
_FORCE_TOKEN_MAX_DECODED_BYTES = 8_192
_FORCE_TOKEN_MAX_DEPTH = 64
_FORCE_TOKEN_MAX_NODES = 10_000
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


CleanupDeadlineExceeded = OperationDeadlineExceeded


@dataclass(frozen=True)
class AuthorizedAudit:
    root_binding: RootBinding
    revision: int
    sessions_digest: str
    processes: tuple[ManagedProcess, ...]
    leases: tuple[SessionLease, ...]
    term_intents: tuple[SignalIntent, ...]
    force_intents: tuple[SignalIntent, ...]
    snapshot: AuditSnapshot

    @property
    def root_token(self) -> tuple[int, int]:
        return self.root_binding.root_token


def _deadline_check(
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise CleanupDeadlineExceeded("cleanup deadline exhausted")


def _remaining_timeout(
    deadline: float | None,
    monotonic: Callable[[], float],
) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - monotonic())


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


def _sessions_digest(leases: tuple[SessionLease, ...]) -> str:
    payload = [
        lease.to_dict() for lease in sorted(leases, key=lambda item: item.session_id)
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def capture_authorized_audit(
    store: StateStore,
    procfs: LinuxProcfs,
    clock: Clock,
    *,
    expected_root_binding: RootBinding,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> AuthorizedAudit:
    if not isinstance(expected_root_binding, RootBinding):
        raise TypeError("initial expected root binding is required")
    _deadline_check(deadline, monotonic)
    with store.locked(
        expected_root_token=expected_root_binding.root_token,
        remaining_timeout=_remaining_timeout(deadline, monotonic),
    ):
        store.validate_root_binding(expected_root_binding)
        store._recover_before_write_locked(deadline, monotonic)
        revision = store.ledger_revision()
        leases = store.load_sessions(deadline=deadline, monotonic=monotonic)
        processes = store.load_raw_processes(
            deadline=deadline,
            monotonic=monotonic,
        )
        term_intents = store.load_signal_intents(
            "term",
            deadline=deadline,
            monotonic=monotonic,
        )
        force_intents = store.load_signal_intents(
            "force",
            deadline=deadline,
            monotonic=monotonic,
        )
        overlaid = tuple(store._overlay_signal_intent(item) for item in processes)
    _deadline_check(deadline, monotonic)
    snapshot = classify.build_audit_from_records(
        overlaid,
        leases,
        procfs,
        clock,
        deadline=deadline,
        monotonic=monotonic,
    )
    _deadline_check(deadline, monotonic)
    with store.locked(
        expected_root_token=expected_root_binding.root_token,
        remaining_timeout=_remaining_timeout(deadline, monotonic),
    ):
        store.validate_root_binding(expected_root_binding)
        if (
            store.ledger_revision() != revision
            or store.load_sessions(deadline=deadline, monotonic=monotonic) != leases
            or store.load_raw_processes(
                deadline=deadline,
                monotonic=monotonic,
            )
            != processes
            or store.load_signal_intents(
                "term",
                deadline=deadline,
                monotonic=monotonic,
            )
            != term_intents
            or store.load_signal_intents(
                "force",
                deadline=deadline,
                monotonic=monotonic,
            )
            != force_intents
        ):
            raise UnsafeStatePath("authorized audit changed during capture")
    return AuthorizedAudit(
        expected_root_binding,
        revision,
        _sessions_digest(leases),
        processes,
        leases,
        term_intents,
        force_intents,
        snapshot,
    )


def _cas_process_and_event(
    store: StateStore,
    authority: AuthorizedAudit,
    expected_revision: int,
    expected: ManagedProcess,
    expected_lease: SessionLease | None,
    updated: ManagedProcess,
    event: dict[str, object],
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int | None:
    del expected_lease
    try:
        return store.transition(
            "processes",
            expected.wrapper.stable_key(),
            expected,
            updated,
            event,
            expected_revision=expected_revision,
            expected_sessions_digest=authority.sessions_digest,
            expected_root_binding=authority.root_binding,
            deadline=deadline,
            monotonic=monotonic,
        )
    except (FileNotFoundError, TimeoutError, UnsafeStatePath, CleanupDeadlineExceeded):
        return None


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


def _persist_intent_status(
    store: StateStore,
    authority: AuthorizedAudit,
    expected_revision: int,
    expected: SignalIntent,
    updated: SignalIntent,
    event: dict[str, object],
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int | None:
    try:
        return store.transition(
            "force-receipts" if expected.action == "force" else "signal-intents",
            expected.process_key,
            expected,
            updated,
            event,
            expected_revision=expected_revision,
            expected_sessions_digest=authority.sessions_digest,
            expected_root_binding=authority.root_binding,
            deadline=deadline,
            monotonic=monotonic,
        )
    except (FileNotFoundError, TimeoutError, UnsafeStatePath, CleanupDeadlineExceeded):
        return None


def _replace_group_outcome_reason(
    outcomes: list[CleanupOutcome],
    group_outcomes: list[CleanupOutcome],
    reason: str,
) -> None:
    if not group_outcomes:
        return
    previous = group_outcomes[-1]
    updated = replace(previous, reason=reason)
    group_outcomes[-1] = updated
    for index in range(len(outcomes) - 1, -1, -1):
        if outcomes[index] is previous:
            outcomes[index] = updated
            break


def _cleanup_event(
    classification: Classification,
    observed_wall: str,
    name: str,
    state: str,
    reason_codes: tuple[str, ...],
) -> dict[str, object]:
    process = classification.process
    return {
        "schema_version": 1,
        "event": name,
        "observed_wall": observed_wall,
        "server": process.server,
        "scope": process.scope,
        "process_key": process.wrapper.stable_key(),
        "state": state,
        "reason_codes": list(reason_codes),
    }


def _authorized_lease(
    process: ManagedProcess,
    leases: dict[str, SessionLease],
) -> SessionLease | None:
    if process.owner_session_id is None:
        return None
    return leases.get(process.owner_session_id)


def _validate_process_authority_locked(
    store: StateStore,
    process: ManagedProcess,
    lease: SessionLease | None,
) -> None:
    current = store.load_raw_process(process.wrapper.stable_key())
    if current != process:
        raise UnsafeStatePath("authorized process generation changed")
    if process.owner_session_id is None:
        if lease is not None:
            raise UnsafeStatePath("authorized lease changed")
        return
    current_lease = store.load_session(process.owner_session_id)
    if (
        current_lease != lease
        or current_lease is None
        or process.owner_generation != lease_generation_digest(current_lease)
    ):
        raise UnsafeStatePath("authorized lease generation changed")


def _validate_force_delivery(
    payload: dict[str, object],
    classification: Classification,
    clock: Clock,
) -> None:
    now_boot = _finite_boot_time(clock.boottime(), "current_boot")
    issued = _finite_boot_time(payload["issued_boot"], "issued_boot")
    expires = _finite_boot_time(payload["expires_boot"], "expires_boot")
    if now_boot < issued or now_boot > expires:
        raise InvalidForceConfirmation("force confirmation expired or not yet valid")
    boot_id, keys, reasons, term_sent = _force_evidence(classification)
    if (
        payload["boot_id"] != boot_id
        or payload["identity_keys"] != keys
        or payload["reason_codes"] != reasons
        or payload["term_sent_boot"] != term_sent
    ):
        raise InvalidForceConfirmation("force confirmation evidence changed")


def _transition_intent(
    store: StateStore,
    authority: AuthorizedAudit,
    expected_revision: int,
    expected: SignalIntent | None,
    updated: SignalIntent,
    event: dict[str, object],
    *,
    process: ManagedProcess,
    lease: SessionLease | None,
    deadline: float | None,
    monotonic: Callable[[], float],
    before_effect: Callable[[], None] | None = None,
    effect: Callable[[], None] | None = None,
) -> int:
    def final_precondition() -> None:
        _validate_process_authority_locked(store, process, lease)
        if before_effect is not None:
            before_effect()

    return store.transition(
        "force-receipts" if updated.action == "force" else "signal-intents",
        updated.process_key,
        expected,
        updated,
        event,
        expected_revision=expected_revision,
        expected_sessions_digest=authority.sessions_digest,
        expected_root_binding=authority.root_binding,
        deadline=deadline,
        monotonic=monotonic,
        before_effect=final_precondition,
        effect=effect,
    )


def _intent_for_actions(
    process: ManagedProcess,
    classification: Classification,
    force: bool,
) -> SignalIntent:
    identity_keys = tuple(
        sorted(identity.stable_key() for identity in classification.live_identities)
    )
    return SignalIntent(
        1,
        process.wrapper.stable_key(),
        process.owner_generation or ("0" * 64),
        identity_keys,
        "force" if force else "term",
        "pending",
        (),
    )


def _capture_final_audit(
    store: StateStore,
    procfs: LinuxProcfs,
    clock: Clock,
    authority: AuthorizedAudit,
    expected_revision: int,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> AuditSnapshot:
    _deadline_check(deadline, monotonic)
    with store.locked(
        expected_root_token=authority.root_token,
        remaining_timeout=_remaining_timeout(deadline, monotonic),
    ):
        store.validate_root_binding(authority.root_binding)
        store._recover_before_write_locked(deadline, monotonic)
        if store.ledger_revision() != expected_revision:
            raise UnsafeStatePath("cleanup lineage revision changed")
        if (
            store.sessions_digest(deadline=deadline, monotonic=monotonic)
            != authority.sessions_digest
        ):
            raise UnsafeStatePath("cleanup session set changed")
        leases = store.load_sessions(deadline=deadline, monotonic=monotonic)
        raw_processes = store.load_raw_processes(
            deadline=deadline,
            monotonic=monotonic,
        )
        processes = tuple(store._overlay_signal_intent(item) for item in raw_processes)
    _deadline_check(deadline, monotonic)
    return classify.build_audit_from_records(
        processes,
        leases,
        procfs,
        clock,
        deadline=deadline,
        monotonic=monotonic,
    )


def _unavailable_report(
    before: AuditSnapshot,
    before_count: int,
    before_rss_kib: int,
    outcomes: list[CleanupOutcome],
    attempted: int,
    *,
    authority_lost: bool,
    partial_force: bool,
) -> CleanupReport:
    rendered = tuple(outcomes)
    return CleanupReport(
        before_count=before_count,
        before_rss_kib=before_rss_kib,
        after_count=0,
        after_rss_kib=0,
        attempted=attempted,
        terminated=sum(item.status == "terminated" for item in rendered),
        survived=sum(item.status == "survived" for item in rendered),
        skipped=sum(item.status == "skipped" for item in rendered),
        outcomes=rendered,
        before_state_counts=before.state_counts,
        after_state_counts=(),
        before_classifications=before.classifications,
        after_classifications=(),
        after_state_available=False,
        authority_lost=authority_lost,
        partial_force=partial_force,
    )


def _verify_authorized_audit(
    store: StateStore,
    authority: AuthorizedAudit,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    _deadline_check(deadline, monotonic)
    with store.locked(
        expected_root_token=authority.root_token,
        remaining_timeout=_remaining_timeout(deadline, monotonic),
    ):
        store.validate_root_binding(authority.root_binding)
        store._recover_before_write_locked(deadline, monotonic)
        if (
            store.ledger_revision() != authority.revision
            or store.sessions_digest(deadline=deadline, monotonic=monotonic)
            != authority.sessions_digest
            or store.load_sessions(deadline=deadline, monotonic=monotonic)
            != authority.leases
            or store.load_raw_processes(deadline=deadline, monotonic=monotonic)
            != authority.processes
            or store.load_signal_intents("term", deadline=deadline, monotonic=monotonic)
            != authority.term_intents
            or store.load_signal_intents(
                "force", deadline=deadline, monotonic=monotonic
            )
            != authority.force_intents
        ):
            raise UnsafeStatePath("cleanup authority changed after audit")


def _execute_cleanup_protocol(
    actions: tuple[CleanupAction, ...],
    store: StateStore,
    procfs: LinuxProcfs,
    signaler: SignalBackend,
    clock: Clock,
    *,
    confirm_token: str | None,
    deadline: float | None,
    monotonic: Callable[[], float],
    authority: AuthorizedAudit | None,
) -> CleanupReport:
    _deadline_check(deadline, monotonic)
    if authority is None:
        initial_binding = store.root_binding()
        _deadline_check(deadline, monotonic)
        authority = capture_authorized_audit(
            store,
            procfs,
            clock,
            expected_root_binding=initial_binding,
            deadline=deadline,
            monotonic=monotonic,
        )
    else:
        _verify_authorized_audit(store, authority, deadline, monotonic)

    before = authority.snapshot
    before_count, before_rss_kib = _fresh_metrics(
        before,
        procfs,
        deadline,
        monotonic,
    )
    _deadline_check(deadline, monotonic)
    has_force = any(action.force for action in actions)
    if has_force and not all(action.force for action in actions):
        raise InvalidForceConfirmation("automatic and force actions cannot be mixed")
    force_payload: dict[str, object] | None = None
    if has_force:
        force_payload = _decode_force_token(confirm_token, clock.boottime())
        _deadline_check(deadline, monotonic)
        try:
            current_boot_id = procfs.boot_id()
        except OSError as error:
            raise InvalidForceConfirmation("current boot ID is unavailable") from error
        _deadline_check(deadline, monotonic)
        if force_payload["boot_id"] != current_boot_id:
            raise InvalidForceConfirmation("force confirmation boot ID changed")

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
        _deadline_check(deadline, monotonic)

    revision = authority.revision
    raw_by_key = {
        process.wrapper.stable_key(): process for process in authority.processes
    }
    leases = {lease.session_id: lease for lease in authority.leases}
    authority_lost = False
    for process_key, classification in current.items():
        _deadline_check(deadline, monotonic)
        raw = raw_by_key.get(process_key)
        if raw is None:
            continue
        first_loss = bool(
            raw.first_owner_gone_boot is None
            and classification.process.first_owner_gone_boot is not None
            and classification.state == "exiting"
        )
        if not first_loss:
            continue
        updated = replace(
            raw,
            first_owner_gone_boot=classification.process.first_owner_gone_boot,
        )
        next_revision = _cas_process_and_event(
            store,
            authority,
            revision,
            raw,
            _authorized_lease(raw, leases),
            updated,
            _owner_loss_event(classification, before.generated.wall_iso),
            deadline,
            monotonic,
        )
        if next_revision is None:
            authority_lost = True
            return _unavailable_report(
                before,
                before_count,
                before_rss_kib,
                [],
                0,
                authority_lost=True,
                partial_force=False,
            )
        revision = next_revision
        raw_by_key[process_key] = updated
        _deadline_check(deadline, monotonic)

    outcomes: list[CleanupOutcome] = []
    attempted = 0
    partial_force = False
    deadline_expired = False
    stop_all = False
    initial_term_intents = {
        intent.process_key: intent for intent in authority.term_intents
    }
    initial_force_intents = {
        intent.process_key: intent for intent in authority.force_intents
    }

    for process_key, process_actions in grouped.items():
        if stop_all:
            for action in process_actions:
                outcomes.append(
                    CleanupOutcome(action, "skipped", "state_authority_changed")
                )
            continue
        classification = current.get(process_key)
        raw = raw_by_key.get(process_key)
        forced = bool(process_actions and process_actions[0].force)
        if classification is None or raw is None:
            outcomes.extend(
                CleanupOutcome(action, "skipped", "classification_changed")
                for action in process_actions
            )
            continue
        lease = _authorized_lease(raw, leases)
        intent = (
            initial_force_intents.get(process_key)
            if forced
            else initial_term_intents.get(process_key)
        )
        created_intent = False
        delivered_keys: set[str] = set()
        group_outcomes: list[CleanupOutcome] = []
        term_sent_boot: float | None = None
        term_time_floor = before.generated.boottime
        term_time_valid = True

        for index, action in enumerate(process_actions):
            if force_payload is not None:
                try:
                    _validate_force_delivery(force_payload, classification, clock)
                except InvalidForceConfirmation:
                    if delivered_keys:
                        partial_force = True
                        for remaining in process_actions[index:]:
                            outcome = CleanupOutcome(
                                remaining,
                                "skipped",
                                "partial_force_authority_expired",
                            )
                            outcomes.append(outcome)
                            group_outcomes.append(outcome)
                        break
                    raise
            try:
                _deadline_check(deadline, monotonic)
            except CleanupDeadlineExceeded:
                if forced and delivered_keys:
                    partial_force = True
                    deadline_expired = True
                    for remaining in process_actions[index:]:
                        outcome = CleanupOutcome(
                            remaining,
                            "skipped",
                            "partial_force_deadline_exhausted",
                        )
                        outcomes.append(outcome)
                        group_outcomes.append(outcome)
                    break
                raise
            matches = (
                _matches_force_action(action, classification)
                if forced
                else _matches_automatic_action(action, classification)
            )
            if not matches:
                outcome = CleanupOutcome(action, "skipped", "classification_changed")
                outcomes.append(outcome)
                group_outcomes.append(outcome)
                continue
            pidfd, prepared_outcome = _prepare_exact_signal(
                action,
                procfs,
                signaler,
                deadline,
                monotonic,
            )
            if prepared_outcome is not None:
                outcomes.append(prepared_outcome)
                group_outcomes.append(prepared_outcome)
                if forced:
                    partial_force = bool(delivered_keys)
                    for remaining in process_actions[index + 1 :]:
                        skipped = CleanupOutcome(
                            remaining,
                            "skipped",
                            (
                                "partial_force_identity_failure"
                                if delivered_keys
                                else "force_confirmation_invalidated"
                            ),
                        )
                        outcomes.append(skipped)
                        group_outcomes.append(skipped)
                    break
                continue
            assert pidfd is not None
            close_failed = False
            try:
                if not created_intent:
                    proposed_intent = _intent_for_actions(
                        raw,
                        classification,
                        forced,
                    )
                    same_intent_authority = bool(
                        intent is not None
                        and intent.process_key == proposed_intent.process_key
                        and intent.owner_generation == proposed_intent.owner_generation
                        and intent.identity_keys == proposed_intent.identity_keys
                        and intent.action == proposed_intent.action
                    )
                    if same_intent_authority:
                        outcome = CleanupOutcome(
                            action,
                            "skipped",
                            "signal_receipt_blocks_repeat",
                        )
                        outcomes.append(outcome)
                        group_outcomes.append(outcome)
                        continue
                    previous_intent = intent
                    intent = proposed_intent
                    revision = _transition_intent(
                        store,
                        authority,
                        revision,
                        previous_intent,
                        intent,
                        _cleanup_event(
                            classification,
                            before.generated.wall_iso,
                            "cleanup_force_pending"
                            if forced
                            else "cleanup_term_pending",
                            "stubborn" if forced else "exiting",
                            classification.reason_codes + ("signal_intent_pending",),
                        ),
                        process=raw,
                        lease=lease,
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                    created_intent = True
                assert intent is not None
                delivered = tuple(
                    sorted(set(intent.delivered_keys) | {action.identity.stable_key()})
                )
                delivered_intent = replace(intent, delivered_keys=delivered)

                def before_send() -> None:
                    if force_payload is not None:
                        _validate_force_delivery(
                            force_payload,
                            classification,
                            clock,
                        )

                try:
                    revision = _transition_intent(
                        store,
                        authority,
                        revision,
                        intent,
                        delivered_intent,
                        _cleanup_event(
                            classification,
                            before.generated.wall_iso,
                            (
                                "cleanup_force_partial"
                                if forced and not intent.delivered_keys
                                else "cleanup_force_delivery_receipt"
                                if forced
                                else "cleanup_term_delivery_receipt"
                            ),
                            "stubborn" if forced else "exiting",
                            classification.reason_codes + ("signal_delivered",),
                        ),
                        process=raw,
                        lease=lease,
                        deadline=deadline,
                        monotonic=monotonic,
                        before_effect=before_send,
                        effect=lambda: signaler.send(
                            pidfd,
                            signal.SIGKILL if forced else signal.SIGTERM,
                        ),
                    )
                except InvalidForceConfirmation:
                    if delivered_keys:
                        partial_force = True
                        outcome = CleanupOutcome(
                            action,
                            "skipped",
                            "partial_force_authority_expired",
                        )
                        outcomes.append(outcome)
                        group_outcomes.append(outcome)
                        for remaining in process_actions[index + 1 :]:
                            skipped = CleanupOutcome(
                                remaining,
                                "skipped",
                                "partial_force_authority_expired",
                            )
                            outcomes.append(skipped)
                            group_outcomes.append(skipped)
                        break
                    raise
                except OSError:
                    attempted += 1
                    outcome = CleanupOutcome(action, "skipped", "signal_failed")
                    outcomes.append(outcome)
                    group_outcomes.append(outcome)
                    if forced:
                        partial_force = bool(delivered_keys)
                        for remaining in process_actions[index + 1 :]:
                            skipped = CleanupOutcome(
                                remaining,
                                "skipped",
                                (
                                    "partial_force_signal_failure"
                                    if delivered_keys
                                    else "force_signal_failure"
                                ),
                            )
                            outcomes.append(skipped)
                            group_outcomes.append(skipped)
                        break
                    continue
                except (FileNotFoundError, TimeoutError, UnsafeStatePath):
                    authority_lost = True
                    outcome = CleanupOutcome(
                        action,
                        "skipped",
                        "state_authority_changed",
                    )
                    outcomes.append(outcome)
                    group_outcomes.append(outcome)
                    if forced and delivered_keys:
                        partial_force = True
                    for remaining in process_actions[index + 1 :]:
                        skipped = CleanupOutcome(
                            remaining,
                            "skipped",
                            (
                                "partial_force_authority_changed"
                                if forced and delivered_keys
                                else "state_authority_changed"
                            ),
                        )
                        outcomes.append(skipped)
                        group_outcomes.append(skipped)
                    stop_all = True
                    break
                attempted += 1
                intent = delivered_intent
                delivered_keys.add(action.identity.stable_key())
                if deadline is not None and monotonic() >= deadline:
                    outcome = CleanupOutcome(
                        action,
                        "survived",
                        "signal_delivered_unobserved_deadline",
                    )
                    outcomes.append(outcome)
                    group_outcomes.append(outcome)
                    deadline_expired = True
                    if forced:
                        partial_force = True
                    if forced and index + 1 < len(process_actions):
                        for remaining in process_actions[index + 1 :]:
                            skipped = CleanupOutcome(
                                remaining,
                                "skipped",
                                "partial_force_deadline_exhausted",
                            )
                            outcomes.append(skipped)
                            group_outcomes.append(skipped)
                    break
                try:
                    outcome = _post_signal_outcome(
                        action,
                        procfs,
                        close_failed,
                        deadline,
                        monotonic,
                    )
                except CleanupDeadlineExceeded:
                    outcome = CleanupOutcome(
                        action,
                        "survived",
                        "signal_delivered_unobserved_deadline",
                    )
                    outcomes.append(outcome)
                    group_outcomes.append(outcome)
                    deadline_expired = True
                    if forced:
                        partial_force = True
                    for remaining in process_actions[index + 1 :]:
                        skipped = CleanupOutcome(
                            remaining,
                            "skipped",
                            (
                                "partial_force_deadline_exhausted"
                                if forced
                                else "cleanup_deadline_exhausted"
                            ),
                        )
                        outcomes.append(skipped)
                        group_outcomes.append(skipped)
                    break
                if outcome.status == "survived" and not forced:
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
                if forced and outcome.status == "skipped":
                    partial_force = True
                    for remaining in process_actions[index + 1 :]:
                        skipped = CleanupOutcome(
                            remaining,
                            "skipped",
                            "partial_force_identity_failure",
                        )
                        outcomes.append(skipped)
                        group_outcomes.append(skipped)
                    break
            except (FileNotFoundError, TimeoutError, UnsafeStatePath, OSError):
                authority_lost = True
                outcome = CleanupOutcome(
                    action,
                    "skipped",
                    "state_authority_changed",
                )
                outcomes.append(outcome)
                group_outcomes.append(outcome)
                if forced and delivered_keys:
                    partial_force = True
                for remaining in process_actions[index + 1 :]:
                    skipped = CleanupOutcome(
                        remaining,
                        "skipped",
                        (
                            "partial_force_authority_changed"
                            if forced and delivered_keys
                            else "state_authority_changed"
                        ),
                    )
                    outcomes.append(skipped)
                    group_outcomes.append(skipped)
                stop_all = True
                break
            finally:
                try:
                    signaler.close(pidfd)
                except OSError:
                    close_failed = True
                    if group_outcomes:
                        prior = group_outcomes[-1]
                        if prior.status in {"survived", "terminated"}:
                            amended = replace(
                                prior,
                                reason=prior.reason + "_pidfd_close_failed",
                            )
                            group_outcomes[-1] = amended
                            outcomes[-1] = amended

        if deadline_expired or stop_all or intent is None or not created_intent:
            continue
        authorized_keys = set(intent.identity_keys)
        complete = delivered_keys == authorized_keys
        survived = any(item.status == "survived" for item in group_outcomes)
        all_conclusive = bool(group_outcomes) and all(
            item.status in {"survived", "terminated"} for item in group_outcomes
        )
        all_terminated = bool(group_outcomes) and all(
            item.status == "terminated" for item in group_outcomes
        )
        if forced:
            if partial_force or not complete or not all_conclusive:
                status = "conflict"
                event_name = "cleanup_force_conflict"
                event_state = "unknown"
                final_reasons = ("partial_force_delivery",)
            else:
                status = "delivered"
                event_name = (
                    "cleanup_force_terminated"
                    if all_terminated
                    else "cleanup_force_sent"
                )
                event_state = "gone" if all_terminated else "stubborn"
                final_reasons = (
                    "sigkill_terminated" if all_terminated else "sigkill_survived",
                )
            final_intent = replace(intent, status=status)
        else:
            if (
                complete
                and all_conclusive
                and survived
                and term_time_valid
                and term_sent_boot is not None
            ):
                status = "delivered"
                event_name = "cleanup_term_sent"
                event_state = "exiting"
                final_reasons = ("sigterm_survived",)
                final_intent = replace(
                    intent,
                    status=status,
                    term_sent_boot=term_sent_boot,
                )
            elif complete and all_terminated:
                status = "conflict"
                event_name = "cleanup_terminated"
                event_state = "gone"
                final_reasons = ("sigterm_terminated",)
                final_intent = replace(intent, status=status)
            else:
                status = "conflict"
                event_name = "cleanup_signal_indeterminate"
                event_state = "unknown"
                final_reasons = tuple(
                    dict.fromkeys(
                        ("partial_signal_delivery",)
                        + tuple(item.reason for item in group_outcomes)
                    )
                )
                final_intent = replace(intent, status=status)
        next_revision = _persist_intent_status(
            store,
            authority,
            revision,
            intent,
            final_intent,
            _cleanup_event(
                classification,
                before.generated.wall_iso,
                event_name,
                event_state,
                classification.reason_codes + final_reasons,
            ),
            deadline,
            monotonic,
        )
        if next_revision is None:
            authority_lost = True
            if forced and delivered_keys:
                partial_force = True
            _replace_group_outcome_reason(
                outcomes,
                group_outcomes,
                "state_persistence_conflict",
            )
            stop_all = True
        else:
            revision = next_revision

    if deadline_expired:
        return _unavailable_report(
            before,
            before_count,
            before_rss_kib,
            outcomes,
            attempted,
            authority_lost=authority_lost,
            partial_force=partial_force,
        )
    try:
        after = _capture_final_audit(
            store,
            procfs,
            clock,
            authority,
            revision,
            deadline,
            monotonic,
        )
        after_count, after_rss_kib = _fresh_metrics(
            after,
            procfs,
            deadline,
            monotonic,
        )
    except (CleanupDeadlineExceeded, FileNotFoundError, UnsafeStatePath):
        return _unavailable_report(
            before,
            before_count,
            before_rss_kib,
            outcomes,
            attempted,
            authority_lost=True,
            partial_force=partial_force,
        )
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
        after_state_available=True,
        authority_lost=authority_lost,
        partial_force=partial_force,
    )


def execute_cleanup(
    actions: tuple[CleanupAction, ...],
    store: StateStore,
    procfs: LinuxProcfs,
    signaler: SignalBackend,
    clock: Clock,
    apply: bool = False,
    confirm_token: str | None = None,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    authority: AuthorizedAudit | None = None,
) -> CleanupReport:
    _deadline_check(deadline, monotonic)
    if not apply:
        before = classify.build_audit(
            store,
            procfs,
            clock,
            deadline=deadline,
            monotonic=monotonic,
        )
        _deadline_check(deadline, monotonic)
        before_count, before_rss_kib = _fresh_metrics(
            before, procfs, deadline, monotonic
        )
        _deadline_check(deadline, monotonic)
        after = classify.build_audit(
            store,
            procfs,
            clock,
            deadline=deadline,
            monotonic=monotonic,
        )
        _deadline_check(deadline, monotonic)
        after_count, after_rss_kib = _fresh_metrics(after, procfs, deadline, monotonic)
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

    return _execute_cleanup_protocol(
        actions,
        store,
        procfs,
        signaler,
        clock,
        confirm_token=confirm_token,
        deadline=deadline,
        monotonic=monotonic,
        authority=authority,
    )


def _canonical_json(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        return rendered.encode("utf-8")
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeEncodeError,
    ) as error:
        raise InvalidForceConfirmation(
            "force confirmation is not canonical JSON"
        ) from error


def _finite_boot_time(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidForceConfirmation(f"{field} must be a finite boot time")
    try:
        converted = float(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise InvalidForceConfirmation(f"{field} must be a finite boot time") from error
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
    if (
        not isinstance(token, str)
        or len(token) > _FORCE_TOKEN_MAX_ENCODED_BYTES
        or token.count(".") != 1
    ):
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
    except (ValueError, TypeError, OverflowError, binascii.Error) as error:
        raise InvalidForceConfirmation("invalid force confirmation encoding") from error
    if len(canonical) > _FORCE_TOKEN_MAX_DECODED_BYTES:
        raise InvalidForceConfirmation("invalid force confirmation payload")
    expected_digest = hashlib.sha256(canonical).hexdigest()
    if not hmac.compare_digest(provided_digest, expected_digest):
        raise InvalidForceConfirmation("force confirmation digest mismatch")
    try:
        payload = json.loads(canonical.decode("utf-8"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise InvalidForceConfirmation("invalid force confirmation payload") from error
    _validate_force_payload_resources(payload)
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


def _validate_force_payload_resources(payload: object) -> None:
    pending: list[tuple[object, int]] = [(payload, 1)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > _FORCE_TOKEN_MAX_NODES or depth > _FORCE_TOKEN_MAX_DEPTH:
            raise InvalidForceConfirmation("invalid force confirmation payload")
        if isinstance(value, dict):
            pending.extend((key, depth + 1) for key in value)
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)


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
    supplied_keys = [action.identity.stable_key() for action in actions]
    if (
        len(supplied_keys) != len(set(supplied_keys))
        or sorted(supplied_keys) != identity_keys
    ):
        raise InvalidForceConfirmation(
            "force actions must exactly equal the authorized live identities"
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
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
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
    return _fresh_identity_metrics(
        tuple(identities.values()), procfs, deadline, monotonic
    )


def _fresh_identity_metrics(
    identities: tuple[ProcessIdentity, ...],
    procfs: LinuxProcfs,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[int, int]:
    count = 0
    total_rss_kib = 0
    for identity in sorted(identities, key=lambda item: item.stable_key()):
        _deadline_check(deadline, monotonic)
        before = _observe_identity(procfs, identity.pid)
        _deadline_check(deadline, monotonic)
        if before.kind != "live" or before.identity != identity:
            continue
        try:
            _deadline_check(deadline, monotonic)
            rss_kib = procfs.rss_kib(identity)
            _deadline_check(deadline, monotonic)
        except (OSError, ValueError):
            continue
        _deadline_check(deadline, monotonic)
        after = _observe_identity(procfs, identity.pid)
        _deadline_check(deadline, monotonic)
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


def _prepare_exact_signal(
    action: CleanupAction,
    procfs: LinuxProcfs,
    signaler: SignalBackend,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[int | None, CleanupOutcome | None]:
    _deadline_check(deadline, monotonic)
    before = _observe_identity(procfs, action.identity.pid)
    _deadline_check(deadline, monotonic)
    reason = _mismatch_reason(before, stage="before_pidfd")
    if reason is not None:
        return None, CleanupOutcome(action, "skipped", reason)
    if before.identity != action.identity:
        return None, CleanupOutcome(action, "skipped", "identity_changed")
    try:
        _deadline_check(deadline, monotonic)
        pidfd = signaler.open(action.identity)
        _deadline_check(deadline, monotonic)
    except CleanupDeadlineExceeded:
        if "pidfd" in locals():
            try:
                signaler.close(pidfd)
            except OSError:
                pass
        raise
    except (PidfdUnavailable, OSError):
        return None, CleanupOutcome(action, "skipped", "pidfd_unavailable")
    _deadline_check(deadline, monotonic)
    after_open = _observe_identity(procfs, action.identity.pid)
    try:
        _deadline_check(deadline, monotonic)
    except CleanupDeadlineExceeded:
        try:
            signaler.close(pidfd)
        except OSError:
            pass
        raise
    reason = _mismatch_reason(after_open, stage="after_pidfd")
    if reason is not None or after_open.identity != action.identity:
        try:
            signaler.close(pidfd)
        except OSError:
            pass
        if reason is None:
            reason = "identity_changed_after_pidfd"
        return None, CleanupOutcome(action, "skipped", reason)
    return pidfd, None


def _post_signal_outcome(
    action: CleanupAction,
    procfs: LinuxProcfs,
    close_failed: bool,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> CleanupOutcome:
    _deadline_check(deadline, monotonic)
    after_signal = _observe_identity(procfs, action.identity.pid)
    _deadline_check(deadline, monotonic)
    close_suffix = "_pidfd_close_failed" if close_failed else ""
    if after_signal.kind == "missing":
        reason = "sigkill_terminated" if action.force else "sigterm_terminated"
        return CleanupOutcome(action, "terminated", reason + close_suffix)
    if after_signal.kind != "live" or after_signal.identity is None:
        return CleanupOutcome(action, "skipped", "identity_unavailable_after_signal")
    if after_signal.identity != action.identity:
        return CleanupOutcome(action, "skipped", "identity_changed_after_signal")
    reason = "sigkill_survived" if action.force else "sigterm_survived"
    return CleanupOutcome(action, "survived", reason + close_suffix)
