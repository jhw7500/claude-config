from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from codex_mcp_ownership import classify, model, procfs, state, supervisor
from codex_mcp_ownership.clock import SystemClock
from helpers import FakeClock, sample_process


FAKE_MCP = Path(__file__).with_name("fixtures") / "fake_mcp.py"
PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "codex-mcp-ownership"

_SUPERVISOR_RUNNER = """
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])

from codex_mcp_ownership.clock import SystemClock
from codex_mcp_ownership.procfs import LinuxProcfs
from codex_mcp_ownership.state import StateStore
from codex_mcp_ownership.supervisor import SupervisorRequest, run_supervisor

request = SupervisorRequest(
    scope="user",
    server="fake",
    command=sys.argv[3],
    args=tuple(sys.argv[4:]),
    cwd=os.getcwd(),
)
raise SystemExit(
    run_supervisor(
        request,
        StateStore(Path(sys.argv[2])),
        LinuxProcfs(),
        SystemClock(),
    )
)
"""

_FINALIZATION_FAULT_RUNNER = """
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, sys.argv[1])

from codex_mcp_ownership.clock import SystemClock
from codex_mcp_ownership.procfs import LinuxProcfs
from codex_mcp_ownership.state import StateStore
from codex_mcp_ownership import supervisor

state_root = Path(sys.argv[2])
cwd = sys.argv[3]
fault = sys.argv[4]
trace = Path(sys.argv[5])

def record(value):
    with trace.open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")

if fault == "handler":
    original_signal = signal.signal
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }

    def fail_handler_restore(signum, handler):
        if signum == signal.SIGINT and handler == previous[signum]:
            record("handler-restore-SIGINT")
            raise OSError("handler-finalization-secret")
        return original_signal(signum, handler)

    supervisor.signal.signal = fail_handler_restore
elif fault == "mask":
    original_mask = signal.pthread_sigmask

    def fail_mask_restore(how, mask):
        if how == signal.SIG_SETMASK:
            record("mask-restore")
            raise OSError("mask-finalization-secret")
        return original_mask(how, mask)

    supervisor.signal.pthread_sigmask = fail_mask_restore
elif fault == "fd":
    original_pidfd_open = os.pidfd_open
    original_close = os.close
    tracked = set()
    interrupted = False

    def track_pidfd(pid, flags=0):
        fd = original_pidfd_open(pid, flags)
        tracked.add(fd)
        record(f"opened-{fd}")
        return fd

    def interrupt_one_close(fd):
        global interrupted
        if fd in tracked:
            record(f"close-attempt-{fd}")
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt("fd-finalization-secret")
            result = original_close(fd)
            tracked.remove(fd)
            record(f"closed-{fd}")
            return result
        return original_close(fd)

    supervisor.os.pidfd_open = track_pidfd
    supervisor.os.close = interrupt_one_close
elif fault == "pending_sigint":
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
    original_lifecycle_close = supervisor._OwnedGroupLifecycle.close
    queued = False

    def raise_from_original_handler(_signum, _frame):
        record("original-handler-SIGINT")
        raise KeyboardInterrupt("pending-sigint-secret")

    def close_then_queue_sigint(self):
        global queued
        result = original_lifecycle_close(self)
        record("owned-fds-closed")
        if not queued:
            queued = True
            os.kill(os.getpid(), signal.SIGINT)
            record("pending-SIGINT-queued")
        return result

    signal.signal(signal.SIGINT, raise_from_original_handler)
    supervisor._OwnedGroupLifecycle.close = close_then_queue_sigint
else:
    raise AssertionError("unknown fault")

child_code = "import time;time.sleep(.05)"
if fault == "fd":
    child_code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(.3)'],"
        "close_fds=True);time.sleep(.1)"
    )

request = supervisor.SupervisorRequest(
    scope="user",
    server="fake",
    command=sys.executable,
    args=("-c", child_code, "finalization-argv-secret"),
    cwd=cwd,
)
result = supervisor.run_supervisor(
    request,
    StateStore(state_root),
    LinuxProcfs(),
    SystemClock(),
    sleeper=lambda _delay: None,
)
record(f"returned-{result}")
raise SystemExit(result)
"""


@pytest.fixture
def supervisor_command(tmp_path: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        _SUPERVISOR_RUNNER,
        str(PACKAGE_ROOT),
        str(tmp_path / "state"),
    ]


def _exact_processes(store: state.StateStore) -> tuple[model.ManagedProcess, ...]:
    try:
        return store.load_processes()
    except (state.StateCorruption, state.UnsafeStatePath):
        return ()


def _require_pidfd_signaling() -> None:
    if not callable(getattr(os, "pidfd_open", None)) or not callable(
        getattr(signal, "pidfd_send_signal", None)
    ):
        pytest.skip("exact pidfd fixture cleanup is unavailable")


def _open_exact_pidfd(identity: model.ProcessIdentity) -> int | None:
    live_procfs = procfs.LinuxProcfs()
    if live_procfs.identity(identity.pid) != identity:
        return None
    try:
        pidfd = live_procfs.open_pidfd(identity)
    except OSError:
        return None
    if live_procfs.identity(identity.pid) != identity:
        os.close(pidfd)
        return None
    return pidfd


def _wait_exact_identity_gone(
    identity: model.ProcessIdentity,
    timeout: float = 2.0,
) -> None:
    live_procfs = procfs.LinuxProcfs()
    deadline = time.monotonic() + timeout
    current = live_procfs.identity(identity.pid)
    while current is not None and current.stable_key() == identity.stable_key():
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"exact fixture identity {identity.stable_key()} did not exit"
            )
        time.sleep(0.01)
        current = live_procfs.identity(identity.pid)


def _signal_exact_pidfds(
    opened: list[tuple[model.ProcessIdentity, int]],
    signum: int,
) -> None:
    sender = signal.pidfd_send_signal
    for _, pidfd in opened:
        try:
            sender(pidfd, signum, None, 0)
        except ProcessLookupError:
            pass


def _stop_exact_process(
    process: subprocess.Popen[bytes],
    store: state.StateStore,
) -> None:
    _require_pidfd_signaling()
    records = _exact_processes(store)
    identities: dict[str, model.ProcessIdentity] = {}
    for record in records:
        recorded = (() if record.child is None else (record.child,)) + record.members
        identities.update(
            {
                identity.stable_key(): identity
                for identity in recorded
                if identity.pid != process.pid
            }
        )
    opened: list[tuple[model.ProcessIdentity, int]] = []
    try:
        for identity in identities.values():
            pidfd = _open_exact_pidfd(identity)
            if pidfd is not None:
                opened.append((identity, pidfd))
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"test wrapper PID {process.pid} did not exit"
            ) from None
        _signal_exact_pidfds(opened, signal.SIGKILL)
        for identity, _ in opened:
            _wait_exact_identity_gone(identity)
    finally:
        for _, pidfd in reversed(opened):
            os.close(pidfd)


def _run_wrapper(
    command: list[str],
    store: state.StateStore,
    cwd: Path,
    *,
    input_bytes: bytes = b"",
    timeout: float = 5.0,
) -> tuple[int, bytes, bytes]:
    _require_pidfd_signaling()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        close_fds=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_bytes, timeout=timeout)
        return process.returncode, stdout, stderr
    finally:
        _stop_exact_process(process, store)


def test_supervisor_preserves_stdio_and_exit_code(
    supervisor_command: list[str],
    tmp_path: Path,
) -> None:
    returncode, stdout, _ = _run_wrapper(
        supervisor_command + [sys.executable, str(FAKE_MCP)],
        state.StateStore(tmp_path / "state"),
        tmp_path,
        input_bytes=b'{"jsonrpc":"2.0"}\n',
    )
    assert returncode == 0
    assert stdout == b'{"jsonrpc":"2.0"}\n'


