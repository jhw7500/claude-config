"""Private, descriptor-anchored atomic storage and tribunal round state."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .git_state import (
    DIFF_RECIPE_VERSION,
    GitStateError,
    assert_auto_fix_scope,
    capture_snapshot,
)
from . import model as m
from .attempt_store import (
    MAX_ATTEMPT_RAW_BYTES,
    OPERATIONAL_FAILURE_CODES,
    REPORT_RETRYABLE_CODES,
    append_attempt_evidence,
    preserve_legacy_report,
    reset_round_attempt_evidence,
)
from .model import (
    ContractBinding,
    Decision,
    GateStatus,
    GateSummary,
    ReportReceipt,
    Reviewer,
    ReviewerReport,
    ReviewerSlot,
    RoundSummary,
    SchemaError,
    Severity,
    Snapshot,
    Verdict,
)
from .review_context import context_sha256, current_contract_binding
from .review_store import (
    atomic_create_bytes,
    atomic_replace_bytes,
    check_ignored,
    locked_review,
    open_directory,
    preflight_review_directory,
    read_named_file,
    repository_root,
    safe_file,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(review_fd: int, verdict: Verdict) -> None:
    raw = json.dumps(
        verdict.to_json(), ensure_ascii=False, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")
    atomic_replace_bytes(
        review_fd,
        "verdict.json",
        raw,
        maximum=m.MAX_VERDICT_BYTES,
        too_large="VERDICT_TOO_LARGE",
        unsafe="VERDICT_FILE_UNSAFE",
        exact_mode=0o600,
        write_failed="VERDICT_WRITE_FAILED",
    )


def _parse_history(value: object) -> tuple[RoundSummary, ...]:
    items = m._array(value, 2, "VERDICT_INVALID")
    result: list[RoundSummary] = []
    for item in items:
        obj = m._object(
            item,
            {
                "round",
                "head_sha",
                "diff_sha256",
                "blocking_findings",
                "decision_outcomes",
            },
            "VERDICT_INVALID",
        )
        number = m._integer(obj["round"], "VERDICT_INVALID", minimum=1, maximum=3)
        head = obj["head_sha"]
        digest = obj["diff_sha256"]
        if (
            not isinstance(head, str)
            or m._SHA1.fullmatch(head) is None
            or not isinstance(digest, str)
            or m._SHA256.fullmatch(digest) is None
        ):
            raise SchemaError("VERDICT_INVALID")
        blockers: list[dict[str, str]] = []
        blocker_ids: set[str] = set()
        for raw_blocker in m._array(
            obj["blocking_findings"], m.MAX_FINDINGS_PER_REVIEWER * 3, "VERDICT_INVALID"
        ):
            blocker = m._object(
                raw_blocker, {"id", "reviewer", "severity"}, "VERDICT_INVALID"
            )
            identifier = blocker["id"]
            reviewer = blocker["reviewer"]
            severity = blocker["severity"]
            if (
                not isinstance(identifier, str)
                or (finding_match := m._FINDING_ID.fullmatch(identifier)) is None
                or not isinstance(reviewer, str)
                or reviewer not in {"A", "B", "C"}
                or finding_match.group(1) != reviewer
                or int(finding_match.group(2)) != number
                or not isinstance(severity, str)
                or severity not in {"CRITICAL", "HIGH"}
                or identifier in blocker_ids
            ):
                raise SchemaError("VERDICT_INVALID")
            blockers.append(dict(blocker))
            blocker_ids.add(identifier)
        outcomes: list[dict[str, object]] = []
        outcome_ids: set[str] = set()
        replacement_ids: set[str] = set()
        for raw_outcome in m._array(
            obj["decision_outcomes"], m.MAX_FINDINGS_PER_REVIEWER * 3, "VERDICT_INVALID"
        ):
            outcome = m._object(
                raw_outcome,
                {"decision_id", "outcome", "replacement_finding_id"},
                "VERDICT_INVALID",
            )
            response = m._parse_prior_response(outcome)
            decision_match = m._DECISION_ID.fullmatch(response.decision_id)
            if (
                decision_match is None
                or int(decision_match.group(1)) != number - 1
                or response.decision_id in outcome_ids
            ):
                raise SchemaError("VERDICT_INVALID")
            if response.outcome == "reissued":
                replacement = response.replacement_finding_id
                replacement_match = (
                    m._FINDING_ID.fullmatch(replacement)
                    if isinstance(replacement, str)
                    else None
                )
                if (
                    replacement_match is None
                    or int(replacement_match.group(2)) != number
                    or replacement_match.group(1) != decision_match.group(2)
                    or replacement not in blocker_ids
                    or replacement in replacement_ids
                ):
                    raise SchemaError("VERDICT_INVALID")
                replacement_ids.add(replacement)
            outcomes.append(response.to_json())
            outcome_ids.add(response.decision_id)
        if not blockers or (number == 1 and outcomes):
            raise SchemaError("VERDICT_INVALID")
        if number > 1:
            previous = result[-1] if result else None
            if previous is None or any(
                sum(
                    1
                    for outcome in outcomes
                    if m._DECISION_ID.fullmatch(outcome["decision_id"]).group(2)
                    == reviewer
                )
                != sum(
                    1
                    for blocker in previous.blocking_findings
                    if blocker["reviewer"] == reviewer
                )
                for reviewer in "ABC"
            ):
                raise SchemaError("VERDICT_INVALID")
        result.append(
            RoundSummary(number, head, digest, tuple(blockers), tuple(outcomes))
        )
    if any(
        result[index].round >= result[index + 1].round
        for index in range(len(result) - 1)
    ):
        raise SchemaError("VERDICT_INVALID")
    return tuple(result)


@dataclass(frozen=True)
class _VerdictFields:
    schema: int
    repository: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    merge_base_sha: str
    diff_sha256: str
    initial_paths: tuple[str, ...]
    round: int
    producer_runtime: str
    reviewers: dict[str, object]
    decisions: tuple[Decision, ...]
    history: tuple[RoundSummary, ...]
    gate: GateSummary
    created_at: str
    snapshot: Snapshot


def _parse_verdict_fields(data: dict[str, object], *, schema: int) -> _VerdictFields:
    verdict_keys = {
        "schema",
        "repository",
        "base",
        "head_ref",
        "head_sha",
        "merge_base_sha",
        "diff_sha256",
        "initial_paths",
        "round",
        "producer_runtime",
        "reviewers",
        "decisions",
        "history",
        "gate",
        "created_at",
    }
    if schema == m.VERDICT_SCHEMA_VERSION:
        verdict_keys.add("contract")
        valid_shapes = {frozenset(verdict_keys)}
    else:
        valid_shapes = {
            frozenset(verdict_keys),
            frozenset(verdict_keys - {"head_ref"}),
        }
    if set(data) not in valid_shapes or data["schema"] != schema:
        raise SchemaError("VERDICT_INVALID")

    repository = m._text(data["repository"], 256)
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+", repository)
        is None
    ):
        raise SchemaError("VERDICT_INVALID")
    base = m._object(data["base"], {"ref", "sha"}, "VERDICT_INVALID")
    base_ref = m._text(base["ref"], 256)
    head_ref = m._head_ref(data["head_ref"]) if "head_ref" in data else ""
    base_sha, head_sha, merge_base_sha, digest = (
        base["sha"],
        data["head_sha"],
        data["merge_base_sha"],
        data["diff_sha256"],
    )
    if (
        any(
            not isinstance(value, str) or m._SHA1.fullmatch(value) is None
            for value in (base_sha, head_sha, merge_base_sha)
        )
        or not isinstance(digest, str)
        or m._SHA256.fullmatch(digest) is None
    ):
        raise SchemaError("VERDICT_INVALID")
    initial_paths = tuple(
        m._path(item)
        for item in m._array(
            data["initial_paths"], m.MAX_INITIAL_PATHS, "VERDICT_INVALID"
        )
    )
    if len(initial_paths) != len(set(initial_paths)):
        raise SchemaError("VERDICT_INVALID")
    round_number = m._integer(
        data["round"], "VERDICT_INVALID", minimum=1, maximum=3
    )
    runtime = data["producer_runtime"]
    if not isinstance(runtime, str) or runtime not in {"claude", "codex"}:
        raise SchemaError("VERDICT_INVALID")
    created_at = m._text(data["created_at"], 64)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created_at) is None:
            raise ValueError
        datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise SchemaError("VERDICT_INVALID") from None

    reviewers = m._object(data["reviewers"], {"A", "B", "C"}, "VERDICT_INVALID")
    history = _parse_history(data["history"])
    if tuple(item.round for item in history) != tuple(range(1, round_number)):
        raise SchemaError("VERDICT_INVALID")
    prior_ids = tuple(
        item["id"] for item in (history[-1].blocking_findings if history else ())
    )
    decisions = m.parse_decisions(
        json.dumps(data["decisions"], ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        prior_blockers=prior_ids,
    )
    if round_number == 1 and decisions:
        raise SchemaError("VERDICT_INVALID")
    if any(item.disposition == "fixed" for item in decisions) and (
        not history or head_sha == history[-1].head_sha
    ):
        raise SchemaError("FIXED_HEAD_UNCHANGED")
    gate_obj = m._object(
        data["gate"], {"status", "blocking_count"}, "VERDICT_INVALID"
    )
    status = m._enum(GateStatus, gate_obj["status"], "VERDICT_INVALID")
    count = m._integer(
        gate_obj["blocking_count"],
        "VERDICT_INVALID",
        minimum=0,
        maximum=m.MAX_FINDINGS_PER_REVIEWER * 3,
    )
    snapshot = Snapshot(
        m.SCHEMA_VERSION,
        repository,
        base_ref,
        base_sha,
        head_ref,
        head_sha,
        merge_base_sha,
        digest,
        (),
        initial_paths,
        created_at,
    )
    return _VerdictFields(
        schema,
        repository,
        base_ref,
        base_sha,
        head_ref,
        head_sha,
        merge_base_sha,
        digest,
        initial_paths,
        round_number,
        runtime,
        reviewers,
        decisions,
        history,
        GateSummary(status, count),
        created_at,
        snapshot,
    )


def _parse_embedded_report(
    value: object, *, reviewer: str, fields: _VerdictFields
) -> ReviewerReport:
    completed = m._object(
        value,
        {"status", "findings", "executions", "claims", "prior_decisions"},
        "VERDICT_INVALID",
    )
    report_value = {
        "schema": m.SCHEMA_VERSION,
        "reviewer": reviewer,
        "round": fields.round,
        "snapshot": {
            "head_sha": fields.head_sha,
            "diff_sha256": fields.diff_sha256,
        },
        **completed,
    }
    return m.parse_reviewer_report(
        json.dumps(
            report_value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"),
        expected_reviewer=Reviewer(reviewer),
        expected_round=fields.round,
        snapshot=fields.snapshot,
    )


def _blocking_count(reviewers: Mapping[str, ReviewerSlot]) -> int:
    return sum(
        1
        for slot in reviewers.values()
        if slot.report is not None
        for finding in slot.report.findings
        if finding.severity in {Severity.CRITICAL, Severity.HIGH}
    )


def _verdict_from_fields(
    fields: _VerdictFields,
    reviewers: Mapping[str, ReviewerSlot],
    *,
    contract: ContractBinding | None = None,
) -> Verdict:
    return Verdict(
        fields.schema,
        fields.repository,
        fields.base_ref,
        fields.base_sha,
        fields.head_ref,
        fields.head_sha,
        fields.merge_base_sha,
        fields.diff_sha256,
        fields.initial_paths,
        fields.round,
        fields.producer_runtime,
        reviewers,
        fields.decisions,
        fields.history,
        fields.gate,
        fields.created_at,
        contract,
    )


def _parse_verdict_v1(data: dict[str, object]) -> Verdict:
    fields = _parse_verdict_fields(data, schema=m.SCHEMA_VERSION)
    reviewers: dict[str, ReviewerSlot] = {}
    for key in "ABC":
        slot = fields.reviewers[key]
        if (
            isinstance(slot, dict)
            and set(slot) == {"status"}
            and slot["status"] == "pending"
        ):
            reviewers[key] = ReviewerSlot("pending")
        else:
            reviewers[key] = ReviewerSlot(
                "complete", _parse_embedded_report(slot, reviewer=key, fields=fields)
            )
    pending_count = sum(slot.status == "pending" for slot in reviewers.values())
    if pending_count not in {0, 3}:
        raise SchemaError("VERDICT_INVALID")
    complete = pending_count == 0
    status = fields.gate.status
    count = fields.gate.blocking_count
    if (status is GateStatus.IN_PROGRESS) != (not complete):
        raise SchemaError("VERDICT_INVALID")
    actual = _blocking_count(reviewers)
    if status is GateStatus.IN_PROGRESS and count != 0:
        raise SchemaError("VERDICT_INVALID")
    if status is not GateStatus.IN_PROGRESS and count != actual:
        raise SchemaError("VERDICT_INVALID")
    if (status is GateStatus.PASS) != (complete and count == 0):
        raise SchemaError("VERDICT_INVALID")
    verdict = _verdict_from_fields(fields, reviewers)
    if complete:
        _validate_closure(
            verdict,
            {key: reviewers[key].report for key in "ABC"},
        )
    return verdict


def _parse_contract(value: object) -> ContractBinding:
    obj = m._object(
        value,
        {"report_text", "diff_recipe", "verdict_schema"},
        "VERDICT_INVALID",
    )
    contract = ContractBinding(
        m._integer(obj["report_text"], "VERDICT_INVALID", minimum=1),
        m._integer(obj["diff_recipe"], "VERDICT_INVALID", minimum=1),
        m._integer(obj["verdict_schema"], "VERDICT_INVALID", minimum=1),
    )
    return contract


def _parse_v2_receipt(
    value: object,
    *,
    reviewer: str,
    fields: _VerdictFields,
    contract: ContractBinding,
    attempt_count: int,
) -> ReportReceipt:
    obj = m._object(
        value,
        {
            "raw_sha256",
            "context_sha256",
            "report_contract_version",
            "attempt",
            "provenance",
        },
        "VERDICT_INVALID",
    )
    raw_sha256 = obj["raw_sha256"]
    context_sha256 = obj["context_sha256"]
    if (
        not isinstance(raw_sha256, str)
        or m._SHA256.fullmatch(raw_sha256) is None
        or not isinstance(context_sha256, str)
        or m._SHA256.fullmatch(context_sha256) is None
    ):
        raise SchemaError("VERDICT_INVALID")
    report_contract_version = m._integer(
        obj["report_contract_version"], "VERDICT_INVALID", minimum=1
    )
    attempt = m._integer(
        obj["attempt"], "VERDICT_INVALID", minimum=1
    )
    provenance = obj["provenance"]
    if (
        report_contract_version != contract.report_text
        or attempt != attempt_count
        or not isinstance(provenance, str)
        or provenance not in m.RECEIPT_PROVENANCE
    ):
        raise SchemaError("VERDICT_INVALID")
    return ReportReceipt(
        Reviewer(reviewer),
        fields.round,
        f".review/inbox/round-{fields.round}/{reviewer}.json",
        raw_sha256,
        context_sha256,
        report_contract_version,
        attempt,
        provenance,
    )


def _parse_verdict_v2(data: dict[str, object]) -> Verdict:
    fields = _parse_verdict_fields(data, schema=m.VERDICT_SCHEMA_VERSION)
    contract = _parse_contract(data["contract"])
    reviewers: dict[str, ReviewerSlot] = {}
    for key in "ABC":
        raw_slot = fields.reviewers[key]
        if not isinstance(raw_slot, dict):
            raise SchemaError("VERDICT_INVALID")
        state = raw_slot.get("state")
        expected_keys = (
            {"state", "attempt_count", "last_error"}
            if state == "pending"
            else {"state", "report", "receipt", "attempt_count", "last_error"}
        )
        slot = m._object(raw_slot, expected_keys, "VERDICT_INVALID")
        attempt_count = m._integer(
            slot["attempt_count"], "VERDICT_INVALID", minimum=0
        )
        last_error = slot["last_error"]
        if last_error is not None:
            last_error = m._text(last_error, 128)
        if state == "pending":
            reviewers[key] = ReviewerSlot(
                "pending", attempt_count=attempt_count, last_error=last_error
            )
            continue
        if state != "sealed" or attempt_count < 1 or last_error is not None:
            raise SchemaError("VERDICT_INVALID")
        report = _parse_embedded_report(slot["report"], reviewer=key, fields=fields)
        receipt = _parse_v2_receipt(
            slot["receipt"],
            reviewer=key,
            fields=fields,
            contract=contract,
            attempt_count=attempt_count,
        )
        reviewers[key] = ReviewerSlot(
            "sealed", report, receipt, attempt_count, None
        )

    status = fields.gate.status
    count = fields.gate.blocking_count
    all_sealed = all(slot.status == "sealed" for slot in reviewers.values())
    if status is GateStatus.IN_PROGRESS:
        if count != 0:
            raise SchemaError("VERDICT_INVALID")
    else:
        actual = _blocking_count(reviewers)
        if not all_sealed or count != actual:
            raise SchemaError("VERDICT_INVALID")
        if (status is GateStatus.PASS) != (count == 0):
            raise SchemaError("VERDICT_INVALID")
    verdict = _verdict_from_fields(fields, reviewers, contract=contract)
    if status is not GateStatus.IN_PROGRESS:
        _validate_closure(
            verdict,
            {key: reviewers[key].report for key in "ABC"},
        )
    return verdict


def _parse_verdict(raw: bytes) -> Verdict:
    data = m._load_json(raw, limit=m.MAX_VERDICT_BYTES, too_large="VERDICT_TOO_LARGE")
    if not isinstance(data, dict) or type(data.get("schema")) is not int:
        raise SchemaError("VERDICT_INVALID")
    if data["schema"] == m.SCHEMA_VERSION:
        return _parse_verdict_v1(data)
    if data["schema"] == m.VERDICT_SCHEMA_VERSION:
        return _parse_verdict_v2(data)
    raise SchemaError("VERDICT_SCHEMA_UNSUPPORTED")


def _read_verdict_locked(review_fd: int) -> Verdict:
    raw = read_named_file(
        review_fd,
        "verdict.json",
        maximum=m.MAX_VERDICT_BYTES,
        missing="VERDICT_MISSING",
        unsafe="VERDICT_FILE_UNSAFE",
    )
    return _parse_verdict(raw)


def _read_optional_verdict_locked(review_fd: int) -> Verdict | None:
    try:
        return _read_verdict_locked(review_fd)
    except SchemaError as error:
        if error.code == "VERDICT_MISSING":
            return None
        raise


def _expected_input(root: Path, supplied: Path, relative: str, code: str) -> None:
    try:
        raw = os.fspath(supplied)
    except (TypeError, ValueError, OSError):
        raise SchemaError(code) from None
    if (
        not isinstance(raw, str)
        or "\\" in raw
        or "//" in raw
        or any(
            part in {"", ".", ".."}
            for part in raw.split("/")
            if not (raw.startswith("/") and part == "")
        )
    ):
        raise SchemaError(code)
    expected_absolute = str(root / relative)
    if raw not in {relative, expected_absolute}:
        raise SchemaError(code)


def _exact_report_directory(fd: int) -> None:
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            raise SchemaError("FILE_UNSAFE")
    except OSError:
        raise SchemaError("FILE_UNSAFE") from None


def _round_fd(
    review_fd: int,
    round_number: int,
    *,
    create: bool,
    exact_report_directories: bool = False,
) -> int:
    inbox_code = (
        "FILE_UNSAFE" if exact_report_directories else "INBOX_DIRECTORY_UNSAFE"
    )
    round_code = (
        "FILE_UNSAFE" if exact_report_directories else "ROUND_DIRECTORY_UNSAFE"
    )
    inbox_fd = open_directory(
        review_fd,
        "inbox",
        create=create,
        code=inbox_code,
    )
    try:
        if exact_report_directories:
            _exact_report_directory(inbox_fd)
        round_fd = open_directory(
            inbox_fd,
            f"round-{round_number}",
            create=create,
            code=round_code,
        )
        try:
            if exact_report_directories:
                _exact_report_directory(round_fd)
            return round_fd
        except BaseException:
            os.close(round_fd)
            raise
    finally:
        os.close(inbox_fd)


def _invalidate_round_inputs(review_fd: int, round_number: int) -> None:
    round_fd = _round_fd(review_fd, round_number, create=True)
    try:
        changed = False
        for basename in ("A.json", "B.json", "C.json", "decisions.json"):
            try:
                file_fd = os.open(
                    basename,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=round_fd,
                )
            except FileNotFoundError:
                continue
            except OSError:
                raise SchemaError("FILE_UNSAFE") from None
            try:
                opened = safe_file(file_fd, "FILE_UNSAFE")
                named = os.stat(basename, dir_fd=round_fd, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise SchemaError("FILE_UNSAFE")
            except SchemaError:
                raise
            except OSError:
                raise SchemaError("FILE_UNSAFE") from None
            finally:
                os.close(file_fd)
            try:
                os.unlink(basename, dir_fd=round_fd)
            except OSError:
                raise SchemaError("FILE_UNSAFE") from None
            changed = True
        if changed:
            os.fsync(round_fd)
    finally:
        os.close(round_fd)


def _read_input(
    root: Path,
    review_fd: int,
    supplied: Path,
    *,
    round_number: int,
    basename: str,
    maximum: int,
    missing: str,
    path_code: str,
    exact_mode: int | None = None,
) -> bytes:
    relative = f".review/inbox/round-{round_number}/{basename}"
    _expected_input(root, supplied, relative, path_code)
    round_fd = _round_fd(review_fd, round_number, create=False)
    try:
        return read_named_file(
            round_fd,
            basename,
            maximum=maximum,
            missing=missing,
            unsafe="FILE_UNSAFE",
            exact_mode=exact_mode,
        )
    finally:
        os.close(round_fd)


def _snapshot_equal(verdict: Verdict, snapshot: Snapshot) -> bool:
    return (
        verdict.repository,
        verdict.base_ref,
        verdict.base_sha,
        verdict.head_ref,
        verdict.head_sha,
        verdict.merge_base_sha,
        verdict.diff_sha256,
    ) == (
        snapshot.repository,
        snapshot.base_ref,
        snapshot.base_sha,
        snapshot.head_ref,
        snapshot.head_sha,
        snapshot.merge_base_sha,
        snapshot.diff_sha256,
    )


def _require_all_pending(verdict: Verdict) -> None:
    if verdict.gate.status is not GateStatus.IN_PROGRESS or any(
        slot.status != "pending" for slot in verdict.reviewers.values()
    ):
        raise SchemaError("ROUND_NOT_IN_PROGRESS")


def require_v2_in_progress(verdict: Verdict) -> Verdict:
    if verdict.schema == m.SCHEMA_VERSION:
        raise SchemaError("LEGACY_ADOPTION_REQUIRED")
    if (
        verdict.schema != m.VERDICT_SCHEMA_VERSION
        or verdict.gate.status is not GateStatus.IN_PROGRESS
    ):
        raise SchemaError("ROUND_NOT_IN_PROGRESS")
    return verdict


def _validate_report_store_input(reviewer: Reviewer, raw: bytes) -> None:
    if not isinstance(reviewer, Reviewer) or not isinstance(raw, bytes):
        raise SchemaError("REPORT_SCHEMA_INVALID")
    if len(raw) > m.MAX_REPORT_BYTES:
        raise SchemaError("REPORT_TOO_LARGE")


def _pending_slot_snapshot(
    root: Path, pending: Verdict, reviewer: Reviewer, *, now: Callable[[], str]
) -> Snapshot:
    require_v2_in_progress(pending)
    if pending.contract != current_contract_binding():
        raise SchemaError("CONTRACT_DRIFT")
    if pending.reviewers[reviewer.value].status != "pending":
        raise SchemaError("REVIEWER_SLOT_SEALED")
    snapshot = capture_snapshot(root, pending.base_ref, now=now)
    if not _snapshot_equal(pending, snapshot):
        raise SchemaError("SNAPSHOT_CHANGED")
    return snapshot


def _record_failure_locked(
    review_fd: int, pending: Verdict, reviewer: Reviewer, reason_code: str,
    raw: bytes | None,
) -> ReviewerSlot:
    slot = ReviewerSlot(
        "pending", attempt_count=pending.reviewers[reviewer.value].attempt_count + 1,
        last_error=reason_code,
    )
    append_attempt_evidence(
        review_fd, round_number=pending.round, reviewer=reviewer,
        sequence=slot.attempt_count, reason_code=reason_code, raw=raw,
    )
    _atomic_write(review_fd, replace(
        pending, reviewers={**pending.reviewers, reviewer.value: slot},
    ))
    return slot


def submit_reviewer_report(
    cwd: Path, *, reviewer: Reviewer, raw: bytes, now: Callable[[], str] = utc_now,
) -> ReportReceipt:
    """Validate exact bytes, publish the canonical report, and seal one v2 slot."""
    if not isinstance(reviewer, Reviewer) or not isinstance(raw, bytes):
        raise SchemaError("REPORT_SCHEMA_INVALID")
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        pending = _read_verdict_locked(review_fd)
        snapshot = _pending_slot_snapshot(root, pending, reviewer, now=now)
        context_digest = context_sha256(pending, reviewer)
        round_fd = _round_fd(
            review_fd, pending.round, create=True, exact_report_directories=True,
        )
        try:
            name = f"{reviewer.value}.json"
            try:
                existing = read_named_file(
                    round_fd, name, maximum=m.MAX_REPORT_BYTES,
                    missing="REVIEWER_REPORT_MISSING", unsafe="FILE_UNSAFE", exact_mode=0o600,
                )
            except SchemaError as error:
                if error.code != "REVIEWER_REPORT_MISSING":
                    raise
                existing = None
            if existing is not None:
                # A canonical publication can precede a failed verdict write.
                # Its validation is an integrity boundary, outside retry handling.
                parsed, digest = m.validate_report_bytes(
                    existing, expected_reviewer=reviewer, expected_round=pending.round,
                    snapshot=snapshot,
                )
                _validate_reviewer_closure(pending, parsed)
            else:
                if len(raw) > MAX_ATTEMPT_RAW_BYTES:
                    raise SchemaError("REPORT_TOO_LARGE")
                try:
                    parsed, digest = m.validate_report_bytes(
                        raw, expected_reviewer=reviewer, expected_round=pending.round,
                        snapshot=snapshot,
                    )
                    _validate_reviewer_closure(pending, parsed)
                except SchemaError as error:
                    if error.code in REPORT_RETRYABLE_CODES:
                        _record_failure_locked(review_fd, pending, reviewer, error.code, raw)
                    raise
                atomic_create_bytes(
                    round_fd, name, raw, maximum=m.MAX_REPORT_BYTES,
                    exists="REPORT_FILE_EXISTS", unsafe="FILE_UNSAFE", exact_mode=0o600,
                    write_failed="REPORT_WRITE_FAILED",
                )
        finally:
            os.close(round_fd)
        attempt = pending.reviewers[reviewer.value].attempt_count + 1
        receipt = ReportReceipt(
            reviewer, pending.round, f".review/inbox/round-{pending.round}/{reviewer.value}.json",
            digest, context_digest, m.REPORT_TEXT_CONTRACT_VERSION, attempt, "native_submit",
        )
        slot = ReviewerSlot("sealed", parsed, receipt, attempt_count=attempt)
        _atomic_write(review_fd, replace(
            pending, reviewers={**pending.reviewers, reviewer.value: slot},
        ))
        return receipt


def record_reviewer_failure(
    cwd: Path, *, reviewer: Reviewer, reason_code: str,
) -> ReviewerSlot:
    """Persist one bounded operational failure for a still-pending v2 slot."""
    if (
        not isinstance(reviewer, Reviewer)
        or not isinstance(reason_code, str)
        or reason_code not in OPERATIONAL_FAILURE_CODES
    ):
        raise SchemaError("REVIEWER_FAILURE_INVALID")
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        pending = _read_verdict_locked(review_fd)
        _pending_slot_snapshot(root, pending, reviewer, now=utc_now)
        return _record_failure_locked(review_fd, pending, reviewer, reason_code, None)


def store_reviewer_report(
    cwd: Path,
    *,
    reviewer: Reviewer,
    raw: bytes,
    replace_pending_recovery: bool = False,
) -> ReportReceipt:
    """Store exact report bytes at the pending round's only canonical path."""
    _validate_report_store_input(reviewer, raw)
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        pending = _read_verdict_locked(review_fd)
        if pending.schema != 1:
            raise SchemaError("LEGACY_COMMAND_NOT_ALLOWED")
        _require_all_pending(pending)
        if replace_pending_recovery:
            snapshot = capture_snapshot(root, pending.base_ref)
            if not _snapshot_equal(pending, snapshot):
                raise SchemaError("SNAPSHOT_CHANGED")
        round_fd = _round_fd(
            review_fd,
            pending.round,
            create=True,
            exact_report_directories=True,
        )
        try:
            name = f"{reviewer.value}.json"
            if replace_pending_recovery:
                atomic_replace_bytes(
                    round_fd,
                    name,
                    raw,
                    maximum=m.MAX_REPORT_BYTES,
                    too_large="REPORT_TOO_LARGE",
                    unsafe="FILE_UNSAFE",
                    exact_mode=0o600,
                    write_failed="REPORT_WRITE_FAILED",
                )
                digest = hashlib.sha256(raw).hexdigest()
            else:
                digest = atomic_create_bytes(
                    round_fd,
                    name,
                    raw,
                    maximum=m.MAX_REPORT_BYTES,
                    exists="REPORT_FILE_EXISTS",
                    unsafe="FILE_UNSAFE",
                    exact_mode=0o600,
                )
        finally:
            os.close(round_fd)
    return ReportReceipt(
        reviewer,
        pending.round,
        f".review/inbox/round-{pending.round}/{reviewer.value}.json",
        digest,
    )


