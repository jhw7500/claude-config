import copy
from dataclasses import FrozenInstanceError
import json
import os
import stat

import pytest

from pre_pr_tribunal.git_state import capture_snapshot
from pre_pr_tribunal.model import Reviewer, SchemaError
from pre_pr_tribunal import review_store
from pre_pr_tribunal.telemetry import (
    TelemetryOutcome, TelemetryStage, bind_run, close_run, create_run,
    finish_span, read_ledger, record_candidate, recover_run, start_span,
    summarize_run,
)


def NOW():
    return "2026-09-09T00:00:00Z"


def new_run(repo, number=1, **kwargs):
    return create_run(
        repo, base_ref="master", runtime="codex", round_number=1,
        started_at=NOW(), started_monotonic_ns=1,
        token_hex=lambda _size: f"{number:032x}", **kwargs,
    )


def ledger_path(repo):
    return repo / ".review/telemetry.json"


def running_span(git_repo, *, started_at="2026-09-09T00:00:10Z",
                 started_monotonic_ns=10_000):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = new_run(git_repo)
    bind_run(git_repo, run_id=run.run_id, snapshot=snapshot)
    span = start_span(
        git_repo, run_id=run.run_id, stage=TelemetryStage.REVIEWER_TOTAL,
        reviewer=Reviewer.B, attempt=1, started_at=started_at,
        started_monotonic_ns=started_monotonic_ns,
        token_hex=lambda _size: "2" * 32,
    )
    return run, span


def finish(repo, run, span, **kwargs):
    return finish_span(
        repo, run_id=run.run_id, span_id=span.span_id,
        outcome=TelemetryOutcome.SUCCESS, reason_code=None,
        ended_at="2026-09-09T00:00:11Z",
        ended_monotonic_ns=1_000_010_000, **kwargs,
    )


def test_bound_run_records_terminal_span_and_sanitized_summary(git_repo):
    run, span = running_span(git_repo, started_monotonic_ns=2_000_000_000)
    terminal = finish_span(
        git_repo, run_id=run.run_id, span_id=span.span_id,
        outcome=TelemetryOutcome.SUCCESS, reason_code=None,
        ended_at="2026-09-09T00:00:15Z", ended_monotonic_ns=6_000_000_000,
    )
    summary = summarize_run(git_repo, run_id=run.run_id)
    assert summary["reviewers"]["B"]["total_ms"] == 4000
    assert summary["outcomes"]["success"] == 1
    assert summary["binding"]["contract"] == {
        "report_text": 2, "diff_recipe": 1, "telemetry_schema": 1,
    }
    assert summary["stages"]["reviewer_total"] == {"count": 1, "total_ms": 4000}
    assert summary["early_detection"] is None
    assert set(summary) == {
        "schema", "binding", "reviewers", "stages", "outcomes",
        "telemetry_incomplete", "anomaly_reason_codes", "early_detection",
    }
    assert set(summary["binding"]) == {"contract", "diff_sha256"}
    for forbidden in ("command", "/home/", "monotonic", "started_at", span.span_id):
        assert forbidden not in json.dumps(summary)
    with pytest.raises(FrozenInstanceError):
        terminal.duration_ms = 0
    bound = read_ledger(git_repo).runs[0]
    with pytest.raises(TypeError):
        bound.binding.contract["report_text"] = 1
    assert span.outcome is None


@pytest.mark.parametrize(("outcome", "reason"), (
    (TelemetryOutcome.FAILURE, "REPORT_SCHEMA_INVALID"),
    (TelemetryOutcome.TIMEOUT, "REVIEWER_TIMEOUT"),
    (TelemetryOutcome.INCOMPLETE, "CONTROLLER_INTERRUPTED"),
))
def test_terminal_outcomes_preserve_stable_reason(git_repo, outcome, reason):
    run, span = running_span(git_repo)
    terminal = finish_span(
        git_repo, run_id=run.run_id, span_id=span.span_id,
        outcome=outcome, reason_code=reason,
        ended_at="2026-09-09T00:00:11Z", ended_monotonic_ns=1_000_010_000,
    )
    assert terminal.outcome is outcome
    assert terminal.reason_code == reason
    assert terminal.duration_ms == 1000


