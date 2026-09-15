"""Fixed source/tool profiles; no inherited user configuration or credentials."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from . import evidence
from .evidence_process import run_owned
from .git_state import _command_output
from .model import SchemaError

TOOL_PATH = '/usr/bin:/bin'
MAX_TREE_FILES = 50000
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
PYTHON_INLINE_FLAGS = frozenset({'-c'})
NODE_INLINE_FLAGS = frozenset({'-e', '--eval', '-p', '--print'})
# Only flags that cannot themselves load code from outside the snapshot may
# surround inline program text; node honours -r and --experimental-loader both
# before and after the inline flag.
NODE_PRE_INLINE_FLAGS = frozenset({'--input-type=module', '--input-type=commonjs'})


def _local_script_chain(command, directory, tracked, root):
    """Bounded declared local scope, not a proof of arbitrary program purity."""
    if not isinstance(command, str) or len(command.encode('utf-8')) > 4096:
        return False
    chains = command.split('&&')
    if not 1 <= len(chains) <= 16:
        return False
    for chain in chains:
        if re.fullmatch(r'[A-Za-z0-9_./=*,:+ \t-]+', chain) is None:
            return False
        words = chain.split()
        if not words:
            return False
        if words[0] in {'vitest', 'tsc', 'eslint', 'prettier'}:
            continue
        if words[0] == 'node' and len(words) >= 2:
            script = Path(words[1])
            if script.is_absolute() or '..' in script.parts or script.suffix not in {'.js', '.cjs', '.mjs'}:
                return False
            relative = Path(directory) / script
            if relative.as_posix() in tracked and _regular_in_tree(root, relative):
                continue
        if words[0:2] == ['chmod', '+x'] and len(words) > 2:
            if all(not Path(path).is_absolute() and '..' not in Path(path).parts and not path.startswith('-') for path in words[2:]):
                continue
        return False
    return True


def _regular_in_tree(root, relative):
    """Git binds a symlink's target string, never the bytes it points at, so a
    tracked path is declared scope only when it is a real file inside the tree."""
    root = Path(root).resolve()
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file():
            return False
        return path.resolve(strict=True).is_relative_to(root)
    except OSError:
        return False


def _interpreter_freshness(words, inline_flags, preceding_flags):
    """Only inline program text is reusable, because it is carried verbatim in
    the recorded command. A file operand is never reusable: a tracked path may be
    a symlink whose target lives outside the snapshot, so head_sha pins the link
    and not the executed bytes. Any option after the inline text is refused too,
    since node honours -r and --experimental-loader there."""
    index = 0
    while index < len(words) and words[index] in preceding_flags:
        index += 1
    if index >= len(words) or words[index] not in inline_flags:
        return 'always-fresh'
    if any(word.startswith('-') for word in words[index + 2:]):
        return 'always-fresh'
    return 'deterministic'


@contextmanager
def sanitized_environment(profile):
    names = ('python3',) if profile == 'python-v1' else ('node', 'npm')
    selected = {}
    for name in names:
        found = shutil.which(name)
        if found is None:
            raise SchemaError('EVIDENCE_TOOL_UNAVAILABLE')
        selected[name] = Path(found).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='tribunal-env-') as directory:
        home = Path(directory)
        private_bin = home / 'bin'
        private_bin.mkdir(mode=0o700)
        for name, executable in selected.items():
            (private_bin / name).symlink_to(executable)
        for name in ('user.npmrc', 'global.npmrc'):
            (home / name).write_bytes(b'')
        env = {'PATH': str(private_bin) + ':' + TOOL_PATH, 'HOME': directory, 'LC_ALL': 'C', 'LANG': 'C',
               'TZ': 'UTC', 'TMPDIR': directory}
        if profile == 'node-lock-v1':
            env.update(NODE_ENV='test', CI='1', NO_COLOR='1', npm_config_ignore_scripts='true',
                npm_config_userconfig=str(home / 'user.npmrc'),
                npm_config_globalconfig=str(home / 'global.npmrc'),
                npm_config_cache=str(home / 'npm-cache'), npm_config_update_notifier='false',
                npm_config_fund='false')
        yield env


def _hash_file(path, maximum=MAX_FILE_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise SchemaError('EVIDENCE_ENVIRONMENT_UNSUPPORTED')
        digest = hashlib.sha256()
        count = 0
        while chunk := os.read(fd, 65536):
            count += len(chunk)
            if count > maximum:
                raise SchemaError('EVIDENCE_ENVIRONMENT_TOO_LARGE')
            digest.update(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise SchemaError('EVIDENCE_ENVIRONMENT_CHANGED')
        if path.stat().st_ino != after.st_ino:
            raise SchemaError('EVIDENCE_ENVIRONMENT_CHANGED')
        return digest.hexdigest(), count
    finally:
        os.close(fd)


@evidence.bounded_errors
def installed_tree(root):
    """Hash all installed files and internal links. No cache exclusions in v1."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise SchemaError('EVIDENCE_DEPENDENCIES_MISSING')
    root = root.resolve(strict=True)
    records, count, size = [], 0, 0
    def unreadable(_error):
        raise SchemaError('EVIDENCE_DEPENDENCIES_UNREADABLE') from None

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            count += 1
            if count > MAX_TREE_FILES:
                raise SchemaError('EVIDENCE_ENVIRONMENT_TOO_LARGE')
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                if Path(target).is_absolute() or not path.resolve(strict=True).is_relative_to(root):
                    raise SchemaError('EVIDENCE_DEPENDENCIES_UNSAFE')
                records.append([relative, 'link', target])
            elif stat.S_ISDIR(metadata.st_mode):
                records.append([relative, 'dir'])
            elif stat.S_ISREG(metadata.st_mode):
                digest, length = _hash_file(path)
                size += length
                if size > MAX_TREE_BYTES:
                    raise SchemaError('EVIDENCE_ENVIRONMENT_TOO_LARGE')
                records.append([relative, 'file', metadata.st_mode & 0o777, digest])
            else:
                raise SchemaError('EVIDENCE_DEPENDENCIES_UNSAFE')
    return hashlib.sha256(evidence.canonical_json(sorted(records))).hexdigest()