def validate_stored_reviewer_report(
    cwd: Path, *, reviewer: Reviewer
) -> tuple[ReviewerReport, str]:
    """Re-open and validate one canonical exact-mode report without mutation."""
    if not isinstance(reviewer, Reviewer):
        raise SchemaError("REPORT_SCHEMA_INVALID")
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        verdict = _read_verdict_locked(review_fd)
        if verdict.schema == m.SCHEMA_VERSION:
            _require_all_pending(verdict)
        elif verdict.schema == m.VERDICT_SCHEMA_VERSION:
            if verdict.contract != current_contract_binding():
                raise SchemaError("CONTRACT_DRIFT")
            slot = verdict.reviewers[reviewer.value]
            if slot.status == "pending" and verdict.gate.status is not GateStatus.IN_PROGRESS:
                raise SchemaError("ROUND_NOT_IN_PROGRESS")
        else:
            raise SchemaError("VERDICT_INVALID")
        snapshot = capture_snapshot(root, verdict.base_ref)
        if not _snapshot_equal(verdict, snapshot):
            raise SchemaError("SNAPSHOT_CHANGED")
        if verdict.schema == m.VERDICT_SCHEMA_VERSION and slot.status == "sealed":
            parsed = _read_sealed_report(review_fd, verdict, reviewer)
            if slot.receipt is None:
                raise SchemaError("VERDICT_INVALID")
            return parsed, slot.receipt.raw_sha256
        round_fd = _round_fd(review_fd, verdict.round, create=False)
        try:
            raw = read_named_file(
                round_fd,
                f"{reviewer.value}.json",
                maximum=m.MAX_REPORT_BYTES,
                missing="REVIEWER_REPORT_MISSING",
                unsafe="FILE_UNSAFE",
                exact_mode=0o600,
            )
        finally:
            os.close(round_fd)
        return m.validate_report_bytes(
            raw,
            expected_reviewer=reviewer,
            expected_round=verdict.round,
            snapshot=snapshot,
        )