@pytest.mark.parametrize(("wall", "mono"), (
    ("2026-09-09T00:00:09Z", 9_999),
    ("2026-09-09T00:00:09Z", 20_000),
    ("2026-09-09T00:00:11Z", 9_999),
))
def test_clock_reversal_records_null_duration_and_anomaly(git_repo, wall, mono):
    run, span = running_span(git_repo)
    terminal = finish_span(
        git_repo, run_id=run.run_id, span_id=span.span_id,
        outcome=TelemetryOutcome.SUCCESS, reason_code=None,
        ended_at=wall, ended_monotonic_ns=mono,
    )
    assert terminal.outcome is TelemetryOutcome.CLOCK_ANOMALY
    assert terminal.duration_ms is None
    assert terminal.reason_code == "TELEMETRY_CLOCK_ANOMALY"
    summary = summarize_run(git_repo)
    assert summary["reviewers"]["B"]["total_ms"] is None
    assert summary["anomaly_reason_codes"] == ["TELEMETRY_CLOCK_ANOMALY"]


def test_recover_closes_every_running_span_without_reopening_it(git_repo):
    run, first = running_span(git_repo)
    second = start_span(
        git_repo, run_id=run.run_id, stage=TelemetryStage.REPORT_VALIDATION,
        reviewer=Reviewer.A, attempt=1, started_at="2026-09-09T00:00:10Z",
        started_monotonic_ns=10_000, token_hex=lambda _size: "3" * 32,
    )
    assert recover_run(
        git_repo, run_id=run.run_id, ended_at="2026-09-09T00:00:12Z",
        ended_monotonic_ns=2_010_000,
    ) == 2
    stored = read_ledger(git_repo).runs[0]
    assert {item.outcome for item in stored.spans} == {TelemetryOutcome.INCOMPLETE}
    assert {item.reason_code for item in stored.spans} == {"CONTROLLER_INTERRUPTED"}
    previous = ledger_path(git_repo).read_bytes()
    for item in (first, second):
        with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
            finish(git_repo, run, item)
        assert ledger_path(git_repo).read_bytes() == previous
    assert recover_run(
        git_repo, run_id=run.run_id, ended_at="2026-09-09T00:00:13Z",
        ended_monotonic_ns=3_010_000,
    ) == 0


@pytest.mark.parametrize("mask", (0o000, 0o022, 0o077))
def test_telemetry_file_is_exact_private_mode(git_repo, mask):
    previous = os.umask(mask)
    try:
        new_run(git_repo)
    finally:
        os.umask(previous)
    info = ledger_path(git_repo).lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.geteuid()


def test_telemetry_symlink_is_rejected_without_touching_target(git_repo, tmp_path):
    (git_repo / ".review").mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    ledger_path(git_repo).symlink_to(outside)
    with pytest.raises(SchemaError, match="^TELEMETRY_FILE_UNSAFE$"):
        read_ledger(git_repo)
    assert outside.read_bytes() == b"keep"
    assert ledger_path(git_repo).is_symlink()


@pytest.mark.parametrize("kind", ("fifo", "wrong_owner", "directory", "mode"))
def test_telemetry_rejects_fifo_and_wrong_owner(git_repo, monkeypatch, kind):
    run = new_run(git_repo)
    path = ledger_path(git_repo)
    previous = path.read_bytes()
    if kind in {"fifo", "directory"}:
        path.unlink()
        os.mkfifo(path) if kind == "fifo" else path.mkdir()
    elif kind == "mode":
        path.chmod(0o644)
    else:
        real_fstat = review_store.os.fstat
        target_inode = path.stat().st_ino

        def wrong_owner(fd):
            info = real_fstat(fd)
            if info.st_ino == target_inode:
                fields = list(info)
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(review_store.os, "fstat", wrong_owner)
    with pytest.raises(SchemaError, match="^TELEMETRY_FILE_UNSAFE$"):
        read_ledger(git_repo)
    if kind in {"mode", "wrong_owner"}:
        assert path.read_bytes() == previous
        assert json.loads(previous)["runs"][0]["run_id"] == run.run_id
    else:
        assert stat.S_ISFIFO(path.lstat().st_mode) if kind == "fifo" else path.is_dir()


