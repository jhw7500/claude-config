"""Snapshot-bound tribunal risk, intensity, reviewer, and model policy."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Mapping, Sequence

from . import model
from .git_state import _command_output, _run_git


CONFIG_NAME = ".pre-pr-tribunal.toml"
MAX_CONFIG_BYTES = 64 * 1024
MAX_POLICY_REASONS = 128
MAX_REQUESTER_BYTES = 128
MAX_REASON_BYTES = 1024

_RUNTIME_MODELS = {
    "claude": frozenset(
        {
            "inherit",
            "haiku",
            "sonnet",
            "opus",
            "claude-haiku-4-5",
            "claude-sonnet-4-5",
            "claude-sonnet-4-6",
            "claude-opus-4-5",
            "claude-opus-4-6",
            "claude-opus-4-1",
        }
    ),
    "codex": frozenset(
        {
            "inherit",
            "gpt-5.5",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-6-astra",
        }
    ),
}
_MODE_INTENSITY = {
    model.ReviewMode.OFF: 0,
    model.ReviewMode.SINGLE: 50,
    model.ReviewMode.ITERATIVE: 100,
}
_SOURCE_SUFFIXES = frozenset(
    {
        ".bash",
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".lua",
        ".mjs",
        ".php",
        ".pl",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
        ".yaml",
        ".yml",
    }
)
_DOC_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".txt"})
_HIGH_PREFIXES = (
    ".github/workflows/",
    "auth/",
    "database/",
    "deploy/",
    "deployment/",
    "hooks/",
    "infra/",
    "migrations/",
    "scripts/install",
    "scripts/deploy",
)
_HIGH_BASENAMES = frozenset(
    {
        CONFIG_NAME,
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.mod",
        "go.sum",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "requirements.txt",
        "requirements-test.in",
        "requirements-test.lock",
        "uv.lock",
        "yarn.lock",
    }
)
_FAIL_CLOSED_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


@dataclass(frozen=True)
class _RepositoryConfig:
    digest: str
    policy: tuple[tuple[str, int], ...]
    reviewers: Mapping[str, Mapping[str, object]]


def _bounded_text(value: object, maximum: int, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise model.SchemaError(code)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise model.SchemaError(code) from None
    if (
        len(encoded) > maximum
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)
    ):
        raise model.SchemaError(code)
    return value


def intensity_mode(value: int) -> model.ReviewMode:
    if value == 0:
        return model.ReviewMode.OFF
    if 1 <= value <= 66:
        return model.ReviewMode.SINGLE
    if 67 <= value <= 100:
        return model.ReviewMode.ITERATIVE
    raise model.SchemaError("INTENSITY_INVALID")


def _request_fail_closed(reason: str) -> model.IntensityRequest:
    return model.IntensityRequest(
        100,
        "fail_closed",
        "system",
        "invalid-intensity-request",
        reason,
    )


def parse_intensity_request(
    values: Sequence[str] | None,
    *,
    requester: str | None,
    reason: str | None,
) -> model.IntensityRequest:
    if not values:
        if requester is not None or reason is not None:
            return _request_fail_closed("INTENSITY_ATTRIBUTION_WITHOUT_VALUE")
        return model.IntensityRequest(0, "default", "system", "policy-default")
    if len(values) != 1:
        return _request_fail_closed("INTENSITY_MULTIPLE")
    raw = values[0]
    if not isinstance(raw, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", raw) is None:
        return _request_fail_closed("INTENSITY_INVALID")
    value = int(raw)
    if value > 100:
        return _request_fail_closed("INTENSITY_OUT_OF_RANGE")
    try:
        bound_requester = _bounded_text(requester, MAX_REQUESTER_BYTES, "INTENSITY_INVALID")
        bound_reason = _bounded_text(reason, MAX_REASON_BYTES, "INTENSITY_INVALID")
    except model.SchemaError:
        return _request_fail_closed("INTENSITY_ATTRIBUTION_REQUIRED")
    return model.IntensityRequest(value, "cli", bound_requester, bound_reason)


def _strip_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in {'"', "'"}:
            quote = None if quote == character else character if quote is None else quote
            continue
        if character == "#" and quote is None:
            return line[:index]
    if quote is not None:
        raise model.SchemaError("CONFIG_INVALID")
    return line


def _split_unquoted(value: str, separator: str) -> tuple[str, str]:
    quote: str | None = None
    escaped = False
    depth = 0
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in {'"', "'"}:
            quote = None if quote == character else character if quote is None else quote
            continue
        if quote is None:
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth < 0:
                    break
            elif character == separator and depth == 0:
                return value[:index], value[index + 1 :]
    raise model.SchemaError("CONFIG_INVALID")


def _split_inline(value: str) -> list[str]:
    result: list[str] = []
    rest = value
    while rest.strip():
        try:
            left, right = _split_unquoted(rest, ",")
        except model.SchemaError:
            result.append(rest)
            break
        result.append(left)
        rest = right
    return result


def _parse_key(raw: str) -> str:
    value = raw.strip()
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise model.SchemaError("CONFIG_INVALID") from None
        return _bounded_text(parsed, model.MAX_COMMAND_TEXT_BYTES, "CONFIG_INVALID")
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return _bounded_text(value[1:-1], model.MAX_COMMAND_TEXT_BYTES, "CONFIG_INVALID")
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise model.SchemaError("CONFIG_INVALID")
    return value


def _parse_value(raw: str) -> object:
    value = raw.strip()
    if value in {"true", "false"}:
        return value == "true"
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
        try:
            return int(value)
        except ValueError:
            raise model.SchemaError("CONFIG_INVALID") from None
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            raise model.SchemaError("CONFIG_INVALID") from None
        return _bounded_text(parsed, model.MAX_COMMAND_TEXT_BYTES, "CONFIG_INVALID")
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return _bounded_text(value[1:-1], model.MAX_COMMAND_TEXT_BYTES, "CONFIG_INVALID")
    if value.startswith("{") and value.endswith("}"):
        result: dict[str, object] = {}
        for item in _split_inline(value[1:-1]):
            key_raw, item_raw = _split_unquoted(item, "=")
            key = _parse_key(key_raw)
            if key in result:
                raise model.SchemaError("CONFIG_INVALID")
            parsed = _parse_value(item_raw)
            if isinstance(parsed, (dict, bool, int)):
                raise model.SchemaError("CONFIG_INVALID")
            result[key] = parsed
        return result
    raise model.SchemaError("CONFIG_INVALID")


def _parse_toml(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_CONFIG_BYTES:
        raise model.SchemaError("CONFIG_TOO_LARGE")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise model.SchemaError("CONFIG_INVALID") from None
    if unicodedata.normalize("NFC", text) != text or "\x00" in text or "\r" in text:
        raise model.SchemaError("CONFIG_INVALID")
    root: dict[str, object] = {}
    current: dict[str, object] | None = None
    seen_tables: set[tuple[str, ...]] = set()
    for physical in text.split("\n"):
        line = _strip_comment(physical).strip()
        if not line:
            continue
        if line.startswith("["):
            if not line.endswith("]") or line.startswith("[["):
                raise model.SchemaError("CONFIG_INVALID")
            parts = tuple(_parse_key(part) for part in line[1:-1].split("."))
            if parts not in {
                ("policy",),
                ("reviewer", "A"),
                ("reviewer", "B"),
                ("reviewer", "C"),
            } or parts in seen_tables:
                raise model.SchemaError("CONFIG_INVALID")
            seen_tables.add(parts)
            target = root
            for part in parts:
                existing = target.get(part)
                if existing is None:
                    existing = {}
                    target[part] = existing
                if not isinstance(existing, dict):
                    raise model.SchemaError("CONFIG_INVALID")
                target = existing
            current = target
            continue
        if current is None:
            raise model.SchemaError("CONFIG_INVALID")
        key_raw, value_raw = _split_unquoted(line, "=")
        key = _parse_key(key_raw)
        if key in current:
            raise model.SchemaError("CONFIG_INVALID")
        current[key] = _parse_value(value_raw)
    return root


def _validate_pattern(value: object) -> str:
    pattern = _bounded_text(value, model.MAX_COMMAND_TEXT_BYTES, "CONFIG_INVALID")
    if (
        pattern.startswith(("/", "\\"))
        or "\\" in pattern
        or any(part in {"", ".", ".."} for part in pattern.split("/") if part != "**")
    ):
        raise model.SchemaError("CONFIG_INVALID")
    return pattern


def _configured_intensity(value: object) -> int:
    if isinstance(value, bool):
        raise model.SchemaError("CONFIG_INVALID")
    if isinstance(value, int):
        if 0 <= value <= 100:
            return value
        raise model.SchemaError("CONFIG_INVALID")
    if isinstance(value, str):
        try:
            return _MODE_INTENSITY[model.ReviewMode(value)]
        except (KeyError, ValueError):
            pass
    raise model.SchemaError("CONFIG_INVALID")


def _default_reviewers() -> dict[str, dict[str, object]]:
    return {
        "A": {
            "enabled": True,
            "model": {"claude": "opus", "codex": "gpt-5.6-sol"},
        },
        "B": {
            "enabled": True,
            "model": {"claude": "sonnet", "codex": "gpt-6-astra"},
        },
        "C": {"enabled": False, "model": {"claude": "inherit", "codex": "inherit"}},
    }


def _validate_config(raw: bytes) -> _RepositoryConfig:
    parsed = _parse_toml(raw) if raw else {}
    if set(parsed) - {"policy", "reviewer"}:
        raise model.SchemaError("CONFIG_INVALID")
    raw_policy = parsed.get("policy", {})
    if not isinstance(raw_policy, dict):
        raise model.SchemaError("CONFIG_INVALID")
    rules = tuple(
        (_validate_pattern(pattern), _configured_intensity(value))
        for pattern, value in raw_policy.items()
    )
    reviewers = _default_reviewers()
    raw_reviewers = parsed.get("reviewer", {})
    if not isinstance(raw_reviewers, dict) or set(raw_reviewers) - set("ABC"):
        raise model.SchemaError("CONFIG_INVALID")
    for key, value in raw_reviewers.items():
        if not isinstance(value, dict) or set(value) - {"enabled", "model"}:
            raise model.SchemaError("CONFIG_INVALID")
        enabled = value.get("enabled", reviewers[key]["enabled"])
        if not isinstance(enabled, bool):
            raise model.SchemaError("CONFIG_INVALID")
        raw_models = value.get("model", reviewers[key]["model"])
        if not isinstance(raw_models, dict) or set(raw_models) - set(_RUNTIME_MODELS):
            raise model.SchemaError("CONFIG_INVALID")
        models = dict(reviewers[key]["model"])
        for runtime, name in raw_models.items():
            if not isinstance(name, str) or name not in _RUNTIME_MODELS[runtime]:
                raise model.SchemaError("MODEL_UNKNOWN")
            models[runtime] = name
        reviewers[key] = {"enabled": enabled, "model": models}
    if not any(bool(value["enabled"]) for value in reviewers.values()):
        raise model.SchemaError("CONFIG_INVALID")
    return _RepositoryConfig(hashlib.sha256(raw).hexdigest(), rules, reviewers)


def _committed_config(root: Path, head_sha: str) -> _RepositoryConfig:
    tree = _run_git(root, "ls-tree", "-z", head_sha, "--", CONFIG_NAME)
    if tree.returncode != 0 or tree.stderr:
        raise model.SchemaError("CONFIG_INVALID")
    if not tree.stdout:
        return _validate_config(b"")
    fields = tree.stdout[:-1].split(b"\x00") if tree.stdout.endswith(b"\x00") else []
    if len(fields) != 1:
        raise model.SchemaError("CONFIG_INVALID")
    try:
        metadata, path = fields[0].split(b"\t", 1)
        mode, object_type, _digest = metadata.split(b" ", 2)
    except ValueError:
        raise model.SchemaError("CONFIG_INVALID") from None
    if path != CONFIG_NAME.encode() or mode not in {b"100644", b"100755"} or object_type != b"blob":
        raise model.SchemaError("CONFIG_INVALID")
    raw = _command_output(root, ("show", f"{head_sha}:{CONFIG_NAME}"), failure="CONFIG_INVALID")
    return _validate_config(raw)


def _pattern_matches(pattern: str, path: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
    )


def _pattern_floor(rules: Sequence[tuple[str, int]], path: str) -> tuple[int, str | None]:
    matches = [
        (sum(character not in "*?[]" for character in pattern), -index, value, pattern)
        for index, (pattern, value) in enumerate(rules)
        if _pattern_matches(pattern, path)
    ]
    if not matches:
        return 0, None
    _specificity, _order, value, pattern = max(matches)
    return value, pattern


def _path_floor(path: str) -> tuple[int, str]:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    suffix = Path(lower).suffix
    if lower == CONFIG_NAME or name in _HIGH_BASENAMES:
        return 100, f"sensitive-file:{path}"
    if name.startswith(("requirements", "dockerfile")):
        return 100, f"dependency-or-deploy:{path}"
    if any(lower.startswith(prefix) for prefix in _HIGH_PREFIXES):
        return 100, f"sensitive-path:{path}"
    if lower.startswith(("docs/", "doc/")) and suffix in _DOC_SUFFIXES:
        return 0, f"documentation:{path}"
    if "/" not in lower and suffix in _DOC_SUFFIXES:
        return 0, f"documentation:{path}"
    if suffix in _SOURCE_SUFFIXES:
        return 50, f"source:{path}"
    return 100, f"unknown-path:{path}"


def _unsafe_git_kinds(root: Path, snapshot: model.Snapshot) -> tuple[bool, bool]:
    revision = f"{snapshot.merge_base_sha}..{snapshot.head_sha}"
    raw = _command_output(
        root,
        ("diff", "--raw", "-z", "--full-index", "--no-renames", revision),
    )
    items = raw[:-1].split(b"\x00") if raw.endswith(b"\x00") else []
    if len(items) % 2:
        raise model.SchemaError("POLICY_GIT_METADATA_INVALID")
    special = False
    for index in range(0, len(items), 2):
        header = items[index]
        try:
            metadata, _path = header.split(b"\t", 1)
        except ValueError:
            metadata, _path = header, items[index + 1]
        parts = metadata.split()
        if len(parts) != 5 or not parts[0].startswith(b":"):
            raise model.SchemaError("POLICY_GIT_METADATA_INVALID")
        old_mode = parts[0][1:]
        new_mode = parts[1]
        if old_mode in {b"120000", b"160000"} or new_mode in {b"120000", b"160000"}:
            special = True
    numstat = _command_output(root, ("diff", "--numstat", "-z", "--no-renames", revision))
    binary = False
    for record in numstat[:-1].split(b"\x00") if numstat.endswith(b"\x00") else ():
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            raise model.SchemaError("POLICY_GIT_METADATA_INVALID")
        if fields[:2] == [b"-", b"-"]:
            binary = True
    return special, binary


def resolve_policy(
    root: Path,
    snapshot: model.Snapshot,
    *,
    runtime: str,
    request: model.IntensityRequest,
) -> model.PolicyBinding:
    if runtime not in _RUNTIME_MODELS or not isinstance(request, model.IntensityRequest):
        raise model.SchemaError("POLICY_INVALID")
    config = _committed_config(root, snapshot.head_sha)
    floors: list[int] = []
    reasons: list[str] = []
    for changed in snapshot.paths:
        if changed.status.startswith(("R", "C")) or changed.status in {"T", "U", "X", "B"}:
            floors.append(100)
            reasons.append(f"unsafe-status:{changed.status}")
        for path in (changed.path, changed.old_path):
            if path is None:
                continue
            built_in, built_in_reason = _path_floor(path)
            configured, pattern = _pattern_floor(config.policy, path)
            floor = max(built_in, configured)
            floors.append(floor)
            reasons.append(built_in_reason)
            if pattern is not None and configured >= built_in:
                reasons.append(f"config:{pattern}:{configured}")
    if not floors:
        raise model.SchemaError("POLICY_INVALID")
    if len(set(floors)) > 1:
        floors.append(100)
        reasons.append("mixed-risk-change")
    special, binary = _unsafe_git_kinds(root, snapshot)
    if special:
        floors.append(100)
        reasons.append("symlink-or-submodule")
    if binary:
        floors.append(100)
        reasons.append("binary-change")
    risk_floor = max(floors)
    effective = max(risk_floor, request.value)
    reviewers = {
        key: model.ReviewerPolicy(
            enabled=(True if request.fail_closed_reason is not None else bool(value["enabled"])),
            model=str(value["model"][runtime]),
        )
        for key, value in config.reviewers.items()
    }
    unique_reasons = tuple(dict.fromkeys(reasons))
    if len(unique_reasons) > MAX_POLICY_REASONS:
        unique_reasons = (*unique_reasons[: MAX_POLICY_REASONS - 1], "reason-limit")
    return model.PolicyBinding(
        config.digest,
        risk_floor,
        effective,
        intensity_mode(effective),
        unique_reasons,
        request,
        reviewers,
    )


def conservative_legacy_policy(runtime: str) -> model.PolicyBinding:
    request = _request_fail_closed("LEGACY_MIGRATION")
    reviewers = {
        key: model.ReviewerPolicy(True, "inherit") for key in "ABC"
    }
    return model.PolicyBinding(
        hashlib.sha256(b"").hexdigest(),
        100,
        100,
        model.ReviewMode.ITERATIVE,
        ("legacy-migration",),
        request,
        reviewers,
    )


def parse_policy_binding(value: object, *, runtime: str) -> model.PolicyBinding:
    obj = model._object(
        value,
        {
            "config_sha256",
            "risk_floor",
            "effective_intensity",
            "mode",
            "reasons",
            "request",
            "reviewers",
        },
        "POLICY_INVALID",
    )
    digest = obj["config_sha256"]
    if not isinstance(digest, str) or model._SHA256.fullmatch(digest) is None:
        raise model.SchemaError("POLICY_INVALID")
    floor = model._integer(obj["risk_floor"], "POLICY_INVALID", minimum=0, maximum=100)
    effective = model._integer(
        obj["effective_intensity"], "POLICY_INVALID", minimum=0, maximum=100
    )
    try:
        mode = model.ReviewMode(obj["mode"])
    except (TypeError, ValueError):
        raise model.SchemaError("POLICY_INVALID") from None
    reasons = tuple(
        _bounded_text(item, model.MAX_COMMAND_TEXT_BYTES, "POLICY_INVALID")
        for item in model._array(obj["reasons"], MAX_POLICY_REASONS, "POLICY_INVALID")
    )
    if not reasons or len(reasons) != len(set(reasons)):
        raise model.SchemaError("POLICY_INVALID")
    request_obj = model._object(
        obj["request"],
        {"value", "source", "requester", "reason", "fail_closed_reason"},
        "POLICY_INVALID",
    )
    request_value = model._integer(
        request_obj["value"], "POLICY_INVALID", minimum=0, maximum=100
    )
    source = request_obj["source"]
    if source not in {"default", "cli", "fail_closed"}:
        raise model.SchemaError("POLICY_INVALID")
    requester = _bounded_text(request_obj["requester"], MAX_REQUESTER_BYTES, "POLICY_INVALID")
    request_reason = _bounded_text(request_obj["reason"], MAX_REASON_BYTES, "POLICY_INVALID")
    fail_closed = request_obj["fail_closed_reason"]
    if fail_closed is not None and (
        not isinstance(fail_closed, str) or _FAIL_CLOSED_REASON.fullmatch(fail_closed) is None
    ):
        raise model.SchemaError("POLICY_INVALID")
    if (source == "fail_closed") != (fail_closed is not None) or (
        fail_closed is not None and request_value != 100
    ):
        raise model.SchemaError("POLICY_INVALID")
    if source == "default" and (
        request_value != 0
        or requester != "system"
        or request_reason != "policy-default"
    ):
        raise model.SchemaError("POLICY_INVALID")
    if source == "fail_closed" and (
        requester != "system" or request_reason != "invalid-intensity-request"
    ):
        raise model.SchemaError("POLICY_INVALID")
    request = model.IntensityRequest(
        request_value, source, requester, request_reason, fail_closed
    )
    raw_reviewers = model._object(obj["reviewers"], {"A", "B", "C"}, "POLICY_INVALID")
    reviewers: dict[str, model.ReviewerPolicy] = {}
    for key in "ABC":
        reviewer = model._object(
            raw_reviewers[key], {"enabled", "model"}, "POLICY_INVALID"
        )
        enabled = reviewer["enabled"]
        selected_model = reviewer["model"]
        if (
            not isinstance(enabled, bool)
            or not isinstance(selected_model, str)
            or runtime not in _RUNTIME_MODELS
            or selected_model not in _RUNTIME_MODELS[runtime]
        ):
            raise model.SchemaError("POLICY_INVALID")
        reviewers[key] = model.ReviewerPolicy(enabled, selected_model)
    binding = model.PolicyBinding(
        digest, floor, effective, mode, reasons, request, reviewers
    )
    binding.to_json()
    if (
        effective != max(floor, request_value)
        or mode is not intensity_mode(effective)
        or (mode is not model.ReviewMode.OFF and not binding.active_reviewers)
        or (fail_closed is not None and set(binding.active_reviewers) != set("ABC"))
    ):
        raise model.SchemaError("POLICY_INVALID")
    return binding