def _blockers(verdict: Verdict) -> tuple[m.Finding, ...]:
    return tuple(
        finding
        for slot in verdict.reviewers.values()
        if slot.report
        for finding in slot.report.findings
        if finding.severity in {Severity.CRITICAL, Severity.HIGH}
    )


def _summary(verdict: Verdict) -> RoundSummary:
    blockers = tuple(
        {
            "id": item.id,
            "reviewer": item.reviewer.value,
            "severity": item.severity.value,
        }
        for item in _blockers(verdict)
    )
    outcomes = tuple(
        response.to_json()
        for key in "ABC"
        for response in (
            verdict.reviewers[key].report.prior_decisions
            if verdict.reviewers[key].report
            else ()
        )
    )
    return RoundSummary(
        verdict.round, verdict.head_sha, verdict.diff_sha256, blockers, outcomes
    )


def _new_v2_pending(
    snapshot: Snapshot,
    *,
    runtime: str,
    initial_paths: Sequence[str],
    round_number: int,
    decisions: Sequence[Decision],
    history: Sequence[RoundSummary],
    contract: ContractBinding,
) -> Verdict:
    return Verdict(
        m.VERDICT_SCHEMA_VERSION,
        snapshot.repository,
        snapshot.base_ref,
        snapshot.base_sha,
        snapshot.head_ref,
        snapshot.head_sha,
        snapshot.merge_base_sha,
        snapshot.diff_sha256,
        tuple(initial_paths),
        round_number,
        runtime,
        {key: ReviewerSlot("pending") for key in "ABC"},
        tuple(decisions),
        tuple(history),
        GateSummary(GateStatus.IN_PROGRESS, 0),
        snapshot.created_at,
        contract,
    )


