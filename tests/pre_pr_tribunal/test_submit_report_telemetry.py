"""`submit-report` owns its `report_store` span without touching its own result."""

import hashlib
import io
import json
import shutil
import sys

import pytest

from pre_pr_tribunal import cli, telemetry
from pre_pr_tribunal.model import TribunalError


BEGIN = ("begin", "--base", "master", "--runtime", "codex", "--round", "1")


def report(begun, reviewer):
    value = {
        "schema": 1, "reviewer": reviewer, "round": 1,
        "snapshot": {key: begun["snapshot"][key] for key in ("head_sha", "diff_sha256")},
        "status": "complete", "findings": [], "executions": [], "claims": [],
        "prior_decisions": [],
    }
    if reviewer == "B":
        stdout = "1 passed"
        value.update({
            "executions": [{
                "id": "B-R1-E999",
                "command": "python3 -c print-ok",
                "exit_code": 0,
                "stdout_excerpt": stdout,
                "stderr_excerpt": "",
                "capture_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
                "truncated": False,
            }],
            "claims": [{
                "id": "B-R1-C999",
                "statement": "The reviewed behavior is executable.",
                "result": "supported",
                "execution_ids": ["B-R1-E999"],
                "reason": "",
            }],
            "coverage": {"complete": True, "primary_entry_paths": []},
        })
    return json.dumps(value, indent=2).encode() + b"\n \n"


def command(monkeypatch, capsys, repo, arguments, *, raw=None, second=0):
    """Return the exact (exit code, stdout, stderr) an installed invocation produces."""
    with monkeypatch.context() as invocation:
        invocation.chdir(repo)
        if raw is not None:
            invocation.setattr(
                sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
            )
        code = cli.main(
            list(arguments),
            wall_clock=lambda: f"2026-09-09T00:00:{second:02d}Z",
            monotonic_ns=lambda: second * 1_000_000_000,
        )
        captured = capsys.readouterr()
    return code, captured.out, captured.err


def spans(repo, run_id, stage="report_store"):
    ledger = json.loads((repo / ".review/telemetry.json").read_bytes())
    run = next(item for item in ledger["runs"] if item["run_id"] == run_id)
    return [span for span in run["spans"] if span["stage"] == stage]


@pytest.fixture
def begun(git_repo, monkeypatch, capsys):
    code, out, err = command(monkeypatch, capsys, git_repo, BEGIN)
    assert (code, err) == (0, "")
    value = json.loads(out)
    assert value["telemetry"]["status"] == "active"
    return value


def control_copy(git_repo, tmp_path):
    """An identical Git/verdict copy makes submit-report stdout byte-comparable."""
    control = tmp_path / "control"
    shutil.copytree(git_repo, control)
    return control


def test_absent_run_id_and_attempt_leave_stdout_and_exit_unchanged(
    git_repo, tmp_path, monkeypatch, capsys, begun,
):
    """Omitting either flag must reproduce today's bytes and record nothing."""
    run_id = begun["telemetry"]["run_id"]
    raw = report(begun, "A")
    both = control_copy(git_repo, tmp_path)
    only_run = control_copy(git_repo, tmp_path / "run-only")
    only_attempt = control_copy(git_repo, tmp_path / "attempt-only")

    plain = command(monkeypatch, capsys, git_repo, ("submit-report", "--reviewer", "A"),
                    raw=raw, second=5)
    assert plain[0] == 0 and plain[2] == ""
    assert json.loads(plain[1])["state"] == "sealed"
    assert spans(git_repo, run_id) == []

    for repo, arguments in (
        (both, ("submit-report", "--reviewer", "A", "--run-id", run_id, "--attempt", "1")),
        (only_run, ("submit-report", "--reviewer", "A", "--run-id", run_id)),
        (only_attempt, ("submit-report", "--reviewer", "A", "--attempt", "1")),
    ):
        assert command(monkeypatch, capsys, repo, arguments, raw=raw, second=5) == plain
    assert spans(only_run, run_id) == [] and spans(only_attempt, run_id) == []
    assert len(spans(both, run_id)) == 1


