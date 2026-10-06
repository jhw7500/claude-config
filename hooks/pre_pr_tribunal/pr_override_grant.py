"""One-shot, snapshot-bound human approval for a terminal non-pass verdict.

Like ``intensity_grant``, a relayed grant records what a policy-following
agent says the user chose in its prompt.  It is friction against accidental
bypass, not cryptographic proof of human presence.  The gate consumes a
matching grant while holding the private review lock, so at most one tool call
can use it.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import shlex

from . import model as m
from .review_context import current_contract_binding
from .review_store import (
    atomic_replace_bytes,
    check_ignored,
    locked_review,
    preflight_review_directory,
    read_named_file,
    repository_root,
)
from .verdict_store import _parse_verdict, read_verdict


GRANT_NAME = "pr-override-grant.json"
MAX_GRANT_BYTES = 16 * 1024
_CHANNELS = frozenset(("tty", "relayed"))
_RUNTIMES = frozenset(("claude", "codex"))


def _validate_runtime(runtime: str) -> None:
    if runtime not in _RUNTIMES:
        raise m.SchemaError("PR_OVERRIDE_GRANT_INVALID")


def _eligible(verdict: m.Verdict) -> bool:
    return (
        verdict.gate.status is m.GateStatus.INCONCLUSIVE
        or (
            verdict.gate.status is m.GateStatus.FAIL
            and verdict.round == 3
            and verdict.gate.blocking_count > 0
        )
    )


def _binding(verdict: m.Verdict, raw: bytes) -> dict[str, object]:
    contract = current_contract_binding()
    return {
        "verdict_sha256": hashlib.sha256(raw).hexdigest(),
        "repository": verdict.repository,
        "base_ref": verdict.base_ref,
        "base_sha": verdict.base_sha,
        "head_ref": verdict.head_ref,
        "head_sha": verdict.head_sha,
        "merge_base_sha": verdict.merge_base_sha,
        "diff_sha256": verdict.diff_sha256,
        "round": verdict.round,
        "gate": verdict.gate.to_json(),
        "validation": (
            {
                "phase": verdict.validation.phase.value,
                "failure_code": verdict.validation.failure_code,
            }
            if verdict.validation is not None else None
        ),
        "contract": {
            "report_text": contract.report_text,
            "diff_recipe": contract.diff_recipe,
            "verdict_schema": contract.verdict_schema,
        },
    }


def _read_bound_verdict(review_fd: int) -> tuple[m.Verdict, bytes]:
    raw = read_named_file(
        review_fd,
        "verdict.json",
        maximum=m.MAX_VERDICT_BYTES,
        missing="VERDICT_MISSING",
        unsafe="VERDICT_FILE_UNSAFE",
    )
    return _parse_verdict(raw), raw


def preview_grant(cwd: Path, *, runtime: str) -> m.Verdict:
    """Return the exact terminal verdict only when the normal gate recognizes it."""
    _validate_runtime(runtime)
    verdict = read_verdict(cwd)
    command = (
        "PATH=/usr/bin:/bin /usr/bin/gh pr create --base "
        + shlex.quote(verdict.base_ref)
    )
    from .gate import GateCode, evaluate_gate

    decision = evaluate_gate(cwd, command)
    if decision.code not in {
        GateCode.ROUND_LIMIT_EXHAUSTED,
        GateCode.VERIFICATION_INCOMPLETE,
    } or not _eligible(verdict):
        raise m.SchemaError("PR_OVERRIDE_NOT_ELIGIBLE")
    return verdict


def preview_grant_binding(cwd: Path, *, runtime: str) -> dict[str, object]:
    """Return the exact verdict binding that may be shown for approval."""
    expected = preview_grant(cwd, runtime=runtime)
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        verdict, verdict_raw = _read_bound_verdict(review_fd)
        if verdict != expected or not _eligible(verdict):
            raise m.SchemaError("PR_OVERRIDE_VERDICT_CHANGED")
        return _binding(verdict, verdict_raw)


def record_grant(
    cwd: Path,
    *,
    runtime: str,
    expected_verdict_sha256: str,
    reason: str,
    channel: str,
    now: Callable[[], str],
) -> dict[str, object]:
    """Record approval only for the exact verdict bytes shown to the user."""
    from .policy import MAX_REASON_BYTES, _bounded_text

    if channel not in _CHANNELS:
        raise m.SchemaError("PR_OVERRIDE_GRANT_INVALID")
    if (
        not isinstance(expected_verdict_sha256, str)
        or len(expected_verdict_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_verdict_sha256
        )
    ):
        raise m.SchemaError("PR_OVERRIDE_GRANT_INVALID")
    reason = _bounded_text(
        reason, MAX_REASON_BYTES, "PR_OVERRIDE_GRANT_INVALID"
    )
    expected = preview_grant(cwd, runtime=runtime)
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        verdict, verdict_raw = _read_bound_verdict(review_fd)
        if verdict != expected or not _eligible(verdict):
            raise m.SchemaError("PR_OVERRIDE_NOT_ELIGIBLE")
        binding = _binding(verdict, verdict_raw)
        if binding["verdict_sha256"] != expected_verdict_sha256:
            raise m.SchemaError("PR_OVERRIDE_VERDICT_CHANGED")
        payload: dict[str, object] = {
            "schema": 1,
            "binding": binding,
            "runtime": runtime,
            "reason": reason,
            "channel": channel,
            "created_at": now(),
        }
        raw = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode()
        atomic_replace_bytes(
            review_fd,
            GRANT_NAME,
            raw,
            maximum=MAX_GRANT_BYTES,
            too_large="PR_OVERRIDE_GRANT_INVALID",
            unsafe="PR_OVERRIDE_GRANT_FILE_UNSAFE",
        )
        return payload


def revoke_grant(cwd: Path) -> bool:
    root = repository_root(cwd)
    if not (root / ".review").exists():
        return False
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        try:
            read_named_file(
                review_fd,
                GRANT_NAME,
                maximum=MAX_GRANT_BYTES,
                missing="PR_OVERRIDE_GRANT_MISSING",
                unsafe="PR_OVERRIDE_GRANT_FILE_UNSAFE",
                exact_mode=0o600,
            )
        except m.SchemaError as error:
            if error.code == "PR_OVERRIDE_GRANT_MISSING":
                return False
            raise
        try:
            os.unlink(GRANT_NAME, dir_fd=review_fd)
            os.fsync(review_fd)
        except OSError:
            raise m.SchemaError("PR_OVERRIDE_GRANT_FILE_UNSAFE") from None
        return True


def consume_matching_grant(
    cwd: Path, *, verdict: m.Verdict, runtime: str
) -> bool:
    """Atomically consume one exact matching grant; mismatches remain denied."""
    _validate_runtime(runtime)
    if not _eligible(verdict):
        return False
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        current, verdict_raw = _read_bound_verdict(review_fd)
        if current != verdict or not _eligible(current):
            return False
        try:
            raw = read_named_file(
                review_fd,
                GRANT_NAME,
                maximum=MAX_GRANT_BYTES,
                missing="PR_OVERRIDE_GRANT_MISSING",
                unsafe="PR_OVERRIDE_GRANT_FILE_UNSAFE",
                exact_mode=0o600,
            )
        except m.SchemaError as error:
            if error.code == "PR_OVERRIDE_GRANT_MISSING":
                return False
            raise
        try:
            payload = json.loads(raw)
            binding = payload["binding"]
            reason = payload["reason"]
            channel = payload["channel"]
        except (ValueError, TypeError, KeyError, RecursionError, AttributeError):
            return False
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != 1
            or payload.get("runtime") != runtime
            or channel not in _CHANNELS
            or not isinstance(reason, str)
            or binding != _binding(current, verdict_raw)
        ):
            return False
        from .policy import MAX_REASON_BYTES, _bounded_text

        try:
            _bounded_text(
                reason, MAX_REASON_BYTES, "PR_OVERRIDE_GRANT_INVALID"
            )
        except m.SchemaError:
            return False
        try:
            os.unlink(GRANT_NAME, dir_fd=review_fd)
            os.fsync(review_fd)
        except OSError:
            raise m.SchemaError("PR_OVERRIDE_GRANT_FILE_UNSAFE") from None
        return True
