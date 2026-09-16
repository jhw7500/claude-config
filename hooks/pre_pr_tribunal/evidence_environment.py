"""Fixed source/tool profiles; no inherited user configuration or credentials."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import tempfile

from . import evidence, model
from .evidence_process import run_owned
from .git_state import _command_output
from .model import SchemaError

TOOL_PATH = '/usr/bin:/bin'
MAX_TREE_FILES = 50000
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
PYTHON_TRACKED_READ_PROGRAM = (
    "from pathlib import Path; import sys; "
    "[Path(value).read_bytes() for value in sys.argv[1:]]"
)
NODE_PROFILES = frozenset({'node-lock-v1', 'node-sandbox-v1'})
NPM_RECIPE_SCRIPTS = frozenset({'build', 'typecheck', 'test'})
_TSC_FLAGS = frozenset({
    '--build', '-b', '--noEmit', '--strict', '--skipLibCheck',
    '--declaration', '--emitDeclarationOnly', '--sourceMap', '--declarationMap',
})
_TSC_EQUALS_FLAGS = frozenset({'--pretty', '--incremental', '--composite'})
_VITEST_FLAGS = frozenset({
    '--no-cache', '--coverage', '--passWithNoTests',
    '--reporter=dot', '--reporter=default', '--pool=forks', '--pool=threads',
})


def _tracked_regular(root, command_cwd, operand, *, suffixes=None):
    candidate = Path(operand)
    if candidate.is_absolute() or '..' in candidate.parts or not candidate.parts:
        return False
    relative = Path(command_cwd) / candidate
    if suffixes is not None and relative.suffix not in suffixes:
        return False
    tracked = set(_command_output(Path(root), ('ls-files', '-z')).decode('utf-8').split('\0'))
    if relative.as_posix() not in tracked:
        return False
    path = Path(root) / relative
    try:
        return (not path.is_symlink() and path.is_file()
                and path.resolve(strict=True).is_relative_to(Path(root).resolve()))
    except OSError:
        return False


def _local_dependency_tool(root, command_cwd, name):
    try:
        modules = (Path(root) / command_cwd / 'node_modules').resolve(strict=True)
        executable = modules / '.bin' / name
        target = executable.resolve(strict=True)
        return target.is_relative_to(modules) and target.is_file() and os.access(target, os.X_OK)
    except OSError:
        return False


def _read_package(root, command_cwd):
    path = Path(root) / command_cwd / 'package.json'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
            raise SchemaError('EVIDENCE_ENVIRONMENT_UNSUPPORTED')
        raw = bytearray()
        while chunk := os.read(fd, 65536):
            raw.extend(chunk)
            if len(raw) > 1024 * 1024:
                raise SchemaError('EVIDENCE_ENVIRONMENT_TOO_LARGE')
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                                        after.st_mtime_ns, after.st_ctime_ns):
            raise SchemaError('EVIDENCE_ENVIRONMENT_CHANGED')
    finally:
        os.close(fd)
    package = model._load_json(bytes(raw), limit=1024 * 1024,
                               too_large='EVIDENCE_ENVIRONMENT_TOO_LARGE')
    if not isinstance(package, dict):
        raise SchemaError('EVIDENCE_ENVIRONMENT_UNSUPPORTED')
    return package


def _tsc_recipe(root, command_cwd, script, *, require_local_tools):
    if re.fullmatch(r'[A-Za-z0-9_./=,+ \t-]{1,4096}', script) is None:
        return False
    try:
        words = shlex.split(script, posix=True)
    except ValueError:
        return False
    if not words or words[0] != 'tsc' or (require_local_tools and not _local_dependency_tool(
            root, command_cwd, 'tsc')):
        return False
    project = 'tsconfig.json'
    index = 1
    while index < len(words):
        word = words[index]
        if word in {'--project', '-p'}:
            if index + 1 >= len(words):
                return False
            project = words[index + 1]
            index += 2
            continue
        if word in _TSC_FLAGS or any(word == flag + '=true' or word == flag + '=false'
                                     for flag in _TSC_EQUALS_FLAGS):
            index += 1
            continue
        return False
    return _tracked_regular(root, command_cwd, project, suffixes={'.json'})


def _vitest_recipe(root, command_cwd, script, *, require_local_tools):
    if re.fullmatch(r'[A-Za-z0-9_=,+ \t-]{1,4096}', script) is None:
        return False
    try:
        words = shlex.split(script, posix=True)
    except ValueError:
        return False
    return (len(words) >= 2 and words[:2] == ['vitest', 'run']
            and all(word in _VITEST_FLAGS for word in words[2:])
            and (not require_local_tools or _local_dependency_tool(root, command_cwd, 'vitest')))


def _npm_recipe(root, command_cwd, words, *, require_local_tools):
    if words == ['test']:
        script_name = 'test'
    elif len(words) == 2 and words[0] == 'run' and words[1] in NPM_RECIPE_SCRIPTS:
        script_name = words[1]
    else:
        return False
    package = _read_package(root, command_cwd)
    scripts = package.get('scripts')
    if not isinstance(scripts, dict) or not isinstance(scripts.get(script_name), str):
        return False
    if any(name in scripts for name in ('pre' + script_name, 'post' + script_name)):
        return False
    script = scripts[script_name]
    if script_name in {'build', 'typecheck'}:
        return _tsc_recipe(root, command_cwd, script, require_local_tools=require_local_tools)
    return _vitest_recipe(root, command_cwd, script, require_local_tools=require_local_tools)


@contextmanager
def sanitized_environment(profile):
    names = ('python3',) if profile == 'python-v1' else (
        ('node', 'npm', 'bwrap') if profile == 'node-sandbox-v1' else ('node', 'npm')
    )
    selected = {}
    for name in names:
        fixed_system_tool = name == 'bwrap'
        found = shutil.which(name, path=TOOL_PATH) if fixed_system_tool else shutil.which(name)
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
        if profile in NODE_PROFILES:
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


def selected_node_runtime(env):
    """Return the measured Node executable, npm package tree and their digest."""
    node_found = shutil.which('node', path=env['PATH'])
    npm_found = shutil.which('npm', path=env['PATH'])
    if node_found is None or npm_found is None:
        raise SchemaError('EVIDENCE_TOOL_UNAVAILABLE')
    try:
        node = Path(node_found).resolve(strict=True)
        npm_cli = Path(npm_found).resolve(strict=True)
    except OSError:
        raise SchemaError('EVIDENCE_TOOL_UNAVAILABLE') from None
    npm_root = npm_cli.parent.parent
    if (npm_cli != npm_root / 'bin' / 'npm-cli.js' or not node.is_file()
            or not os.access(node, os.X_OK) or not npm_root.is_dir()):
        raise SchemaError('EVIDENCE_TOOL_UNAVAILABLE')
    identity = {
        'node_sha256': _hash_file(node)[0],
        'npm_tree_sha256': installed_tree(npm_root),
    }
    return node, npm_root, hashlib.sha256(evidence.canonical_json(identity)).hexdigest()


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
    if re.fullmatch(r'(?:Python |bubblewrap )?v?\d+\.\d+\.\d+(?:[-+.][A-Za-z0-9.]+)?', version) is None:
        raise SchemaError('EVIDENCE_TOOL_UNAVAILABLE')
    return {'name': name, 'version': version, 'executable_sha256': digest}


@evidence.bounded_errors
def measure_environment(root, profile, command_cwd, *, dependency_proof=None,
                        head_sha=None):
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
    elif profile in NODE_PROFILES:
        inputs = [(Path(command_cwd) / name).as_posix() for name in ('package.json', 'package-lock.json')]
        if not set(inputs).issubset(tracked):
            raise SchemaError('EVIDENCE_DEPENDENCIES_UNSUPPORTED')
        # Bind tracked local script bodies as declared source inputs. The full
        # committed snapshot additionally binds every other source/config file.
        inputs = sorted(set(inputs) | {
            p for p in tracked
            if (Path(p).suffix in {'.js', '.mjs', '.cjs'}
                or Path(p).name.startswith('tsconfig') and Path(p).suffix == '.json'
                or Path(p).name.startswith(('vitest.config.', 'vite.config.')))
        })
        # Project/ancestor config can override npm command behavior: unsupported.
        config_paths = []
        for parent in (directory, *directory.parents):
            config_path = parent / '.npmrc'
            config_paths.append(config_path.relative_to(root).as_posix())
            try:
                config_path.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                raise SchemaError('EVIDENCE_CONFIG_UNSUPPORTED') from None
            else:
                raise SchemaError('EVIDENCE_CONFIG_UNSUPPORTED')
            if parent == root:
                break
        if head_sha is None:
            head_sha = _command_output(
                root, ('rev-parse', '--verify', 'HEAD^{commit}')
            ).decode('ascii').strip()
        if (not isinstance(head_sha, str)
                or re.fullmatch(r'[0-9a-f]{40}', head_sha) is None):
            raise SchemaError('EVIDENCE_ENVIRONMENT_UNSUPPORTED')
        pathspecs = tuple(f':(literal){path}' for path in config_paths)
        if _command_output(
                root,
                ('ls-tree', '-r', '-z', '--name-only', head_sha, '--', *pathspecs)):
            raise SchemaError('EVIDENCE_CONFIG_UNSUPPORTED')
        tools = ('node', 'npm', 'bwrap') if profile == 'node-sandbox-v1' else ('node', 'npm')
        config = {'node_env': 'test', 'npm_ignore_scripts': True,
                  'dependency_proof_kind': 'installed-tree-v1'}
        if profile == 'node-sandbox-v1':
            config['sandbox_kind'] = 'bubblewrap-clean-clone-v1'
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
        if profile == 'node-sandbox-v1':
            config['node_runtime_sha256'] = selected_node_runtime(env)[2]
    result = {'profile': profile, 'cwd': command_cwd, 'inputs': facts,
              'tools': records, 'config': config}
    evidence.validate_environment(result)
    return result


@evidence.bounded_errors
def validate_command(root, profile, command_cwd, argv, *, require_local_tools=True):
    """Return fixed freshness. Python scope is isolated stdlib; Node is declared."""
    executable = Path(argv[0])
    name = executable.name
    allowed = ('python3',) if profile == 'python-v1' else ('node', 'npm')
    if name not in allowed:
        raise SchemaError('EVIDENCE_COMMAND_UNSUPPORTED')
    expected = shutil.which(name)
    if expected is None:
        raise SchemaError('EVIDENCE_COMMAND_UNSUPPORTED')
    if profile == 'node-sandbox-v1':
        invalid_executable = executable.is_absolute() or '/' in argv[0]
    else:
        invalid_executable = (
            executable.is_absolute() and executable.resolve() != Path(expected).resolve()
            or not executable.is_absolute() and '/' in argv[0]
        )
    if invalid_executable:
        raise SchemaError('EVIDENCE_COMMAND_UNSUPPORTED')
    if profile == 'python-v1':
        # Require exact leading isolation flags; do not claim arbitrary pytest.
        if argv[1:3] != ['-I', '-S']:
            raise SchemaError('EVIDENCE_COMMAND_UNSUPPORTED')
        if (argv[3:5] == ['-c', PYTHON_TRACKED_READ_PROGRAM] and len(argv) >= 6
                and all(_tracked_regular(root, command_cwd, operand) for operand in argv[5:])):
            return 'deterministic'
        return 'always-fresh'
    if name == 'node':
        return 'always-fresh'
    words = argv[1:]
    # npm configuration flags are not caller-declared environment facts. Only
    # the small advisory presentation flags below may precede the '--' boundary.
    npm_words = words[:words.index('--')] if '--' in words else words
    for word in npm_words:
        if word.startswith('-') and word not in {'--json', '--omit=dev', '--omit=optional', '--production'} and re.fullmatch(r'--audit-level=(?:info|low|moderate|high|critical|none)', word) is None:
            raise SchemaError('EVIDENCE_CONFIG_UNSUPPORTED')
    if profile == 'node-sandbox-v1' and _npm_recipe(
            root, command_cwd, words, require_local_tools=require_local_tools):
        return 'deterministic'
    # node-lock-v1 remains capture-only. Reuse requires the clean-clone sandbox
    # contract in node-sandbox-v1.
    return 'always-fresh'