def test_normal_exit_removes_process_record_and_retains_redacted_event(
    supervisor_command: list[str],
    tmp_path: Path,
) -> None:
    canary = "argv-canary-DO-NOT-PERSIST"
    store = state.StateStore(tmp_path / "state")
    returncode, stdout, stderr = _run_wrapper(
        supervisor_command + [sys.executable, "-c", "raise SystemExit(17)", canary],
        store,
        tmp_path,
    )
    records = store.load_processes()
    persisted = b"".join(
        path.read_bytes() for path in store.root.rglob("*") if path.is_file()
    )
    assert returncode == 17
    assert records == ()
    assert stdout == stderr == b""
    assert canary.encode() not in persisted + stdout + stderr
    events = [
        json.loads(line)
        for line in (store.root / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["event"] == "supervisor_child_exited"
    assert events[-1]["exit_code"] == 17
    assert events[-1]["state"] == "exiting"
    assert events[-1]["reason_codes"] == ["child_exit_observed"]


def test_live_descendant_keeps_terminal_process_record(
    supervisor_command: list[str],
    tmp_path: Path,
) -> None:
    _require_pidfd_signaling()
    descendant_pid = tmp_path / "live-descendant-pid"
    descendant_code = (
        "import os,pathlib,sys,time;"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
        "time.sleep(30)"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time;"
        "pid=pathlib.Path(sys.argv[1]);"
        "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,close_fds=True);"
        "deadline=time.monotonic()+2;"
        "exec('while not pid.exists():\\n"
        " if time.monotonic() >= deadline: raise SystemExit(91)\\n"
        " time.sleep(.01)')"
    )
    store = state.StateStore(tmp_path / "state")
    wrapper = subprocess.Popen(
        supervisor_command
        + [sys.executable, "-c", leader_code, str(descendant_pid), descendant_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        close_fds=True,
    )
    descendant_handle: tuple[model.ProcessIdentity, int] | None = None
    try:
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if descendant_pid.exists():
                identity = procfs.LinuxProcfs().identity(
                    int(descendant_pid.read_text(encoding="utf-8"))
                )
                if identity is not None:
                    pidfd = _open_exact_pidfd(identity)
                    if pidfd is not None:
                        descendant_handle = (identity, pidfd)
                        break
            time.sleep(0.01)
        assert descendant_handle is not None

        stdout, stderr = wrapper.communicate(timeout=5.0)
        records = store.load_processes()
        assert wrapper.returncode == 0
        assert stdout == stderr == b""
        assert len(records) == 1
        assert records[0].exit_code == 0
        assert descendant_handle[0].stable_key() in {
            member.stable_key() for member in records[0].members
        }
    finally:
        try:
            _stop_exact_process(wrapper, store)
        finally:
            if descendant_handle is not None:
                try:
                    _signal_exact_pidfds([descendant_handle], signal.SIGKILL)
                    _wait_exact_identity_gone(descendant_handle[0])
                finally:
                    os.close(descendant_handle[1])


def test_new_descendant_is_persisted_before_wrapper_crash(
    supervisor_command: list[str],
    tmp_path: Path,
) -> None:
    _require_pidfd_signaling()
    release = tmp_path / "release-descendant"
    descendant_pid = tmp_path / "descendant-pid"
    descendant_ready = tmp_path / "descendant-ready"
    descendant_code = (
        "import os,pathlib,sys,time;"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
        "pathlib.Path(sys.argv[2]).write_text('ready');"
        "time.sleep(30)"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time\n"
        "release=pathlib.Path(sys.argv[1])\n"
        "while not release.exists(): time.sleep(.01)\n"
        "subprocess.Popen([sys.executable,'-c',sys.argv[2],*sys.argv[3:5]],"
        "close_fds=True)\n"
        "time.sleep(30)\n"
    )
    store = state.StateStore(tmp_path / "state")
    wrapper = subprocess.Popen(
        supervisor_command
        + [
            sys.executable,
            "-c",
            leader_code,
            str(release),
            descendant_code,
            str(descendant_pid),
            str(descendant_ready),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        close_fds=True,
    )
    leader_handle: tuple[model.ProcessIdentity, int] | None = None
    descendant_handle: tuple[model.ProcessIdentity, int] | None = None
    try:
        deadline = time.monotonic() + 4.0
        initial: model.ManagedProcess | None = None
        while time.monotonic() < deadline:
            records = _exact_processes(store)
            if records and records[0].child is not None:
                initial = records[0]
                break
            time.sleep(0.01)
        assert initial is not None
        assert initial.child is not None
        while time.monotonic() < deadline:
            pidfd = _open_exact_pidfd(initial.child)
            if pidfd is not None:
                leader_handle = (initial.child, pidfd)
                break
            time.sleep(0.01)
        assert leader_handle is not None

        release.write_text("release", encoding="utf-8")
        while time.monotonic() < deadline:
            if descendant_ready.exists() and descendant_pid.exists():
                identity = procfs.LinuxProcfs().identity(
                    int(descendant_pid.read_text(encoding="utf-8"))
                )
                if identity is not None:
                    pidfd = _open_exact_pidfd(identity)
                    if pidfd is not None:
                        descendant_handle = (identity, pidfd)
                        break
            time.sleep(0.01)
        assert descendant_handle is not None

        descendant_identity = descendant_handle[0]
        while time.monotonic() < deadline:
            records = _exact_processes(store)
            if records and descendant_identity.stable_key() in {
                member.stable_key() for member in records[0].members
            }:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("late descendant was not persisted while supervised")

        wrapper.kill()
        wrapper.wait(timeout=2.0)
        persisted = store.load_processes()
        assert len(persisted) == 1
        assert descendant_identity.stable_key() in {
            member.stable_key() for member in persisted[0].members
        }
    finally:
        try:
            _stop_exact_process(wrapper, store)
        finally:
            cleanup_deadline = time.monotonic() + 2.0
            while leader_handle is None and time.monotonic() < cleanup_deadline:
                if initial is not None and initial.child is not None:
                    pidfd = _open_exact_pidfd(initial.child)
                    if pidfd is not None:
                        leader_handle = (initial.child, pidfd)
                        break
                time.sleep(0.01)
            while descendant_handle is None and time.monotonic() < cleanup_deadline:
                if descendant_pid.exists():
                    identity = procfs.LinuxProcfs().identity(
                        int(descendant_pid.read_text(encoding="utf-8"))
                    )
                    if identity is not None:
                        pidfd = _open_exact_pidfd(identity)
                        if pidfd is not None:
                            descendant_handle = (identity, pidfd)
                            break
                time.sleep(0.01)
            fixture_handles = [
                handle
                for handle in (leader_handle, descendant_handle)
                if handle is not None
            ]
            try:
                _signal_exact_pidfds(fixture_handles, signal.SIGKILL)
                for identity, _ in fixture_handles:
                    _wait_exact_identity_gone(identity)
            finally:
                for _, pidfd in reversed(fixture_handles):
                    os.close(pidfd)


def _active_lease(
    session_id: str,
    cwd: Path,
    observed: model.ObservedTime,
) -> model.SessionLease:
    identity = procfs.LinuxProcfs().identity(os.getpid())
    assert identity is not None
    return model.SessionLease(
        schema_version=1,
        session_id=session_id,
        cwd=str(cwd),
        source="SessionStart",
        host_keys=(identity.stable_key(),),
        state="active",
        observed=observed,
    )


def test_unique_session_lease_is_persisted_as_owner(
    supervisor_command: list[str],
    tmp_path: Path,
) -> None:
    store = state.StateStore(tmp_path / "state")
    clock = SystemClock()
    boot_id = procfs.LinuxProcfs().boot_id()
    assert boot_id is not None
    lease = _active_lease(
        "session:unique",
        tmp_path,
        model.ObservedTime(clock.wall_iso(), boot_id, clock.boottime()),
    )
    store.save_session(lease)
    returncode, _, _ = _run_wrapper(
        supervisor_command + [sys.executable, "-c", "pass"],
        store,
        tmp_path,
    )
    events = [
        json.loads(line)
        for line in (store.root / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    reconciled = next(event for event in events if event["event"] == "owner_reconciled")
    assert returncode == 0
    assert reconciled["state"] == "session"
    assert reconciled["session_id"] == lease.session_id
    assert reconciled["reason_codes"] == ["unique_matching_session"]
    assert store.load_processes() == ()


def test_two_matching_leases_persist_ambiguous_unknown(
    supervisor_command: list[str],
    tmp_path: Path,
) -> None:
    store = state.StateStore(tmp_path / "state")
    clock = SystemClock()
    boot_id = procfs.LinuxProcfs().boot_id()
    assert boot_id is not None
    observed = model.ObservedTime(clock.wall_iso(), boot_id, clock.boottime())
    first = _active_lease("session:first", tmp_path, observed)
    store.save_session(first)
    store.save_session(replace(first, session_id="session:second"))
    returncode, _, _ = _run_wrapper(
        supervisor_command + [sys.executable, "-c", "pass"],
        store,
        tmp_path,
    )
    events = [
        json.loads(line)
        for line in (store.root / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    reconciled = next(event for event in events if event["event"] == "owner_reconciled")
    assert returncode == 0
    assert reconciled["state"] == "unknown"
    assert "session_id" not in reconciled
    assert reconciled["reason_codes"] == ["multiple_matching_sessions"]
    assert store.load_processes() == ()


def test_reconciliation_reloads_leases_and_uses_bounded_retry_delays(
    tmp_path: Path,
) -> None:
    live_procfs = procfs.LinuxProcfs()
    wrapper = live_procfs.identity(os.getpid())
    chain = live_procfs.ancestor_chain(os.getpid())
    assert wrapper is not None
    assert chain and chain[0] == wrapper
    host_keys = frozenset(identity.stable_key() for identity in chain[1:])
    assert host_keys
    clock = FakeClock(boot=100.0)
    observed = model.ObservedTime(clock.wall_iso(), wrapper.boot_id, clock.boottime())
    managed = model.ManagedProcess(
        schema_version=1,
        record_id=wrapper.stable_key(),
        scope="user",
        server="fake",
        cwd=str(tmp_path),
        wrapper=wrapper,
        child=None,
        members=(wrapper,),
        pgid=wrapper.pgid,
        host_keys=host_keys,
        spawned=observed,
        owner_reason_codes=("association_pending",),
    )
    store = state.StateStore(tmp_path / "state")
    lease = model.SessionLease(
        schema_version=1,
        session_id="session:late",
        cwd=str(tmp_path),
        source="SessionStart",
        host_keys=(next(iter(host_keys)),),
        state="active",
        observed=observed,
    )
    delays: list[float] = []

    def add_lease_after_first_attempt(delay: float) -> None:
        delays.append(delay)
        store.save_session(lease)

    reconciled = supervisor._reconcile_owner(
        managed,
        store,
        clock,
        add_lease_after_first_attempt,
    )
    assert reconciled.owner_session_id == lease.session_id
    assert delays == [0.05]
    assert store.load_processes() == (reconciled,)


def test_unknown_reconciliation_uses_all_bounded_retry_delays(
    tmp_path: Path,
) -> None:
    live_procfs = procfs.LinuxProcfs()
    wrapper = live_procfs.identity(os.getpid())
    assert wrapper is not None
    observed = model.ObservedTime(
        "2026-08-29T00:00:00+00:00",
        wrapper.boot_id,
        100.0,
    )
    managed = model.ManagedProcess(
        schema_version=1,
        record_id=wrapper.stable_key(),
        scope="user",
        server="fake",
        cwd=str(tmp_path),
        wrapper=wrapper,
        child=None,
        members=(wrapper,),
        pgid=wrapper.pgid,
        host_keys=frozenset(),
        spawned=observed,
        owner_reason_codes=("association_pending",),
    )
    store = state.StateStore(tmp_path / "state")
    delays: list[float] = []
    reconciled = supervisor._reconcile_owner(
        managed,
        store,
        FakeClock(boot=100.0),
        delays.append,
    )
    assert delays == [0.05, 0.1, 0.2, 0.4, 0.8]
    assert reconciled.owner_reason_codes == ("no_matching_session",)
    assert store.load_processes() == (reconciled,)


def test_shared_reconciliation_stops_immediately_without_loading_leases_again(
    tmp_path: Path,
) -> None:
    live_procfs = procfs.LinuxProcfs()
    wrapper = live_procfs.identity(os.getpid())
    assert wrapper is not None
    managed = model.ManagedProcess(
        schema_version=1,
        record_id=wrapper.stable_key(),
        scope="user",
        server="fake",
        cwd=str(tmp_path),
        wrapper=wrapper,
        child=None,
        members=(wrapper,),
        pgid=wrapper.pgid,
        host_keys=frozenset(),
        spawned=model.ObservedTime(
            "2026-08-29T00:00:00+00:00",
            wrapper.boot_id,
            100.0,
        ),
        shared_owner="user:fake",
        owner_reason_codes=("association_pending",),
    )
    store = state.StateStore(tmp_path / "state")

    def forbidden_sleep(_delay: float) -> None:
        raise AssertionError("shared owner must not retry")

    reconciled = supervisor._reconcile_owner(
        managed,
        store,
        FakeClock(boot=100.0),
        forbidden_sleep,
    )
    assert reconciled.shared_owner == "user:fake"
    assert reconciled.owner_reason_codes == ("explicit_shared_owner",)


def test_corrupt_lease_evidence_retries_all_observations_and_stays_unknown(
    tmp_path: Path,
) -> None:
    live_procfs = procfs.LinuxProcfs()
    wrapper = live_procfs.identity(os.getpid())
    assert wrapper is not None
    managed = model.ManagedProcess(
        schema_version=1,
        record_id=wrapper.stable_key(),
        scope="user",
        server="fake",
        cwd=str(tmp_path),
        wrapper=wrapper,
        child=None,
        members=(wrapper,),
        pgid=wrapper.pgid,
        host_keys=frozenset(),
        spawned=model.ObservedTime(
            "2026-08-29T00:00:00+00:00",
            wrapper.boot_id,
            100.0,
        ),
        owner_reason_codes=("association_pending",),
    )
    store = state.StateStore(tmp_path / "state")
    store.save_session(
        model.SessionLease(
            1,
            "session:corrupt",
            str(tmp_path),
            "SessionStart",
            (),
            "active",
            managed.spawned,
        )
    )
    session_file = next((store.root / "sessions").iterdir())
    session_file.write_bytes(b"{corrupt}\n")
    session_file.chmod(0o600)
    delays: list[float] = []
    reconciled = supervisor._reconcile_owner(
        managed,
        store,
        FakeClock(boot=100.0),
        delays.append,
    )
    assert delays == [0.05, 0.1, 0.2, 0.4, 0.8]
    assert reconciled.owner_session_id is None
    assert reconciled.owner_reason_codes == ("corrupt_session_state",)
    assert store.load_processes() == (reconciled,)


def test_temporarily_unavailable_lease_evidence_can_later_associate_exact_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_procfs = procfs.LinuxProcfs()
    wrapper = live_procfs.identity(os.getpid())
    chain = live_procfs.ancestor_chain(os.getpid())
    assert wrapper is not None
    assert chain and chain[0] == wrapper
    host_keys = frozenset(identity.stable_key() for identity in chain[1:])
    observed = model.ObservedTime(
        "2026-08-29T00:00:00+00:00",
        wrapper.boot_id,
        100.0,
    )
    managed = model.ManagedProcess(
        schema_version=1,
        record_id=wrapper.stable_key(),
        scope="user",
        server="fake",
        cwd=str(tmp_path),
        wrapper=wrapper,
        child=None,
        members=(wrapper,),
        pgid=wrapper.pgid,
        host_keys=host_keys,
        spawned=observed,
        owner_reason_codes=("association_pending",),
    )
    lease = model.SessionLease(
        schema_version=1,
        session_id="session:after-unavailable",
        cwd=str(tmp_path),
        source="SessionStart",
        host_keys=(next(iter(host_keys)),),
        state="active",
        observed=observed,
    )
    store = state.StateStore(tmp_path / "state")
    original_load = store.load_sessions
    loads = 0

    def temporarily_unavailable() -> tuple[model.SessionLease, ...]:
        nonlocal loads
        loads += 1
        if loads == 1:
            raise state.StateLockTimeout("temporary-secret")
        return original_load()

    def add_lease(_delay: float) -> None:
        store.save_session(lease)

    monkeypatch.setattr(store, "load_sessions", temporarily_unavailable)
    reconciled = supervisor._reconcile_owner(
        managed,
        store,
        FakeClock(boot=100.0),
        add_lease,
    )
    assert loads == 2
    assert reconciled.owner_session_id == lease.session_id
    assert reconciled.owner_reason_codes == ("unique_matching_session",)


def test_temporarily_unavailable_lock_retries_and_later_exact_owner_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_procfs = procfs.LinuxProcfs()
    wrapper = live_procfs.identity(os.getpid())
    chain = live_procfs.ancestor_chain(os.getpid())
    assert wrapper is not None
    assert chain and chain[0] == wrapper
    host_keys = frozenset(identity.stable_key() for identity in chain[1:])
    observed = model.ObservedTime(
        "2026-08-29T00:00:00+00:00",
        wrapper.boot_id,
        100.0,
    )
    managed = model.ManagedProcess(
        schema_version=1,
        record_id=wrapper.stable_key(),
        scope="user",
        server="fake",
        cwd=str(tmp_path),
        wrapper=wrapper,
        child=None,
        members=(wrapper,),
        pgid=wrapper.pgid,
        host_keys=host_keys,
        spawned=observed,
        owner_reason_codes=("association_pending",),
    )
    lease = model.SessionLease(
        schema_version=1,
        session_id="session:after-lock-unavailable",
        cwd=str(tmp_path),
        source="SessionStart",
        host_keys=(next(iter(host_keys)),),
        state="active",
        observed=observed,
    )
    store = state.StateStore(tmp_path / "state")
    store.save_session(lease)
    original_locked = store.locked
    observations = 0

    @contextmanager
    def temporarily_unavailable_lock():
        nonlocal observations
        observations += 1
        if observations == 1:
            raise state.StateLockTimeout("temporary-secret")
        with original_locked():
            yield store

    monkeypatch.setattr(store, "locked", temporarily_unavailable_lock)
    delays: list[float] = []
    reconciled = supervisor._reconcile_owner(
        managed,
        store,
        FakeClock(boot=100.0),
        delays.append,
    )
    assert delays == [0.05]
    assert reconciled.owner_session_id == lease.session_id
    assert reconciled.owner_reason_codes == ("unique_matching_session",)


def test_wrapper_sigterm_reaches_only_its_child_group(
    supervisor_command: list[str],
    tmp_path: Path,
) -> None:
    _require_pidfd_signaling()
    marker = tmp_path / "term-received"
    ready = tmp_path / "child-ready"
    child_code = (
        "import pathlib,signal,sys,time;"
        "marker=pathlib.Path(sys.argv[1]);ready=pathlib.Path(sys.argv[2]);"
        "signal.signal(signal.SIGTERM,lambda *_:(marker.write_text('term'),sys.exit(0)));"
        "ready.write_text('ready');"
        "time.sleep(30)"
    )
    store = state.StateStore(tmp_path / "state")
    process = subprocess.Popen(
        supervisor_command
        + [sys.executable, "-c", child_code, str(marker), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        close_fds=True,
    )
    child_pgid: int | None = None
    try:
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            records = _exact_processes(store)
            if ready.exists() and records and records[0].child is not None:
                child_pgid = records[0].pgid
                break
            time.sleep(0.01)
        assert child_pgid is not None
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=4.0)
        assert marker.read_text(encoding="utf-8") == "term"
    finally:
        _stop_exact_process(process, store)


def test_exited_wrapper_converges_to_zero_managed_processes(
    supervisor_command: list[str],
    tmp_path: Path,
) -> None:
    store = state.StateStore(tmp_path / "state")
    returncode, _, _ = _run_wrapper(
        supervisor_command + [sys.executable, "-c", "pass"],
        store,
        tmp_path,
    )
    audit = classify.build_audit(
        store,
        procfs.LinuxProcfs(),
        SystemClock(),
    )
    assert returncode == 0
    assert store.load_processes() == ()
    assert audit.process_count == 0
    assert audit.rss_kib == 0


def test_spawn_failure_redacts_command_and_uses_stable_reason(
    supervisor_command: list[str],
    tmp_path: Path,
) -> None:
    canary = "missing-command-secret-canary"
    store = state.StateStore(tmp_path / "state")
    returncode, stdout, stderr = _run_wrapper(
        supervisor_command + [str(tmp_path / canary)],
        store,
        tmp_path,
    )
    assert returncode != 0
    assert stdout == b""
    assert stderr == b"codex-mcp-supervisor: server=fake reason=child_spawn_failed\n"
    assert canary.encode() not in stderr
    assert store.load_processes() == ()


def test_invalid_server_is_rejected_before_spawn_without_echoing_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def forbidden_popen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Popen must not be called")

    monkeypatch.setattr(supervisor.subprocess, "Popen", forbidden_popen)
    result = supervisor.run_supervisor(
        supervisor.SupervisorRequest(
            scope="user",
            server="unsafe\nserver-secret",
            command="command-secret",
            args=("argument-secret",),
            cwd=str(tmp_path),
        ),
        state.StateStore(tmp_path / "state"),
        procfs.LinuxProcfs(),
        FakeClock(),
    )
    captured = capsys.readouterr()
    assert result == 70
    assert not called
    assert captured.out == ""
    assert captured.err == (
        "codex-mcp-supervisor: server=<invalid> reason=invalid_request\n"
    )
    assert "secret" not in captured.err
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("field", ["scope", "server"])
@pytest.mark.parametrize(
    "rejected",
    [
        "unsafe label",
        "unsafe=label",
        'unsafe"label',
        "unsafe\tlabel",
        "안전하지않음",
    ],
)
def test_unsafe_labels_are_rejected_by_bounded_ascii_grammar_without_echo(
    field: str,
    rejected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawned = False

    def forbidden_popen(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("Popen must not be called")

    monkeypatch.setattr(supervisor.subprocess, "Popen", forbidden_popen)
    request_values = {
        "scope": "user",
        "server": "fake",
    }
    request_values[field] = rejected
    result = supervisor.run_supervisor(
        supervisor.SupervisorRequest(
            scope=request_values["scope"],
            server=request_values["server"],
            command="command-secret",
            args=("argument-secret",),
            cwd=str(tmp_path),
        ),
        state.StateStore(tmp_path / "state"),
        procfs.LinuxProcfs(),
        FakeClock(),
    )
    captured = capsys.readouterr()
    assert result == 70
    assert not spawned
    assert captured.out == ""
    assert captured.err == (
        "codex-mcp-supervisor: server=<invalid> reason=invalid_request\n"
    )
    assert rejected not in captured.err
    assert not (tmp_path / "state").exists()


def _capture_real_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> list[subprocess.Popen[bytes]]:
    _require_pidfd_signaling()
    original = subprocess.Popen
    spawned: list[subprocess.Popen[bytes]] = []

    def capture(*args, **kwargs):
        child = original(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", capture)
    return spawned


def _kill_captured_children(children: list[subprocess.Popen[bytes]]) -> None:
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2.0)


def test_state_failure_after_spawn_reaps_exact_child_and_restores_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    children = _capture_real_popen(monkeypatch)
    store = state.StateStore(tmp_path / "state")

    def fail_save(_process: model.ManagedProcess) -> None:
        raise OSError("state-secret")

    monkeypatch.setattr(store, "save_process", fail_save)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "import time; time.sleep(30)", "argv-secret"),
                str(tmp_path),
            ),
            store,
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        assert result == 70
        assert len(children) == 1
        assert children[0].poll() is not None
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=state_spawn_failed\n"
        )
        assert "secret" not in captured.err
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        _kill_captured_children(children)


def test_stale_terminal_best_effort_never_replaces_a_newer_generation(
    tmp_path,
) -> None:
    store = state.StateStore(tmp_path / "state")
    base = sample_process()
    stale = replace(base, owner_generation="1" * 64)
    current = replace(
        base,
        owner_generation="2" * 64,
        owner_reason_codes=("new_generation",),
    )
    store.save_process(current)

    supervisor._persist_terminal_best_effort(
        stale,
        supervisor._ChildDisposition(23, ()),
        "state_exit_failed",
        store,
        record_may_exist=True,
    )

    assert store.load_process(base.wrapper.stable_key()) == current


def test_state_and_transient_restore_failure_dispose_owned_group_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    original_signal = signal.signal
    original_forward = supervisor.forward_signal
    restore_failed_once = False
    group_terms: list[tuple[int, int]] = []
    children = _capture_real_popen(monkeypatch)
    store = state.StateStore(tmp_path / "state")

    def fail_save(_process: model.ManagedProcess) -> None:
        raise OSError("state-secret")

    def fail_first_sigint_restore(signum, handler):
        nonlocal restore_failed_once
        if (
            signum == signal.SIGINT
            and handler == previous[signum]
            and not restore_failed_once
        ):
            restore_failed_once = True
            raise OSError("restore-secret")
        return original_signal(signum, handler)

    def record_group_term(pgid: int, signum: int) -> None:
        group_terms.append((pgid, signum))
        original_forward(pgid, signum)

    monkeypatch.setattr(store, "save_process", fail_save)
    monkeypatch.setattr(supervisor.signal, "signal", fail_first_sigint_restore)
    monkeypatch.setattr(supervisor, "forward_signal", record_group_term)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "import time; time.sleep(30)", "argv-secret"),
                str(tmp_path),
            ),
            store,
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        assert result == 70
        assert len(group_terms) == 1
        assert group_terms[0][1] == signal.SIGTERM
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=state_spawn_failed\n"
        )
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        _kill_captured_children(children)


def test_wait_exception_reaps_exact_child_preserves_record_and_restores_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def flaky_popen(*args, **kwargs):
        child = original(*args, **kwargs)
        original_wait = child.wait
        calls = 0

        def flaky_wait(*wait_args, **wait_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt("wait-secret")
            return original_wait(*wait_args, **wait_kwargs)

        child.wait = flaky_wait  # type: ignore[method-assign]
        children.append(child)
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", flaky_popen)
    store = state.StateStore(tmp_path / "state")
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "import time; time.sleep(30)", "argv-secret"),
                str(tmp_path),
            ),
            store,
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        assert result == 70
        assert children[0].poll() is not None
        assert len(store.load_processes()) == 1
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=child_wait_failed\n"
        )
        assert "secret" not in captured.err
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        _kill_captured_children(children)


def test_handler_install_failure_restores_partial_install_and_never_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    original_signal = signal.signal
    spawned = False

    def fail_second_handler(signum, handler):
        if signum == signal.SIGINT:
            raise ValueError("handler-secret")
        return original_signal(signum, handler)

    def forbidden_popen(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("child must not spawn")

    monkeypatch.setattr(supervisor.signal, "signal", fail_second_handler)
    monkeypatch.setattr(supervisor.subprocess, "Popen", forbidden_popen)
    result = supervisor.run_supervisor(
        supervisor.SupervisorRequest(
            "user",
            "fake",
            sys.executable,
            ("-c", "pass", "argv-secret"),
            str(tmp_path),
        ),
        state.StateStore(tmp_path / "state"),
        procfs.LinuxProcfs(),
        FakeClock(boot=100.0),
    )
    captured = capsys.readouterr()
    assert result == 70
    assert not spawned
    assert captured.err == (
        "codex-mcp-supervisor: server=fake reason=handler_install_failed\n"
    )
    assert "secret" not in captured.err
    assert {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    } == previous


def test_install_rollback_restore_failure_uses_stable_reason_and_never_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    original_signal = signal.signal
    attempts: list[tuple[str, int]] = []
    spawned = False
    failed_once = False

    def fail_install_then_restore(signum, handler):
        nonlocal failed_once
        if signum == signal.SIGINT and handler != previous[signum]:
            attempts.append(("install", signum))
            raise ValueError("install-secret")
        if handler == previous[signum]:
            attempts.append(("restore", signum))
            if signum == signal.SIGTERM and not failed_once:
                failed_once = True
                raise OSError("restore-secret")
            return original_signal(signum, handler)
        attempts.append(("install", signum))
        return original_signal(signum, handler)

    def forbidden_popen(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("child must not spawn")

    monkeypatch.setattr(supervisor.signal, "signal", fail_install_then_restore)
    monkeypatch.setattr(supervisor.subprocess, "Popen", forbidden_popen)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "pass", "argv-secret"),
                str(tmp_path),
            ),
            state.StateStore(tmp_path / "state"),
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
        )
        captured = capsys.readouterr()
        assert result == 70
        assert not spawned
        assert attempts == [
            ("install", signal.SIGTERM),
            ("install", signal.SIGINT),
            ("restore", signal.SIGTERM),
            ("restore", signal.SIGTERM),
        ]
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=handler_install_failed\n"
        )
        assert "secret" not in captured.err
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)


