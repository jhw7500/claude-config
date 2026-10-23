#!/usr/bin/env python3
"""Check a commit message against the title rules and Change Evidence Contract v1.

Python 3.10 standard library only. Shared by the commit-time gate and the CI audit.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Sequence


_DIR = Path(__file__).resolve().parent
_SOURCE_NAMES = (
    "validate_change_evidence.py",
    "commit_title_rules.py",
    "check_commit_message.py",
)


def _load_sibling(module_name: str):
    path = _DIR / f"{module_name}.py"
    loaded = sys.modules.get(module_name)
    if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve() == path:
        return loaded
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_rules = _load_sibling("commit_title_rules")
_validator = _load_sibling("validate_change_evidence")


def source_digests() -> dict[str, str]:
    return {
        name: hashlib.sha256((_DIR / name).read_bytes()).hexdigest()
        for name in _SOURCE_NAMES
    }


def check_message(message: str) -> dict[str, object]:
    sources = source_digests()
    if len(message.encode("utf-8")) > _validator.MAX_BYTES:
        return {
            "auto": False,
            "valid": False,
            "title_findings": [finding.code for finding in _rules.check_title(message)],
            "evidence_findings": ["document-too-large"],
            "rules_version": _rules.RULES_VERSION,
            "sources": sources,
        }
    if _rules.is_auto_message(message):
        return {
            "auto": True,
            "valid": True,
            "title_findings": [],
            "evidence_findings": [],
            "rules_version": _rules.RULES_VERSION,
            "sources": sources,
        }
    title_findings = [finding.code for finding in _rules.check_title(message)]
    result = _validator.validate_text(message, kind="commit", expected_version="v1")
    evidence_findings = [finding.code for finding in result.findings]
    return {
        "auto": False,
        "valid": not title_findings and not evidence_findings,
        "title_findings": title_findings,
        "evidence_findings": evidence_findings,
        "rules_version": _rules.RULES_VERSION,
        "sources": sources,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a commit message file against the title rules and Change Evidence Contract v1."
    )
    parser.add_argument("--path", required=True, help="commit message file (UTF-8, checked byte for byte)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        message = Path(args.path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"check_commit_message: cannot read {args.path}: {error}", file=sys.stderr)
        return 2

    try:
        report = check_message(message)
    except Exception as error:  # noqa: BLE001 - any internal failure is an I/O-class error, not a verdict
        print(f"check_commit_message: internal error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    elif report["auto"]:
        print("SKIP: automatic message")
    elif report["valid"]:
        print("PASS")
    else:
        print("FAIL")
        for code in report["title_findings"]:
            print(f"- {code}")
        for code in report["evidence_findings"]:
            print(f"- {code}")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
