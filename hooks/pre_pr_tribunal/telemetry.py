"""Bounded observational timing ledger, independent of verdict and gate state."""

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
import re
import secrets
from types import MappingProxyType

from .git_state import DIFF_RECIPE_VERSION
from .model import (
    REPORT_TEXT_CONTRACT_VERSION, Reviewer, SchemaError, Snapshot,
    _contains_home_path, _head_ref, _load_json, _SECRET, _text,
)
from .review_store import (
    atomic_replace_bytes, locked_review, read_named_file, repository_root,
)


TELEMETRY_SCHEMA_VERSION = 1
MAX_TELEMETRY_BYTES = 2 * 1024 * 1024
MAX_TELEMETRY_RUNS = 16
MAX_TELEMETRY_SPANS_PER_RUN = 128
MAX_REASON_CODE_BYTES = 64

_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIFF = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]+\Z")
_MAX_INTEGER = 2**63 - 1
_INVALID = "TELEMETRY_INVALID"
_UNSAFE = "TELEMETRY_FILE_UNSAFE"
_TOO_LARGE = "TELEMETRY_TOO_LARGE"
_ANOMALY = "TELEMETRY_CLOCK_ANOMALY"


class TelemetryStage(str, Enum):
    SNAPSHOT_PREFLIGHT = "snapshot_preflight"
    VIEW_CREATE = "view_create"
    REVIEWER_DISPATCH_WAIT = "reviewer_dispatch_wait"
    REVIEWER_TOTAL = "reviewer_total"
    REPORT_STORE = "report_store"
    REPORT_VALIDATION = "report_validation"
    FINALIZE = "finalize"
    VIEW_CLEANUP = "view_cleanup"
    RECOVERY_RETRY = "recovery_retry"


class TelemetryOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    INCOMPLETE = "incomplete"
    CLOCK_ANOMALY = "clock_anomaly"


@dataclass(frozen=True)
class TelemetryBinding:
    status: str
    repository: str | None
    base_ref: str
    base_sha: str | None
    head_ref: str | None
    head_sha: str | None
    merge_base_sha: str | None
    diff_sha256: str | None
    contract: Mapping[str, int]

    def __post_init__(self):
        object.__setattr__(self, "contract", MappingProxyType(dict(self.contract)))

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status, "repository": self.repository,
            "base_ref": self.base_ref, "base_sha": self.base_sha,
            "head_ref": self.head_ref, "head_sha": self.head_sha,
            "merge_base_sha": self.merge_base_sha, "diff_sha256": self.diff_sha256,
            "contract": dict(self.contract),
        }


