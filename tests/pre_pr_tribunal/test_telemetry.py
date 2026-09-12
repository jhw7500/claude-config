import copy
from dataclasses import FrozenInstanceError
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from pre_pr_tribunal.git_state import capture_snapshot
from pre_pr_tribunal.model import Reviewer, SchemaError
from pre_pr_tribunal import review_store
from pre_pr_tribunal import cli, git_state
from pre_pr_tribunal.telemetry import (
    TelemetryOutcome, TelemetryStage, bind_run, close_run, create_run,
    finish_span, read_ledger, record_candidate, recover_run, start_span,
    summarize_run,
)
from pre_pr_tribunal import telemetry as telemetry_module


def NOW():
    return "2026-09-09T00:00:00Z"


LIFECYCLE = "1" * 32


@pytest.mark.parametrize("telemetry_state", ("successful", "substituted", "missing", "corrupt"))
def test_legacy_telemetry_cannot_authorize_current_report_bytes(git_repo, telemetry_state):
    from pre_pr_tribunal.verdict_store import (
        begin_round, migrate_legacy_pending_round, read_verdict, store_reviewer_report,
    )
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    legacy = pending.to_json()
    legacy["schema"] = 1
    del legacy["contract"]
    del legacy["lifecycle_id"]
    legacy["reviewers"] = {key: {"status": "pending"} for key in "ABC"}
    (git_repo / ".review/verdict.json").write_text(json.dumps(legacy))
    value = {"schema": 1, "reviewer": "A", "round": 1,
             "snapshot": {"head_sha": pending.head_sha, "diff_sha256": pending.diff_sha256},
             "status": "complete", "findings": [], "executions": [], "claims": [], "prior_decisions": []}
    raw = json.dumps(value).encode()
    store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
    if telemetry_state in {"successful", "substituted"}:
        run = create_run(git_repo, base_ref="master", runtime="codex", round_number=1,
                         started_at=NOW(), started_monotonic_ns=0)
        bind_run(git_repo, run_id=run.run_id, snapshot=pending.snapshot,
                 lifecycle_id=LIFECYCLE)
        for stage in (TelemetryStage.REPORT_STORE, TelemetryStage.REPORT_VALIDATION):
            span = start_span(git_repo, run_id=run.run_id, stage=stage, reviewer=Reviewer.A,
                              attempt=1, started_at=NOW(), started_monotonic_ns=0)
            finish_span(git_repo, run_id=run.run_id, span_id=span.span_id,
                        outcome=TelemetryOutcome.SUCCESS, reason_code=None, ended_at=NOW(), ended_monotonic_ns=1)
        if telemetry_state == "substituted":
            value["findings"] = [{"id": "A-R1-001", "reviewer": "A", "severity": "HIGH",
                                  "title": "New blocker", "rationale": "Introduces invalid state.",
                                  "path": "tracked.txt", "line": 1, "execution_ids": [],
                                  "acceptance_condition": "Reject invalid state."}]
            raw = json.dumps(value).encode() + b"\r\n"
            store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, replace_pending_recovery=True)
    elif telemetry_state == "corrupt":
        path = git_repo / ".review/telemetry.json"
        path.write_bytes(b"{")
        path.chmod(0o600)
    result = migrate_legacy_pending_round(git_repo)
    assert result.reviewers["A"] == "pending:LEGACY_PROVENANCE_UNAVAILABLE"
    assert all(slot.status == "pending" for slot in read_verdict(git_repo).reviewers.values())
    assert (git_repo / ".review/attempts/round-1/A/attempt-1.raw").read_bytes() == raw
    assert not (git_repo / ".review/inbox/round-1/A.json").exists()


def run_cli(git_repo, *arguments, input=None):
    path = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    return subprocess.run(
        [sys.executable, str(path), *arguments], cwd=git_repo,
        text=True, input=input, capture_output=True, check=False,
    )


def cli_json(repo, *arguments):
    result = run_cli(repo, *arguments)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    return json.loads(result.stdout)


BEGIN = ("begin", "--base", "master", "--runtime", "codex", "--round", "1")


def test_cli_begin_returns_snapshot_bound_telemetry_run(git_repo):
    payload = cli_json(git_repo, *BEGIN)
    run_id = payload["telemetry"]["run_id"]
    assert payload["telemetry"] == {"status": "active", "run_id": run_id}
    value = cli_json(git_repo, "telemetry-summary", "--run-id", run_id)
    assert value == cli_json(git_repo, "telemetry-summary")
    assert value["binding"]["diff_sha256"] == payload["snapshot"]["diff_sha256"]
    assert value["stages"]["snapshot_preflight"]["count"] == 1
    run = read_ledger(git_repo).runs[0]
    assert run.binding.status == "bound"
    assert run.spans[0].outcome is TelemetryOutcome.SUCCESS


