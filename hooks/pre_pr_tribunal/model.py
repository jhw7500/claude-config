"""Strict, bounded data model for the pre-PR tribunal."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import posixpath
import re
import shlex
from typing import Mapping, Sequence
import unicodedata
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
SLOT_VERDICT_SCHEMA_VERSION = 2
LIFECYCLE_VERDICT_SCHEMA_VERSION = 3
EVIDENCE_VERDICT_SCHEMA_VERSION = 4
VERDICT_SCHEMA_VERSION = 5
LIFECYCLE_VERDICT_SCHEMAS = frozenset(
    (
        LIFECYCLE_VERDICT_SCHEMA_VERSION,
        EVIDENCE_VERDICT_SCHEMA_VERSION,
        VERDICT_SCHEMA_VERSION,
    )
)
EVIDENCE_VERDICT_SCHEMAS = frozenset(
    (EVIDENCE_VERDICT_SCHEMA_VERSION, VERDICT_SCHEMA_VERSION)
)
MIXED_SLOT_VERDICT_SCHEMAS = frozenset(
    (SLOT_VERDICT_SCHEMA_VERSION, *LIFECYCLE_VERDICT_SCHEMAS)
)
SUPPORTED_VERDICT_SCHEMAS = frozenset(
    (SCHEMA_VERSION, *MIXED_SLOT_VERDICT_SCHEMAS)
)
RECEIPT_PROVENANCE = frozenset(("native_submit", "legacy_telemetry_v1"))
REPORT_TEXT_CONTRACT_VERSION = 5
MAX_VERDICT_BYTES = 256 * 1024
MAX_REPORT_BYTES = 128 * 1024
MAX_EVIDENCE_TEXT_BYTES = 8 * 1024
MAX_COMMAND_TEXT_BYTES = 4 * 1024
MAX_FINDINGS_PER_REVIEWER = 128
MAX_INITIAL_PATHS = MAX_FINDINGS_PER_REVIEWER * 8
MAX_EXECUTIONS_PER_REVIEWER = 128

_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LIFECYCLE_ID = re.compile(r"[0-9a-f]{32}\Z")
_FINDING_ID = re.compile(r"([ABC])-R([1-3])-([0-9]{3})\Z")
_EXECUTION_ID = re.compile(r"([ABC])-R([1-3])-E([0-9]{3})\Z")
_DECISION_EXECUTION_ID = re.compile(r"D-R([1-3])-E([0-9]{3})\Z")
_DECISION_ID = re.compile(r"D-R([1-3])-([ABC])-([0-9]{3})\Z")
_CLAIM_ID = re.compile(r"([ABC])-R([1-3])-C([0-9]{3})\Z")
_SECRET = re.compile(
    r"BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY|"
    r"(?i:Authorization[ \t]*:[ \t]*Bearer[ \t]+[^\s\"']{8,})|"
    r"(?<![A-Za-z0-9])(?:xox[a-z]|xapp)-[A-Za-z0-9-]{8,}|"
    r"ghp_[A-Za-z0-9]{8}|github_pat_[A-Za-z0-9_]{8}|"
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8}|AKIA[A-Z0-9]{12}"
)
_HOME_PATH = re.compile(r"/(?:home|Users)/[^/\s?#'\"<>]+")
_HTTP_URL_START = re.compile(r"https?://", re.IGNORECASE)
_SHELL_CONTROL = frozenset(";|&()<>`")
_EXCERPT_CONTROLS = frozenset(("\n", "\t"))
_MAX_SHELL_NESTING = 64
_DRY_RUN_BUILD_TOOLS = frozenset({"make", "gmake", "ninja"})
_DRY_RUN_LONG_OPTIONS = frozenset((
    "--dry-run", "--just-print", "--question", "--recon", "--touch",
))
_MAKE_REQUIRED_ARGUMENT_OPTIONS = frozenset({"C", "f", "I", "W"})
_MAKE_OPTIONAL_ARGUMENT_OPTIONS = frozenset({"j", "l", "O"})
_MAKE_RELEVANT_LONG_OPTIONS = frozenset((
    "--assume-new", "--directory", "--file", "--include-dir", "--makefile",
    "--new-file", "--what-if",
))
_MAKE_IGNORED_LONG_OPTIONS = frozenset((
    "--jobs", "--load-average", "--max-load", "--output-sync",
))
_NINJA_ARGUMENT_OPTIONS = frozenset({"C", "d", "f", "j", "k", "l", "t", "w"})
_SHELL_EXECUTABLES = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_DYNAMIC_DIRECTORY = re.compile(r"[$`*?\[~]")
_AMBIGUOUS_CWD = "<ambiguous>"


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
    SKIPPED = "skipped"
    INCONCLUSIVE = "inconclusive"


class ReviewMode(str, Enum):
    OFF = "off"
    SINGLE = "single"
    ITERATIVE = "iterative"


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
class EvidenceReference:
    bundle_sha256: str
    entry_id: str

    def to_json(self):
        return {"bundle_sha256": self.bundle_sha256, "entry_id": self.entry_id}


@dataclass(frozen=True)
class EvidenceBinding:
    bundle_sha256: str
    expected_binding_json: bytes

    def to_json(self):
        return {"bundle_sha256": self.bundle_sha256,
                "expected_binding": json.loads(self.expected_binding_json)}


@dataclass(frozen=True)
class Execution:
    id: str
    command: str
    exit_code: int
    stdout_excerpt: str
    stderr_excerpt: str
    capture_sha256: str
    truncated: bool
    evidence_ref: EvidenceReference | None = None

    def to_json(self) -> dict[str, object]:
        value = {
            "id": self.id,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "capture_sha256": self.capture_sha256,
            "truncated": self.truncated,
        }
        if self.evidence_ref is not None:
            value["evidence_ref"] = self.evidence_ref.to_json()
        return value


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
class PrimaryEntryPath:
    path: str
    claim_id: str

    def to_json(self) -> dict[str, str]:
        return {"path": self.path, "claim_id": self.claim_id}


@dataclass(frozen=True)
class ClaimCoverage:
    complete: bool
    primary_entry_paths: Sequence[PrimaryEntryPath]

    def to_json(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "primary_entry_paths": [
                item.to_json() for item in self.primary_entry_paths
            ],
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
    coverage: ClaimCoverage | None
    prior_decisions: Sequence[PriorDecisionResponse]

    def to_json(self) -> dict[str, object]:
        value = {
            "status": "complete",
            "findings": [item.to_json() for item in self.findings],
            "executions": [item.to_json() for item in self.executions],
            "claims": [item.to_json() for item in self.claims],
            "prior_decisions": [item.to_json() for item in self.prior_decisions],
        }
        if self.coverage is not None:
            value["coverage"] = self.coverage.to_json()
        return value


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
class ContractBinding:
    report_text: int
    diff_recipe: int
    verdict_schema: int

    def to_json(self) -> dict[str, int]:
        return {
            "report_text": self.report_text,
            "diff_recipe": self.diff_recipe,
            "verdict_schema": self.verdict_schema,
        }


@dataclass(frozen=True)
class ReportReceipt:
    reviewer: Reviewer
    round: int
    path: str
    raw_sha256: str
    context_sha256: str | None = None
    report_contract_version: int | None = None
    attempt: int | None = None
    provenance: str | None = None


@dataclass(frozen=True)
class ReviewerSlot:
    # The v2 wire format calls this field "state". Keep the Python name for
    # compatibility with the v1 lifecycle and callers.
    status: str
    report: ReviewerReport | None = None
    receipt: ReportReceipt | None = None
    attempt_count: int = 0
    last_error: str | None = None

    def to_json(self, verdict_schema: int = SCHEMA_VERSION) -> dict[str, object]:
        if verdict_schema == SCHEMA_VERSION:
            if self.status == "pending":
                return {"status": "pending"}
            if self.status != "complete" or self.report is None:
                raise SchemaError("VERDICT_INVALID")
            return self.report.to_json()
        if verdict_schema not in MIXED_SLOT_VERDICT_SCHEMAS:
            raise SchemaError("VERDICT_INVALID")
        if (
            not isinstance(self.attempt_count, int)
            or isinstance(self.attempt_count, bool)
            or self.attempt_count < 0
            or (
                self.last_error is not None
                and (not isinstance(self.last_error, str) or not self.last_error)
            )
        ):
            raise SchemaError("VERDICT_INVALID")
        base: dict[str, object] = {
            "state": self.status,
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
        }
        if self.status == "disabled":
            if (
                verdict_schema != VERDICT_SCHEMA_VERSION
                or self.report is not None
                or self.receipt is not None
                or self.attempt_count != 0
                or self.last_error is not None
            ):
                raise SchemaError("VERDICT_INVALID")
            return base
        if self.status == "pending":
            if self.report is not None or self.receipt is not None:
                raise SchemaError("VERDICT_INVALID")
            return base
        receipt = self.receipt
        if (
            self.status != "sealed"
            or self.report is None
            or receipt is None
            or receipt.context_sha256 is None
            or receipt.report_contract_version is None
            or receipt.attempt is None
            or receipt.provenance is None
            or not isinstance(receipt.raw_sha256, str)
            or _SHA256.fullmatch(receipt.raw_sha256) is None
            or not isinstance(receipt.context_sha256, str)
            or _SHA256.fullmatch(receipt.context_sha256) is None
            or not isinstance(receipt.report_contract_version, int)
            or isinstance(receipt.report_contract_version, bool)
            or receipt.report_contract_version < 1
            or not isinstance(receipt.attempt, int)
            or isinstance(receipt.attempt, bool)
            or receipt.attempt < 1
            or receipt.attempt != self.attempt_count
            or receipt.provenance not in RECEIPT_PROVENANCE
            or self.last_error is not None
        ):
            raise SchemaError("VERDICT_INVALID")
        return {
            "state": "sealed",
            "report": self.report.to_json(),
            "receipt": {
                "raw_sha256": receipt.raw_sha256,
                "context_sha256": receipt.context_sha256,
                "report_contract_version": receipt.report_contract_version,
                "attempt": receipt.attempt,
                "provenance": receipt.provenance,
            },
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
        }


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
class IntensityRequest:
    value: int
    source: str
    requester: str
    reason: str
    fail_closed_reason: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "value": self.value,
            "source": self.source,
            "requester": self.requester,
            "reason": self.reason,
            "fail_closed_reason": self.fail_closed_reason,
        }


@dataclass(frozen=True)
class ReviewerPolicy:
    enabled: bool
    model: str

    def to_json(self) -> dict[str, object]:
        return {"enabled": self.enabled, "model": self.model}


@dataclass(frozen=True)
class PolicyBinding:
    config_sha256: str
    risk_floor: int
    effective_intensity: int
    mode: ReviewMode
    reasons: Sequence[str]
    request: IntensityRequest
    reviewers: Mapping[str, ReviewerPolicy]

    @property
    def active_reviewers(self) -> tuple[str, ...]:
        if self.mode is ReviewMode.OFF:
            return ()
        return tuple(key for key in "ABC" if self.reviewers[key].enabled)

    def to_json(self) -> dict[str, object]:
        if (
            not isinstance(self.config_sha256, str)
            or _SHA256.fullmatch(self.config_sha256) is None
            or not isinstance(self.risk_floor, int)
            or isinstance(self.risk_floor, bool)
            or not 0 <= self.risk_floor <= 100
            or not isinstance(self.effective_intensity, int)
            or isinstance(self.effective_intensity, bool)
            or not 0 <= self.effective_intensity <= 100
            or self.effective_intensity < self.risk_floor
            or not isinstance(self.mode, ReviewMode)
            or set(self.reviewers) != set("ABC")
        ):
            raise SchemaError("POLICY_INVALID")
        return {
            "config_sha256": self.config_sha256,
            "risk_floor": self.risk_floor,
            "effective_intensity": self.effective_intensity,
            "mode": self.mode.value,
            "reasons": list(self.reasons),
            "request": self.request.to_json(),
            "reviewers": {
                key: self.reviewers[key].to_json() for key in "ABC"
            },
        }


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
    contract: ContractBinding | None = None
    lifecycle_id: str | None = None
    evidence_binding: EvidenceBinding | None = None
    evidence_fallback_reason: str | None = None
    evidence_contract: int | None = None
    policy: PolicyBinding | None = None

    @property
    def snapshot(self) -> Snapshot:
        return Snapshot(
            SCHEMA_VERSION,
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
        if set(self.reviewers) != {"A", "B", "C"}:
            raise SchemaError("VERDICT_INVALID")
        if self.schema in MIXED_SLOT_VERDICT_SCHEMAS:
            if (
                self.contract is None
                or (
                    self.schema in LIFECYCLE_VERDICT_SCHEMAS
                    and (
                        not isinstance(self.lifecycle_id, str)
                        or _LIFECYCLE_ID.fullmatch(self.lifecycle_id) is None
                    )
                )
                or (
                    self.schema == SLOT_VERDICT_SCHEMA_VERSION
                    and self.lifecycle_id is not None
                )
            ):
                raise SchemaError("VERDICT_INVALID")
            for key in "ABC":
                receipt = self.reviewers[key].receipt
                if receipt is not None and (
                    receipt.reviewer is not Reviewer(key)
                    or not isinstance(receipt.round, int)
                    or isinstance(receipt.round, bool)
                    or receipt.round != self.round
                    or receipt.path
                    != f".review/inbox/round-{self.round}/{key}.json"
                    or receipt.report_contract_version != self.contract.report_text
                ):
                    raise SchemaError("VERDICT_INVALID")
        elif self.schema == SCHEMA_VERSION:
            if self.contract is not None or self.lifecycle_id is not None:
                raise SchemaError("VERDICT_INVALID")
        else:
            raise SchemaError("VERDICT_INVALID")
        value: dict[str, object] = {
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
            "reviewers": {
                key: self.reviewers[key].to_json(self.schema) for key in "ABC"
            },
            "decisions": [item.to_json() for item in self.decisions],
            "history": [item.to_json() for item in self.history],
            "gate": self.gate.to_json(),
            "created_at": self.created_at,
        }
        if self.schema in MIXED_SLOT_VERDICT_SCHEMAS:
            value["contract"] = self.contract.to_json()
        if self.schema in LIFECYCLE_VERDICT_SCHEMAS:
            value["lifecycle_id"] = self.lifecycle_id
        if self.schema in EVIDENCE_VERDICT_SCHEMAS:
            value["evidence_binding"] = self.evidence_binding.to_json() if self.evidence_binding else None
            value["evidence_fallback_reason"] = self.evidence_fallback_reason
            if self.evidence_contract is not None:
                value["evidence_contract"] = _integer(
                    self.evidence_contract, "VERDICT_INVALID", minimum=1, maximum=2)
        elif (self.evidence_binding is not None or self.evidence_fallback_reason is not None
                or self.evidence_contract is not None):
            raise SchemaError("VERDICT_INVALID")
        if self.schema == VERDICT_SCHEMA_VERSION:
            if self.policy is None:
                raise SchemaError("POLICY_INVALID")
            value["policy"] = self.policy.to_json()
        elif self.policy is not None:
            raise SchemaError("VERDICT_INVALID")
        return value


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


def _execution_excerpt(value: object) -> str:
    if not isinstance(value, str):
        raise SchemaError("TEXT_INVALID")
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeEncodeError:
        raise SchemaError("TEXT_INVALID") from None
    if size > MAX_EVIDENCE_TEXT_BYTES:
        raise SchemaError("TEXT_TOO_LARGE")
    if unicodedata.normalize("NFC", value) != value or any(
        unicodedata.category(character) in {"Cc", "Cs"}
        and character not in _EXCERPT_CONTROLS
        for character in value
    ):
        raise SchemaError("TEXT_INVALID")
    if _SECRET.search(value) or _contains_home_path(value):
        raise SchemaError("EVIDENCE_SECRET_DETECTED")
    return value


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
    value: object, *, reviewer: Reviewer | None, round_number: int,
    report_contract_version: int = REPORT_TEXT_CONTRACT_VERSION,
) -> Execution:
    reference = None
    if isinstance(value, dict) and "evidence_ref" in value:
        if reviewer is not Reviewer.B or report_contract_version < 3:
            raise SchemaError("EXECUTION_SCHEMA_INVALID")
        ref = _object(value["evidence_ref"], {"bundle_sha256", "entry_id"}, "EXECUTION_SCHEMA_INVALID")
        if (not isinstance(ref["bundle_sha256"], str) or _SHA256.fullmatch(ref["bundle_sha256"]) is None
            or not isinstance(ref["entry_id"], str) or re.fullmatch(r"E(?:00[1-9]|0[1-5][0-9]|06[0-4])", ref["entry_id"]) is None):
            raise SchemaError("EXECUTION_SCHEMA_INVALID")
        reference = EvidenceReference(ref["bundle_sha256"], ref["entry_id"])
        value = {key: item for key, item in value.items() if key != "evidence_ref"}
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
    stdout = _execution_excerpt(obj["stdout_excerpt"])
    stderr = _execution_excerpt(obj["stderr_excerpt"])
    capture = obj["capture_sha256"]
    if not isinstance(capture, str) or _SHA256.fullmatch(capture) is None:
        raise SchemaError("EXECUTION_SCHEMA_INVALID")
    truncated = obj["truncated"]
    if not isinstance(truncated, bool):
        raise SchemaError("EXECUTION_SCHEMA_INVALID")
    return Execution(identifier, command, exit_code, stdout, stderr, capture, truncated, reference)


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


def _parse_claim_coverage(
    value: object, *, claims: Sequence[Claim]
) -> ClaimCoverage:
    obj = _object(
        value,
        {"complete", "primary_entry_paths"},
        "CLAIM_COVERAGE_INVALID",
    )
    if obj["complete"] is not True or not claims:
        raise SchemaError("CLAIM_COVERAGE_INVALID")
    claim_ids = {claim.id for claim in claims}
    entries: list[PrimaryEntryPath] = []
    paths: set[str] = set()
    referenced_claims: set[str] = set()
    for raw in _array(
        obj["primary_entry_paths"],
        MAX_FINDINGS_PER_REVIEWER,
        "CLAIM_COVERAGE_INVALID",
    ):
        entry = _object(
            raw, {"path", "claim_id"}, "CLAIM_COVERAGE_INVALID"
        )
        try:
            path = _path(entry["path"])
        except SchemaError:
            raise SchemaError("CLAIM_COVERAGE_INVALID") from None
        claim_id = entry["claim_id"]
        if (
            not isinstance(claim_id, str)
            or claim_id not in claim_ids
            or path in paths
            or claim_id in referenced_claims
        ):
            raise SchemaError("CLAIM_COVERAGE_INVALID")
        paths.add(path)
        referenced_claims.add(claim_id)
        entries.append(PrimaryEntryPath(path, claim_id))
    return ClaimCoverage(True, tuple(entries))


BuildEvidenceKey = tuple[str, str, tuple[str, ...]]


def _make_evidence(
    arguments: Sequence[str],
    *,
    cwd: str,
) -> tuple[bool, BuildEvidenceKey]:
    dry_run = False
    signature: list[str] = []
    targets: list[str] = []
    options_ended = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if options_ended:
            targets.append(argument)
            index += 1
            continue
        if argument == "--":
            options_ended = True
            index += 1
            continue
        if argument in _DRY_RUN_LONG_OPTIONS:
            dry_run = True
            index += 1
            continue
        if argument.startswith("--"):
            option, separator, value = argument.partition("=")
            if option in _MAKE_RELEVANT_LONG_OPTIONS:
                if not separator and index + 1 < len(arguments):
                    index += 1
                    value = arguments[index]
                signature.append(f"{option}={value}")
            elif option in _MAKE_IGNORED_LONG_OPTIONS:
                if (
                    not separator
                    and index + 1 < len(arguments)
                    and not arguments[index + 1].startswith("-")
                ):
                    index += 1
            else:
                signature.append(argument)
            index += 1
            continue
        if not argument.startswith("-") or argument == "-":
            targets.append(argument)
            index += 1
            continue
        cluster = argument[1:]
        preserved: list[str] = []
        for offset, option in enumerate(cluster):
            if option in {"n", "q", "t"}:
                dry_run = True
                continue
            if option in _MAKE_REQUIRED_ARGUMENT_OPTIONS:
                value = cluster[offset + 1 :]
                if not value and index + 1 < len(arguments):
                    index += 1
                    value = arguments[index]
                if preserved:
                    signature.append("-" + "".join(preserved))
                signature.append(f"-{option}={value}")
                break
            if option in _MAKE_OPTIONAL_ARGUMENT_OPTIONS:
                value = cluster[offset + 1 :]
                if (
                    not value
                    and index + 1 < len(arguments)
                    and not arguments[index + 1].startswith("-")
                ):
                    index += 1
                if preserved:
                    signature.append("-" + "".join(preserved))
                break
            preserved.append(option)
        else:
            if preserved:
                signature.append("-" + "".join(preserved))
        index += 1
    signature.append("--targets")
    signature.extend(targets)
    return dry_run, ("make", cwd, tuple(signature))


def _ninja_evidence(
    arguments: Sequence[str],
    *,
    cwd: str,
) -> tuple[bool, BuildEvidenceKey]:
    dry_run = False
    signature: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--dry-run", "--help", "--tool", "--version"}:
            dry_run = True
            index += 1
            continue
        if not argument.startswith("-") or argument.startswith("--"):
            signature.append(argument)
            index += 1
            continue
        cluster = argument[1:]
        preserved: list[str] = []
        for offset, option in enumerate(cluster):
            if option == "n":
                dry_run = True
                continue
            if option in _NINJA_ARGUMENT_OPTIONS:
                value = cluster[offset + 1 :]
                if not value and index + 1 < len(arguments):
                    index += 1
                    value = arguments[index]
                if preserved:
                    signature.append("-" + "".join(preserved))
                if option == "t":
                    dry_run = True
                else:
                    signature.append(f"-{option}={value}")
                break
            preserved.append(option)
        else:
            if preserved:
                signature.append("-" + "".join(preserved))
        index += 1
    return dry_run, ("ninja", cwd, tuple(signature))


def _makeflags_are_dry_run(words: Sequence[str], *, cwd: str) -> bool:
    for word in words:
        name, separator, value = word.partition("=")
        if not separator or name not in {"MAKEFLAGS", "MFLAGS"}:
            continue
        try:
            arguments = list(shlex.split(value, posix=True))
        except ValueError:
            return True
        if arguments and not arguments[0].startswith("-"):
            arguments[0] = "-" + arguments[0]
        if _make_evidence(arguments, cwd=cwd)[0]:
            return True
    return False


def _shell_command_argument(arguments: Sequence[str]) -> str | None:
    for index, argument in enumerate(arguments):
        if argument == "--":
            continue
        if (
            argument.startswith("-")
            and not argument.startswith("--")
            and "c" in argument[1:]
        ):
            if index + 1 < len(arguments):
                return arguments[index + 1]
            return None
    return None


def _compose_static_cwd(cwd: str | None, directory: str) -> str | None:
    if cwd is None or _DYNAMIC_DIRECTORY.search(directory):
        return None
    if posixpath.isabs(directory):
        return posixpath.normpath(directory)
    return posixpath.normpath(posixpath.join(cwd, directory))


def _command_executable(
    words: Sequence[str],
    *,
    cwd: str | None,
) -> tuple[str, tuple[str, ...], str | None] | None:
    current = tuple(words)
    index = 0
    unwrap_count = 0
    while index < len(current) and _ENV_ASSIGNMENT.match(current[index]):
        index += 1
    while index < len(current):
        unwrap_count += 1
        if unwrap_count > _MAX_SHELL_NESTING:
            return None
        executable = current[index].rsplit("/", 1)[-1]
        if executable == "rtk":
            index += 1
            if index < len(current) and current[index] == "proxy":
                index += 1
            continue
        if executable == "command":
            index += 1
            while index < len(current) and current[index].startswith("-"):
                index += 1
            continue
        if executable == "env":
            index += 1
            chdir_seen = False
            while index < len(current):
                word = current[index]
                if word == "--":
                    index += 1
                    break
                if _ENV_ASSIGNMENT.match(word):
                    index += 1
                    continue
                if word in {"-S", "--split-string"}:
                    if index + 1 >= len(current):
                        return None
                    try:
                        expanded = tuple(shlex.split(current[index + 1], posix=True))
                    except ValueError:
                        return None
                    current = expanded + current[index + 2 :]
                    index = 0
                    break
                if word.startswith("--split-string=") or (
                    word.startswith("-S") and len(word) > 2
                ):
                    payload = word.split("=", 1)[1] if "=" in word else word[2:]
                    try:
                        expanded = tuple(shlex.split(payload, posix=True))
                    except ValueError:
                        return None
                    current = expanded + current[index + 1 :]
                    index = 0
                    break
                if word in {"-C", "--chdir"}:
                    if index + 1 >= len(current):
                        return None
                    cwd = (
                        None
                        if chdir_seen
                        else _compose_static_cwd(cwd, current[index + 1])
                    )
                    chdir_seen = True
                    index += 2
                    continue
                if word.startswith("--chdir="):
                    cwd = (
                        None
                        if chdir_seen
                        else _compose_static_cwd(cwd, word.split("=", 1)[1])
                    )
                    chdir_seen = True
                    index += 1
                    continue
                if word.startswith("-C") and len(word) > 2:
                    cwd = (
                        None
                        if chdir_seen
                        else _compose_static_cwd(cwd, word[2:])
                    )
                    chdir_seen = True
                    index += 1
                    continue
                if word in {"-u", "--unset"}:
                    index += 2
                    continue
                if word.startswith("-"):
                    index += 1
                    continue
                break
            continue
        if executable == "timeout":
            index += 1
            while index < len(current):
                word = current[index]
                if word == "--":
                    index += 1
                    break
                if word in {"-k", "--kill-after", "-s", "--signal"}:
                    index += 2
                    continue
                if word.startswith("-"):
                    index += 1
                    continue
                index += 1
                break
            continue
        return executable, tuple(current[index + 1 :]), cwd
    return None


def _static_cd(arguments: Sequence[str]) -> str | None:
    paths = tuple(argument for argument in arguments if argument != "--")
    if len(paths) == 1 and not paths[0].startswith("-"):
        return paths[0]
    return None


def _embedded_dry_runs(
    words: Sequence[str],
    *,
    cwd: str,
) -> set[BuildEvidenceKey]:
    found: set[BuildEvidenceKey] = set()
    for index, word in enumerate(words):
        executable = word.rsplit("/", 1)[-1]
        if executable in {"make", "gmake"}:
            dry_run, key = _make_evidence(words[index + 1 :], cwd=cwd)
            dry_run = dry_run or _makeflags_are_dry_run(words, cwd=cwd)
        elif executable == "ninja":
            dry_run, key = _ninja_evidence(words[index + 1 :], cwd=cwd)
        else:
            continue
        if dry_run:
            found.add(key)
    return found


def _classify_build_evidence(
    command: str,
    *,
    depth: int = 0,
    cwd: str | None = ".",
    successful: bool = True,
) -> tuple[frozenset[BuildEvidenceKey], frozenset[BuildEvidenceKey]]:
    if depth >= _MAX_SHELL_NESTING:
        return frozenset(), frozenset()
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="();&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = tuple(lexer)
    except ValueError:
        return frozenset(), frozenset()
    dry_runs: set[BuildEvidenceKey] = set()
    live_runs: set[BuildEvidenceKey] = set()
    segment: list[str] = []
    separator: str | None = None
    trailing_separator: str | None = None
    ancestry: list[str | None] = []
    parts: list[tuple[str | None, tuple[str | None, ...], tuple[str, ...]]] = []
    for word in words:
        if word == "(":
            if segment:
                parts.append((separator, tuple(ancestry), tuple(segment)))
                segment = []
            ancestry.append(separator)
            separator = None
            trailing_separator = None
            continue
        if word == ")":
            if segment:
                parts.append((separator, tuple(ancestry), tuple(segment)))
                segment = []
            if ancestry:
                ancestry.pop()
            separator = None
            trailing_separator = None
            continue
        if word and all(character in ";&|" for character in word):
            if segment:
                parts.append((separator, tuple(ancestry), tuple(segment)))
                segment = []
            separator = word
            trailing_separator = word
            continue
        segment.append(word)
        trailing_separator = None
    if segment:
        parts.append((separator, tuple(ancestry), tuple(segment)))

    for part_index, (preceding, conditional_ancestry, part) in enumerate(parts):
        resolved = _command_executable(part, cwd=cwd)
        if resolved is None:
            dry_runs.update(_embedded_dry_runs(part, cwd=_AMBIGUOUS_CWD))
            continue
        executable, arguments, command_cwd = resolved
        following = tuple(
            item[0] for item in parts[part_index + 1 :]
            if item[0] is not None
        )
        if trailing_separator is not None:
            following += (trailing_separator,)
        unambiguous = (
            successful
            and preceding not in {"||", "|"}
            and all(item in {None, "&&"} for item in conditional_ancestry)
            and all(item == "&&" for item in following)
        )
        if executable == "cd":
            next_cwd = _static_cd(arguments)
            if next_cwd is not None and unambiguous:
                cwd = _compose_static_cwd(command_cwd, next_cwd)
            else:
                cwd = None
            continue
        if executable in _SHELL_EXECUTABLES:
            nested = _shell_command_argument(arguments)
            if nested is None:
                continue
            nested_dry, nested_live = _classify_build_evidence(
                nested,
                depth=depth + 1,
                cwd=command_cwd,
                successful=successful,
            )
            dry_runs.update(nested_dry)
            if unambiguous:
                live_runs.update(nested_live)
            continue
        if executable not in _DRY_RUN_BUILD_TOOLS:
            dry_runs.update(
                _embedded_dry_runs(
                    part,
                    cwd=command_cwd or _AMBIGUOUS_CWD,
                )
            )
            continue
        evidence_cwd = command_cwd or _AMBIGUOUS_CWD
        is_dry_run, key = (
            _make_evidence(arguments, cwd=evidence_cwd)
            if executable in {"make", "gmake"}
            else _ninja_evidence(arguments, cwd=evidence_cwd)
        )
        if executable in {"make", "gmake"} and _makeflags_are_dry_run(
            part,
            cwd=evidence_cwd,
        ):
            is_dry_run = True
        if is_dry_run:
            dry_runs.add(key)
        elif unambiguous and command_cwd is not None:
            live_runs.add(key)
    return frozenset(dry_runs), frozenset(live_runs)


def _validate_primary_entry_path_evidence(
    *,
    claims: Sequence[Claim],
    coverage: ClaimCoverage,
    executions: Sequence[Execution],
) -> None:
    claims_by_id = {claim.id: claim for claim in claims}
    executions_by_id = {execution.id: execution for execution in executions}
    for entry in coverage.primary_entry_paths:
        claim = claims_by_id[entry.claim_id]
        if claim.result != "supported":
            continue
        dry_runs: set[BuildEvidenceKey] = set()
        live_runs: set[BuildEvidenceKey] = set()
        for execution_id in claim.execution_ids:
            execution = executions_by_id[execution_id]
            execution_dry, execution_live = _classify_build_evidence(
                execution.command,
                successful=execution.exit_code == 0,
            )
            dry_runs.update(execution_dry)
            live_runs.update(execution_live)
        if dry_runs - live_runs:
            raise SchemaError("DRY_RUN_EVIDENCE_INSUFFICIENT")


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
    raw: bytes, *, expected_reviewer: Reviewer, expected_round: int, snapshot: Snapshot,
    report_contract_version: int = REPORT_TEXT_CONTRACT_VERSION,
) -> ReviewerReport:
    if not isinstance(expected_reviewer, Reviewer) or not isinstance(
        snapshot, Snapshot
    ):
        raise SchemaError("REPORT_SCHEMA_INVALID")
    data = _load_json(raw, limit=MAX_REPORT_BYTES, too_large="REPORT_TOO_LARGE")
    report_keys = {
        "schema",
        "reviewer",
        "round",
        "snapshot",
        "status",
        "findings",
        "executions",
        "claims",
        "prior_decisions",
    }
    if expected_reviewer is Reviewer.B and report_contract_version >= 4:
        report_keys.add("coverage")
    obj = _object(
        data,
        report_keys,
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
        _parse_execution(item, reviewer=expected_reviewer, round_number=expected_round,
                         report_contract_version=report_contract_version)
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
    coverage = None
    if expected_reviewer is Reviewer.B and report_contract_version >= 4:
        coverage = _parse_claim_coverage(obj["coverage"], claims=claims)
    if (
        expected_reviewer is Reviewer.B
        and report_contract_version >= 5
        and coverage is not None
    ):
        _validate_primary_entry_path_evidence(
            claims=claims,
            coverage=coverage,
            executions=executions,
        )
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
        coverage,
        responses,
    )


def validate_report_bytes(
    raw: bytes,
    *,
    expected_reviewer: Reviewer,
    expected_round: int,
    snapshot: Snapshot,
) -> tuple[ReviewerReport, str]:
    """Validate one exact reviewer response and return its raw-byte digest."""
    report = parse_reviewer_report(
        raw,
        expected_reviewer=expected_reviewer,
        expected_round=expected_round,
        snapshot=snapshot,
    )
    return report, hashlib.sha256(raw).hexdigest()


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