@pytest.mark.parametrize("operation", ("start_span", "finish_span"))
def test_telemetry_failure_leaves_the_receipt_bytes_and_exit_untouched(
    git_repo, tmp_path, monkeypatch, capsys, begun, operation,
):
    """A raising telemetry call must not flip the exit code or drop the digest."""
    run_id = begun["telemetry"]["run_id"]
    raw = report(begun, "A")
    control = control_copy(git_repo, tmp_path)
    plain = command(monkeypatch, capsys, control, ("submit-report", "--reviewer", "A"),
                    raw=raw, second=5)
    assert plain[0] == 0 and json.loads(plain[1])["raw_sha256"]

    def broken(*args, **kwargs):
        raise RuntimeError("unbounded private exception /home/private/secret")

    monkeypatch.setattr(telemetry, operation, broken)
    observed = command(
        monkeypatch, capsys, git_repo,
        ("submit-report", "--reviewer", "A", "--run-id", run_id, "--attempt", "1"),
        raw=raw, second=5,
    )
    assert observed == plain
    assert "unbounded" not in observed[1]
    assert len(spans(git_repo, run_id)) == (0 if operation == "start_span" else 1)


def test_supplied_run_and_attempt_record_one_span_outside_the_store_lock(
    git_repo, monkeypatch, capsys, begun,
):
    """The store lock is non-reentrant: nesting costs the span, then the submission."""
    run_id = begun["telemetry"]["run_id"]
    code, out, err = command(
        monkeypatch, capsys, git_repo,
        ("submit-report", "--reviewer", "B", "--run-id", run_id, "--attempt", "2"),
        raw=report(begun, "B"), second=5,
    )
    assert (code, err) == (0, "")
    assert json.loads(out)["state"] == "sealed"
    recorded = spans(git_repo, run_id)
    assert len(recorded) == 1
    assert (recorded[0]["reviewer"], recorded[0]["attempt"], recorded[0]["status"]) == (
        "B", 2, "success",
    )
    assert recorded[0]["reason_code"] is None and recorded[0]["duration_ms"] == 0


def test_rejected_report_finishes_the_span_with_its_primary_reason_code(
    git_repo, monkeypatch, capsys, begun,
):
    """A rejection still closes the span, which only a post-lock finish can do."""
    run_id = begun["telemetry"]["run_id"]
    code, out, err = command(
        monkeypatch, capsys, git_repo,
        ("submit-report", "--reviewer", "A", "--run-id", run_id, "--attempt", "1"),
        raw=b"{", second=5,
    )
    assert (code, out, err) == (1, "", "PRE_PR_TRIBUNAL:JSON_INVALID\n")
    recorded = spans(git_repo, run_id)
    assert len(recorded) == 1
    assert (recorded[0]["status"], recorded[0]["reason_code"]) == ("failure", "JSON_INVALID")


def test_primary_exception_propagates_unchanged_through_the_finished_span(
    git_repo, monkeypatch, capsys, begun,
):
    """Finishing the span must not swallow, wrap, or replace the primary error."""
    run_id = begun["telemetry"]["run_id"]
    original = TribunalError("BASE_INVALID")

    def fail_primary(*args, **kwargs):
        raise original

    monkeypatch.setattr(cli, "submit_reviewer_report", fail_primary)
    arguments = cli._parser().parse_args(
        ["submit-report", "--reviewer", "A", "--run-id", run_id, "--attempt", "3"]
    )
    with pytest.raises(TribunalError) as captured:
        cli._submit_with_telemetry(
            git_repo, arguments, b"{}",
            wall_clock=lambda: "2026-09-09T00:00:05Z", monotonic_ns=lambda: 5_000_000_000,
        )
    assert captured.value is original
    recorded = spans(git_repo, run_id)
    assert len(recorded) == 1
    assert (recorded[0]["attempt"], recorded[0]["status"], recorded[0]["reason_code"]) == (
        3, "failure", "BASE_INVALID",
    )
