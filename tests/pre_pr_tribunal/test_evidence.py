"""Behavior and hostile-storage tests for immutable validation evidence."""
import copy
import hashlib
import json
import os
import stat
import subprocess

import pytest

from pre_pr_tribunal.model import SchemaError
from pre_pr_tribunal import evidence, evidence_store


@pytest.fixture
def api():
    return evidence, evidence_store


@pytest.fixture
def repo(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitignore').write_text('.review/\n')
    return tmp_path


def binding():
    return {'snapshot': {'repository': 'owner/repo', 'base': {'ref': 'main', 'sha': 'a'*40},
                         'head_sha': 'b'*40, 'merge_base_sha': 'a'*40, 'diff_sha256': 'c'*64},
            'contract': {'report_text': 2, 'diff_recipe': 1, 'verdict_schema': 3, 'evidence': 1},
            'environment': {'profile': 'python-v1', 'cwd': '.',
                            'inputs': [{'path': 'test.py', 'sha256': 'd'*64}],
                            'tools': [{'name': 'python3', 'version': '3.12.1',
                                       'executable_sha256': 'e'*64}], 'config': {}}}


def entry(api, stdout=b'ok\n', stderr=b'', **updates):
    evidence, _ = api
    value = {'id': 'E001', 'argv': ['python3', '-m', 'pytest'], 'cwd': '.', 'exit_code': 0,
             'captured_at': '2026-09-12T00:00:00Z', 'duration_ms': 1.5,
             'stdout_excerpt': stdout.decode(), 'stderr_excerpt': stderr.decode(),
             'truncated': False, 'stdout_sha256': hashlib.sha256(stdout).hexdigest(),
             'stderr_sha256': hashlib.sha256(stderr).hexdigest(),
             'capture_sha256': evidence.capture_digest(stdout, stderr),
             'capture_bytes': len(stdout)+len(stderr), 'freshness': 'deterministic'}
    value.update(updates)
    return value


def bundle(api, **updates):
    value = {'schema': 1, 'binding': binding(), 'entries': [entry(api)]}
    value.update(updates)
    return value


def parse(api, value):
    return api[0].parse_bundle(json.dumps(value).encode())


def test_capture_boundary_and_strict_frames(api):
    evidence, _ = api
    assert evidence.capture_digest(b'ab', b'c') != evidence.capture_digest(b'a', b'bc')
    raw = evidence.encode_capture(b'ab', b'c')
    assert evidence.decode_capture(raw) == (b'ab', b'c')
    for bad in (raw[:-1], raw+b'x', b'', b'x'+raw[1:]):
        with pytest.raises(SchemaError, match='^EVIDENCE_'):
            evidence.decode_capture(bad)


@pytest.mark.parametrize('field,value', [('schema', True), ('schema', 1.0), ('unknown', 1)])
def test_bundle_strict_fields(api, field, value):
    obj = bundle(api)
    obj[field] = value
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        parse(api, obj)


@pytest.mark.parametrize('field,value', [
    ('id', 'E065'), ('argv', []), ('argv', ['x'*4097]), ('cwd', '../a'),
    ('exit_code', True), ('duration_ms', True), ('duration_ms', float('inf')),
    ('duration_ms', -1), ('captured_at', '2026-09-12T00:00:00+01:00'),
    ('captured_at', '2026-02-30T00:00:00Z'), ('capture_bytes', True),
    ('capture_bytes', 1048577), ('stdout_sha256', 'bad'), ('truncated', 0),
    ('freshness', 'maybe'), ('argv', ['ghp_abcdefghijk']), ('cwd', '/home/user/project'),
])
def test_entry_validation(api, field, value):
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        parse(api, bundle(api, entries=[entry(api, **{field: value})]))


def test_duplicate_keys_identities_and_limits(api):
    evidence, _ = api
    raw = json.dumps(bundle(api)).encode().replace(b'"schema": 1', b'"schema": 1, "schema": 1')
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        evidence.parse_bundle(raw)
    first = entry(api)
    for entries in ([first, first], [first, dict(first, id='E002')],
                    [dict(first, id=f'E{i:03}', argv=['tool', str(i)], capture_bytes=1048576)
                     for i in range(1, 18)]):
        with pytest.raises(SchemaError, match='^EVIDENCE_'):
            parse(api, bundle(api, entries=entries))
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        evidence.parse_bundle(b' '*262145)


@pytest.mark.parametrize('mutation', [
    lambda b: b['snapshot'].update(head_sha='f'*40),
    lambda b: b['snapshot']['base'].update(ref='other'),
    lambda b: b['snapshot']['base'].update(sha='f'*40),
    lambda b: b['snapshot'].update(diff_sha256='f'*64),
    lambda b: b['contract'].update(report_text=3),
    lambda b: b['environment']['tools'][0].update(version='3.13.0'),
])
def test_binding_drift(api, mutation):
    actual = binding()
    mutation(actual)
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        api[0].validate_binding(actual, binding())


@pytest.mark.parametrize('mutation', [
    lambda e: e.update(profile='arbitrary'),
    lambda e: e.update(config={'TOKEN': 'secret'}),
    lambda e: e['tools'][0].update(name='arbitrary'),
    lambda e: e['tools'][0].update(version='/home/user/bin/python'),
    lambda e: e['inputs'][0].update(path='../secret'),
    lambda e: e['inputs'].append(e['inputs'][0]),
    lambda e: e['tools'].append(e['tools'][0]),
])
def test_environment_strict(api, mutation):
    obj = bundle(api)
    mutation(obj['binding']['environment'])
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        parse(api, obj)


def test_capture_metadata_and_truncation(api):
    evidence, _ = api
    raw = evidence.encode_capture(b'ok\n', b'')
    evidence.validate_entry_capture(entry(api), raw)
    for field, value in [('stdout_excerpt', 'wrong'), ('truncated', True),
                         ('capture_bytes', 4), ('stdout_sha256', 'f'*64),
                         ('capture_sha256', 'f'*64)]:
        with pytest.raises(SchemaError, match='^EVIDENCE_'):
            evidence.validate_entry_capture(entry(api, **{field: value}), raw)
    large = b'a'*9000
    value = entry(api, large, stdout_excerpt='a'*8192, truncated=True)
    evidence.validate_entry_capture(value, evidence.encode_capture(large, b''))
    for sensitive in (b'ghp_abcdefghijk', b'/home/user/secret'):
        with pytest.raises(SchemaError, match='^EVIDENCE_'):
            evidence.encode_capture(sensitive, b'')


def test_real_store_roundtrip_and_eligibility(api, repo):
    evidence, store = api
    capture = store.put_capture(repo, b'ok\n', b'')
    assert store.read_capture(repo, capture) == evidence.encode_capture(b'ok\n', b'')
    receipt = {'schema': 1, 'binding': binding(), 'entry': entry(api)}
    rdigest = store.put_receipt(repo, receipt)
    assert store.read_receipt(repo, rdigest) == receipt
    obj = bundle(api, entries=[entry(api), entry(api, id='E002', argv=['npm', 'audit'],
                                              freshness='always-fresh')])
    digest = store.put_bundle(repo, obj)
    assert store.put_bundle(repo, obj) == digest
    assert store.read_bundle(repo, digest) == obj
    result = store.verify_bundle(repo, digest, binding())
    assert result['eligible'] == [obj['entries'][0]]
    assert result['rejected'] == [{'id': 'E002', 'reason': 'EVIDENCE_ALWAYS_FRESH'}]
    for path in (repo / '.review' / 'evidence').rglob('*'):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize('kind', ['bundles', 'captures'])
@pytest.mark.parametrize('attack', ['missing', 'modified', 'symlink', 'fifo', 'mode', 'owner'])
def test_hostile_artifacts(api, repo, kind, attack, monkeypatch):
    _, store = api
    capture = store.put_capture(repo, b'ok\n', b'')
    digest = store.put_bundle(repo, bundle(api))
    target = repo / '.review' / 'evidence' / kind / (digest+'.json' if kind == 'bundles' else capture+'.bin')
    original = target.read_bytes()
    if attack == 'modified':
        target.write_bytes(original[:-1]+bytes([original[-1]^1]))
    elif attack == 'mode':
        target.chmod(0o400)
    elif attack == 'owner':
        original_fstat = os.fstat
        inode = target.stat().st_ino
        def foreign_owner(fd):
            value = original_fstat(fd)
            if value.st_ino == inode:
                fields = list(value)
                fields[4] = os.geteuid()+1
                return os.stat_result(fields)
            return value
        monkeypatch.setattr(os, 'fstat', foreign_owner)
    else:
        target.unlink()
        if attack == 'symlink':
            target.symlink_to(repo / '.gitignore')
        elif attack == 'fifo':
            os.mkfifo(target, 0o600)
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        store.verify_bundle(repo, digest, binding())


def test_self_rehash_does_not_replace_pinned_digest(api, repo):
    _, store = api
    store.put_capture(repo, b'ok\n', b'')
    original = bundle(api)
    digest = store.put_bundle(repo, original)
    changed = copy.deepcopy(original)
    changed['entries'][0]['duration_ms'] = 10
    replacement = store.put_bundle(repo, changed)
    path = repo / '.review' / 'evidence' / 'bundles'
    (path / (digest+'.json')).write_bytes((path / (replacement+'.json')).read_bytes())
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        store.verify_bundle(repo, digest, binding())
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        store.put_bundle(repo, original)


def test_interrupted_publication_preserves_complete_artifact(api, repo, monkeypatch):
    _, store = api
    from pre_pr_tribunal import review_store
    store.put_capture(repo, b'ok\n', b'')
    original = review_store._link_descriptor
    def interrupted(parent, fd, name):
        original(parent, fd, name)
        raise OSError('simulated interruption after publication')
    with monkeypatch.context() as patch:
        patch.setattr(review_store, '_link_descriptor', interrupted)
        with pytest.raises(SchemaError, match='^EVIDENCE_'):
            store.put_bundle(repo, bundle(api))
    files = list((repo / '.review' / 'evidence' / 'bundles').iterdir())
    assert len(files) == 1
    assert store.read_bundle(repo, files[0].stem) == bundle(api)
    assert store.put_bundle(repo, bundle(api)) == files[0].stem


def test_verification_under_existing_review_lock(api, repo):
    from pre_pr_tribunal import review_store
    _, store = api
    store.put_capture(repo, b'ok\n', b'')
    digest = store.put_bundle(repo, bundle(api))
    with review_store.locked_review(repo, create=False):
        assert store.verify_bundle(repo, digest, binding())['eligible'] == [entry(api)]


def test_node_dependency_config_and_complete_secret_scan(api):
    evidence, _ = api
    env = binding()['environment']
    env['profile'] = 'node-lock-v1'
    env['tools'] = [{'name': name, 'version': 'v22.0.0', 'executable_sha256': 'a'*64}
                    for name in ('node', 'npm')]
    env['config'] = {'dependency_tree_sha256': 'b'*64, 'dependency_proof_kind': 'installed-tree-v1'}
    evidence.validate_environment(env)
    for key in tuple(env['config']):
        bad = copy.deepcopy(env)
        del bad['config'][key]
        with pytest.raises(SchemaError, match='^EVIDENCE_'):
            evidence.validate_environment(bad)
    assert evidence.environment_fingerprint(env) == hashlib.sha256(evidence.canonical_json(env)).hexdigest()
    for secret in (b'ghp_abcdefghijk', b'/home/user/secret'):
        with pytest.raises(SchemaError, match='^EVIDENCE_'):
            evidence.encode_capture(b'a'*9000+secret, b'')


def test_node_sandbox_environment_requires_exact_contract(api):
    evidence, _ = api
    env = binding()['environment']
    env['profile'] = 'node-sandbox-v1'
    env['tools'] = [{'name': name, 'version': 'v22.0.0', 'executable_sha256': 'a'*64}
                    for name in ('node', 'npm', 'bwrap')]
    env['config'] = {'dependency_tree_sha256': 'b'*64,
                     'dependency_proof_kind': 'installed-tree-v1',
                     'sandbox_kind': 'bubblewrap-clean-clone-v1'}
    evidence.validate_environment(env)

    missing = copy.deepcopy(env)
    del missing['config']['sandbox_kind']
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        evidence.validate_environment(missing)

    legacy = copy.deepcopy(env)
    legacy['profile'] = 'node-lock-v1'
    legacy['tools'] = legacy['tools'][:2]
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        evidence.validate_environment(legacy)


def test_evidence_contract_reads_v1_and_v2_only(api):
    for version in (1, 2):
        obj = bundle(api)
        obj['binding']['contract']['evidence'] = version
        parse(api, obj)
    obj = bundle(api)
    obj['binding']['contract']['evidence'] = 3
    with pytest.raises(SchemaError, match='^EVIDENCE_'):
        parse(api, obj)