def _tool(name, root, env):
    found = shutil.which(name, path=env['PATH'])
    if found is None:
        raise SchemaError('EVIDENCE_TOOL_UNAVAILABLE')
    path = Path(found).resolve(strict=True)
    digest, _ = _hash_file(path)
    argv = [str(path), '-I', '-S', '--version'] if name == 'python3' else [str(path), '--version']
    output = run_owned(argv, cwd=root, env=env, timeout_seconds=5, limit=1024)
    if output['exit_code'] != 0 or output['timed_out'] or output['overflow'] or output['cleanup_failed'] or output['intervention']:
        raise SchemaError('EVIDENCE_TOOL_UNAVAILABLE')
    version = output['stdout'].decode('ascii').strip()
    if re.fullmatch(r'(?:Python )?v?\d+\.\d+\.\d+(?:[-+.][A-Za-z0-9.]+)?', version) is None:
        raise SchemaError('EVIDENCE_TOOL_UNAVAILABLE')
    return {'name': name, 'version': version, 'executable_sha256': digest}


@evidence.bounded_errors
def measure_environment(root, profile, command_cwd, *, dependency_proof=None):
    """dependency_proof is an authenticated capture fact, never local availability.

    Omit it for strict controller verification. Detached B may pass the pinned
    environment config after authenticating its context externally.
    """
    root = Path(root)
    directory = root / command_cwd
    tracked = set(_command_output(root, ('ls-files', '-z')).decode('utf-8').split('\0')) - {''}
    if profile == 'python-v1':
        names = {'pyproject.toml', 'setup.cfg', 'setup.py', 'Pipfile', 'Pipfile.lock',
                 'poetry.lock', 'uv.lock', 'tox.ini', 'pytest.ini', '.python-version'}
        inputs = sorted(p for p in tracked if Path(p).name in names or
                        Path(p).name.startswith('requirements') and Path(p).suffix in {'.txt', '.in'})
        tools, config = ('python3',), {'python_isolated': True, 'python_no_user_site': True}
    elif profile == 'node-lock-v1':
        inputs = [(Path(command_cwd) / name).as_posix() for name in ('package.json', 'package-lock.json')]
        if not set(inputs).issubset(tracked):
            raise SchemaError('EVIDENCE_DEPENDENCIES_UNSUPPORTED')
        # Bind tracked local script bodies as declared source inputs. The full
        # committed snapshot additionally binds every other source/config file.
        inputs = sorted(set(inputs) | {p for p in tracked if Path(p).suffix in {'.js', '.mjs', '.cjs'}})
        # Project/ancestor config can override npm command behavior: unsupported.
        for parent in (directory, *directory.parents):
            if (parent / '.npmrc').exists():
                raise SchemaError('EVIDENCE_CONFIG_UNSUPPORTED')
            if parent == root:
                break
        tools = ('node', 'npm')
        config = {'node_env': 'test', 'npm_ignore_scripts': True,
                  'dependency_proof_kind': 'installed-tree-v1'}
        if dependency_proof is None:
            config['dependency_tree_sha256'] = installed_tree(directory / 'node_modules')
        else:
            if dependency_proof.get('dependency_proof_kind') != 'installed-tree-v1':
                raise SchemaError('EVIDENCE_DEPENDENCIES_UNSUPPORTED')
            config['dependency_tree_sha256'] = dependency_proof['dependency_tree_sha256']
    else:
        raise SchemaError('EVIDENCE_PROFILE_UNSUPPORTED')
    facts = []
    for name in inputs:
        path = root / name
        if not path.resolve(strict=True).is_relative_to(root.resolve()):
            raise SchemaError('EVIDENCE_ENVIRONMENT_UNSUPPORTED')
        facts.append({'path': name, 'sha256': _hash_file(path)[0]})
    with sanitized_environment(profile) as env:
        records = [_tool(name, directory, env) for name in tools]
    result = {'profile': profile, 'cwd': command_cwd, 'inputs': facts,
              'tools': records, 'config': config}
    evidence.validate_environment(result)
    return result


