"""Fail-closed binding of direct PR creation to a current tribunal verdict."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path

from .git_state import (
    GitStateError,
    _command_output,
    _physical_root,
    _validated_cwd,
    _worktree_is_dirty,
    capture_snapshot,
)
from .model import GateStatus, SchemaError, TribunalError
from .shell_scan import ScanKind, scan_pr_create
from .verdict_store import read_verdict


class GateCode(str, Enum):
    NOT_PR_CREATE = "NOT_PR_CREATE"
    PASS = "PASS"
    COMMAND_AMBIGUOUS = "COMMAND_AMBIGUOUS"
    TRIBUNAL_REQUIRED = "TRIBUNAL_REQUIRED"
    REPOSITORY_UNSUPPORTED = "REPOSITORY_UNSUPPORTED"
    VERDICT_UNSAFE = "VERDICT_UNSAFE"
    VERDICT_INVALID = "VERDICT_INVALID"
    VERDICT_STALE = "VERDICT_STALE"
    WORKTREE_DIRTY = "WORKTREE_DIRTY"
    REVIEW_INCOMPLETE = "REVIEW_INCOMPLETE"
    BLOCKERS_OPEN = "BLOCKERS_OPEN"
    ROUND_LIMIT_EXHAUSTED = "ROUND_LIMIT_EXHAUSTED"


@dataclass(frozen=True)
class GateDecision:
    block: bool
    code: GateCode


_UNSAFE_VERDICT_CODES = {
    "FILE_UNSAFE",
    "INBOX_DIRECTORY_UNSAFE",
    "LOCK_FILE_UNSAFE",
    "REPOSITORY_DIRECTORY_UNSAFE",
    "REVIEW_DIRECTORY_UNSAFE",
    "ROUND_DIRECTORY_UNSAFE",
    "STORE_LOCKED",
    "VERDICT_FILE_UNSAFE",
    "VERDICT_NOT_IGNORED",
}
_UNSUPPORTED_GIT_CODES = {
    "BASE_INVALID",
    "DETACHED_HEAD",
    "NOT_GIT_REPOSITORY",
    "PATH_INVALID",
    "REPOSITORY_UNSUPPORTED",
}
_STALE_GIT_CODES = {"EMPTY_DIFF", "SNAPSHOT_CHANGED"}


def _decision(block: bool, code: GateCode) -> GateDecision:
    return GateDecision(block=block, code=code)


def _exact_clean_root(cwd: Path) -> tuple[Path | None, GateCode | None]:
    try:
        requested = _validated_cwd(cwd)
        root = _physical_root(requested)
        if requested.resolve(strict=True) != root:
            return None, GateCode.REPOSITORY_UNSUPPORTED
        status = _command_output(
            root, ("status", "--porcelain=v2", "-z", "--untracked-files=all")
        )
        if _worktree_is_dirty(status):
            return None, GateCode.WORKTREE_DIRTY
        return root, None
    except GitStateError as error:
        if error.code == "WORKTREE_DIRTY":
            return None, GateCode.WORKTREE_DIRTY
        return None, GateCode.REPOSITORY_UNSUPPORTED
    except Exception:
        return None, GateCode.REPOSITORY_UNSUPPORTED


def _missing_review_directory(root: Path) -> bool:
    try:
        os.lstat(root / ".review")
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _read_current_verdict(root: Path):
    if _missing_review_directory(root):
        return None, GateCode.TRIBUNAL_REQUIRED
    try:
        return read_verdict(root), None
    except SchemaError as error:
        if error.code == "VERDICT_MISSING":
            return None, GateCode.TRIBUNAL_REQUIRED
        if error.code in _UNSAFE_VERDICT_CODES or error.code.endswith("_UNSAFE"):
            return None, GateCode.VERDICT_UNSAFE
        return None, GateCode.VERDICT_INVALID
    except TribunalError:
        return None, GateCode.VERDICT_INVALID
    except Exception:
        return None, GateCode.VERDICT_INVALID


def _current_snapshot(root: Path, base_ref: str):
    try:
        return capture_snapshot(root, base_ref), None
    except GitStateError as error:
        if error.code == "WORKTREE_DIRTY":
            return None, GateCode.WORKTREE_DIRTY
        if error.code in _UNSUPPORTED_GIT_CODES:
            return None, GateCode.REPOSITORY_UNSUPPORTED
        if error.code in _STALE_GIT_CODES:
            return None, GateCode.VERDICT_STALE
        return None, GateCode.VERDICT_INVALID
    except TribunalError:
        return None, GateCode.VERDICT_INVALID
    except Exception:
        return None, GateCode.VERDICT_INVALID


def _snapshot_is_bound(verdict, snapshot) -> bool:
    return (
        verdict.repository,
        verdict.base_ref,
        verdict.base_sha,
        verdict.head_sha,
        verdict.merge_base_sha,
        verdict.diff_sha256,
    ) == (
        snapshot.repository,
        snapshot.base_ref,
        snapshot.base_sha,
        snapshot.head_sha,
        snapshot.merge_base_sha,
        snapshot.diff_sha256,
    )


def _evaluate_direct_pr_create(cwd: Path) -> GateDecision:
    root, preflight_error = _exact_clean_root(cwd)
    if preflight_error is not None or root is None:
        return _decision(True, preflight_error or GateCode.REPOSITORY_UNSUPPORTED)

    verdict, verdict_error = _read_current_verdict(root)
    if verdict_error is not None or verdict is None:
        return _decision(True, verdict_error or GateCode.VERDICT_INVALID)

    snapshot, snapshot_error = _current_snapshot(root, verdict.base_ref)
    if snapshot_error is not None or snapshot is None:
        return _decision(True, snapshot_error or GateCode.VERDICT_INVALID)
    if not _snapshot_is_bound(verdict, snapshot):
        return _decision(True, GateCode.VERDICT_STALE)

    if set(verdict.reviewers) != set("ABC") or any(
        slot.status != "complete" or slot.report is None
        for slot in verdict.reviewers.values()
    ):
        return _decision(True, GateCode.REVIEW_INCOMPLETE)
    if verdict.round not in {1, 2, 3}:
        return _decision(True, GateCode.VERDICT_INVALID)
    if verdict.gate.status is GateStatus.IN_PROGRESS:
        return _decision(True, GateCode.REVIEW_INCOMPLETE)
    if verdict.gate.status is GateStatus.FAIL:
        if verdict.gate.blocking_count <= 0:
            return _decision(True, GateCode.VERDICT_INVALID)
        if verdict.round == 3:
            return _decision(True, GateCode.ROUND_LIMIT_EXHAUSTED)
        return _decision(True, GateCode.BLOCKERS_OPEN)
    if verdict.gate.status is not GateStatus.PASS or verdict.gate.blocking_count != 0:
        return _decision(True, GateCode.VERDICT_INVALID)
    return _decision(False, GateCode.PASS)


def evaluate_gate(cwd: Path, command: str) -> GateDecision:
    """Return a bounded decision without executing the candidate command."""

    scan = scan_pr_create(command)
    if scan.kind is ScanKind.NO_MATCH:
        return _decision(False, GateCode.NOT_PR_CREATE)
    if scan.kind is ScanKind.AMBIGUOUS_CANDIDATE:
        return _decision(True, GateCode.COMMAND_AMBIGUOUS)
    try:
        return _evaluate_direct_pr_create(cwd)
    except Exception:
        return _decision(True, GateCode.VERDICT_INVALID)
