from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
import select
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


class SupervisorProcessExitRequired(BaseException):
    """Process-exit-only boundary for a handler that could not be restored."""


class _PersistentHandlerRestoreFailure(RuntimeError):
    pass


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


@dataclass
class _OwnedGroupLifecycle:
    """Exact lifecycle for the process group created by this wrapper.

    The unreaped direct leader is the only authority for numeric PGID actions.
    Once the leader is reaped, cleanup uses only pidfds captured and revalidated
    while identities were exact. A TERM-resistant exact identity is supervised
    indefinitely; a reused PID or PGID is never treated as owned.
    """

    child: subprocess.Popen[bytes]
    procfs: LinuxProcfs
    pgid: int
    leader_reaped: bool = False
    group_term_sent: bool = False
    disposed: bool = False
    identities: dict[str, ProcessIdentity] = field(default_factory=dict)
    pidfds: dict[str, int] = field(default_factory=dict)
    exact_term_sent: set[str] = field(default_factory=set)

    def _observation(self, identity: ProcessIdentity) -> str:
        try:
            observed = self.procfs.observe_identity(identity.pid)
        except BaseException:
            return "unavailable"
        if observed.kind == "missing":
            return "gone"
        if observed.kind == "live":
            if (
                observed.identity is not None
                and observed.identity.stable_key() == identity.stable_key()
            ):
                return "exact"
            return "gone"
        return "unavailable"

    def _pidfd_exited(self, stable_key: str) -> bool:
        pidfd = self.pidfds.get(stable_key)
        if pidfd is None:
            return False
        try:
            readable, _, _ = select.select((pidfd,), (), (), 0)
        except BaseException:
            return False
        return bool(readable)

    def _identity_live(self, identity: ProcessIdentity) -> bool:
        key = identity.stable_key()
        if self._pidfd_exited(key):
            return False
        observation = self._observation(identity)
        if observation == "gone":
            return False
        return True

    def _open_pidfd(self, identity: ProcessIdentity) -> None:
        key = identity.stable_key()
        if key in self.pidfds or self._observation(identity) != "exact":
            return
        opener = getattr(os, "pidfd_open", None)
        if not callable(opener):
            return
        try:
            pidfd = opener(identity.pid, 0)
        except BaseException:
            return
        try:
            if self._observation(identity) == "exact":
                self.pidfds[key] = pidfd
                return
        except BaseException:
            pass
        try:
            os.close(pidfd)
        except OSError:
            pass

    def capture_exact(self, identities: tuple[ProcessIdentity, ...]) -> None:
        for identity in identities:
            key = identity.stable_key()
            self.identities.setdefault(key, identity)
            self._open_pidfd(identity)

    def capture_group(self) -> None:
        if self.child.returncode is not None:
            self.leader_reaped = True
        if self.leader_reaped:
            return
        captured: dict[str, ProcessIdentity] = {}
        try:
            leader_observation = self.procfs.observe_identity(self.child.pid)
        except BaseException:
            leader_observation = None
        if (
            leader_observation is not None
            and leader_observation.kind == "live"
            and leader_observation.identity is not None
            and leader_observation.identity.pgid == self.pgid
        ):
            leader = leader_observation.identity
            captured[leader.stable_key()] = leader
        try:
            members = self.procfs.group_members(self.pgid)
        except BaseException:
            members = ()
        captured.update(
            {
                member.stable_key(): member
                for member in members
                if member.pgid == self.pgid
            }
        )
        self.capture_exact(tuple(captured.values()))

    def _send_exact(self, signum: int, *, migrated_only: bool = False) -> None:
        sender = getattr(signal, "pidfd_send_signal", None)
        if not callable(sender):
            return
        for key, identity in tuple(self.identities.items()):
            if signum == signal.SIGTERM and key in self.exact_term_sent:
                continue
            if not self._identity_live(identity):
                continue
            if migrated_only:
                try:
                    observed = self.procfs.observe_identity(identity.pid)
                except BaseException:
                    continue
                if (
                    observed.kind != "live"
                    or observed.identity is None
                    or observed.identity.stable_key() != identity.stable_key()
                    or observed.identity.pgid == self.pgid
                ):
                    continue
            pidfd = self.pidfds.get(key)
            if pidfd is None:
                continue
            if signum == signal.SIGTERM:
                self.exact_term_sent.add(key)
            try:
                sender(pidfd, signum, None, 0)
            except BaseException:
                pass

    def forward(self, signum: int) -> None:
        if self.child.returncode is not None:
            self.leader_reaped = True
        if self.leader_reaped or (signum == signal.SIGTERM and self.group_term_sent):
            self._send_exact(signum)
            return
        self.capture_group()
        if self.leader_reaped:
            self._send_exact(signum)
            return
        if signum == signal.SIGTERM:
            self.group_term_sent = True
        forward_signal(self.pgid, signum)

    def _wait_once(self, timeout: float | None) -> int:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _FORWARDED_SIGNALS)
        try:
            return self.child.wait(timeout=timeout)
        finally:
            if self.child.returncode is not None:
                self.leader_reaped = True
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def wait_for_exit(self, check_forwarding: Callable[[], None]) -> int:
        while True:
            self.capture_group()
            try:
                result = self._wait_once(0.05)
            except subprocess.TimeoutExpired:
                check_forwarding()
                continue
            check_forwarding()
            return result

    def _reap_after_term(self) -> None:
        while not self.leader_reaped:
            self.capture_group()
            try:
                self._wait_once(0.05)
            except subprocess.TimeoutExpired:
                continue
            except BaseException:
                _cleanup_pause()

    def dispose(
        self,
        known_members: tuple[ProcessIdentity, ...] = (),
    ) -> _ChildDisposition:
        if self.disposed:
            return self.disposition()
        self.capture_exact(known_members)
        if self.child.returncode is not None:
            self.leader_reaped = True
        group_term_failed = False
        if not self.leader_reaped and not self.group_term_sent:
            self.capture_group()
            self.group_term_sent = True
            try:
                forward_signal(self.pgid, signal.SIGTERM)
            except BaseException:
                group_term_failed = True
            self.capture_group()
            self._send_exact(
                signal.SIGTERM,
                migrated_only=not group_term_failed,
            )
        elif not self.leader_reaped:
            self.capture_group()
            self._send_exact(signal.SIGTERM)
        self._reap_after_term()
        self._send_exact(signal.SIGTERM)
        while any(
            self._identity_live(identity) for identity in self.identities.values()
        ):
            self._send_exact(signal.SIGTERM)
            _cleanup_pause()
        self.disposed = True
        return self.disposition()

    def disposition(self) -> _ChildDisposition:
        return _ChildDisposition(
            exit_code=self.child.returncode,
            members=tuple(self.identities[key] for key in sorted(self.identities)),
        )

    def close(self) -> None:
        for pidfd in reversed(tuple(self.pidfds.values())):
            try:
                os.close(pidfd)
            except OSError:
                pass
        self.pidfds.clear()


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
    dict[str, object],
    list[int],
]:
    previous: dict[int, signal.Handlers] = {}
    child_ref: dict[str, object] = {
        "child": None,
        "lifecycle": None,
        "forward_failed": False,
    }
    pending: list[int] = []

    def handle(signum: int, _frame: object) -> None:
        try:
            child = child_ref["child"]
            if child is None:
                pending.append(signum)
                return
            lifecycle = child_ref["lifecycle"]
            if isinstance(lifecycle, _OwnedGroupLifecycle):
                lifecycle.forward(signum)
                return
            child_pid = getattr(child, "pid", None)
            if type(child_pid) is not int:
                raise RuntimeError("child publication is incomplete")
            forward_signal(child_pid, signum)
        except BaseException:
            child_ref["forward_failed"] = True

    installed: list[int] = []
    try:
        for signum in _FORWARDED_SIGNALS:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handle)
            installed.append(signum)
    except BaseException:
        failed = _restore_signal_handlers(previous, tuple(installed))
        if failed and _retry_signal_handlers(previous, tuple(installed), failed):
            raise _PersistentHandlerRestoreFailure from None
        raise _StartupFailure("handler_install_failed") from None
    return previous, child_ref, pending