def begin_round(
    cwd: Path,
    *,
    base: str,
    runtime: str,
    round_number: int,
    decisions_path: Path | None = None,
    now: Callable[[], str] = utc_now,
) -> Verdict:
    if (
        not isinstance(round_number, int)
        or isinstance(round_number, bool)
        or round_number < 1
        or round_number > 3
    ):
        raise SchemaError(
            "ROUND_LIMIT_EXHAUSTED" if round_number == 4 else "ROUND_INVALID"
        )
    if runtime not in {"claude", "codex"}:
        raise SchemaError("RUNTIME_INVALID")
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=True) as review_fd:
        stored = _read_optional_verdict_locked(review_fd)
        if stored is not None and stored.gate.status is GateStatus.IN_PROGRESS:
            raise SchemaError("ROUND_TRANSITION_INVALID")
        previous: Verdict | None = None
        if round_number == 1:
            if decisions_path is not None:
                raise SchemaError("DECISIONS_NOT_ALLOWED")
            if stored is not None and stored.gate.status is GateStatus.FAIL:
                if stored.round == 3:
                    raise SchemaError("ROUND_LIMIT_EXHAUSTED")
                raise SchemaError("ROUND_TRANSITION_INVALID")
        else:
            if stored is None:
                raise SchemaError("VERDICT_MISSING")
            previous = stored
            if previous.round == 3:
                raise SchemaError("ROUND_LIMIT_EXHAUSTED")
            if (
                previous.gate.status is not GateStatus.FAIL
                or round_number != previous.round + 1
            ):
                raise SchemaError("ROUND_TRANSITION_INVALID")
            if base != previous.base_ref:
                raise SchemaError("BASE_CHANGED")
            if decisions_path is None:
                raise SchemaError("DECISIONS_REQUIRED")
        snapshot = capture_snapshot(root, base, now=now)
        if previous is None:
            initial_paths = tuple(snapshot.initial_paths)
            decisions: tuple[Decision, ...] = ()
            history: tuple[RoundSummary, ...] = ()
        else:
            if (
                snapshot.repository != previous.repository
                or snapshot.base_ref != previous.base_ref
                or snapshot.base_sha != previous.base_sha
                or (
                    previous.head_ref
                    and snapshot.head_ref != previous.head_ref
                )
            ):
                raise SchemaError("REPOSITORY_OR_BASE_CHANGED")
            try:
                assert_auto_fix_scope(previous.initial_paths, snapshot.initial_paths)
            except GitStateError:
                raise SchemaError("AUTO_FIX_SCOPE_VIOLATION") from None
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
            decisions = m.parse_decisions(raw, prior_blockers=_blockers(previous))
            if (
                any(item.disposition == "fixed" for item in decisions)
                and snapshot.head_sha == previous.head_sha
            ):
                raise SchemaError("FIXED_HEAD_UNCHANGED")
            initial_paths = tuple(previous.initial_paths)
            history = tuple((*previous.history, _summary(previous))[-2:])
        # Without a terminal predecessor, no old fixed namespace has proven
        # completion. Check all three before a future transition could reuse one.
        for target_round in ((round_number,) if stored is not None else (1, 2, 3)):
            reset_round_attempt_evidence(
                review_fd, round_number=target_round, allow_reset=stored is not None,
            )
        _invalidate_round_inputs(review_fd, round_number)
        pending = _new_v2_pending(
            snapshot, runtime=runtime, initial_paths=initial_paths,
            round_number=round_number, decisions=decisions, history=history,
            contract=current_contract_binding(),
        )
        _atomic_write(review_fd, pending)
        return pending


