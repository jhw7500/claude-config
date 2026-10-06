"""Fast authenticated suite receipts for tests unrelated to suite execution."""

from unittest.mock import patch

from pre_pr_tribunal import evidence_runtime, evidence_store
from pre_pr_tribunal.model import FullSuiteKind
from pre_pr_tribunal.verdict_store import read_verdict, seal_final_validation


def seal_synthetic_python_suite(repo):
    verdict = read_verdict(repo)
    recipe = verdict.validation.full_suite_recipe
    if recipe.kind is not FullSuiteKind.PYTHON_PYTEST:
        raise AssertionError("synthetic helper supports only the Python recipe")
    captured = evidence_runtime.capture_evidence(
        repo, base=verdict.base_ref, profile=recipe.profile,
        command_cwd=recipe.cwd,
        argv=["python3", "-I", "-S", "-c", "pass"],
        timeout_seconds=15,
    )
    assert captured["exit_code"] == 0
    receipt = evidence_store.read_receipt(repo, captured["receipt_sha256"])
    receipt["entry"] = {
        **receipt["entry"],
        "argv": list(recipe.argv),
        "cwd": recipe.cwd,
        "captured_at": "2026-09-09T00:00:00.000000Z",
        "duration_ms": 0,
    }
    digest = evidence_store.put_receipt(repo, receipt)
    with patch.object(
        evidence_runtime, "capture_evidence",
        return_value={"exit_code": 0, "receipt_sha256": digest},
    ):
        return seal_final_validation(repo, timeout_seconds=15)
