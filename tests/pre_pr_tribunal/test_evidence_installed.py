import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]


def test_disposable_installed_cli_reuse_lifecycle_and_sealed_mutation(git_repo, home, tmp_path):
    if not shutil.which('bwrap', path='/usr/bin:/bin'):
        pytest.skip('bubblewrap unavailable')
    installed = subprocess.run([sys.executable, str(REPO / 'scripts/install-pre-pr-tribunal.py'),
        '--repo', str(REPO), '--home', str(home)], capture_output=True, text=True)
    assert installed.returncode == 0, installed.stderr
    package = home / '.local/share/claude-config/pre_pr_tribunal'
    names = {p.name for p in (REPO / 'hooks/pre_pr_tribunal').glob('*.py')}
    assert {p.name for p in package.iterdir()} == names
    for name in names:
        path = package / name
        assert not path.is_symlink() and path.stat().st_uid == os.geteuid()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_bytes() == (REPO / 'hooks/pre_pr_tribunal' / name).read_bytes()

    def cli(*argv, cwd=git_repo, raw=None, success=True):
        result = subprocess.run([sys.executable, str(package / 'cli.py'), *map(str, argv)],
            cwd=cwd, input=raw, capture_output=True)
        if success:
            assert result.returncode == 0, result.stderr + result.stdout
            return json.loads(result.stdout)
        assert result.returncode != 0
        return result

    (git_repo / 'package.json').write_text(json.dumps({'name': 'fixture', 'version': '1.0.0',
        'scripts': {'typecheck': 'tsc --noEmit --project tsconfig.json'}}))
    (git_repo / 'package-lock.json').write_text('{"lockfileVersion":3}')
    (git_repo / 'tsconfig.json').write_text('{"compilerOptions":{"noEmit":true}}')
    with (git_repo / '.gitignore').open('a') as file:
        file.write('node_modules/\n')
    subprocess.run(['git', '-C', str(git_repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(git_repo), 'commit', '-qm', 'sandbox recipe'], check=True)
    modules = git_repo / 'node_modules'; (modules / '.bin').mkdir(parents=True)
    tool = modules / 'typescript/bin/tsc'; tool.parent.mkdir(parents=True)
    tool.write_text("#!/usr/bin/env node\nconsole.log('ok')\n"); tool.chmod(0o755)
    (modules / '.bin/tsc').symlink_to('../typescript/bin/tsc')
    captured = cli('evidence-capture', '--base', 'master', '--profile', 'node-sandbox-v1',
        '--cwd', '.', '--timeout', '15', '--', 'npm', 'run', 'typecheck')
    frozen = cli('evidence-freeze', '--base', 'master', '--receipt', captured['receipt_sha256'])
    cli('begin', '--base', 'master', '--runtime', 'codex', '--round', '1',
        '--evidence-bundle', frozen['bundle_sha256'])
    context = cli('context', '--reviewer', 'B')
    assert 'evidence' not in cli('context', '--reviewer', 'A')
    assert 'evidence' not in cli('context', '--reviewer', 'C')
    view = tmp_path / 'reviewer-b'
    subprocess.run(['git', '-C', str(git_repo), 'worktree', 'add', '--detach', str(view), 'HEAD'],
        capture_output=True, check=True)
    cli('evidence-project', '--reviewer-root', view)
    context_path = tmp_path / 'context.json'
    context_path.write_text(json.dumps(context)); context_path.chmod(0o600)
    verified = cli('evidence-verify', '--bundle', frozen['bundle_sha256'],
        '--context', context_path, '--context-sha', context['context_sha256'], cwd=view)
    execution = {'id': 'B-R1-E001', **verified['executions'][0]['execution']}
    for role in 'ABC':
        raw = json.dumps({'schema': 1, 'reviewer': role, 'round': 1,
            'snapshot': {'head_sha': context['snapshot']['head_sha'],
                         'diff_sha256': context['snapshot']['diff_sha256']},
            'status': 'complete', 'findings': [], 'executions': [execution] if role == 'B' else [],
            'claims': [], 'prior_decisions': []}, indent=2).encode()
        cli('submit-report', '--reviewer', role, raw=raw)
        path = git_repo / f'.review/inbox/round-1/{role}.json'
        assert path.read_bytes() == raw
        assert path.stat().st_uid == os.geteuid() and stat.S_ISREG(path.lstat().st_mode)
        assert not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600
    cli('finalize')
    observed = cli('telemetry-summary')['evidence']
    assert observed['reused_entry_count'] == 1
    assert observed['claim_reused_entry_count'] == 0
    assert observed['fresh_execution_count'] == 0
    next((git_repo / '.review/evidence/captures').iterdir()).write_bytes(b'changed')
    cli('validate-report', '--reviewer', 'B', '--source', 'stored', success=False)
    assert cli('telemetry-summary')['evidence']['status'] == 'unavailable'
