"""Private, descriptor-anchored atomic storage and tribunal round state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .git_state import GitStateError, assert_auto_fix_scope, capture_snapshot
from . import model as m
from .model import (
    Decision,
    GateStatus,
    GateSummary,
    Reviewer,
    ReviewerReport,
    ReviewerSlot,
    RoundSummary,
    SchemaError,
    Severity,
    Snapshot,
    Verdict,
)
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


@dataclass(frozen=True)
class ReportReceipt:
    reviewer: Reviewer
    round: int
    path: str
    raw_sha256: str


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


def _parse_verdict(raw: bytes) -> Verdict:
    data = m._load_json(raw, limit=m.MAX_VERDICT_BYTES, too_large="VERDICT_TOO_LARGE")
    if isinstance(data, dict):
        schema = data.get("schema")
        if isinstance(schema, int) and not isinstance(schema, bool) and schema != 1:
            raise SchemaError("VERDICT_SCHEMA_UNSUPPORTED")
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
    if not isinstance(data, dict) or set(data) not in {
        frozenset(verdict_keys),
        frozenset(verdict_keys - {"head_ref"}),
    }:
        raise SchemaError("VERDICT_INVALID")
    obj = data
    if obj["schema"] != 1 or isinstance(obj["schema"], bool):
        raise SchemaError("VERDICT_INVALID")
    repository = m._text(obj["repository"], 256)
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+", repository)
        is None
    ):
        raise SchemaError("VERDICT_INVALID")
    base = m._object(obj["base"], {"ref", "sha"}, "VERDICT_INVALID")
    base_ref = m._text(base["ref"], 256)
    head_ref = m._head_ref(obj["head_ref"]) if "head_ref" in obj else ""
    base_sha, head_sha, merge_base_sha, digest = (
        base["sha"],
        obj["head_sha"],
        obj["merge_base_sha"],
        obj["diff_sha256"],
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
            obj["initial_paths"], m.MAX_INITIAL_PATHS, "VERDICT_INVALID"
        )
    )
    if len(initial_paths) != len(set(initial_paths)):
        raise SchemaError("VERDICT_INVALID")
    round_number = m._integer(obj["round"], "VERDICT_INVALID", minimum=1, maximum=3)
    runtime = obj["producer_runtime"]
    if not isinstance(runtime, str) or runtime not in {"claude", "codex"}:
        raise SchemaError("VERDICT_INVALID")
    created_at = m._text(obj["created_at"], 64)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created_at) is None:
            raise ValueError
        datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise SchemaError("VERDICT_INVALID") from None
    reviewers_obj = m._object(obj["reviewers"], {"A", "B", "C"}, "VERDICT_INVALID")
    snapshot = Snapshot(
        1,
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
    reviewers: dict[str, ReviewerSlot] = {}
    for key in "ABC":
        slot = reviewers_obj[key]
        if (
            isinstance(slot, dict)
            and set(slot) == {"status"}
            and slot["status"] == "pending"
        ):
            reviewers[key] = ReviewerSlot("pending")
            continue
        completed = m._object(
            slot,
            {"status", "findings", "executions", "claims", "prior_decisions"},
            "VERDICT_INVALID",
        )
        report_value = {
            "schema": 1,
            "reviewer": key,
            "round": round_number,
            "snapshot": {"head_sha": head_sha, "diff_sha256": digest},
            **completed,
        }
        encoded = json.dumps(
            report_value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        report = m.parse_reviewer_report(
            encoded,
            expected_reviewer=Reviewer(key),
            expected_round=round_number,
            snapshot=snapshot,
        )
        reviewers[key] = ReviewerSlot("complete", report)
    history = _parse_history(obj["history"])
    if tuple(item.round for item in history) != tuple(range(1, round_number)):
        raise SchemaError("VERDICT_INVALID")
    prior_ids = tuple(
        item["id"] for item in (history[-1].blocking_findings if history else ())
    )
    decisions = m.parse_decisions(
        json.dumps(obj["decisions"], ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        prior_blockers=prior_ids,
    )
    if round_number == 1 and decisions:
        raise SchemaError("VERDICT_INVALID")
    gate_obj = m._object(obj["gate"], {"status", "blocking_count"}, "VERDICT_INVALID")
    status = m._enum(GateStatus, gate_obj["status"], "VERDICT_INVALID")
    count = m._integer(
        gate_obj["blocking_count"],
        "VERDICT_INVALID",
        minimum=0,
        maximum=m.MAX_FINDINGS_PER_REVIEWER * 3,
    )
    pending_count = sum(slot.status == "pending" for slot in reviewers.values())
    if pending_count not in {0, 3}:
        raise SchemaError("VERDICT_INVALID")
    complete = pending_count == 0
    if (status is GateStatus.IN_PROGRESS) != (not complete):
        raise SchemaError("VERDICT_INVALID")
    actual = sum(
        1
        for slot in reviewers.values()
        if slot.report
        for finding in slot.report.findings
        if finding.severity in {Severity.CRITICAL, Severity.HIGH}
    )
    if status is GateStatus.IN_PROGRESS and count != 0:
        raise SchemaError("VERDICT_INVALID")
    if status is not GateStatus.IN_PROGRESS and count != actual:
        raise SchemaError("VERDICT_INVALID")
    if (status is GateStatus.PASS) != (complete and count == 0):
        raise SchemaError("VERDICT_INVALID")
    if any(item.disposition == "fixed" for item in decisions) and (
        not history or head_sha == history[-1].head_sha
    ):
        raise SchemaError("FIXED_HEAD_UNCHANGED")
    verdict = Verdict(
        1,
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
    )
    if complete:
        _validate_closure(
            verdict,
            {
                key: reviewers[key].report
                for key in "ABC"
                if reviewers[key].report is not None
            },
        )
    return verdict


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
    inbox_fd = open_directory(
        review_fd, "inbox", create=create, code="INBOX_DIRECTORY_UNSAFE"
    )
    try:
        if exact_report_directories:
            _exact_report_directory(inbox_fd)
        round_fd = open_directory(
            inbox_fd,
            f"round-{round_number}",
            create=create,
            code="ROUND_DIRECTORY_UNSAFE",
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


def _validate_report_store_input(reviewer: Reviewer, raw: bytes) -> None:
    if not isinstance(reviewer, Reviewer) or not isinstance(raw, bytes):
        raise SchemaError("REPORT_SCHEMA_INVALID")
    if len(raw) > m.MAX_REPORT_BYTES:
        raise SchemaError("REPORT_TOO_LARGE")


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
        _require_all_pending(pending)
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
        pending = _read_verdict_locked(review_fd)
        _require_all_pending(pending)
        round_fd = _round_fd(review_fd, pending.round, create=False)
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
        snapshot = capture_snapshot(root, pending.base_ref)
        if not _snapshot_equal(pending, snapshot):
            raise SchemaError("SNAPSHOT_CHANGED")
        return m.validate_report_bytes(
            raw,
            expected_reviewer=reviewer,
            expected_round=pending.round,
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
        _invalidate_round_inputs(review_fd, round_number)
        pending = Verdict(
            1,
            snapshot.repository,
            snapshot.base_ref,
            snapshot.base_sha,
            snapshot.head_ref,
            snapshot.head_sha,
            snapshot.merge_base_sha,
            snapshot.diff_sha256,
            initial_paths,
            round_number,
            runtime,
            {key: ReviewerSlot("pending") for key in "ABC"},
            decisions,
            history,
            GateSummary(GateStatus.IN_PROGRESS, 0),
            snapshot.created_at,
        )
        _atomic_write(review_fd, pending)
        return pending


def _validate_closure(verdict: Verdict, reports: Mapping[str, ReviewerReport]) -> None:
    decisions = {item.id: item for item in verdict.decisions}
    seen_replacements: set[str] = set()
    for reviewer in "ABC":
        responses = {
            item.decision_id: item for item in reports[reviewer].prior_decisions
        }
        expected = {
            item.id for item in decisions.values() if item.reviewer.value == reviewer
        }
        if set(responses) != expected:
            if not expected.issubset(responses):
                raise SchemaError("PRIOR_DECISION_RESPONSE_MISSING")
            raise SchemaError("PRIOR_DECISION_RESPONSE_INVALID")
        findings = {item.id: item for item in reports[reviewer].findings}
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
    if verdict.round == 1 and any(
        report.prior_decisions for report in reports.values()
    ):
        raise SchemaError("PRIOR_DECISION_RESPONSE_INVALID")


def finalize_round(
    cwd: Path, *, reviewer_paths: Mapping[str, Path], now: Callable[[], str] = utc_now
) -> Verdict:
    if not isinstance(reviewer_paths, Mapping) or set(reviewer_paths) != {
        "A",
        "B",
        "C",
    }:
        raise SchemaError("REVIEWER_REPORT_MISSING")
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        pending = _read_verdict_locked(review_fd)
        _require_all_pending(pending)
        snapshot = capture_snapshot(root, pending.base_ref, now=now)
        if not _snapshot_equal(pending, snapshot):
            raise SchemaError("SNAPSHOT_CHANGED")
        reports: dict[str, ReviewerReport] = {}
        for key in "ABC":
            raw = _read_input(
                root,
                review_fd,
                reviewer_paths[key],
                round_number=pending.round,
                basename=f"{key}.json",
                maximum=m.MAX_REPORT_BYTES,
                missing="REVIEWER_REPORT_MISSING",
                path_code="REPORT_PATH_INVALID",
                exact_mode=0o600,
            )
            reports[key], _raw_sha256 = m.validate_report_bytes(
                raw,
                expected_reviewer=Reviewer(key),
                expected_round=pending.round,
                snapshot=snapshot,
            )
        _validate_closure(pending, reports)
        blockers = tuple(
            finding
            for report in reports.values()
            for finding in report.findings
            if finding.severity in {Severity.CRITICAL, Severity.HIGH}
        )
        status = GateStatus.FAIL if blockers else GateStatus.PASS
        final = Verdict(
            pending.schema,
            pending.repository,
            pending.base_ref,
            pending.base_sha,
            pending.head_ref,
            pending.head_sha,
            pending.merge_base_sha,
            pending.diff_sha256,
            pending.initial_paths,
            pending.round,
            pending.producer_runtime,
            {key: ReviewerSlot("complete", reports[key]) for key in "ABC"},
            pending.decisions,
            pending.history,
            GateSummary(status, len(blockers)),
            snapshot.created_at,
        )
        _atomic_write(review_fd, final)
        return final


def read_verdict(cwd: Path) -> Verdict:
    root = repository_root(cwd)
    preflight_review_directory(root)
    check_ignored(root)
    with locked_review(root, create=False) as review_fd:
        return _read_verdict_locked(review_fd)
