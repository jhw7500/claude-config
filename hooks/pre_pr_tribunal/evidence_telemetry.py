"""Read-only evidence observations; never an input to gate authorization."""
import time

from . import evidence_lifecycle, model
from .review_store import locked_review, repository_root
from .verdict_store import _read_verdict_locked, _read_sealed_report


def summarize(cwd, run):
    unknown = dict.fromkeys(('eligible_entry_count', 'rejected_entry_count',
        'reused_entry_count', 'claim_reused_entry_count', 'fresh_execution_count',
        'fresh_non_blocking_execution_count', 'supported_claim_count',
        'verified_claim_count', 'unverified_claim_count',
        'budget_exhausted_claim_count', 'budget_profile',
        'verified_claim_limit', 'fresh_execution_limit', 'native_tool_call_limit',
        'validation_phase', 'full_suite_required',
        'full_suite_fresh_execution_count', 'full_suite_reused_execution_count',
        'full_suite_escalation_reason',
        'capture_duration_ms', 'verification_duration_ms', 'measured_saved_elapsed_ms',
        'original_command_count', 'verifier_command_count', 'total_command_count'))
    try:
        return {**unknown, **_observe(cwd, run), 'status': 'observed'}
    except Exception:
        # Observation failures must neither grant nor remove primary authority.
        return {**unknown, 'status': 'unavailable'}


def _observe(cwd, run):
    root = repository_root(cwd)
    with locked_review(root, create=False) as directory:
        verdict = _read_verdict_locked(directory)
        fields = ('repository', 'base_ref', 'base_sha', 'head_ref', 'head_sha',
                  'merge_base_sha', 'diff_sha256')
        if (verdict.schema != model.VERDICT_SCHEMA_VERSION or run.lifecycle_id is None
            or verdict.lifecycle_id != run.lifecycle_id or verdict.round != run.round
            or run.binding.contract['report_text'] != verdict.contract.report_text
            or any(getattr(run.binding, key) != getattr(verdict.snapshot, key) for key in fields)):
            raise ValueError('unmatched lifecycle')
        result = {'eligible_entry_count': 0, 'rejected_entry_count': 0,
                  'capture_duration_ms': 0, 'verification_duration_ms': None}
        if verdict.validation is not None:
            validation = verdict.validation
            result.update(
                validation_phase=validation.phase.value,
                full_suite_required=validation.requires_full_suite,
                full_suite_fresh_execution_count=(
                    1 if validation.full_suite_receipt_sha256 is not None else 0
                ),
                full_suite_reused_execution_count=0,
                full_suite_escalation_reason=validation.escalation_reason,
            )
            if validation.full_suite_receipt_sha256 is not None:
                from .validation import verify_full_suite_receipt

                verify_full_suite_receipt(
                    root, verdict, validation.full_suite_receipt_sha256,
                    expected_success=(
                        validation.phase is not model.ValidationPhase.FINAL_VALIDATION_FAILED
                    ),
                )
        if verdict.evidence_binding is not None:
            started = time.monotonic_ns()
            _, verified = evidence_lifecycle.verify_selected_evidence(root, verdict)
            result.update(eligible_entry_count=len(verified['eligible']),
                rejected_entry_count=len(verified['rejected']),
                capture_duration_ms=sum(e['duration_ms'] for e in verified['bundle']['entries']),
                verification_duration_ms=(time.monotonic_ns() - started) / 1_000_000)
        if verdict.reviewers['B'].status == 'sealed':
            report = _read_sealed_report(directory, verdict, model.Reviewer.B, root=root)
            used = evidence_lifecycle.authenticate_report(root, verdict, report)
            claim_ids = {identifier for claim in report.claims for identifier in claim.execution_ids}
            claim_used = {execution.evidence_ref.entry_id for execution in report.executions
                if execution.id in claim_ids and execution.evidence_ref is not None}
            result.update(reused_entry_count=len(used), claim_reused_entry_count=len(claim_used),
                fresh_execution_count=sum(e.evidence_ref is None for e in report.executions))
            if verdict.contract.report_text >= model.REVIEWER_B_BUDGET_CONTRACT_VERSION:
                budget = model.reviewer_b_budget(verdict.policy.risk_floor)
                result.update(
                fresh_non_blocking_execution_count=
                    model.reviewer_b_fresh_non_blocking_execution_count(report),
                supported_claim_count=sum(c.result == 'supported' for c in report.claims),
                verified_claim_count=sum(c.result != 'unverified' for c in report.claims),
                unverified_claim_count=sum(c.result == 'unverified' for c in report.claims),
                budget_exhausted_claim_count=sum(
                    c.result == 'unverified' and c.reason.startswith('BUDGET_EXHAUSTED:')
                    for c in report.claims),
                budget_profile=budget['profile'],
                verified_claim_limit=budget['verified_claims'],
                fresh_execution_limit=budget['fresh_executions'],
                native_tool_call_limit=(
                    budget['native_tool_calls']
                    if verdict.contract.report_text
                    >= model.REVIEWER_B_NATIVE_TOOL_CALL_BUDGET_CONTRACT_VERSION
                    else None
                ))
        return result
