import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from pre_pr_tribunal import evidence_runtime as runtime
from pre_pr_tribunal.model import Reviewer, TribunalError
from pre_pr_tribunal.review_context import reviewer_context_envelope
from pre_pr_tribunal.verdict_store import (begin_round, submit_reviewer_report,
    finalize_round, read_verdict, validate_stored_reviewer_report)


def bundle(repo):
    captured = runtime.capture_evidence(repo, base='master', profile='python-v1',
        command_cwd='.', argv=['python3', '-I', '-S', '-c', "print('ok')"], timeout_seconds=3)
    return runtime.freeze_evidence(repo, base='master', receipt_sha256s=[captured['receipt_sha256']])


def begin(repo, digest=None):
    return begin_round(repo, base='master', runtime='codex', round_number=1,
                       evidence_bundle_sha256=digest)


def raw_report(verdict, reviewer, execution=None):
    return json.dumps({'schema': 1, 'reviewer': reviewer, 'round': verdict.round,
        'snapshot': {'head_sha': verdict.head_sha, 'diff_sha256': verdict.diff_sha256},
        'status': 'complete', 'findings': [], 'executions': [execution] if execution else [],
        'claims': [], 'prior_decisions': []}, indent=2).encode()


def reused(repo, frozen):
    from pre_pr_tribunal.evidence_lifecycle import reusable_execution
    entry = runtime.verify_evidence(repo, bundle_sha256=frozen['bundle_sha256'],
        expected_binding=frozen['binding'])['eligible'][0]
    return {'id': 'B-R1-E001', **reusable_execution(repo, frozen['bundle_sha256'], entry)}


def seal(repo, verdict, execution=None):
    for reviewer in 'ABC':
        raw = raw_report(verdict, reviewer, execution if reviewer == 'B' else None)
        submit_reviewer_report(repo, reviewer=Reviewer(reviewer), raw=raw)
        assert (repo / f'.review/inbox/round-1/{reviewer}.json').read_bytes() == raw


def test_complete_reuse_round_and_private_context(git_repo):
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    assert verdict.schema == 4 and verdict.contract.report_text == 3
    for reviewer in 'AC':
        assert 'evidence' not in reviewer_context_envelope(verdict, Reviewer(reviewer))
    context = reviewer_context_envelope(verdict, Reviewer.B)
    assert context['evidence']['bundle_sha256'] == frozen['bundle_sha256']
    seal(git_repo, verdict, reused(git_repo, frozen))
    assert finalize_round(git_repo).gate.status.value == 'pass'


def test_exact_report_command_boundary_captures_and_reuses_as_parser_valid_execution(git_repo):
    from pre_pr_tribunal.evidence_lifecycle import reusable_execution
    from pre_pr_tribunal.model import _parse_execution
    argv = ['python3', '-I', '-S', '-c', 'x' * 4068]
    captured = runtime.capture_evidence(git_repo, base='master', profile='python-v1',
        command_cwd='.', argv=argv, timeout_seconds=3)
    frozen = runtime.freeze_evidence(git_repo, base='master', receipt_sha256s=[captured['receipt_sha256']])
    entry = runtime.verify_evidence(git_repo, bundle_sha256=frozen['bundle_sha256'],
        expected_binding=frozen['binding'])['eligible'][0]
    execution = reusable_execution(git_repo, frozen['bundle_sha256'], entry)
    assert len(execution['command'].encode()) == 4096
    parsed = _parse_execution({'id': 'B-R1-E001', **execution}, reviewer=Reviewer.B, round_number=1)
    assert parsed.command == execution['command']


@pytest.mark.parametrize('selected', [False, True])
def test_independent_fallback_ignores_unused_invalid_bundle(git_repo, selected):
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'] if selected else 'f' * 64)
    (git_repo / f".review/evidence/bundles/{frozen['bundle_sha256']}.json").write_bytes(b'changed')
    seal(git_repo, verdict)
    assert finalize_round(git_repo).gate.status.value == 'pass'


