"""Snapshot-bound, human-direct intensity grants (#162).

A grant records one human decision to review a snapshot below its built-in
floor. `begin` applies it only while the snapshot, committed configuration,
resolved floor/reasons, and installed contract still equal what the human saw.
It is friction against policy-following agent mistakes, not a security boundary.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path

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

GRANT_NAME = "intensity-grant.json"
MAX_GRANT_BYTES = 16 * 1024


def _binding(snapshot: m.Snapshot, policy: m.PolicyBinding) -> dict[str, object]:
    contract = current_contract_binding()
    return {
        "repository": snapshot.repository,
        "base_ref": snapshot.base_ref,
        "base_sha": snapshot.base_sha,
        "head_sha": snapshot.head_sha,
        "merge_base_sha": snapshot.merge_base_sha,
        "diff_sha256": snapshot.diff_sha256,
        "config_sha256": policy.config_sha256,
        "risk_floor": policy.risk_floor,
        "reasons": list(policy.reasons),
        "contract": {
            "report_text": contract.report_text,
            "diff_recipe": contract.diff_recipe,
            "verdict_schema": contract.verdict_schema,
        },
    }


def record_grant(
    cwd: Path,
    *,
    snapshot: m.Snapshot,
    policy: m.PolicyBinding,
    value: int,
    reason: str,
    now: Callable[[], str],
) -> dict[str, object]:
    """Write the grant for the snapshot and baseline policy the human was shown."""
    from .policy import MAX_REASON_BYTES, _bounded_text, validate_grant_lowering

    if policy.request.source != "default":
        raise m.SchemaError("GRANT_INVALID")
    validate_grant_lowering(policy.risk_floor, value)
    reason = _bounded_text(reason, MAX_REASON_BYTES, "GRANT_INVALID")
    payload: dict[str, object] = {
        "schema": 1,
        "binding": _binding(snapshot, policy),
        "value": value,
        "reason": reason,
        "created_at": now(),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=True) as review_fd:
        atomic_replace_bytes(
            review_fd, GRANT_NAME, raw, maximum=MAX_GRANT_BYTES,
            too_large="GRANT_INVALID", unsafe="GRANT_FILE_UNSAFE",
        )
    return payload


def matching_grant_request(
    review_fd: int, snapshot: m.Snapshot, baseline: Callable[[], m.PolicyBinding]
) -> m.IntensityRequest | None:
    """Return the grant's request only when it binds exactly this snapshot.

    A missing, malformed, or mismatched grant is ignored: the snapshot is then
    reviewed at its floor, which is never weaker than what the grant allowed.
    """
    try:
        raw = read_named_file(
            review_fd, GRANT_NAME, maximum=MAX_GRANT_BYTES,
            missing="GRANT_MISSING", unsafe="GRANT_FILE_UNSAFE", exact_mode=0o600,
        )
    except m.SchemaError as error:
        if error.code == "GRANT_MISSING":
            return None
        raise
    try:
        payload = json.loads(raw)
        value = payload["value"]
        reason = payload["reason"]
        binding = payload["binding"]
    except (ValueError, TypeError, KeyError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or not isinstance(value, int)
        or isinstance(value, bool)
        or not isinstance(reason, str)
        or binding != _binding(snapshot, baseline())
    ):
        return None
    return m.IntensityRequest(
        value, m.HUMAN_GRANT_SOURCE, m.HUMAN_GRANT_REQUESTER, reason
    )
