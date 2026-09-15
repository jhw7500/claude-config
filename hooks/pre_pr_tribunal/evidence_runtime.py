"""Capture explicit caller commands, freeze receipts, and verify pinned facts."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path

from . import evidence, evidence_store, model
from .evidence_environment import measure_environment, sanitized_environment, validate_command
from .evidence_process import run_owned
from .git_state import DIFF_RECIPE_VERSION, _physical_root, capture_snapshot
from .model import SchemaError, TribunalError


def contract_binding():
    return {'report_text': model.REPORT_TEXT_CONTRACT_VERSION,
            'diff_recipe': DIFF_RECIPE_VERSION, 'verdict_schema': model.VERDICT_SCHEMA_VERSION,
            'evidence': 1}


def snapshot_binding(snapshot):
    return {'repository': snapshot.repository,
            'base': {'ref': snapshot.base_ref, 'sha': snapshot.base_sha},
            'head_sha': snapshot.head_sha, 'merge_base_sha': snapshot.merge_base_sha,
            'diff_sha256': snapshot.diff_sha256}


def _binding(root, base, profile, command_cwd):
    return {'snapshot': snapshot_binding(capture_snapshot(root, base)),
            'contract': contract_binding(),
            'environment': measure_environment(root, profile, command_cwd)}


def _entry(argv, command_cwd, result, freshness):
    stdout, stderr = result['stdout'], result['stderr']
    return {'id': 'E001', 'argv': argv, 'cwd': command_cwd,
        'exit_code': result['exit_code'],
        'captured_at': datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
        'duration_ms': result['duration_ms'],
        'stdout_excerpt': evidence.capture_excerpt(stdout), 'stderr_excerpt': evidence.capture_excerpt(stderr),
        'truncated': any(len(s.decode('utf-8', 'replace').encode('utf-8')) > 8192 for s in (stdout, stderr)),
        'stdout_sha256': hashlib.sha256(stdout).hexdigest(),
        'stderr_sha256': hashlib.sha256(stderr).hexdigest(),
        'capture_sha256': evidence.capture_digest(stdout, stderr),
        'capture_bytes': len(stdout) + len(stderr), 'freshness': freshness}


@evidence.bounded_errors
def capture_evidence(cwd, *, base, profile, command_cwd, argv, timeout_seconds):
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise SchemaError('EVIDENCE_TIMEOUT_INVALID')
    root = _physical_root(Path(cwd))
    # Use the strict metadata parser before any command, including fixed probes.
    empty = {'stdout': b'', 'stderr': b'', 'exit_code': 0, 'duration_ms': 0}
    evidence.validate_entry(_entry(argv, command_cwd, empty, 'deterministic'))
    directory = root / command_cwd
    if not directory.is_dir() or directory.resolve() != directory or not directory.is_relative_to(root):
        raise SchemaError('EVIDENCE_CWD_INVALID')
    if profile not in ('python-v1', 'node-lock-v1'):
        raise SchemaError('EVIDENCE_PROFILE_UNSUPPORTED')
    freshness = validate_command(root, profile, command_cwd, argv)
    before = _binding(root, base, profile, command_cwd)
    with sanitized_environment(profile) as env:
        result = run_owned(argv, cwd=directory, env=env, timeout_seconds=timeout_seconds,
                           limit=evidence.MAX_CAPTURE_BYTES)
    payload = {key: result[key] for key in ('exit_code', 'timed_out', 'duration_ms')}
    if result['timed_out']:
        return {**payload, 'reason_code': 'EVIDENCE_TIMEOUT'}
    if result['cleanup_failed'] or result['intervention']:
        return {**payload, 'reason_code': 'EVIDENCE_PROCESS_CLEANUP_FAILED' if result['cleanup_failed'] else 'EVIDENCE_PROCESS_INTERVENTION'}
    if result['overflow']:
        return {**payload, 'reason_code': 'EVIDENCE_CAPTURE_TOO_LARGE'}
    try:
        after_snapshot = snapshot_binding(capture_snapshot(root, base))
        if after_snapshot != before['snapshot']:
            raise SchemaError('EVIDENCE_SNAPSHOT_CHANGED')
    except (TribunalError, OSError, ValueError):
        return {**payload, 'reason_code': 'EVIDENCE_SNAPSHOT_CHANGED'}
    try:
        after = measure_environment(root, profile, command_cwd)
        if after != before['environment']:
            raise SchemaError('EVIDENCE_ENVIRONMENT_CHANGED')
    except (TribunalError, OSError, ValueError, TypeError, UnicodeError):
        return {**payload, 'reason_code': 'EVIDENCE_ENVIRONMENT_CHANGED'}
    try:
        entry = _entry(argv, command_cwd, result, freshness)
        receipt = {'schema': 1, 'binding': before, 'entry': entry}
        evidence.parse_receipt(evidence.canonical_json(receipt))
        evidence_store.put_capture(root, result['stdout'], result['stderr'])
        digest = evidence_store.put_receipt(root, receipt)
    except TribunalError as error:
        return {**payload, 'reason_code': error.code if error.code.startswith('EVIDENCE_') else 'EVIDENCE_INVALID'}
    except (OSError, ValueError, TypeError, UnicodeError):
        return {**payload, 'reason_code': 'EVIDENCE_INVALID'}
    return {**payload, 'receipt_sha256': digest, 'freshness': freshness}


@evidence.bounded_errors
def freeze_evidence(cwd, *, base, receipt_sha256s):
    if not isinstance(receipt_sha256s, list) or not 1 <= len(receipt_sha256s) <= evidence.MAX_ENTRIES or len(set(receipt_sha256s)) != len(receipt_sha256s):
        raise SchemaError('EVIDENCE_RECEIPTS_INVALID')
    root = _physical_root(Path(cwd))
    receipts = [evidence_store.read_receipt(root, digest) for digest in receipt_sha256s]
    declared = receipts[0]['binding']
    environment = declared['environment']
    current = _binding(root, base, environment['profile'], environment['cwd'])
    entries = []
    for index, receipt in enumerate(receipts, 1):
        evidence.validate_binding(receipt['binding'], current)
        entry = dict(receipt['entry'], id=f'E{index:03d}')
        if entry['cwd'] != environment['cwd']:
            raise SchemaError('EVIDENCE_BINDING_MISMATCH')
        evidence.validate_entry_capture(entry, evidence_store.read_capture(root, entry['capture_sha256']))
        if entry['freshness'] != validate_command(root, environment['profile'], entry['cwd'], entry['argv']):
            raise SchemaError('EVIDENCE_FRESHNESS_MISMATCH')
        entries.append(entry)
    evidence.validate_binding(current, _binding(root, base, environment['profile'], environment['cwd']))
    bundle = {'schema': 1, 'binding': current, 'entries': entries}
    return {'bundle_sha256': evidence_store.put_bundle(root, bundle), 'binding': current,
            'entry_count': len(entries)}


@evidence.bounded_errors
def verify_source_tool_environment(cwd, *, expected_environment):
    """Verify source/tools only, preserving an externally authenticated proof.

    Does not grant round authority, authenticate context, inspect installed B
    dependencies or execute receipt argv. Caller must authenticate its binding.
    """
    evidence.validate_environment(expected_environment)
    root = _physical_root(Path(cwd))
    profile = expected_environment['profile']
    current = measure_environment(root, profile, expected_environment['cwd'],
        dependency_proof=expected_environment['config'] if profile == 'node-lock-v1' else None)
    if current != expected_environment:
        raise SchemaError('EVIDENCE_ENVIRONMENT_CHANGED')
    return {'environment': current, 'dependency_availability': 'not-checked',
            'round_authority': False}


@evidence.bounded_errors
def verify_evidence(cwd, *, bundle_sha256, expected_binding):
    evidence.validate_binding(expected_binding, expected_binding)
    root = _physical_root(Path(cwd))
    environment = expected_binding['environment']
    current = _binding(root, expected_binding['snapshot']['base']['ref'], environment['profile'], environment['cwd'])
    evidence.validate_binding(current, expected_binding)
    result = evidence_store.verify_bundle(root, bundle_sha256, current)
    for entry in result['bundle']['entries']:
        if entry['cwd'] != environment['cwd']:
            raise SchemaError('EVIDENCE_BINDING_MISMATCH')
        if entry['freshness'] != validate_command(root, environment['profile'], entry['cwd'], entry['argv']):
            raise SchemaError('EVIDENCE_FRESHNESS_MISMATCH')
    evidence.validate_binding(current, _binding(root, current['snapshot']['base']['ref'], environment['profile'], environment['cwd']))
    return result