@pytest.mark.parametrize("failed_restore", [signal.SIGHUP, signal.SIGINT])
def test_transient_final_handler_restore_failure_is_retried_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed_restore: int,
) -> None:
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    original_signal = signal.signal
    restore_attempts: list[int] = []
    group_terms: list[tuple[int, int]] = []
    failed_once = False
    original_forward = supervisor.forward_signal

    def fail_one_restore(signum, handler):
        nonlocal failed_once
        if handler == previous[signum]:
            restore_attempts.append(signum)
            if signum == failed_restore and not failed_once:
                failed_once = True
                raise OSError("restore-secret")
        return original_signal(signum, handler)

    children = _capture_real_popen(monkeypatch)

    def record_group_term(pgid: int, signum: int) -> None:
        group_terms.append((pgid, signum))
        original_forward(pgid, signum)

    monkeypatch.setattr(supervisor.signal, "signal", fail_one_restore)
    monkeypatch.setattr(supervisor, "forward_signal", record_group_term)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "pass", "argv-secret"),
                str(tmp_path),
            ),
            state.StateStore(tmp_path / "state"),
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        assert result == 0
        assert restore_attempts == [
            signal.SIGHUP,
            signal.SIGINT,
            signal.SIGTERM,
            failed_restore,
        ]
        assert captured.err == ""
        assert group_terms == []
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        _kill_captured_children(children)


