from __future__ import annotations

from dataclasses import replace
import math
import os
import subprocess
import time
import unicodedata

from .cleanup import (
    CleanupDeadlineExceeded,
    PidfdSignalBackend,
    execute_cleanup,
    plan_cleanup,
)
from .clock import Clock
from .classify import build_audit
from .model import ObservedTime, SessionLease, validate_session_id
from .procfs import LinuxProcfs
from .state import StateStore, session_key


_SYSTEMD_COMMAND = [
    "/usr/bin/systemctl",
    "--user",
    "start",
    "--no-block",
    "codex-mcp-ownership-cleanup.service",
]
FALLBACK_MAX_RECORDS = 64
FALLBACK_MAX_ACTIONS = 64
FALLBACK_MAX_ELAPSED_SECONDS = 0.25


class SystemdNotifier:
    def request_cleanup(self) -> bool:
        try:
            result = subprocess.run(
                _SYSTEMD_COMMAND,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
                timeout=1.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0


def _validated_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"invalid {field}")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"invalid {field}")
    return value


def _validated_payload(payload: object) -> tuple[str, str, str, str | None]:
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be an object")
    session_id = validate_session_id(payload.get("session_id"))
    cwd = _validated_text(payload.get("cwd"), "cwd", 4096)
    if not os.path.isabs(cwd):
        raise ValueError("invalid cwd")
    event = _validated_text(payload.get("hook_event_name"), "event", 32)
    if event not in {"SessionStart", "SessionEnd"}:
        raise ValueError("unsupported hook event")
    source: str | None = None
    if event == "SessionStart":
        source = _validated_text(payload.get("source"), "source", 128)
    elif "source" in payload:
        _validated_text(payload["source"], "source", 128)
    if "reason" in payload:
        _validated_text(payload["reason"], "reason", 128)
    if "transcript_path" in payload:
        _validated_text(payload["transcript_path"], "transcript_path", 4096)
    if "model" in payload:
        _validated_text(payload["model"], "model", 256)
    return session_id, cwd, event, source


def _observed(clock: Clock, boot_id: str) -> ObservedTime:
    wall = _validated_text(clock.wall_iso(), "wall clock", 128)
    validated_boot_id = _validated_text(boot_id, "boot id", 128)
    boot_time = clock.boottime()
    if (
        isinstance(boot_time, bool)
        or not isinstance(boot_time, (int, float))
        or not math.isfinite(float(boot_time))
        or float(boot_time) < 0
    ):
        raise ValueError("invalid boot time")
    observed = ObservedTime(wall, validated_boot_id, float(boot_time))
    observed.to_dict()
    return observed


def _event(name: str, lease: SessionLease) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event": name,
        "observed_wall": (
            lease.observed.wall_iso if lease.ended is None else lease.ended.wall_iso
        ),
        "process_key": session_key(lease.session_id),
        "state": lease.state,
        "reason_codes": [
            "session_started" if lease.state == "active" else "session_ended"
        ],
    }


def _same_generation(
    current: SessionLease,
    cwd: str,
    host_keys: tuple[str, ...],
    boot_id: str,
) -> bool:
    return bool(
        current.state == "active"
        and current.cwd == cwd
        and current.host_keys == host_keys
        and current.observed.boot_id == boot_id
    )


def _append_event_best_effort(
    store: StateStore,
    event: dict[str, object],
    observed_wall: str,
) -> None:
    try:
        store.append_event(event, maintenance=False)
    except Exception:
        try:
            store.append_event(
                {
                    "schema_version": 1,
                    "event": "hook_event_failed",
                    "observed_wall": observed_wall,
                    "state": "unknown",
                    "reason_codes": ["hook_event_write_failed"],
                },
                maintenance=False,
            )
        except Exception:
            pass


def _request_cleanup(notifier: SystemdNotifier) -> bool:
    try:
        return notifier.request_cleanup() is True
    except Exception:
        return False


def _opportunistic_cleanup(
    store: StateStore,
    procfs: LinuxProcfs,
    clock: Clock,
) -> None:
    try:
        _opportunistic_cleanup_bounded(store, procfs, clock)
    except CleanupDeadlineExceeded:
        _fallback_deferred(store, clock, "elapsed_budget_exhausted")


def _opportunistic_cleanup_bounded(
    store: StateStore,
    procfs: LinuxProcfs,
    clock: Clock,
) -> None:
    deadline = time.monotonic() + FALLBACK_MAX_ELAPSED_SECONDS

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise CleanupDeadlineExceeded("fallback deadline exhausted")

    record_count = 0
    for kind in ("sessions", "processes"):
        check_deadline()
        directory = store.root / kind
        try:
            for _item in directory.iterdir():
                check_deadline()
                record_count += 1
                if record_count > FALLBACK_MAX_RECORDS:
                    break
        except FileNotFoundError:
            continue
        if record_count > FALLBACK_MAX_RECORDS:
            _fallback_deferred(store, clock, "record_budget_exhausted")
            return
    check_deadline()
    snapshot = build_audit(store, procfs, clock)
    check_deadline()
    if snapshot.corrupt_count:
        return
    actions = plan_cleanup(snapshot)
    check_deadline()
    if len(actions) > FALLBACK_MAX_ACTIONS:
        _fallback_deferred(store, clock, "action_budget_exhausted")
        return
    if actions:
        check_deadline()
        signaler = PidfdSignalBackend()
    else:

        class _NoSignal:
            def open(self, identity):
                del identity
                raise AssertionError("empty fallback opened signal backend")

            def send(self, pidfd, signum):
                del pidfd, signum

            def close(self, pidfd):
                del pidfd

        signaler = _NoSignal()
    check_deadline()
    execute_cleanup(
        actions,
        store,
        procfs,
        signaler,
        clock,
        apply=True,
        deadline=deadline,
        monotonic=time.monotonic,
    )


