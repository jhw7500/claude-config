"""Command line for recording and revoking one-shot commit approvals.

Approvals are friction against accidental bypass, not cryptographic proof of
human presence.  See ``grant.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

try:  # package import (hooks on sys.path) and direct script run
    from . import grant
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from commit_gate import grant

_MAX_MESSAGE_BYTES = 1024 * 1024


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="commit_gate",
        description=(
            "Record or revoke a one-shot approval for one rule-violating commit. "
            "An approval is friction against accidental bypass, not "
            "cryptographic proof of human presence."
        ),
    )
    parser.add_argument("--home", type=Path, default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("grant", help="approve exactly one commit (valid 10 minutes)")
    g.add_argument("--runtime", choices=sorted(grant._RUNTIMES), required=True)
    g.add_argument("--repo", type=Path, required=True)
    source = g.add_mutually_exclusive_group(required=True)
    source.add_argument("--message-file", type=Path)
    source.add_argument("--message-sha256")
    g.add_argument("--reason", required=True, help="the user's answer")
    g.add_argument("--relayed", action="store_true",
                   help="the agent relays the user's answer; skip the terminal prompt")
    r = sub.add_parser("revoke", help="delete every approval for a repository")
    r.add_argument("--repo", type=Path, required=True)
    return parser


def _repo_root(path: Path) -> Optional[Path]:
    top = _git(path, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def _grant(args: argparse.Namespace, home: Path) -> int:
    root = _repo_root(args.repo)
    if root is None:
        print("error: --repo is not a git repository", file=sys.stderr)
        return 2
    if args.message_file is not None:
        try:
            with open(args.message_file, "rb") as handle:
                data = handle.read(_MAX_MESSAGE_BYTES + 1)
        except OSError as error:
            print(f"error: cannot read message file: {error}", file=sys.stderr)
            return 2
        if len(data) > _MAX_MESSAGE_BYTES:
            print("error: message file is too large", file=sys.stderr)
            return 2
        digest = hashlib.sha256(data).hexdigest()
    else:
        digest = args.message_sha256
        if not grant._is_hex(digest, (64,)):
            print("error: --message-sha256 must be 64 lowercase hex characters",
                  file=sys.stderr)
            return 2
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    binding = grant.make_binding(
        repo_root=root, head=head, message_sha256=digest, runtime=args.runtime
    )
    if not args.relayed:
        if not sys.stdin.isatty():
            print("error: no terminal; use --relayed only when the user answered "
                  "in the agent prompt", file=sys.stderr)
            return 2
        print(f"repo {binding['repo_root']}\nhead {binding['head']}\n"
              f"message sha256 {digest}")
        if input("type 'commit' to approve one commit: ").strip() != "commit":
            print("not approved", file=sys.stderr)
            return 1
    try:
        path = grant.record_grant(
            home, binding=binding, reason=args.reason,
            channel="relayed" if args.relayed else "tty", now=time.time(),
        )
    except (OSError, ValueError) as error:
        print(f"error: cannot record approval: {error}", file=sys.stderr)
        return 2
    print(f"approved one commit for {args.runtime} (valid {grant.GRANT_TTL_SECONDS}s)\n"
          f"repo {binding['repo_root']}\nhead {binding['head']}\n"
          f"message sha256 {digest}\ngrant {path.name}")
    return 0


def _revoke(args: argparse.Namespace, home: Path) -> int:
    root = _repo_root(args.repo) or args.repo
    print(f"revoked {grant.revoke_repo(home, repo_root=root)} approval(s)")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    home = args.home if args.home is not None else Path.home()
    return _grant(args, home) if args.command == "grant" else _revoke(args, home)


if __name__ == "__main__":
    sys.exit(main())
