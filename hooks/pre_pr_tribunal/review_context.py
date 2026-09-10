"""Reviewer-specific, contract-bound context projections."""

from __future__ import annotations

import hashlib
import json

from .git_state import DIFF_RECIPE_VERSION, diff_contract
from .model import (
    MAX_COMMAND_TEXT_BYTES,
    MAX_EVIDENCE_TEXT_BYTES,
    MAX_EXECUTIONS_PER_REVIEWER,
    MAX_FINDINGS_PER_REVIEWER,
    MAX_REPORT_BYTES,
    MAX_VERDICT_BYTES,
    REPORT_TEXT_CONTRACT_VERSION,
    VERDICT_SCHEMA_VERSION,
    ContractBinding,
    Reviewer,
    SchemaError,
    Verdict,
)


def current_contract_binding() -> ContractBinding:
    return ContractBinding(
        report_text=REPORT_TEXT_CONTRACT_VERSION,
        diff_recipe=DIFF_RECIPE_VERSION,
        verdict_schema=VERDICT_SCHEMA_VERSION,
    )


def snapshot_projection(verdict: Verdict) -> dict[str, object]:
    return {
        "repository": verdict.repository,
        "base": {"ref": verdict.base_ref, "sha": verdict.base_sha},
        "head_ref": verdict.head_ref,
        "head_sha": verdict.head_sha,
        "merge_base_sha": verdict.merge_base_sha,
        "diff_sha256": verdict.diff_sha256,
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


def reviewer_context_body(verdict: Verdict, reviewer: Reviewer) -> dict[str, object]:
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
        "snapshot": snapshot_projection(verdict),
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


def context_sha256(verdict: Verdict, reviewer: Reviewer) -> str:
    body = reviewer_context_body(verdict, reviewer)
    raw = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def reviewer_context_envelope(
    verdict: Verdict, reviewer: Reviewer
) -> dict[str, object]:
    if verdict.contract != current_contract_binding():
        raise SchemaError("CONTRACT_DRIFT")
    if verdict.reviewers[reviewer.value].status != "pending":
        raise SchemaError("REVIEWER_SLOT_SEALED")
    body = reviewer_context_body(verdict, reviewer)
    return {**body, "context_sha256": context_sha256(verdict, reviewer)}