def test_forward_failure_during_final_handler_restore_is_not_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    original_signal = signal.signal
    original_forward = supervisor._OwnedGroupLifecycle.forward
    installed: dict[int, signal.Handlers] = {}
    injected = False

    def capture_and_inject(signum, handler):
        nonlocal injected
        if handler != previous[signum]:
            installed[signum] = handler
        elif not injected:
            injected = True
            installed[signal.SIGTERM](signal.SIGTERM, None)
        return original_signal(signum, handler)

    def fail_injected_forward(self, signum: int) -> None:
        if injected:
            raise OSError("forward-secret")
        original_forward(self, signum)

    children = _capture_real_popen(monkeypatch)
    monkeypatch.setattr(supervisor.signal, "signal", capture_and_inject)
    monkeypatch.setattr(
        supervisor._OwnedGroupLifecycle,
        "forward",
        fail_injected_forward,
    )
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "pass", "argv-secret"),
                str(tmp_path),
            ),
            state.StateStore(tmp_path / "state"),
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        assert result == 70
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=signal_forward_failed\n"
        )
        assert "secret" not in captured.err
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        _kill_captured_children(children)


def test_post_reap_exact_forward_failure_becomes_sticky_and_cleans_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_pidfd_signaling()
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    original_signal = signal.signal
    original_pidfd_send = signal.pidfd_send_signal
    descendant_pid = tmp_path / "exact-forward-descendant-pid"
    descendant_ready = tmp_path / "exact-forward-descendant-ready"
    descendant_term = tmp_path / "exact-forward-descendant-term"
    descendant_code = (
        "import os,pathlib,signal,sys,time;"
        "pid=pathlib.Path(sys.argv[1]);ready=pathlib.Path(sys.argv[2]);"
        "term=pathlib.Path(sys.argv[3]);pid.write_text(str(os.getpid()));"
        "signal.signal(signal.SIGTERM,lambda *_:(term.write_text('term'),sys.exit(0)));"
        "ready.write_text('ready');time.sleep(30)"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time\n"
        "ready=pathlib.Path(sys.argv[3])\n"
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],*sys.argv[2:5]],"
        "close_fds=True)\n"
        "while not ready.exists(): time.sleep(.01)\n"
    )
    children = _capture_real_popen(monkeypatch)
    descendant_handles: list[tuple[model.ProcessIdentity, int]] = []
    capture_failures: list[str] = []
    capture_calls = [0, 0]  # group_members calls, calls that saw a ready descendant
    installed: dict[int, signal.Handlers] = {}
    injected = False
    fail_exact_send = False

    class CaptureDescendantProcfs(procfs.LinuxProcfs):
        def group_members(self, pgid: int):
            members = super().group_members(pgid)
            capture_calls[0] += 1
            if descendant_ready.exists() and not descendant_handles:
                capture_calls[1] += 1
                # The supervisor wraps this call in `except BaseException`, so an
                # assertion raised here is swallowed and the only symptom left is
                # an empty handle list. Record why instead of raising into that void.
                try:
                    raw = descendant_pid.read_text(encoding="utf-8")
                    identity = self.identity(int(raw))
                    if identity is None:
                        capture_failures.append(f"identity=None pid={raw!r}")
                    else:
                        pidfd = _open_exact_pidfd(identity)
                        if pidfd is None:
                            capture_failures.append(f"pidfd=None identity={identity!r}")
                        else:
                            descendant_handles.append((identity, pidfd))
                except Exception as error:
                    capture_failures.append(f"{type(error).__name__}: {error}")
            return members

    def capture_and_inject(signum, handler):
        nonlocal fail_exact_send, injected
        if handler != previous[signum]:
            installed[signum] = handler
        elif not injected:
            injected = True
            fail_exact_send = True
            installed[signal.SIGTERM](signal.SIGTERM, None)
            fail_exact_send = False
        return original_signal(signum, handler)

    def fail_one_exact_send(pidfd, signum, siginfo, flags):
        if fail_exact_send:
            raise PermissionError("exact-forward-secret")
        return original_pidfd_send(pidfd, signum, siginfo, flags)

    monkeypatch.setattr(supervisor.signal, "signal", capture_and_inject)
    monkeypatch.setattr(supervisor.signal, "pidfd_send_signal", fail_one_exact_send)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                (
                    "-c",
                    leader_code,
                    descendant_code,
                    str(descendant_pid),
                    str(descendant_ready),
                    str(descendant_term),
                ),
                str(tmp_path),
            ),
            state.StateStore(tmp_path / "state"),
            CaptureDescendantProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        assert result == 70
        assert len(descendant_handles) == 1, (
            "descendant capture failed: "
            f"group_members_calls={capture_calls[0]} "
            f"calls_with_ready={capture_calls[1]} "
            f"ready_exists={descendant_ready.exists()} "
            f"pid_exists={descendant_pid.exists()} "
            f"failures={capture_failures}"
        )
        assert descendant_term.read_text(encoding="utf-8") == "term"
        _wait_exact_identity_gone(descendant_handles[0][0])
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=signal_forward_failed\n"
        )
        assert "secret" not in captured.err
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        if not descendant_handles and descendant_pid.exists():
            identity = procfs.LinuxProcfs().identity(
                int(descendant_pid.read_text(encoding="utf-8"))
            )
            if identity is not None:
                pidfd = _open_exact_pidfd(identity)
                if pidfd is not None:
                    descendant_handles.append((identity, pidfd))
        _signal_exact_pidfds(descendant_handles, signal.SIGKILL)
        for identity, _ in descendant_handles:
            _wait_exact_identity_gone(identity)
        for _, pidfd in descendant_handles:
            os.close(pidfd)
        _kill_captured_children(children)


def _run_finalization_fault(
    tmp_path: Path,
    fault: str,
) -> tuple[subprocess.CompletedProcess[bytes], tuple[str, ...]]:
    _require_pidfd_signaling()
    trace = tmp_path / f"{fault}-trace"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _FINALIZATION_FAULT_RUNNER,
            str(PACKAGE_ROOT),
            str(tmp_path / "state"),
            str(tmp_path),
            fault,
            str(trace),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        close_fds=True,
        check=False,
        timeout=5.0,
    )
    lines = (
        tuple(trace.read_text(encoding="utf-8").splitlines()) if trace.exists() else ()
    )
    return completed, lines


def test_persistent_handler_restore_exits_cleanly_without_manual_repair(
    tmp_path: Path,
) -> None:
    completed, trace = _run_finalization_fault(tmp_path, "handler")

    assert completed.returncode == 70
    assert completed.stdout == b""
    assert completed.stderr == (
        b"codex-mcp-supervisor: server=fake reason=handler_restore_failed\n"
    )
    assert b"Traceback" not in completed.stderr
    assert trace.count("handler-restore-SIGINT") >= 2


def test_persistent_signal_mask_restore_exits_after_verified_retries(
    tmp_path: Path,
) -> None:
    completed, trace = _run_finalization_fault(tmp_path, "mask")

    assert completed.returncode == 70
    assert completed.stdout == b""
    assert completed.stderr == (
        b"codex-mcp-supervisor: server=fake reason=signal_mask_restore_failed\n"
    )
    assert b"Traceback" not in completed.stderr
    assert trace.count("mask-restore") >= 2


def test_interrupted_pidfd_close_attempts_every_fd_then_exits_stably(
    tmp_path: Path,
) -> None:
    completed, trace = _run_finalization_fault(tmp_path, "fd")
    opened = {
        line.removeprefix("opened-") for line in trace if line.startswith("opened-")
    }
    closed = {
        line.removeprefix("closed-") for line in trace if line.startswith("closed-")
    }
    attempted = [
        line.removeprefix("close-attempt-")
        for line in trace
        if line.startswith("close-attempt-")
    ]

    assert completed.returncode == 70
    assert completed.stdout == b""
    assert completed.stderr == (
        b"codex-mcp-supervisor: server=fake reason=pidfd_close_failed\n"
    )
    assert b"Traceback" not in completed.stderr
    assert opened
    assert opened == set(attempted)
    assert all(attempted.count(fd) == 1 for fd in opened)
    assert len(closed) == len(opened) - 1


def test_pending_sigint_original_handler_exits_130_after_owned_cleanup(
    tmp_path: Path,
) -> None:
    completed, trace = _run_finalization_fault(tmp_path, "pending_sigint")

    assert completed.returncode == 130
    assert completed.stdout == b""
    assert completed.stderr == b""
    assert trace == (
        "owned-fds-closed",
        "pending-SIGINT-queued",
        "original-handler-SIGINT",
    )
    assert not any(line.startswith("returned-") for line in trace)
    assert b"Traceback" not in completed.stderr
    assert b"secret" not in completed.stderr


