"""Explicit JSON command-line adapter for backlog checkpoint primitives."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from typing import Any, Sequence

from .admission import acquire_admission, reconcile_admission, release_admission
from .codec import CheckpointError, CheckpointIndeterminateError, canonical_bytes
from .git_evidence import bind_commit, collect_sources, inspect_worktree
from .queue import collect_queue, rank_issues
from .storage import read_evidence, read_snapshot, write_evidence, write_snapshot


_MAX_INPUT = 1024 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError("input JSON has a duplicate key")
        result[key] = value
    return result


def _input(path: str) -> Any:
    descriptor = -1
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(_MAX_INPUT + 1)
        else:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CheckpointError("input JSON must be a regular file")
            chunks: list[bytes] = []
            remaining = _MAX_INPUT + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
    except CheckpointError:
        raise
    except OSError as exc:
        raise CheckpointError("input JSON is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > _MAX_INPUT:
        raise CheckpointError("input JSON exceeds 1 MiB")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except CheckpointError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CheckpointError("input JSON is invalid") from exc


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CheckpointError(f"{label} has missing or unknown keys")
    return value


def _emit(value: Any, *, stream: Any = sys.stdout) -> None:
    data = canonical_bytes(value)
    if len(data) > 2 * 1024 * 1024:
        raise CheckpointError("CLI output exceeds 2 MiB")
    stream.buffer.write(data + b"\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backlog-checkpoint")
    groups = parser.add_subparsers(dest="group", required=True)

    snapshot = groups.add_parser("snapshot").add_subparsers(dest="verb", required=True)
    read = snapshot.add_parser("read")
    read.add_argument("--path", required=True)
    write = snapshot.add_parser("write")
    write.add_argument("--path", required=True)
    write.add_argument("--input", required=True)
    write.add_argument("--expected-generation", required=True)

    evidence = groups.add_parser("evidence").add_subparsers(dest="verb", required=True)
    for verb in ("read", "write"):
        command = evidence.add_parser(verb)
        command.add_argument("--state-root", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--input", required=True)

    admission = groups.add_parser("admission").add_subparsers(
        dest="verb", required=True
    )
    for verb in ("acquire", "release", "reconcile"):
        command = admission.add_parser(verb)
        command.add_argument("--state-root", required=True)
        command.add_argument("--worktree-root", required=True)
        command.add_argument("--input", required=True)

    git = groups.add_parser("git").add_subparsers(dest="verb", required=True)
    inspect = git.add_parser("inspect")
    inspect.add_argument("--root", required=True)
    inspect.add_argument("--branch", required=True)
    inspect.add_argument(
        "--record-root",
        required=True,
        help="exact physical path expected in the Git worktree record",
    )
    collect = git.add_parser("collect")
    collect.add_argument("--root", required=True)
    collect.add_argument("--input", required=True)
    collect.add_argument("--checkpoint-path")
    bind = git.add_parser("bind")
    bind.add_argument("--root", required=True)
    bind.add_argument("--parent-head", required=True)
    bind.add_argument("--commit-oid", required=True)
    bind.add_argument("--input", required=True)
    bind.add_argument("--checkpoint-path")

    queue = groups.add_parser("queue").add_subparsers(dest="verb", required=True)
    plan = queue.add_parser("plan")
    source = plan.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo")
    source.add_argument("--input")
    plan.add_argument("--explicit-edges")
    return parser


def _dispatch(arguments: argparse.Namespace) -> Any:
    group, verb = arguments.group, arguments.verb
    if group == "snapshot":
        if verb == "read":
            return read_snapshot(arguments.path)
        generation = arguments.expected_generation
        expected = None if generation == "new" else int(generation)
        return write_snapshot(arguments.path, _input(arguments.input), expected)
    if group == "evidence":
        value = _input(arguments.input)
        if verb == "write":
            return write_evidence(arguments.state_root, arguments.run_id, value)
        return read_evidence(arguments.state_root, arguments.run_id, value)
    if group == "admission":
        value = _input(arguments.input)
        if verb == "acquire":
            return acquire_admission(
                arguments.state_root, arguments.worktree_root, value
            )
        if verb == "release":
            release_admission(arguments.state_root, arguments.worktree_root, value)
            return None
        payload = _object(value, {"expected", "successor", "proof"}, "reconcile input")
        return reconcile_admission(
            arguments.state_root,
            arguments.worktree_root,
            payload["expected"],
            payload["successor"],
            payload["proof"],
        )
    if group == "git":
        if verb == "inspect":
            return inspect_worktree(
                arguments.root, arguments.branch, arguments.record_root
            )
        value = _input(arguments.input)
        if verb == "collect":
            payload = _object(value, {"paths"}, "collect input")
            return collect_sources(
                arguments.root, payload["paths"], arguments.checkpoint_path
            )
        return bind_commit(
            arguments.root,
            arguments.parent_head,
            value,
            arguments.commit_oid,
            arguments.checkpoint_path,
        )
    if arguments.repo is not None:
        edges = (
            [] if arguments.explicit_edges is None else _input(arguments.explicit_edges)
        )
        if not isinstance(edges, list):
            raise CheckpointError("explicit edges input must be a JSON array")
        return collect_queue(arguments.repo, edges)
    if arguments.explicit_edges is not None:
        raise CheckpointError("explicit edges require --repo")
    payload = _object(
        _input(arguments.input), {"issues", "dependencies"}, "queue input"
    )
    return rank_issues(payload["issues"], payload["dependencies"])


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = _dispatch(arguments)
        _emit({"ok": True, "result": result})
        return 0
    except CheckpointIndeterminateError as exc:
        _emit({"ok": False, "status": "PAUSED", "error": str(exc)}, stream=sys.stderr)
        return 3
    except (CheckpointError, ValueError) as exc:
        _emit({"ok": False, "status": "ERROR", "error": str(exc)}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
