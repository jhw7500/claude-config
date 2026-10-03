import json

import pytest

from pre_pr_tribunal import evidence_runtime, round_grant, telemetry, validation
from pre_pr_tribunal.cli import _status
from pre_pr_tribunal.model import (
    GateStatus,
    Reviewer,
    SchemaError,
    ValidationBinding,
    ValidationPhase,
)
from pre_pr_tribunal.review_context import reviewer_context_body
from pre_pr_tribunal.verdict_store import (
    begin_round,
    finalize_round,
    seal_final_validation,
    submit_reviewer_report,
)
from tests.pre_pr_tribunal.test_policy import (
    NOW,
    _authorize_next_round,
    _finding,
    _git,
    _report,
    _repo,
    _seal_empty,
    _write,
)


def _failed_first_round(repo):
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.A,
        raw=_report(first, "A", findings=(_finding("A-R1-001", "A"),)),
        now=NOW,
    )
    submit_reviewer_report(
        repo, reviewer=Reviewer.B, raw=_report(first, "B"), now=NOW,
    )
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.FAIL
    return first


def _restart_after_fix(repo):
    _write(repo, "src/app.py", "fixed\n")
    _git(repo, "commit", "-qam", "fix")
    preview = _authorize_next_round(repo)
    restarted = begin_round(
        repo, base="master", runtime="codex", round_number=1, now=NOW,
    )
    return preview, restarted


def _full_suite_receipt(repo):
    captured = evidence_runtime.capture_evidence(
        repo,
        base="master",
        profile="python-v1",
        command_cwd=".",
        argv=["python3", "-I", "-S", "-c", "pass"],
        timeout_seconds=15,
    )
    assert captured["exit_code"] == 0
    return captured["receipt_sha256"]


def test_escalation_reason_must_be_nonblank():
    with pytest.raises(SchemaError, match="^VALIDATION_BINDING_INVALID$"):
        ValidationBinding(
            ValidationPhase.FINAL_VALIDATION,
            True,
            "a" * 64,
            "  ",
        ).to_json()


def test_followup_round_binds_fix_phase_in_grant_verdict_and_context(tmp_path):
    repo = _repo(tmp_path)
    first = _failed_first_round(repo)
    preview, restarted = _restart_after_fix(repo)

    assert first.validation.phase is ValidationPhase.FINAL_VALIDATION
    assert first.validation.requires_full_suite is False
    assert preview["target"]["validation"] == {
        "phase": "fix_verification",
        "requires_full_suite": True,
        "full_suite_receipt_sha256": None,
        "escalation_reason": None,
    }
    assert restarted.validation.phase is ValidationPhase.FIX_VERIFICATION
    assert restarted.validation.requires_full_suite is True
    assert reviewer_context_body(restarted, Reviewer.B)["validation"] == {
        "phase": "fix_verification",
        "planned": ["finding_reproduction", "direct_impact_tests"],
        "full_suite": "deferred_until_provisional_pass",
    }


def test_followup_pass_requires_one_snapshot_bound_final_validation_receipt(tmp_path):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _preview, restarted = _restart_after_fix(repo)
    _seal_empty(repo, restarted)

    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_REQUIRED$"):
        finalize_round(repo, now=NOW)

    receipt_sha256 = _full_suite_receipt(repo)
    sealed = seal_final_validation(repo, receipt_sha256=receipt_sha256)
    assert sealed.validation.to_json() == {
        "phase": "final_validation",
        "requires_full_suite": True,
        "full_suite_receipt_sha256": receipt_sha256,
        "escalation_reason": None,
    }
    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_ALREADY_SEALED$"):
        seal_final_validation(repo, receipt_sha256=receipt_sha256)

    final = finalize_round(repo, now=NOW)
    assert final.gate.status is GateStatus.PASS
    assert _status(final)["validation"] == sealed.validation.to_json()