def _validate_reviewer_closure(
    verdict: Verdict, report: ReviewerReport, *, seen_replacements: set[str] | None = None,
) -> None:
    """Check only this role's responses before accepting its immutable report."""
    decisions = {
        item.id: item for item in verdict.decisions if item.reviewer is report.reviewer
    }
    responses = {item.decision_id: item for item in report.prior_decisions}
    if set(responses) != set(decisions):
        if not set(decisions).issubset(responses):
            raise SchemaError("PRIOR_DECISION_RESPONSE_MISSING")
        raise SchemaError("PRIOR_DECISION_RESPONSE_INVALID")
    if seen_replacements is None:
        seen_replacements = set()
    findings = {item.id: item for item in report.findings}
    for response in responses.values():
        decision = decisions[response.decision_id]
        if response.outcome == "accepted":
            continue
        replacement = response.replacement_finding_id
        if replacement is None:
            raise SchemaError("REPLACEMENT_FINDING_REQUIRED")
        finding = findings.get(replacement)
        if (
            finding is None
            or finding.severity not in {Severity.CRITICAL, Severity.HIGH}
            or replacement == decision.finding_id
            or replacement in seen_replacements
        ):
            raise SchemaError("REPLACEMENT_FINDING_INVALID")
        seen_replacements.add(replacement)


