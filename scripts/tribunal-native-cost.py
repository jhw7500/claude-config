#!/usr/bin/python3
"""Read-only, unbound Codex session cost observations for one tribunal run.

The caller supplies both the tribunal telemetry file and each reviewer session.
Session-to-run association is not authenticated and these numbers are never gate
evidence. No repository discovery, lock acquisition, or artifact writes occur.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from pre_pr_tribunal import telemetry  # noqa: E402
from pre_pr_tribunal.model import SchemaError  # noqa: E402


MAX_SESSION_BYTES = 32 * 1024 * 1024
MAX_SESSION_LINES = 100_000
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
TOKEN_KEYS = (
    "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens",
)


class CostError(Exception):
    pass


def read_file(path: Path, maximum: int, *, exact_mode: int | None = None) -> bytes:
    if not path.is_absolute():
        raise CostError("COST_PATH_INVALID")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise CostError("COST_FILE_UNAVAILABLE") from None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & stat.S_IWOTH
                or (exact_mode is not None and stat.S_IMODE(before.st_mode) != exact_mode)):
            raise CostError("COST_FILE_UNSAFE")
        if before.st_size > maximum:
            raise CostError("COST_FILE_TOO_LARGE")
        chunks = []
        size = 0
        while True:
            chunk = os.read(fd, min(65536, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise CostError("COST_FILE_TOO_LARGE")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise CostError("COST_FILE_CHANGED")
        return b"".join(chunks)
    except OSError:
        raise CostError("COST_FILE_UNAVAILABLE") from None
    finally:
        os.close(fd)


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise CostError("COST_SESSION_INVALID")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CostError("COST_SESSION_INVALID") from None
    if result.tzinfo is None:
        raise CostError("COST_SESSION_INVALID")
    return result.astimezone(timezone.utc)


def microseconds(delta) -> int:
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def summarize_session(raw: bytes) -> dict[str, object]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        raise CostError("COST_SESSION_INVALID") from None
    if not lines or len(lines) > MAX_SESSION_LINES:
        raise CostError("COST_SESSION_INVALID")
    session_meta_count = 0
    calls = {}
    outputs = {}
    tokens = None
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            raise CostError("COST_SESSION_INVALID") from None
        if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
            raise CostError("COST_SESSION_INVALID")
        kind = item.get("type")
        payload = item["payload"]
        if kind == "session_meta":
            session_meta_count += 1
        elif kind == "token_usage_record":
            usage = payload.get("thread_token_usage")
            if not isinstance(usage, dict) or any(
                type(usage.get(key)) is not int or usage[key] < 0 for key in TOKEN_KEYS
            ):
                raise CostError("COST_SESSION_INVALID")
            if tokens is not None and any(usage[key] < tokens[key] for key in TOKEN_KEYS):
                raise CostError("COST_SESSION_INVALID")
            tokens = {key: usage[key] for key in TOKEN_KEYS}
        elif kind == "response_item" and payload.get("type") in (
            "custom_tool_call", "custom_tool_call_output"
        ):
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
                raise CostError("COST_SESSION_INVALID")
            target = calls if payload["type"] == "custom_tool_call" else outputs
            if call_id in target:
                raise CostError("COST_SESSION_INVALID")
            target[call_id] = timestamp(item.get("timestamp"))
    if session_meta_count != 1 or set(outputs) - set(calls):
        raise CostError("COST_SESSION_INVALID")
    complete = set(calls) == set(outputs)
    intervals = []
    for call_id, start in calls.items():
        if call_id not in outputs:
            continue
        end = outputs[call_id]
        if end < start:
            raise CostError("COST_SESSION_INVALID")
        intervals.append((start, end))
    wait_sum = None
    wait_union = None
    if complete:
        wait_sum = sum(microseconds(end - start) for start, end in intervals) // 1000
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        wait_union = sum(microseconds(end - start) for start, end in merged) // 1000
    return {
        "native_tool_call_count_observed": len(calls),
        "paired_tool_call_count": len(intervals),
        "pairing_complete": complete,
        "tool_wait_sum_ms": wait_sum,
        "tool_wait_union_ms": wait_union,
        "tokens_cumulative_last": tokens,
    }


def summarize(telemetry_path: Path, run_id: str, transcripts: list[str]) -> dict[str, object]:
    if RUN_ID.fullmatch(run_id) is None or not transcripts:
        raise CostError("COST_ARGUMENT_INVALID")
    try:
        ledger = telemetry._parse_ledger(
            read_file(telemetry_path, telemetry.MAX_TELEMETRY_BYTES, exact_mode=0o600)
        )
    except SchemaError:
        raise CostError("COST_TELEMETRY_INVALID") from None
    run = next((item for item in ledger.runs if item.run_id == run_id), None)
    if run is None:
        raise CostError("COST_RUN_NOT_FOUND")
    reviewers = {}
    for item in transcripts:
        role, separator, name = item.partition("=")
        if not separator or role not in ("A", "B", "C") or role in reviewers:
            raise CostError("COST_ARGUMENT_INVALID")
        reviewers[role] = summarize_session(read_file(Path(name), MAX_SESSION_BYTES))
    reviewer_totals = {}
    for role in reviewers:
        total_spans = [
            span for span in run.spans
            if span.reviewer is not None and span.reviewer.value == role
            and span.stage is telemetry.TelemetryStage.REVIEWER_TOTAL
        ]
        if len(total_spans) > 1:
            raise CostError("COST_REVIEWER_ATTEMPT_AMBIGUOUS")
        reviewer_totals[role] = total_spans[0].duration_ms if total_spans else None
    return {
        "schema": 1,
        "provenance": "user_supplied_transcripts_unbound",
        "transcript_scope": "entire_supplied_session",
        "gate_evidence": False,
        "run_id": run_id,
        "round": run.round,
        "telemetry_outcome": run.outcome.value if run.outcome else "running",
        "binding": {
            "head_sha": run.binding.head_sha,
            "diff_sha256": run.binding.diff_sha256,
            "contract": dict(run.binding.contract),
        },
        "invocation_elapsed_ms": telemetry._invocation_elapsed(run),
        "reviewers": {
            role: {"reviewer_total_ms": reviewer_totals[role], **result}
            for role, result in reviewers.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--transcript", action="append", required=True, metavar="ROLE=ABS_PATH")
    args = parser.parse_args()
    try:
        result = summarize(args.telemetry, args.run_id, args.transcript)
    except CostError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
