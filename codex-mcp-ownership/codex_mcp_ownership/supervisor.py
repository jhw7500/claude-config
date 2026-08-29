from __future__ import annotations

from dataclasses import dataclass, replace
import os
import signal
import subprocess
import sys
import time
from typing import Callable

from .clock import Clock
from .classify import associate_owner
from .model import (
    Association,
    ManagedProcess,
    ObservedTime,
    ProcessIdentity,
    validate_scope,
    validate_server_name,
)
from .procfs import LinuxProcfs
from .state import StateCorruption, StateLockTimeout, StateStore, UnsafeStatePath


_RECONCILE_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)
_FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_FATAL_EXIT = 70
_ABORT_WAIT_SECONDS = 2.0


@dataclass(frozen=True)
class SupervisorRequest:
    scope: str
    server: str
    command: str
    args: tuple[str, ...]
    cwd: str


class _StartupFailure(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _ReconciliationPersistenceFailure(RuntimeError):
    def __init__(self, process: ManagedProcess) -> None:
        super().__init__("owner_reconcile_failed")
        self.process = process


class _PostSpawnFailure(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        process: ManagedProcess | None,
        *,
        record_may_exist: bool,
        result: int = _FATAL_EXIT,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.process = process
        self.record_may_exist = record_may_exist
        self.result = result


@dataclass(frozen=True)
class _ChildDisposition:
    exit_code: int | None
    members: tuple[ProcessIdentity, ...]


def _validated_request(request: SupervisorRequest) -> SupervisorRequest:
    if not isinstance(request, SupervisorRequest):
        raise _StartupFailure("invalid_request")
    try:
        scope = validate_scope(request.scope)
        server = validate_server_name(request.server)
    except ValueError:
        raise _StartupFailure("invalid_request") from None
    if (
        not isinstance(request.command, str)
        or not request.command
        or "\0" in request.command
        or type(request.args) is not tuple
        or any(
            not isinstance(argument, str) or "\0" in argument
            for argument in request.args
        )
        or not isinstance(request.cwd, str)
        or not request.cwd
        or "\0" in request.cwd
    ):
        raise _StartupFailure("invalid_request")
    return replace(request, scope=scope, server=server)


def _fatal(server: str | None, reason_code: str) -> None:
    safe_server = "<invalid>" if server is None else server
    try:
        sys.stderr.write(
            f"codex-mcp-supervisor: server={safe_server} reason={reason_code}\n"
        )
        sys.stderr.flush()
    except OSError:
        pass


def _observed(clock: Clock, boot_id: str) -> ObservedTime:
    observed = ObservedTime(clock.wall_iso(), boot_id, clock.boottime())
    observed.to_dict()
    return observed


def _wrapper_observation(
    procfs: LinuxProcfs,
    clock: Clock,
) -> tuple[ProcessIdentity, frozenset[str], ObservedTime]:
    try:
        chain = procfs.ancestor_chain(os.getpid())
        wrapper = procfs.identity(os.getpid())
    except BaseException:
        raise _StartupFailure("wrapper_identity_unavailable") from None
    if not chain or wrapper is None or chain[0] != wrapper:
        raise _StartupFailure("wrapper_identity_unavailable")
    try:
        spawned = _observed(clock, wrapper.boot_id)
    except BaseException:
        raise _StartupFailure("spawn_time_unavailable") from None
    return (
        wrapper,
        frozenset(identity.stable_key() for identity in chain[1:]),
        spawned,
    )


def forward_signal(child_pgid: int, signum: int) -> None:
    if type(child_pgid) is not int or child_pgid < 1:
        raise ValueError("invalid child process group")
    if signum not in _FORWARDED_SIGNALS:
        raise ValueError("unsupported forwarded signal")
    try:
        os.killpg(child_pgid, signum)
    except ProcessLookupError:
        return


def _install_signal_handlers() -> tuple[
    dict[int, signal.Handlers],
    dict[str, subprocess.Popen[bytes] | None],
    list[int],
]:
    previous: dict[int, signal.Handlers] = {}
    child_ref: dict[str, subprocess.Popen[bytes] | None] = {"child": None}
    pending: list[int] = []

    def handle(signum: int, _frame: object) -> None:
        child = child_ref["child"]
        if child is None:
            pending.append(signum)
            return
        try:
            live = child.poll() is None
        except OSError:
            live = False
        if live:
            forward_signal(child.pid, signum)

    installed: list[int] = []
    try:
        for signum in _FORWARDED_SIGNALS:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handle)
            installed.append(signum)
    except BaseException:
        if not _restore_signal_handlers(previous, tuple(installed)):
            raise _StartupFailure("handler_restore_failed") from None
        raise _StartupFailure("handler_install_failed") from None
    return previous, child_ref, pending


def _restore_signal_handlers(
    previous: dict[int, signal.Handlers],
    installed: tuple[int, ...] = _FORWARDED_SIGNALS,
) -> bool:
    restored = True
    for signum in reversed(installed):
        if signum in previous:
            try:
                signal.signal(signum, previous[signum])
            except BaseException:
                restored = False
    return restored


def _child_observation(
    child: subprocess.Popen[bytes],
    procfs: LinuxProcfs,
) -> tuple[ProcessIdentity, tuple[ProcessIdentity, ...]]:
    try:
        identity = procfs.identity(child.pid)
        members = procfs.group_members(child.pid)
    except BaseException:
        raise _StartupFailure("child_identity_unavailable") from None
    if identity is None or identity.pgid != child.pid:
        raise _StartupFailure("child_identity_unavailable")
    exact_members = {identity.stable_key(): identity}
    exact_members.update(
        {
            member.stable_key(): member
            for member in members
            if member.pgid == identity.pgid
        }
    )
    return identity, tuple(exact_members[key] for key in sorted(exact_members))


def _apply_association(
    process: ManagedProcess,
    association: Association,
) -> ManagedProcess:
    return replace(
        process,
        owner_session_id=association.session_id,
        shared_owner=association.shared_owner,
        owner_reason_codes=association.reason_codes,
    )


def _association_event(
    process: ManagedProcess,
    association: Association,
    observed_wall: str,
) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": 1,
        "event": "owner_reconciled",
        "observed_wall": observed_wall,
        "server": process.server,
        "scope": process.scope,
        "process_key": process.wrapper.stable_key(),
        "state": association.kind,
        "reason_codes": list(association.reason_codes),
    }
    if association.session_id is not None:
        event["session_id"] = association.session_id
    return event