def _restore_signal_handlers(
    previous: dict[int, signal.Handlers],
    installed: tuple[int, ...] = _FORWARDED_SIGNALS,
) -> tuple[int, ...]:
    failed: set[int] = set()
    for signum in reversed(installed):
        if signum in previous:
            try:
                signal.signal(signum, previous[signum])
            except BaseException:
                failed.add(signum)
    for signum in reversed(installed):
        if signum not in previous:
            continue
        try:
            current = signal.getsignal(signum)
        except BaseException:
            failed.add(signum)
            continue
        if current != previous[signum]:
            failed.add(signum)
    return tuple(signum for signum in reversed(installed) if signum in failed)


def _retry_signal_handlers(
    previous: dict[int, signal.Handlers],
    installed: tuple[int, ...],
    failed: tuple[int, ...],
) -> tuple[int, ...]:
    for signum in failed:
        try:
            signal.signal(signum, previous[signum])
        except BaseException:
            pass
    remaining: list[int] = []
    for signum in reversed(installed):
        if signum not in previous:
            continue
        try:
            current = signal.getsignal(signum)
        except BaseException:
            remaining.append(signum)
            continue
        if current != previous[signum]:
            remaining.append(signum)
    return tuple(remaining)


def _publish_child(
    child_ref: dict[str, object], child: subprocess.Popen[bytes]
) -> None:
    child_ref["child"] = child


def _forwarding_failed(child_ref: dict[str, object]) -> bool:
    return child_ref.get("forward_failed") is True


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


def _cleanup_pause() -> None:
    try:
        time.sleep(0.05)
    except BaseException:
        pass


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


