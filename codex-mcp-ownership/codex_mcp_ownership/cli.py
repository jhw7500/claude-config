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
    select_force_actions,
)
from .clock import SystemClock
from .hook import SystemdNotifier, handle_payload
from .model import AuditSnapshot, Classification, CleanupReport, ProcessIdentity
from .procfs import IdentityObservation, LinuxProcfs
from .state import StateCorruption, StateLockTimeout, StateStore, UnsafeStatePath
from .supervisor import SupervisorRequest, run_supervisor


class _UsageError(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        del message
        raise _UsageError("usage error")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="codex-mcp-ownership")
    subparsers = parser.add_subparsers(
        dest="command_name",
        required=True,
        parser_class=_SafeArgumentParser,
    )
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
        before_state_counts = snapshot.state_counts
        after_state_counts = snapshot.state_counts
        before_classifications = snapshot.classifications
        after_classifications = snapshot.classifications
        outcomes = ()
    else:
        before_count = report.before_count
        before_rss = report.before_rss_kib
        after_count = report.after_count
        after_rss = report.after_rss_kib
        attempted = report.attempted
        terminated = report.terminated
        survived = report.survived
        skipped = report.skipped
        before_state_counts = report.before_state_counts
        after_state_counts = report.after_state_counts
        before_classifications = report.before_classifications
        after_classifications = report.after_classifications
        outcomes = report.outcomes
        after_state_available = report.after_state_available
        authority_lost = report.authority_lost
    if report is None:
        after_state_available = True
        authority_lost = False
    rendered = {
        "schema_version": 1,
        "ownership_coverage": dict(snapshot.ownership_coverage),
        "before_count": before_count,
        "before_rss_kib": before_rss,
        "after_count": after_count,
        "after_rss_kib": after_rss,
        "after_state_available": after_state_available,
        "authority_lost": authority_lost,
        "attempted": attempted,
        "terminated": terminated,
        "survived": survived,
        "skipped": skipped,
        "before_state_counts": dict(before_state_counts),
        "after_state_counts": dict(after_state_counts),
        "before_classifications": [
            _safe_classification(item) for item in before_classifications
        ],
        "after_classifications": [
            _safe_classification(item) for item in after_classifications
        ],
        "outcomes": [
            {
                "pid": item.action.identity.pid,
                "identity": item.action.identity.stable_key()[:16],
                "status": item.status,
                "reason": item.reason,
            }
            for item in outcomes
        ],
        "classifications": [
            _safe_classification(item) for item in snapshot.classifications
        ],
    }
    prefix = "" if report is None else "preflight_"
    rendered[prefix + "state_counts"] = dict(snapshot.state_counts)
    rendered[prefix + "process_count"] = snapshot.process_count
    rendered[prefix + "rss_kib"] = snapshot.rss_kib
    return rendered


def _human_report(snapshot: AuditSnapshot, report: CleanupReport | None = None) -> str:
    values = _report_dict(snapshot, report)
    before_counts = " ".join(
        f"{state}={count}" for state, count in values["before_state_counts"].items()
    )
    after_counts = " ".join(
        f"{state}={count}" for state, count in values["after_state_counts"].items()
    )
    lines = [
        f"before_states {before_counts}",
        f"after_states {after_counts}",
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
        (
            f"after_state_available={str(values['after_state_available']).lower()} "
            f"authority_lost={str(values['authority_lost']).lower()}"
        ),
    ]
    for phase in ("before", "after"):
        classifications = values[f"{phase}_classifications"]
        assert isinstance(classifications, list)
        for item in classifications:
            assert isinstance(item, dict)
            lines.append(
                " ".join(
                    (
                        f"phase={phase}",
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
    for item in values["outcomes"]:
        assert isinstance(item, dict)
        lines.append(
            " ".join(
                (
                    "outcome",
                    f"pid={item['pid']}",
                    f"identity={item['identity']}",
                    f"status={item['status']}",
                    f"reason={item['reason']}",
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
    try:
        audited_root_token = store.root_token()
    except FileNotFoundError:
        audited_root_token = None
    snapshot = build_audit(store, procfs, clock)
    if snapshot.corrupt_count:
        if apply and audited_root_token is not None:
            with store.locked(expected_root_token=audited_root_token):
                store.load_sessions()
                store.load_processes()
        return _diagnostic("state unavailable")
    actions = (
        select_force_actions(snapshot, confirm, clock)
        if force
        else plan_cleanup(snapshot)
    )
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
    matches: list[tuple[Classification, ProcessIdentity]] = []
    for item in snapshot.classifications:
        process = item.process
        recorded = (
            (process.wrapper,)
            + (() if process.child is None else (process.child,))
            + process.members
        )
        for identity in recorded:
            if identity.pid == pid:
                matches.append((item, identity))
                break
    if not matches:
        observation = _explain_observation(procfs, pid)
        sys.stdout.write(
            f"pid={pid} state=unmanaged observed={observation.kind} "
            "recorded=false signal_allowed=false reasons=not_managed\n"
        )
        return 0
    for item, recorded_identity in matches:
        observation = _explain_observation(procfs, pid)
        exact_live = bool(
            observation.kind == "live" and observation.identity == recorded_identity
        )
        refusal = list(item.reason_codes)
        if observation.kind == "missing":
            refusal.append("identity_missing")
        elif observation.kind == "unavailable":
            refusal.append("identity_unavailable")
        elif not exact_live:
            refusal.append("identity_changed")
        safe = _safe_classification(item)
        sys.stdout.write(
            f"pid={pid} state={safe['state']} observed={observation.kind} "
            f"recorded_identity={recorded_identity.stable_key()[:16]} "
            f"owner={safe['owner']} grace_deadline_boot={safe['grace_deadline_boot']} "
            f"signal_allowed={'true' if safe['eligible_term'] and exact_live else 'false'} "
            f"reasons={','.join(refusal)}\n"
        )
    return 0


def _explain_observation(procfs: LinuxProcfs, pid: int) -> IdentityObservation:
    try:
        observation = procfs.observe_identity(pid)
    except (OSError, ValueError):
        return IdentityObservation("unavailable", None)
    if not isinstance(observation, IdentityObservation):
        return IdentityObservation("unavailable", None)
    return observation


def _hook_command() -> int:
    try:
        maximum = 65_536
        binary = getattr(sys.stdin, "buffer", None)
        if binary is not None:
            raw = binary.read(maximum + 1)
            if len(raw) > maximum:
                return 0
            payload = json.loads(raw.decode("utf-8"))
        else:
            raw_text = sys.stdin.read(maximum + 1)
            if len(raw_text.encode("utf-8")) > maximum:
                return 0
            payload = json.loads(raw_text)
        store, procfs, clock = _runtime()
        handle_payload(payload, store, procfs, clock, SystemdNotifier())
    except BaseException:
        pass
    return 0


def _supervise_command(scope: str, server: str, command: list[str]) -> int:
    if not command or command[0] != "--" or len(command) == 1:
        raise _UsageError("supervise requires a command separator")
    remainder = command[1:]
    if not remainder[0]:
        raise _UsageError("supervise requires a nonempty command")
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