def test_child_identity_failure_reaps_spawned_child_and_restores_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ChildIdentityUnavailable(procfs.LinuxProcfs):
        def ancestor_chain(self, pid: int):
            return procfs.LinuxProcfs().ancestor_chain(pid)

        def identity(self, pid: int):
            if pid == os.getpid():
                return super().identity(pid)
            raise RuntimeError("identity-secret")

    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    children = _capture_real_popen(monkeypatch)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "import time; time.sleep(30)", "argv-secret"),
                str(tmp_path),
            ),
            state.StateStore(tmp_path / "state"),
            ChildIdentityUnavailable(),
            FakeClock(boot=100.0),
        )
        captured = capsys.readouterr()
        assert result == 70
        assert len(children) == 1
        assert children[0].poll() is not None
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=child_identity_unavailable\n"
        )
        assert "secret" not in captured.err
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        _kill_captured_children(children)


def test_popen_failure_restores_handlers_and_redacts_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }

    def fail_popen(*args, **kwargs):
        raise OSError("popen-secret")

    monkeypatch.setattr(supervisor.subprocess, "Popen", fail_popen)
    result = supervisor.run_supervisor(
        supervisor.SupervisorRequest(
            "user",
            "fake",
            "command-secret",
            ("argument-secret",),
            str(tmp_path),
        ),
        state.StateStore(tmp_path / "state"),
        procfs.LinuxProcfs(),
        FakeClock(boot=100.0),
    )
    captured = capsys.readouterr()
    assert result == 70
    assert captured.err == (
        "codex-mcp-supervisor: server=fake reason=child_spawn_failed\n"
    )
    assert "secret" not in captured.err
    assert {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    } == previous


def test_exit_event_append_failure_recovers_after_atomic_process_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    children = _capture_real_popen(monkeypatch)
    store = state.StateStore(tmp_path / "state")
    original_append = store._append_event_locked
    original_forward = supervisor.forward_signal
    group_terms: list[tuple[int, int]] = []
    failed = False

    def fail_exit_event(root_fd: int, record: bytes, **kwargs) -> None:
        nonlocal failed
        event = json.loads(record)
        if event.get("event") == "supervisor_child_exited" and not failed:
            failed = True
            raise OSError("exit-state-secret")
        original_append(root_fd, record, **kwargs)

    def record_group_term(pgid: int, signum: int) -> None:
        group_terms.append((pgid, signum))
        original_forward(pgid, signum)

    monkeypatch.setattr(store, "_append_event_locked", fail_exit_event)
    monkeypatch.setattr(supervisor, "forward_signal", record_group_term)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "raise SystemExit(17)", "argv-secret"),
                str(tmp_path),
            ),
            store,
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        monkeypatch.setattr(store, "_append_event_locked", original_append)
        store.recover_transition_events()
        records = store.load_processes()
        assert result == 17
        assert records == ()
        assert captured.err == ""
        assert "secret" not in captured.err
        assert group_terms == []
        receipts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (store.root / "event-receipts").iterdir()
        ]
        exit_receipts = [
            receipt
            for receipt in receipts
            if receipt["event"]["event"] == "supervisor_child_exited"
        ]
        assert len(exit_receipts) == 1
        assert exit_receipts[0]["event"]["exit_code"] == 17
        assert list((store.root / "event-journal").iterdir()) == []
    finally:
        _kill_captured_children(children)


def test_exit_delete_failure_keeps_child_exit_code_and_exact_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    children = _capture_real_popen(monkeypatch)
    store = state.StateStore(tmp_path / "state")
    original_transition = store.transition

    def fail_delete(record_kind, record_key, expected, updated, event, **kwargs):
        if updated is None:
            raise OSError("delete-state-secret")
        return original_transition(
            record_kind,
            record_key,
            expected,
            updated,
            event,
            **kwargs,
        )

    monkeypatch.setattr(store, "transition", fail_delete)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "raise SystemExit(17)", "argv-secret"),
                str(tmp_path),
            ),
            store,
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        records = store.load_processes()
        assert result == 17
        assert len(records) == 1
        assert records[0].exit_code == 17
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=state_exit_failed\n"
        )
        assert "secret" not in captured.err
    finally:
        _kill_captured_children(children)


def test_reaped_leader_numeric_pgid_substitution_is_never_signaled_or_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_pidfd_signaling()
    leader_reaped = False
    children: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen
    group_terms: list[tuple[int, int]] = []
    store = state.StateStore(tmp_path / "state")
    original_save = store.save_process
    saved_processes: list[model.ManagedProcess] = []
    boot_id = procfs.LinuxProcfs().boot_id()
    assert boot_id is not None

    replacement = model.ProcessIdentity(
        boot_id=boot_id,
        pid=999_999,
        ppid=1,
        pgid=1,
        start_ticks=999_999,
        exe_dev=1,
        exe_ino=1,
        exe_name="unrelated-replacement",
    )

    class ReplacementAfterReapProcfs(procfs.LinuxProcfs):
        def group_members(self, pgid: int):
            if leader_reaped:
                return (replace(replacement, pgid=pgid),)
            return super().group_members(pgid)

    def capture_reap(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        original_wait = child.wait

        def record_reap(*wait_args, **wait_kwargs):
            nonlocal leader_reaped
            result = original_wait(*wait_args, **wait_kwargs)
            leader_reaped = True
            return result

        child.wait = record_reap  # type: ignore[method-assign]
        children.append(child)
        return child

    def capture_save(process: model.ManagedProcess, **kwargs) -> None:
        saved_processes.append(process)
        original_save(process, **kwargs)

    def record_group_term(pgid: int, signum: int) -> None:
        group_terms.append((pgid, signum))

    monkeypatch.setattr(supervisor.subprocess, "Popen", capture_reap)
    monkeypatch.setattr(store, "save_process", capture_save)
    monkeypatch.setattr(supervisor, "forward_signal", record_group_term)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "raise SystemExit(17)", "argv-secret"),
                str(tmp_path),
            ),
            store,
            ReplacementAfterReapProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        records = store.load_processes()
        assert result == 17
        assert group_terms == []
        assert records == ()
        assert all(
            replace(replacement, pgid=children[0].pid) not in process.members
            for process in saved_processes
        )
        assert captured.err == ""
    finally:
        _kill_captured_children(children)


def test_spawn_event_failure_persists_terminal_replacement_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    children = _capture_real_popen(monkeypatch)
    store = state.StateStore(tmp_path / "state")
    original_append = store.append_event

    def fail_spawn_event(event: dict[str, object]) -> None:
        if event.get("event") == "supervisor_spawned":
            raise OSError("spawn-event-secret")
        original_append(event)

    monkeypatch.setattr(store, "append_event", fail_spawn_event)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "import time; time.sleep(30)", "argv-secret"),
                str(tmp_path),
            ),
            store,
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        records = store.load_processes()
        assert result == 70
        assert len(records) == 1
        assert records[0].exit_code is not None
        assert records[0].owner_reason_codes == ("state_spawn_failed",)
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=state_spawn_failed\n"
        )
        assert "secret" not in captured.err
    finally:
        _kill_captured_children(children)


def test_reconciliation_event_failure_preserves_owner_and_terminal_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    children = _capture_real_popen(monkeypatch)
    store = state.StateStore(tmp_path / "state")
    live_procfs = procfs.LinuxProcfs()
    wrapper_chain = live_procfs.ancestor_chain(os.getpid())
    boot_id = live_procfs.boot_id()
    assert len(wrapper_chain) > 1
    assert boot_id is not None
    lease = model.SessionLease(
        schema_version=1,
        session_id="session:event-failure",
        cwd=str(tmp_path),
        source="SessionStart",
        host_keys=(wrapper_chain[1].stable_key(),),
        state="active",
        observed=model.ObservedTime(
            "2026-08-29T00:00:00+00:00",
            boot_id,
            100.0,
        ),
    )
    store.save_session(lease)
    original_append = store.append_event

    def fail_owner_event(event: dict[str, object]) -> None:
        if event.get("event") == "owner_reconciled":
            raise OSError("owner-event-secret")
        original_append(event)

    monkeypatch.setattr(store, "append_event", fail_owner_event)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "import time; time.sleep(30)", "argv-secret"),
                str(tmp_path),
            ),
            store,
            live_procfs,
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        records = store.load_processes()
        assert result == 70
        assert len(records) == 1
        assert records[0].exit_code is not None
        assert records[0].owner_session_id == lease.session_id
        assert records[0].owner_reason_codes == ("unique_matching_session",)
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=owner_reconcile_failed\n"
        )
        assert "secret" not in captured.err
    finally:
        _kill_captured_children(children)


def test_forward_signal_tolerates_child_group_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    def gone(pgid: int, signum: int) -> None:
        calls.append((pgid, signum))
        raise ProcessLookupError

    monkeypatch.setattr(supervisor.os, "killpg", gone)
    supervisor.forward_signal(12345, signal.SIGTERM)
    assert calls == [(12345, signal.SIGTERM)]


def test_live_signal_forward_captures_exact_group_before_numeric_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child:
        pid = 12345
        returncode = None

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )
    order: list[str] = []
    monkeypatch.setattr(lifecycle, "capture_group", lambda: order.append("capture"))
    monkeypatch.setattr(
        supervisor,
        "forward_signal",
        lambda _pgid, _signum: order.append("signal"),
    )

    lifecycle.forward(signal.SIGTERM)

    assert order == ["capture", "signal"]


def test_reentrant_pending_term_cannot_repeat_numeric_group_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child:
        pid = 12345
        returncode = None

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )
    blocked = False
    pending = False
    capture_calls = 0
    group_terms: list[tuple[int, int]] = []

    def fake_mask(how, mask):
        nonlocal blocked, pending
        previous = frozenset(supervisor._FORWARDED_SIGNALS) if blocked else frozenset()
        if how == signal.SIG_BLOCK:
            blocked = True
        elif how == signal.SIG_SETMASK:
            blocked = bool(mask)
            if not blocked and pending:
                pending = False
                lifecycle.forward(signal.SIGTERM)
        return previous

    def deliver_term() -> None:
        nonlocal pending
        if blocked:
            pending = True
        else:
            lifecycle.forward(signal.SIGTERM)

    def capture_with_reentrant_term() -> None:
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 1:
            deliver_term()

    monkeypatch.setattr(supervisor.signal, "pthread_sigmask", fake_mask)
    monkeypatch.setattr(lifecycle, "capture_group", capture_with_reentrant_term)
    monkeypatch.setattr(lifecycle, "_send_exact", lambda _signum, **_kwargs: None)
    monkeypatch.setattr(
        supervisor,
        "forward_signal",
        lambda pgid, signum: group_terms.append((pgid, signum)),
    )

    lifecycle.forward(signal.SIGTERM)

    assert group_terms == [(Child.pid, signal.SIGTERM)]
    assert not blocked
    assert not pending


def test_nonreaping_exit_check_and_final_capture_share_one_signal_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class Child:
        pid = 12345
        returncode = None
        args = ("fixture",)

        def wait(self):
            order.append("reap")
            self.returncode = 0
            return 0

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )

    def fake_mask(how, _mask):
        if how == signal.SIG_BLOCK:
            order.append("block")
        elif how == signal.SIG_SETMASK:
            order.append("restore")
        return frozenset()

    monkeypatch.setattr(supervisor.signal, "pthread_sigmask", fake_mask)
    monkeypatch.setattr(
        lifecycle, "_exit_observed", lambda: order.append("check") or True
    )
    monkeypatch.setattr(
        lifecycle,
        "capture_final_group",
        lambda: order.append("capture") or True,
    )

    assert lifecycle._wait_once(0.05) == 0
    assert order == ["block", "check", "capture", "reap", "restore"]