def test_used_capture_tamper_rejects_stored_finalize_and_terminal_gate(git_repo):
    from pre_pr_tribunal.gate import evaluate_gate
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    seal(git_repo, verdict, reused(git_repo, frozen))
    final = finalize_round(git_repo)
    command = 'PATH=/usr/bin:/bin /usr/bin/gh pr create --base master'
    assert not evaluate_gate(git_repo, command).block
    capture = next((git_repo / '.review/evidence/captures').iterdir())
    capture.write_bytes(b'changed')
    with pytest.raises(TribunalError):
        validate_stored_reviewer_report(git_repo, reviewer=Reviewer.B)
    assert evaluate_gate(git_repo, command).block
    payload = final.to_json()
    payload['gate'] = {'status': 'in_progress', 'blocking_count': 0}
    (git_repo / '.review/verdict.json').write_text(json.dumps(payload))
    with pytest.raises(TribunalError):
        finalize_round(git_repo)


def test_detached_projection_requires_external_context_and_source_proof(git_repo, tmp_path):
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    context = reviewer_context_envelope(verdict, Reviewer.B)
    from pre_pr_tribunal.evidence_lifecycle import project_evidence, verify_detached_evidence
    reviewer = tmp_path / 'reviewer-b'
    subprocess.run(['git', '-C', str(git_repo), 'worktree', 'add', '--detach', str(reviewer), 'HEAD'], check=True, capture_output=True)
    project_evidence(git_repo, reviewer_root=reviewer)
    assert sorted(p.name for p in (reviewer / '.review').iterdir()) == ['evidence', 'lock']
    assert not (reviewer / '.review/evidence/receipts').exists()
    path = tmp_path / 'context.json'
    path.write_text(json.dumps(context)); path.chmod(0o600)
    result = verify_detached_evidence(reviewer, bundle_sha256=frozen['bundle_sha256'],
        context_path=path, expected_context_sha256=context['context_sha256'])
    assert result['round_authority'] is True
    expected_execution = reused(git_repo, frozen)
    expected_execution.pop('id')
    assert result['executions'] == [{'entry_id': 'E001', 'execution': expected_execution}]
    with pytest.raises(TribunalError):
        verify_detached_evidence(reviewer, bundle_sha256=frozen['bundle_sha256'], context_path=path,
            expected_context_sha256='f' * 64)
    (reviewer / 'tracked.txt').write_text('changed')
    with pytest.raises(TribunalError):
        verify_detached_evidence(reviewer, bundle_sha256=frozen['bundle_sha256'], context_path=path,
            expected_context_sha256=context['context_sha256'])


@pytest.mark.parametrize('change', ['digest', 'entry', 'command', 'capture', 'exit', 'always-fresh'])
def test_invalid_reference_never_seals(git_repo, change):
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    execution = reused(git_repo, frozen)
    if change == 'digest':
        execution['evidence_ref']['bundle_sha256'] = 'f' * 64
    elif change == 'entry':
        execution['evidence_ref']['entry_id'] = 'E064'
    elif change == 'command':
        execution['command'] = 'python3 -I -S -c pass'
    elif change == 'capture':
        execution['capture_sha256'] = 'a' * 64
    elif change == 'exit':
        execution['exit_code'] = 7
    else:
        from pre_pr_tribunal.evidence_store import read_bundle
        data = read_bundle(git_repo, frozen['bundle_sha256'])
        data['entries'][0]['freshness'] = 'always-fresh'
        (git_repo / f".review/evidence/bundles/{frozen['bundle_sha256']}.json").write_text(json.dumps(data))
    with pytest.raises(TribunalError):
        submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=raw_report(verdict, 'B', execution))
    assert read_verdict(git_repo).reviewers['B'].status == 'pending'


