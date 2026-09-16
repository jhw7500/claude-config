#!/usr/bin/python3
"""Bounded command observations for the Issue 113 comparison, not a sandbox.

Use the same helper in both native arms; audit transcripts for bypasses. Counts
are requested top-level commands, not subprocesses. Select Node 22.23.1 and
npm 10.9.8 on PATH before invocation. No source checkout or agent dispatch.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import stat
import sys
import time

HEAD = '47df3a80bfb97778812b3c094957701cb335687b'
MERGE_BASE = '439ddc35170df27bf74a056f48f28e7c07e4c384'
MCP_PROBE = """import {Client} from './node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js';
import {StdioClientTransport} from './node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js';
const client = new Client({name:'issue113-canary',version:'1.0.0'},{capabilities:{}});
const transport = new StdioClientTransport({command:process.execPath,args:['dist/index.js'],
 env:{...process.env,NOTION_API_KEY:'benchmark-placeholder'}});
try { await client.connect(transport); await client.ping();
 const tools = await client.listTools();
 console.log(JSON.stringify({initialized:true,ping:true,tools:tools.tools.map(t=>t.name)}));
} finally { await client.close(); }
"""
CASES = {
    'install': ['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'],
    'audit-prod': ['npm', 'audit', '--omit=dev', '--json'],
    'audit-full': ['npm', 'audit', '--json'],
    'build': ['npm', 'run', 'build'],
    'typecheck': ['npm', 'run', 'typecheck'],
    'test': ['npm', 'test', '--', '--no-cache', '--reporter=dot'],
    'coverage-smoke': ['npm', 'test', '--', '--no-cache', '--reporter=dot', '--coverage',
                       'src/__tests__/config.test.ts'],
    'mcp-smoke': ['node', '--input-type=module', '-e', ' '.join(MCP_PROBE.splitlines())],
    'runtime-scope': ['git', 'diff', '--name-only', MERGE_BASE + '..' + HEAD, '--', 'mcp-server/src'],
    'false-typecheck': ['npm', 'run', 'typecheck', '--', '--not-a-real-option-issue113'],
}


def private_directory(path):
    path = path.absolute()
    if path.resolve() != path:
        raise ValueError('CANARY_ARTIFACT_UNSAFE')
    path.mkdir(mode=0o700, exist_ok=True)
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ValueError('CANARY_ARTIFACT_UNSAFE')
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def write_private(directory, name, raw):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    try:
        os.fchmod(fd, 0o600)
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ValueError('CANARY_ARTIFACT_UNSAFE')
        with os.fdopen(os.dup(fd), 'wb') as stream:
            stream.write(raw)
    finally:
        os.close(fd)


def now():
    return datetime.now(timezone.utc).isoformat()


def observe(args, evidence, model, sanitized_environment, run_owned):
    argv = CASES[args.case] if args.case else args.argv
    if argv and argv[0] == '--':
        argv = argv[1:]
    if (not argv or (args.case and (args.category or args.argv))
        or (not args.case and not args.category)
        or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600):
        raise ValueError('CANARY_ARGUMENT_INVALID')
    # Never persist unsafe caller arguments. Fixed MCP code contains only a
    # non-secret placeholder and is selected by this program, not by a bundle.
    if not args.case:
        try:
            evidence.encode_capture(json.dumps(argv).encode(), b'')
        except model.TribunalError:
            raise ValueError('CANARY_ARGUMENT_INVALID') from None
    command = 'cd -- ' + shlex.quote('mcp-server' if args.case and args.case != 'runtime-scope' else '.') + ' && ' + shlex.join(argv)
    try:
        model._command(command)
    except model.TribunalError:
        raise ValueError('CANARY_ARGUMENT_INVALID') from None
    view = args.view.absolute()
    relative = 'mcp-server' if args.case and args.case != 'runtime-scope' else '.'
    cwd = view / relative
    if view.resolve(strict=True) != view or cwd.resolve(strict=True) != cwd or not cwd.is_dir():
        raise ValueError('CANARY_VIEW_INVALID')
    parent = private_directory(args.artifacts)
    identifier = secrets.token_hex(16)
    try:
        os.mkdir(identifier, mode=0o700, dir_fd=parent)
        directory = os.open(identifier, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    record = {'schema': 1, 'invocation_id': identifier, 'category': args.category or 'original-validation',
        'case': args.case, 'argv': argv, 'cwd': relative, 'started_at': now(), 'ended_at': None,
        'started_monotonic_ns': time.monotonic_ns(), 'ended_monotonic_ns': None,
        'duration_ms': None, 'exit_code': None, 'reason_code': None, 'execution': None}
    try:
        write_private(directory, 'observation.json', evidence.canonical_json(record))
        try:
            with sanitized_environment('node-lock-v1') as env:
                result = run_owned(argv, cwd=cwd, env=env, timeout_seconds=args.timeout,
                                   limit=evidence.MAX_CAPTURE_BYTES)
            record['exit_code'] = result['exit_code']
            reason = next((code for flag, code in (
                ('timed_out', 'EVIDENCE_TIMEOUT'), ('overflow', 'EVIDENCE_CAPTURE_TOO_LARGE'),
                ('cleanup_failed', 'EVIDENCE_PROCESS_CLEANUP_FAILED'),
                ('intervention', 'EVIDENCE_PROCESS_INTERVENTION')) if result[flag]), None)
            if reason:
                record['reason_code'] = reason
            else:
                stdout, stderr = result['stdout'], result['stderr']
                evidence.encode_capture(stdout, stderr)  # Policy before persistence or output.
                execution = {'command': command,
                    'exit_code': result['exit_code'], 'stdout_excerpt': evidence.capture_excerpt(stdout),
                    'stderr_excerpt': evidence.capture_excerpt(stderr),
                    'truncated': any(len(s.decode('utf-8', 'replace').encode()) > model.MAX_EVIDENCE_TEXT_BYTES
                                     for s in (stdout, stderr)),
                    'capture_sha256': hashlib.sha256(stdout + stderr).hexdigest()}
                model._parse_execution({'id': 'B-R1-E001', **execution}, reviewer=model.Reviewer.B,
                                       round_number=1)
                write_private(directory, 'stdout.bin', stdout)
                write_private(directory, 'stderr.bin', stderr)
                record['execution'] = execution
        except model.TribunalError as error:
            record['reason_code'] = error.code
        except (OSError, ValueError):
            record['reason_code'] = 'CANARY_EXECUTION_UNAVAILABLE'
        record.update(ended_at=now(), ended_monotonic_ns=time.monotonic_ns())
        record['duration_ms'] = (record['ended_monotonic_ns'] - record['started_monotonic_ns']) / 1_000_000
        write_private(directory, 'finished.json', evidence.canonical_json(record))
        os.replace('finished.json', 'observation.json', src_dir_fd=directory, dst_dir_fd=directory)
        return record
    finally:
        os.close(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True, help='explicit installed pre_pr_tribunal package directory')
    parser.add_argument('--view', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--case', choices=tuple(CASES))
    parser.add_argument('--category', choices=('inspection', 'verification'))
    parser.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        runtime = args.runtime.absolute()
        if runtime.resolve(strict=True) != runtime or runtime.name != 'pre_pr_tribunal':
            raise ValueError('CANARY_RUNTIME_INVALID')
        sys.path.insert(0, str(runtime.parent))
        from pre_pr_tribunal import evidence, model
        from pre_pr_tribunal.evidence_environment import sanitized_environment
        from pre_pr_tribunal.evidence_process import run_owned
        if Path(evidence.__file__).parent != runtime:
            raise ValueError('CANARY_RUNTIME_INVALID')
        result = observe(args, evidence, model, sanitized_environment, run_owned)
    except (OSError, ImportError):
        result = {'reason_code': 'CANARY_RUNTIME_OR_PATH_UNAVAILABLE'}
    except ValueError as error:
        result = {'reason_code': str(error) if str(error).startswith('CANARY_') else 'CANARY_ARGUMENT_INVALID'}
    print(json.dumps(result, ensure_ascii=True))
    return 1 if result.get('reason_code') else 0


if __name__ == '__main__':
    raise SystemExit(main())
