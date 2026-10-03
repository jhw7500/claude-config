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
    REVIEWER_B_NATIVE_TOOL_CALL_BUDGET_CONTRACT_VERSION,
    VALIDATION_PHASE_CONTRACT_VERSION,
    VERDICT_SCHEMA_VERSION,
    ContractBinding,
    Reviewer,
    SchemaError,
    ValidationPhase,
    Verdict,
    reviewer_b_budget,
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
    body = {
        "schema": 1,
        "round": verdict.round,
        "reviewer": reviewer.value,
        "snapshot": snapshot_projection(verdict),
        "own_prior_findings": findings,
        "own_decisions": decisions,
        "contract": {
            "report_text": (
                verdict.contract.report_text if verdict.contract is not None
                else REPORT_TEXT_CONTRACT_VERSION
            ),
            "diff_recipe": (
                verdict.contract.diff_recipe if verdict.contract is not None
                else DIFF_RECIPE_VERSION
            ),
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
    if verdict.schema == VERDICT_SCHEMA_VERSION:
        if verdict.policy is None:
            raise SchemaError("POLICY_INVALID")
        selected = verdict.policy.reviewers[reviewer.value]
        body["review_policy"] = {
            "mode": verdict.policy.mode.value,
            "effective_intensity": verdict.policy.effective_intensity,
            "model": selected.model,
        }
        if reviewer is Reviewer.B:
            budget = reviewer_b_budget(verdict.policy.risk_floor)
            if (
                verdict.contract is not None
                and verdict.contract.report_text
                < REVIEWER_B_NATIVE_TOOL_CALL_BUDGET_CONTRACT_VERSION
            ):
                budget.pop("native_tool_calls")
            body["review_budget"] = budget
        if (
            verdict.contract is not None
            and verdict.contract.report_text >= VALIDATION_PHASE_CONTRACT_VERSION
        ):
            if verdict.validation is None:
                raise SchemaError("VALIDATION_BINDING_INVALID")
            if verdict.validation.phase is ValidationPhase.FIX_VERIFICATION:
                body["validation"] = {
                    "phase": "fix_verification",
                    "planned": ["finding_reproduction", "direct_impact_tests"],
                    "full_suite": "deferred_until_provisional_pass",
                }
            else:
                body["validation"] = {
                    "phase": "final_validation",
                    "planned": ["full_suite"],
                    "full_suite": (
                        "receipt_sealed"
                        if verdict.validation.full_suite_receipt_sha256 is not None
                        else "initial_round"
                    ),
                }
    if (reviewer is Reviewer.B and verdict.schema == VERDICT_SCHEMA_VERSION
        and (verdict.evidence_binding is not None or verdict.evidence_fallback_reason is not None)):
        body['evidence'] = verdict.evidence_binding.to_json() if verdict.evidence_binding else None
        body['evidence_fallback_reason'] = verdict.evidence_fallback_reason
    return body


def context_sha256(
    verdict: Verdict, reviewer: Reviewer, *, include_review_budget: bool = True
) -> str:
    body = reviewer_context_body(verdict, reviewer)
    if not include_review_budget:
        body.pop("review_budget", None)
    raw = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def reviewer_context_envelope(
    verdict: Verdict, reviewer: Reviewer
) -> dict[str, object]:
    from .evidence_lifecycle import has_current_evidence_contract
    if (verdict.schema != VERDICT_SCHEMA_VERSION
            or verdict.contract != current_contract_binding()
            or not has_current_evidence_contract(verdict)):
        raise SchemaError("CONTRACT_DRIFT")
    if verdict.reviewers[reviewer.value].status == "disabled":
        raise SchemaError("REVIEWER_DISABLED")
    if verdict.reviewers[reviewer.value].status != "pending":
        raise SchemaError("REVIEWER_SLOT_SEALED")
    body = reviewer_context_body(verdict, reviewer)
    return {**body, "context_sha256": context_sha256(verdict, reviewer)}
