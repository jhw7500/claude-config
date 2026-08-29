from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from .classify import build_audit
from .cleanup import (
    InvalidForceConfirmation,
    PidfdSignalBackend,
    PidfdUnavailable,
    execute_cleanup,
    issue_force_token,
    plan_cleanup,
)
from .clock import SystemClock
from .hook import SystemdNotifier, handle_payload
from .model import AuditSnapshot, Classification, CleanupReport
from .procfs import LinuxProcfs
from .state import StateCorruption, StateLockTimeout, StateStore, UnsafeStatePath
from .supervisor import SupervisorRequest, run_supervisor


class _UsageError(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _UsageError("usage error")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="codex-mcp-ownership")
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--json", action="store_true")
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--apply", action="store_true")
    cleanup_parser.add_argument("--force", action="store_true")
    cleanup_parser.add_argument("--confirm")
    explain_parser = subparsers.add_parser("explain")
    explain_parser.add_argument("pid", type=int)
    subparsers.add_parser("hook")
    supervise_parser = subparsers.add_parser("supervise")
    supervise_parser.add_argument("--scope", required=True)
    supervise_parser.add_argument("--server", required=True)
    supervise_parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _state_root() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "claude-config" / "codex-mcp-ownership"


def _owner_reference(classification: Classification) -> str:
    session_id = classification.process.owner_session_id
    if session_id is None:
        return "none"
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return "sha256:" + digest[:16]


def _safe_classification(classification: Classification) -> dict[str, object]:
    return {
        "scope": classification.process.scope,
        "server": classification.process.server,
        "owner": _owner_reference(classification),
        "workspace": "<redacted>",
        "state": classification.state,
        "reason_codes": list(classification.reason_codes),
        "pid": classification.process.wrapper.pid,
        "identity": classification.process.wrapper.stable_key()[:16],
        "grace_deadline_boot": classification.grace_deadline_boot,
        "eligible_term": classification.eligible_term,
    }


def _report_dict(
    snapshot: AuditSnapshot,
    report: CleanupReport | None = None,
) -> dict[str, object]:
    if report is None:
        before_count = snapshot.process_count
        before_rss = snapshot.rss_kib
        after_count = snapshot.process_count
        after_rss = snapshot.rss_kib
        attempted = terminated = survived = skipped = 0
    else:
        before_count = report.before_count
        before_rss = report.before_rss_kib
        after_count = report.after_count
        after_rss = report.after_rss_kib
        attempted = report.attempted
        terminated = report.terminated
        survived = report.survived
        skipped = report.skipped
    return {
        "schema_version": 1,
        "state_counts": dict(snapshot.state_counts),
        "process_count": snapshot.process_count,
        "rss_kib": snapshot.rss_kib,
        "ownership_coverage": dict(snapshot.ownership_coverage),
        "before_count": before_count,
        "before_rss_kib": before_rss,
        "after_count": after_count,
        "after_rss_kib": after_rss,
        "attempted": attempted,
        "terminated": terminated,
        "survived": survived,
        "skipped": skipped,
        "classifications": [
            _safe_classification(item) for item in snapshot.classifications
        ],
    }


def _human_report(snapshot: AuditSnapshot, report: CleanupReport | None = None) -> str:
    values = _report_dict(snapshot, report)
    counts = " ".join(f"{state}={count}" for state, count in snapshot.state_counts)
    lines = [
        f"states {counts}",
        (
            f"before_count={values['before_count']} "
            f"before_rss_kib={values['before_rss_kib']} "
            f"after_count={values['after_count']} "
            f"after_rss_kib={values['after_rss_kib']}"
        ),
        (
            f"attempted={values['attempted']} terminated={values['terminated']} "
            f"survived={values['survived']} skipped={values['skipped']}"
        ),
    ]
    for item in values["classifications"]:
        assert isinstance(item, dict)
        lines.append(
            " ".join(
                (
                    f"scope={item['scope']}",
                    f"server={item['server']}",
                    f"pid={item['pid']}",
                    f"state={item['state']}",
                    f"owner={item['owner']}",
                    "workspace=<redacted>",
                    "reasons=" + ",".join(item["reason_codes"]),
                )
            )
        )
    return "\n".join(lines) + "\n"


def _diagnostic(message: str) -> int:
    sys.stderr.write(f"codex-mcp-ownership: {message}\n")
    return 1


def _runtime() -> tuple[StateStore, LinuxProcfs, SystemClock]:
    return StateStore(_state_root()), LinuxProcfs(), SystemClock()