def test_failed_atomic_update_preserves_previous_ledger(git_repo, monkeypatch):
    run = new_run(git_repo)
    previous = ledger_path(git_repo).read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise OSError("PRIVATE_FAILURE_MUST_NOT_LEAK")

    monkeypatch.setattr(review_store.os, "replace", fail_replace)
    with pytest.raises(SchemaError, match="^TELEMETRY_FILE_UNSAFE$"):
        new_run(git_repo, 2)
    assert ledger_path(git_repo).read_bytes() == previous
    assert [item.run_id for item in read_ledger(git_repo).runs] == [run.run_id]
    residue = list((git_repo / ".review").glob(".tmp.*"))
    assert len(residue) == 1
    assert stat.S_IMODE(residue[0].stat().st_mode) == 0o600
    assert [item["run_id"] for item in json.loads(residue[0].read_bytes())["runs"]] == [
        f"{1:032x}", f"{2:032x}",
    ]


def test_seventeenth_run_prunes_only_oldest_closed_run(git_repo):
    for number in range(1, 17):
        run = new_run(git_repo, number)
        if number in (2, 3):
            close_run(git_repo, run_id=run.run_id, outcome=TelemetryOutcome.SUCCESS,
                      reason_code=None, ended_at="2026-09-09T00:00:01Z")
    before = json.loads(ledger_path(git_repo).read_bytes())
    new_run(git_repo, 17)
    after = json.loads(ledger_path(git_repo).read_bytes())
    assert [item["run_id"] for item in after["runs"]] == [
        f"{number:032x}" for number in (1, *range(3, 18))
    ]
    assert after["runs"][:-1] == [item for item in before["runs"] if item["run_id"] != f"{2:032x}"]
    assert ledger_path(git_repo).read_bytes() == json.dumps(after, ensure_ascii=False, separators=(",", ":")).encode()


def test_running_and_incomplete_runs_are_never_pruned(git_repo):
    for number in range(1, 17):
        run = new_run(git_repo, number)
        if number % 2 == 0:
            close_run(git_repo, run_id=run.run_id, outcome=TelemetryOutcome.INCOMPLETE,
                      reason_code="CONTROLLER_INTERRUPTED", ended_at=NOW())
    previous = ledger_path(git_repo).read_bytes()
    with pytest.raises(SchemaError, match="^TELEMETRY_TOO_LARGE$"):
        new_run(git_repo, 17)
    assert ledger_path(git_repo).read_bytes() == previous
    assert [item.run_id for item in read_ledger(git_repo).runs] == [f"{n:032x}" for n in range(1, 17)]


def test_span_limit_sets_reserved_incomplete_marker(git_repo):
    run = new_run(git_repo)
    for number in range(128):
        start_span(git_repo, run_id=run.run_id, stage=TelemetryStage.RECOVERY_RETRY,
                   reviewer=None, attempt=number + 1, started_at=NOW(),
                   started_monotonic_ns=1, token_hex=lambda _size, n=number: f"{n + 100:032x}")
    before = json.loads(ledger_path(git_repo).read_bytes())
    with pytest.raises(SchemaError, match="^TELEMETRY_TOO_LARGE$"):
        start_span(git_repo, run_id=run.run_id, stage=TelemetryStage.RECOVERY_RETRY,
                   reviewer=None, attempt=129, started_at=NOW(),
                   started_monotonic_ns=1, token_hex=lambda _size: "f" * 32)
    after = json.loads(ledger_path(git_repo).read_bytes())
    expected = copy.deepcopy(before)
    expected["runs"][0]["telemetry_incomplete"] = True
    expected["runs"][0]["telemetry_incomplete_reason"] = "TELEMETRY_TOO_LARGE"
    assert after == expected
    assert ledger_path(git_repo).read_bytes() == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    assert [item.run_id for item in read_ledger(git_repo).runs] == [run.run_id]
    assert summarize_run(git_repo)["telemetry_incomplete"] is True


