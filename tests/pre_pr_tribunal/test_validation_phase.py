import json

import pytest

from pre_pr_tribunal import (
    evidence_runtime,
    evidence_store,
    round_grant,
    telemetry,
    validation,
)
from pre_pr_tribunal.cli import _status
from pre_pr_tribunal.evidence_environment import PYTHON_TRACKED_READ_PROGRAM
from pre_pr_tribunal.model import (
    GateStatus,
    FullSuiteKind,
    Reviewer,
    SchemaError,
    ValidationBinding,
    ValidationPhase,
    full_suite_recipe,
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
    _write(repo, "test_full_suite_probe.py", "def test_probe():\n    assert True\n")
    _git(repo, "add", "test_full_suite_probe.py")
    _git(repo, "commit", "-qm", "add full-suite probe")
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
    recipe = full_suite_recipe()
    source_sha256 = _no_op_receipt(repo)
    receipt = evidence_store.read_receipt(repo, source_sha256)
    receipt["entry"] = {
        **receipt["entry"],
        "argv": list(recipe.argv),
        "cwd": recipe.cwd,
    }
    return evidence_store.put_receipt(repo, receipt)


def _no_op_receipt(repo):
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


def _targeted_receipt(repo):
    captured = evidence_runtime.capture_evidence(
        repo,
        base="master",
        profile="python-v1",
        command_cwd=".",
        argv=[
            "python3",
            "-I",
            "-S",
            "-c",
            PYTHON_TRACKED_READ_PROGRAM,
            "src/app.py",
        ],
        timeout_seconds=15,
    )
    assert captured["exit_code"] == 0
    return captured["receipt_sha256"]


def _direct_impact_report(verdict, reviewer, *, findings=()):
    raw = json.loads(_report(verdict, reviewer, findings=findings))
    if reviewer == "B":
        raw["executions"][0]["command"] = (
            "python3 -m pytest -q tests/test_direct_impact.py"
        )
    return json.dumps(raw, separators=(",", ":")).encode()


def _seal_direct_impact_reports(repo, verdict, *, blocker=False):
    for reviewer in verdict.policy.active_reviewers:
        findings = (
            (_finding(f"A-R{verdict.round}-002", "A"),)
            if blocker and reviewer == "A"
            else ()
        )
        submit_reviewer_report(
            repo,
            reviewer=Reviewer(reviewer),
            raw=_direct_impact_report(verdict, reviewer, findings=findings),
            now=NOW,
        )


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
        "full_suite_recipe": full_suite_recipe().to_json(),
    }
    assert restarted.validation.phase is ValidationPhase.FIX_VERIFICATION
    assert restarted.validation.requires_full_suite is True
    assert reviewer_context_body(restarted, Reviewer.B)["validation"] == {
        "phase": "fix_verification",
        "planned": ["finding_reproduction", "direct_impact_tests"],
        "full_suite": "deferred_until_provisional_pass",
        "full_suite_recipe": full_suite_recipe().to_json(),
    }


def test_followup_pass_requires_one_snapshot_bound_final_validation_receipt(tmp_path):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _preview, restarted = _restart_after_fix(repo)
    _seal_direct_impact_reports(repo, restarted)

    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_REQUIRED$"):
        finalize_round(repo, now=NOW)

    receipt_sha256 = _full_suite_receipt(repo)
    sealed = seal_final_validation(repo, receipt_sha256=receipt_sha256)
    assert sealed.validation.to_json() == {
        "phase": "final_validation",
        "requires_full_suite": True,
        "full_suite_receipt_sha256": receipt_sha256,
        "escalation_reason": None,
        "full_suite_recipe": full_suite_recipe().to_json(),
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
        submit_reviewer_report(
            repo,
            reviewer=Reviewer(reviewer),
            raw=_direct_impact_report(
                restarted,
                reviewer,
                findings=(
                    (_finding("A-R1-002", "A"),)
                    if reviewer == "A"
                    else ()
                ),
            ),
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
            raw = json.loads(
                _direct_impact_report(current, reviewer, findings=findings)
            )
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


@pytest.mark.parametrize("receipt_factory", (_no_op_receipt, _targeted_receipt))
def test_non_suite_receipt_cannot_seal_final_validation(
    tmp_path, receipt_factory,
):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _preview, restarted = _restart_after_fix(repo)
    _seal_direct_impact_reports(repo, restarted)

    with pytest.raises(
        SchemaError, match="^FINAL_VALIDATION_RECIPE_MISMATCH$"
    ):
        seal_final_validation(repo, receipt_sha256=receipt_factory(repo))


def test_full_suite_execution_is_rejected_during_fix_verification(tmp_path):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _preview, restarted = _restart_after_fix(repo)

    raw = json.loads(_report(restarted, "B"))
    assert raw["executions"][0]["command"] == full_suite_recipe().command
    with pytest.raises(
        SchemaError, match="^FULL_SUITE_DURING_FIX_VERIFICATION$"
    ):
        submit_reviewer_report(
            repo,
            reviewer=Reviewer.B,
            raw=json.dumps(raw, separators=(",", ":")).encode(),
            now=NOW,
        )


def test_node_recipe_is_machine_bound_in_grant_and_verdict(tmp_path):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _write(repo, "src/app.py", "fixed\n")
    _git(repo, "commit", "-qam", "fix")
    recipe = full_suite_recipe(FullSuiteKind.NODE_NPM_TEST, ".")
    preview = round_grant.preview_grant_binding(
        repo,
        base="master",
        runtime="codex",
        round_number=1,
        full_suite_kind=recipe.kind,
        full_suite_cwd=recipe.cwd,
    )
    round_grant.record_grant(
        repo,
        base="master",
        runtime="codex",
        round_number=1,
        full_suite_kind=recipe.kind,
        full_suite_cwd=recipe.cwd,
        expected_verdict_sha256=preview["verdict_sha256"],
        expected_binding_sha256=preview["binding_sha256"],
        reason="Bind the repository suite recipe.",
        channel="relayed",
        now=NOW,
    )
    restarted = begin_round(
        repo,
        base="master",
        runtime="codex",
        round_number=1,
        full_suite_kind=recipe.kind,
        full_suite_cwd=recipe.cwd,
        now=NOW,
    )

    assert preview["target"]["validation"]["full_suite_recipe"] == recipe.to_json()
    assert restarted.validation.full_suite_recipe == recipe


def test_begin_cannot_change_the_granted_full_suite_recipe(tmp_path):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _write(repo, "src/app.py", "fixed\n")
    _git(repo, "commit", "-qam", "fix")
    preview = round_grant.preview_grant_binding(
        repo, base="master", runtime="codex", round_number=1,
    )
    round_grant.record_grant(
        repo,
        base="master",
        runtime="codex",
        round_number=1,
        expected_verdict_sha256=preview["verdict_sha256"],
        expected_binding_sha256=preview["binding_sha256"],
        reason="Bind the Python full suite.",
        channel="relayed",
        now=NOW,
    )

    with pytest.raises(SchemaError, match="^RE_REVIEW_GRANT_REQUIRED$"):
        begin_round(
            repo,
            base="master",
            runtime="codex",
            round_number=1,
            full_suite_kind=FullSuiteKind.NODE_NPM_TEST,
            now=NOW,
        )

    restarted = begin_round(
        repo, base="master", runtime="codex", round_number=1, now=NOW,
    )
    assert restarted.validation.full_suite_recipe == full_suite_recipe()
