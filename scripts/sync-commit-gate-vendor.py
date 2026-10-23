#!/usr/bin/env python3
"""Sync or verify the vendored commit-rule copies for the commit gate.

--automation/--commit : write the three files from `git show <commit>:scripts/<name>`
                        and refresh SOURCE.json.
--check               : exit 1 if a vendored file differs from SOURCE.json digests.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

NAMES = ("validate_change_evidence.py", "commit_title_rules.py", "check_commit_message.py")
RULES_VERSION = "1"
DEFAULT_VENDOR = Path(__file__).resolve().parents[1] / "hooks" / "commit_gate" / "vendor"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def sync(automation, commit, vendor):
    contents = {}
    for name in NAMES:
        proc = subprocess.run(
            ["git", "-C", str(automation), "show", f"{commit}:scripts/{name}"],
            capture_output=True,
        )
        if proc.returncode != 0:
            print(f"git show failed for {name}: {proc.stderr.decode(errors='replace').strip()}", file=sys.stderr)
            return 2
        contents[name] = proc.stdout
    vendor.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        (vendor / name).write_bytes(data)
    source = {
        "automation_commit": commit,
        "rules_version": RULES_VERSION,
        "files": {name: sha256(contents[name]) for name in NAMES},
    }
    (vendor / "SOURCE.json").write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
    print(f"synced {len(NAMES)} files at {commit}")
    return 0


def check(vendor):
    try:
        source = json.loads((vendor / "SOURCE.json").read_text(encoding="utf-8"))
        expected = source["files"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"cannot read SOURCE.json: {exc}", file=sys.stderr)
        return 2
    drift = []
    for name in NAMES:
        try:
            actual = sha256((vendor / name).read_bytes())
        except OSError:
            actual = None
        if actual != expected.get(name):
            drift.append(name)
    if drift:
        print("drift: " + ", ".join(drift), file=sys.stderr)
        return 1
    print("vendor copies match SOURCE.json")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--automation", type=Path, help="automation checkout")
    parser.add_argument("--commit", help="40-hex automation commit")
    parser.add_argument("--check", action="store_true", help="verify digests only")
    parser.add_argument("--vendor-dir", type=Path, default=DEFAULT_VENDOR)
    args = parser.parse_args(argv)
    if args.check:
        return check(args.vendor_dir)
    if not args.automation or not args.commit or not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("--automation and a 40-hex --commit are required (or use --check)")
    return sync(args.automation, args.commit, args.vendor_dir)


if __name__ == "__main__":
    sys.exit(main())
