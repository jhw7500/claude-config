"""Recovery accounting is observational, invocation-bound, and never a verdict."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from pre_pr_tribunal import telemetry
from pre_pr_tribunal.git_state import capture_snapshot
from pre_pr_tribunal.model import Reviewer


CLI = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
NOW = "2026-09-11T00:00:00Z"


def invoke(repo, *args, raw=None):
    return subprocess.run([sys.executable, str(CLI), *args], cwd=repo,
                          input=raw, capture_output=True, check=False)


def payload(repo, *args, raw=None):
    result = invoke(repo, *args, raw=raw)
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    return json.loads(result.stdout)


def report(begun, reviewer):
    return json.dumps({
        "schema": 1, "reviewer": reviewer, "round": 1,
        "snapshot": {key: begun["snapshot"][key] for key in ("head_sha", "diff_sha256")},
        "status": "complete", "findings": [], "executions": [], "claims": [],
        "prior_decisions": [],
    }, indent=2).encode() + b"\n \n"


def pending_b(repo, *, close=True):
    begun = payload(repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1")
    for role in "AC":
        payload(repo, "submit-report", "--reviewer", role, raw=report(begun, role))
    for _ in range(3):
        result = invoke(repo, "submit-report", "--reviewer", "B", raw=b"{")
        assert result.returncode != 0 and b"JSON_INVALID" in result.stderr
    if close:
        payload(repo, "telemetry-close", "--run-id", begun["telemetry"]["run_id"],
                "--outcome", "failure", "--reason-code", "JSON_INVALID")
    return begun


def dispatch(repo, run_id, role, attempt):
    opened = payload(repo, "telemetry-start", "--run-id", run_id,
                     "--stage", "reviewer_dispatch_wait", "--reviewer", role,
                     "--attempt", str(attempt))
    payload(repo, "telemetry-finish", "--run-id", run_id, "--span-id", opened["span_id"],
            "--outcome", "incomplete", "--reason-code", "RUNTIME_SIGNAL_UNAVAILABLE")


def test_resume_records_reuse_and_local_retry_without_rewriting_receipts(git_repo):
    """A terminal prior run must not leave resumed B unmeasured or count its old 3 attempts."""
    old = pending_b(git_repo)
    files = [git_repo / ".review/verdict.json", *(
        git_repo / f".review/inbox/round-1/{role}.json" for role in "AC"
    )]
    before = [path.read_bytes() for path in files]
    resumed = payload(git_repo, "telemetry-resume", "--runtime", "codex")
    run_id = resumed["run_id"]
    assert resumed["status"] == "active" and run_id != old["telemetry"]["run_id"]
    assert [path.read_bytes() for path in files] == before
    dispatch(git_repo, run_id, "B", 1)
    dispatch(git_repo, run_id, "B", 2)
    summary = payload(git_repo, "telemetry-summary", "--run-id", run_id)
    assert summary["recovery"] == {
        "kind": "resume", "reused_slot_count": 2, "requested_slot_count": 1,
        "rerun_slot_count": 1, "dispatch_request_count": 2, "retry_request_count": 1,
        "accounting_complete": True,
    }
    assert summary["invocation_elapsed_ms"] is None  # Still running, not zero.
    assert payload(git_repo, "status")["reviewers"]["B"]["attempt_count"] == 3
    payload(git_repo, "telemetry-close", "--run-id", run_id, "--outcome", "success")
    assert payload(git_repo, "telemetry-summary", "--run-id", run_id)["invocation_elapsed_ms"] >= 0
    assert [path.read_bytes() for path in files] == before


def test_resume_counts_prior_observed_request_without_verdict_attempt(git_repo):
    """A rejected dispatch stays a rerun even when it never changed the verdict slot."""
    begun = payload(
        git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1",
    )
    for role in "AC":
        payload(git_repo, "submit-report", "--reviewer", role, raw=report(begun, role))
    prior_run_id = begun["telemetry"]["run_id"]
    opened = payload(
        git_repo, "telemetry-start", "--run-id", prior_run_id,
        "--stage", "reviewer_dispatch_wait", "--reviewer", "B", "--attempt", "1",
    )
    payload(
        git_repo, "telemetry-finish", "--run-id", prior_run_id,
        "--span-id", opened["span_id"], "--outcome", "failure",
        "--reason-code", "CAPACITY_REJECTED",
    )
    payload(
        git_repo, "telemetry-close", "--run-id", prior_run_id,
        "--outcome", "failure", "--reason-code", "REVIEWER_UNAVAILABLE",
    )
    assert payload(git_repo, "status")["reviewers"]["B"]["attempt_count"] == 0

    resumed = payload(git_repo, "telemetry-resume", "--runtime", "codex")
    dispatch(git_repo, resumed["run_id"], "B", 1)

    recovery = payload(
        git_repo, "telemetry-summary", "--run-id", resumed["run_id"],
    )["recovery"]
    assert recovery["accounting_complete"] is True
    assert recovery["rerun_slot_count"] == 1


def test_resume_carries_prior_request_across_ledger_eviction(git_repo):
    """Evicting the dispatch run must not turn a later request into a first attempt."""
    begun = payload(
        git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1",
    )
    for role in "AC":
        payload(git_repo, "submit-report", "--reviewer", role, raw=report(begun, role))
    original_run_id = begun["telemetry"]["run_id"]
    opened = payload(
        git_repo, "telemetry-start", "--run-id", original_run_id,
        "--stage", "reviewer_dispatch_wait", "--reviewer", "B", "--attempt", "1",
    )
    payload(
        git_repo, "telemetry-finish", "--run-id", original_run_id,
        "--span-id", opened["span_id"], "--outcome", "failure",
        "--reason-code", "CAPACITY_REJECTED",
    )
    payload(
        git_repo, "telemetry-close", "--run-id", original_run_id,
        "--outcome", "failure", "--reason-code", "REVIEWER_UNAVAILABLE",
    )

    for _ in range(telemetry.MAX_TELEMETRY_RUNS):
        resumed = payload(git_repo, "telemetry-resume", "--runtime", "codex")
        payload(
            git_repo, "telemetry-close", "--run-id", resumed["run_id"],
            "--outcome", "success",
        )
    assert all(
        run.run_id != original_run_id for run in telemetry.read_ledger(git_repo).runs
    )

    resumed = payload(git_repo, "telemetry-resume", "--runtime", "codex")
    dispatch(git_repo, resumed["run_id"], "B", 1)
    recovery = payload(
        git_repo, "telemetry-summary", "--run-id", resumed["run_id"],
    )["recovery"]
    assert recovery["accounting_complete"] is True
    assert recovery["rerun_slot_count"] == 1


def test_resume_marks_interrupted_prior_request_history_unknown(git_repo):
    """An interrupted prior request cannot become a certain zero-rerun observation."""
    begun = payload(
        git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1",
    )
    for role in "AC":
        payload(git_repo, "submit-report", "--reviewer", role, raw=report(begun, role))
    prior_run_id = begun["telemetry"]["run_id"]
    payload(
        git_repo, "telemetry-start", "--run-id", prior_run_id,
        "--stage", "reviewer_dispatch_wait", "--reviewer", "B", "--attempt", "1",
    )
    payload(git_repo, "telemetry-recover", "--run-id", prior_run_id)
    payload(
        git_repo, "telemetry-close", "--run-id", prior_run_id,
        "--outcome", "incomplete", "--reason-code", "CONTROLLER_INTERRUPTED",
    )

    resumed = payload(git_repo, "telemetry-resume", "--runtime", "codex")
    dispatch(git_repo, resumed["run_id"], "B", 1)

    recovery = payload(
        git_repo, "telemetry-summary", "--run-id", resumed["run_id"],
    )["recovery"]
    assert recovery["accounting_complete"] is False
    assert recovery["rerun_slot_count"] is None


@pytest.mark.parametrize("change", ("dirty", "tampered_receipt", "active_observation", "terminal_verdict"))
def test_resume_refuses_unverifiable_or_ambiguous_observation_without_writes(git_repo, change):
    begun = pending_b(git_repo, close=change != "active_observation")
    if change == "dirty":
        (git_repo / "tracked.txt").write_text("changed\n")
    elif change == "tampered_receipt":
        with (git_repo / ".review/inbox/round-1/A.json").open("ab") as stream:
            stream.write(b" ")
    elif change == "terminal_verdict":
        payload(git_repo, "submit-report", "--reviewer", "B", raw=report(begun, "B"))
        payload(git_repo, "finalize")
    paths = [git_repo / ".review/telemetry.json", git_repo / ".review/verdict.json"]
    before = [path.read_bytes() for path in paths]
    result = invoke(git_repo, "telemetry-resume", "--runtime", "codex")
    assert result.returncode == 1 and result.stderr == b"PRE_PR_TRIBUNAL:TELEMETRY_INVALID\n"
    assert [path.read_bytes() for path in paths] == before


def test_new_round_starts_with_zero_reuse_and_does_not_inherit_old_attempts(git_repo):
    pending_b(git_repo)
    resumed = payload(git_repo, "telemetry-resume", "--runtime", "codex")
    dispatch(git_repo, resumed["run_id"], "B", 1)
    payload(git_repo, "telemetry-close", "--run-id", resumed["run_id"], "--outcome", "success")
    snapshot = capture_snapshot(git_repo, "master")
    run = telemetry.create_run(git_repo, base_ref="master", runtime="codex", round_number=2,
                               started_at=NOW, started_monotonic_ns=0)
    telemetry.bind_run(git_repo, run_id=run.run_id, snapshot=snapshot,
                       invocation=telemetry.Invocation("new_round", (), ()))
    summary = telemetry.summarize_run(git_repo, run_id=run.run_id)
    assert summary["recovery"] == {
        "kind": "new_round", "reused_slot_count": 0, "requested_slot_count": 0,
        "rerun_slot_count": 0, "dispatch_request_count": 0, "retry_request_count": 0,
        "accounting_complete": True,
    }


def test_elapsed_is_measured_once_not_sum_of_overlapping_reviewer_spans(git_repo):
    # Use one controlled clock domain, not real preflight wall time plus fake endpoints.
    run = telemetry.create_run(git_repo, base_ref="master", runtime="codex", round_number=1,
                               started_at=NOW, started_monotonic_ns=0)
    run_id = run.run_id
    telemetry.bind_run(git_repo, run_id=run_id, snapshot=capture_snapshot(git_repo, "master"),
                       invocation=telemetry.Invocation("new_round", (), ()))
    for role in (Reviewer.A, Reviewer.C):
        span = telemetry.start_span(git_repo, run_id=run_id, stage=telemetry.TelemetryStage.REVIEWER_TOTAL,
            reviewer=role, attempt=1, started_at=run.started_at,
            started_monotonic_ns=run.started_monotonic_ns)
        telemetry.finish_span(git_repo, run_id=run_id, span_id=span.span_id,
            outcome=telemetry.TelemetryOutcome.SUCCESS, reason_code=None,
            ended_at=run.started_at, ended_monotonic_ns=run.started_monotonic_ns + 2_000_000_000)
    telemetry.close_run(git_repo, run_id=run_id, outcome=telemetry.TelemetryOutcome.SUCCESS,
        reason_code=None, ended_at=run.started_at,
        ended_monotonic_ns=run.started_monotonic_ns + 3_000_000_000)
    summary = telemetry.summarize_run(git_repo, run_id=run_id)
    assert summary["reviewers"]["A"]["total_ms"] == 2000
    assert summary["reviewers"]["C"]["total_ms"] == 2000
    assert summary["invocation_elapsed_ms"] == 3000
    assert summary["recovery"]["accounting_complete"] is False  # No dispatch observations.
    assert summary["recovery"]["dispatch_request_count"] is None


def test_legacy_ledger_is_read_without_fabricating_reuse_or_elapsed(git_repo):
    begun = payload(git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1")
    path = git_repo / ".review/telemetry.json"
    ledger = json.loads(path.read_bytes())
    ledger["schema"] = 1
    for run in ledger["runs"]:
        run["binding"]["contract"]["telemetry_schema"] = 1
        run.pop("invocation", None)
        run.pop("ended_monotonic_ns", None)
    path.write_text(json.dumps(ledger))
    before = path.read_bytes()
    summary = payload(git_repo, "telemetry-summary", "--run-id", begun["telemetry"]["run_id"])
    assert summary["recovery"] is None
    assert summary["invocation_elapsed_ms"] is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("stage", ("report_store", "reviewer_total", "report_validation"))
def test_unobserved_request_is_unknown_not_zero(git_repo, stage):
    begun = payload(git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1")
    run_id = begun["telemetry"]["run_id"]
    payload(git_repo, "telemetry-start", "--run-id", run_id, "--stage", stage,
            "--reviewer", "B", "--attempt", "1")
    recovery = payload(git_repo, "telemetry-summary", "--run-id", run_id)["recovery"]
    assert recovery["accounting_complete"] is False
    assert recovery["dispatch_request_count"] is None


def test_interrupted_request_has_unknown_accounting_and_elapsed(git_repo):
    begun = payload(git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1")
    run_id = begun["telemetry"]["run_id"]
    payload(git_repo, "telemetry-start", "--run-id", run_id, "--stage", "reviewer_dispatch_wait",
            "--reviewer", "B", "--attempt", "1")
    payload(git_repo, "telemetry-recover", "--run-id", run_id)
    payload(git_repo, "telemetry-close", "--run-id", run_id, "--outcome", "failure",
            "--reason-code", "REVIEWER_UNAVAILABLE")
    summary = payload(git_repo, "telemetry-summary", "--run-id", run_id)
    assert summary["recovery"]["accounting_complete"] is False
    assert summary["recovery"]["dispatch_request_count"] is None
    assert summary["invocation_elapsed_ms"] is None


@pytest.mark.parametrize("attempts", ((1, 1), (2,), (1, 3)))
def test_duplicate_or_gapped_dispatch_counts_are_unknown(git_repo, attempts):
    pending_b(git_repo)
    run_id = payload(git_repo, "telemetry-resume", "--runtime", "codex")["run_id"]
    for attempt in attempts:
        dispatch(git_repo, run_id, "B", attempt)
    summary = payload(git_repo, "telemetry-summary", "--run-id", run_id)
    assert summary["recovery"]["accounting_complete"] is False
    assert summary["recovery"]["retry_request_count"] is None


@pytest.mark.parametrize("invocation", (
    {"kind": "resume", "reused": ["A", "A"], "previously_attempted": []},
    {"kind": "resume", "reused": ["A"], "previously_attempted": ["A"]},
    {"kind": "new_round", "reused": ["A"], "previously_attempted": []},
    {"kind": "resume", "reused": ["D"], "previously_attempted": []},
    {"kind": "resume", "reused": [], "previously_attempted": [], "command": "ignored?"},
))
def test_invalid_invocation_metadata_is_rejected_without_rewrite(git_repo, invocation):
    begun = payload(git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1")
    path = git_repo / ".review/telemetry.json"
    ledger = json.loads(path.read_bytes())
    ledger["runs"][0]["invocation"] = invocation
    path.write_text(json.dumps(ledger))
    before = path.read_bytes()
    result = invoke(git_repo, "telemetry-summary", "--run-id", begun["telemetry"]["run_id"])
    assert result.returncode == 1
    assert result.stderr == b"PRE_PR_TRIBUNAL:TELEMETRY_INVALID\n"
    assert path.read_bytes() == before


def test_resume_appends_to_legacy_ledger_without_inventing_prior_measurements(git_repo):
    pending_b(git_repo)
    path = git_repo / ".review/telemetry.json"
    ledger = json.loads(path.read_bytes())
    ledger["schema"] = 1
    old = ledger["runs"][0]
    old["binding"]["contract"]["telemetry_schema"] = 1
    del old["invocation"], old["ended_monotonic_ns"]
    path.write_text(json.dumps(ledger))
    resumed = payload(git_repo, "telemetry-resume", "--runtime", "codex")
    persisted = json.loads(path.read_bytes())
    assert len(persisted["runs"]) == 2
    assert persisted["runs"][0] == old
    assert payload(git_repo, "telemetry-summary", "--run-id", old["run_id"])["recovery"] is None
    assert payload(git_repo, "telemetry-summary", "--run-id", resumed["run_id"])["recovery"]["reused_slot_count"] == 2


def test_reversed_invocation_clock_is_unknown_not_negative(git_repo):
    run = telemetry.create_run(git_repo, base_ref="master", runtime="codex", round_number=1,
                               started_at=NOW, started_monotonic_ns=2_000_000)
    telemetry.close_run(git_repo, run_id=run.run_id, outcome=telemetry.TelemetryOutcome.SUCCESS,
                        reason_code=None, ended_at=NOW, ended_monotonic_ns=1_000_000)
    summary = telemetry.summarize_run(git_repo, run_id=run.run_id)
    assert summary["invocation_elapsed_ms"] is None
    assert summary["anomaly_reason_codes"] == ["TELEMETRY_CLOCK_ANOMALY"]


def test_resumed_invocation_records_current_runtime_not_original_producer(git_repo):
    pending_b(git_repo)
    before = (git_repo / ".review/verdict.json").read_bytes()
    result = payload(git_repo, "telemetry-resume", "--runtime", "claude")
    run = next(item for item in telemetry.read_ledger(git_repo).runs if item.run_id == result["run_id"])
    assert run.runtime == "claude"
    assert (git_repo / ".review/verdict.json").read_bytes() == before


def test_resume_requires_runtime_without_guessing_or_writing(git_repo):
    pending_b(git_repo)
    path = git_repo / ".review/telemetry.json"
    before = path.read_bytes()
    result = invoke(git_repo, "telemetry-resume")
    assert result.returncode == 2
    assert result.stderr == b"PRE_PR_TRIBUNAL:USAGE\n"
    assert path.read_bytes() == before


def test_span_outside_invocation_bounds_does_not_publish_impossible_latency(git_repo):
    run = telemetry.create_run(git_repo, base_ref="master", runtime="codex", round_number=1,
                               started_at=NOW, started_monotonic_ns=0)
    span = telemetry.start_span(git_repo, run_id=run.run_id, stage=telemetry.TelemetryStage.REVIEWER_TOTAL,
                                reviewer=Reviewer.B, attempt=1, started_at=NOW,
                                started_monotonic_ns=1_000_000_000)
    telemetry.finish_span(git_repo, run_id=run.run_id, span_id=span.span_id,
                           outcome=telemetry.TelemetryOutcome.SUCCESS, reason_code=None,
                           ended_at=NOW, ended_monotonic_ns=4_000_000_000)
    telemetry.close_run(git_repo, run_id=run.run_id, outcome=telemetry.TelemetryOutcome.SUCCESS,
                        reason_code=None, ended_at=NOW, ended_monotonic_ns=2_000_000_000)
    summary = telemetry.summarize_run(git_repo, run_id=run.run_id)
    assert summary["invocation_elapsed_ms"] is None
    assert summary["anomaly_reason_codes"] == ["TELEMETRY_CLOCK_ANOMALY"]