@pytest.mark.parametrize(("where", "key", "value"), (
    ("ledger", "schema", True), ("ledger", "schema", 1.0),
    ("ledger", "schema", 2), ("ledger", "unknown", 1),
    ("run", "runtime", "unknown"), ("run", "round", True),
    ("run", "round", 4), ("run", "run_id", "a" * 31),
    ("run", "started_late", 1), ("run", "telemetry_incomplete", "true"),
    ("run", "telemetry_incomplete_reason", "UNEXPECTED"),
    ("binding", "status", "other"), ("binding", "repository", "/home/private"),
    ("binding", "base_sha", "x" * 40), ("binding", "head_ref", "main"),
    ("binding", "base_ref", "../bad"), ("binding", "diff_sha256", None),
    ("binding", "contract", {"report_text": 2, "diff_recipe": 1, "telemetry_schema": True}),
    ("span", "unknown", 1), ("span", "stage", "command"),
    ("span", "reviewer", "D"), ("span", "reviewer", None),
    ("span", "attempt", True), ("span", "attempt", 0),
    ("span", "attempt", 2**63), ("span", "span_id", "A" * 32),
    ("span", "started_at", "2026-02-30T00:00:00Z"),
    ("span", "started_at", "2026-09-09T00:00:00+00:00"),
    ("span", "started_monotonic_ns", -1),
    ("span", "started_monotonic_ns", 1.0),
    ("span", "started_monotonic_ns", 2**63),
    ("span", "status", "other"), ("span", "duration_ms", 0),
    ("span", "ended_at", NOW()), ("span", "ended_monotonic_ns", 1),
    ("span", "reason_code", "bad code"),
    ("span", "reason_code", "A" * 65),
))
def test_strict_parser_rejects_invalid_fields_without_rewriting(git_repo, where, key, value):
    running_span(git_repo)
    raw = json.loads(ledger_path(git_repo).read_bytes())
    targets = {"ledger": raw, "run": raw["runs"][0],
               "binding": raw["runs"][0]["binding"], "span": raw["runs"][0]["spans"][0]}
    targets[where][key] = value
    corrupted = json.dumps(raw).encode()
    ledger_path(git_repo).write_bytes(corrupted)
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        read_ledger(git_repo)
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        new_run(git_repo, 2)
    assert ledger_path(git_repo).read_bytes() == corrupted


@pytest.mark.parametrize("kind", ("duplicate_key", "duplicate_run", "duplicate_span", "runs", "spans", "bytes", "nan", "nesting"))
def test_parser_rejects_duplicates_and_resource_overflow(git_repo, kind):
    running_span(git_repo)
    raw = json.loads(ledger_path(git_repo).read_bytes())
    code = "TELEMETRY_INVALID"
    if kind in {"runs", "spans", "bytes"}:
        code = "TELEMETRY_TOO_LARGE"
    if kind in {"runs", "duplicate_run"}:
        raw["runs"] *= 17 if kind == "runs" else 2
    if kind in {"spans", "duplicate_span"}:
        raw["runs"][0]["spans"] *= 129 if kind == "spans" else 2
    payload = json.dumps(raw).encode()
    if kind == "duplicate_key":
        payload = payload.replace(b'"schema": 1', b'"schema": 1, "schema": 1')
    elif kind == "bytes":
        payload = b" " * (2 * 1024 * 1024 + 1)
    elif kind == "nan":
        payload = b'{"schema":NaN,"runs":[]}'
    elif kind == "nesting":
        payload = b"[" * 2000 + b"]" * 2000
    ledger_path(git_repo).write_bytes(payload)
    with pytest.raises(SchemaError, match=f"^{code}$"):
        read_ledger(git_repo)
    assert ledger_path(git_repo).read_bytes() == payload


def test_pending_candidate_and_binding_are_one_way(git_repo):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = new_run(git_repo)
    assert run.binding.status == "pending"
    candidate = record_candidate(git_repo, run_id=run.run_id, repository=snapshot.repository,
                                 head_ref=snapshot.head_ref, head_sha=snapshot.head_sha)
    assert candidate.binding.head_sha == snapshot.head_sha
    bound = bind_run(git_repo, run_id=run.run_id, snapshot=snapshot)
    assert bound.binding.status == "bound"
    before = ledger_path(git_repo).read_bytes()
    for action in (
        lambda: bind_run(git_repo, run_id=run.run_id, snapshot=snapshot),
        lambda: record_candidate(git_repo, run_id=run.run_id, repository=snapshot.repository,
                                 head_ref=snapshot.head_ref, head_sha=snapshot.head_sha),
        lambda: new_run(git_repo),
    ):
        with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
            action()
        assert ledger_path(git_repo).read_bytes() == before


def test_stale_pending_candidate_is_replaced_once_by_authoritative_snapshot(git_repo):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = new_run(git_repo)
    record_candidate(git_repo, run_id=run.run_id, repository="old/repository",
                     head_ref="refs/heads/old", head_sha="f" * 40)
    bound = bind_run(git_repo, run_id=run.run_id, snapshot=snapshot)
    assert bound.binding.repository == snapshot.repository
    assert bound.binding.head_ref == snapshot.head_ref
    assert bound.binding.head_sha == snapshot.head_sha
    assert bound.binding.diff_sha256 == snapshot.diff_sha256
    before = ledger_path(git_repo).read_bytes()
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        bind_run(git_repo, run_id=run.run_id, snapshot=snapshot)
    assert ledger_path(git_repo).read_bytes() == before


