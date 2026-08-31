from __future__ import annotations

from dataclasses import dataclass, field, replace
import errno
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


class SupervisorProcessExitRequired(SystemExit):
    """Process-exit-only boundary for unverifiable supervisor finalization."""

    def __init__(self) -> None:
        super().__init__(_FATAL_EXIT)


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
class _SignalMaskState:
    original_mask: frozenset[signal.Signals]
    restore_failed: bool = False

    @classmethod
    def capture(cls) -> _SignalMaskState:
        current = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        return cls(frozenset(current))

    def block_forwarded(self) -> frozenset[signal.Signals]:
        try:
            previous = signal.pthread_sigmask(signal.SIG_BLOCK, _FORWARDED_SIGNALS)
        except BaseException:
            self.restore_failed = True
            raise
        return frozenset(previous)

    def restore(self, previous: frozenset[signal.Signals]) -> None:
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        except BaseException:
            self.restore_failed = True
            raise

    def restore_original(self) -> tuple[bool, BaseException | None]:
        try:
            current = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        except BaseException:
            current = None
        if current is not None and frozenset(current) == self.original_mask:
            self.restore_failed = False
            return True, None
        for _attempt in range(2):
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, self.original_mask)
                current = signal.pthread_sigmask(signal.SIG_BLOCK, ())
            except BaseException as error:
                self.restore_failed = True
                try:
                    current = signal.pthread_sigmask(signal.SIG_BLOCK, ())
                except BaseException:
                    continue
                if frozenset(current) == self.original_mask:
                    self.restore_failed = False
                    return True, error
                continue
            if frozenset(current) == self.original_mask:
                self.restore_failed = False
                return True, None
            self.restore_failed = True
        return False, None


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
    leader_exit_observed: bool = False
    leader_reaped: bool = False
    group_access_closed: bool = False
    group_term_sent: bool = False
    disposed: bool = False
    identities: dict[str, ProcessIdentity] = field(default_factory=dict)
    pidfds: dict[str, int] = field(default_factory=dict)
    owned_pidfds: set[int] = field(default_factory=set)
    pidfd_close_unverifiable: bool = False
    exact_term_sent: set[str] = field(default_factory=set)
    mask_state: _SignalMaskState = field(default_factory=_SignalMaskState.capture)

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
        self.owned_pidfds.add(pidfd)
        try:
            if self._observation(identity) == "exact":
                published = self.pidfds.setdefault(key, pidfd)
                if published == pidfd:
                    return
        except BaseException:
            pass
        self._close_pidfd_once(pidfd)

    def _close_pidfd_once(self, pidfd: int) -> None:
        self.owned_pidfds.discard(pidfd)
        try:
            os.close(pidfd)
        except OSError as error:
            if error.errno != errno.EBADF:
                self.pidfd_close_unverifiable = True
            return
        except BaseException:
            self.pidfd_close_unverifiable = True

    def _open_unreaped_leader_pidfd(self) -> None:
        if not self.leader_exit_observed or self.leader_reaped:
            return
        leader = next(
            (
                identity
                for identity in self.identities.values()
                if identity.pid == self.child.pid
            ),
            None,
        )
        if leader is None:
            return
        key = leader.stable_key()
        if key in self.pidfds:
            return
        opener = getattr(os, "pidfd_open", None)
        if not callable(opener):
            return
        try:
            pidfd = opener(self.child.pid, 0)
        except BaseException:
            return
        self.owned_pidfds.add(pidfd)
        published = self.pidfds.setdefault(key, pidfd)
        if published != pidfd:
            self._close_pidfd_once(pidfd)

    def capture_exact(self, identities: tuple[ProcessIdentity, ...]) -> None:
        for identity in identities:
            key = identity.stable_key()
            self.identities.setdefault(key, identity)
            self._open_pidfd(identity)

    def capture_group(self) -> None:
        if self.child.returncode is not None:
            self.leader_reaped = True
        if self.leader_reaped or self.group_access_closed:
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

    def capture_final_group(self) -> bool:
        try:
            observation = self.procfs.observe_group_members(self.pgid)
        except BaseException:
            return False
        self.capture_exact(observation.members)
        if observation.kind != "complete":
            return False
        self._open_unreaped_leader_pidfd()
        for identity in tuple(self.identities.values()):
            key = identity.stable_key()
            if key in self.pidfds:
                continue
            self._open_pidfd(identity)
            if key not in self.pidfds and self._observation(identity) != "gone":
                return False
        return True

    def _send_exact(
        self,
        signum: int,
        *,
        migrated_only: bool = False,
        forwarding: bool = False,
    ) -> None:
        sender = getattr(signal, "pidfd_send_signal", None)
        for key, identity in tuple(self.identities.items()):
            if signum == signal.SIGTERM and key in self.exact_term_sent:
                continue
            if self._pidfd_exited(key):
                continue
            observation = self._observation(identity)
            if observation == "gone":
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
            if not callable(sender) or pidfd is None:
                if forwarding:
                    raise RuntimeError("exact signal forwarding unavailable")
                continue
            try:
                sender(pidfd, signum, None, 0)
            except BaseException:
                if forwarding and not self._pidfd_exited(key):
                    if self._observation(identity) != "gone":
                        raise RuntimeError("exact signal forwarding failed") from None
                continue
            if signum == signal.SIGTERM:
                self.exact_term_sent.add(key)

    def forward(self, signum: int) -> None:
        previous_mask = self.mask_state.block_forwarded()
        try:
            if self.child.returncode is not None:
                self.leader_reaped = True
            if (
                self.leader_reaped
                or self.group_access_closed
                or (signum == signal.SIGTERM and self.group_term_sent)
            ):
                self._send_exact(signum, forwarding=True)
                return
            self.capture_group()
            if (
                self.leader_reaped
                or self.group_access_closed
                or (signum == signal.SIGTERM and self.group_term_sent)
            ):
                self._send_exact(signum, forwarding=True)
                return
            if signum == signal.SIGTERM:
                self.group_term_sent = True
            forward_signal(self.pgid, signum)
        finally:
            self.mask_state.restore(previous_mask)

    def _exit_observed(self) -> bool:
        waiter = getattr(os, "waitid", None)
        if not callable(waiter):
            raise RuntimeError("non-reaping wait is unavailable")
        try:
            result = waiter(
                os.P_PID,
                self.child.pid,
                os.WEXITED | os.WNOWAIT | os.WNOHANG,
            )
        except InterruptedError:
            return False
        return result is not None

    def _wait_once(
        self,
        timeout: float | None,
        persist_captured: Callable[[tuple[ProcessIdentity, ...]], None] | None = None,
    ) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            previous_mask = self.mask_state.block_forwarded()
            try:
                if not self.leader_exit_observed:
                    self.leader_exit_observed = self._exit_observed()
                if self.leader_exit_observed:
                    complete = self.capture_final_group()
                    if persist_captured is not None:
                        persist_captured(self.disposition().members)
                    if complete:
                        self.group_access_closed = True
                        result = self.child.wait()
                        self.leader_reaped = True
                        return result
            finally:
                self.mask_state.restore(previous_mask)
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.child.args, timeout)
            time.sleep(0.01)

    def wait_for_exit(
        self,
        check_forwarding: Callable[[], None],
        persist_captured: Callable[[tuple[ProcessIdentity, ...]], None] | None = None,
    ) -> int:
        while True:
            self.capture_group()
            if persist_captured is not None:
                persist_captured(self.disposition().members)
            try:
                result = self._wait_once(0.05, persist_captured)
            except subprocess.TimeoutExpired:
                check_forwarding()
                continue
            check_forwarding()
            return result

    def captured_identities_are_gone(self) -> bool:
        return all(
            not self._identity_live(identity) for identity in self.identities.values()
        )

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
        if (
            not self.leader_reaped
            and not self.group_access_closed
            and not self.group_term_sent
        ):
            previous_mask = self.mask_state.block_forwarded()
            try:
                self.capture_group()
                if not self.leader_reaped and not self.group_access_closed:
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
            finally:
                self.mask_state.restore(previous_mask)
        elif not self.leader_reaped and not self.group_access_closed:
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

    def close(self) -> bool:
        had_failure = self.pidfd_close_unverifiable
        opened = tuple(self.owned_pidfds)
        self.owned_pidfds.clear()
        self.pidfds.clear()
        for pidfd in reversed(opened):
            try:
                os.close(pidfd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    had_failure = True
            except BaseException:
                had_failure = True
        return not had_failure


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
        owner_generation=association.owner_generation,
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
    if process.exit_code is not None:
        event["exit_code"] = process.exit_code
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
            current = store.load_raw_process(process.wrapper.stable_key())
            if current is None or (
                current.wrapper != process.wrapper
                or current.owner_generation != process.owner_generation
            ):
                return process
            current_reasons = current.owner_reason_codes
            if current_reasons == ("association_pending",):
                current_reasons = ()
            terminal = replace(
                current,
                members=terminal.members,
                exit_code=terminal.exit_code,
                owner_reason_codes=tuple(
                    dict.fromkeys(current_reasons + terminal.owner_reason_codes)
                ),
            )
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

    def persist_captured_members(
        captured: tuple[ProcessIdentity, ...],
    ) -> None:
        nonlocal process
        known_keys = {member.stable_key() for member in process.members}
        if all(member.stable_key() in known_keys for member in captured):
            return
        try:
            with store.locked():
                current = store.load_raw_process(process.wrapper.stable_key())
                if current is None or (
                    current.wrapper != process.wrapper
                    or current.owner_generation != process.owner_generation
                ):
                    raise _PostSpawnFailure(
                        "state_capture_conflict",
                        process,
                        record_may_exist=record_may_exist,
                    )
                merged = {member.stable_key(): member for member in current.members}
                for member in captured:
                    merged.setdefault(member.stable_key(), member)
                process = replace(
                    current,
                    members=tuple(merged[key] for key in sorted(merged)),
                )
                store.save_process(process)
        except _PostSpawnFailure:
            raise
        except BaseException:
            raise _PostSpawnFailure(
                "state_capture_failed",
                process,
                record_may_exist=record_may_exist,
            ) from None

    try:
        lifecycle.capture_group()
        persist_captured_members(lifecycle.disposition().members)
        exit_code = lifecycle.wait_for_exit(
            fail_if_forwarding_failed,
            persist_captured_members,
        )
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
        captured_identities_are_gone = lifecycle.captured_identities_are_gone()
        with store.locked():
            current = store.load_raw_process(process.wrapper.stable_key())
            if current is None or (
                current.wrapper != process.wrapper
                or current.owner_generation != process.owner_generation
            ):
                raise _PostSpawnFailure(
                    "state_exit_conflict",
                    process,
                    record_may_exist=record_may_exist,
                    result=exit_code,
                )
            updated = replace(
                current,
                members=updated.members,
                exit_code=updated.exit_code,
            )
            event = _exit_event(updated, clock.wall_iso())
            if captured_identities_are_gone:
                store.transition(
                    "processes",
                    current.wrapper.stable_key(),
                    current,
                    None,
                    event,
                )
                record_may_exist = False
            else:
                store.save_process(updated)
                store.append_event(event)
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
        mask_state = _SignalMaskState.capture()
        wrapper, host_keys, spawned = _wrapper_observation(procfs, clock)
        previous, child_ref, pending = _install_signal_handlers()
    except _PersistentHandlerRestoreFailure:
        _fatal(safe_server, "handler_restore_failed")
        raise SupervisorProcessExitRequired from None
    except _StartupFailure as error:
        _fatal(safe_server, error.reason_code)
        return _FATAL_EXIT
    except BaseException:
        _fatal(safe_server, "signal_mask_unavailable")
        return _FATAL_EXIT

    child: subprocess.Popen[bytes] | None = None
    lifecycle: _OwnedGroupLifecycle | None = None
    process: ManagedProcess | None = None
    record_may_exist = False
    result = _FATAL_EXIT
    fatal_reason: str | None = None
    process_exit_required = False
    pending_handler_outcome: BaseException | None = None
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
                lifecycle = _OwnedGroupLifecycle(
                    child,
                    procfs,
                    child.pid,
                    mask_state=mask_state,
                )
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
                    lifecycle = _OwnedGroupLifecycle(
                        child,
                        procfs,
                        child.pid,
                        mask_state=mask_state,
                    )
                try:
                    disposition = lifecycle.dispose(known_members)
                except BaseException:
                    pass
                else:
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
        handler_finalization_failed = False
        mask_finalization_failed = False
        fd_finalization_failed = False
        other_finalization_failed = False
        try:
            mask_state.block_forwarded()
        except BaseException:
            mask_finalization_failed = True
        failed_handlers = _restore_signal_handlers(previous)
        forward_failed = _forwarding_failed(child_ref)
        if forward_failed and fatal_reason is None:
            fatal_reason = "signal_forward_failed"
            result = _FATAL_EXIT
        needs_disposal = (
            failed_handlers
            or forward_failed
            or (
                lifecycle is not None
                and fatal_reason is not None
                and not lifecycle.disposed
            )
        )
        if needs_disposal:
            if lifecycle is not None:
                known = () if process is None else process.members
                disposition: _ChildDisposition | None = None
                for _attempt in range(2):
                    try:
                        disposition = lifecycle.dispose(known)
                    except BaseException:
                        other_finalization_failed = True
                        if mask_state.restore_failed:
                            mask_finalization_failed = True
                        continue
                    break
                if disposition is not None:
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
                handler_finalization_failed = True
        if lifecycle is not None:
            if not lifecycle.close():
                fd_finalization_failed = True
        mask_restored, pending_handler_outcome = mask_state.restore_original()
        if not mask_restored:
            mask_finalization_failed = True
        finalization_reason: str | None = None
        if handler_finalization_failed:
            finalization_reason = "handler_restore_failed"
        elif mask_finalization_failed:
            finalization_reason = "signal_mask_restore_failed"
        elif fd_finalization_failed:
            finalization_reason = "pidfd_close_failed"
        elif other_finalization_failed:
            finalization_reason = "supervisor_finalization_failed"
        if finalization_reason is not None:
            fatal_reason = finalization_reason
            result = _FATAL_EXIT
            process_exit_required = True
    if fatal_reason is not None:
        _fatal(safe_server, fatal_reason)
    if process_exit_required:
        raise SupervisorProcessExitRequired from None
    if pending_handler_outcome is not None:
        if isinstance(pending_handler_outcome, KeyboardInterrupt):
            raise SystemExit(128 + signal.SIGINT) from None
        raise pending_handler_outcome from None
    return result