def _validate_closure(verdict: Verdict, reports: Mapping[str, ReviewerReport]) -> None:
    seen_replacements: set[str] = set()
    for reviewer in "ABC":
        _validate_reviewer_closure(
            verdict, reports[reviewer], seen_replacements=seen_replacements,
        )
    if verdict.round == 1 and any(
        report.prior_decisions for report in reports.values()
    ):
        raise SchemaError("PRIOR_DECISION_RESPONSE_INVALID")


def _read_sealed_report(
    review_fd: int, verdict: Verdict, reviewer: Reviewer
) -> ReviewerReport:
    slot = verdict.reviewers[reviewer.value]
    if slot.status != "sealed" or slot.report is None or slot.receipt is None:
        raise SchemaError("ROUND_NOT_READY")
    round_fd = _round_fd(
        review_fd, verdict.round, create=False, exact_report_directories=True
    )
    try:
        raw = read_named_file(
            round_fd, f"{reviewer.value}.json", maximum=m.MAX_REPORT_BYTES,
            missing="REVIEWER_REPORT_MISSING", unsafe="FILE_UNSAFE", exact_mode=0o600,
        )
    finally:
        os.close(round_fd)
    if hashlib.sha256(raw).hexdigest() != slot.receipt.raw_sha256:
        raise SchemaError("REPORT_BYTES_MISMATCH")
    parsed, digest = m.validate_report_bytes(
        raw, expected_reviewer=reviewer,
        expected_round=verdict.round, snapshot=verdict.snapshot,
    )
    if digest != slot.receipt.raw_sha256 or parsed != slot.report:
        raise SchemaError("REPORT_RECEIPT_MISMATCH")
    if slot.receipt.context_sha256 != context_sha256(verdict, reviewer):
        raise SchemaError("CONTEXT_DRIFT")
    return parsed