def _reconcile_owner(
    process: ManagedProcess,
    store: StateStore,
    clock: Clock,
    sleeper: Callable[[float], None] = time.sleep,
) -> ManagedProcess:
    delays = (None, *_RECONCILE_DELAYS)
    sticky_reason: str | None = None
    for attempt, delay in enumerate(delays):
        if delay is not None:
            sleeper(delay)
        updated: ManagedProcess | None = None
        try:
            with store.locked():
                try:
                    leases = store.load_sessions()
                except StateCorruption:
                    sticky_reason = "corrupt_session_state"
                    association = Association(
                        kind="unknown",
                        session_id=None,
                        shared_owner=None,
                        reason_codes=("corrupt_session_state",),
                    )
                except (OSError, StateLockTimeout, UnsafeStatePath):
                    if sticky_reason is None:
                        sticky_reason = "session_state_unavailable"
                    association = Association(
                        kind="unknown",
                        session_id=None,
                        shared_owner=None,
                        reason_codes=("session_state_unavailable",),
                    )
                else:
                    association = associate_owner(process, leases, clock.boottime())
                final = attempt == len(delays) - 1
                if association.kind != "unknown" or final:
                    if association.kind == "unknown" and sticky_reason is not None:
                        association = replace(
                            association,
                            reason_codes=(sticky_reason,),
                        )
                    updated = _apply_association(process, association)
                    try:
                        store.save_process(updated)
                        store.append_event(
                            _association_event(updated, association, clock.wall_iso())
                        )
                    except BaseException:
                        raise _ReconciliationPersistenceFailure(updated) from None
        except (OSError, StateLockTimeout, UnsafeStatePath):
            if updated is not None:
                raise _ReconciliationPersistenceFailure(updated) from None
            if sticky_reason is None:
                sticky_reason = "session_state_unavailable"
            final = attempt == len(delays) - 1
            if final:
                association = Association(
                    kind="unknown",
                    session_id=None,
                    shared_owner=None,
                    reason_codes=(sticky_reason,),
                )
                return _apply_association(process, association)
            continue
        if updated is not None:
            return updated
    raise AssertionError("unreachable reconciliation state")


def _spawn_event(process: ManagedProcess) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event": "supervisor_spawned",
        "observed_wall": process.spawned.wall_iso,
        "server": process.server,
        "scope": process.scope,
        "process_key": process.wrapper.stable_key(),
        "state": "unknown",
        "reason_codes": ["association_pending"],
    }