def test_cli_external_lifecycle_shapes(git_repo):
    run_id = cli_json(git_repo, *BEGIN)["telemetry"]["run_id"]
    def start(stage, reviewer):
        return cli_json(git_repo, "telemetry-start", "--run-id", run_id,
                        "--stage", stage, "--reviewer", reviewer, "--attempt", "1")
    first = start("view_create", "A")
    assert first == {"run_id": run_id, "span_id": first["span_id"],
                     "stage": "view_create", "reviewer": "A", "status": "running"}
    done = cli_json(git_repo, "telemetry-finish", "--run-id", run_id,
                    "--span-id", first["span_id"], "--outcome", "success")
    assert done == {"run_id": run_id, "span_id": first["span_id"],
                    "status": "success", "duration_ms": done["duration_ms"]}
    assert type(done["duration_ms"]) is int and done["duration_ms"] >= 0
    second = start("reviewer_total", "B")
    timed = cli_json(git_repo, "telemetry-finish", "--run-id", run_id,
                     "--span-id", second["span_id"], "--outcome", "timeout",
                     "--reason-code", "REVIEWER_TIMEOUT")
    assert timed["status"] == "timeout" and timed["duration_ms"] >= 0
    start("reviewer_total", "C")
    assert cli_json(git_repo, "telemetry-recover", "--run-id", run_id) == {
        "run_id": run_id, "recovered_count": 1,
    }
    assert cli_json(git_repo, "telemetry-recover", "--run-id", run_id)["recovered_count"] == 0
    assert cli_json(git_repo, "telemetry-close", "--run-id", run_id,
                    "--outcome", "incomplete", "--reason-code", "CONTROLLER_INTERRUPTED") == {
        "run_id": run_id, "status": "incomplete",
    }
    run = read_ledger(git_repo).runs[0]
    assert run.spans[2].reason_code == "REVIEWER_TIMEOUT"
    assert run.spans[3].outcome is TelemetryOutcome.INCOMPLETE


def test_production_telemetry_outcome_contract_requires_reason_except_success():
    assert telemetry_module._status("success", None) is TelemetryOutcome.SUCCESS
    for outcome in ("failure", "timeout", "incomplete", "clock_anomaly"):
        assert telemetry_module._status(outcome, "STABLE_REASON") is TelemetryOutcome(outcome)
        with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
            telemetry_module._status(outcome, None)
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        telemetry_module._status("success", "STABLE_REASON")


@pytest.mark.parametrize("arguments", (
    ("telemetry-start", "--stage", "bad", "--attempt", "1"),
    ("telemetry-start", "--stage", "view_create", "--reviewer", "D", "--attempt", "1"),
    ("telemetry-start", "--stage", "view_create"),
    ("telemetry-finish", "--span-id", "1" * 32, "--outcome", "bad"),
    ("telemetry-close", "--outcome", "bad"),
    ("telemetry-recover", "--ended-at", "2026-09-09T00:00:00Z"),
    ("telemetry-summary", "--duration-ms", "1"),
))
def test_cli_telemetry_rejects_usage_without_reflecting_input(git_repo, arguments):
    result = run_cli(git_repo, *arguments, "--run-id", "1" * 32)
    assert (result.returncode, result.stdout, result.stderr) == (2, "", "PRE_PR_TRIBUNAL:USAGE\n")


@pytest.mark.parametrize("arguments", (
    ("telemetry-summary", "--run-id", "SECRET/path"),
    ("telemetry-recover", "--run-id", "A" * 32),
    ("telemetry-close", "--run-id", "1" * 32, "--outcome", "failure", "--reason-code", "bad\nsecret"),
    ("telemetry-start", "--run-id", "1" * 32, "--stage", "view_create", "--attempt", "0"),
    ("telemetry-finish", "--run-id", "1" * 32, "--span-id", "bad", "--outcome", "success"),
))
def test_cli_telemetry_rejects_invalid_domain(git_repo, arguments):
    result = run_cli(git_repo, *arguments)
    assert (result.returncode, result.stdout, result.stderr) == (1, "", "PRE_PR_TRIBUNAL:TELEMETRY_INVALID\n")


def invoke_clocked(monkeypatch, capsys, repo, arguments, seconds):
    monkeypatch.chdir(repo)
    code = cli.main(list(arguments), wall_clock=lambda: f"2026-09-09T00:00:{seconds:02d}Z",
                    monotonic_ns=lambda: seconds * 1_000_000_000)
    result = capsys.readouterr()
    assert code == 0, result.err
    assert result.err == ""
    return json.loads(result.out)


@pytest.mark.parametrize("incomplete", (False, True))
def test_cli_early_detection_uses_actual_validation_failure(git_repo, monkeypatch, capsys, incomplete):
    call = lambda args, sec: invoke_clocked(monkeypatch, capsys, git_repo, args, sec)
    run_id = call(BEGIN, 0)["telemetry"]["run_id"]
    spans = {}
    for reviewer in "ABC":
        spans[reviewer] = call(("telemetry-start", "--run-id", run_id,
            "--stage", "reviewer_total", "--reviewer", reviewer, "--attempt", "1"), 0)["span_id"]
    validation = call(("telemetry-start", "--run-id", run_id,
        "--stage", "report_validation", "--reviewer", "A", "--attempt", "1"), 9)["span_id"]
    before = (git_repo / ".review/verdict.json").read_bytes()
    invalid = run_cli(git_repo, "validate-report", "--reviewer", "A", "--source", "stdin", input="{")
    assert (invalid.returncode, invalid.stderr) == (1, "PRE_PR_TRIBUNAL:JSON_INVALID\n")
    reason = invalid.stderr.strip().split(":")[1]
    call(("telemetry-finish", "--run-id", run_id, "--span-id", validation,
          "--outcome", "failure", "--reason-code", reason), 10)
    for reviewer, second in (("A", 10), ("B", 20), ("C", 30)):
        if reviewer == "C" and incomplete:
            call(("telemetry-recover", "--run-id", run_id), second)
        else:
            call(("telemetry-finish", "--run-id", run_id, "--span-id", spans[reviewer],
                  "--outcome", "success"), second)
    summary = call(("telemetry-summary", "--run-id", run_id), 30)
    assert summary["early_detection"] == {
        "reviewer": "A", "reason_code": "JSON_INVALID", "detected_elapsed_ms": 10000,
        "all_reviewers_terminal_elapsed_ms": None if incomplete else 30000,
        "wait_all_delay_ms": None if incomplete else 20000,
    }
    assert (git_repo / ".review/verdict.json").read_bytes() == before