@evidence.bounded_errors
def validate_command(root, profile, command_cwd, argv):
    """Return fixed freshness. Python scope is isolated stdlib; Node is declared."""
    executable = Path(argv[0])
    name = executable.name
    allowed = ('python3',) if profile == 'python-v1' else ('node', 'npm')
    if name not in allowed:
        raise SchemaError('EVIDENCE_COMMAND_UNSUPPORTED')
    expected = shutil.which(name)
    if expected is None or (executable.is_absolute() and executable.resolve() != Path(expected).resolve()) or (
            not executable.is_absolute() and '/' in argv[0]):
        raise SchemaError('EVIDENCE_COMMAND_UNSUPPORTED')
    if profile == 'python-v1':
        # Require exact leading isolation flags; do not claim arbitrary pytest.
        if argv[1:3] != ['-I', '-S']:
            raise SchemaError('EVIDENCE_COMMAND_UNSUPPORTED')
        return _interpreter_freshness(argv[3:], PYTHON_INLINE_FLAGS, frozenset())
    if name == 'node':
        return _interpreter_freshness(argv[1:], NODE_INLINE_FLAGS, NODE_PRE_INLINE_FLAGS)
    words = argv[1:]
    # npm configuration flags are not caller-declared environment facts. Only
    # the small advisory presentation flags below may precede the '--' boundary.
    npm_words = words[:words.index('--')] if '--' in words else words
    for word in npm_words:
        if word.startswith('-') and word not in {'--json', '--omit=dev', '--omit=optional', '--production'} and re.fullmatch(r'--audit-level=(?:info|low|moderate|high|critical|none)', word) is None:
            raise SchemaError('EVIDENCE_CONFIG_UNSUPPORTED')
    if any(word in {'audit', 'install', 'i', 'ci', 'update', 'up', 'outdated', 'view', 'info', 'search', 'exec', 'x'} for word in words):
        return 'always-fresh'
    # Script contents can invoke live services or aliases; only literal local
    # validation tools are supported, with shell metacharacters rejected.
    if words and words[0] in {'run', 'run-script', 'test', 't', 'tst'}:
        script = words[1] if words[0] in {'run', 'run-script'} and len(words) > 1 else 'test'
        package_path = Path(root) / command_cwd / 'package.json'
        _hash_file(package_path, 1024 * 1024)
        with package_path.open('rb') as source:
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise SchemaError('EVIDENCE_ENVIRONMENT_TOO_LARGE')
        package = json.loads(raw)
        command = package.get('scripts', {}).get(script, '')
        tracked = set(_command_output(Path(root), ('ls-files', '-z')).decode('utf-8').split('\0'))
        if _local_script_chain(command, command_cwd, tracked, root):
            return 'deterministic'
    return 'always-fresh'
