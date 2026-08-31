from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Mapping


class RecoveryDecision(Enum):
    DISCARD_PREPARED = "discard_prepared"
    FINALIZE_UPDATED = "finalize_updated"
    ALREADY_RECEIPTED = "already_receipted"


class RecoveryContradiction(ValueError):
    """Journal phase, raw state, and receipt evidence cannot all be true."""


@dataclass(frozen=True)
class RecoveryEvidence:
    phase: str
    current_digest: str
    expected_digest: str
    updated_digest: str
    has_matching_receipt: bool


def _canonical_json(value: object) -> bytes:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return rendered.encode("utf-8") + b"\n"


def derive_transition_id(
    record_kind: str,
    record_key: str,
    expected_digest: str,
    updated_digest: str,
    event_without_id: Mapping[str, object],
) -> str:
    event = dict(event_without_id)
    event.pop("event_id", None)
    payload = {
        "record_kind": record_kind,
        "record_key": record_key,
        "expected_digest": expected_digest,
        "updated_digest": updated_digest,
        "event": event,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def decide_recovery(evidence: RecoveryEvidence) -> RecoveryDecision:
    if evidence.phase not in {"prepared", "committed"}:
        raise RecoveryContradiction("unknown journal phase")
    if evidence.expected_digest == evidence.updated_digest:
        raise RecoveryContradiction("transition digests are equal")
    if evidence.current_digest == evidence.expected_digest:
        if evidence.phase == "prepared" and not evidence.has_matching_receipt:
            return RecoveryDecision.DISCARD_PREPARED
        raise RecoveryContradiction("expected state contradicts commit evidence")
    if evidence.current_digest != evidence.updated_digest:
        raise RecoveryContradiction("current state matches neither transition digest")
    if evidence.has_matching_receipt:
        return RecoveryDecision.ALREADY_RECEIPTED
    return RecoveryDecision.FINALIZE_UPDATED
