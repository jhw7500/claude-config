#!/usr/bin/env python3
"""Stage a confirmed Markdown document for a claude.ai Artifact and record what publishing it produced.

Only an interactive Claude Code session signed in with /login can publish Artifacts; the tool is off in `claude -p`,
the Agent SDK and MCP-server contexts. So Codex exports the document here, and an interactive session publishes it
and records the result. Entries live outside any repository, under the user's home.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

MAX_BYTES = 256 * 1024
MAX_TITLE = 200
MAX_SLUG_BYTES = 120
ARTIFACT_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9-]+")
# publishing is recorded before the Artifact tool runs, so an entry left in publishing without a URL tells a retry
# that an earlier attempt may have published without recording; it must look for that artifact instead of
# publishing again. A recorded URL is never replaced by another, so a retry after a failure updates that artifact.
TRANSITIONS = {
    "exported": {"publishing", "failed"},
    "publishing": {"published", "failed"},
    "published": {"verified", "failed"},
    "failed": {"publishing", "failed"},
    "verified": set(),
}
REQUIRED = {"title", "document", "sha256", "mode", "target_url", "state", "url", "account", "version", "history"}


class OutboxError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(value: str, name: str, limit: int) -> str:
    value = value.strip()
    if not value or len(value) > limit or "\n" in value or "\r" in value:
        raise OutboxError(f"{name} must be one non-empty line of at most {limit} characters")
    return value


def _url(value: str) -> str:
    if not ARTIFACT_URL.fullmatch(value):
        raise OutboxError("url must be a claude.ai artifact link")
    return value


def _slug(title: str) -> str:
    # A name component is commonly limited to 255 bytes, and the entry name adds a timestamp and a suffix, so the
    # slug is cut by UTF-8 bytes on a character boundary.
    slug = re.sub(r"[^\w-]+", "-", title).strip("-_")
    slug = slug.encode("utf-8")[:MAX_SLUG_BYTES].decode("utf-8", "ignore").strip("-_")
    return slug or "document"


def _write_private(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _save(entry: Path, data: dict) -> None:
    _write_private(entry / "entry.json", (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _load(entry: Path) -> dict:
    try:
        data = json.loads((entry / "entry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise OutboxError(f"cannot read entry: {error}") from None
    if (not isinstance(data, dict) or data.get("schema") != 1 or not REQUIRED <= data.keys()
            or not all(isinstance(data[key], str) for key in ("title", "document", "sha256", "state"))
            or not all(data[key] is None or isinstance(data[key], str)
                       for key in ("target_url", "url", "account", "version"))
            or data["state"] not in TRANSITIONS or data["mode"] not in ("new", "update")
            or not isinstance(data["history"], list)):
        raise OutboxError("entry.json is not an artifact outbox entry")
    # The skill publishes <entry>/<document>, so the name must not reach outside the entry.
    if data["document"] in ("", ".", "..") or Path(data["document"]).name != data["document"]:
        raise OutboxError("entry.json names a document outside the entry")
    return data


def export(outbox: Path, title: str, source: Path, update_url: str | None) -> dict:
    title = _line(title, "title", MAX_TITLE)
    target = _url(update_url) if update_url is not None else None
    try:
        raw = source.read_bytes()
    except OSError as error:
        raise OutboxError(f"cannot read source: {error}") from None
    if not raw or len(raw) > MAX_BYTES:
        raise OutboxError(f"source must be non-empty and at most {MAX_BYTES} bytes")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise OutboxError("source must be UTF-8 text") from None
    outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _slug(title)
    for suffix in range(1, 100):
        entry = outbox / (f"{stamp}-{slug}" if suffix == 1 else f"{stamp}-{slug}-{suffix}")
        try:
            entry.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        raise OutboxError("could not create a unique entry directory")
    document = f"{slug}.md"
    _write_private(entry / document, raw)
    data = {
        "schema": 1, "title": title, "document": document, "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw), "mode": "update" if target else "new", "target_url": target,
        "created_at": _now(), "state": "exported", "url": None, "account": None, "version": None, "history": [],
    }
    _save(entry, data)
    return {
        "entry": str(entry), "document": str(entry / document), "sha256": data["sha256"], "mode": data["mode"],
        "next_step": (
            f"In an interactive Claude Code session signed in with /login, start `claude --add-dir {entry}` "
            "and ask it to publish this entry with the artifact-publish skill."
        ),
    }


def show(entry: Path) -> tuple[dict, bool]:
    data = _load(entry)
    try:
        digest = hashlib.sha256((entry / data["document"]).read_bytes()).hexdigest()
    except OSError:
        digest = None
    return {**data, "entry": str(entry), "digest_ok": digest == data["sha256"]}, digest == data["sha256"]


def record(entry: Path, state: str, url: str | None, account: str | None, version: str | None,
           reason: str | None) -> dict:
    data = _load(entry)
    if state == "publishing" and data["state"] == "publishing":
        raise OutboxError(
            "an earlier publish attempt was not recorded; find that artifact in your artifact list and record it "
            "with --state published --url, or record --state failed before publishing again"
        )
    if state not in TRANSITIONS[data["state"]]:
        raise OutboxError(f"cannot record {state} after {data['state']}")
    if state == "published":
        if url is None:
            raise OutboxError("published needs --url")
        url = _url(url)
        if data["mode"] == "update" and url != data["target_url"]:
            raise OutboxError("an update entry may only publish to its target url")
        if data["url"] is not None and url != data["url"]:
            raise OutboxError("this entry already published to another url; republish to that url instead")
        data["url"] = url
    elif url is not None:
        raise OutboxError("--url is only recorded with published")
    if account is not None:
        data["account"] = _line(account, "account", MAX_TITLE)
    if version is not None:
        data["version"] = _line(version, "version", MAX_TITLE)
    item = {"at": _now(), "state": state, "url": url, "account": data["account"], "version": data["version"]}
    if reason is not None:
        item["reason"] = _line(reason, "reason", 500)
    data["state"] = state
    data["history"].append(item)
    _save(entry, data)
    return {**data, "entry": str(entry)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="artifact-outbox", description="Stage Markdown for a claude.ai Artifact and record publish results."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    staged = commands.add_parser("export", help="stage a confirmed Markdown document")
    staged.add_argument("--outbox", type=Path,
                        default=Path.home() / ".local" / "share" / "claude-config" / "artifact-outbox")
    staged.add_argument("--title", required=True)
    staged.add_argument("--source", type=Path, required=True)
    staged.add_argument("--update-url")
    shown = commands.add_parser("show", help="print an entry and check its document digest")
    shown.add_argument("--entry", type=Path, required=True)
    recorded = commands.add_parser("record", help="record a publish or verification result")
    recorded.add_argument("--entry", type=Path, required=True)
    recorded.add_argument("--state", choices=("publishing", "published", "verified", "failed"), required=True)
    recorded.add_argument("--url")
    recorded.add_argument("--account")
    recorded.add_argument("--version")
    recorded.add_argument("--reason")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result, code = export(args.outbox, args.title, args.source, args.update_url), 0
        elif args.command == "show":
            result, ok = show(args.entry)
            code = 0 if ok else 3
        else:
            result, code = record(args.entry, args.state, args.url, args.account, args.version, args.reason), 0
    except (OutboxError, OSError) as error:
        print(f"artifact-outbox: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
