"""Private, descriptor-anchored atomic storage and tribunal round state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
from typing import Iterator

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


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repository_root(cwd: Path) -> Path:
    try:
        raw = os.fspath(cwd)
        result = subprocess.run(
            ["/usr/bin/git", "-C", raw, "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 16 * 1024:
            raise SchemaError("NOT_GIT_REPOSITORY")
        root = Path(result.stdout.decode("utf-8", "strict").rstrip("\n")).resolve(
            strict=True
        )
        physical = Path(raw).resolve(strict=True)
        if not physical.is_relative_to(root):
            raise SchemaError("NOT_GIT_REPOSITORY")
        return root
    except SchemaError:
        raise
    except (OSError, UnicodeError, subprocess.SubprocessError, ValueError):
        raise SchemaError("NOT_GIT_REPOSITORY") from None


def _check_ignored(root: Path) -> None:
    try:
        result = subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(root),
                "check-ignore",
                "-q",
                "--",
                ".review/verdict.json",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise SchemaError("VERDICT_NOT_IGNORED") from None
    if result.returncode != 0:
        raise SchemaError("VERDICT_NOT_IGNORED")


def _preflight_review_directory(root: Path) -> None:
    root_fd = -1
    review_fd = -1
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            review_fd = os.open(
                ".review",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return
        except OSError:
            raise SchemaError("REVIEW_DIRECTORY_UNSAFE") from None
        _safe_directory(review_fd, "REVIEW_DIRECTORY_UNSAFE")
    finally:
        for fd in (review_fd, root_fd):
            if fd >= 0:
                os.close(fd)


def _safe_directory(fd: int, code: str) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise SchemaError(code)


def _safe_file(fd: int, code: str) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise SchemaError(code)
    return info


def _open_directory(parent_fd: int, name: str, *, create: bool, code: str) -> int:
    created = False
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        except OSError:
            raise SchemaError(code) from None
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
    except OSError:
        raise SchemaError(code) from None
    try:
        if created:
            os.fchmod(fd, 0o700)
        _safe_directory(fd, code)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def _locked_review(root: Path, *, create: bool) -> Iterator[int]:
    root_fd = -1
    review_fd = -1
    lock_fd = -1
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid():
            raise SchemaError("REPOSITORY_DIRECTORY_UNSAFE")
        review_fd = _open_directory(
            root_fd, ".review", create=create, code="REVIEW_DIRECTORY_UNSAFE"
        )
        flags = os.O_RDWR | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT
        try:
            lock_fd = os.open("lock", flags, 0o600, dir_fd=review_fd)
            _safe_file(lock_fd, "LOCK_FILE_UNSAFE")
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SchemaError("STORE_LOCKED") from None
        except SchemaError:
            raise
        except OSError:
            raise SchemaError("LOCK_FILE_UNSAFE") from None
        yield review_fd
    finally:
        for fd in (lock_fd, review_fd, root_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _read_named_file(
    parent_fd: int, name: str, *, maximum: int, missing: str, unsafe: str
) -> bytes:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
        )
    except FileNotFoundError:
        raise SchemaError(missing) from None
    except OSError:
        raise SchemaError(unsafe) from None
    try:
        info = _safe_file(fd, unsafe)
        if info.st_size > maximum:
            raise SchemaError("FILE_TOO_LARGE")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, min(65536, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise SchemaError("FILE_TOO_LARGE")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _atomic_write(review_fd: int, verdict: Verdict) -> None:
    raw = json.dumps(
        verdict.to_json(), ensure_ascii=False, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")
    if len(raw) > m.MAX_VERDICT_BYTES:
        raise SchemaError("VERDICT_TOO_LARGE")
    temporary = f".verdict.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    fd = -1
    try:
        try:
            existing_fd = os.open(
                "verdict.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=review_fd,
            )
        except FileNotFoundError:
            existing_fd = -1
        except OSError:
            raise SchemaError("VERDICT_FILE_UNSAFE") from None
        if existing_fd >= 0:
            try:
                _safe_file(existing_fd, "VERDICT_FILE_UNSAFE")
            finally:
                os.close(existing_fd)
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=review_fd,
        )
        os.fchmod(fd, 0o600)
        _safe_file(fd, "VERDICT_FILE_UNSAFE")
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(
            temporary, "verdict.json", src_dir_fd=review_fd, dst_dir_fd=review_fd
        )
        os.fsync(review_fd)
    except SchemaError:
        raise
    except OSError:
        raise SchemaError("VERDICT_WRITE_FAILED") from None
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=review_fd)
        except OSError:
            pass


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
    raw = _read_named_file(
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


def _round_fd(review_fd: int, round_number: int, *, create: bool) -> int:
    inbox_fd = _open_directory(
        review_fd, "inbox", create=create, code="INBOX_DIRECTORY_UNSAFE"
    )
    try:
        return _open_directory(
            inbox_fd,
            f"round-{round_number}",
            create=create,
            code="ROUND_DIRECTORY_UNSAFE",
        )
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
                opened = _safe_file(file_fd, "FILE_UNSAFE")
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
) -> bytes:
    relative = f".review/inbox/round-{round_number}/{basename}"
    _expected_input(root, supplied, relative, path_code)
    round_fd = _round_fd(review_fd, round_number, create=False)
    try:
        return _read_named_file(
            round_fd, basename, maximum=maximum, missing=missing, unsafe="FILE_UNSAFE"
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
    root = _repository_root(cwd)
    _preflight_review_directory(root)
    _check_ignored(root)
    with _locked_review(root, create=True) as review_fd:
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
    root = _repository_root(cwd)
    _preflight_review_directory(root)
    _check_ignored(root)
    with _locked_review(root, create=False) as review_fd:
        pending = _read_verdict_locked(review_fd)
        if pending.gate.status is not GateStatus.IN_PROGRESS or any(
            slot.status != "pending" for slot in pending.reviewers.values()
        ):
            raise SchemaError("ROUND_NOT_IN_PROGRESS")
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
            )
            reports[key] = m.parse_reviewer_report(
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
    root = _repository_root(cwd)
    _preflight_review_directory(root)
    _check_ignored(root)
    with _locked_review(root, create=False) as review_fd:
        return _read_verdict_locked(review_fd)