@pytest.mark.parametrize('reviewer', ['A', 'C'])
def test_evidence_refs_rejected_for_peers(git_repo, reviewer):
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    execution = reused(git_repo, frozen)
    execution['id'] = f'{reviewer}-R1-E001'
    with pytest.raises(TribunalError):
        submit_reviewer_report(git_repo, reviewer=Reviewer(reviewer), raw=raw_report(verdict, reviewer, execution))


def test_orphan_adoption_reauthenticates_dependency(git_repo):
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    execution = reused(git_repo, frozen)
    path = git_repo / '.review/inbox/round-1/B.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(raw_report(verdict, 'B', execution)); path.chmod(0o600)
    next((git_repo / '.review/evidence/captures').iterdir()).write_bytes(b'changed')
    with pytest.raises(TribunalError):
        submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=b'ignored')
    assert read_verdict(git_repo).reviewers['B'].status == 'pending'


def test_old_schema_three_remains_readable_but_not_pending_authority(git_repo):
    verdict = begin(git_repo)
    value = verdict.to_json()
    value['schema'] = 3
    value['contract'] = {'report_text': 2, 'diff_recipe': 1, 'verdict_schema': 3}
    value.pop('evidence_binding'); value.pop('evidence_fallback_reason')
    path = git_repo / '.review/verdict.json'
    path.write_text(json.dumps(value))
    loaded = read_verdict(git_repo)
    assert loaded.schema == 3 and loaded.lifecycle_id == verdict.lifecycle_id
    assert loaded.to_json() == value
    with pytest.raises(TribunalError):
        reviewer_context_envelope(loaded, Reviewer.B)
    with pytest.raises(TribunalError):
        submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=raw_report(verdict, 'B'))
    with pytest.raises(TribunalError):
        finalize_round(git_repo)
    value.pop('lifecycle_id')
    path.write_text(json.dumps(value))
    with pytest.raises(TribunalError):
        read_verdict(git_repo)


def test_report_two_and_controller_executions_reject_evidence_refs(git_repo):
    from pre_pr_tribunal.model import parse_reviewer_report, parse_decisions
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    execution = reused(git_repo, frozen)
    with pytest.raises(TribunalError):
        parse_reviewer_report(raw_report(verdict, 'B', execution), expected_reviewer=Reviewer.B,
            expected_round=1, snapshot=verdict.snapshot, report_contract_version=2)
    execution['id'] = 'D-R1-E001'
    decision = {'id': 'D-R1-A-001', 'finding_ref': {'round': 1, 'id': 'A-R1-001', 'reviewer': 'A'},
        'disposition': 'fixed', 'rationale': 'Fixed with validation.', 'executions': [execution]}
    with pytest.raises(TribunalError, match='EXECUTION_SCHEMA_INVALID'):
        parse_decisions(json.dumps([decision]).encode(), prior_blockers=['A-R1-001'])


def test_invalid_unused_evidence_allows_recovery(git_repo):
    from pre_pr_tribunal.telemetry import resume_run
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=raw_report(verdict, 'B'))
    next((git_repo / '.review/evidence/captures').iterdir()).write_bytes(b'changed')
    resumed = resume_run(git_repo, runtime='codex', started_at='2026-09-12T00:00:00Z', started_monotonic_ns=100)
    assert resumed is not None


def test_used_evidence_recovery_rejects_changed_capture(git_repo):
    from pre_pr_tribunal.telemetry import resume_run
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=raw_report(verdict, 'B', reused(git_repo, frozen)))
    next((git_repo / '.review/evidence/captures').iterdir()).write_bytes(b'changed')
    with pytest.raises(TribunalError):
        resume_run(git_repo, runtime='codex', started_at='2026-09-12T00:00:00Z', started_monotonic_ns=100)