def test_closed_run_mutations_are_rejected(git_repo):
    run = new_run(git_repo)
    close_run(git_repo, run_id=run.run_id, outcome=TelemetryOutcome.FAILURE,
              reason_code="SNAPSHOT_CHANGED", ended_at=NOW())
    before = ledger_path(git_repo).read_bytes()
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        start_span(git_repo, run_id=run.run_id, stage=TelemetryStage.SNAPSHOT_PREFLIGHT,
                   reviewer=None, attempt=1, started_at=NOW(), started_monotonic_ns=1)
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        close_run(git_repo, run_id=run.run_id, outcome=TelemetryOutcome.SUCCESS,
                  reason_code=None, ended_at=NOW())
    assert ledger_path(git_repo).read_bytes() == before


def test_binding_preserves_requested_base(git_repo):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = create_run(git_repo, base_ref="another-base", runtime="codex", round_number=1,
                     started_at=NOW(), started_monotonic_ns=1)
    before = ledger_path(git_repo).read_bytes()
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        bind_run(git_repo, run_id=run.run_id, snapshot=snapshot)
    assert ledger_path(git_repo).read_bytes() == before


@pytest.mark.parametrize("stage", list(TelemetryStage))
def test_reviewer_dimension_and_duration_floor(git_repo, stage):
    run = new_run(git_repo)
    global_stage = stage in {TelemetryStage.SNAPSHOT_PREFLIGHT, TelemetryStage.FINALIZE}
    reviewer = None if global_stage else Reviewer.A
    span = start_span(git_repo, run_id=run.run_id, stage=stage, reviewer=reviewer,
                      attempt=1, started_at=NOW(), started_monotonic_ns=1)
    terminal = finish_span(git_repo, run_id=run.run_id, span_id=span.span_id,
                           outcome=TelemetryOutcome.SUCCESS, reason_code=None,
                           ended_at=NOW(), ended_monotonic_ns=1_999_999)
    assert terminal.duration_ms == 1
    if stage is not TelemetryStage.RECOVERY_RETRY:
        before = ledger_path(git_repo).read_bytes()
        with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
            start_span(git_repo, run_id=run.run_id, stage=stage,
                       reviewer=Reviewer.A if global_stage else None,
                       attempt=2, started_at=NOW(), started_monotonic_ns=1)
        assert ledger_path(git_repo).read_bytes() == before


@pytest.mark.parametrize(("outcome", "reason"), (
    (TelemetryOutcome.SUCCESS, "UNEXPECTED"), (TelemetryOutcome.FAILURE, None),
    (TelemetryOutcome.TIMEOUT, "free form /home/private"),
    (TelemetryOutcome.INCOMPLETE, "A" * 65),
))
def test_terminal_reason_validation_preserves_running_span(git_repo, outcome, reason):
    run, span = running_span(git_repo)
    before = ledger_path(git_repo).read_bytes()
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        finish_span(git_repo, run_id=run.run_id, span_id=span.span_id, outcome=outcome,
                    reason_code=reason, ended_at=NOW(), ended_monotonic_ns=1)
    assert ledger_path(git_repo).read_bytes() == before


def test_close_rejects_running_spans_and_preserves_terminal_data(git_repo):
    run, span = running_span(git_repo)
    before = ledger_path(git_repo).read_bytes()
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        close_run(git_repo, run_id=run.run_id, outcome=TelemetryOutcome.SUCCESS,
                  reason_code=None, ended_at="2026-09-09T00:00:12Z")
    assert ledger_path(git_repo).read_bytes() == before
    terminal = finish(git_repo, run, span)
    closed = close_run(git_repo, run_id=run.run_id, outcome=TelemetryOutcome.SUCCESS,
                       reason_code=None, ended_at="2026-09-09T00:00:12Z")
    assert closed.spans == (terminal,)
    assert closed.outcome is TelemetryOutcome.SUCCESS


