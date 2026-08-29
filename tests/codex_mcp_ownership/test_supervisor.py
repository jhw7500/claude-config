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
from helpers import FakeClock


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
    pidfd = live_procfs.open_pidfd(identity)
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
    while live_procfs.identity(identity.pid) == identity:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"exact fixture identity {identity.stable_key()} did not exit"
            )
        time.sleep(0.01)


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


def test_child_exit_code_and_lifecycle_state_are_persisted_without_args(
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
    assert len(records) == 1
    record = records[0]
    parent_identity = procfs.LinuxProcfs().identity(os.getpid())
    assert parent_identity is not None
    assert record.exit_code == 17
    assert record.scope == "user"
    assert record.server == "fake"
    assert record.cwd == str(tmp_path)
    assert record.child is not None
    assert record.pgid == record.child.pgid == record.child.pid
    assert record.child in record.members
    assert parent_identity.stable_key() in record.host_keys
    assert record.spawned.boot_id == record.wrapper.boot_id == record.child.boot_id
    assert stdout == stderr == b""
    assert canary.encode() not in persisted + stdout + stderr
    events = [
        json.loads(line)
        for line in (store.root / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["state"] == "exiting"


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
    record = store.load_processes()[0]
    assert returncode == 0
    assert record.owner_session_id == lease.session_id
    assert record.owner_reason_codes == ("unique_matching_session",)


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
    record = store.load_processes()[0]
    assert returncode == 0
    assert record.owner_session_id is None
    assert record.shared_owner is None
    assert record.owner_reason_codes == ("multiple_matching_sessions",)


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


def test_persisted_wrapper_converges_to_gone_after_exit(
    supervisor_command: list[str],
    tmp_path: Path,
) -> None:
    store = state.StateStore(tmp_path / "state")
    returncode, _, _ = _run_wrapper(
        supervisor_command + [sys.executable, "-c", "pass"],
        store,
        tmp_path,
    )
    record = store.load_processes()[0]
    result = classify.classify_process(
        record,
        store.load_sessions(),
        procfs.LinuxProcfs(),
        SystemClock().boottime(),
    )
    assert returncode == 0
    assert result.state == "gone"


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

    def fail_install_then_restore(signum, handler):
        if signum == signal.SIGINT and handler != previous[signum]:
            attempts.append(("install", signum))
            raise ValueError("install-secret")
        if signum == signal.SIGTERM and handler == previous[signum]:
            attempts.append(("restore", signum))
            raise OSError("restore-secret")
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
        ]
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=handler_restore_failed\n"
        )
        assert "secret" not in captured.err
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)


@pytest.mark.parametrize("failed_restore", [signal.SIGHUP, signal.SIGINT])
def test_final_handler_restore_failure_attempts_every_signal_and_surfaces_stably(
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
    failed_once = False

    def fail_one_restore(signum, handler):
        nonlocal failed_once
        if handler == previous[signum]:
            restore_attempts.append(signum)
            if signum == failed_restore and not failed_once:
                failed_once = True
                raise OSError("restore-secret")
        return original_signal(signum, handler)

    children = _capture_real_popen(monkeypatch)
    monkeypatch.setattr(supervisor.signal, "signal", fail_one_restore)
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
        assert restore_attempts == [signal.SIGHUP, signal.SIGINT, signal.SIGTERM]
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=handler_restore_failed\n"
        )
        assert "secret" not in captured.err
        for signum in supervisor._FORWARDED_SIGNALS:
            if signum != failed_restore:
                assert signal.getsignal(signum) == previous[signum]
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        _kill_captured_children(children)


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


def test_exit_state_failure_keeps_child_exit_code_and_exact_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    children = _capture_real_popen(monkeypatch)
    store = state.StateStore(tmp_path / "state")
    original_append = store.append_event

    def fail_exit_event(event: dict[str, object]) -> None:
        if event.get("event") == "supervisor_child_exited":
            raise OSError("exit-state-secret")
        original_append(event)

    monkeypatch.setattr(store, "append_event", fail_exit_event)
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
        assert children[0].poll() is not None
        assert captured.err == (
            "codex-mcp-supervisor: server=fake reason=signal_forward_failed\n"
        )
        assert "secret" not in captured.err
    finally:
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