@pytest.mark.parametrize("reviewer", ("A", "B", "C"))
@pytest.mark.parametrize("reason", ("JSON_INVALID", "TEXT_INVALID"))
def test_cli_early_detection_includes_rejected_v2_submission(
    git_repo, monkeypatch, capsys, reviewer, reason,
):
    """Dropping report_store failures must lose an actual submit-report rejection."""
    call = lambda args, sec: invoke_clocked(monkeypatch, capsys, git_repo, args, sec)
    begun = call(BEGIN, 0)
    run_id = begun["telemetry"]["run_id"]
    totals = {
        role: call(("telemetry-start", "--run-id", run_id,
                    "--stage", "reviewer_total", "--reviewer", role, "--attempt", "1"), 0)["span_id"]
        for role in "ABC"
    }
    raw = "{"
    if reason == "TEXT_INVALID":
        raw = json.dumps({
            "schema": 1, "reviewer": reviewer, "round": 1,
            "snapshot": {
                "head_sha": begun["snapshot"]["head_sha"],
                "diff_sha256": begun["snapshot"]["diff_sha256"],
            },
            "status": "complete", "executions": [], "claims": [], "prior_decisions": [],
            "findings": [{
                "id": f"{reviewer}-R1-001", "reviewer": reviewer, "severity": "LOW",
                "title": "invalid\x00title", "rationale": "Invalid report text.",
                "path": "tracked.txt", "line": 1, "execution_ids": [],
                "acceptance_condition": "Reject the invalid text.",
            }],
        })
    store = call(("telemetry-start", "--run-id", run_id, "--stage", "report_store",
                  "--reviewer", reviewer, "--attempt", "1"), 9)["span_id"]
    rejected = run_cli(git_repo, "submit-report", "--reviewer", reviewer, input=raw)
    assert (rejected.returncode, rejected.stdout, rejected.stderr) == (
        1, "", f"PRE_PR_TRIBUNAL:{reason}\n",
    )
    verdict_path = git_repo / ".review/verdict.json"
    after_rejection = verdict_path.read_bytes()
    verdict = json.loads(after_rejection)
    assert verdict["gate"]["status"] == "in_progress"
    assert all(slot["state"] == "pending" for slot in verdict["reviewers"].values())
    assert verdict["reviewers"][reviewer]["attempt_count"] == 1
    evidence = git_repo / f".review/attempts/round-1/{reviewer}/attempt-1.raw"
    assert evidence.read_bytes() == raw.encode()
    call(("telemetry-finish", "--run-id", run_id, "--span-id", store,
          "--outcome", "failure", "--reason-code", reason), 10)

    # Detect the rejection while peers are still running, without inventing their finish times.
    assert call(("telemetry-summary", "--run-id", run_id), 10)["early_detection"] == {
        "reviewer": reviewer, "reason_code": reason, "detected_elapsed_ms": 10000,
        "all_reviewers_terminal_elapsed_ms": None, "wait_all_delay_ms": None,
    }
    blocked = run_cli(git_repo, "finalize")
    assert (blocked.returncode, blocked.stderr) == (1, "PRE_PR_TRIBUNAL:ROUND_NOT_READY\n")
    for role, second in (("A", 10), ("B", 20), ("C", 30)):
        call(("telemetry-finish", "--run-id", run_id, "--span-id", totals[role],
              "--outcome", "success"), second)
    assert call(("telemetry-summary", "--run-id", run_id), 30)["early_detection"] == {
        "reviewer": reviewer, "reason_code": reason, "detected_elapsed_ms": 10000,
        "all_reviewers_terminal_elapsed_ms": 30000, "wait_all_delay_ms": 20000,
    }
    assert verdict_path.read_bytes() == after_rejection
    assert evidence.read_bytes() == raw.encode()



def test_cli_candidate_is_bounded_provisional_identity(git_repo, monkeypatch):
    # Candidate capture must still work with missing base, dirty tree, and hostile inherited Git env.
    subprocess.run(["/usr/bin/git", "-C", str(git_repo), "update-ref", "-d", "refs/remotes/origin/master"], check=True)
    (git_repo / "tracked.txt").write_text("dirty")
    monkeypatch.setenv("GIT_DIR", "/missing")
    candidate = git_state.capture_telemetry_candidate(git_repo)
    assert candidate.repository == "jhw7500/claude-config"
    assert candidate.head_ref == "refs/heads/feature"
    assert len(candidate.head_sha) == 40
    with pytest.raises(FrozenInstanceError):
        candidate.head_ref = "refs/heads/other"


def test_cli_begin_replaces_stale_candidate(git_repo, monkeypatch, capsys):
    monkeypatch.setattr(cli, "capture_telemetry_candidate", lambda cwd:
        git_state.TelemetryCandidate("other/repo", "refs/heads/old", "1" * 40))
    payload = invoke_clocked(monkeypatch, capsys, git_repo, BEGIN, 0)
    binding = read_ledger(git_repo).runs[0].binding
    assert binding.repository == payload["snapshot"]["repository"]
    assert binding.head_ref == payload["snapshot"]["head_ref"]
    assert binding.head_sha == payload["snapshot"]["head_sha"]


