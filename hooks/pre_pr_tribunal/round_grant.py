"""One-shot approval for starting a new round after a terminal verdict.

A relayed grant records an agent's attestation that the user approved the exact
binding shown by ``re-review-preview``.  It is workflow friction, not
cryptographic proof of human presence.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from . import model as m
from .git_state import GitStateError, assert_auto_fix_scope, capture_snapshot
from .review_context import current_contract_binding
from .review_store import (
    atomic_replace_bytes,
    check_ignored,
    locked_review,
    preflight_review_directory,
    read_named_file,
    repository_root,
)


GRANT_NAME = "re-review-grant.json"
MAX_GRANT_BYTES = 32 * 1024
_CHANNELS = frozenset(("tty", "relayed"))
_RUNTIMES = frozenset(("claude", "codex"))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot_binding(snapshot: m.Snapshot) -> dict[str, object]:
    return {
        "repository": snapshot.repository,
        "base_ref": snapshot.base_ref,
        "base_sha": snapshot.base_sha,
        "head_ref": snapshot.head_ref,
        "head_sha": snapshot.head_sha,
        "merge_base_sha": snapshot.merge_base_sha,
        "diff_sha256": snapshot.diff_sha256,
    }


def binding_sha256(binding: dict[str, object]) -> str:
    """Hash every binding field except the digest field itself."""
    unsigned = {
        key: value for key, value in binding.items() if key != "binding_sha256"
    }
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def grant_binding(
    prior: m.Verdict,
    prior_raw: bytes,
    *,
    snapshot: m.Snapshot,
    runtime: str,
    round_number: int,
    policy: m.PolicyBinding,
    decisions_sha256: str | None,
    evidence_bundle_sha256: str | None,
    evidence_binding: m.EvidenceBinding | None,
    evidence_fallback_reason: str | None,
) -> dict[str, object]:
    """Build the exact prior-verdict and proposed-round approval binding."""
    contract = current_contract_binding()
    binding: dict[str, object] = {
        "verdict_sha256": hashlib.sha256(prior_raw).hexdigest(),
        "prior": {
            **_snapshot_binding(prior.snapshot),
            "round": prior.round,
            "gate": prior.gate.to_json(),
        },
        "target": {
            **_snapshot_binding(snapshot),
            "runtime": runtime,
            "round": round_number,
            "policy": policy.to_json(),
            "active_reviewers": list(policy.active_reviewers),
            "decisions_sha256": decisions_sha256,
            "evidence": {
                "requested_bundle_sha256": evidence_bundle_sha256,
                "selection": (
                    evidence_binding.to_json()
                    if evidence_binding is not None
                    else None
                ),
                "fallback_reason": evidence_fallback_reason,
            },
        },
        "contract": {
            "report_text": contract.report_text,
            "diff_recipe": contract.diff_recipe,
            "verdict_schema": contract.verdict_schema,
        },
    }
    binding["binding_sha256"] = binding_sha256(binding)
    return binding


def _read_prior_locked(review_fd: int) -> tuple[m.Verdict, bytes]:
    from .verdict_store import _parse_verdict

    raw = read_named_file(
        review_fd,
        "verdict.json",
        maximum=m.MAX_VERDICT_BYTES,
        missing="VERDICT_MISSING",
        unsafe="VERDICT_FILE_UNSAFE",
    )
    verdict = _parse_verdict(raw)
    if verdict.gate.status is m.GateStatus.IN_PROGRESS:
        raise m.SchemaError("ROUND_TRANSITION_INVALID")
    return verdict, raw


def _target_binding_locked(
    root: Path,
    review_fd: int,
    prior: m.Verdict,
    prior_raw: bytes,
    *,
    base: str,
    runtime: str,
    round_number: int,
    decisions_path: Path | None,
    intensity_values: Sequence[str] | None,
    intensity_requester: str | Sequence[str] | None,
    intensity_reason: str | Sequence[str] | None,
    evidence_bundle_sha256: str | None,
    now: Callable[[], str],
) -> dict[str, object]:
    """Reproduce the no-explicit-intensity transition used by grant preview."""
    from .intensity_grant import matching_grant_request
    from .policy import conservative_legacy_policy, parse_intensity_request, resolve_policy
    from .verdict_store import _blockers, _read_input, _review_content_equal

    if runtime not in _RUNTIMES:
        raise m.SchemaError("RUNTIME_INVALID")
    if (
        not isinstance(round_number, int)
        or isinstance(round_number, bool)
        or round_number < 1
        or round_number > 3
    ):
        raise m.SchemaError(
            "ROUND_LIMIT_EXHAUSTED" if round_number == 4 else "ROUND_INVALID"
        )
    snapshot = capture_snapshot(root, base, now=now)
    decisions_sha256: str | None = None
    if round_number == 1:
        if decisions_path is not None:
            raise m.SchemaError("DECISIONS_NOT_ALLOWED")
        request = parse_intensity_request(
            intensity_values,
            requester=intensity_requester,
            reason=intensity_reason,
        )
        grant_request = matching_grant_request(
            review_fd,
            snapshot,
            lambda: resolve_policy(
                root,
                snapshot,
                runtime=runtime,
                request=parse_intensity_request(None, requester=None, reason=None),
            ),
        )
        if grant_request is not None:
            if (
                intensity_values
                or intensity_requester is not None
                or intensity_reason is not None
            ):
                raise m.SchemaError("INTENSITY_GRANT_CONFLICT")
            request = grant_request
    else:
        if (
            intensity_values
            or intensity_requester is not None
            or intensity_reason is not None
        ):
            raise m.SchemaError("INTENSITY_NOT_ALLOWED")
        if runtime != prior.producer_runtime:
            raise m.SchemaError("RUNTIME_CHANGED")
        if prior.schema == m.VERDICT_SCHEMA_VERSION:
            if prior.policy is None or prior.policy.mode is not m.ReviewMode.ITERATIVE:
                raise m.SchemaError("MODE_ROUND_INVALID")
        elif prior.schema != m.SCHEMA_VERSION:
            raise m.SchemaError("MODE_ROUND_INVALID")
        if prior.round == 3:
            raise m.SchemaError("ROUND_LIMIT_EXHAUSTED")
        if prior.gate.status is not m.GateStatus.FAIL or round_number != prior.round + 1:
            raise m.SchemaError("ROUND_TRANSITION_INVALID")
        if base != prior.base_ref:
            raise m.SchemaError("BASE_CHANGED")
        if decisions_path is None:
            raise m.SchemaError("DECISIONS_REQUIRED")
        request = (
            conservative_legacy_policy(runtime).request
            if prior.policy is None
            else prior.policy.request
        )
    policy = resolve_policy(root, snapshot, runtime=runtime, request=request)
    if round_number == 1 and _review_content_equal(root, prior, snapshot):
        if prior.gate.status is m.GateStatus.FAIL:
            if prior.round == 3:
                raise m.SchemaError("ROUND_LIMIT_EXHAUSTED")
            raise m.SchemaError("ROUND_TRANSITION_INVALID")
        prior_intensity = (
            prior.policy.effective_intensity if prior.policy is not None else 100
        )
        if (
            prior.gate.status is m.GateStatus.INCONCLUSIVE
            and policy.effective_intensity < prior_intensity
            and policy.request.source != m.HUMAN_GRANT_SOURCE
        ):
            raise m.SchemaError("ROUND_TRANSITION_INVALID")
    if round_number > 1:
        if policy.mode is not m.ReviewMode.ITERATIVE:
            raise m.SchemaError("MODE_ROUND_INVALID")
        if prior.policy is not None and (
            policy.config_sha256 != prior.policy.config_sha256
            or policy.request != prior.policy.request
            or policy.reviewers != prior.policy.reviewers
        ):
            raise m.SchemaError("POLICY_CHANGED")
        if (
            snapshot.repository != prior.repository
            or snapshot.base_ref != prior.base_ref
            or snapshot.base_sha != prior.base_sha
            or (prior.head_ref and snapshot.head_ref != prior.head_ref)
        ):
            raise m.SchemaError("REPOSITORY_OR_BASE_CHANGED")
        try:
            assert_auto_fix_scope(prior.initial_paths, snapshot.initial_paths)
        except GitStateError:
            raise m.SchemaError("AUTO_FIX_SCOPE_VIOLATION") from None
        raw = _read_input(
            root,
            review_fd,
            decisions_path,
            round_number=round_number - 1,
            basename="decisions.json",
            maximum=m.MAX_REPORT_BYTES,
            missing="DECISIONS_REQUIRED",
            path_code="DECISIONS_PATH_INVALID",
        )
        decisions = m.parse_decisions(raw, prior_blockers=_blockers(prior))
        if (
            any(item.disposition == "fixed" for item in decisions)
            and snapshot.head_sha == prior.head_sha
        ):
            raise m.SchemaError("FIXED_HEAD_UNCHANGED")
        decisions_sha256 = hashlib.sha256(raw).hexdigest()
    evidence_binding = None
    evidence_fallback_reason = None
    if policy.mode is not m.ReviewMode.OFF:
        from .evidence_lifecycle import select_evidence

        evidence_binding, evidence_fallback_reason = select_evidence(
            root, snapshot, evidence_bundle_sha256
        )
    return grant_binding(
        prior,
        prior_raw,
        snapshot=snapshot,
        runtime=runtime,
        round_number=round_number,
        policy=policy,
        decisions_sha256=decisions_sha256,
        evidence_bundle_sha256=evidence_bundle_sha256,
        evidence_binding=evidence_binding,
        evidence_fallback_reason=evidence_fallback_reason,
    )


def preview_grant_binding(
    cwd: Path,
    *,
    base: str,
    runtime: str,
    round_number: int,
    decisions_path: Path | None = None,
    intensity_values: Sequence[str] | None = None,
    intensity_requester: str | Sequence[str] | None = None,
    intensity_reason: str | Sequence[str] | None = None,
    evidence_bundle_sha256: str | None = None,
    now: Callable[[], str] = utc_now,
) -> dict[str, object]:
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        prior, prior_raw = _read_prior_locked(review_fd)
        return _target_binding_locked(
            root,
            review_fd,
            prior,
            prior_raw,
            base=base,
            runtime=runtime,
            round_number=round_number,
            decisions_path=decisions_path,
            intensity_values=intensity_values,
            intensity_requester=intensity_requester,
            intensity_reason=intensity_reason,
            evidence_bundle_sha256=evidence_bundle_sha256,
            now=now,
        )


def record_grant(
    cwd: Path,
    *,
    base: str,
    runtime: str,
    round_number: int,
    decisions_path: Path | None = None,
    intensity_values: Sequence[str] | None = None,
    intensity_requester: str | Sequence[str] | None = None,
    intensity_reason: str | Sequence[str] | None = None,
    evidence_bundle_sha256: str | None = None,
    expected_verdict_sha256: str,
    expected_binding_sha256: str,
    reason: str,
    channel: str,
    now: Callable[[], str] = utc_now,
) -> dict[str, object]:
    from .policy import MAX_REASON_BYTES, _bounded_text

    if (
        channel not in _CHANNELS
        or not isinstance(expected_verdict_sha256, str)
        or m._SHA256.fullmatch(expected_verdict_sha256) is None
        or not isinstance(expected_binding_sha256, str)
        or m._SHA256.fullmatch(expected_binding_sha256) is None
    ):
        raise m.SchemaError("RE_REVIEW_GRANT_INVALID")
    reason = _bounded_text(reason, MAX_REASON_BYTES, "RE_REVIEW_GRANT_INVALID")
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        prior, prior_raw = _read_prior_locked(review_fd)
        binding = _target_binding_locked(
            root,
            review_fd,
            prior,
            prior_raw,
            base=base,
            runtime=runtime,
            round_number=round_number,
            decisions_path=decisions_path,
            intensity_values=intensity_values,
            intensity_requester=intensity_requester,
            intensity_reason=intensity_reason,
            evidence_bundle_sha256=evidence_bundle_sha256,
            now=now,
        )
        if binding["verdict_sha256"] != expected_verdict_sha256:
            raise m.SchemaError("RE_REVIEW_VERDICT_CHANGED")
        if binding["binding_sha256"] != expected_binding_sha256:
            raise m.SchemaError("RE_REVIEW_BINDING_CHANGED")
        payload: dict[str, object] = {
            "schema": 2,
            "verdict_sha256": binding["verdict_sha256"],
            "binding_sha256": binding["binding_sha256"],
            "reason": reason,
            "channel": channel,
            "created_at": now(),
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        atomic_replace_bytes(
            review_fd,
            GRANT_NAME,
            raw,
            maximum=MAX_GRANT_BYTES,
            too_large="RE_REVIEW_GRANT_INVALID",
            unsafe="RE_REVIEW_GRANT_FILE_UNSAFE",
            exact_mode=0o600,
            write_failed="RE_REVIEW_GRANT_FILE_UNSAFE",
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
                missing="RE_REVIEW_GRANT_MISSING",
                unsafe="RE_REVIEW_GRANT_FILE_UNSAFE",
                exact_mode=0o600,
            )
        except m.SchemaError as error:
            if error.code == "RE_REVIEW_GRANT_MISSING":
                return False
            raise
        try:
            os.unlink(GRANT_NAME, dir_fd=review_fd)
            os.fsync(review_fd)
        except OSError:
            raise m.SchemaError("RE_REVIEW_GRANT_FILE_UNSAFE") from None
        return True


def consume_matching_grant_locked(
    review_fd: int,
    *,
    prior: m.Verdict,
    prior_raw: bytes,
    snapshot: m.Snapshot,
    runtime: str,
    round_number: int,
    policy: m.PolicyBinding,
    decisions_sha256: str | None,
    evidence_bundle_sha256: str | None,
    evidence_binding: m.EvidenceBinding | None,
    evidence_fallback_reason: str | None,
) -> bool:
    """Consume one exact grant while the caller holds the review lock."""
    expected = grant_binding(
        prior,
        prior_raw,
        snapshot=snapshot,
        runtime=runtime,
        round_number=round_number,
        policy=policy,
        decisions_sha256=decisions_sha256,
        evidence_bundle_sha256=evidence_bundle_sha256,
        evidence_binding=evidence_binding,
        evidence_fallback_reason=evidence_fallback_reason,
    )
    try:
        raw = read_named_file(
            review_fd,
            GRANT_NAME,
            maximum=MAX_GRANT_BYTES,
            missing="RE_REVIEW_GRANT_MISSING",
            unsafe="RE_REVIEW_GRANT_FILE_UNSAFE",
            exact_mode=0o600,
        )
    except m.SchemaError as error:
        if error.code == "RE_REVIEW_GRANT_MISSING":
            return False
        raise
    try:
        payload = json.loads(raw)
        reason = payload["reason"]
        channel = payload["channel"]
        created_at = payload["created_at"]
    except (ValueError, TypeError, KeyError, RecursionError, AttributeError):
        return False
    if (
        not isinstance(payload, dict)
        or set(payload) != {
            "schema", "verdict_sha256", "binding_sha256", "reason", "channel", "created_at"
        }
        or payload.get("schema") != 2
        or payload.get("verdict_sha256") != expected["verdict_sha256"]
        or payload.get("binding_sha256") != expected["binding_sha256"]
        or channel not in _CHANNELS
        or not isinstance(reason, str)
        or not isinstance(created_at, str)
    ):
        return False
    from .policy import MAX_REASON_BYTES, _bounded_text

    try:
        _bounded_text(reason, MAX_REASON_BYTES, "RE_REVIEW_GRANT_INVALID")
        m._text(created_at, 64)
    except m.SchemaError:
        return False
    try:
        os.unlink(GRANT_NAME, dir_fd=review_fd)
        os.fsync(review_fd)
    except OSError:
        raise m.SchemaError("RE_REVIEW_GRANT_FILE_UNSAFE") from None
    return True