def test_partial_final_capture_restores_mask_and_retries_before_owned_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class Child:
        pid = 12345
        returncode = None
        args = ("fixture",)

        def wait(self):
            order.append("reap")
            self.returncode = 0
            return 0

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )
    captures = iter((False, True))

    def fake_mask(how, _mask):
        if how == signal.SIG_BLOCK:
            order.append("block")
        elif how == signal.SIG_SETMASK:
            order.append("restore")
        return frozenset()

    def capture_final_group() -> bool:
        complete = next(captures)
        order.append("capture-complete" if complete else "capture-partial")
        return complete

    monkeypatch.setattr(supervisor.signal, "pthread_sigmask", fake_mask)
    monkeypatch.setattr(
        lifecycle, "_exit_observed", lambda: order.append("check") or True
    )
    monkeypatch.setattr(
        lifecycle,
        "capture_final_group",
        capture_final_group,
        raising=False,
    )
    monkeypatch.setattr(supervisor.time, "sleep", lambda _delay: None)

    assert lifecycle._wait_once(0.05) == 0
    assert order == [
        "block",
        "check",
        "capture-partial",
        "restore",
        "block",
        "capture-complete",
        "reap",
        "restore",
    ]


def test_complete_final_capture_waits_for_previously_discovered_live_pidfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = procfs.LinuxProcfs().identity(os.getpid())
    assert identity is not None

    class Child:
        pid = 12345
        returncode = None

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )
    lifecycle.identities[identity.stable_key()] = identity
    open_attempts = 0

    def observe_group(_pgid: int):
        return procfs.GroupMembersObservation("complete", (), ())

    def open_pidfd(_pid: int, _flags: int) -> int:
        nonlocal open_attempts
        open_attempts += 1
        if open_attempts == 1:
            raise PermissionError("transient pidfd failure")
        return 101

    monkeypatch.setattr(lifecycle.procfs, "observe_group_members", observe_group)
    monkeypatch.setattr(lifecycle, "_observation", lambda _identity: "exact")
    monkeypatch.setattr(supervisor.os, "pidfd_open", open_pidfd)
    monkeypatch.setattr(supervisor.os, "close", lambda _fd: None)

    assert not lifecycle.capture_final_group()
    assert lifecycle.capture_final_group()
    assert lifecycle.pidfds == {identity.stable_key(): 101}
    assert lifecycle.close()


def test_complete_final_capture_anchors_unreaped_zombie_leader_by_pidfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = procfs.LinuxProcfs().identity(os.getpid())
    assert identity is not None

    class Child:
        pid = identity.pid
        returncode = None

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )
    lifecycle.identities[identity.stable_key()] = identity
    lifecycle.leader_exit_observed = True
    opened: list[tuple[int, int]] = []

    def observe_group(_pgid: int):
        return procfs.GroupMembersObservation("complete", (), ())

    def open_pidfd(pid: int, flags: int) -> int:
        opened.append((pid, flags))
        return 101

    monkeypatch.setattr(lifecycle.procfs, "observe_group_members", observe_group)
    monkeypatch.setattr(lifecycle, "_observation", lambda _identity: "unavailable")
    monkeypatch.setattr(supervisor.os, "pidfd_open", open_pidfd)
    monkeypatch.setattr(supervisor.os, "close", lambda _fd: None)

    assert lifecycle.capture_final_group()
    assert opened == [(identity.pid, 0)]
    assert lifecycle.pidfds == {identity.stable_key(): 101}
    assert lifecycle.close()


def test_unavailable_captured_identity_is_not_considered_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = procfs.LinuxProcfs().identity(os.getpid())
    assert identity is not None

    class Child:
        pid = identity.pid
        returncode = None

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )
    lifecycle.identities[identity.stable_key()] = identity
    monkeypatch.setattr(lifecycle, "_pidfd_exited", lambda _key: False)
    monkeypatch.setattr(lifecycle, "_observation", lambda _identity: "unavailable")

    assert not lifecycle.captured_identities_are_gone()
    assert lifecycle.close()


def test_nonreaping_wait_retries_interrupted_waitid_before_owned_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child:
        pid = 12345
        returncode = None
        args = ("fixture",)
        wait_calls = 0

        def wait(self):
            self.wait_calls += 1
            self.returncode = 17
            return 17

    child = Child()
    lifecycle = supervisor._OwnedGroupLifecycle(
        child,  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        child.pid,
    )
    observations = iter((InterruptedError("waitid-secret"), object()))

    def interrupted_then_exited(*_args):
        observation = next(observations)
        if isinstance(observation, BaseException):
            raise observation
        return observation

    monkeypatch.setattr(supervisor.os, "waitid", interrupted_then_exited)
    monkeypatch.setattr(lifecycle, "capture_group", lambda: None)

    assert lifecycle._wait_once(0.05) == 17
    assert child.wait_calls == 1


def test_reentrant_pidfd_publication_closes_duplicate_and_published_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = procfs.LinuxProcfs().identity(os.getpid())
    assert identity is not None

    class Child:
        pid = 12345
        returncode = None

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )
    opened = iter((101, 102))
    opened_fds: list[int] = []
    closed_fds: list[int] = []
    reentered = False
    observations = 0

    def open_pidfd(_pid: int, _flags: int) -> int:
        pidfd = next(opened)
        opened_fds.append(pidfd)
        return pidfd

    def observe(_identity: model.ProcessIdentity) -> str:
        nonlocal observations, reentered
        observations += 1
        if observations == 2 and not reentered:
            reentered = True
            lifecycle._open_pidfd(identity)
        return "exact"

    monkeypatch.setattr(supervisor.os, "pidfd_open", open_pidfd)
    monkeypatch.setattr(supervisor.os, "close", closed_fds.append)
    monkeypatch.setattr(lifecycle, "_observation", observe)

    lifecycle._open_pidfd(identity)
    lifecycle.close()

    assert opened_fds == [101, 102]
    assert sorted(closed_fds) == opened_fds


def test_duplicate_pidfd_loser_close_failure_is_sticky_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = procfs.LinuxProcfs().identity(os.getpid())
    assert identity is not None

    class Child:
        pid = 12345
        returncode = None

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )
    opened = iter((101, 102))
    close_attempts: list[int] = []
    reentered = False
    observations = 0

    def open_pidfd(_pid: int, _flags: int) -> int:
        return next(opened)

    def observe(_identity: model.ProcessIdentity) -> str:
        nonlocal observations, reentered
        observations += 1
        if observations == 2 and not reentered:
            reentered = True
            lifecycle._open_pidfd(identity)
        return "exact"

    def close_fd(fd: int) -> None:
        close_attempts.append(fd)
        if fd == 101:
            raise OSError(5, "ambiguous duplicate close")

    monkeypatch.setattr(supervisor.os, "pidfd_open", open_pidfd)
    monkeypatch.setattr(supervisor.os, "close", close_fd)
    monkeypatch.setattr(lifecycle, "_observation", observe)

    lifecycle._open_pidfd(identity)

    assert not lifecycle.close()
    assert close_attempts.count(101) == 1
    assert close_attempts.count(102) == 1
    assert lifecycle.owned_pidfds == set()


def test_pidfd_close_never_probes_or_closes_reused_unrelated_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child:
        pid = 12345
        returncode = 0

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )
    owned_fd, unrelated_write_fd = os.pipe()
    lifecycle.owned_pidfds.add(owned_fd)
    original_close = os.close
    original_fstat = os.fstat
    close_attempts: list[int] = []
    probes: list[int] = []
    replacement_open = False

    def close_and_reuse(fd: int) -> None:
        nonlocal replacement_open
        close_attempts.append(fd)
        if fd == owned_fd and not replacement_open:
            original_close(fd)
            replacement_fd = os.open(os.devnull, os.O_RDONLY)
            assert replacement_fd == owned_fd
            replacement_open = True
            return
        if fd == owned_fd:
            replacement_open = False
        original_close(fd)

    def record_probe(fd: int):
        probes.append(fd)
        return original_fstat(fd)

    monkeypatch.setattr(supervisor.os, "close", close_and_reuse)
    monkeypatch.setattr(supervisor.os, "fstat", record_probe)
    try:
        assert lifecycle.close()
        assert close_attempts == [owned_fd]
        assert probes == []
        assert replacement_open
        original_fstat(owned_fd)
    finally:
        if replacement_open:
            original_close(owned_fd)
        original_close(unrelated_write_fd)


def test_interrupted_pidfd_close_attempts_every_fd_once_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child:
        pid = 12345
        returncode = 0

    lifecycle = supervisor._OwnedGroupLifecycle(
        Child(),  # type: ignore[arg-type]
        procfs.LinuxProcfs(),
        Child.pid,
    )
    lifecycle.owned_pidfds.update((101, 102))
    live = {101, 102}
    close_attempts: list[int] = []
    probes: list[int] = []

    def close_fd(fd: int) -> None:
        close_attempts.append(fd)
        if fd == 101:
            raise KeyboardInterrupt("close-secret")
        live.discard(fd)

    def verify_fd(fd: int) -> object:
        probes.append(fd)
        if fd in live:
            return object()
        raise OSError(9, "closed")

    monkeypatch.setattr(supervisor.os, "close", close_fd)
    monkeypatch.setattr(supervisor.os, "fstat", verify_fd)

    assert not lifecycle.close()
    assert live == {101}
    assert sorted(close_attempts) == [101, 102]
    assert probes == []
    assert lifecycle.owned_pidfds == set()


def test_pending_signal_failure_reaps_exact_spawned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    children = _capture_real_popen(monkeypatch)
    original_forward = supervisor.forward_signal
    calls = 0

    def install_with_pending_signal():
        return {}, {"child": None}, [signal.SIGTERM]

    def fail_first_forward(pgid: int, signum: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("signal-secret")
        original_forward(pgid, signum)

    monkeypatch.setattr(
        supervisor, "_install_signal_handlers", install_with_pending_signal
    )
    monkeypatch.setattr(supervisor, "forward_signal", fail_first_forward)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "import time; time.sleep(30)", "argv-secret"),
                str(tmp_path),
            ),
            state.StateStore(tmp_path / "state"),
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
        )
        captured = capsys.readouterr()
        assert result == 70
        assert calls == 1
        assert children[0].poll() is not None
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=signal_forward_failed\n"
        )
        assert "secret" not in captured.err
    finally:
        _kill_captured_children(children)