def test_cli_begin_failure_closes_pending_preflight_with_primary_code(git_repo):
    result = run_cli(git_repo, "begin", "--base", "missing", "--runtime", "codex", "--round", "1")
    assert (result.returncode, result.stdout, result.stderr) == (1, "", "PRE_PR_TRIBUNAL:BASE_INVALID\n")
    run = read_ledger(git_repo).runs[0]
    assert run.binding.status == "pending"
    assert run.binding.head_ref == "refs/heads/feature"
    assert run.outcome is TelemetryOutcome.FAILURE and run.reason_code == "BASE_INVALID"
    assert run.spans[0].outcome is TelemetryOutcome.FAILURE
    assert run.spans[0].reason_code == "BASE_INVALID"


def corrupt_telemetry(repo, kind):
    target = ledger_path(repo)
    target.parent.mkdir(mode=0o700, exist_ok=True)
    if kind == "symlink":
        outside = repo.parent / (repo.name + "-telemetry-target")
        outside.write_bytes(b"untouched")
        target.symlink_to(outside)
    else:
        raw = b"{" if kind == "malformed" else b" " * (2 * 1024 * 1024 + 1)
        if kind == "mode":
            raw = b'{"schema":1,"runs":[]}'
        target.write_bytes(raw)
        target.chmod(0o644 if kind == "mode" else 0o600)
    return target.read_bytes(), target.lstat()


@pytest.mark.parametrize(("kind", "reason"), (
    ("malformed", "TELEMETRY_INVALID"), ("oversized", "TELEMETRY_TOO_LARGE"),
    ("symlink", "TELEMETRY_FILE_UNSAFE"), ("mode", "TELEMETRY_FILE_UNSAFE"),
))
@pytest.mark.parametrize("invalid_report", (False, True))
def test_telemetry_cannot_alter_tribunal_result(git_repo, tmp_path, monkeypatch, capsys, kind, reason, invalid_report):
    # Fixed primary clocks and lifecycle IDs make verdict bytes comparable across identical Git copies.
    original_begin, original_finalize = cli.begin_round, cli.finalize_round
    monkeypatch.setattr(
        cli, "begin_round",
        lambda *a, **kw: original_begin(*a, **kw, now=NOW, token_hex=lambda _size: LIFECYCLE),
    )
    monkeypatch.setattr(cli, "finalize_round", lambda *a, **kw: original_finalize(*a, **kw, now=NOW))
    damaged = tmp_path / "damaged"
    shutil.copytree(git_repo, damaged)
    before, metadata = corrupt_telemetry(damaged, kind)
    def command(repo, args, report_raw=None):
        with monkeypatch.context() as invocation:
            invocation.chdir(repo)
            if report_raw is not None:
                invocation.setattr(
                    sys, "stdin", io.TextIOWrapper(io.BytesIO(report_raw), encoding="utf-8")
                )
            code = cli.main(list(args))
            captured = capsys.readouterr()
        return code, captured.out, captured.err
    results = []
    for repo in (git_repo, damaged):
        begun = command(repo, BEGIN)
        assert begun[0] == 0 and begun[2] == ""
        value = json.loads(begun[1])
        if repo == damaged:
            assert value["telemetry"] == {"status": "unavailable", "reason_code": reason}
        pending_bytes = (repo / ".review/verdict.json").read_bytes()
        validation_results = []
        report_bytes = {}
        for reviewer in "ABC":
            raw = json.dumps({"schema": 1, "reviewer": reviewer, "round": 1,
                "snapshot": {"head_sha": value["snapshot"]["head_sha"], "diff_sha256": value["snapshot"]["diff_sha256"]},
                "status": "complete", "findings": [], "executions": [], "claims": [], "prior_decisions": []})
            if invalid_report and reviewer == "A":
                raw = "{"
            submitted = command(
                repo, ("submit-report", "--reviewer", reviewer), raw.encode()
            )
            if submitted[0] != 0:
                validation_results.append(
                    (submitted[0], submitted[1], submitted[2].strip().split(":")[1])
                )
                path = repo / f".review/attempts/round-1/{reviewer}/attempt-1.raw"
            else:
                validation_results.append(
                    (submitted[0], json.loads(submitted[1])["raw_sha256"], submitted[2])
                )
                path = repo / f".review/inbox/round-1/{reviewer}.json"
            report_bytes[reviewer] = path.read_bytes()
        finalized = command(repo, ("finalize",))
        gates = []
        for adapter_name in ("codex_hook.py", "claude_hook.py"):
            adapter = Path(cli.__file__).with_name(adapter_name)
            gate = subprocess.run([sys.executable, str(adapter)], cwd=repo, capture_output=True,
                input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(repo),
                    "tool_input": {"command": "PATH=/usr/bin:/bin /usr/bin/gh pr create --base master"}}).encode())
            assert gate.returncode == 0 and gate.stderr == b""
            if invalid_report:
                assert json.loads(gate.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
            else:
                assert gate.stdout == b""
            gates.append((gate.returncode, gate.stdout, gate.stderr))
        results.append((pending_bytes, validation_results, finalized,
            (repo / ".review/verdict.json").read_bytes(), report_bytes, gates))
    assert results[0] == results[1]
    assert results[0][2][0] == (1 if invalid_report else 0)
    assert results[0][1][0][0] == (1 if invalid_report else 0)
    assert ledger_path(damaged).read_bytes() == before
    after = ledger_path(damaged).lstat()
    # Reads may update atime; persistence must preserve inode, owner, mode and modification times.
    for name in ("st_ino", "st_uid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"):
        assert getattr(after, name) == getattr(metadata, name)


@pytest.mark.parametrize("kind", ("malformed", "oversized", "symlink", "mode"))
def test_cli_bad_telemetry_preserves_primary_begin_failure(git_repo, kind):
    corrupt_telemetry(git_repo, kind)
    result = run_cli(git_repo, "begin", "--base", "missing", "--runtime", "codex", "--round", "1")
    assert (result.returncode, result.stdout, result.stderr) == (1, "", "PRE_PR_TRIBUNAL:BASE_INVALID\n")


@pytest.mark.parametrize("kind", ("unignored", "tracked", "tracked_sibling"))
def test_cli_unsafe_namespace_preserves_primary_rejection(git_repo, tmp_path, monkeypatch, capsys, kind):
    from pre_pr_tribunal.verdict_store import begin_round
    if kind == "unignored":
        (git_repo / ".gitignore").write_text(".review/verdict.json\n.review/lock\n")
    else:
        ledger_path(git_repo).parent.mkdir(mode=0o700)
        tracked = ledger_path(git_repo) if kind == "tracked" else git_repo / ".review/retained.json"
        tracked.write_bytes(b'{"schema":1,"runs":[]}')
        tracked.chmod(0o600)
        subprocess.run(["/usr/bin/git", "-C", str(git_repo), "add", "-f", str(tracked)], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(git_repo), "commit", "-qam", "telemetry ignore boundary"], check=True)
    control = tmp_path / "control"
    shutil.copytree(git_repo, control)
    with pytest.raises(SchemaError, match="^VERDICT_NOT_IGNORED$"):
        begin_round(control, base="master", runtime="codex", round_number=1, now=NOW)
    monkeypatch.setattr(cli, "begin_round", lambda *a, **kw: begin_round(*a, **kw, now=NOW))
    monkeypatch.chdir(git_repo)
    code = cli.main(list(BEGIN), wall_clock=NOW, monotonic_ns=lambda: 0)
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (1, "", "PRE_PR_TRIBUNAL:VERDICT_NOT_IGNORED\n")
    assert not (git_repo / ".review/verdict.json").exists()
    assert not (control / ".review/verdict.json").exists()
    if kind != "tracked":
        assert not ledger_path(git_repo).exists()
    else:
        assert ledger_path(git_repo).read_bytes() == b'{"schema":1,"runs":[]}'
    assert subprocess.check_output(["/usr/bin/git", "-C", str(git_repo), "status", "--porcelain"]) == b""


