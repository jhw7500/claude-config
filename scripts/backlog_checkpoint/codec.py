"""Canonical codec for backlog-drainer checkpoint snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


MAX_SNAPSHOT_BYTES = 12 * 1024
MAX_SNAPSHOT_LINES = 15
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_JSON_NESTING = 64

_PREFIX = b"# Backlog checkpoint schema 1\n```json\n"
_SUFFIX = b"\n```\n"
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PHASES = {
    "SELECTED",
    "PREFLIGHT",
    "APPROACH_REVIEW",
    "IMPLEMENT",
    "INDEPENDENT_REVIEW",
    "VERIFY",
    "COMMIT",
    "READY_FOR_TRIBUNAL",
    "PAUSED",
}
_NEXT_ACTIONS = _PHASES | {"STOP"}


class CheckpointError(ValueError):
    """The checkpoint or its storage metadata violates the contract."""


class CheckpointIndeterminateError(CheckpointError):
    """A publish occurred but its durability could not be confirmed."""


def _validate_json_nesting(value: object) -> None:
    stack: list[tuple[object, int, bool]] = [(value, 1, False)]
    active: set[int] = set()
    while stack:
        item, depth, leaving = stack.pop()
        if not isinstance(item, (dict, list, tuple)):
            continue
        marker = id(item)
        if leaving:
            active.remove(marker)
            continue
        if depth > MAX_JSON_NESTING:
            raise CheckpointError("JSON nesting exceeds 64")
        if marker in active:
            raise CheckpointError("circular JSON value")
        active.add(marker)
        stack.append((item, depth, True))
        children = item.values() if isinstance(item, dict) else item
        for child in reversed(list(children)):
            stack.append((child, depth + 1, False))


def canonical_bytes(value: Any) -> bytes:
    """Return the declared canonical UTF-8 JSON representation."""
    _validate_json_nesting(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise CheckpointError("value is not canonical JSON") from exc


def sha256_bytes(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise CheckpointError("digest input must be bytes")
    return hashlib.sha256(data).hexdigest()


def supervisor_fingerprint(origin_adapter: str, session_id: str, host: str) -> str:
    for value in (origin_adapter, session_id, host):
        if not isinstance(value, str):
            raise CheckpointError("supervisor identity values must be strings")
    return sha256_bytes(canonical_bytes([origin_adapter, session_id, host]))


def _exact_keys(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CheckpointError(f"{label} has missing or unknown keys")
    if not all(isinstance(key, str) for key in value):
        raise CheckpointError(f"{label} keys must be strings")
    return value


def _string(value: object, label: str, max_bytes: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise CheckpointError(f"{label} must be a non-empty string")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise CheckpointError(f"{label} must be valid UTF-8") from exc
    if length > max_bytes:
        raise CheckpointError(f"{label} is too long")
    return value


def _integer(value: object, label: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise CheckpointError(f"{label} is outside its integer range")
    return value


def _enum(value: object, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise CheckpointError(f"{label} is not an allowed value")
    return value


def _hex(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise CheckpointError(f"{label} is not lowercase hexadecimal")
    return value


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _absolute_path(value: object, label: str) -> str:
    path = _string(value, label, 2048)
    if not path.startswith("/") or _has_control(path) or ".." in path.split("/"):
        raise CheckpointError(f"{label} is not a safe absolute path")
    return path


def _relative_ref(value: object, label: str) -> str:
    reference = _string(value, label, 128)
    try:
        reference.encode("ascii")
    except UnicodeError as exc:
        raise CheckpointError(f"{label} must be ASCII") from exc
    parts = reference.split("/")
    if (
        reference.startswith("/")
        or _has_control(reference)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise CheckpointError(f"{label} is not a safe relative reference")
    return reference


def _nullable_hex(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _hex(value, _HEX40, label)


def _validate_reference(
    value: object, *, max_size: int = MAX_MANIFEST_BYTES
) -> dict[str, Any]:
    reference = _exact_keys(value, {"ref", "sha256", "size"}, "evidence")
    ref = _relative_ref(reference["ref"], "evidence.ref")
    digest = _hex(reference["sha256"], _HEX64, "evidence.sha256")
    if ref != f"evidence/{digest}.json":
        raise CheckpointError("evidence ref must match its content digest")
    _integer(reference["size"], "evidence.size", 0, max_size)
    return reference


def validate_snapshot(value: object) -> dict:
    """Validate schema 1 without inferring any live authority."""
    snapshot = _exact_keys(
        value,
        {
            "schema",
            "run_id",
            "generation",
            "phase",
            "phase_status",
            "attempt",
            "next_action",
            "blockers",
            "supervisor_identity",
            "binding",
            "git",
            "child",
            "evidence",
        },
        "snapshot",
    )
    if type(snapshot["schema"]) is not int or snapshot["schema"] != 1:
        raise CheckpointError("schema must be integer 1")
    _hex(snapshot["run_id"], _HEX32, "run_id")
    _integer(snapshot["generation"], "generation", 0)
    _enum(snapshot["phase"], _PHASES, "phase")
    _enum(snapshot["phase_status"], {"started", "verified"}, "phase_status")
    _integer(snapshot["attempt"], "attempt", 0, 1)
    _enum(snapshot["next_action"], _NEXT_ACTIONS, "next_action")

    blockers = snapshot["blockers"]
    if not isinstance(blockers, list) or len(blockers) > 16:
        raise CheckpointError("blockers must be a list of at most 16 IDs")
    for index, blocker in enumerate(blockers):
        _string(blocker, f"blockers[{index}]", 128)

    identity = _exact_keys(
        snapshot["supervisor_identity"],
        {"runtime", "origin_adapter", "host", "session_fingerprint"},
        "supervisor_identity",
    )
    runtime = _enum(identity["runtime"], {"claude", "codex"}, "runtime")
    adapter = _enum(identity["origin_adapter"], {"claude", "codex"}, "origin_adapter")
    if runtime != adapter:
        raise CheckpointError("runtime and origin_adapter must match")
    _string(identity["host"], "host", 255)
    _hex(identity["session_fingerprint"], _HEX64, "session_fingerprint")

    binding = _exact_keys(
        snapshot["binding"],
        {"issue_url", "task_id", "claim_id", "repo_root", "worktree_root"},
        "binding",
    )
    _string(binding["issue_url"], "issue_url", 256)
    _string(binding["task_id"], "task_id", 128)
    _string(binding["claim_id"], "claim_id", 128)
    _absolute_path(binding["repo_root"], "repo_root")
    _absolute_path(binding["worktree_root"], "worktree_root")

    git = _exact_keys(
        snapshot["git"],
        {
            "branch",
            "base_head",
            "observed_head",
            "source_count",
            "path_set_digest",
            "change_digest",
            "commit_parent",
            "result_commit",
        },
        "git",
    )
    _string(git["branch"], "branch", 256)
    _hex(git["base_head"], _HEX40, "base_head")
    _hex(git["observed_head"], _HEX40, "observed_head")
    _integer(git["source_count"], "source_count", 0, 10_000)
    _hex(git["path_set_digest"], _HEX64, "path_set_digest")
    _hex(git["change_digest"], _HEX64, "change_digest")
    _nullable_hex(git["commit_parent"], "commit_parent")
    _nullable_hex(git["result_commit"], "result_commit")

    child = _exact_keys(
        snapshot["child"],
        {"child_id", "phase", "attempt", "observed_status"},
        "child",
    )
    if child["child_id"] is not None:
        _string(child["child_id"], "child_id", 256)
    _enum(child["phase"], _PHASES, "child.phase")
    _integer(child["attempt"], "child.attempt", 0, 1)
    _enum(
        child["observed_status"],
        {"running", "quiescent", "interrupted", "completed", "unknown"},
        "observed_status",
    )
    _validate_reference(snapshot["evidence"])
    return snapshot


def encode_snapshot(value: object) -> bytes:
    snapshot = validate_snapshot(value)
    encoded = _PREFIX + canonical_bytes(snapshot) + _SUFFIX
    if len(encoded) > MAX_SNAPSHOT_BYTES or len(encoded.splitlines()) > MAX_SNAPSHOT_LINES:
        raise CheckpointError("snapshot exceeds its size or line budget")
    return encoded


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CheckpointError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise CheckpointError(f"invalid JSON constant: {value}")


def decode_snapshot(data: bytes) -> dict:
    if not isinstance(data, bytes):
        raise CheckpointError("snapshot input must be bytes")
    if len(data) > MAX_SNAPSHOT_BYTES or len(data.splitlines()) > MAX_SNAPSHOT_LINES:
        raise CheckpointError("snapshot exceeds its size or line budget")
    if not data.startswith(_PREFIX) or not data.endswith(_SUFFIX):
        raise CheckpointError("snapshot Markdown envelope is invalid")
    record = data[len(_PREFIX) : -len(_SUFFIX)]
    if b"\n" in record or b"\r" in record:
        raise CheckpointError("snapshot must contain one JSON record")
    try:
        text = record.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except CheckpointError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise CheckpointError("snapshot JSON is invalid") from exc
    snapshot = validate_snapshot(value)
    if canonical_bytes(snapshot) != record:
        raise CheckpointError("snapshot JSON is not canonical")
    return snapshot
