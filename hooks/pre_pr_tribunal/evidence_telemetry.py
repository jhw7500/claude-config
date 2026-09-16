"""Read-only evidence observations; never an input to gate authorization."""
import time

from . import evidence_lifecycle, evidence_runtime, model
from .review_store import locked_review, repository_root
from .verdict_store import _read_verdict_locked, _read_sealed_report


def summarize(cwd, run):
    unknown = dict.fromkeys(('eligible_entry_count', 'rejected_entry_count',
        'reused_entry_count', 'claim_reused_entry_count', 'fresh_execution_count',
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
        if verdict.evidence_binding is not None:
            started = time.monotonic_ns()
            selection = verdict.evidence_binding
            verified = evidence_runtime.verify_evidence(root,
                bundle_sha256=selection.bundle_sha256,
                expected_binding=selection.to_json()['expected_binding'])
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
        return result