def test_selection_contract_must_agree_with_verdict_contract(git_repo):
    frozen = bundle(git_repo)
    value = begin(git_repo, frozen['bundle_sha256']).to_json()
    value['evidence_binding']['expected_binding']['contract']['report_text'] = 2
    (git_repo / '.review/verdict.json').write_text(json.dumps(value))
    with pytest.raises(TribunalError):
        read_verdict(git_repo)


def test_one_entry_supports_multiple_claims_and_counts_once(git_repo):
    from pre_pr_tribunal.evidence_lifecycle import authenticate_report
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    value = json.loads(raw_report(verdict, 'B', reused(git_repo, frozen)))
    value['claims'] = [{'id': f'B-R1-C00{i}', 'statement': 'Validation completed.',
        'result': 'supported', 'execution_ids': ['B-R1-E001'], 'reason': ''} for i in (1, 2)]
    submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=json.dumps(value).encode())
    stored = read_verdict(git_repo)
    assert authenticate_report(git_repo, stored, stored.reviewers['B'].report) == {'E001'}


def test_detached_node_proof_does_not_claim_installed_dependencies(git_repo, tmp_path):
    from pre_pr_tribunal.evidence_lifecycle import project_evidence, verify_detached_evidence
    (git_repo / 'package.json').write_text('{"name":"fixture","version":"1.0.0"}')
    (git_repo / 'package-lock.json').write_text('{"lockfileVersion":3}')
    with (git_repo / '.gitignore').open('a') as file:
        file.write('node_modules/\n')
    subprocess.run(['git', '-C', str(git_repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(git_repo), 'commit', '-qm', 'node fixture'], check=True)
    modules = git_repo / 'node_modules'
    modules.mkdir(); (modules / 'dependency.js').write_text('module.exports=42')
    result = runtime.capture_evidence(git_repo, base='master', profile='node-lock-v1', command_cwd='.',
        argv=['node', '-e', "console.log(require('./node_modules/dependency.js'))"], timeout_seconds=3)
    frozen = runtime.freeze_evidence(git_repo, base='master', receipt_sha256s=[result['receipt_sha256']])
    verdict = begin(git_repo, frozen['bundle_sha256'])
    view = tmp_path / 'b'
    subprocess.run(['git', '-C', str(git_repo), 'worktree', 'add', '--detach', str(view), 'HEAD'], check=True, capture_output=True)
    project_evidence(git_repo, reviewer_root=view)
    context = reviewer_context_envelope(verdict, Reviewer.B)
    path = tmp_path / 'b.json'; path.write_text(json.dumps(context)); path.chmod(0o600)
    verified = verify_detached_evidence(view, bundle_sha256=frozen['bundle_sha256'],
        context_path=path, expected_context_sha256=context['context_sha256'])
    assert verified['eligible'] == ['E001']
    assert verified['dependency_availability'] == 'not-checked'
    assert not (view / 'node_modules').exists()
    execution = {'id': 'B-R1-E001', **verified['executions'][0]['execution']}
    (modules / 'dependency.js').write_text('changed')
    with pytest.raises(TribunalError):
        submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=raw_report(verdict, 'B', execution))


