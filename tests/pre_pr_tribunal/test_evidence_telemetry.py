import json

from pre_pr_tribunal import telemetry
from pre_pr_tribunal.model import Reviewer
from pre_pr_tribunal.verdict_store import submit_reviewer_report, finalize_round
from tests.pre_pr_tribunal.test_evidence_lifecycle import bundle, begin, reused, raw_report
from tests.pre_pr_tribunal.test_evidence_runtime import node_repo


def observed_run(repo, verdict):
    run = telemetry.create_run(repo, base_ref='master', runtime='codex', round_number=1,
        started_at='2026-09-12T00:00:00Z', started_monotonic_ns=1)
    telemetry.bind_run(repo, run_id=run.run_id, snapshot=verdict.snapshot,
                       lifecycle_id=verdict.lifecycle_id)
    return run.run_id


def test_evidence_usage_is_authenticated_and_claim_subset_is_separate(git_repo):
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    run_id = observed_run(git_repo, verdict)
    before = telemetry.summarize_run(git_repo, run_id=run_id)['evidence']
    assert before['eligible_entry_count'] == 1
    assert before['reused_entry_count'] is None
    assert before['fresh_execution_count'] is None
    execution = reused(git_repo, frozen)
    raw = json.loads(raw_report(verdict, 'B', execution))
    raw['claims'] = [{
        'id': 'B-R1-C999',
        'statement': 'The reused entry is not claimed by this report.',
        'result': 'unverified',
        'execution_ids': [],
        'reason': 'This test isolates unreferenced evidence accounting.',
    }]
    fresh = dict(execution, id='B-R1-E002'); fresh.pop('evidence_ref')
    raw['executions'].append(fresh)
    submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=json.dumps(raw).encode())
    result = telemetry.summarize_run(git_repo, run_id=run_id)['evidence']
    assert result['reused_entry_count'] == 1
    assert result['claim_reused_entry_count'] == 0
    assert result['fresh_execution_count'] == 1
    assert result['rejected_entry_count'] == 0
    assert result['capture_duration_ms'] >= 0
    assert result['verification_duration_ms'] >= 0
    assert result['measured_saved_elapsed_ms'] is None
    assert result['total_command_count'] is None


def test_claim_reuse_and_telemetry_failure_cannot_change_primary_gate(git_repo, monkeypatch):
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    run_id = observed_run(git_repo, verdict)
    execution = reused(git_repo, frozen)
    raw = json.loads(raw_report(verdict, 'B', execution))
    raw['claims'] = [{'id': 'B-R1-C001', 'statement': 'Command succeeds',
        'result': 'supported', 'execution_ids': [execution['id']], 'reason': ''}]
    submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=json.dumps(raw).encode())
    assert telemetry.summarize_run(git_repo, run_id=run_id)['evidence']['claim_reused_entry_count'] == 1
    from pre_pr_tribunal import evidence_telemetry
    monkeypatch.setattr(evidence_telemetry, '_observe', lambda *a: (_ for _ in ()).throw(OSError()))
    assert telemetry.summarize_run(git_repo, run_id=run_id)['evidence']['eligible_entry_count'] is None
    for reviewer in 'AC':
        submit_reviewer_report(git_repo, reviewer=Reviewer(reviewer), raw=raw_report(verdict, reviewer))
    assert finalize_round(git_repo).gate.status.value == 'pass'


def test_old_lifecycle_does_not_borrow_current_verdict(git_repo):
    verdict = begin(git_repo)
    run_id = observed_run(git_repo, verdict)
    path = git_repo / '.review/verdict.json'
    value = json.loads(path.read_bytes()); value['lifecycle_id'] = 'f' * 32
    path.write_text(json.dumps(value))
    result = telemetry.summarize_run(git_repo, run_id=run_id)['evidence']
    assert result['eligible_entry_count'] is None
    assert result['status'] == 'unavailable'


def test_always_fresh_entries_are_rejected_not_reused(node_repo):
    from pre_pr_tribunal import evidence_runtime as runtime
    capture = runtime.capture_evidence(node_repo, base='master', profile='node-lock-v1',
        command_cwd='.', argv=['npm', 'run', 'missing-script'], timeout_seconds=5)
    frozen = runtime.freeze_evidence(node_repo, base='master', receipt_sha256s=[capture['receipt_sha256']])
    verdict = begin(node_repo, frozen['bundle_sha256'])
    run_id = observed_run(node_repo, verdict)
    raw = json.loads(raw_report(verdict, 'B'))
    raw['executions'] = []
    raw['claims'] = [{
        'id': 'B-R1-C999',
        'statement': 'The always-fresh path could not be reused.',
        'result': 'unverified',
        'execution_ids': [],
        'reason': 'The evidence contract requires a fresh execution.',
    }]
    submit_reviewer_report(node_repo, reviewer=Reviewer.B, raw=json.dumps(raw).encode())
    result = telemetry.summarize_run(node_repo, run_id=run_id)['evidence']
    assert result['eligible_entry_count'] == 0
    assert result['rejected_entry_count'] == 1
    assert result['reused_entry_count'] == result['claim_reused_entry_count'] == 0
    assert result['fresh_execution_count'] == 0
