"""Round-pinned evidence selection, isolated B projection and dependency checks.

Immutable evidence reads deliberately do not acquire the primary verdict lock.
The model stores canonical binding bytes; all runtime proof lives here.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from . import evidence, evidence_store, evidence_runtime as runtime, git_state, model
from .evidence_environment import validate_command
from .review_store import read_named_file


def parse_selection(value):
    if value is None:
        return None
    obj = model._object(value, {'bundle_sha256', 'expected_binding'}, 'EVIDENCE_BINDING_INVALID')
    digest = obj['bundle_sha256']
    if not isinstance(digest, str) or model._SHA256.fullmatch(digest) is None:
        raise model.SchemaError('EVIDENCE_BINDING_INVALID')
    evidence.validate_binding(obj['expected_binding'], obj['expected_binding'])
    return model.EvidenceBinding(digest, evidence.canonical_json(obj['expected_binding']))


def parse_fallback(value, selection):
    if value is not None and (selection is not None or not isinstance(value, str)
        or re.fullmatch(r'EVIDENCE_[A-Z_]{1,48}', value) is None):
        raise model.SchemaError('EVIDENCE_BINDING_INVALID')
    return value


def has_current_evidence_contract(verdict):
    if verdict.evidence_contract != runtime.EVIDENCE_CONTRACT_VERSION:
        return False
    if verdict.evidence_binding is None:
        return True
    expected = verdict.evidence_binding.to_json()['expected_binding']
    return expected['contract'] == runtime.contract_binding()


def select_evidence(root, snapshot, digest):
    if digest is None:
        return None, None
    try:
        bundle = evidence_store.read_bundle(root, digest)
        expected = bundle['binding']
        if (expected['snapshot'] != runtime.snapshot_binding(snapshot)
            or expected['contract'] != runtime.contract_binding()):
            raise model.SchemaError('EVIDENCE_BINDING_MISMATCH')
        runtime.verify_evidence(root, bundle_sha256=digest, expected_binding=expected)
        return parse_selection({'bundle_sha256': digest, 'expected_binding': expected}), None
    except (model.TribunalError, OSError, ValueError, TypeError):
        # Do not reflect caller-controlled invalid values or grant partial authority.
        return None, 'EVIDENCE_SELECTION_INVALID'


def _expected(verdict):
    if verdict.schema != model.VERDICT_SCHEMA_VERSION or verdict.evidence_binding is None:
        raise model.SchemaError('EVIDENCE_NOT_SELECTED')
    selected = parse_selection(verdict.evidence_binding.to_json())
    expected = selected.to_json()['expected_binding']
    if (expected['snapshot'] != runtime.snapshot_binding(verdict.snapshot)
        or expected['contract'] != runtime.contract_binding()):
        raise model.SchemaError('EVIDENCE_BINDING_MISMATCH')
    return selected, expected


def reusable_execution(root, bundle_sha256, entry):
    """Exact report fields, excluding reviewer-selected ID; never a native report.

    Command is `cd -- <repository-relative cwd> && <shlex.join(argv)>`.
    Report capture_sha256 hashes stdout+stderr, without the bundle frame.
    """
    stdout, stderr = evidence.decode_capture(evidence_store.read_capture(root, entry['capture_sha256']))
    evidence.validate_entry_capture(entry, evidence.encode_capture(stdout, stderr))
    return {'command': evidence.render_report_command(entry['argv'], entry['cwd']),
        'exit_code': entry['exit_code'], 'stdout_excerpt': entry['stdout_excerpt'],
        'stderr_excerpt': entry['stderr_excerpt'], 'truncated': entry['truncated'],
        'capture_sha256': hashlib.sha256(stdout + stderr).hexdigest(),
        'evidence_ref': {'bundle_sha256': bundle_sha256, 'entry_id': entry['id']}}


def authenticate_report(root, verdict, report):
    reused = [item for item in report.executions if item.evidence_ref is not None]
    if not reused:
        return frozenset()
    if report.reviewer is not model.Reviewer.B:
        raise model.SchemaError('EVIDENCE_REVIEWER_INVALID')
    selected, expected = _expected(verdict)
    verified = runtime.verify_evidence(root, bundle_sha256=selected.bundle_sha256, expected_binding=expected)
    entries = {entry['id']: entry for entry in verified['eligible']}
    used = set()
    for execution in reused:
        ref = execution.evidence_ref
        if ref.bundle_sha256 != selected.bundle_sha256 or ref.entry_id not in entries:
            raise model.SchemaError('EVIDENCE_REFERENCE_INVALID')
        actual = execution.to_json()
        actual.pop('id')
        if actual != reusable_execution(root, selected.bundle_sha256, entries[ref.entry_id]):
            raise model.SchemaError('EVIDENCE_EXECUTION_MISMATCH')
        used.add(ref.entry_id)
    return frozenset(used)


def _detached_snapshot(root, expected):
    if git_state._run_git(root, 'symbolic-ref', '-q', 'HEAD').returncode != 1:
        raise model.SchemaError('EVIDENCE_DETACHED_REQUIRED')
    if git_state._worktree_is_dirty(git_state._command_output(root,
        ('status', '--porcelain=v2', '-z', '--untracked-files=all'))):
        raise model.SchemaError('EVIDENCE_SNAPSHOT_CHANGED')
    git_state._validate_base(root, expected['base']['ref'])
    head = git_state._sha(git_state._command_output(root, ('rev-parse', '--verify', 'HEAD^{commit}')))
    base = git_state._sha(git_state._command_output(root, ('rev-parse', '--verify',
        'refs/remotes/origin/' + expected['base']['ref'] + '^{commit}')))
    merge_base = git_state._sha(git_state._command_output(root, ('merge-base', base, head)))
    repository = git_state._repository_from_origin(git_state._command_output(root, ('remote', 'get-url', 'origin')))
    digest = hashlib.sha256(git_state._command_output(root,
        (*git_state.DIFF_ARGUMENTS, f'{merge_base}..{head}'))).hexdigest()
    actual = {'repository': repository, 'base': {'ref': expected['base']['ref'], 'sha': base},
        'head_sha': head, 'merge_base_sha': merge_base, 'diff_sha256': digest}
    if actual != expected:
        raise model.SchemaError('EVIDENCE_SNAPSHOT_CHANGED')


@evidence.bounded_errors
def project_evidence(cwd, *, reviewer_root):
    from .verdict_store import read_verdict, require_current_in_progress
    from .review_context import current_contract_binding
    root = git_state._physical_root(Path(cwd))
    verdict = require_current_in_progress(read_verdict(root))
    if verdict.contract != current_contract_binding() or verdict.reviewers['B'].status != 'pending':
        raise model.SchemaError('EVIDENCE_CONTEXT_INVALID')
    selected, expected = _expected(verdict)
    verified = runtime.verify_evidence(root, bundle_sha256=selected.bundle_sha256, expected_binding=expected)
    target = Path(reviewer_root).absolute()
    if target.resolve(strict=True) != target or git_state._physical_root(target) != target or target == root:
        raise model.SchemaError('EVIDENCE_PROJECTION_INVALID')
    _detached_snapshot(target, expected['snapshot'])
    git_state.check_review_ignored(target)
    # Require a fresh private namespace: no controller state or old projections.
    if os.path.lexists(target / '.review'):
        raise model.SchemaError('EVIDENCE_PROJECTION_NOT_EMPTY')
    for entry in verified['bundle']['entries']:
        raw = evidence_store.read_capture(root, entry['capture_sha256'])
        evidence_store._put(target, 'captures', '.bin', raw,
                            evidence.MAX_CAPTURE_BYTES + evidence.CAPTURE_OVERHEAD)
    raw = evidence_store._read(root, 'bundles', '.json', selected.bundle_sha256, evidence.MAX_BUNDLE_BYTES)
    evidence_store._put(target, 'bundles', '.json', raw, evidence.MAX_BUNDLE_BYTES)
    _detached_snapshot(target, expected['snapshot'])
    return {'bundle_sha256': selected.bundle_sha256, 'projected': True}


@evidence.bounded_errors
def verify_detached_evidence(cwd, *, bundle_sha256, context_path, expected_context_sha256):
    if not isinstance(expected_context_sha256, str) or model._SHA256.fullmatch(expected_context_sha256) is None:
        raise model.SchemaError('EVIDENCE_CONTEXT_INVALID')
    path = Path(context_path).absolute()
    if path.resolve(strict=True) != path:
        raise model.SchemaError('EVIDENCE_CONTEXT_INVALID')
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        raw = read_named_file(fd, path.name, maximum=model.MAX_VERDICT_BYTES,
            missing='EVIDENCE_CONTEXT_MISSING', unsafe='EVIDENCE_CONTEXT_UNSAFE', exact_mode=0o600)
    finally:
        os.close(fd)
    context = model._load_json(raw, limit=model.MAX_VERDICT_BYTES, too_large='EVIDENCE_CONTEXT_TOO_LARGE')
    if not isinstance(context, dict):
        raise model.SchemaError('EVIDENCE_CONTEXT_INVALID')
    supplied = context.pop('context_sha256', None)
    if supplied != expected_context_sha256 or hashlib.sha256(evidence.canonical_json(context)).hexdigest() != expected_context_sha256:
        raise model.SchemaError('EVIDENCE_CONTEXT_MISMATCH')
    if context.get('reviewer') != 'B' or context.get('schema') != 1 or type(context.get('round')) is not int or context['round'] not in (1, 2, 3):
        raise model.SchemaError('EVIDENCE_CONTEXT_INVALID')
    if context.get('contract') != {'report_text': model.REPORT_TEXT_CONTRACT_VERSION, 'diff_recipe': git_state.DIFF_RECIPE_VERSION}:
        raise model.SchemaError('EVIDENCE_CONTEXT_INVALID')
    selected = parse_selection(context.get('evidence'))
    if selected is None or selected.bundle_sha256 != bundle_sha256:
        raise model.SchemaError('EVIDENCE_NOT_SELECTED')
    expected = selected.to_json()['expected_binding']
    snapshot = dict(context.get('snapshot', {}))
    snapshot.pop('head_ref', None)
    if expected['snapshot'] != snapshot or expected['contract'] != runtime.contract_binding():
        raise model.SchemaError('EVIDENCE_BINDING_MISMATCH')
    root = git_state._physical_root(Path(cwd))
    _detached_snapshot(root, expected['snapshot'])
    runtime.verify_source_tool_environment(root, expected_environment=expected['environment'])
    verified = evidence_store.verify_bundle(root, bundle_sha256, expected)
    for entry in verified['bundle']['entries']:
        if entry['cwd'] != expected['environment']['cwd'] or entry['freshness'] != validate_command(
            root, expected['environment']['profile'], entry['cwd'], entry['argv'],
            require_local_tools=False):
            raise model.SchemaError('EVIDENCE_FRESHNESS_MISMATCH')
    executions = [{'entry_id': entry['id'], 'execution': reusable_execution(root, bundle_sha256, entry)}
                  for entry in verified['eligible']]
    runtime.verify_source_tool_environment(root, expected_environment=expected['environment'])
    _detached_snapshot(root, expected['snapshot'])
    return {'bundle_sha256': bundle_sha256, 'round_authority': True,
        'dependency_availability': 'not-checked', 'eligible': [entry['id'] for entry in verified['eligible']],
        'rejected': verified['rejected'], 'executions': executions}