def test_cli_individual_ignores_reject_before_telemetry_rename(
    git_repo, tmp_path, monkeypatch, capsys,
):
    from pre_pr_tribunal.verdict_store import begin_round

    (git_repo / ".gitignore").write_text(
        ".review/verdict.json\n.review/lock\n.review/telemetry.json\n"
    )
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "commit", "-qam", "individual artifact ignores"],
        check=True,
    )
    control = tmp_path / "without-telemetry"
    shutil.copytree(git_repo, control)
    with pytest.raises(SchemaError, match="^VERDICT_NOT_IGNORED$"):
        begin_round(control, base="master", runtime="codex", round_number=1, now=NOW)
    real_replace = review_store.os.replace

    def fail_telemetry_rename(source, destination, **kwargs):
        if destination == "telemetry.json":
            raise OSError("injected telemetry rename failure")
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(review_store.os, "replace", fail_telemetry_rename)
    monkeypatch.setattr(cli, "begin_round", lambda *a, **kw: begin_round(*a, **kw, now=NOW))
    monkeypatch.chdir(git_repo)
    code = cli.main(list(BEGIN), wall_clock=NOW, monotonic_ns=lambda: 0)
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (1, "", "PRE_PR_TRIBUNAL:VERDICT_NOT_IGNORED\n")
    assert not (git_repo / ".review").exists()
    assert not (control / ".review").exists()
    assert not ledger_path(git_repo).exists()
    assert list((git_repo / ".review").glob(".tmp.*")) == []
    for repo in (control, git_repo):
        assert subprocess.check_output([
            "/usr/bin/git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all",
        ]) == b""
    assert sorted(path.relative_to(git_repo) for path in (git_repo / ".review").rglob("*")) == sorted(
        path.relative_to(control) for path in (control / ".review").rglob("*")
    )


