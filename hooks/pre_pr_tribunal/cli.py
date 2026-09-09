#!/usr/bin/python3
"""Bounded JSON-only command line interface for tribunal state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pre_pr_tribunal.model import (  # type: ignore
        MAX_COMMAND_TEXT_BYTES,
        MAX_EVIDENCE_TEXT_BYTES,
        MAX_EXECUTIONS_PER_REVIEWER,
        MAX_FINDINGS_PER_REVIEWER,
        MAX_REPORT_BYTES,
        MAX_VERDICT_BYTES,
        REPORT_TEXT_CONTRACT_VERSION,
        Reviewer,
        TribunalError,
        validate_report_bytes,
    )
    from pre_pr_tribunal.git_state import (  # type: ignore
        DIFF_RECIPE_VERSION,
        capture_snapshot,
        diff_contract,
    )
    from pre_pr_tribunal.verdict_store import (  # type: ignore
        begin_round,
        finalize_round,
        read_verdict,
        store_reviewer_report,
        validate_stored_reviewer_report,
    )
else:
    from .model import (
        MAX_COMMAND_TEXT_BYTES,
        MAX_EVIDENCE_TEXT_BYTES,
        MAX_EXECUTIONS_PER_REVIEWER,
        MAX_FINDINGS_PER_REVIEWER,
        MAX_REPORT_BYTES,
        MAX_VERDICT_BYTES,
        REPORT_TEXT_CONTRACT_VERSION,
        Reviewer,
        TribunalError,
        validate_report_bytes,
    )
    from .git_state import DIFF_RECIPE_VERSION, capture_snapshot, diff_contract
    from .verdict_store import (
        begin_round,
        finalize_round,
        read_verdict,
        store_reviewer_report,
        validate_stored_reviewer_report,
    )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "PRE_PR_TRIBUNAL:USAGE\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("begin", add_help=False)
    begin.add_argument("--base", required=True)
    begin.add_argument("--runtime", required=True, choices=("claude", "codex"))
    begin.add_argument("--round", required=True, type=int, choices=(1, 2, 3))
    begin.add_argument("--decisions", type=Path)
    context = commands.add_parser("context", add_help=False)
    context.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
    store = commands.add_parser("store-report", add_help=False)
    store.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
    store.add_argument("--replace-pending-recovery", action="store_true")
    validate = commands.add_parser("validate-report", add_help=False)
    validate.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
    validate.add_argument("--source", required=True, choices=("stdin", "stored"))
    finalize = commands.add_parser("finalize", add_help=False)
    finalize.add_argument("--reviewer-a", required=True, type=Path)
    finalize.add_argument("--reviewer-b", required=True, type=Path)
    finalize.add_argument("--reviewer-c", required=True, type=Path)
    commands.add_parser("status", add_help=False)
    return parser


def _snapshot(verdict) -> dict[str, object]:
    return {
        "repository": verdict.repository,
        "base": {"ref": verdict.base_ref, "sha": verdict.base_sha},
        "head_ref": verdict.head_ref,
        "head_sha": verdict.head_sha,
        "merge_base_sha": verdict.merge_base_sha,
        "diff_sha256": verdict.diff_sha256,
    }


def _status(verdict) -> dict[str, object]:
    return {
        "round": verdict.round,
        "gate_status": verdict.gate.status.value,
        "blocking_count": verdict.gate.blocking_count,
        "verdict_path": ".review/verdict.json",
    }


def _context_decision(decision) -> dict[str, object]:
    return {
        "id": decision.id,
        "finding_ref": {
            "round": decision.finding_round,
            "id": decision.finding_id,
            "reviewer": decision.reviewer.value,
        },
        "disposition": decision.disposition,
        "rationale": decision.rationale,
    }


def _context(verdict, reviewer: Reviewer) -> dict[str, object]:
    findings = [
        dict(item)
        for summary in verdict.history
        for item in summary.blocking_findings
        if item["reviewer"] == reviewer.value
    ]
    decisions = [
        _context_decision(item)
        for item in verdict.decisions
        if item.reviewer is reviewer
    ]
    return {
        "schema": 1,
        "round": verdict.round,
        "reviewer": reviewer.value,
        "snapshot": _snapshot(verdict),
        "own_prior_findings": findings,
        "own_decisions": decisions,
        "contract": {
            "report_text": REPORT_TEXT_CONTRACT_VERSION,
            "diff_recipe": DIFF_RECIPE_VERSION,
        },
        "diff_contract": diff_contract(verdict.snapshot),
        "limits": {
            "report_bytes": MAX_REPORT_BYTES,
            "verdict_bytes": MAX_VERDICT_BYTES,
            "evidence_text_bytes": MAX_EVIDENCE_TEXT_BYTES,
            "command_text_bytes": MAX_COMMAND_TEXT_BYTES,
            "findings": MAX_FINDINGS_PER_REVIEWER,
            "executions": MAX_EXECUTIONS_PER_REVIEWER,
        },
    }


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


def _require_all_pending(verdict) -> None:
    if verdict.gate.status.value != "in_progress" or any(
        item.status != "pending" for item in verdict.reviewers.values()
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


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    cwd = Path.cwd()
    try:
        if arguments.command == "begin":
            verdict = begin_round(
                cwd,
                base=arguments.base,
                runtime=arguments.runtime,
                round_number=arguments.round,
                decisions_path=arguments.decisions,
            )
            payload = {
                "schema": 1,
                "round": verdict.round,
                "snapshot": _snapshot(verdict),
                "initial_paths": list(verdict.initial_paths),
                "gate": verdict.gate.to_json(),
            }
        elif arguments.command == "context":
            payload = _context(read_verdict(cwd), Reviewer(arguments.reviewer))
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
                _require_all_pending(verdict)
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
            verdict = finalize_round(
                cwd,
                reviewer_paths={
                    "A": arguments.reviewer_a,
                    "B": arguments.reviewer_b,
                    "C": arguments.reviewer_c,
                },
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
        sys.stderr.write(f"PRE_PR_TRIBUNAL:{code[:64]}\n")
        return 1
    except Exception:
        sys.stderr.write("PRE_PR_TRIBUNAL:VERDICT_INVALID\n")
        return 1
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
