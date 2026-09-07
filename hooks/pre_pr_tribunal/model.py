"""Strict, bounded data model for the pre-PR tribunal."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Mapping, Sequence
import unicodedata
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
MAX_VERDICT_BYTES = 256 * 1024
MAX_REPORT_BYTES = 128 * 1024
MAX_EVIDENCE_TEXT_BYTES = 8 * 1024
MAX_COMMAND_TEXT_BYTES = 4 * 1024
MAX_FINDINGS_PER_REVIEWER = 128
MAX_INITIAL_PATHS = MAX_FINDINGS_PER_REVIEWER * 8
MAX_EXECUTIONS_PER_REVIEWER = 128

_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FINDING_ID = re.compile(r"([ABC])-R([1-3])-([0-9]{3})\Z")
_EXECUTION_ID = re.compile(r"([ABC])-R([1-3])-E([0-9]{3})\Z")
_DECISION_EXECUTION_ID = re.compile(r"D-R([1-3])-E([0-9]{3})\Z")
_DECISION_ID = re.compile(r"D-R([1-3])-([ABC])-([0-9]{3})\Z")
_CLAIM_ID = re.compile(r"([ABC])-R([1-3])-C([0-9]{3})\Z")
_SECRET = re.compile(
    r"BEGIN PRIVATE KEY|ghp_[A-Za-z0-9]{8}|github_pat_[A-Za-z0-9_]{8}|"
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8}|AKIA[A-Z0-9]{12}"
)
_HOME_PATH = re.compile(r"/(?:home|Users)/[^/\s?#'\"<>]+")
_HTTP_URL_START = re.compile(r"https?://", re.IGNORECASE)
_SHELL_CONTROL = frozenset(";|&()<>`")
_MAX_SHELL_NESTING = 64


class TribunalError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SchemaError(TribunalError):
    pass


class Reviewer(str, Enum):
    A = "A"
    B = "B"
    C = "C"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class GateStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class ChangedPath:
    status: str
    path: str
    old_path: str | None = None

    def to_json(self) -> dict[str, object]:
        value: dict[str, object] = {"status": self.status, "path": self.path}
        if self.old_path is not None:
            value["old_path"] = self.old_path
        return value


@dataclass(frozen=True)
class Snapshot:
    schema: int
    repository: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    merge_base_sha: str
    diff_sha256: str
    paths: Sequence[ChangedPath]
    initial_paths: Sequence[str]
    created_at: str

    def to_json(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "repository": self.repository,
            "base": {"ref": self.base_ref, "sha": self.base_sha},
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "merge_base_sha": self.merge_base_sha,
            "diff_sha256": self.diff_sha256,
            "paths": [item.to_json() for item in self.paths],
            "initial_paths": list(self.initial_paths),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Execution:
    id: str
    command: str
    exit_code: int
    stdout_excerpt: str
    stderr_excerpt: str
    capture_sha256: str
    truncated: bool

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "capture_sha256": self.capture_sha256,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class Finding:
    id: str
    reviewer: Reviewer
    severity: Severity
    title: str
    rationale: str
    path: str
    line: int | None
    execution_ids: Sequence[str]
    acceptance_condition: str

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "reviewer": self.reviewer.value,
            "severity": self.severity.value,
            "title": self.title,
            "rationale": self.rationale,
            "path": self.path,
            "line": self.line,
            "execution_ids": list(self.execution_ids),
            "acceptance_condition": self.acceptance_condition,
        }


@dataclass(frozen=True)
class Claim:
    id: str
    statement: str
    result: str
    execution_ids: Sequence[str]
    reason: str

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "statement": self.statement,
            "result": self.result,
            "execution_ids": list(self.execution_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PriorDecisionResponse:
    decision_id: str
    outcome: str
    replacement_finding_id: str | None

    def to_json(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "outcome": self.outcome,
            "replacement_finding_id": self.replacement_finding_id,
        }


@dataclass(frozen=True)
class ReviewerReport:
    reviewer: Reviewer
    round: int
    head_sha: str
    diff_sha256: str
    findings: Sequence[Finding]
    executions: Sequence[Execution]
    claims: Sequence[Claim]
    prior_decisions: Sequence[PriorDecisionResponse]

    def to_json(self) -> dict[str, object]:
        return {
            "status": "complete",
            "findings": [item.to_json() for item in self.findings],
            "executions": [item.to_json() for item in self.executions],
            "claims": [item.to_json() for item in self.claims],
            "prior_decisions": [item.to_json() for item in self.prior_decisions],
        }


@dataclass(frozen=True)
class Decision:
    id: str
    finding_round: int
    finding_id: str
    reviewer: Reviewer
    disposition: str
    rationale: str
    executions: Sequence[Execution]

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "finding_ref": {
                "round": self.finding_round,
                "id": self.finding_id,
                "reviewer": self.reviewer.value,
            },
            "disposition": self.disposition,
            "rationale": self.rationale,
            "executions": [item.to_json() for item in self.executions],
        }


@dataclass(frozen=True)
class ReviewerSlot:
    status: str
    report: ReviewerReport | None = None

    def to_json(self) -> dict[str, object]:
        if self.status == "pending":
            return {"status": "pending"}
        if self.report is None:
            raise SchemaError("VERDICT_INVALID")
        return self.report.to_json()


@dataclass(frozen=True)
class RoundSummary:
    round: int
    head_sha: str
    diff_sha256: str
    blocking_findings: Sequence[Mapping[str, str]]
    decision_outcomes: Sequence[Mapping[str, object]]

    def to_json(self) -> dict[str, object]:
        return {
            "round": self.round,
            "head_sha": self.head_sha,
            "diff_sha256": self.diff_sha256,
            "blocking_findings": [dict(item) for item in self.blocking_findings],
            "decision_outcomes": [dict(item) for item in self.decision_outcomes],
        }


@dataclass(frozen=True)
class GateSummary:
    status: GateStatus
    blocking_count: int

    def to_json(self) -> dict[str, object]:
        return {"status": self.status.value, "blocking_count": self.blocking_count}


@dataclass(frozen=True)
class Verdict:
    schema: int
    repository: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    merge_base_sha: str
    diff_sha256: str
    initial_paths: Sequence[str]
    round: int
    producer_runtime: str
    reviewers: Mapping[str, ReviewerSlot]
    decisions: Sequence[Decision]
    history: Sequence[RoundSummary]
    gate: GateSummary
    created_at: str

    @property
    def snapshot(self) -> Snapshot:
        return Snapshot(
            self.schema,
            self.repository,
            self.base_ref,
            self.base_sha,
            self.head_ref,
            self.head_sha,
            self.merge_base_sha,
            self.diff_sha256,
            (),
            self.initial_paths,
            self.created_at,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "repository": self.repository,
            "base": {"ref": self.base_ref, "sha": self.base_sha},
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "merge_base_sha": self.merge_base_sha,
            "diff_sha256": self.diff_sha256,
            "initial_paths": list(self.initial_paths),
            "round": self.round,
            "producer_runtime": self.producer_runtime,
            "reviewers": {key: self.reviewers[key].to_json() for key in "ABC"},
            "decisions": [item.to_json() for item in self.decisions],
            "history": [item.to_json() for item in self.history],
            "gate": self.gate.to_json(),
            "created_at": self.created_at,
        }


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError("JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _load_json(raw: bytes, *, limit: int, too_large: str) -> object:
    if not isinstance(raw, bytes):
        raise SchemaError("JSON_INVALID")
    if len(raw) > limit:
        raise SchemaError(too_large)
    try:
        return json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                SchemaError("JSON_INVALID")
            ),
        )
    except SchemaError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise SchemaError("JSON_INVALID") from None


def _object(value: object, keys: set[str], code: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise SchemaError(code)
    return value


def _array(value: object, maximum: int, code: str) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise SchemaError(code)
    return value


def _text(value: object, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise SchemaError("TEXT_INVALID")
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeEncodeError:
        raise SchemaError("TEXT_INVALID") from None
    if size > maximum:
        raise SchemaError("TEXT_TOO_LARGE")
    if unicodedata.normalize("NFC", value) != value or any(
        unicodedata.category(character) in {"Cc", "Cs"} for character in value
    ):
        raise SchemaError("TEXT_INVALID")
    return value


def _head_ref(value: object) -> str:
    ref = _text(value, 1024)
    prefix = "refs/heads/"
    if not ref.startswith(prefix):
        raise SchemaError("VERDICT_INVALID")
    branch = ref[len(prefix) :]
    components = branch.split("/")
    if (
        not branch
        or branch == "@"
        or branch.endswith(".")
        or ".." in branch
        or "@{" in branch
        or any(character in branch for character in " ~^:?*[\\")
        or any(
            not component
            or component.startswith(".")
            or component.endswith(".lock")
            for component in components
        )
    ):
        raise SchemaError("VERDICT_INVALID")
    return ref


@dataclass
class _ShellFrame:
    closer: str | None = None
    quote: str | None = None
    interpolated: bool = False
    parenthesis_depth: int = 0


def _shell_quote_context(
    text: str, end: int
) -> tuple[str | None, bool, int, bool] | None:
    frames = [_ShellFrame()]
    index = 0
    while index < end:
        frame = frames[-1]
        character = text[index]
        if frame.quote == "'":
            if character == "'":
                frame.quote = None
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if frame.quote == '"':
            if character == '"':
                frame.quote = None
                frame.interpolated = False
            elif character == "`":
                frame.interpolated = True
                if len(frames) >= _MAX_SHELL_NESTING:
                    return None
                frames.append(_ShellFrame(closer="`"))
                index += 1
                continue
            elif character == "$":
                frame.interpolated = True
                if index + 1 < end and text[index + 1] == "(":
                    if len(frames) >= _MAX_SHELL_NESTING:
                        return None
                    frames.append(_ShellFrame(closer=")"))
                    index += 2
                    continue
            index += 1
            continue
        if character == "`":
            if frame.closer == "`":
                frames.pop()
            else:
                if len(frames) >= _MAX_SHELL_NESTING:
                    return None
                frames.append(_ShellFrame(closer="`"))
            index += 1
            continue
        if character == "$" and index + 1 < end and text[index + 1] == "(":
            if len(frames) >= _MAX_SHELL_NESTING:
                return None
            frames.append(_ShellFrame(closer=")"))
            index += 2
            continue
        if frame.closer == ")" and character == "(":
            frame.parenthesis_depth += 1
            index += 1
            continue
        if frame.closer == ")" and character == ")":
            if frame.parenthesis_depth:
                frame.parenthesis_depth -= 1
            else:
                frames.pop()
            index += 1
            continue
        if character in {"'", '"'}:
            frame.quote = character
            frame.interpolated = False
        index += 1
    frame = frames[-1]
    return (
        frame.quote,
        frame.interpolated,
        len(frames) - 1,
        any(item.closer == "`" for item in frames),
    )


def _matching_url_terminator(text: str, start: int) -> str | None:
    if start and text[start - 1] == "[":
        return "]"
    if start < 3 or text[start - 1] != "(" or text[start - 2] != "]":
        return None
    label_start = text.rfind("[", 0, start - 2)
    if label_start < 0 or "\n" in text[label_start : start - 2]:
        return None
    return ")"


def _http_url_candidate(text: str, start: int) -> tuple[str, int, str | None] | None:
    context = _shell_quote_context(text, start)
    if context is None:
        return None
    quote, interpolated, substitutions, backtick = context
    if interpolated or substitutions or backtick:
        return None
    terminator = _matching_url_terminator(text, start)
    if start:
        previous = text[start - 1]
        if not (
            previous.isspace()
            or previous in {"=", "'", '"', "["}
            or previous in _SHELL_CONTROL
        ):
            return None
    index = start
    if quote is None:
        terminated = terminator is None
        while index < len(text):
            character = text[index]
            if character == terminator:
                terminated = True
                break
            if (
                character.isspace()
                or character in _SHELL_CONTROL
                or character in {"'", '"', "$", "\\"}
            ):
                break
            index += 1
        if not terminated:
            return None
    else:
        token_end: int | None = None
        quote_closed = False
        while index < len(text):
            character = text[index]
            if quote == '"' and character in {"$", "`"}:
                return None
            if character == quote:
                quote_closed = True
                break
            if character == terminator and token_end is None:
                token_end = index
            if quote == '"' and character == "\\":
                index += 2
                continue
            index += 1
        if not quote_closed or terminator is not None and token_end is None:
            return None
        if token_end is not None:
            index = token_end
    return text[start:index], index, terminator


def _http_url_path_spans(text: str) -> tuple[tuple[int, int, str | None], ...]:
    spans: list[tuple[int, int, str | None]] = []
    for match in _HTTP_URL_START.finditer(text):
        start = match.start()
        candidate = _http_url_candidate(text, start)
        if candidate is None:
            continue
        token, _end, terminator = candidate
        try:
            parsed = urlsplit(token)
            hostname = parsed.hostname
            parsed.port
        except ValueError:
            continue
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            continue
        path_start = start + len(parsed.scheme) + 3 + len(parsed.netloc)
        spans.append((path_start, path_start + len(parsed.path), terminator))
    return tuple(spans)


def _contains_home_path(text: str) -> bool:
    url_paths = _http_url_path_spans(text)
    for match in _HOME_PATH.finditer(text):
        if any(
            start <= match.start()
            and (
                match.end() <= end
                or terminator is not None
                and match.end() == end + 1
                and text[end] == terminator
            )
            for start, end, terminator in url_paths
        ):
            continue
        return True
    return False


def _evidence(value: object) -> str:
    text = _text(value, MAX_EVIDENCE_TEXT_BYTES, allow_empty=True)
    if _SECRET.search(text) or _contains_home_path(text):
        raise SchemaError("EVIDENCE_SECRET_DETECTED")
    return text


def _command(value: object) -> str:
    text = _text(value, MAX_COMMAND_TEXT_BYTES)
    if _SECRET.search(text) or _contains_home_path(text):
        raise SchemaError("EVIDENCE_SECRET_DETECTED")
    return text


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        raise SchemaError("PATH_INVALID")
    _text(value, MAX_COMMAND_TEXT_BYTES)
    if "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise SchemaError("PATH_INVALID")
    return value


def _enum(enum_type, value: object, code: str):
    if not isinstance(value, str):
        raise SchemaError(code)
    try:
        return enum_type(value)
    except ValueError:
        raise SchemaError(code) from None


def _integer(
    value: object, code: str, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SchemaError(code)
    if (
        minimum is not None
        and value < minimum
        or maximum is not None
        and value > maximum
    ):
        raise SchemaError(code)
    return value


def _parse_execution(
    value: object, *, reviewer: Reviewer | None, round_number: int
) -> Execution:
    obj = _object(
        value,
        {
            "id",
            "command",
            "exit_code",
            "stdout_excerpt",
            "stderr_excerpt",
            "capture_sha256",
            "truncated",
        },
        "EXECUTION_SCHEMA_INVALID",
    )
    identifier = obj["id"]
    pattern = _DECISION_EXECUTION_ID if reviewer is None else _EXECUTION_ID
    if (
        not isinstance(identifier, str)
        or (match := pattern.fullmatch(identifier)) is None
    ):
        raise SchemaError("EXECUTION_ID_INVALID")
    if reviewer is None:
        matched_round = int(match.group(1))
    else:
        if match.group(1) != reviewer.value:
            raise SchemaError("EXECUTION_ID_INVALID")
        matched_round = int(match.group(2))
    if matched_round != round_number:
        raise SchemaError("EXECUTION_ID_INVALID")
    command = _command(obj["command"])
    exit_code = _integer(
        obj["exit_code"], "EXECUTION_SCHEMA_INVALID", minimum=-255, maximum=255
    )
    stdout = _evidence(obj["stdout_excerpt"])
    stderr = _evidence(obj["stderr_excerpt"])
    capture = obj["capture_sha256"]
    if not isinstance(capture, str) or _SHA256.fullmatch(capture) is None:
        raise SchemaError("EXECUTION_SCHEMA_INVALID")
    truncated = obj["truncated"]
    if not isinstance(truncated, bool):
        raise SchemaError("EXECUTION_SCHEMA_INVALID")
    return Execution(identifier, command, exit_code, stdout, stderr, capture, truncated)


def _parse_finding(value: object, *, reviewer: Reviewer, round_number: int) -> Finding:
    obj = _object(
        value,
        {
            "id",
            "reviewer",
            "severity",
            "title",
            "rationale",
            "path",
            "line",
            "execution_ids",
            "acceptance_condition",
        },
        "FINDING_SCHEMA_INVALID",
    )
    identifier = obj["id"]
    if (
        not isinstance(identifier, str)
        or (match := _FINDING_ID.fullmatch(identifier)) is None
    ):
        raise SchemaError("FINDING_ID_INVALID")
    if match.group(1) != reviewer.value or obj["reviewer"] != reviewer.value:
        raise SchemaError("FINDING_REVIEWER_MISMATCH")
    if int(match.group(2)) != round_number:
        raise SchemaError("FINDING_ID_INVALID")
    severity = _enum(Severity, obj["severity"], "FINDING_SCHEMA_INVALID")
    title = _text(obj["title"], MAX_EVIDENCE_TEXT_BYTES)
    rationale = _text(obj["rationale"], MAX_EVIDENCE_TEXT_BYTES)
    path = _path(obj["path"])
    line_value = obj["line"]
    if line_value is not None:
        line_value = _integer(
            line_value, "FINDING_SCHEMA_INVALID", minimum=1, maximum=2**31 - 1
        )
    refs_raw = _array(
        obj["execution_ids"], MAX_EXECUTIONS_PER_REVIEWER, "FINDING_SCHEMA_INVALID"
    )
    refs: list[str] = []
    for ref in refs_raw:
        if (
            not isinstance(ref, str)
            or _EXECUTION_ID.fullmatch(ref) is None
            or ref in refs
        ):
            raise SchemaError("EXECUTION_REFERENCE_INVALID")
        refs.append(ref)
    acceptance = _text(obj["acceptance_condition"], MAX_EVIDENCE_TEXT_BYTES)
    return Finding(
        identifier,
        reviewer,
        severity,
        title,
        rationale,
        path,
        line_value,
        tuple(refs),
        acceptance,
    )


def _parse_claim(
    value: object, *, reviewer: Reviewer, round_number: int, execution_ids: set[str]
) -> Claim:
    obj = _object(
        value,
        {"id", "statement", "result", "execution_ids", "reason"},
        "CLAIM_SCHEMA_INVALID",
    )
    identifier = obj["id"]
    if (
        not isinstance(identifier, str)
        or (match := _CLAIM_ID.fullmatch(identifier)) is None
        or match.group(1) != reviewer.value
        or int(match.group(2)) != round_number
    ):
        raise SchemaError("CLAIM_SCHEMA_INVALID")
    statement = _text(obj["statement"], MAX_EVIDENCE_TEXT_BYTES)
    result = obj["result"]
    if not isinstance(result, str) or result not in {
        "supported",
        "refuted",
        "unverified",
    }:
        raise SchemaError("CLAIM_SCHEMA_INVALID")
    refs_raw = _array(
        obj["execution_ids"], MAX_EXECUTIONS_PER_REVIEWER, "CLAIM_SCHEMA_INVALID"
    )
    if (
        any(not isinstance(ref, str) for ref in refs_raw)
        or len(set(refs_raw)) != len(refs_raw)
        or not set(refs_raw).issubset(execution_ids)
    ):
        raise SchemaError("EXECUTION_REFERENCE_INVALID")
    reason = _text(obj["reason"], MAX_EVIDENCE_TEXT_BYTES, allow_empty=True)
    if result in {"supported", "refuted"} and not refs_raw:
        raise SchemaError("CLAIM_EVIDENCE_REQUIRED")
    if result == "unverified" and (not reason or refs_raw):
        raise SchemaError("CLAIM_SCHEMA_INVALID")
    return Claim(identifier, statement, result, tuple(refs_raw), reason)


def _parse_prior_response(value: object) -> PriorDecisionResponse:
    obj = _object(
        value,
        {"decision_id", "outcome", "replacement_finding_id"},
        "PRIOR_DECISION_RESPONSE_INVALID",
    )
    decision_id = obj["decision_id"]
    if not isinstance(decision_id, str) or _DECISION_ID.fullmatch(decision_id) is None:
        raise SchemaError("PRIOR_DECISION_RESPONSE_INVALID")
    outcome = obj["outcome"]
    replacement = obj["replacement_finding_id"]
    if not isinstance(outcome, str) or outcome not in {"accepted", "reissued"}:
        raise SchemaError("PRIOR_DECISION_RESPONSE_INVALID")
    if replacement is not None and (
        not isinstance(replacement, str) or _FINDING_ID.fullmatch(replacement) is None
    ):
        raise SchemaError("PRIOR_DECISION_RESPONSE_INVALID")
    if outcome == "accepted" and replacement is not None:
        raise SchemaError("PRIOR_DECISION_RESPONSE_INVALID")
    if outcome == "reissued" and replacement is None:
        raise SchemaError("REPLACEMENT_FINDING_REQUIRED")
    return PriorDecisionResponse(decision_id, outcome, replacement)


def parse_reviewer_report(
    raw: bytes, *, expected_reviewer: Reviewer, expected_round: int, snapshot: Snapshot
) -> ReviewerReport:
    if not isinstance(expected_reviewer, Reviewer) or not isinstance(
        snapshot, Snapshot
    ):
        raise SchemaError("REPORT_SCHEMA_INVALID")
    data = _load_json(raw, limit=MAX_REPORT_BYTES, too_large="REPORT_TOO_LARGE")
    obj = _object(
        data,
        {
            "schema",
            "reviewer",
            "round",
            "snapshot",
            "status",
            "findings",
            "executions",
            "claims",
            "prior_decisions",
        },
        "REPORT_SCHEMA_INVALID",
    )
    if obj["schema"] != SCHEMA_VERSION or isinstance(obj["schema"], bool):
        raise SchemaError("REPORT_SCHEMA_INVALID")
    if obj["reviewer"] != expected_reviewer.value:
        raise SchemaError("REPORT_REVIEWER_MISMATCH")
    if obj["round"] != expected_round or isinstance(obj["round"], bool):
        raise SchemaError("REPORT_ROUND_MISMATCH")
    if obj["status"] != "complete":
        raise SchemaError("REPORT_NOT_TERMINAL")
    snap = _object(
        obj["snapshot"], {"head_sha", "diff_sha256"}, "REPORT_SCHEMA_INVALID"
    )
    if (
        snap["head_sha"] != snapshot.head_sha
        or snap["diff_sha256"] != snapshot.diff_sha256
    ):
        raise SchemaError("REPORT_SNAPSHOT_MISMATCH")
    executions = tuple(
        _parse_execution(item, reviewer=expected_reviewer, round_number=expected_round)
        for item in _array(
            obj["executions"], MAX_EXECUTIONS_PER_REVIEWER, "EXECUTION_LIMIT_EXCEEDED"
        )
    )
    execution_ids = [item.id for item in executions]
    if len(execution_ids) != len(set(execution_ids)):
        raise SchemaError("EXECUTION_ID_DUPLICATE")
    findings = tuple(
        _parse_finding(item, reviewer=expected_reviewer, round_number=expected_round)
        for item in _array(
            obj["findings"], MAX_FINDINGS_PER_REVIEWER, "FINDING_LIMIT_EXCEEDED"
        )
    )
    if len({item.id for item in findings}) != len(findings):
        raise SchemaError("FINDING_ID_DUPLICATE")
    available = set(execution_ids)
    for item in findings:
        if not set(item.execution_ids).issubset(available):
            raise SchemaError("EXECUTION_REFERENCE_INVALID")
        if expected_reviewer is Reviewer.B and not item.execution_ids:
            raise SchemaError("BEHAVIOR_EVIDENCE_REQUIRED")
    claims = tuple(
        _parse_claim(
            item,
            reviewer=expected_reviewer,
            round_number=expected_round,
            execution_ids=available,
        )
        for item in _array(
            obj["claims"], MAX_FINDINGS_PER_REVIEWER, "CLAIM_LIMIT_EXCEEDED"
        )
    )
    if expected_reviewer is not Reviewer.B and claims:
        raise SchemaError("CLAIM_SCHEMA_INVALID")
    if len({item.id for item in claims}) != len(claims):
        raise SchemaError("CLAIM_ID_DUPLICATE")
    responses = tuple(
        _parse_prior_response(item)
        for item in _array(
            obj["prior_decisions"],
            MAX_FINDINGS_PER_REVIEWER,
            "PRIOR_DECISION_RESPONSE_INVALID",
        )
    )
    if len({item.decision_id for item in responses}) != len(responses):
        raise SchemaError("PRIOR_DECISION_RESPONSE_INVALID")
    return ReviewerReport(
        expected_reviewer,
        expected_round,
        snapshot.head_sha,
        snapshot.diff_sha256,
        findings,
        executions,
        claims,
        responses,
    )


def _blocker_identity(value: object) -> tuple[str, int, Reviewer]:
    identifier = value.id if isinstance(value, Finding) else value
    if (
        not isinstance(identifier, str)
        or (match := _FINDING_ID.fullmatch(identifier)) is None
    ):
        raise SchemaError("DECISION_COVERAGE_INVALID")
    return identifier, int(match.group(2)), Reviewer(match.group(1))


def parse_decisions(
    raw: bytes, *, prior_blockers: Sequence[Finding | str]
) -> tuple[Decision, ...]:
    data = _load_json(raw, limit=MAX_REPORT_BYTES, too_large="DECISIONS_TOO_LARGE")
    items = _array(data, MAX_FINDINGS_PER_REVIEWER * 3, "DECISION_LIMIT_EXCEEDED")
    blockers = {
        _blocker_identity(item)[0]: _blocker_identity(item) for item in prior_blockers
    }
    decisions: list[Decision] = []
    for value in items:
        obj = _object(
            value,
            {"id", "finding_ref", "disposition", "rationale", "executions"},
            "DECISION_SCHEMA_INVALID",
        )
        identifier = obj["id"]
        if (
            not isinstance(identifier, str)
            or (decision_match := _DECISION_ID.fullmatch(identifier)) is None
        ):
            raise SchemaError("DECISION_ID_INVALID")
        ref = _object(
            obj["finding_ref"], {"round", "id", "reviewer"}, "DECISION_SCHEMA_INVALID"
        )
        finding_id = ref["id"]
        if not isinstance(finding_id, str) or finding_id not in blockers:
            raise SchemaError("DECISION_COVERAGE_INVALID")
        expected_id, expected_round, expected_reviewer = blockers[finding_id]
        if (
            ref["round"] != expected_round
            or isinstance(ref["round"], bool)
            or ref["reviewer"] != expected_reviewer.value
        ):
            raise SchemaError("DECISION_CROSS_REFERENCE_INVALID")
        if (
            int(decision_match.group(1)) != expected_round
            or decision_match.group(2) != expected_reviewer.value
        ):
            raise SchemaError("DECISION_CROSS_REFERENCE_INVALID")
        disposition = obj["disposition"]
        if not isinstance(disposition, str) or disposition not in {"fixed", "rebutted"}:
            raise SchemaError("DECISION_SCHEMA_INVALID")
        rationale = _text(obj["rationale"], MAX_EVIDENCE_TEXT_BYTES)
        executions = tuple(
            _parse_execution(item, reviewer=None, round_number=expected_round)
            for item in _array(
                obj["executions"],
                MAX_EXECUTIONS_PER_REVIEWER,
                "EXECUTION_LIMIT_EXCEEDED",
            )
        )
        if not executions:
            raise SchemaError("DECISION_EVIDENCE_REQUIRED")
        if len({item.id for item in executions}) != len(executions):
            raise SchemaError("EXECUTION_ID_DUPLICATE")
        decisions.append(
            Decision(
                identifier,
                expected_round,
                expected_id,
                expected_reviewer,
                disposition,
                rationale,
                executions,
            )
        )
    if len({item.id for item in decisions}) != len(decisions):
        raise SchemaError("DECISION_ID_DUPLICATE")
    if len({item.finding_id for item in decisions}) != len(decisions) or {
        item.finding_id for item in decisions
    } != set(blockers):
        raise SchemaError("DECISION_COVERAGE_INVALID")
    return tuple(decisions)
