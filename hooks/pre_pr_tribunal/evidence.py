"""Schema-1 validation evidence. No command execution or environment discovery."""
from __future__ import annotations

from datetime import datetime
from functools import wraps
import hashlib
import json
import math
import re
import shlex
import struct

from . import model
from .model import SchemaError

MAX_BUNDLE_BYTES = 256 * 1024
MAX_CAPTURE_BYTES = 1024 * 1024
MAX_TOTAL_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 64
CAPTURE_MAGIC = b'TRIBUNAL-EVIDENCE\x00\x01'
CAPTURE_OVERHEAD = len(CAPTURE_MAGIC) + 16
PROFILES = {'python-v1': frozenset({'python3', 'pytest'}),
            'node-lock-v1': frozenset({'node', 'npm'})}
ENTRY_KEYS = {'id', 'argv', 'cwd', 'exit_code', 'captured_at', 'duration_ms',
              'stdout_excerpt', 'stderr_excerpt', 'truncated', 'stdout_sha256',
              'stderr_sha256', 'capture_sha256', 'capture_bytes', 'freshness'}


def bounded_errors(function):
    """Keep all failures bounded and avoid reflecting untrusted values."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SchemaError as error:
            if error.code.startswith('EVIDENCE_'):
                raise
            raise SchemaError('EVIDENCE_INVALID') from None
        except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError, OSError):
            raise SchemaError('EVIDENCE_INVALID') from None
    return wrapped


def _object(value, keys):
    return model._object(value, set(keys), 'EVIDENCE_SCHEMA_INVALID')


def _integer(value, minimum, maximum):
    return model._integer(value, 'EVIDENCE_SCHEMA_INVALID', minimum=minimum, maximum=maximum)


def _digest(value, length=64):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{'+str(length)+'}', value) is None:
        raise SchemaError('EVIDENCE_DIGEST_INVALID')
    return value


def _safe_text(value, maximum=4096):
    value = model._text(value, maximum)
    _sensitive(value)
    return value


def _sensitive(value):
    if model._SECRET.search(value) or model._contains_home_path(value):
        raise SchemaError('EVIDENCE_SECRET_DETECTED')


def _relative(value, *, dot=False):
    if value == '.' and dot:
        return value
    model._path(value)
    _sensitive(value)
    if ':' in value or value.startswith('~'):
        raise SchemaError('EVIDENCE_PATH_INVALID')
    return value


@bounded_errors
def render_report_command(argv: list[str], cwd: str) -> str:
    """Render the exact reused-report command and apply its parser policy."""
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        raise SchemaError('EVIDENCE_COMMAND_INVALID')
    command = 'cd -- ' + shlex.quote(cwd) + ' && ' + shlex.join(argv)
    try:
        return model._command(command)
    except SchemaError as error:
        if error.code == 'TEXT_TOO_LARGE':
            raise SchemaError('EVIDENCE_COMMAND_INVALID') from None
        raise


@bounded_errors
def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


@bounded_errors
def validate_environment(value: dict) -> None:
    env = _object(value, {'profile', 'cwd', 'inputs', 'tools', 'config'})
    profile = env['profile']
    if not isinstance(profile, str) or profile not in PROFILES:
        raise SchemaError('EVIDENCE_PROFILE_UNSUPPORTED')
    _relative(env['cwd'], dot=True)
    inputs = model._array(env['inputs'], 1024, 'EVIDENCE_LIMIT_EXCEEDED')
    paths = set()
    for item in inputs:
        item = _object(item, {'path', 'sha256'})
        path = _relative(item['path'])
        _digest(item['sha256'])
        if path in paths:
            raise SchemaError('EVIDENCE_DUPLICATE_IDENTITY')
        paths.add(path)
    names = set()
    for tool in model._array(env['tools'], 3, 'EVIDENCE_LIMIT_EXCEEDED'):
        tool = _object(tool, {'name', 'version', 'executable_sha256'})
        name = tool['name']
        if not isinstance(name, str) or name not in PROFILES[profile] or name in names:
            raise SchemaError('EVIDENCE_TOOL_INVALID')
        names.add(name)
        version = _safe_text(tool['version'], 128)
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 .+_()\-]{0,127}', version) is None:
            raise SchemaError('EVIDENCE_TOOL_INVALID')
        _digest(tool['executable_sha256'])
    required = {'python3'} if profile == 'python-v1' else {'node', 'npm'}
    if not required.issubset(names):
        raise SchemaError('EVIDENCE_TOOL_INVALID')
    config = env['config']
    allowed = ({'python_isolated', 'python_no_user_site'} if profile == 'python-v1' else
               {'node_env', 'npm_ignore_scripts', 'dependency_tree_sha256', 'dependency_proof_kind'})
    if not isinstance(config, dict) or not set(config).issubset(allowed):
        raise SchemaError('EVIDENCE_CONFIG_INVALID')
    for key, value in config.items():
        if key in {'python_isolated', 'python_no_user_site', 'npm_ignore_scripts'}:
            if type(value) is not bool:
                raise SchemaError('EVIDENCE_CONFIG_INVALID')
        elif key == 'node_env':
            if not isinstance(value, str) or value not in {'test', 'development', 'production'}:
                raise SchemaError('EVIDENCE_CONFIG_INVALID')
        elif key == 'dependency_tree_sha256':
            _digest(value)
        elif key == 'dependency_proof_kind' and value != 'installed-tree-v1':
            raise SchemaError('EVIDENCE_CONFIG_INVALID')
    if ('dependency_tree_sha256' in config) != ('dependency_proof_kind' in config):
        raise SchemaError('EVIDENCE_CONFIG_INVALID')


@bounded_errors
def environment_fingerprint(environment: dict) -> str:
    validate_environment(environment)
    return hashlib.sha256(canonical_json(environment)).hexdigest()


def _validate_binding(value):
    value = _object(value, {'snapshot', 'contract', 'environment'})
    snapshot = _object(value['snapshot'], {'repository', 'base', 'head_sha',
                                         'merge_base_sha', 'diff_sha256'})
    repository = _safe_text(snapshot['repository'], 1024)
    if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) is None:
        raise SchemaError('EVIDENCE_REPOSITORY_INVALID')
    base = _object(snapshot['base'], {'ref', 'sha'})
    ref = _safe_text(base['ref'], 1024)
    # Apply Git ref syntax while allowing named remote and short base refs.
    model._head_ref('refs/heads/' + ref)
    _digest(base['sha'], 40)
    _digest(snapshot['head_sha'], 40)
    _digest(snapshot['merge_base_sha'], 40)
    _digest(snapshot['diff_sha256'])
    contract = _object(value['contract'], {'report_text', 'diff_recipe', 'verdict_schema', 'evidence'})
    for key in ('report_text', 'diff_recipe', 'verdict_schema'):
        _integer(contract[key], 1, 2**31-1)
    _integer(contract['evidence'], 1, 1)
    validate_environment(value['environment'])


@bounded_errors
def validate_binding(actual: dict, expected: dict) -> None:
    _validate_binding(actual)
    _validate_binding(expected)
    if canonical_json(actual) != canonical_json(expected):
        raise SchemaError('EVIDENCE_BINDING_MISMATCH')


@bounded_errors
def validate_entry(entry: dict) -> None:
    item = _object(entry, ENTRY_KEYS)
    identifier = item['id']
    if not isinstance(identifier, str) or re.fullmatch(r'E[0-9]{3}', identifier) is None:
        raise SchemaError('EVIDENCE_ID_INVALID')
    _integer(int(identifier[1:]), 1, MAX_ENTRIES)
    argv = model._array(item['argv'], 256, 'EVIDENCE_COMMAND_INVALID')
    if not argv:
        raise SchemaError('EVIDENCE_COMMAND_INVALID')
    for arg in argv:
        _safe_text(arg, model.MAX_COMMAND_TEXT_BYTES)
    if sum(len(arg.encode('utf-8'))+1 for arg in argv)-1 > model.MAX_COMMAND_TEXT_BYTES:
        raise SchemaError('EVIDENCE_COMMAND_INVALID')
    cwd = _relative(item['cwd'], dot=True)
    render_report_command(argv, cwd)
    _integer(item['exit_code'], -(2**31), 2**31-1)
    timestamp = item['captured_at']
    if not isinstance(timestamp, str) or re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', timestamp) is None:
        raise SchemaError('EVIDENCE_TIMESTAMP_INVALID')
    datetime.fromisoformat(timestamp[:-1]+'+00:00')
    duration = item['duration_ms']
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise SchemaError('EVIDENCE_DURATION_INVALID')
    for key in ('stdout_excerpt', 'stderr_excerpt'):
        model._execution_excerpt(item[key])
    if type(item['truncated']) is not bool:
        raise SchemaError('EVIDENCE_SCHEMA_INVALID')
    for key in ('stdout_sha256', 'stderr_sha256', 'capture_sha256'):
        _digest(item[key])
    _integer(item['capture_bytes'], 0, MAX_CAPTURE_BYTES)
    if not isinstance(item['freshness'], str) or item['freshness'] not in {'deterministic', 'always-fresh'}:
        raise SchemaError('EVIDENCE_FRESHNESS_INVALID')


@bounded_errors
def parse_bundle(raw: bytes) -> dict:
    obj = _object(model._load_json(raw, limit=MAX_BUNDLE_BYTES,
                                  too_large='EVIDENCE_LIMIT_EXCEEDED'),
                  {'schema', 'binding', 'entries'})
    _integer(obj['schema'], 1, 1)
    _validate_binding(obj['binding'])
    entries = model._array(obj['entries'], MAX_ENTRIES, 'EVIDENCE_LIMIT_EXCEEDED')
    ids, commands = set(), set()
    total = 0
    for entry in entries:
        validate_entry(entry)
        identity = (tuple(entry['argv']), entry['cwd'])
        if entry['id'] in ids or identity in commands:
            raise SchemaError('EVIDENCE_DUPLICATE_IDENTITY')
        ids.add(entry['id'])
        commands.add(identity)
        total += entry['capture_bytes']
    if total > MAX_TOTAL_CAPTURE_BYTES:
        raise SchemaError('EVIDENCE_LIMIT_EXCEEDED')
    return obj


@bounded_errors
def parse_receipt(raw: bytes) -> dict:
    obj = _object(model._load_json(raw, limit=MAX_BUNDLE_BYTES,
                                  too_large='EVIDENCE_LIMIT_EXCEEDED'),
                  {'schema', 'binding', 'entry'})
    parse_bundle(canonical_json({'schema': obj['schema'], 'binding': obj['binding'],
                                 'entries': [obj['entry']]}))
    return obj


@bounded_errors
def encode_capture(stdout: bytes, stderr: bytes) -> bytes:
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise SchemaError('EVIDENCE_CAPTURE_INVALID')
    if len(stdout)+len(stderr) > MAX_CAPTURE_BYTES:
        raise SchemaError('EVIDENCE_LIMIT_EXCEEDED')
    for stream in (stdout, stderr):
        _sensitive(stream.decode('utf-8', 'replace'))
    return CAPTURE_MAGIC + struct.pack('>QQ', len(stdout), len(stderr)) + stdout + stderr


@bounded_errors
def decode_capture(raw: bytes) -> tuple[bytes, bytes]:
    if (not isinstance(raw, bytes) or len(raw) < CAPTURE_OVERHEAD
            or len(raw) > MAX_CAPTURE_BYTES+CAPTURE_OVERHEAD or not raw.startswith(CAPTURE_MAGIC)):
        raise SchemaError('EVIDENCE_CAPTURE_INVALID')
    out_size, err_size = struct.unpack('>QQ', raw[len(CAPTURE_MAGIC):CAPTURE_OVERHEAD])
    if out_size+err_size != len(raw)-CAPTURE_OVERHEAD:
        raise SchemaError('EVIDENCE_CAPTURE_INVALID')
    stdout, stderr = raw[CAPTURE_OVERHEAD:CAPTURE_OVERHEAD+out_size], raw[CAPTURE_OVERHEAD+out_size:]
    encode_capture(stdout, stderr)
    return stdout, stderr


def capture_digest(stdout: bytes, stderr: bytes) -> str:
    return hashlib.sha256(encode_capture(stdout, stderr)).hexdigest()


@bounded_errors
def capture_excerpt(raw: bytes) -> str:
    """UTF-8 replacement text, clipped to <=8 KiB at a UTF-8 boundary."""
    text = raw.decode('utf-8', 'replace').encode('utf-8')
    return text[:model.MAX_EVIDENCE_TEXT_BYTES].decode('utf-8', 'ignore')


@bounded_errors
def validate_entry_capture(entry: dict, raw: bytes) -> None:
    validate_entry(entry)
    stdout, stderr = decode_capture(raw)
    if (entry['capture_sha256'] != hashlib.sha256(raw).hexdigest()
            or entry['stdout_sha256'] != hashlib.sha256(stdout).hexdigest()
            or entry['stderr_sha256'] != hashlib.sha256(stderr).hexdigest()
            or entry['capture_bytes'] != len(stdout)+len(stderr)):
        raise SchemaError('EVIDENCE_CAPTURE_MISMATCH')
    truncated = any(len(stream.decode('utf-8', 'replace').encode('utf-8')) > model.MAX_EVIDENCE_TEXT_BYTES
                    for stream in (stdout, stderr))
    if (entry['stdout_excerpt'] != capture_excerpt(stdout)
            or entry['stderr_excerpt'] != capture_excerpt(stderr)
            or entry['truncated'] != truncated):
        raise SchemaError('EVIDENCE_EXCERPT_MISMATCH')