@pytest.mark.parametrize("exposed_child", ("telemetry.json", ".tmp.*"))
@pytest.mark.parametrize("failed_rename", (False, True))
def test_cli_wildcard_negations_cannot_dirty_primary_begin(
    git_repo, tmp_path, monkeypatch, capsys, exposed_child, failed_rename,
):
    from pre_pr_tribunal.verdict_store import begin_round

    (git_repo / ".gitignore").write_text(f".review/*\n!.review/{exposed_child}\n")
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "commit", "-qam", "negated telemetry child"],
        check=True,
    )
    control = tmp_path / "without-telemetry"
    shutil.copytree(git_repo, control)
    monkeypatch.setattr(cli, "begin_round", lambda *a, **kw: begin_round(*a, **kw, now=NOW))

    def command(repo):
        monkeypatch.chdir(repo)
        code = cli.main(list(BEGIN), wall_clock=NOW, monotonic_ns=lambda: 0)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def disable_telemetry(_cwd):
        raise git_state.GitStateError("TELEMETRY_FILE_UNSAFE")

    # Disable only telemetry in the control; the primary CLI transaction is real.
    with monkeypatch.context() as disabled:
        disabled.setattr(cli, "check_telemetry_ignored", disable_telemetry)
        expected = command(control)
    assert expected == (1, "", "PRE_PR_TRIBUNAL:VERDICT_NOT_IGNORED\n")
    assert subprocess.check_output([
        "/usr/bin/git", "-C", str(control), "status", "--porcelain", "--untracked-files=all",
    ]) == b""

    real_replace = review_store.os.replace

    def fail_telemetry_rename(source, destination, **kwargs):
        if destination == "telemetry.json":
            raise OSError("injected telemetry rename failure")
        return real_replace(source, destination, **kwargs)

    if failed_rename:
        monkeypatch.setattr(review_store.os, "replace", fail_telemetry_rename)
    actual = command(git_repo)
    assert actual == expected
    assert not (git_repo / ".review").exists()
    assert not (control / ".review").exists()
    assert not ledger_path(git_repo).exists()
    assert list((git_repo / ".review").glob(".tmp.*")) == []
    assert sorted(path.relative_to(git_repo) for path in (git_repo / ".review").rglob("*")) == sorted(
        path.relative_to(control) for path in (control / ".review").rglob("*")
    )
    assert subprocess.check_output([
        "/usr/bin/git", "-C", str(git_repo), "status", "--porcelain", "--untracked-files=all",
    ]) == b""


@pytest.mark.parametrize("operation", ("create_run", "start_span", "record_candidate", "bind_run", "finish_span", "close_run"))
@pytest.mark.parametrize("primary_failure", (False, True))
def test_cli_telemetry_exception_cannot_replace_primary_result(git_repo, monkeypatch, capsys, operation, primary_failure):
    from pre_pr_tribunal import telemetry
    from pre_pr_tribunal.model import TribunalError
    from pre_pr_tribunal.verdict_store import begin_round
    original_error = TribunalError("BASE_INVALID")
    def broken(*args, **kwargs):
        raise RuntimeError("unbounded private exception /home/private/secret")
    monkeypatch.setattr(telemetry, operation, broken)
    if primary_failure:
        def fail_primary(*args, **kwargs):
            raise original_error
        monkeypatch.setattr(cli, "begin_round", fail_primary)
        with pytest.raises(TribunalError) as captured:
            cli._begin_with_telemetry(git_repo, cli._parser().parse_args(BEGIN), wall_clock=NOW, monotonic_ns=lambda: 0)
        assert captured.value is original_error
    else:
        monkeypatch.setattr(cli, "begin_round", lambda *a, **kw: begin_round(*a, **kw, now=NOW))
        result = invoke_clocked(monkeypatch, capsys, git_repo, BEGIN, 0)
        assert result["gate"]["status"] == "in_progress"
        assert "unbounded" not in json.dumps(result)


def test_cli_finish_clock_anomaly_returns_null_duration(git_repo, monkeypatch, capsys):
    call = lambda args, sec: invoke_clocked(monkeypatch, capsys, git_repo, args, sec)
    run_id = call(BEGIN, 0)["telemetry"]["run_id"]
    span_id = call(("telemetry-start", "--run-id", run_id, "--stage", "finalize", "--attempt", "1"), 10)["span_id"]
    result = call(("telemetry-finish", "--run-id", run_id, "--span-id", span_id, "--outcome", "success"), 9)
    assert result == {"run_id": run_id, "span_id": span_id, "status": "clock_anomaly", "duration_ms": None}


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
    bind_run(git_repo, run_id=run.run_id, snapshot=snapshot,
             lifecycle_id=LIFECYCLE)
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
        "report_text": 2, "diff_recipe": 1, "telemetry_schema": 3,
    }
    assert summary["stages"]["reviewer_total"] == {"count": 1, "total_ms": 4000}
    assert summary["early_detection"] is None
    assert set(summary) == {
        "schema", "binding", "reviewers", "stages", "outcomes",
        "telemetry_incomplete", "anomaly_reason_codes", "early_detection",
        "recovery", "invocation_elapsed_ms",
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


def test_schema_three_bound_run_records_lifecycle_id(git_repo):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = new_run(git_repo)
    bound = bind_run(
        git_repo, run_id=run.run_id, snapshot=snapshot,
        lifecycle_id=LIFECYCLE,
    )
    raw = json.loads(ledger_path(git_repo).read_bytes())["runs"][0]
    assert raw["lifecycle_id"] == LIFECYCLE
    assert raw["invocation"] is None
    assert summarize_run(git_repo, run_id=bound.run_id)["recovery"]["kind"] == "new_round"


def test_schema_three_rejects_persisted_new_round_invocation_without_rewrite(git_repo):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = new_run(git_repo)
    bind_run(
        git_repo, run_id=run.run_id, snapshot=snapshot,
        lifecycle_id=LIFECYCLE,
    )
    path = ledger_path(git_repo)
    value = json.loads(path.read_bytes())
    value["runs"][0]["invocation"] = {
        "kind": "new_round", "reused": [], "previously_attempted": [],
    }
    corrupted = json.dumps(value).encode()
    path.write_bytes(corrupted)
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        read_ledger(git_repo)
    assert path.read_bytes() == corrupted


def test_schema_two_new_round_invocation_remains_readable(git_repo):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = new_run(git_repo)
    bind_run(
        git_repo, run_id=run.run_id, snapshot=snapshot,
        lifecycle_id=LIFECYCLE,
    )
    path = ledger_path(git_repo)
    value = json.loads(path.read_bytes())
    value["schema"] = 2
    stored = value["runs"][0]
    stored["binding"]["contract"]["telemetry_schema"] = 2
    del stored["lifecycle_id"]
    stored["invocation"] = {
        "kind": "new_round", "reused": [], "previously_attempted": [],
    }
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    parsed = read_ledger(git_repo).runs[0]
    assert parsed.invocation == telemetry_module.Invocation("new_round", (), ())
    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    ("status", "ended_at", "ended_monotonic_ns"),
    (("success", NOW(), None), ("running", None, 5)),
)
def test_schema_three_rejects_run_end_state_mismatch(
    git_repo, status, ended_at, ended_monotonic_ns,
):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = new_run(git_repo)
    bind_run(
        git_repo, run_id=run.run_id, snapshot=snapshot,
        lifecycle_id=LIFECYCLE,
    )
    close_run(
        git_repo, run_id=run.run_id, outcome=TelemetryOutcome.SUCCESS,
        reason_code=None, ended_at=NOW(), ended_monotonic_ns=2,
    )
    path = ledger_path(git_repo)
    value = json.loads(path.read_bytes())
    stored = value["runs"][0]
    stored["status"] = status
    stored["ended_at"] = ended_at
    stored["reason_code"] = None
    stored["ended_monotonic_ns"] = ended_monotonic_ns
    corrupted = json.dumps(value).encode()
    path.write_bytes(corrupted)
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        read_ledger(git_repo)
    assert path.read_bytes() == corrupted


