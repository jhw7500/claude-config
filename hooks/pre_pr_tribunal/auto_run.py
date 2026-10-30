"""Explicit, snapshot-bound authorization for a bounded Tribunal run chain."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

from . import model as m
from .git_state import capture_snapshot
from .review_context import current_contract_binding
from .review_store import (
    atomic_create_bytes,
    atomic_replace_bytes,
    check_ignored,
    locked_review,
    open_directory,
    preflight_review_directory,
    read_named_file,
    repository_root,
)


STORE_NAME = "auto-runs.json"
ACTIVE_NAME = "auto-run-active.json"
MAX_STORE_BYTES = 256 * 1024
MAX_ROUNDS = 3
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CHOICES = frozenset(("100 자동", "100 auto", "100 automatic"))
_CHANNELS = frozenset(("tty", "relayed"))
_RUNTIMES = frozenset(("claude", "codex"))
_CONTROLLER_STOP_REASONS = frozenset((
    "CANCELLED",
    "INTEGRITY_ERROR",
    "PENDING_TIMEOUT",
    "PROVIDER_FAILURE",
    "QUOTA_EXHAUSTED",
    "SCOPE_CHANGED",
    "USER_STOPPED",
    "VALIDATION_FAILED",
))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _elapsed_ms(started: object, ended: str) -> int:
    try:
        left = datetime.strptime(str(started), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        right = datetime.strptime(ended, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError, OverflowError):
        raise m.SchemaError("AUTO_RUN_STATE_INCONSISTENT") from None
    return max(0, int((right - left).total_seconds() * 1000))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _verdict_raw(verdict: m.Verdict) -> bytes:
    return json.dumps(
        verdict.to_json(), ensure_ascii=False, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")


def _snapshot(value: m.Snapshot) -> dict[str, object]:
    return {
        "repository": value.repository,
        "base_ref": value.base_ref,
        "base_sha": value.base_sha,
        "head_ref": value.head_ref,
        "head_sha": value.head_sha,
        "merge_base_sha": value.merge_base_sha,
        "diff_sha256": value.diff_sha256,
        "initial_paths": list(value.initial_paths),
    }


def _contract() -> dict[str, int]:
    value = current_contract_binding()
    return {
        "report_text": value.report_text,
        "diff_recipe": value.diff_recipe,
        "verdict_schema": value.verdict_schema,
    }


def _intensity_request() -> m.IntensityRequest:
    from .policy import parse_intensity_request

    return parse_intensity_request(
        ["100"], requester="auto-run-consent", reason="explicit-100-auto"
    )


def _read_prior_locked(review_fd: int) -> tuple[m.Verdict | None, bytes | None]:
    from .verdict_store import _parse_verdict

    try:
        raw = read_named_file(
            review_fd,
            "verdict.json",
            maximum=m.MAX_VERDICT_BYTES,
            missing="VERDICT_MISSING",
            unsafe="VERDICT_FILE_UNSAFE",
            exact_mode=0o600,
        )
    except m.SchemaError as error:
        if error.code == "VERDICT_MISSING":
            return None, None
        raise
    verdict = _parse_verdict(raw)
    if verdict.gate.status is m.GateStatus.IN_PROGRESS:
        raise m.SchemaError("AUTO_RUN_PENDING_VERDICT")
    return verdict, raw


def _binding_locked(
    root: Path,
    review_fd: int,
    *,
    base: str,
    runtime: str,
    now: Callable[[], str],
) -> dict[str, object]:
    if runtime not in _RUNTIMES:
        raise m.SchemaError("RUNTIME_INVALID")
    snapshot = capture_snapshot(root, base, now=now)
    from .policy import resolve_policy

    request = _intensity_request()
    policy = resolve_policy(root, snapshot, runtime=runtime, request=request)
    prior, prior_raw = _read_prior_locked(review_fd)
    binding: dict[str, object] = {
        "repository_root": os.fspath(root),
        "runtime": runtime,
        "snapshot": _snapshot(snapshot),
        "policy": policy.to_json(),
        "active_reviewers": list(policy.active_reviewers),
        "contract": _contract(),
        "prior_terminal": (
            None
            if prior is None
            else {
                "verdict_sha256": hashlib.sha256(prior_raw).hexdigest(),
                "round": prior.round,
                "gate": prior.gate.status.value,
            }
        ),
        "max_rounds": MAX_ROUNDS,
    }
    binding["binding_sha256"] = _digest(binding)
    return binding


def preview(
    cwd: Path,
    *,
    base: str,
    runtime: str,
    now: Callable[[], str] = utc_now,
) -> dict[str, object]:
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=True) as review_fd:
        return _binding_locked(root, review_fd, base=base, runtime=runtime, now=now)


def _empty_store() -> dict[str, object]:
    return {"schema": 1, "runs": []}


def _read_store_locked(review_fd: int, *, missing_ok: bool) -> dict[str, object]:
    try:
        raw = read_named_file(
            review_fd,
            STORE_NAME,
            maximum=MAX_STORE_BYTES,
            missing="AUTO_RUN_MISSING",
            unsafe="AUTO_RUN_FILE_UNSAFE",
            exact_mode=0o600,
        )
    except m.SchemaError as error:
        if missing_ok and error.code == "AUTO_RUN_MISSING":
            return _empty_store()
        raise
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        raise m.SchemaError("AUTO_RUN_INVALID") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "runs"}
        or value.get("schema") != 1
        or not isinstance(value.get("runs"), list)
        or len(value["runs"]) > 64
    ):
        raise m.SchemaError("AUTO_RUN_INVALID")
    for run in value["runs"]:
        if not isinstance(run, dict) or _RUN_ID.fullmatch(str(run.get("id", ""))) is None:
            raise m.SchemaError("AUTO_RUN_INVALID")
    return value


def _write_store_locked(review_fd: int, store: dict[str, object]) -> None:
    raw = json.dumps(store, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    atomic_replace_bytes(
        review_fd,
        STORE_NAME,
        raw,
        maximum=MAX_STORE_BYTES,
        too_large="AUTO_RUN_TOO_LARGE",
        unsafe="AUTO_RUN_FILE_UNSAFE",
        exact_mode=0o600,
        write_failed="AUTO_RUN_FILE_UNSAFE",
    )


def _write_active_locked(review_fd: int, auto_run_id: str) -> None:
    atomic_replace_bytes(
        review_fd,
        ACTIVE_NAME,
        _canonical({"schema": 1, "id": auto_run_id}),
        maximum=1024,
        too_large="AUTO_RUN_FILE_UNSAFE",
        unsafe="AUTO_RUN_FILE_UNSAFE",
        exact_mode=0o600,
        write_failed="AUTO_RUN_FILE_UNSAFE",
    )


def _active_locked(review_fd: int) -> str | None:
    try:
        raw = read_named_file(
            review_fd,
            ACTIVE_NAME,
            maximum=1024,
            missing="AUTO_RUN_MISSING",
            unsafe="AUTO_RUN_FILE_UNSAFE",
            exact_mode=0o600,
        )
    except m.SchemaError as error:
        if error.code == "AUTO_RUN_MISSING":
            return None
        raise
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        raise m.SchemaError("AUTO_RUN_INVALID") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "id"}
        or value.get("schema") != 1
        or not isinstance(value.get("id"), str)
        or _RUN_ID.fullmatch(value["id"]) is None
    ):
        raise m.SchemaError("AUTO_RUN_INVALID")
    return value["id"]


def _remove_active_locked(review_fd: int, auto_run_id: str) -> None:
    active = _active_locked(review_fd)
    if active is None:
        return
    if active != auto_run_id:
        raise m.SchemaError("AUTO_RUN_STATE_INCONSISTENT")
    try:
        os.unlink(ACTIVE_NAME, dir_fd=review_fd)
        os.fsync(review_fd)
    except OSError:
        raise m.SchemaError("AUTO_RUN_FILE_UNSAFE") from None


def ensure_manual_allowed_locked(review_fd: int) -> None:
    """An active chain cannot be bypassed through legacy one-shot authority."""
    if _active_locked(review_fd) is not None:
        raise m.SchemaError("AUTO_RUN_ID_REQUIRED")
    store = _read_store_locked(review_fd, missing_ok=True)
    if any(run.get("status") != "stopped" for run in store["runs"]):
        raise m.SchemaError("AUTO_RUN_ID_REQUIRED")


def _archive_round_directory(review_fd: int, run_id: str, round_number: int) -> int:
    archive_fd = run_fd = -1
    try:
        archive_fd = open_directory(
            review_fd,
            "auto-run-archive",
            create=True,
            code="AUTO_RUN_ARCHIVE_UNSAFE",
        )
        run_fd = open_directory(
            archive_fd,
            run_id,
            create=True,
            code="AUTO_RUN_ARCHIVE_UNSAFE",
        )
        return open_directory(
            run_fd,
            f"round-{round_number}",
            create=True,
            code="AUTO_RUN_ARCHIVE_UNSAFE",
        )
    finally:
        for descriptor in (run_fd, archive_fd):
            if descriptor >= 0:
                os.close(descriptor)


def _archive_create(directory: int, name: str, raw: bytes, maximum: int) -> str:
    return atomic_create_bytes(
        directory,
        name,
        raw,
        maximum=maximum,
        exists="AUTO_RUN_ARCHIVE_EXISTS",
        unsafe="AUTO_RUN_ARCHIVE_UNSAFE",
        exact_mode=0o600,
        write_failed="AUTO_RUN_ARCHIVE_UNSAFE",
    )


def _find(store: dict[str, object], auto_run_id: str) -> dict[str, object]:
    if not isinstance(auto_run_id, str) or _RUN_ID.fullmatch(auto_run_id) is None:
        raise m.SchemaError("AUTO_RUN_ID_INVALID")
    matches = [run for run in store["runs"] if run.get("id") == auto_run_id]
    if len(matches) != 1:
        raise m.SchemaError("AUTO_RUN_MISSING")
    return matches[0]


def initial_request_locked(
    review_fd: int,
    *,
    auto_run_id: str,
    snapshot: m.Snapshot,
    runtime: str,
) -> m.IntensityRequest:
    store = _read_store_locked(review_fd, missing_ok=False)
    run = _find(store, auto_run_id)
    binding = run.get("binding")
    if run.get("status") == "begin_reserved":
        raise m.SchemaError("AUTO_RUN_STATE_INCONSISTENT")
    if run.get("status") == "stopped":
        raise m.SchemaError("AUTO_RUN_STOPPED")
    if (
        run.get("status") != "granted"
        or _active_locked(review_fd) != auto_run_id
        or not isinstance(binding, dict)
        or binding.get("runtime") != runtime
        or binding.get("snapshot") != _snapshot(snapshot)
        or binding.get("contract") != _contract()
    ):
        raise m.SchemaError("AUTO_RUN_BINDING_CHANGED")
    return _intensity_request()


def record_grant(
    cwd: Path,
    *,
    base: str,
    runtime: str,
    expected_binding_sha256: str,
    choice: str,
    reason: str,
    channel: str,
    now: Callable[[], str] = utc_now,
    token_hex: Callable[[int], str] = secrets.token_hex,
) -> dict[str, object]:
    from .policy import MAX_REASON_BYTES, _bounded_text

    if choice not in _CHOICES:
        raise m.SchemaError("AUTO_RUN_CONSENT_REQUIRED")
    if channel not in _CHANNELS:
        raise m.SchemaError("AUTO_RUN_GRANT_INVALID")
    if not isinstance(expected_binding_sha256, str) or _SHA256.fullmatch(expected_binding_sha256) is None:
        raise m.SchemaError("AUTO_RUN_GRANT_INVALID")
    reason = _bounded_text(reason, MAX_REASON_BYTES, "AUTO_RUN_GRANT_INVALID")
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=True) as review_fd:
        binding = _binding_locked(root, review_fd, base=base, runtime=runtime, now=now)
        if binding["binding_sha256"] != expected_binding_sha256:
            raise m.SchemaError("AUTO_RUN_BINDING_CHANGED")
        store = _read_store_locked(review_fd, missing_ok=True)
        if len(store["runs"]) >= 64:
            raise m.SchemaError("AUTO_RUN_STORE_FULL")
        if any(run.get("status") not in {"stopped"} for run in store["runs"]):
            raise m.SchemaError("AUTO_RUN_ALREADY_ACTIVE")
        try:
            identifier = token_hex(16)
        except Exception:
            raise m.SchemaError("AUTO_RUN_GRANT_INVALID") from None
        if not isinstance(identifier, str) or _RUN_ID.fullmatch(identifier) is None:
            raise m.SchemaError("AUTO_RUN_GRANT_INVALID")
        run: dict[str, object] = {
            "id": identifier,
            "schema": 1,
            "status": "granted",
            "binding": binding,
            "binding_sha256": binding["binding_sha256"],
            "max_rounds": MAX_ROUNDS,
            "rounds": [],
            "current": None,
            "cumulative_elapsed_ms": 0,
            "stop_reason": None,
            "reason": reason,
            "channel": channel,
            "created_at": now(),
            "updated_at": now(),
        }
        store["runs"].append(run)
        _write_store_locked(review_fd, store)
        _write_active_locked(review_fd, identifier)
        return dict(run)


def status(cwd: Path, *, auto_run_id: str | None = None) -> dict[str, object]:
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        store = _read_store_locked(review_fd, missing_ok=False)
        run = _find(store, auto_run_id) if auto_run_id is not None else (
            store["runs"][-1] if store["runs"] else None
        )
        if run is None:
            raise m.SchemaError("AUTO_RUN_MISSING")
        return json.loads(json.dumps(run))


def stop(
    cwd: Path,
    *,
    auto_run_id: str,
    reason: str,
    now: Callable[[], str] = utc_now,
) -> dict[str, object]:
    if reason not in _CONTROLLER_STOP_REASONS:
        raise m.SchemaError("AUTO_RUN_STOP_INVALID")
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        store = _read_store_locked(review_fd, missing_ok=False)
        run = _find(store, auto_run_id)
        if run.get("status") == "stopped":
            return json.loads(json.dumps(run))
        if run.get("status") not in {"granted", "awaiting_fix", "begin_reserved"}:
            raise m.SchemaError("AUTO_RUN_STATE_INCONSISTENT")
        run["status"] = "stopped"
        run["stop_reason"] = reason
        run["updated_at"] = now()
        _remove_active_locked(review_fd, auto_run_id)
        _write_store_locked(review_fd, store)
        return dict(run)


def _git_bytes(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["/usr/bin/git", "-C", os.fspath(root), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            env={
                **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
                "LC_ALL": "C",
                "LANG": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_PAGER": "cat",
            },
        )
    except (OSError, subprocess.SubprocessError):
        raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED") from None
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED")
    return result.stdout


def _git(root: Path, *arguments: str) -> str:
    try:
        return _git_bytes(root, *arguments).decode("ascii", "strict").strip()
    except UnicodeError:
        raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED") from None


def _commit_paths(root: Path, parent: str, commit: str) -> set[str]:
    raw = _git_bytes(
        root,
        "diff-tree",
        "--no-commit-id",
        "--name-status",
        "-r",
        "-z",
        "-M",
        parent,
        commit,
    )
    fields = raw.split(b"\0")
    if fields[-1:] != [b""]:
        raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED")
    fields.pop()
    paths: set[str] = set()
    index = 0
    try:
        while index < len(fields):
            status = fields[index].decode("ascii", "strict")
            index += 1
            count = 2 if status.startswith(("R", "C")) else 1
            if not re.fullmatch(r"(?:[ACDMRTUXB]|[RC][0-9]{1,3})", status):
                raise ValueError
            for _ in range(count):
                path = fields[index].decode("utf-8", "strict")
                index += 1
                if not path or "\x00" in path:
                    raise ValueError
                paths.add(path)
    except (IndexError, UnicodeError, ValueError):
        raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED") from None
    return paths


def _verify_fix_commits(
    root: Path,
    *,
    prior_head: str,
    target_head: str,
    fix_commits: Sequence[str],
    allowed_paths: Sequence[str],
) -> list[str]:
    commits = list(fix_commits)
    if any(not isinstance(item, str) or _SHA1.fullmatch(item) is None for item in commits):
        raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED")
    if prior_head == target_head:
        if commits:
            raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED")
        return commits
    if not commits:
        raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED")
    actual = _git(root, "rev-list", "--reverse", "--ancestry-path", f"{prior_head}..{target_head}").splitlines()
    if actual != commits or commits[-1] != target_head:
        raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED")
    if _git(root, "rev-list", "--merges", f"{prior_head}..{target_head}"):
        raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED")
    if _git(root, "rev-parse", f"{prior_head}^{{tree}}") == _git(
        root, "rev-parse", f"{target_head}^{{tree}}"
    ):
        raise m.SchemaError("AUTO_RUN_NO_PROGRESS")
    parent = prior_head
    allowed = set(allowed_paths)
    for commit in commits:
        if _git(root, "rev-parse", f"{commit}^") != parent:
            raise m.SchemaError("AUTO_RUN_FIX_COMMITS_CHANGED")
        if not _commit_paths(root, parent, commit).issubset(allowed):
            raise m.SchemaError("AUTO_RUN_FIX_SCOPE_VIOLATION")
        parent = commit
    return commits


def _execution_sha256(execution: m.Execution) -> str:
    value = execution.to_json()
    value.pop("id", None)
    return _digest(value)


def _decision_execution_sha256s(decisions: Sequence[m.Decision]) -> list[str]:
    return sorted({
        _execution_sha256(execution)
        for decision in decisions
        for execution in decision.executions
    })


def _decision_evidence(decisions: Sequence[m.Decision]) -> str | None:
    if not decisions:
        return None
    return _digest(_decision_execution_sha256s(decisions))


def reserve_begin_locked(
    root: Path,
    review_fd: int,
    *,
    auto_run_id: str,
    expected_head: str,
    fix_commits: Sequence[str],
    prior: m.Verdict | None,
    prior_raw: bytes | None,
    pending: m.Verdict,
    decisions: Sequence[m.Decision],
    decisions_raw: bytes | None,
    decisions_sha256: str | None,
    now: Callable[[], str],
) -> None:
    if not isinstance(expected_head, str) or _SHA1.fullmatch(expected_head) is None:
        raise m.SchemaError("AUTO_RUN_EXPECTED_HEAD_INVALID")
    store = _read_store_locked(review_fd, missing_ok=False)
    run = _find(store, auto_run_id)
    if run.get("status") == "stopped":
        raise m.SchemaError("AUTO_RUN_STOPPED")
    if run.get("status") not in {"granted", "awaiting_fix"}:
        raise m.SchemaError("AUTO_RUN_STATE_INCONSISTENT")
    rounds = run.get("rounds")
    if not isinstance(rounds, list) or pending.round != len(rounds) + 1 or pending.round > MAX_ROUNDS:
        raise m.SchemaError("AUTO_RUN_ROUND_INVALID")
    binding = run.get("binding")
    if not isinstance(binding, dict):
        raise m.SchemaError("AUTO_RUN_INVALID")
    if (
        binding.get("repository_root") != os.fspath(root)
        or binding.get("runtime") != pending.producer_runtime
        or binding.get("contract") != _contract()
        or pending.policy is None
        or binding.get("policy") != pending.policy.to_json()
        or binding.get("active_reviewers") != list(pending.policy.active_reviewers)
        or binding.get("max_rounds") != MAX_ROUNDS
    ):
        raise m.SchemaError("AUTO_RUN_BINDING_CHANGED")
    if pending.round == 1:
        if run.get("status") != "granted" or expected_head != pending.head_sha or fix_commits:
            raise m.SchemaError("AUTO_RUN_BINDING_CHANGED")
        if binding.get("snapshot") != _snapshot(pending.snapshot):
            raise m.SchemaError("AUTO_RUN_BINDING_CHANGED")
        expected_prior = binding.get("prior_terminal")
        actual_prior = None if prior is None else {
            "verdict_sha256": hashlib.sha256(prior_raw).hexdigest(),
            "round": prior.round,
            "gate": prior.gate.status.value,
        }
        if expected_prior != actual_prior:
            raise m.SchemaError("AUTO_RUN_PRIOR_VERDICT_CHANGED")
        commits: list[str] = []
    else:
        if prior is None or prior_raw is None or prior.gate.status is not m.GateStatus.FAIL:
            raise m.SchemaError("AUTO_RUN_PRIOR_VERDICT_CHANGED")
        previous = rounds[-1]
        if (
            not isinstance(previous, dict)
            or previous.get("verdict_sha256") != hashlib.sha256(prior_raw).hexdigest()
            or previous.get("head_sha") != prior.head_sha
        ):
            raise m.SchemaError("AUTO_RUN_PRIOR_VERDICT_CHANGED")
        if expected_head != pending.head_sha:
            raise m.SchemaError("AUTO_RUN_EXPECTED_HEAD_CHANGED")
        commits = _verify_fix_commits(
            root,
            prior_head=prior.head_sha,
            target_head=expected_head,
            fix_commits=fix_commits,
            allowed_paths=binding["snapshot"]["initial_paths"],
        )
        evidence = _decision_evidence(decisions)
        if pending.head_sha == prior.head_sha:
            decision_executions = set(_decision_execution_sha256s(decisions))
            prior_executions = {
                digest
                for item in rounds
                for field in (
                    "decision_execution_sha256s",
                    "review_execution_sha256s",
                )
                for digest in item.get(field, [])
            }
            if (
                evidence is None
                or evidence in {item.get("decision_evidence_sha256") for item in rounds}
                or not decision_executions
                or decision_executions.issubset(prior_executions)
            ):
                raise m.SchemaError("AUTO_RUN_IDENTICAL_RETRY")
    evidence = _decision_evidence(decisions)
    if pending.round > 1:
        if decisions_raw is None:
            raise m.SchemaError("AUTO_RUN_STATE_INCONSISTENT")
        archive_fd = _archive_round_directory(
            review_fd, auto_run_id, pending.round - 1
        )
        try:
            decisions_archive_sha256 = _archive_create(
                archive_fd, "decisions.json", decisions_raw, m.MAX_REPORT_BYTES
            )
        finally:
            os.close(archive_fd)
    else:
        decisions_archive_sha256 = None
    run["status"] = "begin_reserved"
    run["current"] = {
        "round": pending.round,
        "lifecycle_id": pending.lifecycle_id,
        "head_sha": pending.head_sha,
        "expected_head": expected_head,
        "fix_commits": commits,
        "decisions_sha256": decisions_sha256,
        "decision_evidence_sha256": evidence,
        "decision_execution_sha256s": _decision_execution_sha256s(decisions),
        "decisions_archive_sha256": decisions_archive_sha256,
        "reserved_at": now(),
    }
    run["updated_at"] = now()
    _write_store_locked(review_fd, store)


def finalize_locked(
    review_fd: int,
    *,
    verdict: m.Verdict,
    continuation_allowed: bool = True,
    now: Callable[[], str],
) -> None:
    try:
        store = _read_store_locked(review_fd, missing_ok=True)
    except m.SchemaError:
        raise
    matches = [
        run for run in store["runs"]
        if isinstance(run.get("current"), dict)
        and run["current"].get("lifecycle_id") == verdict.lifecycle_id
    ]
    if not matches:
        if _active_locked(review_fd) is not None:
            raise m.SchemaError("AUTO_RUN_STATE_INCONSISTENT")
        return
    if len(matches) != 1:
        raise m.SchemaError("AUTO_RUN_STATE_INCONSISTENT")
    run = matches[0]
    stopped_during_round = run.get("status") == "stopped"
    if run.get("status") not in {"begin_reserved", "stopped"}:
        raise m.SchemaError("AUTO_RUN_STATE_INCONSISTENT")
    current = run["current"]
    if current.get("round") != verdict.round or verdict.gate.status is m.GateStatus.IN_PROGRESS:
        raise m.SchemaError("AUTO_RUN_STATE_INCONSISTENT")
    raw = _verdict_raw(verdict)
    archive_fd = _archive_round_directory(review_fd, run["id"], verdict.round)
    report_hashes: dict[str, str] = {}
    try:
        verdict_archive_sha256 = _archive_create(
            archive_fd, "verdict.json", raw, m.MAX_VERDICT_BYTES
        )
        inbox_fd = open_directory(
            review_fd, "inbox", create=False, code="AUTO_RUN_ARCHIVE_UNSAFE"
        )
        try:
            source_fd = open_directory(
                inbox_fd,
                f"round-{verdict.round}",
                create=False,
                code="AUTO_RUN_ARCHIVE_UNSAFE",
            )
        finally:
            os.close(inbox_fd)
        try:
            for reviewer in verdict.policy.active_reviewers:
                report_raw = read_named_file(
                    source_fd,
                    f"{reviewer}.json",
                    maximum=m.MAX_REPORT_BYTES,
                    missing="AUTO_RUN_ARCHIVE_UNSAFE",
                    unsafe="AUTO_RUN_ARCHIVE_UNSAFE",
                    exact_mode=0o600,
                )
                report_hashes[reviewer] = _archive_create(
                    archive_fd, f"{reviewer}.json", report_raw, m.MAX_REPORT_BYTES
                )
        finally:
            os.close(source_fd)
    finally:
        os.close(archive_fd)
    completed_at = now()
    elapsed_ms = _elapsed_ms(current.get("reserved_at"), completed_at)
    record = {
        "round": verdict.round,
        "lifecycle_id": verdict.lifecycle_id,
        "head_sha": verdict.head_sha,
        "diff_sha256": verdict.diff_sha256,
        "gate": verdict.gate.status.value,
        "blocking_count": verdict.gate.blocking_count,
        "verdict_sha256": hashlib.sha256(raw).hexdigest(),
        "verdict_archive_sha256": verdict_archive_sha256,
        "report_sha256": report_hashes,
        "review_execution_sha256s": sorted({
            _execution_sha256(execution)
            for reviewer in verdict.policy.active_reviewers
            for execution in verdict.reviewers[reviewer].report.executions
        }),
        "decisions_sha256": current.get("decisions_sha256"),
        "decision_evidence_sha256": current.get("decision_evidence_sha256"),
        "decision_execution_sha256s": current.get("decision_execution_sha256s"),
        "decisions_archive_sha256": current.get("decisions_archive_sha256"),
        "fix_commits": current.get("fix_commits"),
        "validation": "terminal",
        "elapsed_ms": elapsed_ms,
        "completed_at": completed_at,
    }
    run["rounds"].append(record)
    run["cumulative_elapsed_ms"] += elapsed_ms
    run["current"] = None
    if stopped_during_round:
        run["status"] = "stopped"
    elif (
        verdict.gate.status is m.GateStatus.FAIL
        and verdict.round < MAX_ROUNDS
        and continuation_allowed
    ):
        run["status"] = "awaiting_fix"
        run["stop_reason"] = None
    else:
        run["status"] = "stopped"
        run["stop_reason"] = (
            "INCONCLUSIVE_EVIDENCE"
            if verdict.gate.status is m.GateStatus.FAIL and not continuation_allowed
            else
            "MAX_ROUNDS"
            if verdict.gate.status is m.GateStatus.FAIL
            else verdict.gate.status.value.upper()
        )
    run["updated_at"] = now()
    if run["status"] == "stopped":
        _remove_active_locked(review_fd, run["id"])
    _write_store_locked(review_fd, store)
