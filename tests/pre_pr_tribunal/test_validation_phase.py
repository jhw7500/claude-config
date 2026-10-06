import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import venv
from concurrent.futures import ThreadPoolExecutor

import pytest

from pre_pr_tribunal import (
    evidence_runtime,
    evidence_store,
    round_grant,
    telemetry,
    validation,
)
from pre_pr_tribunal import cli
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
    final_validation_reservation_status,
    recover_final_validation_reservation,
    seal_final_validation,
    submit_reviewer_report,
    validate_stored_reviewer_report,
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


def _relabeled_no_op_receipt(repo):
    recipe = full_suite_recipe()
    source_sha256 = _no_op_receipt(repo)
    receipt = evidence_store.read_receipt(repo, source_sha256)
    receipt["entry"] = {
        **receipt["entry"],
        "argv": list(recipe.argv),
        "cwd": recipe.cwd,
    }
    return evidence_store.put_receipt(repo, receipt)


def _stub_owned_full_suite(monkeypatch, repo):
    receipt_sha256 = _relabeled_no_op_receipt(repo)
    calls = []

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return {"exit_code": 0, "receipt_sha256": receipt_sha256}

    monkeypatch.setattr(evidence_runtime, "capture_evidence", capture)
    return receipt_sha256, calls


def _use_isolated_pytest_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "pytest-runtime"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(runtime)
    site_packages = (
        runtime / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    (site_packages / "controller-pytest.pth").write_text(
        str(Path(pytest.__file__).resolve().parent.parent) + "\n",
        encoding="utf-8",
    )
    shim_bin = tmp_path / "python-shim"
    shim_bin.mkdir()
    shim = shim_bin / "python3"
    shim.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(str(runtime / "bin" / "python3"))
        + ' "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)
    monkeypatch.setenv(
        "PATH", f"{shim_bin}:{os.environ.get('PATH', '')}",
    )


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


def test_followup_pass_runs_owned_bound_recipe_and_finalizes(
    tmp_path, monkeypatch,
):
    _use_isolated_pytest_runtime(tmp_path, monkeypatch)
    repo = _repo(tmp_path)
    _write(repo, ".gitignore", ".review/\n__pycache__/\n.pytest_cache/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore test caches")
    _failed_first_round(repo)
    _preview, restarted = _restart_after_fix(repo)
    _seal_direct_impact_reports(repo, restarted)

    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_REQUIRED$"):
        finalize_round(repo, now=NOW)

    sealed = seal_final_validation(repo, timeout_seconds=30)
    receipt_sha256 = sealed.validation.full_suite_receipt_sha256
    assert receipt_sha256 is not None
    receipt = evidence_store.read_receipt(repo, receipt_sha256)
    assert receipt["entry"]["argv"] == list(full_suite_recipe().argv)
    assert receipt["entry"]["cwd"] == full_suite_recipe().cwd
    assert receipt["entry"]["exit_code"] == 0
    assert sealed.validation.to_json() == {
        "phase": "final_validation",
        "requires_full_suite": True,
        "full_suite_receipt_sha256": receipt_sha256,
        "escalation_reason": None,
        "full_suite_recipe": full_suite_recipe().to_json(),
    }
    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_ALREADY_SEALED$"):
        seal_final_validation(repo, timeout_seconds=30)

    final = finalize_round(repo, now=NOW)
    assert final.gate.status is GateStatus.PASS
    assert _status(final)["validation"] == sealed.validation.to_json()


def test_concurrent_final_validation_admits_only_one_capture(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _preview, restarted = _restart_after_fix(repo)
    _seal_direct_impact_reports(repo, restarted)
    receipt_sha256 = _relabeled_no_op_receipt(repo)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def capture(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return {"exit_code": 0, "receipt_sha256": receipt_sha256}

    monkeypatch.setattr(evidence_runtime, "capture_evidence", capture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(seal_final_validation, repo, timeout_seconds=15)
        assert entered.wait(5)
        try:
            reservation = final_validation_reservation_status(repo)
            with pytest.raises(SchemaError, match="^FINAL_VALIDATION_IN_PROGRESS$"):
                recover_final_validation_reservation(
                    repo, expected_sha256=reservation["sha256"],
                    confirmed_terminal=True,
                )
            with pytest.raises(SchemaError, match="^FINAL_VALIDATION_RESERVED$"):
                seal_final_validation(repo, timeout_seconds=15)
            competing_cli = subprocess.run(
                [
                    sys.executable, "-B", str(Path(cli.__file__).resolve()),
                    "final-validation-seal", "--timeout", "15",
                ],
                cwd=repo, capture_output=True, text=True, timeout=5,
                check=False,
            )
            assert competing_cli.returncode == 1
            assert competing_cli.stderr == "PRE_PR_TRIBUNAL:FINAL_VALIDATION_RESERVED\n"
            assert competing_cli.stdout == ""
        finally:
            release.set()
        assert first.result().validation.phase is ValidationPhase.FINAL_VALIDATION
    assert len(calls) == 1


def test_interrupted_final_validation_requires_explicit_recovery(
    tmp_path, monkeypatch, capsys,
):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _preview, restarted = _restart_after_fix(repo)
    _seal_direct_impact_reports(repo, restarted)
    receipt_sha256 = _relabeled_no_op_receipt(repo)
    with pytest.raises(SchemaError, match="^EVIDENCE_TIMEOUT_INVALID$"):
        seal_final_validation(repo, timeout_seconds=0)
    assert not (repo / ".review/final-validation-reservation.json").exists()
    monkeypatch.setattr(
        evidence_runtime, "capture_evidence", lambda *args, **kwargs: {"exit_code": 1},
    )

    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_FAILED$"):
        seal_final_validation(repo, timeout_seconds=15)
    monkeypatch.chdir(repo)
    assert cli.main(["final-validation-reservation-status"]) == 0
    reservation = json.loads(capsys.readouterr().out)
    assert reservation["status"] == "reserved"
    marker = repo / ".review/final-validation-reservation.json"
    assert marker.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_RESERVED$"):
        seal_final_validation(repo, timeout_seconds=15)
    marker.chmod(0o644)
    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_RESERVATION_UNSAFE$"):
        recover_final_validation_reservation(
            repo, expected_sha256=reservation["sha256"], confirmed_terminal=True,
        )
    marker.chmod(0o600)
    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_RESERVATION_MISMATCH$"):
        recover_final_validation_reservation(
            repo, expected_sha256="0" * 64, confirmed_terminal=True,
        )
    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_RECOVERY_CONFIRMATION_REQUIRED$"):
        recover_final_validation_reservation(repo, expected_sha256=reservation["sha256"])

    assert cli.main([
        "final-validation-recover", "--reservation-sha256", reservation["sha256"],
        "--confirm-process-tree-stopped",
    ]) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["status"] == "recovered"
    assert not (repo / ".review/final-validation-reservation.json").exists()
    assert (repo / recovered["archive_path"]).stat().st_mode & 0o777 == 0o600
    calls = []

    def capture(*args, **kwargs):
        calls.append(1)
        return {"exit_code": 0, "receipt_sha256": receipt_sha256}

    monkeypatch.setattr(evidence_runtime, "capture_evidence", capture)
    assert seal_final_validation(repo, timeout_seconds=15).validation.full_suite_receipt_sha256 == receipt_sha256
    assert len(calls) == 1


def test_terminal_round_cannot_carry_an_interrupted_reservation(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _preview, restarted = _restart_after_fix(repo)
    _seal_direct_impact_reports(repo, restarted, blocker=True)
    monkeypatch.setattr(
        evidence_runtime, "capture_evidence", lambda *args, **kwargs: {"exit_code": 1},
    )

    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_FAILED$"):
        seal_final_validation(
            repo, timeout_seconds=15,
            escalation_reason="Cross-domain build configuration changed.",
        )
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.FAIL
    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_RECOVERY_REQUIRED$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)

    reservation = final_validation_reservation_status(repo)
    assert recover_final_validation_reservation(
        repo, expected_sha256=reservation["sha256"], confirmed_terminal=True,
    )["status"] == "recovered"


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
    with pytest.raises(SchemaError, match="^FINAL_VALIDATION_NOT_READY$"):
        seal_final_validation(repo, timeout_seconds=15)
    receipt_sha256, calls = _stub_owned_full_suite(monkeypatch, repo)
    seal_final_validation(
        repo,
        timeout_seconds=15,
        escalation_reason="Cross-domain build configuration changed.",
    )
    assert len(calls) == 1

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


def test_three_round_contract_runs_full_suite_only_for_final_candidate(
    tmp_path, monkeypatch,
):
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
            _receipt_sha256, calls = _stub_owned_full_suite(monkeypatch, repo)
            full_suite_executions += 1
            seal_final_validation(repo, timeout_seconds=15)
            assert len(calls) == 1
            assert finalize_round(repo, now=NOW).gate.status is GateStatus.PASS
        previous = current

    assert previous.round == 3
    assert full_suite_executions == 1


@pytest.mark.parametrize("receipt_factory", (_no_op_receipt, _targeted_receipt))
def test_caller_cannot_supply_any_receipt_to_final_validation(
    tmp_path, receipt_factory,
):
    repo = _repo(tmp_path)
    _failed_first_round(repo)
    _preview, restarted = _restart_after_fix(repo)
    _seal_direct_impact_reports(repo, restarted)

    receipt_sha256 = receipt_factory(repo)
    with pytest.raises(TypeError, match="receipt_sha256"):
        seal_final_validation(
            repo, timeout_seconds=15, receipt_sha256=receipt_sha256,
        )


def test_cli_rejects_external_final_validation_receipt():
    with pytest.raises(SystemExit):
        cli._parser().parse_args([
            "final-validation-seal", "--receipt", "0" * 64,
        ])
    parsed = cli._parser().parse_args([
        "final-validation-seal", "--timeout", "15",
    ])
    assert parsed.timeout == 15
    assert cli._parser().parse_args([
        "final-validation-reservation-status",
    ]).command == "final-validation-reservation-status"
    recovered = cli._parser().parse_args([
        "final-validation-recover", "--reservation-sha256", "a" * 64,
        "--confirm-process-tree-stopped",
    ])
    assert recovered.confirm_process_tree_stopped is True


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


@pytest.mark.parametrize(
    ("kind", "cwd"),
    (
        (FullSuiteKind.PYTHON_PYTEST, "python-suite"),
        (FullSuiteKind.NODE_NPM_TEST, "node-suite"),
    ),
)
def test_bound_recipe_context_survives_seal_stored_validation_and_finalize(
    tmp_path, monkeypatch, kind, cwd,
):
    repo = _repo(tmp_path)
    _write(repo, f"{cwd}/marker.txt", "suite root\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add suite root")
    _failed_first_round(repo)
    _write(repo, "src/app.py", "fixed\n")
    _git(repo, "commit", "-qam", "fix")
    recipe = full_suite_recipe(kind, cwd)
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
        reason="Bind the non-default suite recipe.",
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
    _seal_direct_impact_reports(repo, restarted)
    calls = []

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return {"exit_code": 0, "receipt_sha256": "f" * 64}

    monkeypatch.setattr(evidence_runtime, "capture_evidence", capture)
    monkeypatch.setattr(
        validation, "verify_full_suite_receipt", lambda *args, **kwargs: {},
    )
    sealed = seal_final_validation(repo, timeout_seconds=15)

    assert sealed.validation.full_suite_recipe == recipe
    assert len(calls) == 1
    assert calls[0][1] == {
        "base": "master",
        "profile": recipe.profile,
        "command_cwd": recipe.cwd,
        "argv": list(recipe.argv),
        "timeout_seconds": 15,
    }
    for reviewer in restarted.policy.active_reviewers:
        validate_stored_reviewer_report(repo, reviewer=Reviewer(reviewer))
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.PASS


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
