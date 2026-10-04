"""Exact-snapshot final-validation receipt authentication."""

from __future__ import annotations

from pathlib import Path
import shlex

from . import evidence, evidence_runtime, evidence_store, model
from .evidence_environment import validate_command


def is_full_suite_execution(
    recipe: model.FullSuiteRecipe, command: str
) -> bool:
    """Recognize the lifecycle-bound suite command in reviewer evidence."""
    return command in {
        recipe.command,
        f"cd -- {shlex.quote(recipe.cwd)} && {recipe.command}",
    }


def verify_full_suite_receipt(
    root: Path, verdict: model.Verdict, receipt_sha256: str
) -> dict[str, object]:
    """Authenticate one successful fresh receipt without granting generic reuse."""
    if (
        not isinstance(receipt_sha256, str)
        or model._SHA256.fullmatch(receipt_sha256) is None
        or verdict.contract is None
    ):
        raise model.SchemaError("FINAL_VALIDATION_RECEIPT_INVALID")
    try:
        receipt = evidence_store.read_receipt(root, receipt_sha256)
        binding = receipt["binding"]
        if binding["snapshot"] != evidence_runtime.snapshot_binding(verdict.snapshot):
            raise model.SchemaError("FINAL_VALIDATION_SNAPSHOT_MISMATCH")
        expected_contract = {
            **verdict.contract.to_json(),
            "evidence": evidence_runtime.EVIDENCE_CONTRACT_VERSION,
        }
        if binding["contract"] != expected_contract:
            raise model.SchemaError("FINAL_VALIDATION_CONTRACT_MISMATCH")
        entry = receipt["entry"]
        validation = verdict.validation
        recipe = (
            validation.full_suite_recipe
            if validation is not None
            else None
        )
        if recipe is None:
            raise model.SchemaError("FINAL_VALIDATION_RECIPE_MISMATCH")
        environment = binding["environment"]
        if (
            environment["profile"] != recipe.profile
            or environment["cwd"] != recipe.cwd
            or entry["cwd"] != recipe.cwd
            or tuple(entry["argv"]) != recipe.argv
        ):
            raise model.SchemaError("FINAL_VALIDATION_RECIPE_MISMATCH")
        evidence.validate_entry_capture(
            entry, evidence_store.read_capture(root, entry["capture_sha256"])
        )
        evidence_runtime.verify_source_tool_environment(
            root, expected_environment=environment
        )
        if entry["freshness"] != validate_command(
            root,
            environment["profile"],
            entry["cwd"],
            entry["argv"],
        ):
            raise model.SchemaError("FINAL_VALIDATION_RECEIPT_INVALID")
        if entry["exit_code"] != 0:
            raise model.SchemaError("FINAL_VALIDATION_FAILED")
        return entry
    except model.SchemaError as error:
        if error.code.startswith("FINAL_VALIDATION_"):
            raise
        raise model.SchemaError("FINAL_VALIDATION_RECEIPT_INVALID") from None
    except (OSError, ValueError, TypeError, UnicodeError):
        raise model.SchemaError("FINAL_VALIDATION_RECEIPT_INVALID") from None