def _run_spawned_child(
    child: subprocess.Popen[bytes],
    lifecycle: _OwnedGroupLifecycle,
    child_ref: dict[str, object],
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

    def fail_if_forwarding_failed() -> None:
        if _forwarding_failed(child_ref):
            raise _PostSpawnFailure(
                "signal_forward_failed",
                process,
                record_may_exist=record_may_exist,
            )

    try:
        for signum in pending:
            lifecycle.forward(signum)
        fail_if_forwarding_failed()
    except _PostSpawnFailure:
        raise
    except BaseException:
        raise _PostSpawnFailure(
            "signal_forward_failed",
            process,
            record_may_exist=record_may_exist,
        ) from None

    try:
        child_identity, members = _child_observation(child, procfs)
        lifecycle.capture_exact(members)
        lifecycle.capture_group()
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
        fail_if_forwarding_failed()
    except _PostSpawnFailure:
        raise
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
        fail_if_forwarding_failed()
    except _PostSpawnFailure:
        raise
    except BaseException:
        raise _PostSpawnFailure(
            "state_spawn_failed",
            process,
            record_may_exist=record_may_exist,
        ) from None

    try:
        process = _reconcile_owner(process, store, clock, sleeper)
        fail_if_forwarding_failed()
    except _ReconciliationPersistenceFailure as error:
        process = error.process
        raise _PostSpawnFailure(
            "owner_reconcile_failed",
            process,
            record_may_exist=record_may_exist,
        ) from None
    except _PostSpawnFailure:
        raise
    except BaseException:
        raise _PostSpawnFailure(
            "owner_reconcile_failed",
            process,
            record_may_exist=record_may_exist,
        ) from None

    try:
        lifecycle.capture_group()
        exit_code = lifecycle.wait_for_exit(fail_if_forwarding_failed)
    except _PostSpawnFailure:
        raise
    except BaseException:
        raise _PostSpawnFailure(
            "child_wait_failed",
            process,
            record_may_exist=record_may_exist,
        ) from None

    updated = replace(
        process,
        members=lifecycle.disposition().members,
        exit_code=exit_code,
    )
    try:
        with store.locked():
            store.save_process(updated)
            store.append_event(_exit_event(updated, clock.wall_iso()))
        fail_if_forwarding_failed()
    except _PostSpawnFailure:
        raise
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
    except _PersistentHandlerRestoreFailure:
        _fatal(safe_server, "handler_restore_failed")
        raise SupervisorProcessExitRequired from None
    except _StartupFailure as error:
        _fatal(safe_server, error.reason_code)
        return _FATAL_EXIT

    child: subprocess.Popen[bytes] | None = None
    lifecycle: _OwnedGroupLifecycle | None = None
    process: ManagedProcess | None = None
    record_may_exist = False
    result = _FATAL_EXIT
    fatal_reason: str | None = None
    process_exit_required = False
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
            try:
                lifecycle = _OwnedGroupLifecycle(child, procfs, child.pid)
                child_ref["lifecycle"] = lifecycle
                _publish_child(child_ref, child)
                if _forwarding_failed(child_ref):
                    raise _PostSpawnFailure(
                        "signal_forward_failed",
                        process,
                        record_may_exist=record_may_exist,
                    )
                result, process, record_may_exist = _run_spawned_child(
                    child,
                    lifecycle,
                    child_ref,
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
            except BaseException as caught:
                if isinstance(caught, _PostSpawnFailure):
                    error = caught
                else:
                    error = _PostSpawnFailure(
                        "signal_forward_failed",
                        process,
                        record_may_exist=record_may_exist,
                    )
                process = error.process
                record_may_exist = error.record_may_exist
                known_members = () if process is None else process.members
                if lifecycle is None:
                    lifecycle = _OwnedGroupLifecycle(child, procfs, child.pid)
                disposition = lifecycle.dispose(known_members)
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
        failed_handlers = _restore_signal_handlers(previous)
        forward_failed = _forwarding_failed(child_ref)
        if forward_failed and fatal_reason is None:
            fatal_reason = "signal_forward_failed"
            result = _FATAL_EXIT
        if failed_handlers or forward_failed:
            if lifecycle is not None:
                known = () if process is None else process.members
                disposition = lifecycle.dispose(known)
                process = _persist_terminal_best_effort(
                    process,
                    disposition,
                    fatal_reason or "handler_restore_failed",
                    store,
                    record_may_exist=record_may_exist,
                )
        if failed_handlers:
            remaining_handlers = _retry_signal_handlers(
                previous,
                _FORWARDED_SIGNALS,
                failed_handlers,
            )
            if _forwarding_failed(child_ref) and fatal_reason is None:
                fatal_reason = "signal_forward_failed"
                result = _FATAL_EXIT
            if remaining_handlers:
                fatal_reason = "handler_restore_failed"
                result = _FATAL_EXIT
                process_exit_required = True
        if lifecycle is not None:
            lifecycle.close()
    if fatal_reason is not None:
        _fatal(safe_server, fatal_reason)
    if process_exit_required:
        raise SupervisorProcessExitRequired from None
    return result
