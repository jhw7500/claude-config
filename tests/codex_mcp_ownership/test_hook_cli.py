from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from codex_mcp_ownership import cli, hook
from codex_mcp_ownership.model import ManagedProcess, ObservedTime, SessionLease
from codex_mcp_ownership.state import StateStore, session_key
from codex_mcp_ownership.supervisor import SupervisorRequest
from helpers import FakeClock, FakeProcTree, make_private_directory, write_private_file


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "codex-mcp-ownership"
ENTRY = PACKAGE_ROOT / "entry.py"


class Notifier:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls = 0

    def request_cleanup(self) -> bool:
        self.calls += 1
        return self.result


@pytest.fixture
def hook_runtime(tmp_path, monkeypatch):
    tree = FakeProcTree(tmp_path / "proc")
    procfs = hook.LinuxProcfs(tree.root, tree.boot_id_path)
    store = StateStore(tmp_path / "state")
    clock = FakeClock(boot=200.0)
    notifier = Notifier()
    monkeypatch.setattr(hook.os, "getppid", lambda: 321)

    class Runtime:
        def handle(self, payload):
            hook.handle_payload(payload, store, procfs, clock, notifier)

        def start(self, session_id="thr_123", source="startup"):
            self.handle(
                {
                    "session_id": session_id,
                    "cwd": "/workspace",
                    "hook_event_name": "SessionStart",
                    "source": source,
                }
            )

        def only_lease(self):
            leases = store.load_sessions()
            assert len(leases) == 1
            return leases[0]

    return Runtime(), store, clock, notifier, procfs


def test_session_start_creates_active_lease_from_exact_parent_chain(hook_runtime):
    runtime, _store, _clock, notifier, procfs = hook_runtime
    runtime.handle(
        {
            "session_id": "thr_123",
            "cwd": "/workspace",
            "hook_event_name": "SessionStart",
            "source": "startup",
            "model": "gpt-5",
        }
    )
    lease = runtime.only_lease()
    assert lease.session_id == "thr_123"
    assert lease.state == "active"
    assert lease.host_keys == tuple(
        identity.stable_key() for identity in procfs.ancestor_chain(321)
    )
    assert notifier.calls == 1


def test_compact_refresh_reuses_sha_address_without_duplicate_owner(hook_runtime):
    runtime, store, clock, _notifier, _procfs = hook_runtime
    runtime.start()
    first_path = store.root / "sessions" / f"{session_key('thr_123')}.json"
    clock.advance(5.0)
    runtime.start(source="compact")
    lease = runtime.only_lease()
    assert lease.source == "compact"
    assert lease.observed.boottime == 205.0
    assert [path.name for path in (store.root / "sessions").iterdir()] == [
        first_path.name
    ]


def test_session_end_marks_only_exact_matching_lease_ended(hook_runtime):
    runtime, store, clock, notifier, _procfs = hook_runtime
    runtime.start()
    other = replace(runtime.only_lease(), session_id="thr_other")
    store.save_session(other)
    clock.advance(1.0)
    runtime.handle(
        {
            "session_id": "thr_123",
            "cwd": "/workspace",
            "hook_event_name": "SessionEnd",
            "reason": "other",
        }
    )
    leases = {lease.session_id: lease for lease in store.load_sessions()}
    assert leases["thr_123"].state == "ended"
    assert leases["thr_123"].ended is not None
    assert leases["thr_123"].ended.boottime == 201.0
    assert leases["thr_other"].state == "active"
    assert notifier.calls == 2


def test_repeated_session_end_is_byte_for_byte_idempotent(hook_runtime):
    runtime, store, _clock, notifier, _procfs = hook_runtime
    runtime.start()
    payload = {
        "session_id": "thr_123",
        "cwd": "/workspace",
        "hook_event_name": "SessionEnd",
        "reason": "other",
    }
    runtime.handle(payload)
    before = {
        path.relative_to(store.root): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    runtime.handle(payload)
    after = {
        path.relative_to(store.root): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert notifier.calls == 2


def test_invalid_start_clock_is_fail_safe_without_state(hook_runtime):
    runtime, store, clock, notifier, _procfs = hook_runtime
    clock._boot = -1.0
    runtime.start()
    assert not store.root.exists()
    assert notifier.calls == 0


def test_regressing_end_clock_does_not_end_the_lease(hook_runtime):
    runtime, _store, clock, notifier, _procfs = hook_runtime
    runtime.start()
    clock._boot = 100.0
    runtime.handle(
        {
            "session_id": "thr_123",
            "cwd": "/workspace",
            "hook_event_name": "SessionEnd",
            "reason": "other",
        }
    )
    assert runtime.only_lease().state == "active"
    assert notifier.calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"session_id": "thr_123", "cwd": "/workspace"},
        {
            "session_id": "thr_123\nCANARY",
            "cwd": "/workspace",
            "hook_event_name": "SessionStart",
            "source": "startup",
        },
        {
            "session_id": "thr_123",
            "cwd": "/workspace\0CANARY",
            "hook_event_name": "SessionStart",
            "source": "startup",
        },
        {
            "session_id": "thr_123",
            "cwd": "/workspace",
            "hook_event_name": "SessionStart",
            "source": "resume\nCANARY",
        },
        {
            "session_id": "thr_123",
            "cwd": "relative/workspace",
            "hook_event_name": "SessionStart",
            "source": "startup",
        },
        {
            "session_id": "thr_123",
            "cwd": "/workspace",
            "hook_event_name": "SessionStart",
            "source": "startup",
            "transcript_path": "/tmp/transcript\nCANARY",
        },
        {
            "session_id": "thr_123",
            "cwd": "/workspace",
            "hook_event_name": "SessionStart",
            "source": "startup",
            "model": "gpt-5\nCANARY",
        },
    ],
)
def test_malformed_payload_is_fail_safe_and_does_not_create_state(
    hook_runtime, payload
):
    runtime, store, _clock, notifier, _procfs = hook_runtime
    runtime.handle(payload)
    assert not store.root.exists()
    assert notifier.calls == 0