def finalize_round(
    cwd: Path, *, reviewer_paths: Mapping[str, Path] | None = None,
    now: Callable[[], str] = utc_now,
) -> Verdict:
    if reviewer_paths is not None and (
        not isinstance(reviewer_paths, Mapping) or set(reviewer_paths) != set("ABC")
    ):
        raise SchemaError("REVIEWER_REPORT_MISSING")
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        pending = _read_verdict_locked(review_fd)
        require_v2_in_progress(pending)
        if pending.contract != current_contract_binding():
            raise SchemaError("CONTRACT_DRIFT")
        if reviewer_paths is not None:
            for key in "ABC":
                _expected_input(
                    root, reviewer_paths[key],
                    f".review/inbox/round-{pending.round}/{key}.json", "REPORT_PATH_INVALID",
                )
        snapshot = capture_snapshot(root, pending.base_ref, now=now)
        if not _snapshot_equal(pending, snapshot):
            raise SchemaError("SNAPSHOT_CHANGED")
        reports: dict[str, ReviewerReport] = {}
        for key in "ABC":
            reports[key] = _read_sealed_report(review_fd, pending, Reviewer(key))
        _validate_closure(pending, reports)
        blockers = tuple(
            finding
            for report in reports.values()
            for finding in report.findings
            if finding.severity in {Severity.CRITICAL, Severity.HIGH}
        )
        status = GateStatus.FAIL if blockers else GateStatus.PASS
        final = replace(
            pending, gate=GateSummary(status, len(blockers)), created_at=snapshot.created_at
        )
        _atomic_write(review_fd, final)
        return final


