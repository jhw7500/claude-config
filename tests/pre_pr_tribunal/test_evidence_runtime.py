import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pre_pr_tribunal.evidence_store import read_receipt
from pre_pr_tribunal import evidence_runtime as runtime
from pre_pr_tribunal.model import TribunalError


def capture(repo, code=None, **kwargs):
    from pre_pr_tribunal.evidence_environment import PYTHON_TRACKED_READ_PROGRAM
    argv = (['python3', '-I', '-S', '-c', PYTHON_TRACKED_READ_PROGRAM, 'tracked.txt']
            if code is None else ['python3', '-I', '-S', '-c', code])
    return runtime.capture_evidence(repo, base='master', profile='python-v1',
        command_cwd='.', argv=argv,
        timeout_seconds=kwargs.pop('timeout_seconds', 3), **kwargs)


def test_capture_real_exit_and_literal_argv(git_repo):
    result = capture(git_repo, "import sys; print('$(touch PWNED);'); sys.exit(7)")
    assert result['exit_code'] == 7
    assert result['timed_out'] is False
    assert result['duration_ms'] > 0
    entry = read_receipt(git_repo, result['receipt_sha256'])['entry']
    assert entry['stdout_excerpt'] == '$(touch PWNED);\n'
    assert entry['exit_code'] == 7
    assert not (git_repo / 'PWNED').exists()


def test_python_profile_binds_the_selected_runtime(git_repo, tmp_path, monkeypatch):
    from pre_pr_tribunal.evidence_environment import PYTHON_TRACKED_READ_PROGRAM
    selected = tmp_path / 'python3'
    selected.write_text('#!/bin/sh\nexec /usr/bin/python3 "$@"\n')
    selected.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path))
    result = runtime.capture_evidence(git_repo, base='master', profile='python-v1',
        command_cwd='.', argv=[str(selected), '-I', '-S', '-c',
                               PYTHON_TRACKED_READ_PROGRAM, 'tracked.txt'],
        timeout_seconds=3)
    receipt = read_receipt(git_repo, result['receipt_sha256'])
    tool = next(item for item in receipt['binding']['environment']['tools']
                if item['name'] == 'python3')
    assert tool['executable_sha256'] == hashlib.sha256(selected.read_bytes()).hexdigest()


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf'), True])
def test_invalid_timeout_never_executes(git_repo, timeout):
    from pre_pr_tribunal.model import TribunalError
    with pytest.raises(TribunalError):
        capture(git_repo, "open('PWNED','w').write('x')", timeout_seconds=timeout)
    assert not (git_repo / 'PWNED').exists()


def test_timeout_reports_actual_result(git_repo):
    result = capture(git_repo, 'import time; time.sleep(60)', timeout_seconds=.1)
    assert result['timed_out'] is True
    assert result['exit_code'] < 0
    assert result['reason_code'] == 'EVIDENCE_TIMEOUT'
    assert 'receipt_sha256' not in result


def test_overflow_drains_both_streams_and_preserves_exit(git_repo):
    result = capture(git_repo, "import os; os.write(1,b'x'*1100000); os.write(2,b'y'*1100000); raise SystemExit(9)")
    assert result['exit_code'] == 9
    assert result['timed_out'] is False
    assert result['reason_code'] == 'EVIDENCE_CAPTURE_TOO_LARGE'
    assert 'receipt_sha256' not in result


def test_post_snapshot_drift_has_no_receipt(git_repo):
    result = capture(git_repo, "open('tracked.txt','w').write('changed')")
    assert result['exit_code'] == 0
    assert result['reason_code'] == 'EVIDENCE_SNAPSHOT_CHANGED'
    assert 'receipt_sha256' not in result


def test_unsafe_output_has_no_receipt(git_repo):
    result = capture(git_repo, "print('/' + 'home/example/private')")
    assert result['exit_code'] == 0
    assert 'receipt_sha256' not in result


def test_capture_freeze_verify_and_changed_source(git_repo):
    result = capture(git_repo)
    frozen = runtime.freeze_evidence(git_repo, base='master', receipt_sha256s=[result['receipt_sha256']])
    verified = runtime.verify_evidence(git_repo, bundle_sha256=frozen['bundle_sha256'], expected_binding=frozen['binding'])
    assert len(verified['eligible']) == 1
    (git_repo / 'tracked.txt').write_text('drift')
    from pre_pr_tribunal.model import TribunalError
    with pytest.raises(TribunalError):
        runtime.verify_evidence(git_repo, bundle_sha256=frozen['bundle_sha256'], expected_binding=frozen['binding'])


def test_capture_rejects_raw_argv_that_expands_past_report_command_before_execution(git_repo):
    code = "open('PWNED','w').write('x')#"
    argv = ['python3', '-I', '-S', '-c', code + 'x' * (4075 - len(code))]
    assert len(' '.join(argv).encode()) == 4092
    with pytest.raises(TribunalError):
        runtime.capture_evidence(git_repo, base='master', profile='python-v1', command_cwd='.',
            argv=argv, timeout_seconds=3)
    assert not (git_repo / 'PWNED').exists()
    assert not (git_repo / '.review/evidence').exists()


