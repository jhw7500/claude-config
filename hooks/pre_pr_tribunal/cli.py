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
        Reviewer,
        TribunalError,
    )
    from pre_pr_tribunal.verdict_store import begin_round, finalize_round, read_verdict  # type: ignore
else:
    from .model import (
        MAX_COMMAND_TEXT_BYTES,
        MAX_EVIDENCE_TEXT_BYTES,
        MAX_EXECUTIONS_PER_REVIEWER,
        MAX_FINDINGS_PER_REVIEWER,
        MAX_REPORT_BYTES,
        MAX_VERDICT_BYTES,
        Reviewer,
        TribunalError,
    )
    from .verdict_store import begin_round, finalize_round, read_verdict


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


def _context(verdict, reviewer: Reviewer) -> dict[str, object]:
    findings = [
        dict(item)
        for summary in verdict.history
        for item in summary.blocking_findings
        if item["reviewer"] == reviewer.value
    ]
    decisions = [
        item.to_json() for item in verdict.decisions if item.reviewer is reviewer
    ]
    return {
        "schema": 1,
        "round": verdict.round,
        "reviewer": reviewer.value,
        "snapshot": _snapshot(verdict),
        "own_prior_findings": findings,
        "own_decisions": decisions,
        "limits": {
            "report_bytes": MAX_REPORT_BYTES,
            "verdict_bytes": MAX_VERDICT_BYTES,
            "evidence_text_bytes": MAX_EVIDENCE_TEXT_BYTES,
            "command_text_bytes": MAX_COMMAND_TEXT_BYTES,
            "findings": MAX_FINDINGS_PER_REVIEWER,
            "executions": MAX_EXECUTIONS_PER_REVIEWER,
        },
    }


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