def _exit_event(process: ManagedProcess, observed_wall: str) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": 1,
        "event": "supervisor_child_exited",
        "observed_wall": observed_wall,
        "server": process.server,
        "scope": process.scope,
        "process_key": process.wrapper.stable_key(),
        "state": "exiting",
        "reason_codes": ["child_exit_observed"],
    }
    if process.owner_session_id is not None:
        event["session_id"] = process.owner_session_id
    return event


def _group_snapshot(
    procfs: LinuxProcfs,
    child_pgid: int,
) -> tuple[ProcessIdentity, ...] | None:
    try:
        return tuple(
            member
            for member in procfs.group_members(child_pgid)
            if member.pgid == child_pgid
        )
    except BaseException:
        return None


def _owned_group_is_gone(child_pgid: int) -> bool | None:
    try:
        os.killpg(child_pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return None
    return False


def _cleanup_pause() -> None:
    try:
        time.sleep(0.05)
    except BaseException:
        pass


def _dispose_child_group(
    child: subprocess.Popen[bytes],
    child_pgid: int,
    procfs: LinuxProcfs,
    known_members: tuple[ProcessIdentity, ...] = (),
) -> _ChildDisposition:
    """TERM and supervise the owned group until it is observably gone.

    There is intentionally no SIGKILL fallback. A TERM-resistant owned group keeps
    this wrapper supervising indefinitely instead of being abandoned or killed
    outside the automatic-cleanup policy.
    """
    members = {member.stable_key(): member for member in known_members}
    before = _group_snapshot(procfs, child_pgid)
    if before is not None:
        members.update({member.stable_key(): member for member in before})
    try:
        forward_signal(child_pgid, signal.SIGTERM)
    except BaseException:
        pass

    first_wait = True
    while child.returncode is None:
        try:
            child.wait(timeout=_ABORT_WAIT_SECONDS if first_wait else None)
            break
        except subprocess.TimeoutExpired:
            first_wait = False
        except BaseException:
            _cleanup_pause()
    exit_code = child.returncode

    while True:
        current = _group_snapshot(procfs, child_pgid)
        if current is not None:
            members.update({member.stable_key(): member for member in current})
        if _owned_group_is_gone(child_pgid) is True:
            break
        _cleanup_pause()
    return _ChildDisposition(
        exit_code=exit_code,
        members=tuple(members[key] for key in sorted(members)),
    )


def _terminal_process(
    process: ManagedProcess,
    disposition: _ChildDisposition,
    failure_reason: str,
) -> ManagedProcess:
    reason_codes = process.owner_reason_codes
    if reason_codes == ("association_pending",):
        reason_codes = (failure_reason,)
    return replace(
        process,
        members=disposition.members,
        exit_code=disposition.exit_code,
        owner_reason_codes=reason_codes,
    )


def _persist_terminal_best_effort(
    process: ManagedProcess | None,
    disposition: _ChildDisposition,
    failure_reason: str,
    store: StateStore,
    *,
    record_may_exist: bool,
) -> ManagedProcess | None:
    if process is None or not record_may_exist:
        return process
    terminal = _terminal_process(process, disposition, failure_reason)
    try:
        with store.locked():
            store.save_process(terminal)
    except BaseException:
        pass
    return terminal


def _merge_members(
    process: ManagedProcess,
    procfs: LinuxProcfs,
) -> tuple[ProcessIdentity, ...]:
    identities = {member.stable_key(): member for member in process.members}
    try:
        current = procfs.group_members(process.pgid)
    except BaseException:
        current = ()
    identities.update(
        {
            member.stable_key(): member
            for member in current
            if member.pgid == process.pgid
        }
    )
    return tuple(identities[key] for key in sorted(identities))


def _run_spawned_child(
    child: subprocess.Popen[bytes],
    pending: list[int],
    validated: SupervisorRequest,
    wrapper: ProcessIdentity,
    host_keys: frozenset[str],
    spawned: ObservedTime,
    store: StateStore,
    procfs: LinuxProcfs,
    clock: Clock,
    sleeper: Callable[[float], None],
) -> tuple[int, ManagedProcess, bool]:
    process: ManagedProcess | None = None
    record_may_exist = False
    try:
        for signum in pending:
            forward_signal(child.pid, signum)
    except BaseException:
        raise _PostSpawnFailure(
            "signal_forward_failed",
            process,
            record_may_exist=record_may_exist,
        ) from None

    try:
        child_identity, members = _child_observation(child, procfs)
        process = ManagedProcess(
            schema_version=1,
            record_id=wrapper.stable_key(),
            scope=validated.scope,
            server=validated.server,
            cwd=validated.cwd,
            wrapper=wrapper,
            child=child_identity,
            members=members,
            pgid=child_identity.pgid,
            host_keys=host_keys,
            spawned=spawned,
            owner_reason_codes=("association_pending",),
        )
        process.to_dict()
    except BaseException:
        raise _PostSpawnFailure(
            "child_identity_unavailable",
            process,
            record_may_exist=record_may_exist,
        ) from None

    record_may_exist = True
    try:
        with store.locked():
            store.save_process(process)
            store.append_event(_spawn_event(process))
    except BaseException:
        raise _PostSpawnFailure(
            "state_spawn_failed",
            process,
            record_may_exist=record_may_exist,
        ) from None

    try:
        process = _reconcile_owner(process, store, clock, sleeper)
    except _ReconciliationPersistenceFailure as error:
        process = error.process
        raise _PostSpawnFailure(
            "owner_reconcile_failed",
            process,
            record_may_exist=record_may_exist,
        ) from None
    except BaseException:
        raise _PostSpawnFailure(
            "owner_reconcile_failed",
            process,
            record_may_exist=record_may_exist,
        ) from None

    try:
        exit_code = child.wait()
    except BaseException:
        raise _PostSpawnFailure(
            "child_wait_failed",
            process,
            record_may_exist=record_may_exist,
        ) from None

    updated = replace(
        process,
        members=_merge_members(process, procfs),
        exit_code=exit_code,
    )
    try:
        with store.locked():
            store.save_process(updated)
            store.append_event(_exit_event(updated, clock.wall_iso()))
    except BaseException:
        raise _PostSpawnFailure(
            "state_exit_failed",
            updated,
            record_may_exist=record_may_exist,
            result=exit_code,
        ) from None
    return exit_code, updated, record_may_exist


def run_supervisor(
    request: SupervisorRequest,
    store: StateStore,
    procfs: LinuxProcfs,
    clock: Clock,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    try:
        validated = _validated_request(request)
    except _StartupFailure as error:
        _fatal(None, error.reason_code)
        return _FATAL_EXIT
    safe_server = validated.server
    try:
        wrapper, host_keys, spawned = _wrapper_observation(procfs, clock)
        previous, child_ref, pending = _install_signal_handlers()
    except _StartupFailure as error:
        _fatal(safe_server, error.reason_code)
        return _FATAL_EXIT

    child: subprocess.Popen[bytes] | None = None
    process: ManagedProcess | None = None
    record_may_exist = False
    result = _FATAL_EXIT
    fatal_reason: str | None = None
    try:
        try:
            child = subprocess.Popen(
                [validated.command, *validated.args],
                stdin=None,
                stdout=None,
                stderr=None,
                cwd=validated.cwd,
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            fatal_reason = "child_spawn_failed"
        else:
            child_ref["child"] = child
            try:
                result, process, record_may_exist = _run_spawned_child(
                    child,
                    pending,
                    validated,
                    wrapper,
                    host_keys,
                    spawned,
                    store,
                    procfs,
                    clock,
                    sleeper,
                )
            except _PostSpawnFailure as error:
                process = error.process
                record_may_exist = error.record_may_exist
                known_members = () if process is None else process.members
                disposition = _dispose_child_group(
                    child,
                    child.pid,
                    procfs,
                    known_members,
                )
                process = _persist_terminal_best_effort(
                    process,
                    disposition,
                    error.reason_code,
                    store,
                    record_may_exist=record_may_exist,
                )
                fatal_reason = error.reason_code
                result = error.result
    finally:
        if not _restore_signal_handlers(previous):
            if child is not None:
                known = () if process is None else process.members
                disposition = _dispose_child_group(child, child.pid, procfs, known)
                process = _persist_terminal_best_effort(
                    process,
                    disposition,
                    "handler_restore_failed",
                    store,
                    record_may_exist=record_may_exist,
                )
            fatal_reason = "handler_restore_failed"
            result = _FATAL_EXIT
    if fatal_reason is not None:
        _fatal(safe_server, fatal_reason)
        return result
    return result
