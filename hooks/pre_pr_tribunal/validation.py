"""Exact-snapshot final-validation receipt authentication."""

from __future__ import annotations

from pathlib import Path
import shlex

from . import evidence, evidence_runtime, evidence_store, model
from .evidence_environment import validate_command


def is_full_suite_execution(
    recipe: model.FullSuiteRecipe, command: str
) -> bool:
    """Recognize whole-suite entrypoints without rejecting file-scoped tests."""
    if command in {
        recipe.command,
        f"cd -- {shlex.quote(recipe.cwd)} && {recipe.command}",
    }:
        return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if tokens[:2] == ["cd", recipe.cwd] and tokens[2:3] == ["&&"]:
        tokens = tokens[3:]
    elif tokens[:3] == ["cd", "--", recipe.cwd] and tokens[3:4] == ["&&"]:
        tokens = tokens[4:]
    if recipe.kind is model.FullSuiteKind.PYTHON_PYTEST:
        if tokens[:3] in (["python3", "-m", "pytest"], ["python", "-m", "pytest"]):
            tokens = tokens[3:]
        elif tokens[:1] == ["pytest"]:
            tokens = tokens[1:]
        else:
            return False
        value_options = {
            "--maxfail", "--tb", "--color", "--capture", "--durations",
            "--junitxml", "--rootdir", "-n",
        }
        narrowing_options = {"-k", "-m", "--keyword", "--markexpr"}
        selectors = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in narrowing_options or token.startswith(("-k", "-m")) or any(
                token.startswith(f"{option}=") for option in narrowing_options
            ):
                return False
            if token in value_options:
                index += 2
                continue
            if token == "--":
                selectors.extend(tokens[index + 1:])
                break
            if not token.startswith("-"):
                selectors.append(token)
            index += 1
        return all(
            selector in {".", "./", "tests", "tests/", "./tests", "./tests/"}
            for selector in selectors
        )
    if tokens[:2] in (["npm", "test"], ["npm", "t"]):
        tail = tokens[2:]
    elif tokens[:3] == ["npm", "run", "test"]:
        tail = tokens[3:]
    else:
        return False
    narrowing_options = {
        "--runTestsByPath", "--testPathPattern", "--testNamePattern",
        "--grep", "--filter", "-t",
    }
    value_options = {"--maxWorkers", "--timeout"}
    index = 0
    while index < len(tail):
        token = tail[index]
        if token in narrowing_options or token.startswith("-t") or any(
            token.startswith(f"{option}=") for option in narrowing_options
        ):
            return False
        if token in value_options:
            index += 2
            continue
        if token != "--" and not token.startswith("-"):
            return False
        index += 1
    return True


def verify_full_suite_receipt(
    root: Path, verdict: model.Verdict, receipt_sha256: str,
    *, expected_success: bool = True,
) -> dict[str, object]:
    """Authenticate a fresh receipt with the expected exit outcome."""
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
        if expected_success and entry["exit_code"] != 0:
            raise model.SchemaError("FINAL_VALIDATION_FAILED")
        if not expected_success and entry["exit_code"] == 0:
            raise model.SchemaError("FINAL_VALIDATION_RECEIPT_INVALID")
        return entry
    except model.SchemaError as error:
        if error.code.startswith("FINAL_VALIDATION_"):
            raise
        raise model.SchemaError("FINAL_VALIDATION_RECEIPT_INVALID") from None
    except (OSError, ValueError, TypeError, UnicodeError):
        raise model.SchemaError("FINAL_VALIDATION_RECEIPT_INVALID") from None