@pytest.mark.parametrize('installed', [False, True])
def test_cli_pinned_detached_projection_and_native_submit(git_repo, tmp_path, installer, home, installed):
    source = Path(__file__).resolve().parents[2]
    if installed:
        installer.apply_transaction(installer.build_plan(source, home), namespace='pre-pr-tribunal')
        cli = home / '.local/share/claude-config/pre_pr_tribunal/cli.py'
    else:
        cli = source / 'hooks/pre_pr_tribunal/cli.py'
    def run(*argv, cwd=git_repo, raw=None):
        return subprocess.run([sys.executable, str(cli), *argv], cwd=cwd, input=raw, capture_output=True)
    frozen = bundle(git_repo)
    result = run('begin', '--base', 'master', '--runtime', 'codex', '--round', '1',
        '--evidence-bundle', frozen['bundle_sha256'])
    assert result.returncode == 0, result.stderr
    verdict = read_verdict(git_repo)
    context_result = run('context', '--reviewer', 'B')
    assert context_result.returncode == 0
    context = json.loads(context_result.stdout)
    path = tmp_path / 'context.json'; path.write_bytes(context_result.stdout); path.chmod(0o600)
    target = tmp_path / 'b'
    subprocess.run(['git', '-C', str(git_repo), 'worktree', 'add', '--detach', str(target), 'HEAD'], check=True, capture_output=True)
    result = run('evidence-project', '--reviewer-root', str(target))
    assert result.returncode == 0, result.stdout
    args = ('evidence-verify', '--bundle', frozen['bundle_sha256'], '--context', str(path))
    missing = run(*args, cwd=target)
    assert missing.returncode == 1 and missing.stderr == b''
    assert json.loads(missing.stdout)['reason_code'] == 'EVIDENCE_CONTEXT_REQUIRED'
    verified = run(*args, '--context-sha', context['context_sha256'], cwd=target)
    assert verified.returncode == 0, verified.stdout
    fields = json.loads(verified.stdout)['executions'][0]['execution']
    raw = raw_report(verdict, 'B', {'id': 'B-R1-E001', **fields})
    result = run('submit-report', '--reviewer', 'B', raw=raw)
    assert result.returncode == 0, result.stderr
    assert (git_repo / '.review/inbox/round-1/B.json').read_bytes() == raw


@pytest.mark.parametrize('tamper', ['peer', 'branch', 'bundle', 'mode'])
def test_detached_verifier_rejects_wrong_authority_or_view(git_repo, tmp_path, tamper):
    from pre_pr_tribunal.evidence_lifecycle import project_evidence, verify_detached_evidence
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    target = tmp_path / 'b'
    subprocess.run(['git', '-C', str(git_repo), 'worktree', 'add', '--detach', str(target), 'HEAD'], check=True, capture_output=True)
    project_evidence(git_repo, reviewer_root=target)
    context = reviewer_context_envelope(verdict, Reviewer.A if tamper == 'peer' else Reviewer.B)
    path = tmp_path / 'context.json'; path.write_text(json.dumps(context)); path.chmod(0o600)
    if tamper == 'branch':
        subprocess.run(['git', '-C', str(target), 'checkout', '-qb', 'wrong-view'], check=True)
    if tamper == 'mode':
        path.chmod(0o644)
    with pytest.raises(TribunalError):
        verify_detached_evidence(target, bundle_sha256='f' * 64 if tamper == 'bundle' else frozen['bundle_sha256'],
            context_path=path, expected_context_sha256=context['context_sha256'])


def test_gate_does_not_mix_two_terminal_report_versions(git_repo, monkeypatch):
    from pre_pr_tribunal import gate
    frozen = bundle(git_repo)
    verdict = begin(git_repo, frozen['bundle_sha256'])
    seal(git_repo, verdict, reused(git_repo, frozen))
    final = finalize_round(git_repo)
    original = gate._current_snapshot
    def replace_report_between_checks(root, base):
        result = original(root, base)
        value = final.to_json()
        raw = raw_report(final, 'B')
        independent = json.loads(raw)
        value['reviewers']['B']['report'] = {key: independent[key] for key in
            ('status', 'findings', 'executions', 'claims', 'prior_decisions')}
        value['reviewers']['B']['receipt']['raw_sha256'] = hashlib.sha256(raw).hexdigest()
        (root / '.review/inbox/round-1/B.json').write_bytes(raw)
        (root / '.review/verdict.json').write_text(json.dumps(value))
        next((root / '.review/evidence/captures').iterdir()).write_bytes(b'changed')
        return result
    monkeypatch.setattr(gate, '_current_snapshot', replace_report_between_checks)
    assert gate.evaluate_gate(git_repo, 'PATH=/usr/bin:/bin /usr/bin/gh pr create --base master').block
