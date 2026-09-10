from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from pre_pr_tribunal.model import (
    Decision,
    REPORT_TEXT_CONTRACT_VERSION,
    ReportReceipt,
    Reviewer,
    ReviewerSlot,
    RoundSummary,
    SchemaError,
    parse_reviewer_report,
)
from pre_pr_tribunal.review_context import (
    context_sha256,
    current_contract_binding,
    reviewer_context_body,
    reviewer_context_envelope,
)
from pre_pr_tribunal.verdict_store import _new_v2_pending, begin_round, read_verdict


def NOW():
    return "2026-09-01T00:00:00Z"


def _v2_pending(git_repo):
    legacy = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    return _new_v2_pending(
        legacy.snapshot,
        runtime="codex",
        initial_paths=legacy.initial_paths,
        round_number=1,
        decisions=(),
        history=(),
        contract=current_contract_binding(),
    )


def _sealed_slot(verdict, reviewer):
    value = {
        "schema": 1,
        "reviewer": reviewer.value,
        "round": verdict.round,
        "snapshot": {
            "head_sha": verdict.head_sha,
            "diff_sha256": verdict.diff_sha256,
        },
        "status": "complete",
        "findings": [],
        "executions": [],
        "claims": [],
        "prior_decisions": [],
    }
    raw = json.dumps(value, separators=(",", ":")).encode()
    parsed = parse_reviewer_report(
        raw,
        expected_reviewer=reviewer,
        expected_round=verdict.round,
        snapshot=verdict.snapshot,
    )
    return ReviewerSlot(
        "sealed",
        report=parsed,
        receipt=ReportReceipt(
            reviewer=reviewer,
            round=verdict.round,
            path=f".review/inbox/round-{verdict.round}/{reviewer.value}.json",
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            context_sha256="2" * 64,
            report_contract_version=REPORT_TEXT_CONTRACT_VERSION,
            attempt=1,
            provenance="native_submit",
        ),
        attempt_count=1,
    )


def _with_mixed_prior_reviewer_data(verdict):
    findings = tuple(
        {
            "id": f"{reviewer}-R1-001",
            "reviewer": reviewer,
            "severity": "HIGH",
        }
        for reviewer in "ABC"
    )
    decisions = tuple(
        Decision(
            id=f"D-R1-{reviewer}-001",
            finding_round=1,
            finding_id=f"{reviewer}-R1-001",
            reviewer=Reviewer(reviewer),
            disposition="fixed",
            rationale=f"{reviewer} remediation is covered.",
            executions=(),
        )
        for reviewer in "ABC"
    )
    return replace(
        verdict,
        history=(
            RoundSummary(
                round=1,
                head_sha=verdict.head_sha,
                diff_sha256=verdict.diff_sha256,
                blocking_findings=findings,
                decision_outcomes=(),
            ),
        ),
        decisions=decisions,
    )


def test_context_digest_is_over_canonical_body_and_is_stable(git_repo):
    pending_verdict = _v2_pending(git_repo)
    body = reviewer_context_body(pending_verdict, Reviewer.A)
    canonical = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    envelope = reviewer_context_envelope(pending_verdict, Reviewer.A)
    assert envelope == {
        **body,
        "context_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    assert context_sha256(pending_verdict, Reviewer.A) == envelope["context_sha256"]


def test_sealing_peer_does_not_change_pending_reviewer_context(git_repo):
    pending_verdict = _with_mixed_prior_reviewer_data(_v2_pending(git_repo))
    before = context_sha256(pending_verdict, Reviewer.C)
    changed = replace(
        pending_verdict,
        reviewers={
            **pending_verdict.reviewers,
            "A": _sealed_slot(pending_verdict, Reviewer.A),
        },
    )
    assert context_sha256(changed, Reviewer.C) == before
    body = reviewer_context_body(changed, Reviewer.C)
    assert "reviewers" not in body
    assert body["own_prior_findings"] == [
        {"id": "C-R1-001", "reviewer": "C", "severity": "HIGH"}
    ]
    assert body["own_decisions"] == [
        {
            "id": "D-R1-C-001",
            "finding_ref": {"round": 1, "id": "C-R1-001", "reviewer": "C"},
            "disposition": "fixed",
            "rationale": "C remediation is covered.",
        }
    ]


def test_context_rejects_sealed_reviewer_slot(git_repo):
    pending_verdict = _v2_pending(git_repo)
    sealed = replace(
        pending_verdict,
        reviewers={
            **pending_verdict.reviewers,
            "A": _sealed_slot(pending_verdict, Reviewer.A),
        },
    )

    with pytest.raises(SchemaError, match="^REVIEWER_SLOT_SEALED$"):
        reviewer_context_envelope(sealed, Reviewer.A)


def test_context_rejects_installed_contract_drift(git_repo):
    pending_verdict = _v2_pending(git_repo)
    drifted = replace(
        pending_verdict,
        contract=replace(pending_verdict.contract, report_text=999),
    )
    with pytest.raises(SchemaError, match="^CONTRACT_DRIFT$"):
        reviewer_context_envelope(drifted, Reviewer.A)


def test_persisted_contract_drift_reaches_context_runtime_check(git_repo):
    pending_verdict = _v2_pending(git_repo)
    payload = pending_verdict.to_json()
    payload["contract"]["report_text"] = 999
    verdict_path = git_repo / ".review/verdict.json"
    verdict_path.write_text(json.dumps(payload), encoding="utf-8")
    verdict_path.chmod(0o600)

    loaded = read_verdict(git_repo)
    assert loaded.contract.report_text == 999
    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    context = subprocess.run(
        [sys.executable, str(cli), "context", "--reviewer", "A"],
        cwd=git_repo,
        text=True,
        capture_output=True,
    )

    assert context.returncode == 1
    assert context.stdout == ""
    assert context.stderr == "PRE_PR_TRIBUNAL:CONTRACT_DRIFT\n"