@dataclass(frozen=True)
class TelemetrySpan:
    span_id: str
    stage: TelemetryStage
    reviewer: Reviewer | None
    attempt: int
    started_at: str
    ended_at: str | None
    started_monotonic_ns: int
    ended_monotonic_ns: int | None
    duration_ms: int | None
    outcome: TelemetryOutcome | None
    reason_code: str | None

    def to_json(self) -> dict[str, object]:
        return {
            "span_id": self.span_id, "stage": self.stage.value,
            "reviewer": self.reviewer.value if self.reviewer else None,
            "attempt": self.attempt, "started_at": self.started_at,
            "ended_at": self.ended_at,
            "started_monotonic_ns": self.started_monotonic_ns,
            "ended_monotonic_ns": self.ended_monotonic_ns,
            "duration_ms": self.duration_ms,
            "status": self.outcome.value if self.outcome else "running",
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class TelemetryRun:
    run_id: str
    runtime: str
    round: int
    binding: TelemetryBinding
    started_at: str
    ended_at: str | None
    started_monotonic_ns: int
    outcome: TelemetryOutcome | None
    reason_code: str | None
    started_late: bool
    telemetry_incomplete: bool
    telemetry_incomplete_reason: str | None
    spans: tuple[TelemetrySpan, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id, "runtime": self.runtime, "round": self.round,
            "binding": self.binding.to_json(), "started_at": self.started_at,
            "ended_at": self.ended_at,
            "started_monotonic_ns": self.started_monotonic_ns,
            "status": self.outcome.value if self.outcome else "running",
            "reason_code": self.reason_code, "started_late": self.started_late,
            "telemetry_incomplete": self.telemetry_incomplete,
            "telemetry_incomplete_reason": self.telemetry_incomplete_reason,
            "spans": [item.to_json() for item in self.spans],
        }


@dataclass(frozen=True)
class TelemetryLedger:
    schema: int
    runs: tuple[TelemetryRun, ...]

    def to_json(self) -> dict[str, object]:
        return {"schema": self.schema, "runs": [item.to_json() for item in self.runs]}


def _contract() -> dict[str, int]:
    return {
        "report_text": REPORT_TEXT_CONTRACT_VERSION,
        "diff_recipe": DIFF_RECIPE_VERSION,
        "telemetry_schema": TELEMETRY_SCHEMA_VERSION,
    }


def _require(condition: bool) -> None:
    if not condition:
        raise SchemaError(_INVALID)


def _object(value: object, keys: str) -> dict:
    _require(isinstance(value, dict) and set(value) == set(keys.split()))
    return value


def _integer(value: object, minimum: int = 0, maximum: int = _MAX_INTEGER) -> int:
    _require(type(value) is int and minimum <= value <= maximum)
    return value


def _pattern(value: object, pattern: re.Pattern) -> str:
    _require(isinstance(value, str) and pattern.fullmatch(value) is not None)
    return value


def _timestamp(value: object) -> str:
    value = _pattern(value, _TIMESTAMP)
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise SchemaError(_INVALID) from None
    return value


def _safe_text(value: object, maximum: int) -> str:
    try:
        text = _text(value, maximum)
        _require(not _SECRET.search(text) and not _contains_home_path(text))
        return text
    except SchemaError:
        raise SchemaError(_INVALID) from None


def _ref(value: object, *, base: bool = False) -> str:
    text = _safe_text(value, 256 if base else 1024)
    _require(not text.startswith("-"))
    try:
        _head_ref("refs/heads/" + text if base else text)
    except SchemaError:
        raise SchemaError(_INVALID) from None
    return text


def _enum(kind, value):
    _require(isinstance(value, str))
    try:
        return kind(value)
    except ValueError:
        raise SchemaError(_INVALID) from None


def _reason(value: object) -> str:
    return _pattern(value, _REASON_CODE)


def _status(value: object, reason: object) -> TelemetryOutcome | None:
    outcome = None if value == "running" else _enum(TelemetryOutcome, value)
    if outcome in (None, TelemetryOutcome.SUCCESS):
        _require(reason is None)
    else:
        _reason(reason)
    return outcome


def _dimension(stage: TelemetryStage, reviewer: Reviewer | None) -> None:
    _require(isinstance(stage, TelemetryStage))
    _require(reviewer is None or isinstance(reviewer, Reviewer))
    if stage in {TelemetryStage.SNAPSHOT_PREFLIGHT, TelemetryStage.FINALIZE}:
        _require(reviewer is None)
    elif stage is not TelemetryStage.RECOVERY_RETRY:
        _require(reviewer is not None)


def _parse_binding(value: object) -> TelemetryBinding:
    obj = _object(value, "status repository base_ref base_sha head_ref head_sha merge_base_sha diff_sha256 contract")
    _require(obj["status"] in ("pending", "bound"))
    _ref(obj["base_ref"], base=True)
    contract = _object(obj["contract"], "report_text diff_recipe telemetry_schema")
    for name, expected in _contract().items():
        _require(type(contract[name]) is int and contract[name] == expected)
    for name in ("repository", "base_sha", "head_ref", "head_sha", "merge_base_sha", "diff_sha256"):
        value = obj[name]
        if value is None:
            _require(obj["status"] == "pending")
        elif name == "repository":
            _pattern(_safe_text(value, 256), _REPOSITORY)
            _require(value.split("/")[1] not in (".", ".."))
        elif name == "head_ref":
            _ref(value)
        else:
            _pattern(value, _DIFF if name == "diff_sha256" else _SHA)
    return TelemetryBinding(**obj)


def _parse_span(value: object) -> TelemetrySpan:
    obj = _object(value, "span_id stage reviewer attempt started_at ended_at started_monotonic_ns ended_monotonic_ns duration_ms status reason_code")
    _pattern(obj["span_id"], _ID)
    stage = _enum(TelemetryStage, obj["stage"])
    reviewer = None if obj["reviewer"] is None else _enum(Reviewer, obj["reviewer"])
    _dimension(stage, reviewer)
    _integer(obj["attempt"], 1, 2**31 - 1)
    started = _timestamp(obj["started_at"])
    start_ns = _integer(obj["started_monotonic_ns"])
    outcome = _status(obj["status"], obj["reason_code"])
    if outcome is None:
        _require(all(obj[name] is None for name in ("ended_at", "ended_monotonic_ns", "duration_ms")))
    else:
        ended = _timestamp(obj["ended_at"])
        end_ns = _integer(obj["ended_monotonic_ns"])
        reversal = ended < started or end_ns < start_ns
        if reversal:
            _require(outcome is TelemetryOutcome.CLOCK_ANOMALY and obj["reason_code"] == _ANOMALY)
        if outcome is TelemetryOutcome.CLOCK_ANOMALY:
            _require(obj["duration_ms"] is None)
        else:
            _require(_integer(obj["duration_ms"]) == (end_ns - start_ns) // 1_000_000)
    fields = dict(obj)
    del fields["status"]
    fields.update(stage=stage, reviewer=reviewer, outcome=outcome)
    return TelemetrySpan(**fields)


def _array(value: object, maximum: int) -> list:
    _require(isinstance(value, list))
    if len(value) > maximum:
        raise SchemaError(_TOO_LARGE)
    return value


def _parse_run(value: object) -> TelemetryRun:
    obj = _object(value, "run_id runtime round binding started_at ended_at started_monotonic_ns status reason_code started_late telemetry_incomplete telemetry_incomplete_reason spans")
    _pattern(obj["run_id"], _ID)
    _require(obj["runtime"] in ("claude", "codex"))
    _integer(obj["round"], 1, 3)
    binding = _parse_binding(obj["binding"])
    _timestamp(obj["started_at"])
    _integer(obj["started_monotonic_ns"])
    outcome = _status(obj["status"], obj["reason_code"])
    if outcome is None:
        _require(obj["ended_at"] is None)
    elif _timestamp(obj["ended_at"]) < obj["started_at"]:
        _require(outcome is TelemetryOutcome.CLOCK_ANOMALY and obj["reason_code"] == _ANOMALY)
    for name in ("started_late", "telemetry_incomplete"):
        _require(type(obj[name]) is bool)
    if obj["telemetry_incomplete"]:
        _reason(obj["telemetry_incomplete_reason"])
    else:
        _require(obj["telemetry_incomplete_reason"] is None)
    spans = tuple(_parse_span(item) for item in _array(obj["spans"], MAX_TELEMETRY_SPANS_PER_RUN))
    _require(len({item.span_id for item in spans}) == len(spans))
    if outcome is not None:
        _require(all(item.outcome is not None for item in spans))
    fields = dict(obj)
    del fields["status"]
    fields.update(binding=binding, spans=spans, outcome=outcome)
    return TelemetryRun(**fields)


def _parse_ledger(raw: bytes) -> TelemetryLedger:
    try:
        obj = _object(_load_json(raw, limit=MAX_TELEMETRY_BYTES, too_large=_TOO_LARGE), "schema runs")
        _require(type(obj["schema"]) is int and obj["schema"] == TELEMETRY_SCHEMA_VERSION)
        runs = tuple(_parse_run(item) for item in _array(obj["runs"], MAX_TELEMETRY_RUNS))
        _require(len({item.run_id for item in runs}) == len(runs))
        return TelemetryLedger(TELEMETRY_SCHEMA_VERSION, runs)
    except SchemaError as error:
        raise SchemaError(_TOO_LARGE if error.code == _TOO_LARGE else _INVALID) from None


@contextmanager
def _store(cwd: Path):
    try:
        with locked_review(repository_root(cwd), create=True) as directory:
            yield directory
    except SchemaError as error:
        if error.code in {_INVALID, _TOO_LARGE, _UNSAFE}:
            raise
        raise SchemaError(_UNSAFE) from None
    except OSError:
        raise SchemaError(_UNSAFE) from None


def _read(directory: int) -> TelemetryLedger:
    try:
        raw = read_named_file(
            directory, "telemetry.json", maximum=MAX_TELEMETRY_BYTES,
            missing="TELEMETRY_MISSING", unsafe=_UNSAFE, exact_mode=0o600,
        )
    except SchemaError as error:
        if error.code == "TELEMETRY_MISSING":
            return TelemetryLedger(TELEMETRY_SCHEMA_VERSION, ())
        if error.code == "FILE_TOO_LARGE":
            raise SchemaError(_TOO_LARGE) from None
        raise
    return _parse_ledger(raw)


def _write(directory: int, ledger: TelemetryLedger) -> None:
    raw = json.dumps(ledger.to_json(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _parse_ledger(raw)
    atomic_replace_bytes(
        directory, "telemetry.json", raw, maximum=MAX_TELEMETRY_BYTES,
        too_large=_TOO_LARGE, unsafe=_UNSAFE, exact_mode=0o600,
    )


def read_ledger(cwd: Path) -> TelemetryLedger:
    with _store(cwd) as directory:
        return _read(directory)


def _find_run(ledger: TelemetryLedger, run_id: str, *, active: bool = False) -> TelemetryRun:
    _pattern(run_id, _ID)
    for run in ledger.runs:
        if run.run_id == run_id:
            _require(not active or run.outcome is None)
            return run
    raise SchemaError(_INVALID)


def _save_run(directory: int, ledger: TelemetryLedger, run: TelemetryRun) -> None:
    _write(directory, replace(ledger, runs=tuple(run if item.run_id == run.run_id else item for item in ledger.runs)))


def _token(token_hex: Callable[[int], str]) -> str:
    try:
        return _pattern(token_hex(16), _ID)
    except Exception:
        raise SchemaError(_INVALID) from None


def create_run(cwd: Path, *, base_ref: str, runtime: str, round_number: int,
               started_at: str, started_monotonic_ns: int,
               token_hex: Callable[[int], str] = secrets.token_hex) -> TelemetryRun:
    run = TelemetryRun(
        _token(token_hex), runtime, round_number,
        TelemetryBinding("pending", None, base_ref, None, None, None, None, None, _contract()),
        started_at, None, started_monotonic_ns, None, None, False, False, None, (),
    )
    run = _parse_run(run.to_json())
    with _store(cwd) as directory:
        ledger = _read(directory)
        _require(all(item.run_id != run.run_id for item in ledger.runs))
        runs = ledger.runs
        if len(runs) == MAX_TELEMETRY_RUNS:
            candidates = [item for item in runs if (
                item.outcome not in (None, TelemetryOutcome.INCOMPLETE)
                and not item.telemetry_incomplete
                and all(span.outcome is not TelemetryOutcome.INCOMPLETE for span in item.spans)
            )]
            if not candidates:
                raise SchemaError(_TOO_LARGE)
            oldest = min(candidates, key=lambda item: item.started_at)
            runs = tuple(item for item in runs if item.run_id != oldest.run_id)
        _write(directory, replace(ledger, runs=(*runs, run)))
    return run


def record_candidate(cwd: Path, *, run_id: str, repository: str,
                     head_ref: str, head_sha: str) -> TelemetryRun:
    _require(all(isinstance(value, str) for value in (repository, head_ref, head_sha)))
    with _store(cwd) as directory:
        ledger = _read(directory)
        run = _find_run(ledger, run_id, active=True)
        _require(run.binding.status == "pending")
        for name, value in (("repository", repository), ("head_ref", head_ref), ("head_sha", head_sha)):
            _require(getattr(run.binding, name) in (None, value))
        binding = _parse_binding(replace(run.binding, repository=repository, head_ref=head_ref, head_sha=head_sha).to_json())
        updated = replace(run, binding=binding)
        _save_run(directory, ledger, updated)
        return updated


def bind_run(cwd: Path, *, run_id: str, snapshot: Snapshot) -> TelemetryRun:
    _require(isinstance(snapshot, Snapshot) and type(snapshot.schema) is int and snapshot.schema == 1)
    with _store(cwd) as directory:
        ledger = _read(directory)
        run = _find_run(ledger, run_id, active=True)
        _require(run.binding.status == "pending" and run.binding.base_ref == snapshot.base_ref)
        fields = ("repository", "base_ref", "base_sha", "head_ref", "head_sha", "merge_base_sha", "diff_sha256")
        # Candidate identity is provisional; capture_snapshot is authoritative.
        binding = _parse_binding(TelemetryBinding(
            status="bound", contract=_contract(), **{name: getattr(snapshot, name) for name in fields},
        ).to_json())
        updated = replace(run, binding=binding)
        _save_run(directory, ledger, updated)
        return updated


def start_span(cwd: Path, *, run_id: str, stage: TelemetryStage,
               reviewer: Reviewer | None, attempt: int, started_at: str,
               started_monotonic_ns: int,
               token_hex: Callable[[int], str] = secrets.token_hex) -> TelemetrySpan:
    _dimension(stage, reviewer)
    span = _parse_span(TelemetrySpan(
        _token(token_hex), stage, reviewer, attempt, started_at, None,
        started_monotonic_ns, None, None, None, None,
    ).to_json())
    with _store(cwd) as directory:
        ledger = _read(directory)
        run = _find_run(ledger, run_id, active=True)
        _require(all(item.span_id != span.span_id for item in run.spans))
        if len(run.spans) == MAX_TELEMETRY_SPANS_PER_RUN:
            _save_run(directory, ledger, replace(run, telemetry_incomplete=True, telemetry_incomplete_reason=_TOO_LARGE))
            raise SchemaError(_TOO_LARGE)
        _save_run(directory, ledger, replace(run, spans=(*run.spans, span)))
    return span


def _finish(span: TelemetrySpan, *, outcome: TelemetryOutcome, reason_code: str | None,
            ended_at: str, ended_monotonic_ns: int) -> TelemetrySpan:
    _require(span.outcome is None and isinstance(outcome, TelemetryOutcome))
    _status(outcome.value, reason_code)
    _timestamp(ended_at)
    _integer(ended_monotonic_ns)
    if ended_at < span.started_at or ended_monotonic_ns < span.started_monotonic_ns:
        outcome, reason_code = TelemetryOutcome.CLOCK_ANOMALY, _ANOMALY
    duration = None if outcome is TelemetryOutcome.CLOCK_ANOMALY else (ended_monotonic_ns - span.started_monotonic_ns) // 1_000_000
    return replace(span, outcome=outcome, reason_code=reason_code, ended_at=ended_at,
                   ended_monotonic_ns=ended_monotonic_ns, duration_ms=duration)


def finish_span(cwd: Path, *, run_id: str, span_id: str,
                outcome: TelemetryOutcome, reason_code: str | None,
                ended_at: str, ended_monotonic_ns: int) -> TelemetrySpan:
    _pattern(span_id, _ID)
    with _store(cwd) as directory:
        ledger = _read(directory)
        run = _find_run(ledger, run_id, active=True)
        span = next((item for item in run.spans if item.span_id == span_id), None)
        _require(span is not None)
        terminal = _finish(span, outcome=outcome, reason_code=reason_code,
                           ended_at=ended_at, ended_monotonic_ns=ended_monotonic_ns)
        updated = replace(run, spans=tuple(terminal if item.span_id == span_id else item for item in run.spans))
        _save_run(directory, ledger, updated)
        return terminal


def recover_run(cwd: Path, *, run_id: str, ended_at: str,
                ended_monotonic_ns: int) -> int:
    _timestamp(ended_at)
    _integer(ended_monotonic_ns)
    with _store(cwd) as directory:
        ledger = _read(directory)
        run = _find_run(ledger, run_id, active=True)
        running = sum(item.outcome is None for item in run.spans)
        if running:
            spans = tuple(
                _finish(item, outcome=TelemetryOutcome.INCOMPLETE,
                        reason_code="CONTROLLER_INTERRUPTED", ended_at=ended_at,
                        ended_monotonic_ns=ended_monotonic_ns)
                if item.outcome is None else item for item in run.spans
            )
            _save_run(directory, ledger, replace(run, spans=spans))
        return running


def close_run(cwd: Path, *, run_id: str, outcome: TelemetryOutcome,
              reason_code: str | None, ended_at: str) -> TelemetryRun:
    _require(isinstance(outcome, TelemetryOutcome))
    _status(outcome.value, reason_code)
    _timestamp(ended_at)
    with _store(cwd) as directory:
        ledger = _read(directory)
        run = _find_run(ledger, run_id, active=True)
        _require(all(item.outcome is not None for item in run.spans))
        if ended_at < run.started_at:
            outcome, reason_code = TelemetryOutcome.CLOCK_ANOMALY, _ANOMALY
        updated = replace(run, outcome=outcome, reason_code=reason_code, ended_at=ended_at)
        _save_run(directory, ledger, updated)
        return updated


def _total(spans: tuple[TelemetrySpan, ...]) -> int | None:
    if not spans or any(item.duration_ms is None for item in spans):
        return None
    return sum(item.duration_ms for item in spans)


def _elapsed(run: TelemetryRun, span: TelemetrySpan) -> int | None:
    if (run.started_late or span.outcome in (None, TelemetryOutcome.INCOMPLETE, TelemetryOutcome.CLOCK_ANOMALY)
            or span.ended_monotonic_ns is None
            or span.ended_monotonic_ns < run.started_monotonic_ns
            or span.ended_at < run.started_at):
        return None
    return (span.ended_monotonic_ns - run.started_monotonic_ns) // 1_000_000


def _early_detection(run: TelemetryRun) -> dict[str, object] | None:
    failures = [item for item in run.spans if item.stage is TelemetryStage.REPORT_VALIDATION and item.outcome is TelemetryOutcome.FAILURE]
    if not failures:
        return None
    failed = min(failures, key=lambda item: item.ended_monotonic_ns)
    detected = _elapsed(run, failed)
    milestones = []
    for reviewer in Reviewer:
        totals = [item for item in run.spans if item.stage is TelemetryStage.REVIEWER_TOTAL and item.reviewer is reviewer]
        latest = max(enumerate(totals), key=lambda pair: (pair[1].attempt, pair[0]))[1] if totals else None
        milestones.append(_elapsed(run, latest) if latest else None)
    terminal = max(milestones) if all(item is not None for item in milestones) else None
    delay = terminal - detected if terminal is not None and detected is not None and terminal >= detected else None
    return {
        "reviewer": failed.reviewer.value, "reason_code": failed.reason_code,
        "detected_elapsed_ms": detected, "all_reviewers_terminal_elapsed_ms": terminal,
        "wait_all_delay_ms": delay,
    }


def summarize_run(cwd: Path, *, run_id: str | None = None) -> dict[str, object]:
    ledger = read_ledger(cwd)
    _require(bool(ledger.runs))
    run = _find_run(ledger, run_id) if run_id is not None else ledger.runs[-1]
    stages = {}
    for stage in TelemetryStage:
        spans = tuple(item for item in run.spans if item.stage is stage)
        if spans:
            stages[stage.value] = {"count": len(spans), "total_ms": _total(spans)}
    return {
        "schema": TELEMETRY_SCHEMA_VERSION,
        "binding": {"contract": dict(run.binding.contract), "diff_sha256": run.binding.diff_sha256},
        "reviewers": {
            reviewer.value: {"total_ms": _total(tuple(item for item in run.spans if item.reviewer is reviewer and item.stage is TelemetryStage.REVIEWER_TOTAL))}
            for reviewer in Reviewer
        },
        "stages": stages,
        "outcomes": {outcome.value: sum(item.outcome is outcome for item in run.spans) for outcome in TelemetryOutcome},
        "telemetry_incomplete": run.telemetry_incomplete,
        "anomaly_reason_codes": sorted({item.reason_code for item in run.spans if item.outcome is TelemetryOutcome.CLOCK_ANOMALY}
                                       | ({run.reason_code} if run.outcome is TelemetryOutcome.CLOCK_ANOMALY else set())),
        "early_detection": _early_detection(run),
    }