def _fallback_deferred(store: StateStore, clock: Clock, reason: str) -> None:
    try:
        store.append_event(
            {
                "schema_version": 1,
                "event": "hook_fallback_deferred",
                "observed_wall": _validated_text(clock.wall_iso(), "wall clock", 128),
                "state": "unknown",
                "reason_codes": [reason],
            },
            maintenance=False,
        )
    except Exception:
        pass


def _hook_diagnostic(store: StateStore, clock: Clock, event: str, reason: str) -> None:
    try:
        store.append_event(
            {
                "schema_version": 1,
                "event": event,
                "observed_wall": _validated_text(clock.wall_iso(), "wall clock", 128),
                "state": "unknown",
                "reason_codes": [reason],
            },
            maintenance=False,
        )
    except Exception:
        pass


def _save_session_milestone(store: StateStore, lease: SessionLease) -> bool:
    try:
        store.save_session(lease, maintenance=False)
    except Exception:
        try:
            if store.load_session(lease.session_id) == lease:
                return True
        except Exception:
            pass
        raise
    return True


def handle_payload(
    payload: object,
    store: StateStore,
    procfs: LinuxProcfs,
    clock: Clock,
    notifier: SystemdNotifier,
) -> None:
    """Handle one lifecycle payload without allowing failures to block Codex."""
    try:
        session_id, cwd, event, source = _validated_payload(payload)
        cwd = os.path.realpath(cwd)
        if event == "SessionStart":
            parent_pid = os.getppid()
            chain = procfs.ancestor_chain(parent_pid)
            if not chain or chain[0].pid != parent_pid:
                return
            boot_ids = {identity.boot_id for identity in chain}
            if len(boot_ids) != 1:
                return
            host_keys = tuple(identity.stable_key() for identity in chain)
            candidate = SessionLease(
                schema_version=1,
                session_id=session_id,
                cwd=cwd,
                source=source or "",
                host_keys=host_keys,
                state="active",
                observed=_observed(clock, chain[0].boot_id),
            )
            candidate.to_dict()
            durable = False
            try:
                with store.locked():
                    current = store.load_session(session_id)
                    lease = (
                        replace(current, source=source or "")
                        if current is not None
                        and _same_generation(
                            current,
                            cwd,
                            host_keys,
                            chain[0].boot_id,
                        )
                        else candidate
                    )
                    durable = _save_session_milestone(store, lease)
                    _append_event_best_effort(
                        store,
                        _event("session_started", lease),
                        candidate.observed.wall_iso,
                    )
            finally:
                notified = durable and _request_cleanup(notifier)
            if durable and not notified:
                _hook_diagnostic(
                    store,
                    clock,
                    "hook_notifier_failed",
                    "notifier_request_failed",
                )
                try:
                    _opportunistic_cleanup(store, procfs, clock)
                except CleanupDeadlineExceeded:
                    _hook_diagnostic(
                        store,
                        clock,
                        "hook_fallback_deferred",
                        "elapsed_budget_exhausted",
                    )
                except Exception:
                    _hook_diagnostic(
                        store,
                        clock,
                        "hook_fallback_failed",
                        "fallback_execution_failed",
                    )
            return

        parent_pid = os.getppid()
        chain = procfs.ancestor_chain(parent_pid)
        if not chain or chain[0].pid != parent_pid:
            return
        host_keys = tuple(identity.stable_key() for identity in chain)
        durable = False
        try:
            with store.locked():
                current = store.load_session(session_id)
                if current is None or current.state == "ended":
                    return
                if not _same_generation(
                    current,
                    cwd,
                    host_keys,
                    chain[0].boot_id,
                ):
                    return
                ended_observed = _observed(clock, current.observed.boot_id)
                if ended_observed.boottime < current.observed.boottime:
                    return
                ended = replace(
                    current,
                    state="ended",
                    ended=ended_observed,
                )
                ended.to_dict()
                durable = _save_session_milestone(store, ended)
                _append_event_best_effort(
                    store,
                    _event("session_ended", ended),
                    ended_observed.wall_iso,
                )
        finally:
            notified = durable and _request_cleanup(notifier)
        if durable and not notified:
            _hook_diagnostic(
                store,
                clock,
                "hook_notifier_failed",
                "notifier_request_failed",
            )
    except Exception:
        return
