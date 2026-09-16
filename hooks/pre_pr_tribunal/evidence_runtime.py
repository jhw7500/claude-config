"""Capture explicit caller commands, freeze receipts, and verify pinned facts."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import evidence, evidence_store, model
from .evidence_environment import (installed_tree, measure_environment,
    sanitized_environment, selected_node_runtime, validate_command)
from .evidence_process import run_owned
from .git_state import DIFF_RECIPE_VERSION, _physical_root, capture_snapshot
from .model import SchemaError, TribunalError


EVIDENCE_CONTRACT_VERSION = 2
_SANDBOX_LAUNCHER_SOURCE = """import os, signal, sys
status = int(sys.argv[1])
command = sys.argv[2:]
os.set_inheritable(status, False)
read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
child = os.fork()
if child == 0:
    os.close(read_fd)
    try:
        os.execvpe(command[0], command, os.environ)
    except BaseException:
        try:
            os.write(write_fd, b'F')
        finally:
            os._exit(127)
os.close(write_fd)
failure = os.read(read_fd, 1)
os.close(read_fd)
os.write(status, b'F' if failure else b'S')
os.close(status)
_, state = os.waitpid(child, 0)
if os.WIFEXITED(state):
    raise SystemExit(os.WEXITSTATUS(state))
if os.WIFSIGNALED(state):
    signum = os.WTERMSIG(state)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
