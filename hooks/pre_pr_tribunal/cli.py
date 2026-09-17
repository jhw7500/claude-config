#!/usr/bin/python3
"""Bounded JSON-only command line interface for tribunal state."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pre_pr_tribunal.model import (  # type: ignore
        MAX_REPORT_BYTES,
        MIXED_SLOT_VERDICT_SCHEMAS,
        VERDICT_SCHEMA_VERSION,
        Reviewer,
        TribunalError,
        validate_report_bytes,
    )
    from pre_pr_tribunal.git_state import (  # type: ignore
        capture_snapshot,
        capture_telemetry_candidate,
        check_telemetry_ignored,
    )
    from pre_pr_tribunal.review_context import (  # type: ignore
        reviewer_context_body,
        reviewer_context_envelope,
        snapshot_projection,
    )
    from pre_pr_tribunal.verdict_store import (  # type: ignore
        begin_round,
        finalize_round,
        migrate_legacy_pending_round,
        migrate_v2_pending_round,
        read_verdict,
        record_reviewer_failure,
        store_reviewer_report,
        submit_reviewer_report,
        validate_stored_reviewer_report,
    )
    from pre_pr_tribunal import telemetry, evidence_runtime, evidence_store, evidence_lifecycle  # type: ignore
else:
    from .model import (
        MAX_REPORT_BYTES,
        MIXED_SLOT_VERDICT_SCHEMAS,
        VERDICT_SCHEMA_VERSION,
        Reviewer,
        TribunalError,
        validate_report_bytes,
    )
    from .git_state import (
        capture_snapshot, capture_telemetry_candidate, check_telemetry_ignored,
    )
    from .review_context import (
        reviewer_context_body, reviewer_context_envelope, snapshot_projection,
    )
    from .verdict_store import (
        begin_round,
        finalize_round,
        migrate_legacy_pending_round,
        migrate_v2_pending_round,
        read_verdict,
        record_reviewer_failure,
        store_reviewer_report,
        submit_reviewer_report,
        validate_stored_reviewer_report,
    )
    from . import telemetry, evidence_runtime, evidence_store, evidence_lifecycle


class _Parser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        values = sys.argv[1:] if args is None else list(args)
        # Trailing unknown options are reported by the top-level parser, whose
        # prog does not contain the selected subcommand.
        self._evidence_command = bool(values and values[0].startswith('evidence-'))
        return super().parse_args(values, namespace)

    def error(self, message: str) -> None:
        if getattr(self, '_evidence_command', False) or 'evidence-' in self.prog:
            sys.stdout.write('{"reason_code":"EVIDENCE_USAGE"}\n')
            self.exit(2)
        self.exit(2, "PRE_PR_TRIBUNAL:USAGE\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser('evidence-capture', add_help=False)
    capture.add_argument('--base', required=True)
    capture.add_argument('--profile', required=True,
                         choices=('python-v1', 'node-lock-v1', 'node-sandbox-v1'))
    capture.add_argument('--cwd', required=True)
    capture.add_argument('--timeout', required=True, type=float)
    capture.add_argument('argv', nargs=argparse.REMAINDER)
    freeze = commands.add_parser('evidence-freeze', add_help=False)
    freeze.add_argument('--base', required=True)
    freeze.add_argument('--receipt', required=True, action='append')
    verify = commands.add_parser('evidence-verify', add_help=False)
    verify.add_argument('--bundle', required=True)
    verify.add_argument('--context', type=Path)
    verify.add_argument('--context-sha')
    project = commands.add_parser('evidence-project', add_help=False)
    project.add_argument('--reviewer-root', type=Path, required=True)
    begin = commands.add_parser("begin", add_help=False)
    begin.add_argument("--base", required=True)
    begin.add_argument("--runtime", required=True, choices=("claude", "codex"))
    begin.add_argument("--round", required=True, type=int, choices=(1, 2, 3))
    begin.add_argument("--decisions", type=Path)
    begin.add_argument('--evidence-bundle')
    begin.add_argument("--intensity", action="append")
    begin.add_argument("--intensity-requester", action="append")
    begin.add_argument("--intensity-reason", action="append")
    context = commands.add_parser("context", add_help=False)
    context.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
    submit = commands.add_parser("submit-report", add_help=False)
    submit.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
    failure = commands.add_parser("record-failure", add_help=False)
    failure.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
    failure.add_argument(
        "--reason",
        required=True,
        choices=("DISPATCH_FAILED", "REVIEWER_FAILED", "REVIEWER_TIMEOUT"),
    )
    commands.add_parser("migrate-legacy-pending", add_help=False)
    commands.add_parser("migrate-v2-pending", add_help=False)
    store = commands.add_parser("store-report", add_help=False)
    store.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
    store.add_argument("--replace-pending-recovery", action="store_true")
    validate = commands.add_parser("validate-report", add_help=False)
    validate.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
    validate.add_argument("--source", required=True, choices=("stdin", "stored"))
    finalize = commands.add_parser("finalize", add_help=False)
    finalize.add_argument("--reviewer-a", type=Path)
    finalize.add_argument("--reviewer-b", type=Path)
    finalize.add_argument("--reviewer-c", type=Path)
    commands.add_parser("status", add_help=False)
    start = commands.add_parser("telemetry-start", add_help=False)
    start.add_argument("--run-id", required=True)
    start.add_argument("--stage", required=True, choices=tuple(item.value for item in telemetry.TelemetryStage))
    start.add_argument("--reviewer", choices=("A", "B", "C"))
    start.add_argument("--attempt", required=True, type=int)
    for name in ("telemetry-finish", "telemetry-close"):
        terminal = commands.add_parser(name, add_help=False)
        terminal.add_argument("--run-id", required=True)
        if name == "telemetry-finish":
            terminal.add_argument("--span-id", required=True)
        terminal.add_argument("--outcome", required=True, choices=tuple(item.value for item in telemetry.TelemetryOutcome))
        terminal.add_argument("--reason-code")
    recover = commands.add_parser("telemetry-recover", add_help=False)
    recover.add_argument("--run-id", required=True)
    resume = commands.add_parser("telemetry-resume", add_help=False)
    resume.add_argument("--runtime", required=True, choices=("claude", "codex"))
    summary = commands.add_parser("telemetry-summary", add_help=False)
    summary.add_argument("--run-id")
    return parser


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _telemetry_unavailable(error: Exception) -> dict[str, str]:
    code = error.code if isinstance(error, TribunalError) else None
    if code not in {"TELEMETRY_INVALID", "TELEMETRY_FILE_UNSAFE", "TELEMETRY_TOO_LARGE", "TELEMETRY_CLOCK_ANOMALY"}:
        code = "TELEMETRY_INVALID"
    return {"status": "unavailable", "reason_code": code}


def _begin_with_telemetry(cwd, arguments, *, wall_clock=utc_now, monotonic_ns=time.monotonic_ns):
    run = span = None
    try:
        started_at, started_ns = wall_clock(), monotonic_ns()
        check_telemetry_ignored(cwd)
        run = telemetry.create_run(
            cwd, base_ref=arguments.base, runtime=arguments.runtime,
            round_number=arguments.round, started_at=started_at,
            started_monotonic_ns=started_ns,
        )
        # Start at entry clocks, including candidate lookup in the observed preflight.
        span = telemetry.start_span(
            cwd, run_id=run.run_id, stage=telemetry.TelemetryStage.SNAPSHOT_PREFLIGHT,
            reviewer=None, attempt=1, started_at=started_at, started_monotonic_ns=started_ns,
        )
        candidate = capture_telemetry_candidate(cwd)
        telemetry.record_candidate(cwd, run_id=run.run_id, repository=candidate.repository,
                                   head_ref=candidate.head_ref, head_sha=candidate.head_sha)
        projection = {"status": "active", "run_id": run.run_id}
    except Exception as error:
        projection = _telemetry_unavailable(error)

    # Keep the primary transaction outside every telemetry exception handler.
    try:
        verdict = begin_round(
            cwd, base=arguments.base, runtime=arguments.runtime,
            round_number=arguments.round, decisions_path=arguments.decisions,
            evidence_bundle_sha256=getattr(arguments, 'evidence_bundle', None),
            intensity_values=getattr(arguments, "intensity", None),
            intensity_requester=getattr(arguments, "intensity_requester", None),
            intensity_reason=getattr(arguments, "intensity_reason", None),
        )
    except TribunalError as primary_error:
        try:
            if run is not None:
                ended_at, ended_ns = wall_clock(), monotonic_ns()
                if span is not None:
                    telemetry.finish_span(
                        cwd, run_id=run.run_id, span_id=span.span_id,
                        outcome=telemetry.TelemetryOutcome.FAILURE, reason_code=primary_error.code,
                        ended_at=ended_at, ended_monotonic_ns=ended_ns,
                    )
                telemetry.close_run(cwd, run_id=run.run_id,
                    outcome=telemetry.TelemetryOutcome.FAILURE, reason_code=primary_error.code,
                    ended_at=ended_at, ended_monotonic_ns=ended_ns)
        except Exception:
            pass
        raise
    try:
        if run is not None:
            telemetry.bind_run(cwd, run_id=run.run_id, snapshot=verdict.snapshot,
                               lifecycle_id=verdict.lifecycle_id)
            if span is not None:
                telemetry.finish_span(
                    cwd, run_id=run.run_id, span_id=span.span_id,
                    outcome=telemetry.TelemetryOutcome.SUCCESS, reason_code=None,
                    ended_at=wall_clock(), ended_monotonic_ns=monotonic_ns(),
                )
    except Exception as error:
        projection = _telemetry_unavailable(error)
    return verdict, projection


def _telemetry_command(cwd, arguments, *, wall_clock=utc_now, monotonic_ns=time.monotonic_ns):
    try:
        check_telemetry_ignored(cwd)
        if arguments.command == "telemetry-resume":
            run = telemetry.resume_run(cwd, runtime=arguments.runtime,
                                       started_at=wall_clock(), started_monotonic_ns=monotonic_ns())
            return {"status": "active", "run_id": run.run_id}
        run_id = arguments.run_id
        if arguments.command == "telemetry-summary":
            return telemetry.summarize_run(cwd, run_id=run_id)
        if arguments.command == "telemetry-start":
            span = telemetry.start_span(
                cwd, run_id=run_id, stage=telemetry.TelemetryStage(arguments.stage),
                reviewer=Reviewer(arguments.reviewer) if arguments.reviewer else None,
                attempt=arguments.attempt, started_at=wall_clock(), started_monotonic_ns=monotonic_ns(),
            )
            return {"run_id": run_id, "span_id": span.span_id, "stage": span.stage.value,
                    "reviewer": span.reviewer.value if span.reviewer else None, "status": "running"}
        if arguments.command == "telemetry-finish":
            span = telemetry.finish_span(
                cwd, run_id=run_id, span_id=arguments.span_id,
                outcome=telemetry.TelemetryOutcome(arguments.outcome), reason_code=arguments.reason_code,
                ended_at=wall_clock(), ended_monotonic_ns=monotonic_ns(),
            )
            return {"run_id": run_id, "span_id": span.span_id,
                    "status": span.outcome.value, "duration_ms": span.duration_ms}
        if arguments.command == "telemetry-recover":
            recovered = telemetry.recover_run(cwd, run_id=run_id,
                ended_at=wall_clock(), ended_monotonic_ns=monotonic_ns())
            return {"run_id": run_id, "recovered_count": recovered}
        run = telemetry.close_run(cwd, run_id=run_id,
            outcome=telemetry.TelemetryOutcome(arguments.outcome), reason_code=arguments.reason_code,
            ended_at=wall_clock(), ended_monotonic_ns=monotonic_ns())
        return {"run_id": run_id, "status": run.outcome.value}
    except Exception as error:
        raise TribunalError(_telemetry_unavailable(error)["reason_code"]) from None


def _status(verdict) -> dict[str, object]:
    payload: dict[str, object] = {
        "round": verdict.round,
        "gate_status": verdict.gate.status.value,
        "blocking_count": verdict.gate.blocking_count,
        "verdict_path": ".review/verdict.json",
        "verdict_schema": verdict.schema,
    }
    if verdict.schema in MIXED_SLOT_VERDICT_SCHEMAS:
        reviewers = {}
        for key in "ABC":
            slot = verdict.reviewers[key]
            projected = {
                "state": slot.status,
                "attempt_count": slot.attempt_count,
                "last_error": slot.last_error,
            }
            if slot.status == "sealed":
                receipt = slot.receipt
                if receipt is None:
                    raise TribunalError("VERDICT_INVALID")
                projected.update(
                    raw_sha256=receipt.raw_sha256,
                    context_sha256=receipt.context_sha256,
                    report_contract_version=receipt.report_contract_version,
                    provenance=receipt.provenance,
                )
            reviewers[key] = projected
        payload["reviewers"] = reviewers
    if verdict.schema == VERDICT_SCHEMA_VERSION:
        if verdict.policy is None:
            raise TribunalError("POLICY_INVALID")
        payload["policy"] = verdict.policy.to_json()
        payload["active_reviewers"] = list(verdict.policy.active_reviewers)
        payload["pr_appendix"] = _pr_appendix(verdict)
    return payload


def _pr_appendix(verdict) -> str:
    header = ["## Pre-PR tribunal advisory findings", ""]
    advisory_lines = []
    for key in verdict.policy.active_reviewers:
        report = verdict.reviewers[key].report
        if report is None:
            continue
        for finding in report.findings:
            if finding.severity.value not in {"MEDIUM", "LOW"}:
                continue
            location = finding.path + (
                f":{finding.line}" if finding.line is not None else ""
            )
            advisory_lines.append(
                f"- [{finding.severity.value}] {_markdown_text(finding.title)} "
                f"({_markdown_text(location)}, reviewer {key})"
            )
    if not advisory_lines:
        return ""
    maximum = 32 * 1024
    rendered = "\n".join((*header, *advisory_lines)) + "\n"
    if len(rendered.encode("utf-8")) <= maximum:
        return rendered

    marker = "- Additional advisory findings were omitted to keep this appendix within 32 KiB."
    bounded = list(header)
    for line in advisory_lines:
        candidate = "\n".join((*bounded, line, marker)) + "\n"
        if len(candidate.encode("utf-8")) <= maximum:
            bounded.append(line)
    return "\n".join((*bounded, marker)) + "\n"


def _markdown_text(value: str) -> str:
    escaped = []
    for character in value:
        if character in "\r\n\t":
            escaped.append(" ")
        elif character in "\\`*_{}[]<>()#+-.!|>":
            escaped.append("\\" + character)
        else:
            escaped.append(character)
    return "".join(escaped)


def _report_stdin() -> bytes:
    return sys.stdin.buffer.read(MAX_REPORT_BYTES + 1)


def _report_projection(
    reviewer: Reviewer, round_number: int, status: str, digest: str
) -> dict[str, object]:
    if status not in {"stored", "valid"}:
        raise TribunalError("VERDICT_INVALID")
    return {
        "reviewer": reviewer.value,
        "round": round_number,
        "status": status,
        "raw_sha256": digest,
    }


def _require_all_pending(verdict, reviewer: Reviewer | None = None) -> None:
    active = (
        verdict.policy.active_reviewers
        if verdict.schema == VERDICT_SCHEMA_VERSION and verdict.policy is not None
        else tuple("ABC")
    )
    if reviewer is not None and reviewer.value not in active:
        raise TribunalError("REVIEWER_DISABLED")
    if verdict.gate.status.value != "in_progress" or any(
        verdict.reviewers[key].status != "pending" for key in active
    ):
        raise TribunalError("ROUND_NOT_IN_PROGRESS")


def _snapshot_equal(verdict, snapshot) -> bool:
    return (
        verdict.repository,
        verdict.base_ref,
        verdict.base_sha,
        verdict.head_ref,
        verdict.head_sha,
        verdict.merge_base_sha,
        verdict.diff_sha256,
    ) == (
        snapshot.repository,
        snapshot.base_ref,
        snapshot.base_sha,
        snapshot.head_ref,
        snapshot.head_sha,
        snapshot.merge_base_sha,
        snapshot.diff_sha256,
    )


def main(argv: list[str] | None = None, *, wall_clock=utc_now, monotonic_ns=time.monotonic_ns) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    cwd = Path.cwd()
    exit_status = 0
    try:
        if arguments.command == 'evidence-capture':
            argv = arguments.argv
            if argv[:1] == ['--']:
                argv = argv[1:]
            payload = evidence_runtime.capture_evidence(cwd, base=arguments.base,
                profile=arguments.profile, command_cwd=arguments.cwd, argv=argv,
                timeout_seconds=arguments.timeout)
            payload['reusable'] = 'receipt_sha256' in payload and payload.get('freshness') == 'deterministic'
            actual_exit = payload['exit_code']
            exit_status = (min(255, actual_exit) if actual_exit > 0 else min(255, 128 - actual_exit)) if actual_exit else (1 if actual_exit is None else 0)
            if payload['timed_out']:
                exit_status = 124
            elif 'receipt_sha256' not in payload:
                exit_status = exit_status or 1
        elif arguments.command == 'evidence-freeze':
            payload = evidence_runtime.freeze_evidence(cwd, base=arguments.base, receipt_sha256s=arguments.receipt)
        elif arguments.command == 'evidence-project':
            payload = evidence_lifecycle.project_evidence(cwd, reviewer_root=arguments.reviewer_root)
        elif arguments.command == 'evidence-verify' and (arguments.context is not None or arguments.context_sha is not None):
            if arguments.context is None or arguments.context_sha is None:
                raise TribunalError('EVIDENCE_CONTEXT_REQUIRED')
            payload = evidence_lifecycle.verify_detached_evidence(cwd, bundle_sha256=arguments.bundle,
                context_path=arguments.context, expected_context_sha256=arguments.context_sha)
        elif arguments.command == 'evidence-verify':
            # Standalone facts can establish eligibility, never round authority.
            bundle = evidence_store.read_bundle(cwd, arguments.bundle)
            verified = evidence_runtime.verify_evidence(cwd, bundle_sha256=arguments.bundle,
                expected_binding=bundle['binding'])
            payload = {'bundle_sha256': arguments.bundle, 'round_authority': False,
                'eligible': [entry['id'] for entry in verified['eligible']], 'rejected': verified['rejected']}
        elif arguments.command == "begin":
            verdict, observation = _begin_with_telemetry(
                cwd, arguments, wall_clock=wall_clock, monotonic_ns=monotonic_ns,
            )
            payload = {
                "schema": 1,
                "round": verdict.round,
                "snapshot": snapshot_projection(verdict),
                "initial_paths": list(verdict.initial_paths),
                "gate": verdict.gate.to_json(),
                "telemetry": observation,
            }
            if verdict.policy is not None:
                payload["policy"] = verdict.policy.to_json()
                payload["active_reviewers"] = list(verdict.policy.active_reviewers)
        elif arguments.command.startswith("telemetry-"):
            payload = _telemetry_command(cwd, arguments, wall_clock=wall_clock, monotonic_ns=monotonic_ns)
        elif arguments.command == "context":
            verdict = read_verdict(cwd)
            reviewer = Reviewer(arguments.reviewer)
            payload = (
                reviewer_context_body(verdict, reviewer)
                if verdict.schema == 1
                else reviewer_context_envelope(verdict, reviewer)
            )
        elif arguments.command == "submit-report":
            receipt = submit_reviewer_report(
                cwd,
                reviewer=Reviewer(arguments.reviewer),
                raw=_report_stdin(),
            )
            payload = {
                "reviewer": receipt.reviewer.value,
                "round": receipt.round,
                "state": "sealed",
                "raw_sha256": receipt.raw_sha256,
                "context_sha256": receipt.context_sha256,
                "report_contract_version": receipt.report_contract_version,
                "attempt": receipt.attempt,
                "provenance": receipt.provenance,
            }
        elif arguments.command == "record-failure":
            slot = record_reviewer_failure(
                cwd,
                reviewer=Reviewer(arguments.reviewer),
                reason_code=arguments.reason,
            )
            payload = {
                "state": slot.status,
                "attempt_count": slot.attempt_count,
                "last_error": slot.last_error,
            }
        elif arguments.command == "migrate-legacy-pending":
            migrated = migrate_legacy_pending_round(cwd)
            payload = {
                "round": migrated.round,
                "reviewers": dict(migrated.reviewers),
            }
        elif arguments.command == "migrate-v2-pending":
            migrated = migrate_v2_pending_round(cwd)
            payload = {
                "round": migrated.round,
                "reviewers": dict(migrated.reviewers),
                "telemetry_history": migrated.telemetry_history,
            }
        elif arguments.command == "store-report":
            receipt = store_reviewer_report(
                cwd,
                reviewer=Reviewer(arguments.reviewer),
                raw=_report_stdin(),
                replace_pending_recovery=arguments.replace_pending_recovery,
            )
            payload = _report_projection(
                receipt.reviewer, receipt.round, "stored", receipt.raw_sha256
            )
        elif arguments.command == "validate-report":
            reviewer = Reviewer(arguments.reviewer)
            if arguments.source == "stored":
                parsed, digest = validate_stored_reviewer_report(
                    cwd, reviewer=reviewer
                )
                payload = _report_projection(reviewer, parsed.round, "valid", digest)
            else:
                verdict = read_verdict(cwd)
                _require_all_pending(verdict, reviewer)
                snapshot = capture_snapshot(cwd, verdict.base_ref)
                if not _snapshot_equal(verdict, snapshot):
                    raise TribunalError("SNAPSHOT_CHANGED")
                _parsed, digest = validate_report_bytes(
                    _report_stdin(),
                    expected_reviewer=reviewer,
                    expected_round=verdict.round,
                    snapshot=snapshot,
                )
                payload = _report_projection(reviewer, verdict.round, "valid", digest)
        elif arguments.command == "finalize":
            supplied = {
                key: value
                for key, value in {
                    "A": arguments.reviewer_a,
                    "B": arguments.reviewer_b,
                    "C": arguments.reviewer_c,
                }.items()
                if value is not None
            }
            reviewer_paths = supplied or None
            verdict = finalize_round(
                cwd,
                reviewer_paths=reviewer_paths,
            )
            payload = _status(verdict)
        else:
            payload = _status(read_verdict(cwd))
    except TribunalError as error:
        code = (
            error.code
            if isinstance(error.code, str)
            and error.code.isascii()
            and error.code.replace("_", "").isalnum()
            else "VERDICT_INVALID"
        )
        if arguments.command.startswith('evidence-'):
            sys.stdout.write(json.dumps({'reason_code': code[:64]}) + '\n')
        else:
            sys.stderr.write(f"PRE_PR_TRIBUNAL:{code[:64]}\n")
        return 1
    except Exception:
        if arguments.command.startswith('evidence-'):
            sys.stdout.write('{"reason_code":"EVIDENCE_INVALID"}\n')
        else:
            sys.stderr.write("PRE_PR_TRIBUNAL:VERDICT_INVALID\n")
        return 1
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