def test_publication_window_forward_failure_cleans_child_group_and_restores_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    descendant_pid = tmp_path / "publication-descendant-pid"
    descendant_ready = tmp_path / "publication-descendant-ready"
    descendant_code = (
        "import os,pathlib,sys,time;"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
        "pathlib.Path(sys.argv[2]).write_text('ready');time.sleep(30)"
    )
    leader_code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],*sys.argv[2:4]],"
        "close_fds=True);time.sleep(2)"
    )
    children = _capture_real_popen(monkeypatch)
    descendant_handles: list[tuple[model.ProcessIdentity, int]] = []
    original_forward = supervisor.forward_signal
    forward_calls = 0

    def publish_during_signal(child_ref, child) -> None:
        child_ref["child"] = child
        deadline = time.monotonic() + 4.0
        while not descendant_ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        identity = procfs.LinuxProcfs().identity(
            int(descendant_pid.read_text(encoding="utf-8"))
        )
        assert identity is not None
        pidfd = _open_exact_pidfd(identity)
        assert pidfd is not None
        descendant_handles.append((identity, pidfd))
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    def fail_publication_forward_once(pgid: int, signum: int) -> None:
        nonlocal forward_calls
        forward_calls += 1
        if forward_calls == 1:
            raise PermissionError("publication-forward-secret")
        original_forward(pgid, signum)

    monkeypatch.setattr(
        supervisor,
        "_publish_child",
        publish_during_signal,
        raising=False,
    )
    monkeypatch.setattr(supervisor, "forward_signal", fail_publication_forward_once)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                (
                    "-c",
                    leader_code,
                    descendant_code,
                    str(descendant_pid),
                    str(descendant_ready),
                ),
                str(tmp_path),
            ),
            state.StateStore(tmp_path / "state"),
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=lambda _delay: None,
        )
        captured = capsys.readouterr()
        assert result == 70
        assert len(descendant_handles) == 1
        _wait_exact_identity_gone(descendant_handles[0][0])
        assert all(child.poll() is not None for child in children)
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=signal_forward_failed\n"
        )
        assert "secret" not in captured.err
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        if not descendant_handles and descendant_pid.exists():
            identity = procfs.LinuxProcfs().identity(
                int(descendant_pid.read_text(encoding="utf-8"))
            )
            if identity is not None:
                pidfd = _open_exact_pidfd(identity)
                if pidfd is not None:
                    descendant_handles.append((identity, pidfd))
        _signal_exact_pidfds(descendant_handles, signal.SIGKILL)
        for identity, _ in descendant_handles:
            _wait_exact_identity_gone(identity)
        for _, pidfd in descendant_handles:
            os.close(pidfd)
        _kill_captured_children(children)


def test_keyboard_interrupt_during_reconciliation_reaps_child_and_restores_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    children = _capture_real_popen(monkeypatch)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("interrupt-secret")

    monkeypatch.setattr(supervisor, "_reconcile_owner", interrupt)
    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                ("-c", "pass", "argv-secret"),
                str(tmp_path),
            ),
            state.StateStore(tmp_path / "state"),
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
        )
        captured = capsys.readouterr()
        assert result == 70
        assert children[0].poll() is not None
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=owner_reconcile_failed\n"
        )
        assert "secret" not in captured.err
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        _kill_captured_children(children)


def test_post_spawn_failure_terms_group_after_leader_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    descendant_pid = tmp_path / "descendant-pid"
    descendant_ready = tmp_path / "descendant-ready"
    descendant_term = tmp_path / "descendant-term"
    release_leader = tmp_path / "release-leader"
    leader_exiting = tmp_path / "leader-exiting"
    descendant_code = (
        "import os,pathlib,signal,sys,time;"
        "pid=pathlib.Path(sys.argv[1]);ready=pathlib.Path(sys.argv[2]);"
        "term=pathlib.Path(sys.argv[3]);pid.write_text(str(os.getpid()));"
        "signal.signal(signal.SIGTERM,lambda *_:(term.write_text('term'),sys.exit(0)));"
        "ready.write_text('ready');time.sleep(30)"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],*sys.argv[2:5]],close_fds=True);"
        "release=pathlib.Path(sys.argv[5]);exiting=pathlib.Path(sys.argv[6]);"
        "deadline=time.monotonic()+4;"
        "\nwhile not pathlib.Path(sys.argv[3]).exists():\n"
        "  assert time.monotonic()<deadline\n  time.sleep(0.01)\n"
        "\nwhile not release.exists():\n  time.sleep(0.01)\n"
        "exiting.write_text('exiting')"
    )
    children = _capture_real_popen(monkeypatch)
    descendant_handles: list[tuple[model.ProcessIdentity, int]] = []

    def fail_after_leader_exit(delay: float) -> None:
        assert delay == 0.05
        deadline = time.monotonic() + 4.0
        while not descendant_ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pid = int(descendant_pid.read_text(encoding="utf-8"))
        identity = procfs.LinuxProcfs().identity(pid)
        assert identity is not None
        pidfd = _open_exact_pidfd(identity)
        assert pidfd is not None
        descendant_handles.append((identity, pidfd))
        release_leader.write_text("release", encoding="utf-8")
        while not leader_exiting.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.05)
        raise KeyboardInterrupt("post-spawn-secret")

    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                (
                    "-c",
                    leader_code,
                    descendant_code,
                    str(descendant_pid),
                    str(descendant_ready),
                    str(descendant_term),
                    str(release_leader),
                    str(leader_exiting),
                ),
                str(tmp_path),
            ),
            state.StateStore(tmp_path / "state"),
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=fail_after_leader_exit,
        )
        captured = capsys.readouterr()
        assert result == 70
        assert descendant_term.read_text(encoding="utf-8") == "term"
        _wait_exact_identity_gone(descendant_handles[0][0])
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=owner_reconcile_failed\n"
        )
        assert "secret" not in captured.err
    finally:
        _signal_exact_pidfds(descendant_handles, signal.SIGKILL)
        for identity, _ in descendant_handles:
            _wait_exact_identity_gone(identity)
        for _, pidfd in descendant_handles:
            os.close(pidfd)
        _kill_captured_children(children)


def test_captured_descendant_migrating_session_receives_exact_pidfd_term(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    descendant_pid = tmp_path / "migrated-descendant-pid"
    descendant_ready = tmp_path / "migrated-descendant-ready"
    descendant_migrated = tmp_path / "descendant-migrated"
    descendant_exact_term = tmp_path / "descendant-exact-term"
    descendant_code = (
        "import os,pathlib,signal,sys,time\n"
        "pid=pathlib.Path(sys.argv[1])\n"
        "ready=pathlib.Path(sys.argv[2])\n"
        "migrated=pathlib.Path(sys.argv[3])\n"
        "exact_term=pathlib.Path(sys.argv[4])\n"
        "term_count=0\n"
        "def on_term(*_):\n"
        "  global term_count\n"
        "  term_count += 1\n"
        "  if term_count == 1:\n"
        "    os.setsid()\n"
        "    migrated.write_text('migrated')\n"
        "    return\n"
        "  exact_term.write_text('exact-term')\n"
        "  raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM,on_term)\n"
        "pid.write_text(str(os.getpid()))\n"
        "ready.write_text('ready')\n"
        "time.sleep(30)\n"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],*sys.argv[2:6]],"
        "close_fds=True);"
        "deadline=time.monotonic()+4;ready=pathlib.Path(sys.argv[3]);"
        "\nwhile not ready.exists():\n"
        "  assert time.monotonic()<deadline\n  time.sleep(0.01)\n"
        "time.sleep(30)"
    )
    children = _capture_real_popen(monkeypatch)
    descendant_handles: list[tuple[model.ProcessIdentity, int]] = []

    def fail_after_descendant_capture(delay: float) -> None:
        assert delay == 0.05
        deadline = time.monotonic() + 4.0
        while not descendant_ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        identity = procfs.LinuxProcfs().identity(
            int(descendant_pid.read_text(encoding="utf-8"))
        )
        assert identity is not None
        pidfd = _open_exact_pidfd(identity)
        assert pidfd is not None
        descendant_handles.append((identity, pidfd))
        raise KeyboardInterrupt("migrated-descendant-secret")

    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                (
                    "-c",
                    leader_code,
                    descendant_code,
                    str(descendant_pid),
                    str(descendant_ready),
                    str(descendant_migrated),
                    str(descendant_exact_term),
                ),
                str(tmp_path),
            ),
            state.StateStore(tmp_path / "state"),
            procfs.LinuxProcfs(),
            FakeClock(boot=100.0),
            sleeper=fail_after_descendant_capture,
        )
        captured = capsys.readouterr()
        assert result == 70
        assert descendant_migrated.read_text(encoding="utf-8") == "migrated"
        assert descendant_exact_term.read_text(encoding="utf-8") == "exact-term"
        _wait_exact_identity_gone(descendant_handles[0][0])
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=owner_reconcile_failed\n"
        )
        assert "secret" not in captured.err
    finally:
        _signal_exact_pidfds(descendant_handles, signal.SIGKILL)
        for identity, _ in descendant_handles:
            _wait_exact_identity_gone(identity)
        for _, pidfd in descendant_handles:
            os.close(pidfd)
        _kill_captured_children(children)