def test_nonpass_escalation_is_retained_and_reauthenticated(
    tmp_path, monkeypatch,
):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _preview, restarted = _restart_after_fix(repo)
    run = telemetry.create_run(
        repo,
        base_ref="master",
        runtime="codex",
        round_number=1,
        started_at="2026-10-03T00:00:00Z",
        started_monotonic_ns=1,
    )
    telemetry.bind_run(
        repo,
        run_id=run.run_id,
        snapshot=restarted.snapshot,
        lifecycle_id=restarted.lifecycle_id,
    )
    for reviewer in restarted.policy.active_reviewers:
        findings = (
            (_finding("A-R1-002", "A"),)
            if reviewer == "A"
            else ()
        )
        submit_reviewer_report(
            repo,
            reviewer=Reviewer(reviewer),
            raw=_report(restarted, reviewer, findings=findings),
            now=NOW,
        )
    receipt_sha256 = _full_suite_receipt(repo)
    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_NOT_READY$"):
        seal_final_validation(repo, receipt_sha256=receipt_sha256)
    seal_final_validation(
        repo,
        receipt_sha256=receipt_sha256,
        escalation_reason="Cross-domain build configuration changed.",
    )

    observed = telemetry.summarize_run(repo, run_id=run.run_id)["evidence"]
    assert observed["validation_phase"] == "final_validation"
    assert observed["full_suite_required"] is True
    assert observed["full_suite_fresh_execution_count"] == 1
    assert observed["full_suite_reused_execution_count"] == 0
    assert observed["full_suite_escalation_reason"] == (
        "Cross-domain build configuration changed."
    )

    calls = []
    original = validation.verify_full_suite_receipt

    def observe_verification(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(validation, "verify_full_suite_receipt", observe_verification)
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.FAIL
    assert calls == [receipt_sha256]


def test_three_round_contract_runs_full_suite_only_for_final_candidate(tmp_path):
    repo = _repo(tmp_path, path="hooks/guard.py")
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    for reviewer in first.policy.active_reviewers:
        findings = (_finding("A-R1-001", "A"),) if reviewer == "A" else ()
        submit_reviewer_report(
            repo, reviewer=Reviewer(reviewer),
            raw=_report(first, reviewer, findings=findings), now=NOW,
        )
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.FAIL

    full_suite_executions = 0
    previous = first
    for round_number in (2, 3):
        _write(repo, "hooks/guard.py", f"fixed-{round_number}\n")
        _git(repo, "commit", "-qam", f"fix {round_number}")
        decision_path = repo / f".review/inbox/round-{round_number - 1}/decisions.json"
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        prior_finding = f"A-R{round_number - 1}-001"
        decision_path.write_text(json.dumps([{
            "id": f"D-R{round_number - 1}-A-001",
            "finding_ref": {
                "round": round_number - 1,
                "id": prior_finding,
                "reviewer": "A",
            },
            "disposition": "fixed",
            "rationale": "Covered by direct-impact verification.",
            "executions": [{
                "id": f"D-R{round_number - 1}-E001",
                "command": "python3 -m pytest -q targeted",
                "exit_code": 0,
                "stdout_excerpt": "ok",
                "stderr_excerpt": "",
                "capture_sha256": "a" * 64,
                "truncated": False,
            }],
        }]), encoding="utf-8")
        decision_path.chmod(0o600)
        _authorize_next_round(
            repo, round_number=round_number, decisions_path=decision_path,
        )
        current = begin_round(
            repo,
            base="master",
            runtime="codex",
            round_number=round_number,
            decisions_path=decision_path,
            now=NOW,
        )
        assert current.validation.phase is ValidationPhase.FIX_VERIFICATION
        for reviewer in current.policy.active_reviewers:
            blocker = round_number == 2 and reviewer == "A"
            findings = (_finding("A-R2-001", "A"),) if blocker else ()
            prior_decisions = ([{
                "decision_id": f"D-R{round_number - 1}-A-001",
                "outcome": "accepted",
                "replacement_finding_id": None,
            }] if reviewer == "A" else [])
            raw = json.loads(_report(current, reviewer, findings=findings))
            raw["prior_decisions"] = prior_decisions
            submit_reviewer_report(
                repo, reviewer=Reviewer(reviewer),
                raw=json.dumps(raw).encode(), now=NOW,
            )
        if round_number == 2:
            assert finalize_round(repo, now=NOW).gate.status is GateStatus.FAIL
        else:
            receipt_sha256 = _full_suite_receipt(repo)
            full_suite_executions += 1
            seal_final_validation(repo, receipt_sha256=receipt_sha256)
            assert finalize_round(repo, now=NOW).gate.status is GateStatus.PASS
        previous = current

    assert previous.round == 3
    assert full_suite_executions == 1