raise SystemExit(127)
"""


def contract_binding():
    return {'report_text': model.REPORT_TEXT_CONTRACT_VERSION,
            'diff_recipe': DIFF_RECIPE_VERSION, 'verdict_schema': model.VERDICT_SCHEMA_VERSION,
            'evidence': EVIDENCE_CONTRACT_VERSION}


def _fixed_command(argv, *, cwd, env=None, timeout=60):
    try:
        result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise SchemaError('EVIDENCE_SANDBOX_UNAVAILABLE') from None
    if result.returncode != 0:
        raise SchemaError('EVIDENCE_SANDBOX_UNAVAILABLE')


@contextmanager
def _sandboxed_node_command(root, command_cwd, argv, env, binding):
    """Execute a deterministic Node recipe in a clean committed clone.

    The clone excludes ignored/generated source state. Its dependency tree is a
    byte-for-byte copy of the measured tree. Bubblewrap removes network access
    and hides the controller's home and temporary directories.
    """
    resolved_root = Path(root).resolve(strict=True)
    for system_path in ('/usr', '/bin', '/lib', '/lib64'):
        if os.path.exists(system_path) and resolved_root.is_relative_to(
                Path(system_path).resolve(strict=True)):
            raise SchemaError('EVIDENCE_SANDBOX_UNAVAILABLE')
    with tempfile.TemporaryDirectory(prefix='tribunal-sandbox-') as directory:
        temporary = Path(directory)
        view = temporary / 'repo'
        git_home = temporary / 'git-home'
        git_home.mkdir(mode=0o700)
        git_env = {'PATH': '/usr/bin:/bin', 'HOME': str(git_home), 'TMPDIR': str(temporary),
                   'LC_ALL': 'C', 'LANG': 'C', 'GIT_PAGER': 'cat',
                   'GIT_OPTIONAL_LOCKS': '0', 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_ATTR_NOSYSTEM': '1'}
        _fixed_command(['/usr/bin/git', 'clone', '--no-checkout', '--no-local',
                        '--no-hardlinks', '--quiet', '--config', 'core.hooksPath=/dev/null',
                        str(root), str(view)], cwd=temporary, env=git_env)
        head = binding['snapshot']['head_sha']
        _fixed_command(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-C', str(view),
                        'checkout', '--quiet', '--detach', head], cwd=temporary, env=git_env)
        try:
            shutil.rmtree(view / '.git')
        except OSError:
            raise SchemaError('EVIDENCE_SANDBOX_UNAVAILABLE') from None
        source_modules = Path(root) / command_cwd / 'node_modules'
        target_modules = view / command_cwd / 'node_modules'
        try:
            shutil.copytree(source_modules, target_modules, symlinks=True,
                            copy_function=shutil.copy2)
        except OSError:
            raise SchemaError('EVIDENCE_SANDBOX_UNAVAILABLE') from None
        if installed_tree(target_modules) != binding['environment']['config']['dependency_tree_sha256']:
            raise SchemaError('EVIDENCE_ENVIRONMENT_CHANGED')
        try:
            selected_node, selected_npm, _digest = selected_node_runtime(env)
            node_runtime = temporary / 'node-runtime'
            runtime_bin = node_runtime / 'bin'
            runtime_npm = node_runtime / 'lib/node_modules/npm'
            runtime_bin.mkdir(parents=True)
            runtime_npm.parent.mkdir(parents=True)
            shutil.copy2(selected_node, runtime_bin / 'node')
            shutil.copytree(selected_npm, runtime_npm, symlinks=True,
                            copy_function=shutil.copy2)
            (runtime_bin / 'npm').symlink_to('../lib/node_modules/npm/bin/npm-cli.js')
            copied_env = {'PATH': str(runtime_bin) + ':/usr/bin:/bin'}
            if selected_node_runtime(copied_env)[2] != binding['environment']['config'][
                    'node_runtime_sha256']:
                raise SchemaError('EVIDENCE_ENVIRONMENT_CHANGED')
        except SchemaError:
            raise
        except OSError:
            raise SchemaError('EVIDENCE_SANDBOX_UNAVAILABLE') from None
        host_home = Path(env['HOME'])
        sandbox_env = dict(env, PATH='/opt/evidence-node/bin:/usr/bin:/bin',
            HOME='/home/evidence', TMPDIR='/tmp',
            npm_config_userconfig='/home/evidence/user.npmrc',
            npm_config_globalconfig='/home/evidence/global.npmrc',
            npm_config_cache='/home/evidence/npm-cache')
        bwrap = shutil.which('bwrap', path=env['PATH'])
        if bwrap is None:
            raise SchemaError('EVIDENCE_SANDBOX_UNAVAILABLE')
        wrapped = [str(Path(bwrap).resolve()), '--die-with-parent', '--new-session',
                   '--unshare-all', '--ro-bind', '/usr', '/usr']
        for system_path in ('/bin', '/lib', '/lib64'):
            if os.path.exists(system_path):
                wrapped.extend(('--ro-bind', system_path, system_path))
        if os.path.exists('/usr/local'):
            wrapped.extend(('--tmpfs', '/usr/local'))
        wrapped.extend(('--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                        '--dir', '/opt', '--ro-bind', str(node_runtime), '/opt/evidence-node',
                        '--dir', '/home', '--bind', str(host_home), '/home/evidence',
                        '--bind', str(view), '/workspace', '--chdir',
                        str(Path('/workspace') / command_cwd)))
        for key, value in sorted(sandbox_env.items()):
            wrapped.extend(('--setenv', key, value))
        status_read, status_write = os.pipe()
        try:
            wrapped.extend(('--', '/usr/bin/python3', '-I', '-S', '-c',
                            _SANDBOX_LAUNCHER_SOURCE, str(status_write), *argv))
            yield (wrapped, root, {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'LANG': 'C'},
                   (status_read, status_write))
        finally:
            for fd in (status_read, status_write):
                try:
                    os.close(fd)
                except OSError:
                    pass


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
    if profile not in evidence.PROFILES:
        raise SchemaError('EVIDENCE_PROFILE_UNSUPPORTED')
    freshness = validate_command(root, profile, command_cwd, argv)
    before = _binding(root, base, profile, command_cwd)
    with sanitized_environment(profile) as env:
        if profile == 'node-sandbox-v1' and freshness == 'deterministic':
            with _sandboxed_node_command(root, command_cwd, argv, env, before) as execution:
                command, execution_cwd, execution_env, control_pipe = execution
                result = run_owned(command, cwd=execution_cwd, env=execution_env,
                    timeout_seconds=timeout_seconds, limit=evidence.MAX_CAPTURE_BYTES,
                    control_pipe=control_pipe, pid_namespace_contained=True)
            if result['control'] != b'S':
                raise SchemaError('EVIDENCE_SANDBOX_UNAVAILABLE')
        else:
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
        dependency_proof=expected_environment['config'] if profile in {
            'node-lock-v1', 'node-sandbox-v1'} else None)
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
