from __future__ import annotations

from dataclasses import replace
import math
import os
import subprocess
import unicodedata

from .cleanup import PidfdSignalBackend, execute_cleanup, plan_cleanup
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


def _opportunistic_cleanup(
    store: StateStore,
    procfs: LinuxProcfs,
    clock: Clock,
) -> None:
    snapshot = build_audit(store, procfs, clock)
    if snapshot.corrupt_count:
        return
    actions = plan_cleanup(snapshot)
    if not actions:
        return
    signaler = PidfdSignalBackend()
    execute_cleanup(
        actions,
        store,
        procfs,
        signaler,
        clock,
        apply=True,
    )


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
        if event == "SessionStart":
            parent_pid = os.getppid()
            chain = procfs.ancestor_chain(parent_pid)
            if not chain or chain[0].pid != parent_pid:
                return
            boot_ids = {identity.boot_id for identity in chain}
            if len(boot_ids) != 1:
                return
            lease = SessionLease(
                schema_version=1,
                session_id=session_id,
                cwd=cwd,
                source=source or "",
                host_keys=tuple(identity.stable_key() for identity in chain),
                state="active",
                observed=_observed(clock, chain[0].boot_id),
            )
            lease.to_dict()
            with store.locked():
                store.save_session(lease)
                store.append_event(_event("session_started", lease))
            if not notifier.request_cleanup():
                try:
                    _opportunistic_cleanup(store, procfs, clock)
                except Exception:
                    pass
            return

        with store.locked():
            current = store.load_session(session_id)
            if current is None or current.state == "ended":
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
            store.save_session(ended)
            store.append_event(_event("session_ended", ended))
        notifier.request_cleanup()
    except Exception:
        return