def test_schema_two_terminal_null_monotonic_end_remains_readable(git_repo):
    run = new_run(git_repo)
    path = ledger_path(git_repo)
    value = json.loads(path.read_bytes())
    value["schema"] = 2
    stored = value["runs"][0]
    stored["binding"]["contract"]["telemetry_schema"] = 2
    del stored["lifecycle_id"]
    stored["invocation"] = None
    stored["ended_monotonic_ns"] = None
    stored["status"] = "success"
    stored["ended_at"] = NOW()
    stored["reason_code"] = None
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    assert read_ledger(git_repo).runs[0].ended_monotonic_ns is None
    assert path.read_bytes() == raw


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
                      reason_code=None, ended_at="2026-09-09T00:00:01Z",
                      ended_monotonic_ns=1)
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
                      reason_code="CONTROLLER_INTERRUPTED", ended_at=NOW(),
                      ended_monotonic_ns=1)
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
    ("ledger", "schema", 4), ("ledger", "unknown", 1),
    ("run", "runtime", "unknown"), ("run", "round", True),
    ("run", "round", 4), ("run", "run_id", "a" * 31),
    ("run", "lifecycle_id", None), ("run", "lifecycle_id", "A" * 32),
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
        payload = b'{"schema":2,' + payload[1:]
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
    bound = bind_run(git_repo, run_id=run.run_id, snapshot=snapshot,
                     lifecycle_id=LIFECYCLE)
    assert bound.binding.status == "bound"
    before = ledger_path(git_repo).read_bytes()
    for action in (
        lambda: bind_run(git_repo, run_id=run.run_id, snapshot=snapshot,
                         lifecycle_id=LIFECYCLE),
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
    bound = bind_run(git_repo, run_id=run.run_id, snapshot=snapshot,
                     lifecycle_id=LIFECYCLE)
    assert bound.binding.repository == snapshot.repository
    assert bound.binding.head_ref == snapshot.head_ref
    assert bound.binding.head_sha == snapshot.head_sha
    assert bound.binding.diff_sha256 == snapshot.diff_sha256
    before = ledger_path(git_repo).read_bytes()
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        bind_run(git_repo, run_id=run.run_id, snapshot=snapshot,
                 lifecycle_id=LIFECYCLE)
    assert ledger_path(git_repo).read_bytes() == before


def test_closed_run_mutations_are_rejected(git_repo):
    run = new_run(git_repo)
    close_run(git_repo, run_id=run.run_id, outcome=TelemetryOutcome.FAILURE,
              reason_code="SNAPSHOT_CHANGED", ended_at=NOW(), ended_monotonic_ns=1)
    before = ledger_path(git_repo).read_bytes()
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        start_span(git_repo, run_id=run.run_id, stage=TelemetryStage.SNAPSHOT_PREFLIGHT,
                   reviewer=None, attempt=1, started_at=NOW(), started_monotonic_ns=1)
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        close_run(git_repo, run_id=run.run_id, outcome=TelemetryOutcome.SUCCESS,
                  reason_code=None, ended_at=NOW(), ended_monotonic_ns=1)
    assert ledger_path(git_repo).read_bytes() == before


def test_binding_preserves_requested_base(git_repo):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = create_run(git_repo, base_ref="another-base", runtime="codex", round_number=1,
                     started_at=NOW(), started_monotonic_ns=1)
    before = ledger_path(git_repo).read_bytes()
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        bind_run(git_repo, run_id=run.run_id, snapshot=snapshot,
                 lifecycle_id=LIFECYCLE)
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
                  reason_code=None, ended_at="2026-09-09T00:00:12Z",
                  ended_monotonic_ns=1_000_010_001)
    assert ledger_path(git_repo).read_bytes() == before
    terminal = finish(git_repo, run, span)
    closed = close_run(git_repo, run_id=run.run_id, outcome=TelemetryOutcome.SUCCESS,
                       reason_code=None, ended_at="2026-09-09T00:00:12Z",
                       ended_monotonic_ns=1_000_010_001)
    assert closed.spans == (terminal,)
    assert closed.outcome is TelemetryOutcome.SUCCESS