@pytest.mark.parametrize("last_outcome", (TelemetryOutcome.SUCCESS, TelemetryOutcome.INCOMPLETE, TelemetryOutcome.CLOCK_ANOMALY, None))
def test_early_detection_requires_all_three_usable_terminal_milestones(git_repo, last_outcome):
    run = new_run(git_repo)
    for reviewer, seconds in ((Reviewer.A, 5), (Reviewer.B, 20), (Reviewer.C, 30)):
        span = start_span(git_repo, run_id=run.run_id, stage=TelemetryStage.REVIEWER_TOTAL,
                          reviewer=reviewer, attempt=1, started_at=NOW(), started_monotonic_ns=1)
        outcome = last_outcome if reviewer is Reviewer.C else TelemetryOutcome.SUCCESS
        if outcome is not None:
            finish_span(git_repo, run_id=run.run_id, span_id=span.span_id, outcome=outcome,
                        reason_code=None if outcome is TelemetryOutcome.SUCCESS else "CONTROLLER_INTERRUPTED",
                        ended_at=f"2026-09-09T00:00:{seconds:02d}Z", ended_monotonic_ns=seconds * 1_000_000_000 + 1)
    validation = start_span(git_repo, run_id=run.run_id, stage=TelemetryStage.REPORT_VALIDATION,
                            reviewer=Reviewer.A, attempt=1, started_at=NOW(), started_monotonic_ns=1)
    finish_span(git_repo, run_id=run.run_id, span_id=validation.span_id,
                outcome=TelemetryOutcome.FAILURE, reason_code="REPORT_SCHEMA_INVALID",
                ended_at="2026-09-09T00:00:10Z", ended_monotonic_ns=10_000_000_001)
    assert summarize_run(git_repo)["early_detection"] == {
        "reviewer": "A", "reason_code": "REPORT_SCHEMA_INVALID", "detected_elapsed_ms": 10000,
        "all_reviewers_terminal_elapsed_ms": 30000 if last_outcome is TelemetryOutcome.SUCCESS else None,
        "wait_all_delay_ms": 20000 if last_outcome is TelemetryOutcome.SUCCESS else None,
    }


def test_missing_ledger_is_empty_and_unknown_run_is_bounded(git_repo):
    assert read_ledger(git_repo).to_json() == {"schema": 1, "runs": []}
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        summarize_run(git_repo)


@pytest.mark.parametrize("field", ("repository", "head_ref", "head_sha"))
def test_record_candidate_requires_complete_nonnullable_candidate(git_repo, field):
    run = new_run(git_repo)
    candidate = {"repository": "owner/repository", "head_ref": "refs/heads/main", "head_sha": "a" * 40}
    candidate[field] = None
    before = ledger_path(git_repo).read_bytes()
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        record_candidate(git_repo, run_id=run.run_id, **candidate)
    assert ledger_path(git_repo).read_bytes() == before


@pytest.mark.parametrize(("field", "value"), (
    ("duration_ms", 1001), ("duration_ms", None), ("duration_ms", True),
    ("ended_at", None), ("ended_monotonic_ns", None),
    ("status", "running"), ("status", "failure"),
    ("reason_code", "UNEXPECTED"),
))
def test_terminal_parser_rejects_inconsistent_state(git_repo, field, value):
    run, span = running_span(git_repo)
    finish(git_repo, run, span)
    raw = json.loads(ledger_path(git_repo).read_bytes())
    raw["runs"][0]["spans"][0][field] = value
    payload = json.dumps(raw).encode()
    ledger_path(git_repo).write_bytes(payload)
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        read_ledger(git_repo)
    assert ledger_path(git_repo).read_bytes() == payload


@pytest.mark.parametrize("phase", ("write", "fsync", "anonymous", "proc"))
def test_prepublication_failures_preserve_ledger_without_partial_residue(git_repo, monkeypatch, phase):
    run = new_run(git_repo)
    before = ledger_path(git_repo).read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("PRIVATE_ERROR")

    if phase == "anonymous":
        monkeypatch.setattr(review_store.os, "O_TMPFILE", 0)
    elif phase == "proc":
        monkeypatch.setattr(review_store.os, "link", fail)
    else:
        monkeypatch.setattr(review_store.os, phase, fail)
    with pytest.raises(SchemaError, match="^TELEMETRY_FILE_UNSAFE$"):
        new_run(git_repo, 2)
    assert ledger_path(git_repo).read_bytes() == before
    assert [item.run_id for item in read_ledger(git_repo).runs] == [run.run_id]
    assert list((git_repo / ".review").glob(".tmp.*")) == []