def _audit_command(as_json: bool) -> int:
    store, procfs, clock = _runtime()
    snapshot = build_audit(store, procfs, clock)
    if snapshot.corrupt_count:
        return _diagnostic("state unavailable")
    if as_json:
        sys.stdout.write(json.dumps(_report_dict(snapshot), sort_keys=True) + "\n")
    else:
        sys.stdout.write(_human_report(snapshot))
    return 0


def _cleanup_command(apply: bool, force: bool, confirm: str | None) -> int:
    if force and not apply:
        raise _UsageError("force requires apply")
    store, procfs, clock = _runtime()
    snapshot = build_audit(store, procfs, clock)
    if snapshot.corrupt_count:
        return _diagnostic("state unavailable")
    actions = plan_cleanup(snapshot, force=force)
    if apply and actions:
        try:
            signaler = PidfdSignalBackend()
        except PidfdUnavailable:

            class _Unavailable:
                def open(self, identity):
                    del identity
                    raise PidfdUnavailable("pidfd signaling is unavailable")

                def send(self, pidfd, signum):
                    del pidfd, signum

                def close(self, pidfd):
                    del pidfd

            signaler = _Unavailable()
    else:

        class _Unused:
            def open(self, identity):
                del identity
                raise AssertionError("empty cleanup plan opened a pidfd")

            def send(self, pidfd, signum):
                del pidfd, signum

            def close(self, pidfd):
                del pidfd

        signaler = _Unused()
    report = execute_cleanup(
        actions,
        store,
        procfs,
        signaler,
        clock,
        apply=apply,
        confirm_token=confirm,
    )
    sys.stdout.write(_human_report(snapshot, report))
    if not apply:
        for item in snapshot.classifications:
            if item.state == "stubborn":
                token = issue_force_token(item, clock)
                sys.stdout.write(
                    f"pid={item.process.wrapper.pid} force_confirmation={token}\n"
                )
    return 0


def _explain_command(pid: int) -> int:
    store, procfs, clock = _runtime()
    snapshot = build_audit(store, procfs, clock)
    if snapshot.corrupt_count:
        return _diagnostic("state unavailable")
    matches = [
        item
        for item in snapshot.classifications
        if any(identity.pid == pid for identity in item.live_identities)
        or item.process.wrapper.pid == pid
    ]
    if not matches:
        identity = procfs.identity(pid)
        state = "unmanaged" if identity is not None else "gone"
        sys.stdout.write(f"pid={pid} state={state} signal_allowed=false\n")
        return 0
    for item in matches:
        safe = _safe_classification(item)
        sys.stdout.write(
            f"pid={pid} state={safe['state']} identity={safe['identity']} "
            f"signal_allowed={'true' if safe['eligible_term'] else 'false'} "
            f"reasons={','.join(safe['reason_codes'])}\n"
        )
    return 0


def _hook_command() -> int:
    try:
        payload = json.load(sys.stdin)
        store, procfs, clock = _runtime()
        handle_payload(payload, store, procfs, clock, SystemdNotifier())
    except BaseException:
        pass
    return 0


def _supervise_command(scope: str, server: str, command: list[str]) -> int:
    if not command or command[0] != "--" or len(command) == 1:
        raise _UsageError("supervise requires a command separator")
    remainder = command[1:]
    request = SupervisorRequest(
        scope=scope,
        server=server,
        command=remainder[0],
        args=tuple(remainder[1:]),
        cwd=os.getcwd(),
    )
    store, procfs, clock = _runtime()
    return run_supervisor(request, store, procfs, clock)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command_name == "hook":
            return _hook_command()
        if arguments.command_name == "audit":
            return _audit_command(arguments.json)
        if arguments.command_name == "cleanup":
            return _cleanup_command(
                arguments.apply,
                arguments.force,
                arguments.confirm,
            )
        if arguments.command_name == "explain":
            return _explain_command(arguments.pid)
        if arguments.command_name == "supervise":
            return _supervise_command(
                arguments.scope,
                arguments.server,
                arguments.command,
            )
        raise _UsageError("unknown command")
    except _UsageError:
        sys.stderr.write("codex-mcp-ownership: usage error\n")
        return 2
    except InvalidForceConfirmation:
        return _diagnostic("force confirmation invalid")
    except (StateCorruption, StateLockTimeout, UnsafeStatePath, OSError, ValueError):
        return _diagnostic("state unavailable")