@pytest.mark.parametrize("first_stage", (
    TelemetryStage.REPORT_STORE, TelemetryStage.REPORT_VALIDATION,
))
def test_early_detection_preserves_first_report_failure_across_retries(git_repo, first_stage):
    """Choosing only one report stage, list order, or the latest retry loses the first error."""
    run = new_run(git_repo)
    later_stage = (
        TelemetryStage.REPORT_VALIDATION
        if first_stage is TelemetryStage.REPORT_STORE else TelemetryStage.REPORT_STORE
    )
    for stage, reviewer, attempt, second, reason in (
        (later_stage, Reviewer.B, 1, 20, "REPORT_SCHEMA_INVALID"),
        (first_stage, Reviewer.A, 1, 10, "JSON_INVALID"),
        (TelemetryStage.REPORT_STORE, Reviewer.A, 2, 25, "TEXT_INVALID"),
    ):
        span = start_span(
            git_repo, run_id=run.run_id, stage=stage, reviewer=reviewer, attempt=attempt,
            started_at=NOW(), started_monotonic_ns=1,
        )
        finish_span(
            git_repo, run_id=run.run_id, span_id=span.span_id,
            outcome=TelemetryOutcome.FAILURE, reason_code=reason,
            ended_at=f"2026-09-09T00:00:{second:02d}Z",
            ended_monotonic_ns=second * 1_000_000_000 + 1,
        )
    before = ledger_path(git_repo).read_bytes()
    assert summarize_run(git_repo)["early_detection"] == {
        "reviewer": "A", "reason_code": "JSON_INVALID", "detected_elapsed_ms": 10000,
        "all_reviewers_terminal_elapsed_ms": None, "wait_all_delay_ms": None,
    }
    assert ledger_path(git_repo).read_bytes() == before


@pytest.mark.parametrize(("stage", "outcome", "reason"), (
    (TelemetryStage.REPORT_STORE, TelemetryOutcome.SUCCESS, None),
    (TelemetryStage.REPORT_STORE, TelemetryOutcome.TIMEOUT, "REVIEWER_TIMEOUT"),
    (TelemetryStage.REPORT_STORE, TelemetryOutcome.INCOMPLETE, "CONTROLLER_INTERRUPTED"),
    (TelemetryStage.REPORT_STORE, TelemetryOutcome.CLOCK_ANOMALY, "TELEMETRY_CLOCK_ANOMALY"),
    (TelemetryStage.REVIEWER_TOTAL, TelemetryOutcome.FAILURE, "JSON_INVALID"),
))
def test_early_detection_ignores_nonfailures_and_nonreport_stages(git_repo, stage, outcome, reason):
    """Including successful/uncertain stores or unrelated failures creates false detections."""
    run = new_run(git_repo)
    span = start_span(
        git_repo, run_id=run.run_id, stage=stage, reviewer=Reviewer.A, attempt=1,
        started_at=NOW(), started_monotonic_ns=1,
    )
    finish_span(
        git_repo, run_id=run.run_id, span_id=span.span_id,
        outcome=outcome, reason_code=reason,
        ended_at="2026-09-09T00:00:10Z", ended_monotonic_ns=10_000_000_001,
    )
    assert summarize_run(git_repo)["early_detection"] is None



@pytest.mark.parametrize("stage", (TelemetryStage.REPORT_VALIDATION, TelemetryStage.REPORT_STORE))
@pytest.mark.parametrize("last_outcome", (TelemetryOutcome.SUCCESS, TelemetryOutcome.INCOMPLETE, TelemetryOutcome.CLOCK_ANOMALY, None))
def test_early_detection_requires_all_three_usable_terminal_milestones(git_repo, stage, last_outcome):
    run = new_run(git_repo)
    for reviewer, seconds in ((Reviewer.A, 5), (Reviewer.B, 20), (Reviewer.C, 30)):
        span = start_span(git_repo, run_id=run.run_id, stage=TelemetryStage.REVIEWER_TOTAL,
                          reviewer=reviewer, attempt=1, started_at=NOW(), started_monotonic_ns=1)
        outcome = last_outcome if reviewer is Reviewer.C else TelemetryOutcome.SUCCESS
        if outcome is not None:
            finish_span(git_repo, run_id=run.run_id, span_id=span.span_id, outcome=outcome,
                        reason_code=None if outcome is TelemetryOutcome.SUCCESS else "CONTROLLER_INTERRUPTED",
                        ended_at=f"2026-09-09T00:00:{seconds:02d}Z", ended_monotonic_ns=seconds * 1_000_000_000 + 1)
    validation = start_span(git_repo, run_id=run.run_id, stage=stage,
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
    assert read_ledger(git_repo).to_json() == {"schema": 3, "runs": []}
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
