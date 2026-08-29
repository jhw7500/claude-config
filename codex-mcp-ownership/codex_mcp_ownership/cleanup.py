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
    ProcessIdentity,
)
from .procfs import IdentityObservation, LinuxProcfs
from .state import StateStore


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
        forced = force and classification.state == "stubborn"
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
    if (
        not isinstance(classification, Classification)
        or classification.state != "stubborn"
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
    expires_boot = issued_boot + FORCE_TOKEN_TTL_SECONDS
    if not math.isfinite(expires_boot):
        raise InvalidForceConfirmation("force confirmation window is invalid")
    payload = {
        "schema_version": _FORCE_TOKEN_SCHEMA_VERSION,
        "boot_id": next(iter(boot_ids)),
        "identity_keys": identity_keys,
        "reason_codes": list(classification.reason_codes),
        "issued_boot": issued_boot,
        "expires_boot": expires_boot,
    }
    canonical = _canonical_json(payload)
    encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"{encoded}.{digest}"


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
        before_count, before_rss_kib = _fresh_action_metrics(actions, procfs)
        after_count, after_rss_kib = _fresh_action_metrics(actions, procfs)
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

    outcomes: list[CleanupOutcome] = []
    attempted = 0
    with store.locked():
        before = classify.build_audit(store, procfs, clock)
        before_count, before_rss_kib = _fresh_metrics(before, procfs)
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
        for process_key, process_actions in grouped.items():
            classification = current.get(process_key)
            survived_term = False
            terminated_term = False
            indeterminate_signal = False
            term_sent_boot = before.generated.boottime
            delivered_keys: set[str] = set()
            seen: set[str] = set()
            for action in process_actions:
                identity_key = action.identity.stable_key()
                if identity_key in seen:
                    outcomes.append(
                        CleanupOutcome(action, "skipped", "duplicate_action")
                    )
                    continue
                seen.add(identity_key)
                if action.force:
                    matches = _matches_force_action(action, classification)
                    signum = signal.SIGKILL
                else:
                    matches = _matches_automatic_action(action, classification)
                    signum = signal.SIGTERM
                if not matches:
                    outcomes.append(
                        CleanupOutcome(action, "skipped", "classification_changed")
                    )
                    continue
                outcome, was_attempted = _signal_exact(
                    action,
                    procfs,
                    signaler,
                    signum,
                )
                attempted += int(was_attempted)
                outcomes.append(outcome)
                survived_term |= outcome.status == "survived"
                terminated_term |= outcome.status == "terminated"
                if outcome.status in {"survived", "terminated"}:
                    delivered_keys.add(identity_key)
                if outcome.status == "survived" and not action.force:
                    term_sent_boot = _observed_boot_time(clock, term_sent_boot)
                indeterminate_signal |= outcome.reason in {
                    "identity_unavailable_after_signal",
                    "identity_changed_after_signal",
                }
            forced = process_actions[0].force
            current_keys = (
                set()
                if classification is None
                else {
                    identity.stable_key() for identity in classification.live_identities
                }
            )
            complete_delivery = bool(current_keys) and delivered_keys == current_keys
            if (
                survived_term
                and classification is not None
                and not forced
                and complete_delivery
            ):
                updated = replace(
                    classification.process,
                    term_sent_boot=term_sent_boot,
                )
                store.save_process(updated)
                store.append_event(
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
                    }
                )
            elif survived_term and classification is not None and forced:
                store.append_event(
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
                    }
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
                store.append_event(
                    {
                        "schema_version": 1,
                        "event": (
                            "cleanup_force_terminated"
                            if forced
                            else "cleanup_terminated"
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
                    }
                )
            elif (
                terminated_term
                or indeterminate_signal
                or (survived_term and not complete_delivery)
            ) and classification is not None:
                store.append_event(
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
                            + ("identity_unavailable_after_signal",)
                        ),
                    }
                )
        after = classify.build_audit(store, procfs, clock)
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


def _observed_boot_time(clock: Clock, floor: float) -> float:
    try:
        observed = clock.boottime()
    except (OSError, ValueError):
        return floor
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return floor
    converted = float(observed)
    if not math.isfinite(converted) or converted < floor:
        return floor
    return converted


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
    now_boot = _finite_boot_time(now_boot_value, "current_boot")
    if expires_boot != issued_boot + FORCE_TOKEN_TTL_SECONDS:
        raise InvalidForceConfirmation("invalid force confirmation window")
    if now_boot < issued_boot or now_boot > expires_boot:
        raise InvalidForceConfirmation("force confirmation expired or not yet valid")
    return payload


def _not_hex_digest(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return True
    return False


def _force_evidence(classification: Classification) -> tuple[str, list[str], list[str]]:
    identities = list(classification.live_identities)
    boot_ids = {identity.boot_id for identity in identities}
    if not identities or len(boot_ids) != 1:
        raise InvalidForceConfirmation("current stubborn evidence is incomplete")
    return (
        next(iter(boot_ids)),
        sorted(identity.stable_key() for identity in identities),
        list(classification.reason_codes),
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
    boot_id, identity_keys, reason_codes = _force_evidence(classification)
    if (
        payload["boot_id"] != snapshot.generated.boot_id
        or payload["boot_id"] != boot_id
        or payload["identity_keys"] != identity_keys
        or payload["reason_codes"] != reason_codes
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


def _fresh_action_metrics(
    actions: tuple[CleanupAction, ...],
    procfs: LinuxProcfs,
) -> tuple[int, int]:
    identities = {action.identity.stable_key(): action.identity for action in actions}
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
        and classification.state == "stubborn"
        and not classification.eligible_term
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