def test_capture_rejects_quote_and_nontrivial_cwd_expansion_before_artifact_publication(git_repo):
    directory = git_repo / 'nested directory'
    directory.mkdir()
    (directory / 'fixture.py').write_text('pass\n')
    subprocess.run(['git', '-C', str(git_repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(git_repo), 'commit', '-qm', 'nested fixture'], check=True)
    code = "open('PWNED','w').write('x')#'"
    argv = ['python3', '-I', '-S', '-c', code + 'x' * (4066 - len(code))]
    assert len(' '.join(argv).encode()) < 4096
    with pytest.raises(TribunalError):
        runtime.capture_evidence(git_repo, base='master', profile='python-v1', command_cwd='nested directory',
            argv=argv, timeout_seconds=3)
    assert not (directory / 'PWNED').exists()
    assert not (git_repo / '.review/evidence').exists()


def test_python_requires_isolation_before_execution(git_repo):
    from pre_pr_tribunal.model import TribunalError
    with pytest.raises(TribunalError):
        runtime.capture_evidence(git_repo, base='master', profile='python-v1', command_cwd='.',
            argv=['python3', '-c', "open('PWNED','w').write('x')"], timeout_seconds=1)
    assert not (git_repo / 'PWNED').exists()


def test_timeout_kills_tracked_child_only(git_repo, tmp_path):
    import select
    marker = tmp_path / 'child-pid'
    unrelated = subprocess.Popen(['python3', '-I', '-S', '-c', 'import time; time.sleep(60)'])
    try:
        code = ("import subprocess,time; p=subprocess.Popen(['python3','-I','-S','-c',"
                "'import time; time.sleep(60)']); "
                f"open({str(marker)!r},'w').write(str(p.pid)); time.sleep(60)")
        result = capture(git_repo, code, timeout_seconds=.4)
        assert result['timed_out']
        pid = int(marker.read_text())
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            pass
        else:
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            assert poll.poll(1000)
            os.close(fd)
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait()


@pytest.fixture
def node_repo(git_repo):
    import shutil
    if not shutil.which('npm', path='/usr/bin:/bin'):
        pytest.skip('node/npm unavailable')
    (git_repo / 'package.json').write_text(json.dumps({'name': 'fixture', 'version': '1.0.0',
        'scripts': {'audit-alias': 'npm --json audit', 'test': 'vitest run'}}))
    (git_repo / 'package-lock.json').write_text('{"name":"fixture","lockfileVersion":3}')
    with (git_repo / '.gitignore').open('a') as file:
        file.write('node_modules/\n')
    subprocess.run(['git', '-C', str(git_repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(git_repo), 'commit', '-qm', 'node fixture'], check=True)
    modules = git_repo / 'node_modules'
    modules.mkdir()
    (modules / 'dependency.js').write_text('module.exports=42;')
    return git_repo


@pytest.fixture
def sandbox_node_repo(node_repo):
    import shutil
    if not shutil.which('bwrap', path='/usr/bin:/bin'):
        pytest.skip('bubblewrap unavailable')
    package = json.loads((node_repo / 'package.json').read_text())
    package['scripts'].update({
        'build': 'tsc',
        'typecheck': 'tsc --noEmit --project tsconfig.json',
        'test': 'vitest run',
    })
    (node_repo / 'package.json').write_text(json.dumps(package))
    (node_repo / 'tsconfig.json').write_text('{"compilerOptions":{"noEmit":true}}')
    modules = node_repo / 'node_modules'
    bin_dir = modules / '.bin'
    tsc = modules / 'typescript/bin/tsc'
    vitest = modules / 'vitest/vitest.mjs'
    bin_dir.mkdir(parents=True)
    tsc.parent.mkdir(parents=True)
    vitest.parent.mkdir(parents=True)
    tsc.write_text("#!/usr/bin/env node\nconsole.log('tsc-ok')\n")
    vitest.write_text("#!/usr/bin/env node\nconsole.log('vitest-ok')\n")
    tsc.chmod(0o755); vitest.chmod(0o755)
    (bin_dir / 'tsc').symlink_to('../typescript/bin/tsc')
    (bin_dir / 'vitest').symlink_to('../vitest/vitest.mjs')
    subprocess.run(['git', '-C', str(node_repo), 'add', 'package.json', 'tsconfig.json'], check=True)
    subprocess.run(['git', '-C', str(node_repo), 'commit', '-qm', 'sandbox recipes'], check=True)
    return node_repo


@pytest.mark.parametrize(('argv', 'expected'), [
    (['npm', 'run', 'build'], 'tsc-ok\n'),
    (['npm', 'run', 'typecheck'], 'tsc-ok\n'),
    (['npm', 'test'], 'vitest-ok\n'),
])
def test_sandboxed_node_recipes_capture_and_reuse(sandbox_node_repo, argv, expected):
    from pre_pr_tribunal.evidence_environment import validate_command
    assert validate_command(sandbox_node_repo, 'node-sandbox-v1', '.', argv) == 'deterministic'
    captured = runtime.capture_evidence(sandbox_node_repo, base='master',
        profile='node-sandbox-v1', command_cwd='.', argv=argv, timeout_seconds=15)
    assert captured['exit_code'] == 0 and captured['freshness'] == 'deterministic'
    receipt = read_receipt(sandbox_node_repo, captured['receipt_sha256'])
    assert receipt['entry']['stdout_excerpt'].endswith(expected)
    assert len(receipt['binding']['environment']['config']['node_runtime_sha256']) == 64
    frozen = runtime.freeze_evidence(sandbox_node_repo, base='master',
        receipt_sha256s=[captured['receipt_sha256']])
    verified = runtime.verify_evidence(sandbox_node_repo,
        bundle_sha256=frozen['bundle_sha256'], expected_binding=frozen['binding'])
    assert [entry['id'] for entry in verified['eligible']] == ['E001']


def test_sandboxed_node_preserves_actual_recipe_failure(sandbox_node_repo):
    tool = sandbox_node_repo / 'node_modules/typescript/bin/tsc'
    tool.write_text("#!/usr/bin/env node\nconsole.error('type-error');process.exit(7)\n")
    tool.chmod(0o755)
    captured = runtime.capture_evidence(sandbox_node_repo, base='master',
        profile='node-sandbox-v1', command_cwd='.', argv=['npm', 'run', 'typecheck'],
        timeout_seconds=15)
    receipt = read_receipt(sandbox_node_repo, captured['receipt_sha256'])
    assert receipt['entry']['exit_code'] == 7
    assert receipt['entry']['stderr_excerpt'].endswith('type-error\n')
    assert receipt['entry']['freshness'] == 'deterministic'


def test_sandbox_setup_failure_never_publishes_receipt(sandbox_node_repo, monkeypatch):
    def denied(*_args, **_kwargs):
        return {'stdout': b'', 'stderr': b'bwrap setup denied', 'exit_code': 1,
                'timed_out': False, 'overflow': False, 'cleanup_failed': False,
                'intervention': False, 'duration_ms': 1, 'control': b''}

    monkeypatch.setattr(runtime, 'run_owned', denied)
    with pytest.raises(TribunalError) as error:
        runtime.capture_evidence(sandbox_node_repo, base='master',
            profile='node-sandbox-v1', command_cwd='.', argv=['npm', 'run', 'typecheck'],
            timeout_seconds=15)
    assert error.value.code == 'EVIDENCE_SANDBOX_UNAVAILABLE'
    assert not (sandbox_node_repo / '.review/evidence/receipts').exists()
    assert not (sandbox_node_repo / '.review/evidence/captures').exists()


def test_sandboxed_node_recipe_hides_host_tmp(sandbox_node_repo, tmp_path):
    outside = tmp_path / 'outside-input'
    outside.write_text('host-data')
    tool = sandbox_node_repo / 'node_modules/typescript/bin/tsc'
    tool.write_text("#!/usr/bin/env node\nconst fs=require('fs');console.log(fs.existsSync("
                    + repr(str(outside)) + ")?'visible':'absent')\n")
    tool.chmod(0o755)
    captured = runtime.capture_evidence(sandbox_node_repo, base='master',
        profile='node-sandbox-v1', command_cwd='.', argv=['npm', 'run', 'build'],
        timeout_seconds=15)
    receipt = read_receipt(sandbox_node_repo, captured['receipt_sha256'])
    assert receipt['entry']['stdout_excerpt'].endswith('absent\n'), receipt['entry']


def test_sandboxed_node_recipe_cannot_reach_host_network(sandbox_node_repo):
    import socket
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen()
    port = server.getsockname()[1]
    tool = sandbox_node_repo / 'node_modules/typescript/bin/tsc'
    tool.write_text("#!/usr/bin/env node\nconst net=require('net');"
                    f"const socket=net.connect({port},'127.0.0.1');"
                    "socket.on('connect',()=>{console.log('visible');socket.destroy()});"
                    "socket.on('error',()=>console.log('absent'));"
                    "socket.setTimeout(1000,()=>{console.log('absent');socket.destroy()})\n")
    tool.chmod(0o755)
    try:
        captured = runtime.capture_evidence(sandbox_node_repo, base='master',
            profile='node-sandbox-v1', command_cwd='.', argv=['npm', 'run', 'build'],
            timeout_seconds=15)
    finally:
        server.close()
    assert 'receipt_sha256' in captured, captured
    receipt = read_receipt(sandbox_node_repo, captured['receipt_sha256'])
    assert receipt['entry']['stdout_excerpt'].endswith('absent\n'), receipt['entry']


def test_sandbox_pid_namespace_reaps_detached_grandchild(sandbox_node_repo):
    import secrets
    import signal
    token = f'tribunal-sandbox-grandchild-{secrets.token_hex(16)}'
    tool = sandbox_node_repo / 'node_modules/typescript/bin/tsc'
    tool.write_text(
        "#!/usr/bin/env node\nconst {spawn}=require('child_process');"
        "const child=spawn(process.execPath,['-e','setTimeout(()=>{},60000)',"
        + repr(token)
        + "],{detached:true,stdio:'ignore'});child.unref();console.log('spawned')\n"
    )
    tool.chmod(0o755)
    captured = runtime.capture_evidence(sandbox_node_repo, base='master',
        profile='node-sandbox-v1', command_cwd='.', argv=['npm', 'run', 'build'],
        timeout_seconds=15)
    survivors = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if token.encode() in (entry / 'cmdline').read_bytes():
                survivors.append(int(entry.name))
        except OSError:
            continue
    try:
        assert 'receipt_sha256' in captured, captured
        assert not survivors
    finally:
        for pid in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_sandboxed_node_recipe_excludes_ignored_config_and_git_metadata(sandbox_node_repo):
    with (sandbox_node_repo / '.gitignore').open('a') as file:
        file.write('vitest.config.js\n')
    (sandbox_node_repo / 'vitest.config.js').write_text('throw new Error("host-only")\n')
    subprocess.run(['git', '-C', str(sandbox_node_repo), 'add', '.gitignore'], check=True)
    subprocess.run(['git', '-C', str(sandbox_node_repo), 'commit', '-qm', 'ignore host config'],
                   check=True)
    tool = sandbox_node_repo / 'node_modules/vitest/vitest.mjs'
    tool.write_text("#!/usr/bin/env node\nimport fs from 'fs';console.log("
                    "fs.existsSync('vitest.config.js') || fs.existsSync('.git')"
                    "?'visible':'absent')\n")
    tool.chmod(0o755)
    captured = runtime.capture_evidence(sandbox_node_repo, base='master',
        profile='node-sandbox-v1', command_cwd='.', argv=['npm', 'test'],
        timeout_seconds=15)
    receipt = read_receipt(sandbox_node_repo, captured['receipt_sha256'])
    assert receipt['entry']['exit_code'] == 0, receipt['entry']['stderr_excerpt']
    assert receipt['entry']['stdout_excerpt'].endswith('absent\n'), receipt['entry']


def test_sandbox_rejects_dangling_npmrc_that_resolves_in_workspace(
        sandbox_node_repo, tmp_path):
    config_name = f'{tmp_path.name}.npmrc'
    (sandbox_node_repo / config_name).write_text(
        'script-shell=/workspace/fake-script-shell\n'
    )
    fake_shell = sandbox_node_repo / 'fake-script-shell'
    fake_shell.write_text('#!/bin/sh\nexit 0\n')
    fake_shell.chmod(0o755)
    npmrc = sandbox_node_repo / '.npmrc'
    npmrc.symlink_to(f'/workspace/{config_name}')
    assert npmrc.is_symlink()
    assert not npmrc.exists()
    subprocess.run(
        ['git', '-C', str(sandbox_node_repo), 'add', '.npmrc', config_name,
         'fake-script-shell'],
        check=True,
    )
    subprocess.run(
        ['git', '-C', str(sandbox_node_repo), 'commit', '-qm',
         'dangling npm config'],
        check=True,
    )

    with pytest.raises(TribunalError) as error:
        runtime.capture_evidence(
            sandbox_node_repo,
            base='master',
            profile='node-sandbox-v1',
            command_cwd='.',
            argv=['npm', 'run', 'build'],
            timeout_seconds=15,
        )

    assert error.value.code == 'EVIDENCE_CONFIG_UNSUPPORTED'
    assert not (sandbox_node_repo / '.review/evidence/receipts').exists()
    assert not (sandbox_node_repo / '.review/evidence/captures').exists()


def test_sandbox_rejects_tracked_npmrc_hidden_from_worktree(sandbox_node_repo):
    (sandbox_node_repo / '.npmrc').write_text(
        'script-shell=/workspace/fake-script-shell\n'
    )
    fake_shell = sandbox_node_repo / 'fake-script-shell'
    fake_shell.write_text('#!/bin/sh\necho forged-success\n')
    fake_shell.chmod(0o755)
    subprocess.run(
        ['git', '-C', str(sandbox_node_repo), 'add', '.npmrc',
         'fake-script-shell'],
        check=True,
    )
    subprocess.run(
        ['git', '-C', str(sandbox_node_repo), 'commit', '-qm',
         'tracked hidden npm config'],
        check=True,
    )
    subprocess.run(
        ['git', '-C', str(sandbox_node_repo), 'update-index',
         '--skip-worktree', '.npmrc'],
        check=True,
    )
    (sandbox_node_repo / '.npmrc').unlink()
    assert not (sandbox_node_repo / '.npmrc').exists()
    status = subprocess.run(
        ['git', '-C', str(sandbox_node_repo), 'status', '--porcelain'],
        check=True,
        stdout=subprocess.PIPE,
    )
    assert status.stdout == b''

    with pytest.raises(TribunalError) as error:
        runtime.capture_evidence(
            sandbox_node_repo,
            base='master',
            profile='node-sandbox-v1',
            command_cwd='.',
            argv=['npm', 'run', 'build'],
            timeout_seconds=15,
        )

    assert error.value.code == 'EVIDENCE_CONFIG_UNSUPPORTED'
    assert not (sandbox_node_repo / '.review/evidence/receipts').exists()
    assert not (sandbox_node_repo / '.review/evidence/captures').exists()


def test_sandboxed_node_recipe_hides_usr_local(sandbox_node_repo):
    tool = sandbox_node_repo / 'node_modules/typescript/bin/tsc'
    tool.write_text("#!/usr/bin/env node\nconst fs=require('fs');console.log("
                    "fs.readdirSync('/usr/local').length?'visible':'empty')\n")
    tool.chmod(0o755)
    captured = runtime.capture_evidence(sandbox_node_repo, base='master',
        profile='node-sandbox-v1', command_cwd='.', argv=['npm', 'run', 'build'],
        timeout_seconds=15)
    receipt = read_receipt(sandbox_node_repo, captured['receipt_sha256'])
    assert receipt['entry']['stdout_excerpt'].endswith('empty\n'), receipt['entry']


def test_sandbox_hides_selected_host_node_path(sandbox_node_repo, monkeypatch):
    import shutil
    from pre_pr_tribunal.evidence_environment import selected_node_runtime
    source_node, source_npm, _digest = selected_node_runtime(os.environ)
    private = sandbox_node_repo.parent / 'private-node-runtime'
    private_bin = private / 'bin'
    private_npm = private / 'lib/node_modules/npm'
    private_bin.mkdir(parents=True)
    private_npm.parent.mkdir(parents=True)
    shutil.copy2(source_node, private_bin / 'node')
    shutil.copytree(source_npm, private_npm, symlinks=True, copy_function=shutil.copy2)
    (private_bin / 'npm').symlink_to('../lib/node_modules/npm/bin/npm-cli.js')
    selected = str((private_bin / 'node').resolve())
    monkeypatch.setenv('PATH', str(private_bin) + ':/usr/bin:/bin')
    tool = sandbox_node_repo / 'node_modules/typescript/bin/tsc'
    tool.write_text("#!/usr/bin/env node\nconst fs=require('fs');console.log("
                    + repr(selected) + "===process.execPath?'host':'copied',"
                    "fs.existsSync(" + repr(selected) + ")?'visible':'hidden')\n")
    tool.chmod(0o755)
    captured = runtime.capture_evidence(sandbox_node_repo, base='master',
        profile='node-sandbox-v1', command_cwd='.', argv=['npm', 'run', 'build'],
        timeout_seconds=15)
    receipt = read_receipt(sandbox_node_repo, captured['receipt_sha256'])
    assert receipt['entry']['stdout_excerpt'].endswith('copied hidden\n'), receipt['entry']


def test_selected_node_runtime_binds_the_complete_npm_tree(tmp_path):
    from pre_pr_tribunal.evidence_environment import selected_node_runtime
    runtime_root = tmp_path / 'runtime'
    runtime_bin = runtime_root / 'bin'
    npm_root = runtime_root / 'lib/node_modules/npm'
    (npm_root / 'bin').mkdir(parents=True)
    nested = npm_root / 'node_modules/dependency.js'
    nested.parent.mkdir()
    node = runtime_bin / 'node'
    runtime_bin.mkdir()
    node.write_text('#!/bin/sh\nexit 0\n')
    node.chmod(0o700)
    npm_cli = npm_root / 'bin/npm-cli.js'
    npm_cli.write_text('console.log("npm")\n')
    npm_cli.chmod(0o700)
    nested.write_text('before\n')
    (runtime_bin / 'npm').symlink_to('../lib/node_modules/npm/bin/npm-cli.js')
    env = {'PATH': str(runtime_bin)}
    before = selected_node_runtime(env)[2]
    nested.write_text('after\n')
    assert selected_node_runtime(env)[2] != before


def test_sandbox_rejects_copied_node_runtime_drift(sandbox_node_repo, monkeypatch):
    from pre_pr_tribunal.evidence_environment import selected_node_runtime
    selected_node = selected_node_runtime(os.environ)[0]
    original = runtime.shutil.copy2

    def mutate_node_copy(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(source).resolve() == selected_node and Path(destination).name == 'node':
            with Path(destination).open('ab') as file:
                file.write(b'copy-time-drift')
        return result

    monkeypatch.setattr(runtime.shutil, 'copy2', mutate_node_copy)
    with pytest.raises(TribunalError, match='^EVIDENCE_ENVIRONMENT_CHANGED$'):
        runtime.capture_evidence(sandbox_node_repo, base='master',
            profile='node-sandbox-v1', command_cwd='.', argv=['npm', 'run', 'build'],
            timeout_seconds=15)
    assert not (sandbox_node_repo / '.review/evidence/receipts').exists()


def test_sandbox_rejects_controller_root_inside_system_mount():
    with pytest.raises(TribunalError) as error:
        with runtime._sandboxed_node_command(Path('/usr'), '.', ['npm', 'test'], {}, {}):
            pass
    assert error.value.code == 'EVIDENCE_SANDBOX_UNAVAILABLE'


def test_sandbox_recipe_requires_local_tool_only_during_controller_checks(sandbox_node_repo):
    from pre_pr_tribunal.evidence_environment import validate_command
    modules = sandbox_node_repo / 'node_modules'
    moved = sandbox_node_repo / 'dependencies-away'
    modules.rename(moved)
    try:
        assert validate_command(sandbox_node_repo, 'node-sandbox-v1', '.',
            ['npm', 'run', 'typecheck']) == 'always-fresh'
        assert validate_command(sandbox_node_repo, 'node-sandbox-v1', '.',
            ['npm', 'run', 'typecheck'], require_local_tools=False) == 'deterministic'
    finally:
        moved.rename(modules)


@pytest.mark.parametrize('argv', [
    ['node', '-e', "require('child_process').spawnSync('npm',['audit'])"],
    ['npm', 'audit'],
    ['npm', 'run-script', 'build'],
    ['npm', 'test', '--', '--config', '/tmp/outside.ts'],
])
def test_sandbox_profile_rejects_non_recipe_commands(sandbox_node_repo, argv):
    from pre_pr_tribunal.evidence_environment import validate_command
    assert validate_command(sandbox_node_repo, 'node-sandbox-v1', '.', argv) == 'always-fresh'


@pytest.mark.parametrize('script', [
    'vitest',
    'vitest run run --passWithNoTests',
    'vitest run --config=/tmp/outside.ts',
    'vitest run && node -e 0',
])
def test_sandbox_profile_rejects_unsafe_test_script(sandbox_node_repo, script):
    from pre_pr_tribunal.evidence_environment import validate_command
    package = json.loads((sandbox_node_repo / 'package.json').read_text())
    package['scripts']['test'] = script
    (sandbox_node_repo / 'package.json').write_text(json.dumps(package))
    assert validate_command(sandbox_node_repo, 'node-sandbox-v1', '.',
                            ['npm', 'test']) == 'always-fresh'


@pytest.mark.parametrize('hook', ['prebuild', 'postbuild', 'pretest', 'posttest'])
def test_sandbox_profile_rejects_lifecycle_hooks(sandbox_node_repo, hook):
    from pre_pr_tribunal.evidence_environment import validate_command
    package = json.loads((sandbox_node_repo / 'package.json').read_text())
    package['scripts'][hook] = 'node -e 0'
    (sandbox_node_repo / 'package.json').write_text(json.dumps(package))
    command = ['npm', 'test'] if hook.endswith('test') else ['npm', 'run', 'build']
    assert validate_command(sandbox_node_repo, 'node-sandbox-v1', '.', command) == 'always-fresh'


def test_sandbox_profile_rejects_local_tool_escape(sandbox_node_repo):
    from pre_pr_tribunal.evidence_environment import validate_command
    executable = sandbox_node_repo / 'node_modules/.bin/tsc'
    executable.unlink()
    executable.symlink_to('/usr/bin/node')
    assert validate_command(sandbox_node_repo, 'node-sandbox-v1', '.',
                            ['npm', 'run', 'build']) == 'always-fresh'


def test_sandbox_profile_rejects_non_system_command_path(sandbox_node_repo, tmp_path,
                                                         monkeypatch):
    from pre_pr_tribunal.evidence_environment import validate_command
    from pre_pr_tribunal.model import TribunalError
    fake = tmp_path / 'npm'
    fake.write_text('#!/bin/sh\nexit 0\n')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(TribunalError):
        validate_command(sandbox_node_repo, 'node-sandbox-v1', '.',
                         [str(fake), 'run', 'build'])


def test_node_tree_and_detached_source_tool_proof(node_repo):
    from pre_pr_tribunal.evidence_runtime import capture_evidence, verify_source_tool_environment
    from pre_pr_tribunal.model import TribunalError
    import shutil
    result = capture_evidence(node_repo, base='master', profile='node-lock-v1', command_cwd='.',
        argv=['node', '-e', "console.log(require('./node_modules/dependency.js'))"], timeout_seconds=3)
    binding = read_receipt(node_repo, result['receipt_sha256'])['binding']
    shutil.rmtree(node_repo / 'node_modules')
    verified = verify_source_tool_environment(node_repo, expected_environment=binding['environment'])
    assert verified['dependency_availability'] == 'not-checked'
    assert verified['round_authority'] is False
    with pytest.raises(TribunalError):
        capture_evidence(node_repo, base='master', profile='node-lock-v1', command_cwd='.',
            argv=['node', '-e', '0'], timeout_seconds=3)


def test_node_post_dependency_drift_is_rejected(node_repo):
    from pre_pr_tribunal.evidence_runtime import capture_evidence
    result = capture_evidence(node_repo, base='master', profile='node-lock-v1', command_cwd='.',
        argv=['node', '-e', "require('fs').writeFileSync('node_modules/dependency.js','changed')"], timeout_seconds=3)
    assert result['exit_code'] == 0
    assert result['reason_code'] == 'EVIDENCE_ENVIRONMENT_CHANGED'


def test_node_tree_symlinks_and_hidden_files(node_repo):
    from pre_pr_tribunal.evidence_environment import installed_tree
    from pre_pr_tribunal.model import TribunalError
    root = node_repo / 'node_modules'
    (root / 'link').symlink_to('dependency.js')
    before = installed_tree(root)
    (root / '.hidden').write_text('mutation')
    assert installed_tree(root) != before
    (root / 'escape').symlink_to('/etc/passwd')
    with pytest.raises(TribunalError):
        installed_tree(root)


@pytest.mark.parametrize('unreadable', ['root', 'nested'])
def test_unreadable_dependency_directory_prevents_publication(node_repo, monkeypatch, unreadable):
    import errno
    from pre_pr_tribunal import evidence_environment
    from pre_pr_tribunal.model import TribunalError
    root = node_repo / 'node_modules'
    nested = root / 'nested'
    nested.mkdir()
    (nested / 'dependency.js').write_text('module.exports=1;')
    blocked = root if unreadable == 'root' else nested
    original = os.scandir

    def denied(path):
        if not isinstance(path, int) and Path(path) == blocked:
            raise PermissionError(errno.EACCES, 'sensitive traversal detail', str(path))
        return original(path)

    # Inject the OS access failure so the case also works for privileged runners.
    monkeypatch.setattr(evidence_environment.os, 'scandir', denied)
    with pytest.raises(TribunalError) as error:
        runtime.capture_evidence(node_repo, base='master', profile='node-lock-v1',
            command_cwd='.', argv=['node', '-e', "console.log('ok')"], timeout_seconds=3)
    assert error.value.code == 'EVIDENCE_DEPENDENCIES_UNREADABLE'
    assert not (node_repo / '.review/evidence/receipts').exists()
    assert not (node_repo / '.review/evidence/captures').exists()


@pytest.mark.parametrize('argv', [ ['npm','--json','audit'], ['npm','audit','--omit=dev'],
    ['npm','run','audit-alias'], ['npm','ci'], ['npm','install']])
def test_live_node_commands_never_deterministic(node_repo, argv):
    from pre_pr_tribunal.evidence_environment import validate_command
    assert validate_command(node_repo, 'node-lock-v1', '.', argv) == 'always-fresh'


def test_tracked_build_chain_is_declared_local_scope(node_repo):
    from pre_pr_tribunal.evidence_environment import validate_command, measure_environment
    (node_repo / 'scripts').mkdir()
    (node_repo / 'scripts/clean.mjs').write_text("console.log('local cleanup')")
    package = json.loads((node_repo / 'package.json').read_text())
    package['scripts']['build'] = 'node scripts/clean.mjs && tsc && chmod +x dist/cli.js'
    (node_repo / 'package.json').write_text(json.dumps(package))
    subprocess.run(['git','-C',str(node_repo),'add','.'], check=True)
    subprocess.run(['git','-C',str(node_repo),'commit','-qm','build fixture'], check=True)
    assert validate_command(node_repo, 'node-lock-v1', '.', ['npm','run','build']) == 'always-fresh'
    facts = measure_environment(node_repo, 'node-lock-v1', '.')
    assert 'scripts/clean.mjs' in {item['path'] for item in facts['inputs']}
    package['scripts']['build'] += ' | curl example.com'
    (node_repo / 'package.json').write_text(json.dumps(package))
    assert validate_command(node_repo, 'node-lock-v1', '.', ['npm','run','build']) == 'always-fresh'


@pytest.mark.parametrize('flag', ['--userconfig=/tmp/other', '--prefix=/tmp/other', '--ignore-scripts=false', '--script-shell=/bin/bash'])
def test_npm_cannot_override_fixed_safe_config(node_repo, flag):
    from pre_pr_tribunal.evidence_environment import validate_command
    from pre_pr_tribunal.model import TribunalError
    with pytest.raises(TribunalError):
        validate_command(node_repo, 'node-lock-v1', '.', ['npm', flag, 'run', 'test'])


def test_sanitized_env_does_not_inherit_secrets(git_repo, monkeypatch):
    monkeypatch.setenv('NODE_OPTIONS', '--require=/missing')
    monkeypatch.setenv('NPM_TOKEN', 'not-for-child')
    result = capture(git_repo, "import os; print(sorted(k for k in os.environ if k in ('NODE_OPTIONS','NPM_TOKEN','PYTHONPATH')))")
    assert read_receipt(git_repo, result['receipt_sha256'])['entry']['stdout_excerpt'] == '[]\n'


def test_caller_toolchain_is_measured_and_executed(node_repo, tmp_path, monkeypatch):
    import hashlib
    import shutil
    from pre_pr_tribunal.evidence_runtime import capture_evidence
    tools = tmp_path / 'selected-bin'
    tools.mkdir()
    node = tools / 'node'
    shutil.copyfile(Path(shutil.which('node')).resolve(), node)
    with node.open('ab') as file:
        file.write(b'caller-toolchain-identity')
    node.chmod(0o700)
    monkeypatch.setenv('PATH', str(tools) + ':' + os.environ['PATH'])
    monkeypatch.setenv('FORCE_COLOR', '1')
    result = capture_evidence(node_repo, base='master', profile='node-lock-v1', command_cwd='.',
        argv=['node','-e', "console.log(process.env.CI,process.env.NO_COLOR,process.env.FORCE_COLOR || 'absent')"], timeout_seconds=3)
    receipt = read_receipt(node_repo, result['receipt_sha256'])
    tool = next(tool for tool in receipt['binding']['environment']['tools'] if tool['name'] == 'node')
    assert tool['executable_sha256'] == hashlib.sha256(node.read_bytes()).hexdigest()
    assert receipt['entry']['stdout_excerpt'] == '1 1 absent\n'


def test_entry_cwd_must_match_environment(git_repo):
    from pre_pr_tribunal import evidence_runtime, evidence_store
    from pre_pr_tribunal.model import TribunalError
    result = capture(git_repo)
    receipt = read_receipt(git_repo, result['receipt_sha256'])
    receipt['entry']['cwd'] = 'different'
    digest = evidence_store.put_receipt(git_repo, receipt)
    with pytest.raises(TribunalError):
        evidence_runtime.freeze_evidence(git_repo, base='master', receipt_sha256s=[digest])


def test_post_measurement_failure_preserves_command_exit(node_repo, monkeypatch):
    from pre_pr_tribunal import evidence_runtime
    original = evidence_runtime.measure_environment
    calls = 0
    def fail_after(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('sensitive detail must not escape')
        return original(*args, **kwargs)
    monkeypatch.setattr(evidence_runtime, 'measure_environment', fail_after)
    result = evidence_runtime.capture_evidence(node_repo, base='master', profile='node-lock-v1',
        command_cwd='.', argv=['node','-e','process.exit(7)'], timeout_seconds=3)
    assert result['exit_code'] == 7
    assert result['reason_code'] == 'EVIDENCE_ENVIRONMENT_CHANGED'


def test_tracking_dead_root_does_not_scan_reused_pid(monkeypatch):
    from pre_pr_tribunal import evidence_process
    process = subprocess.Popen(['python3', '-I', '-S', '-c', 'pass'])
    fd = os.pidfd_open(process.pid)
    process.wait()
    try:
        def forbidden(*args):
            pytest.fail('must not inspect ancestry from a dead/reused root pid')
        monkeypatch.setattr(evidence_process, '_descendants', forbidden)
        evidence_process._track(process.pid, {process.pid: fd})
    finally:
        os.close(fd)


def test_live_descendant_cleanup_is_not_ordinary_completion(git_repo):
    code = ("import subprocess,time; subprocess.Popen(['python3','-I','-S','-c',"
            "'import time; time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(.2)")
    result = capture(git_repo, code)
    assert result['exit_code'] == 0
    assert result['reason_code'] == 'EVIDENCE_PROCESS_INTERVENTION'
    assert 'receipt_sha256' not in result


def test_generic_descendant_cannot_use_sandbox_settle_to_escape(tmp_path):
    from pre_pr_tribunal import evidence_process
    import time
    marker = tmp_path / 'escaped-pid'
    grandchild = 'import time;time.sleep(60)'
    child = ("import subprocess,time;time.sleep(.12);"
             f"p=subprocess.Popen(['python3','-I','-S','-c',{grandchild!r}],"
             "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True);"
             f"open({str(marker)!r},'w').write(str(p.pid));time.sleep(.01)")
    code = ("import subprocess,time;"
            f"subprocess.Popen(['python3','-I','-S','-c',{child!r}],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(.1)")
    result = evidence_process.run_owned(
        ['python3', '-I', '-S', '-c', code], cwd=tmp_path,
        env={'PATH': '/usr/bin:/bin'}, timeout_seconds=1, limit=100)
    assert result['exit_code'] == 0
    assert result['intervention']
    assert not result['cleanup_failed']
    time.sleep(.15)
    assert not marker.exists()


def test_selector_register_failure_reaps_started_child(tmp_path, monkeypatch):
    from pre_pr_tribunal import evidence_process
    children = []
    original = subprocess.Popen
    def started(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(evidence_process.subprocess, 'Popen', started)
    class BrokenSelector:
        def register(self, *args):
            raise OSError('injected selector registration failure')
        def close(self):
            pass
    monkeypatch.setattr(evidence_process.selectors, 'DefaultSelector', BrokenSelector)
    with pytest.raises(OSError):
        evidence_process.run_owned(['python3','-I','-S','-c','import time;time.sleep(60)'],
            cwd=tmp_path, env={'PATH':'/usr/bin:/bin'}, timeout_seconds=1, limit=100)
    assert len(children) == 1
    assert children[0].returncode is not None


def test_owned_process_captures_private_control_pipe(tmp_path):
    from pre_pr_tribunal import evidence_process
    read_fd, write_fd = os.pipe()
    result = evidence_process.run_owned(
        ['python3', '-I', '-S', '-c',
         'import os,sys;os.write(int(sys.argv[1]),b"S")', str(write_fd)],
        cwd=tmp_path, env={'PATH': '/usr/bin:/bin'}, timeout_seconds=1, limit=100,
        control_pipe=(read_fd, write_fd))
    assert result['control'] == b'S'


def test_pidfd_signal_failure_still_reaps_root(tmp_path, monkeypatch):
    from pre_pr_tribunal import evidence_process
    def fail(*args):
        raise OSError('injected signal failure')
    monkeypatch.setattr(evidence_process.signal, 'pidfd_send_signal', fail)
    result = evidence_process.run_owned(['python3','-I','-S','-c','import time;time.sleep(60)'],
        cwd=tmp_path, env={'PATH':'/usr/bin:/bin'}, timeout_seconds=.05, limit=100)
    assert result['timed_out']
    assert result['exit_code'] < 0
    assert result['cleanup_failed']


@pytest.mark.parametrize('installed', [False, True])
def test_cli_cycle_and_failure_json(git_repo, installer, home, installed):
    repo = Path(__file__).resolve().parents[2]
    if installed:
        installer.apply_transaction(installer.build_plan(repo, home), namespace='pre-pr-tribunal')
        cli = home / '.local/share/claude-config/pre_pr_tribunal/cli.py'
    else:
        cli = repo / 'hooks/pre_pr_tribunal/cli.py'
    def run(*argv):
        return subprocess.run([sys.executable, str(cli), *argv], cwd=git_repo, capture_output=True, text=True)
    captured = run('evidence-capture', '--base', 'master', '--profile', 'python-v1', '--cwd', '.',
        '--timeout', '3', '--', 'python3', '-I', '-S', '-c', 'raise SystemExit(7)')
    assert captured.returncode == 7
    data = json.loads(captured.stdout)
    assert data['exit_code'] == 7
    frozen = run('evidence-freeze', '--base', 'master', '--receipt', data['receipt_sha256'])
    assert frozen.returncode == 0, frozen.stderr
    bundle = json.loads(frozen.stdout)['bundle_sha256']
    verified = run('evidence-verify', '--bundle', bundle)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)['round_authority'] is False
    unsafe = run('evidence-capture', '--base', 'master', '--profile', 'python-v1', '--cwd', '.',
        '--timeout', '3', '--', 'python3', '-I', '-S', '-c', "print('/'+'home/private/file')")
    assert unsafe.returncode != 0
    assert 'receipt_sha256' not in json.loads(unsafe.stdout)
    invalid = run('evidence-capture', '--profile', 'python-v1')
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout) == {'reason_code': 'EVIDENCE_USAGE'}
    assert invalid.stderr == ''


@pytest.mark.parametrize('argv', [
    ['evidence-verify', '--bundle', '0' * 64, '--unknown'],
    ['evidence-freeze', '--base', 'master', '--receipt', '0' * 64, '--unknown'],
    ['evidence-verify'],
    ['evidence-capture', '--profile', 'python-v1'],
])
def test_evidence_cli_usage_is_json_for_top_level_and_subparser_errors(git_repo, argv):
    cli = Path(__file__).resolve().parents[2] / 'hooks/pre_pr_tribunal/cli.py'
    result = subprocess.run([sys.executable, str(cli), *argv], cwd=git_repo,
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout == '{"reason_code":"EVIDENCE_USAGE"}\n'
    assert result.stderr == ''


def _freshness(repo, profile, argv):
    from pre_pr_tribunal.evidence_environment import validate_command
    return validate_command(repo, profile, '.', argv)


def _programs(repo, suffix, outside):
    """Tracked regular file, tracked symlink pointing outside, and an untracked file."""
    (repo / ('bound' + suffix)).write_text('print(1)' if suffix == '.py' else 'console.log(1)')
    (repo / ('untracked' + suffix)).write_text('print(1)' if suffix == '.py' else 'console.log(1)')
    os.symlink(str(outside), repo / ('link' + suffix))
    subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'programs'], check=True)


@pytest.mark.parametrize('argv', [
    ['python3', '-I', '-S', '/tmp/outside-the-snapshot.py'],
    ['python3', '-I', '-S', '../outside.py'],
    ['python3', '-I', '-S', 'untracked.py'],
    ['python3', '-I', '-S', 'bound.py'],
    ['python3', '-I', '-S', 'link.py'],
    ['python3', '-I', '-S', '-m', 'pytest'],
    ['python3', '-I', '-S'],
    ['python3', '-I', '-S', '-c', "print('ok')", '-X', 'faulthandler'],
])
def test_python_program_operand_is_never_reusable(git_repo, tmp_path, argv):
    outside = tmp_path / 'outside-target.py'
    outside.write_text("print('outside')")
    _programs(git_repo, '.py', outside)
    assert _freshness(git_repo, 'python-v1', argv) == 'always-fresh'


def test_arbitrary_inline_python_program_is_always_fresh(git_repo):
    argv = ['python3', '-I', '-S', '-c', "print('ok')"]
    assert _freshness(git_repo, 'python-v1', argv) == 'always-fresh'


@pytest.mark.parametrize('argv', [
    ['node', '/tmp/outside-the-snapshot.js'],
    ['node', '../outside.js'],
    ['node', 'untracked.js'],
    ['node', 'bound.js'],
    ['node', 'link.js'],
    ['node', 'not-a-script.txt'],
    ['node', '--experimental-loader=/tmp/loader.mjs', '-e', '0'],
    ['node', '-e', '0', '-r', '/tmp/preload.cjs'],
    ['node', '-p', '1', '-r', '/tmp/preload.cjs'],
    ['node', '-e', '0', '--experimental-loader=/tmp/loader.mjs'],
    ['node'],
])
def test_node_program_operand_is_never_reusable(node_repo, tmp_path, argv):
    outside = tmp_path / 'outside-target.js'
    outside.write_text("console.log('outside')")
    _programs(node_repo, '.js', outside)
    assert _freshness(node_repo, 'node-lock-v1', argv) == 'always-fresh'


@pytest.mark.parametrize('argv', [
    ['node', '-e', "console.log('ok')"],
    ['node', '--input-type=module', '-e', "console.log('ok')"],
])
def test_inline_node_program_is_always_fresh(node_repo, argv):
    assert _freshness(node_repo, 'node-lock-v1', argv) == 'always-fresh'


def test_npm_script_body_rejects_a_tracked_symlink(node_repo, tmp_path):
    outside = tmp_path / 'runner-outside.js'
    outside.write_text("console.log('outside runner')")
    os.symlink(str(outside), node_repo / 'runner.js')
    package = json.loads((node_repo / 'package.json').read_text())
    package['scripts']['build'] = 'node runner.js'
    (node_repo / 'package.json').write_text(json.dumps(package))
    subprocess.run(['git', '-C', str(node_repo), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(node_repo), 'commit', '-qm', 'symlink runner'], check=True)
    assert _freshness(node_repo, 'node-lock-v1', ['npm', 'run', 'build']) == 'always-fresh'


def test_npm_is_never_reusable(node_repo):
    """Neither a tracked script body nor post-'--' argv can be classified here."""
    (node_repo / 'runner.mjs').write_text("console.log('local runner')")
    package = json.loads((node_repo / 'package.json').read_text())
    package['scripts']['build'] = 'node runner.mjs'
    package['scripts']['lint'] = 'vitest run --config /tmp/outside/vitest.config.ts'
    (node_repo / 'package.json').write_text(json.dumps(package))
    subprocess.run(['git', '-C', str(node_repo), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(node_repo), 'commit', '-qm', 'regular runner'], check=True)
    for argv in (['npm', 'run', 'build'], ['npm', 'run', 'lint'], ['npm', 'test'],
                 ['npm', 'test', '--', '--config', '/tmp/outside/vitest.config.ts'],
                 ['npm', 'run', 'build', '--', '--require', '/tmp/outside/pre.cjs']):
        assert _freshness(node_repo, 'node-lock-v1', argv) == 'always-fresh', argv