def read_verdict(cwd: Path) -> Verdict:
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        return _read_verdict_locked(review_fd)


@dataclass(frozen=True)
class LegacyMigrationResult:
    round: int
    reviewers: Mapping[str, str]


def migrate_legacy_pending_round(cwd: Path) -> LegacyMigrationResult:
    """Preserve all legacy bytes and atomically migrate A/B/C to pending v2 slots.

    Historical telemetry has no byte/context hashes. Its successful spans cannot
    authenticate current canonical bytes, including replacements after validation.
    """
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd, ExitStack() as stack:
        legacy = _read_verdict_locked(review_fd)
        if legacy.schema != 1 or legacy.gate.status is not GateStatus.IN_PROGRESS:
            raise SchemaError("LEGACY_MIGRATION_NOT_ALLOWED")
        _require_all_pending(legacy)
        snapshot = capture_snapshot(root, legacy.base_ref)
        if not _snapshot_equal(legacy, snapshot):
            raise SchemaError("SNAPSHOT_CHANGED")
        pending = _new_v2_pending(
            legacy.snapshot, runtime=legacy.producer_runtime,
            initial_paths=legacy.initial_paths, round_number=legacy.round,
            decisions=legacy.decisions, history=legacy.history,
            contract=current_contract_binding(),
        )
        round_fd = _round_fd(
            review_fd, legacy.round, create=False, exact_report_directories=True
        )
        stack.callback(os.close, round_fd)
        scanned: dict[str, tuple[bytes | None, str, int | None]] = {}
        try:
            # Pre-scan every fixed path before touching evidence or canonical files.
            for key in "ABC":
                name = f"{key}.json"
                try:
                    fd = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=round_fd
                    )
                except FileNotFoundError:
                    scanned[key] = None, "REVIEWER_REPORT_MISSING", None
                    continue
                stack.callback(os.close, fd)
                safe_file(fd, "FILE_UNSAFE", exact_mode=0o600)
                raw = read_named_file(
                    round_fd, name, maximum=MAX_ATTEMPT_RAW_BYTES,
                    missing="REVIEWER_REPORT_MISSING", unsafe="FILE_UNSAFE", exact_mode=0o600,
                )
                _verify_legacy_name(round_fd, name, fd)
                reason = _legacy_report_reason(raw, legacy, Reviewer(key))
                scanned[key] = raw, reason, fd
            slots = {}
            for key, (raw, reason, _) in scanned.items():
                evidence = preserve_legacy_report(
                    review_fd, round_number=legacy.round, reviewer=Reviewer(key), raw=raw,
                    reason_code=reason if raw is not None else None,
                )
                if evidence and (
                    _legacy_report_reason(evidence[0], legacy, Reviewer(key)) != evidence[1]
                ):
                    raise SchemaError("ATTEMPT_EVIDENCE_UNSAFE")
                slots[key] = ReviewerSlot(
                    "pending", attempt_count=1 if evidence else 0,
                    last_error=evidence[1] if evidence else reason,
                )
            # All exact evidence is durable before the first canonical removal.
            # On failure, authenticated attempt-one records allow a bounded retry.
            for key, (_, _, fd) in scanned.items():
                if fd is not None:
                    _verify_legacy_name(round_fd, f"{key}.json", fd)
                    os.unlink(f"{key}.json", dir_fd=round_fd)
            os.fsync(round_fd)
        except OSError:
            raise SchemaError("FILE_UNSAFE") from None
        _atomic_write(review_fd, replace(pending, reviewers=slots))
        return LegacyMigrationResult(
            legacy.round, {key: f"pending:{slot.last_error}" for key, slot in slots.items()}
        )


def _legacy_report_reason(raw: bytes, verdict: Verdict, reviewer: Reviewer) -> str:
    try:
        m.validate_report_bytes(
            raw, expected_reviewer=reviewer,
            expected_round=verdict.round, snapshot=verdict.snapshot,
        )
    except SchemaError as error:
        if error.code not in REPORT_RETRYABLE_CODES:
            raise
        return error.code
    return "LEGACY_PROVENANCE_UNAVAILABLE"


def _verify_legacy_name(round_fd: int, name: str, fd: int) -> None:
    opened = safe_file(fd, "FILE_UNSAFE", exact_mode=0o600)
    named = os.stat(name, dir_fd=round_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(named.st_mode) or named.st_uid != os.geteuid()
        or stat.S_IMODE(named.st_mode) != 0o600
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise SchemaError("FILE_UNSAFE")