def test_hook_events_never_contain_session_cwd_or_source_canaries(hook_runtime):
    runtime, store, _clock, _notifier, _procfs = hook_runtime
    runtime.handle(
        {
            "session_id": "SESSION-CANARY",
            "cwd": "/CWD-CANARY",
            "hook_event_name": "SessionStart",
            "source": "SOURCE-CANARY",
        }
    )
    event_bytes = (store.root / "events.jsonl").read_bytes()
    assert b"SESSION-CANARY" not in event_bytes
    assert b"CWD-CANARY" not in event_bytes
    assert b"SOURCE-CANARY" not in event_bytes


def test_systemd_notifier_uses_fixed_no_block_argv(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(hook.subprocess, "run", run)
    assert hook.SystemdNotifier().request_cleanup() is True
    assert calls[0][0] == [
        "/usr/bin/systemctl",
        "--user",
        "start",
        "--no-block",
        "codex-mcp-ownership-cleanup.service",
    ]
    assert calls[0][1]["shell"] is False


def test_failed_systemd_request_runs_one_session_start_opportunistic_pass(
    hook_runtime, monkeypatch
):
    runtime, _store, _clock, notifier, _procfs = hook_runtime
    notifier.result = False
    calls = []
    monkeypatch.setattr(
        hook, "_opportunistic_cleanup", lambda *args: calls.append(args)
    )
    runtime.start()
    assert len(calls) == 1


@pytest.mark.parametrize("stdin", ["{", "[]", "null", "{}", "\n"])
def test_executable_hook_always_succeeds_silently_for_malformed_input(tmp_path, stdin):
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(PACKAGE_ROOT),
        "XDG_STATE_HOME": str(tmp_path / "state-CANARY"),
    }
    result = subprocess.run(
        [sys.executable, str(ENTRY), "hook"],
        input=stdin,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
        timeout=2.0,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (path.stat().st_mode, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def cli_runtime(tmp_path, monkeypatch):
    root = tmp_path / "state"
    tree = FakeProcTree(tmp_path / "proc")
    procfs = cli.LinuxProcfs(tree.root, tree.boot_id_path)
    clock = FakeClock(boot=300.0)
    monkeypatch.setattr(cli, "_state_root", lambda: root)
    monkeypatch.setattr(cli, "LinuxProcfs", lambda: procfs)
    monkeypatch.setattr(cli, "SystemClock", lambda: clock)
    return root, tree, procfs, clock


def test_audit_json_is_read_only_and_reports_required_metrics(cli_runtime, capsys):
    root, _tree, _procfs, _clock = cli_runtime
    StateStore(root).append_event({"schema_version": 1, "event": "seed"})
    before = _snapshot(root)
    result = cli.main(["audit", "--json"])
    captured = capsys.readouterr()
    after = _snapshot(root)
    report = json.loads(captured.out)
    assert result == 0
    assert before == after
    assert report["schema_version"] == 1
    assert report["state_counts"]["active"] == 0
    assert report["rss_kib"] == 0
    assert report["attempted"] == 0
    assert report["terminated"] == 0
    assert report["survived"] == 0
    assert report["skipped"] == 0
    assert captured.err == ""


def test_cleanup_without_apply_is_byte_for_byte_read_only(cli_runtime, capsys):
    root, _tree, _procfs, _clock = cli_runtime
    StateStore(root).append_event({"schema_version": 1, "event": "seed"})
    before = _snapshot(root)
    result = cli.main(["cleanup"])
    captured = capsys.readouterr()
    assert result == 0
    assert _snapshot(root) == before
    assert "attempted=0" in captured.out
    assert captured.err == ""


def test_cleanup_dry_run_does_not_construct_signal_backend(
    cli_runtime, monkeypatch, capsys
):
    monkeypatch.setattr(
        cli,
        "PidfdSignalBackend",
        lambda: (_ for _ in ()).throw(AssertionError("signaler constructed")),
    )
    assert cli.main(["cleanup"]) == 0
    capsys.readouterr()


def test_cleanup_dry_run_issues_short_lived_force_evidence_for_stubborn_process(
    cli_runtime, capsys
):
    root, tree, _procfs, clock = cli_runtime
    identity = tree.identity(321)
    assert identity is not None
    observed = ObservedTime(clock.wall_iso(), identity.boot_id, 40.0)
    ended = ObservedTime(clock.wall_iso(), identity.boot_id, 50.0)
    lease = SessionLease(
        1,
        "thr_stubborn",
        "/workspace",
        "startup",
        (identity.stable_key(),),
        "ended",
        observed,
        ended,
    )
    process = ManagedProcess(
        1,
        identity.stable_key(),
        "user",
        "safe-server",
        "/workspace",
        identity,
        None,
        (identity,),
        identity.pgid,
        frozenset({identity.stable_key()}),
        observed,
        owner_session_id=lease.session_id,
        first_owner_gone_boot=50.0,
        term_sent_boot=200.0,
        term_sent_keys=frozenset({identity.stable_key()}),
    )
    store = StateStore(root)
    store.save_session(lease)
    store.save_process(process)
    before = _snapshot(root)
    assert cli.main(["cleanup"]) == 0
    captured = capsys.readouterr()
    assert "force_confirmation=" in captured.out
    assert _snapshot(root) == before


def test_cleanup_force_without_apply_is_safe_usage_error(cli_runtime, capsys):
    result = cli.main(["cleanup", "--force", "--confirm", "ARG-CANARY"])
    captured = capsys.readouterr()
    assert result == 2
    assert "ARG-CANARY" not in captured.out + captured.err


def test_corrupt_state_is_nonzero_and_never_constructs_signaler(
    cli_runtime, monkeypatch, capsys
):
    root, _tree, _procfs, _clock = cli_runtime
    sessions = root / "sessions"
    make_private_directory(sessions)
    write_private_file(sessions / ("a" * 64 + ".json"), b"{CORRUPT-CANARY}\n")
    monkeypatch.setattr(
        cli,
        "PidfdSignalBackend",
        lambda: (_ for _ in ()).throw(AssertionError("signal backend constructed")),
    )
    result = cli.main(["cleanup", "--apply"])
    captured = capsys.readouterr()
    assert result != 0
    assert "CORRUPT-CANARY" not in captured.out + captured.err


def test_audit_redacts_session_cwd_and_environment_canaries(
    cli_runtime, monkeypatch, capsys
):
    root, tree, _procfs, clock = cli_runtime
    identity = tree.identity(321)
    assert identity is not None
    observed = ObservedTime(clock.wall_iso(), identity.boot_id, clock.boottime())
    lease = SessionLease(
        1,
        "SESSION-CANARY",
        "/CWD-CANARY",
        "startup",
        (identity.stable_key(),),
        "active",
        observed,
    )
    process = ManagedProcess(
        1,
        identity.stable_key(),
        "user",
        "safe-server",
        "/CWD-CANARY",
        identity,
        None,
        (identity,),
        identity.pgid,
        frozenset({identity.stable_key()}),
        observed,
        owner_session_id=lease.session_id,
    )
    store = StateStore(root)
    store.save_session(lease)
    store.save_process(process)
    monkeypatch.setenv("SECRET_ENV_CANARY", "ENV-VALUE-CANARY")
    assert cli.main(["audit", "--json"]) == 0
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    for canary in (
        "SESSION-CANARY",
        "CWD-CANARY",
        "ENV-VALUE-CANARY",
    ):
        assert canary not in rendered


def test_supervise_requires_separator_and_never_logs_command_or_args(
    cli_runtime, monkeypatch, capsys
):
    requests: list[SupervisorRequest] = []

    def run(request, *_args):
        requests.append(request)
        return 17

    monkeypatch.setattr(cli, "run_supervisor", run)
    result = cli.main(
        [
            "supervise",
            "--scope",
            "user",
            "--server",
            "safe-server",
            "--",
            "/COMMAND-CANARY",
            "ARG-CANARY",
        ]
    )
    captured = capsys.readouterr()
    assert result == 17
    assert requests[0].command == "/COMMAND-CANARY"
    assert requests[0].args == ("ARG-CANARY",)
    assert "COMMAND-CANARY" not in captured.out + captured.err
    assert "ARG-CANARY" not in captured.out + captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["supervise", "--scope", "user", "--server", "safe-server"],
        [
            "supervise",
            "--scope",
            "user",
            "--server",
            "safe-server",
            "/NO-SEPARATOR-CANARY",
        ],
        ["supervise", "--scope", "user", "--server", "safe-server", "--"],
    ],
)
def test_supervise_rejects_missing_separator_or_empty_command(
    cli_runtime, monkeypatch, capsys, argv
):
    monkeypatch.setattr(
        cli,
        "run_supervisor",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not supervise")),
    )
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert "NO-SEPARATOR-CANARY" not in captured.out + captured.err


def test_explain_reports_only_safe_exact_evidence(cli_runtime, capsys):
    _root, _tree, _procfs, _clock = cli_runtime
    assert cli.main(["explain", "321"]) == 0
    captured = capsys.readouterr()
    assert "pid=321" in captured.out
    assert "state=unmanaged" in captured.out
    assert captured.err == ""