@pytest.mark.parametrize("recovery", ["exit_event", "handler_restore"])
def test_late_fork_migrated_inside_final_scan_is_anchored_before_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recovery: str,
) -> None:
    _require_pidfd_signaling()
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    original_signal = signal.signal
    original_popen = subprocess.Popen
    original_open_owned_pidfd = supervisor._OwnedGroupLifecycle._open_pidfd
    original_pidfd_send_signal = signal.pidfd_send_signal
    arm = tmp_path / "scan-migration-arm"
    release = tmp_path / "scan-migration-release"
    migrate = tmp_path / "scan-migration-request"
    descendant_pid = tmp_path / "scan-migration-descendant-pid"
    descendant_ready = tmp_path / "scan-migration-descendant-ready"
    descendant_migrated = tmp_path / "scan-migration-descendant-migrated"
    descendant_term = tmp_path / "scan-migration-descendant-term"
    leader_exiting = tmp_path / "scan-migration-leader-exiting"
    descendant_code = (
        "import os,pathlib,signal,sys,time\n"
        "pid=pathlib.Path(sys.argv[1])\n"
        "ready=pathlib.Path(sys.argv[2])\n"
        "migrate=pathlib.Path(sys.argv[3])\n"
        "migrated=pathlib.Path(sys.argv[4])\n"
        "term=pathlib.Path(sys.argv[5])\n"
        "signal.signal(signal.SIGTERM,lambda *_:(term.write_text('term'),sys.exit(0)))\n"
        "pid.write_text(str(os.getpid()))\n"
        "ready.write_text('ready')\n"
        "while not migrate.exists(): time.sleep(.01)\n"
        "os.setsid()\n"
        "migrated.write_text('migrated')\n"
        "time.sleep(30)\n"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time\n"
        "release=pathlib.Path(sys.argv[1])\n"
        "ready=pathlib.Path(sys.argv[4])\n"
        "exiting=pathlib.Path(sys.argv[8])\n"
        "while not release.exists(): time.sleep(.01)\n"
        "subprocess.Popen([sys.executable,'-c',sys.argv[2],*sys.argv[3:8]],"
        "close_fds=True)\n"
        "while not ready.exists(): time.sleep(.01)\n"
        "exiting.write_text('exiting')\n"
    )
    leader_handles: list[tuple[model.ProcessIdentity, int]] = []
    descendant_handles: list[tuple[model.ProcessIdentity, int]] = []
    children: list[subprocess.Popen[bytes]] = []
    armed_snapshots = 0
    released = False
    inside_strict_scan = False
    migration_requested = False
    candidate_stat: procfs.ProcStat | None = None
    exact_observations: list[model.ProcessIdentity] = []
    anchor_returncodes: list[int | None] = []
    supervisor_descendant_pidfds: set[int] = set()
    supervisor_pidfd_terms: list[int] = []

    def capture_fixture(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        deadline = time.monotonic() + 2.0
        while True:
            identity = procfs.LinuxProcfs().identity(child.pid)
            if identity is not None:
                pidfd = _open_exact_pidfd(identity)
                if pidfd is not None:
                    leader_handles.append((identity, pidfd))
                    break
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        return child

    class MigrateInsideStrictScanProcfs(procfs.LinuxProcfs):
        def group_members(self, pgid: int):
            nonlocal armed_snapshots, released
            snapshot = super().group_members(pgid)
            if arm.exists():
                armed_snapshots += 1
            if armed_snapshots == 2 and not released:
                released = True
                release.write_text("release", encoding="utf-8")
                deadline = time.monotonic() + 4.0
                while not descendant_ready.exists() or not leader_exiting.exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                identity = self.identity(
                    int(descendant_pid.read_text(encoding="utf-8"))
                )
                while identity is None or not descendant_handles:
                    if identity is not None:
                        pidfd = _open_exact_pidfd(identity)
                        if pidfd is not None:
                            descendant_handles.append((identity, pidfd))
                            break
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                    identity = self.identity(
                        int(descendant_pid.read_text(encoding="utf-8"))
                    )
            return snapshot

        def _read_text(self, path: Path) -> str:
            nonlocal candidate_stat
            raw = super()._read_text(path)
            if (
                inside_strict_scan
                and descendant_pid.exists()
                and path
                == self.proc_root
                / descendant_pid.read_text(encoding="utf-8").strip()
                / "stat"
                and candidate_stat is None
            ):
                candidate_stat = procfs.parse_stat(raw)
            return raw

        def observe_identity(self, pid: int):
            nonlocal migration_requested
            if (
                inside_strict_scan
                and descendant_pid.exists()
                and pid == int(descendant_pid.read_text(encoding="utf-8"))
                and not migration_requested
            ):
                assert candidate_stat is not None
                migration_requested = True
                migrate.write_text("migrate", encoding="utf-8")
                deadline = time.monotonic() + 4.0
                while not descendant_migrated.exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                observation = super().observe_identity(pid)
                assert observation.kind == "live"
                assert observation.identity is not None
                exact_observations.append(observation.identity)
                return observation
            return super().observe_identity(pid)

        def observe_group_members(self, pgid: int):
            nonlocal inside_strict_scan
            assert children and children[0].returncode is None
            inside_strict_scan = True
            try:
                return super().observe_group_members(pgid)
            finally:
                inside_strict_scan = False

    def track_owned_pidfd(
        lifecycle: supervisor._OwnedGroupLifecycle,
        identity: model.ProcessIdentity,
    ) -> None:
        key = identity.stable_key()
        was_anchored = key in lifecycle.pidfds
        original_open_owned_pidfd(lifecycle, identity)
        if (
            not was_anchored
            and descendant_handles
            and key == descendant_handles[0][0].stable_key()
            and key in lifecycle.pidfds
        ):
            pidfd = lifecycle.pidfds[key]
            supervisor_descendant_pidfds.add(pidfd)
            anchor_returncodes.append(lifecycle.child.returncode)

    def record_pidfd_term(pidfd, signum, siginfo, flags):
        if pidfd in supervisor_descendant_pidfds and signum == signal.SIGTERM:
            supervisor_pidfd_terms.append(pidfd)
        return original_pidfd_send_signal(pidfd, signum, siginfo, flags)

    monkeypatch.setattr(supervisor.subprocess, "Popen", capture_fixture)
    monkeypatch.setattr(
        supervisor._OwnedGroupLifecycle,
        "_open_pidfd",
        track_owned_pidfd,
    )
    monkeypatch.setattr(supervisor.signal, "pidfd_send_signal", record_pidfd_term)
    store = state.StateStore(tmp_path / "state")
    if recovery == "exit_event":
        original_append = store.append_event

        def fail_exit_event(event: dict[str, object]) -> None:
            if event.get("event") == "supervisor_child_exited":
                raise OSError("exit-event-secret")
            original_append(event)

        monkeypatch.setattr(store, "append_event", fail_exit_event)
    else:
        restore_failed = False

        def fail_first_restore(signum, handler):
            nonlocal restore_failed
            if handler == previous[signum] and not restore_failed:
                restore_failed = True
                raise OSError("restore-secret")
            return original_signal(signum, handler)

        monkeypatch.setattr(supervisor.signal, "signal", fail_first_restore)

    def arm_before_wait(delay: float) -> None:
        if delay == 0.8:
            arm.write_text("armed", encoding="utf-8")

    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                (
                    "-c",
                    leader_code,
                    str(release),
                    descendant_code,
                    str(descendant_pid),
                    str(descendant_ready),
                    str(migrate),
                    str(descendant_migrated),
                    str(descendant_term),
                    str(leader_exiting),
                ),
                str(tmp_path),
            ),
            store,
            MigrateInsideStrictScanProcfs(),
            FakeClock(boot=100.0),
            sleeper=arm_before_wait,
        )
        captured = capsys.readouterr()
        assert result == 0
        assert len(descendant_handles) == 1
        assert candidate_stat is not None
        assert len(exact_observations) == 1
        exact = exact_observations[0]
        assert (candidate_stat.pid, candidate_stat.start_ticks) == (
            exact.pid,
            exact.start_ticks,
        )
        assert candidate_stat.pgid != exact.pgid
        assert anchor_returncodes == [None]
        assert supervisor_pidfd_terms
        records = store.load_processes()
        assert len(records) == 1
        assert exact in records[0].members
        assert descendant_term.read_text(encoding="utf-8") == "term"
        _wait_exact_identity_gone(exact)
        expected_reason = "state_exit_failed" if recovery == "exit_event" else None
        if expected_reason is None:
            assert captured.err == ""
        else:
            assert captured.err == (
                f"codex-mcp-supervisor: server=fake reason={expected_reason}\n"
            )
        assert "secret" not in captured.err
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        fixture_handles = [*leader_handles, *descendant_handles]
        try:
            _signal_exact_pidfds(fixture_handles, signal.SIGTERM)
            for child in children:
                child.wait(timeout=2.0)
            for identity, _ in fixture_handles:
                _wait_exact_identity_gone(identity)
        finally:
            for _, pidfd in reversed(fixture_handles):
                os.close(pidfd)


@pytest.mark.parametrize("recovery", ["exit_event", "handler_restore"])
@pytest.mark.parametrize("first_final_kind", ["unavailable", "partial"])
def test_late_fork_is_captured_before_reap_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recovery: str,
    first_final_kind: str,
) -> None:
    _require_pidfd_signaling()
    previous = {
        signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
    }
    original_signal = signal.signal
    arm = tmp_path / "late-fork-arm"
    release = tmp_path / "late-fork-release"
    descendant_pid = tmp_path / "late-fork-descendant-pid"
    descendant_ready = tmp_path / "late-fork-descendant-ready"
    descendant_term = tmp_path / "late-fork-descendant-term"
    leader_exiting = tmp_path / "late-fork-leader-exiting"
    descendant_code = (
        "import os,pathlib,signal,sys,time;"
        "pid=pathlib.Path(sys.argv[1]);ready=pathlib.Path(sys.argv[2]);"
        "term=pathlib.Path(sys.argv[3]);pid.write_text(str(os.getpid()));"
        "signal.signal(signal.SIGTERM,lambda *_:(term.write_text('term'),sys.exit(0)));"
        "ready.write_text('ready');time.sleep(30)"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time\n"
        "release=pathlib.Path(sys.argv[1])\n"
        "ready=pathlib.Path(sys.argv[4])\n"
        "exiting=pathlib.Path(sys.argv[6])\n"
        "while not release.exists(): time.sleep(.01)\n"
        "subprocess.Popen([sys.executable,'-c',sys.argv[2],*sys.argv[3:6]],"
        "close_fds=True)\n"
        "while not ready.exists(): time.sleep(.01)\n"
        "exiting.write_text('exiting')\n"
    )
    children = _capture_real_popen(monkeypatch)
    descendant_handles: list[tuple[model.ProcessIdentity, int]] = []
    released = False
    armed_snapshots = 0
    strict_attempts = 0
    strict_attempt_returncodes: list[int | None] = []

    class ReleaseAfterSnapshotProcfs(procfs.LinuxProcfs):
        def group_members(self, pgid: int):
            nonlocal armed_snapshots, released
            snapshot = super().group_members(pgid)
            if arm.exists():
                armed_snapshots += 1
            if armed_snapshots == 2 and not released:
                released = True
                release.write_text("release", encoding="utf-8")
                deadline = time.monotonic() + 4.0
                while not descendant_ready.exists() or not leader_exiting.exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                while not descendant_handles:
                    assert time.monotonic() < deadline
                    identity = self.identity(
                        int(descendant_pid.read_text(encoding="utf-8"))
                    )
                    if identity is not None:
                        pidfd = _open_exact_pidfd(identity)
                        if pidfd is not None:
                            descendant_handles.append((identity, pidfd))
                            break
                    time.sleep(0.01)
            return snapshot

        def observe_group_members(self, pgid: int):
            nonlocal strict_attempts
            strict_attempts += 1
            assert children
            strict_attempt_returncodes.append(children[0].returncode)
            observation = super().observe_group_members(pgid)
            if strict_attempts != 1:
                return observation
            if first_final_kind == "unavailable":
                return procfs.GroupMembersObservation("unavailable", (), ())
            return procfs.GroupMembersObservation(
                "partial",
                (),
                (int(descendant_pid.read_text(encoding="utf-8")),),
            )

    store = state.StateStore(tmp_path / "state")
    if recovery == "exit_event":
        original_append = store.append_event

        def fail_exit_event(event: dict[str, object]) -> None:
            if event.get("event") == "supervisor_child_exited":
                raise OSError("exit-event-secret")
            original_append(event)

        monkeypatch.setattr(store, "append_event", fail_exit_event)
    else:
        restore_failed = False

        def fail_first_restore(signum, handler):
            nonlocal restore_failed
            if handler == previous[signum] and not restore_failed:
                restore_failed = True
                raise OSError("restore-secret")
            return original_signal(signum, handler)

        monkeypatch.setattr(supervisor.signal, "signal", fail_first_restore)

    def arm_before_wait(delay: float) -> None:
        if delay == 0.8:
            arm.write_text("armed", encoding="utf-8")

    try:
        result = supervisor.run_supervisor(
            supervisor.SupervisorRequest(
                "user",
                "fake",
                sys.executable,
                (
                    "-c",
                    leader_code,
                    str(release),
                    descendant_code,
                    str(descendant_pid),
                    str(descendant_ready),
                    str(descendant_term),
                    str(leader_exiting),
                ),
                str(tmp_path),
            ),
            store,
            ReleaseAfterSnapshotProcfs(),
            FakeClock(boot=100.0),
            sleeper=arm_before_wait,
        )
        captured = capsys.readouterr()
        assert result == 0
        assert len(descendant_handles) == 1
        assert strict_attempts >= 2
        assert strict_attempt_returncodes[:2] == [None, None]
        descendant_identity = descendant_handles[0][0]
        records = store.load_processes()
        assert len(records) == 1
        assert descendant_identity.stable_key() in {
            member.stable_key() for member in records[0].members
        }
        assert descendant_term.read_text(encoding="utf-8") == "term"
        _wait_exact_identity_gone(descendant_identity)
        expected_reason = "state_exit_failed" if recovery == "exit_event" else None
        if expected_reason is None:
            assert captured.err == ""
        else:
            assert captured.err == (
                f"codex-mcp-supervisor: server=fake reason={expected_reason}\n"
            )
        assert "secret" not in captured.err
        assert {
            signum: signal.getsignal(signum) for signum in supervisor._FORWARDED_SIGNALS
        } == previous
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        if not descendant_handles and descendant_pid.exists():
            identity = procfs.LinuxProcfs().identity(
                int(descendant_pid.read_text(encoding="utf-8"))
            )
            if identity is not None:
                pidfd = _open_exact_pidfd(identity)
                if pidfd is not None:
                    descendant_handles.append((identity, pidfd))
        _signal_exact_pidfds(descendant_handles, signal.SIGKILL)
        for identity, _ in descendant_handles:
            _wait_exact_identity_gone(identity)
        for _, pidfd in descendant_handles:
            os.close(pidfd)
        _kill_captured_children(children)
