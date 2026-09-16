import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / 'scripts/measure-pre-pr-evidence.py'


def invoke(tmp_path, *args):
    view = tmp_path / 'view'; view.mkdir(exist_ok=True)
    (view / 'mcp-server').mkdir(exist_ok=True)
    return subprocess.run([sys.executable, str(HELPER), '--runtime', str(REPO / 'hooks/pre_pr_tribunal'),
        '--view', str(view), '--artifacts', str(tmp_path / 'observations'), '--timeout', '3', *args],
        capture_output=True, text=True)


def test_canary_records_actual_command_and_exact_private_capture(tmp_path):
    result = invoke(tmp_path, '--category', 'inspection', '--', '/usr/bin/printf', 'exact\\n')
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['category'] == 'inspection'
    assert value['argv'] == ['/usr/bin/printf', 'exact\\n']
    assert value['execution']['capture_sha256'] == hashlib.sha256(b'exact\n').hexdigest()
    assert value['duration_ms'] >= 0
    files = list((tmp_path / 'observations').glob('*/*'))
    assert {p.name for p in files} == {'observation.json', 'stdout.bin', 'stderr.bin'}
    assert all(stat.S_IMODE(p.lstat().st_mode) == 0o600 and p.stat().st_uid == os.geteuid()
               and not p.is_symlink() for p in files)
    assert next(p for p in files if p.name == 'stdout.bin').read_bytes() == b'exact\n'


def test_canary_rejected_output_has_observation_without_fabricated_execution(tmp_path):
    result = invoke(tmp_path, '--category', 'inspection', '--', '/usr/bin/printf', '/home/alice/private')
    assert result.returncode != 0
    assert json.loads(result.stdout)['reason_code'] == 'CANARY_ARGUMENT_INVALID'
    # Unsafe argv rejected before execution; no copied sensitive data.
    assert '/home/alice' not in result.stdout + result.stderr


def test_canary_timeout_is_observed(tmp_path):
    result = invoke(tmp_path, '--timeout', '.02', '--category', 'inspection', '--', '/usr/bin/sleep', '1')
    assert result.returncode == 1, result.stderr
    value = json.loads(result.stdout)
    assert value['reason_code'] == 'EVIDENCE_TIMEOUT'
    assert value['execution'] is None
    assert value['exit_code'] != 0


def test_canary_fixed_false_case_keeps_nonzero_exit(tmp_path, monkeypatch):
    tools = tmp_path / 'tools'; tools.mkdir()
    npm = tools / 'npm'
    npm.write_text('#!/bin/sh\nprintf "unknown option\\n"\nexit 7\n'); npm.chmod(0o700)
    monkeypatch.setenv('PATH', str(tools) + ':' + os.environ['PATH'])
    result = invoke(tmp_path, '--case', 'false-typecheck')
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['argv'] == ['npm', 'run', 'typecheck', '--', '--not-a-real-option-issue113']
    assert value['exit_code'] == value['execution']['exit_code'] == 7
    assert value['category'] == 'original-validation'


def test_canary_rejects_artifact_symlink(tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir()
    (tmp_path / 'observations').symlink_to(outside)
    result = invoke(tmp_path, '--category', 'verification', '--', '/usr/bin/true')
    assert result.returncode != 0
    assert json.loads(result.stdout)['reason_code'] == 'CANARY_ARTIFACT_UNSAFE'
    assert not list(outside.iterdir())


def test_mcp_case_returns_parser_valid_execution(tmp_path, monkeypatch):
    from pre_pr_tribunal.model import _parse_execution, Reviewer
    tools = tmp_path / 'tools'; tools.mkdir()
    node = tools / 'node'
    node.write_text('#!/bin/sh\nprintf "smoke succeeded\\n"\n'); node.chmod(0o700)
    monkeypatch.setenv('PATH', str(tools) + ':' + os.environ['PATH'])
    result = invoke(tmp_path, '--case', 'mcp-smoke')
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    parsed = _parse_execution({'id': 'B-R1-E001', **value['execution']},
                              reviewer=Reviewer.B, round_number=1)
    assert parsed.exit_code == 0


def test_non_nfc_output_is_rejected_without_execution_or_raw_capture(tmp_path):
    result = invoke(tmp_path, '--category', 'inspection', '--', '/usr/bin/python3', '-c',
                    "print(chr(101)+chr(769))")
    assert result.returncode == 1
    value = json.loads(result.stdout)
    assert value['execution'] is None
    assert value['reason_code'] == 'TEXT_INVALID'
    assert not list((tmp_path / 'observations').glob('*/stdout.bin'))
