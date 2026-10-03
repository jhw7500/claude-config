import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/tribunal-native-cost.py"
SPEC = importlib.util.spec_from_file_location("tribunal_native_cost", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cost = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cost)


def record(at, kind, payload):
    return {"timestamp": at, "type": kind, "payload": payload}


def session(*items):
    return ("\n".join(json.dumps(item) for item in items) + "\n").encode()


def sample_session():
    return session(
        record("2026-10-03T00:00:00Z", "session_meta", {"id": "session"}),
        record("2026-10-03T00:00:00Z", "response_item", {
            "type": "custom_tool_call", "call_id": "one", "input": "SECRET"}),
        record("2026-10-03T00:00:00.500Z", "response_item", {
            "type": "custom_tool_call", "call_id": "two"}),
        record("2026-10-03T00:00:01Z", "response_item", {
            "type": "custom_tool_call_output", "call_id": "one", "output": "SECRET"}),
        record("2026-10-03T00:00:02Z", "response_item", {
            "type": "custom_tool_call_output", "call_id": "two"}),
        record("2026-10-03T00:00:02Z", "token_usage_record", {
            "thread_token_usage": {"input_tokens": 10, "cached_input_tokens": 2,
                                   "output_tokens": 3, "reasoning_output_tokens": 1,
                                   "total_tokens": 13}}),
        record("2026-10-03T00:00:03Z", "token_usage_record", {
            "thread_token_usage": {"input_tokens": 20, "cached_input_tokens": 5,
                                   "output_tokens": 7, "reasoning_output_tokens": 2,
                                   "total_tokens": 27}}),
    )


def test_session_counts_overlapping_waits_and_last_cumulative_tokens():
    result = cost.summarize_session(sample_session())
    assert result == {
        "native_tool_call_count_observed": 2,
        "paired_tool_call_count": 2,
        "pairing_complete": True,
        "tool_wait_sum_ms": 2500,
        "tool_wait_union_ms": 2000,
        "tokens_cumulative_last": {
            "input_tokens": 20, "cached_input_tokens": 5,
            "output_tokens": 7, "reasoning_output_tokens": 2,
            "total_tokens": 27,
        },
    }
    assert "SECRET" not in json.dumps(result)


def test_unpaired_call_has_unknown_wait_and_missing_tokens_are_unknown():
    raw = session(
        record("2026-10-03T00:00:00Z", "session_meta", {}),
        record("2026-10-03T00:00:01Z", "response_item", {
            "type": "custom_tool_call", "call_id": "one"}),
    )
    result = cost.summarize_session(raw)
    assert result["native_tool_call_count_observed"] == 1
    assert result["paired_tool_call_count"] == 0
    assert result["pairing_complete"] is False
    assert result["tool_wait_sum_ms"] is None
    assert result["tool_wait_union_ms"] is None
    assert result["tokens_cumulative_last"] is None


@pytest.mark.parametrize("raw", [
    b"not json\n",
    session(record("2026-10-03T00:00:00Z", "session_meta", {}),
            record("2026-10-03T00:00:01Z", "response_item", {
                "type": "custom_tool_call_output", "call_id": "orphan"})),
    session(record("2026-10-03T00:00:00Z", "session_meta", {}),
            record("2026-10-03T00:00:01Z", "response_item", {
                "type": "custom_tool_call", "call_id": "same"}),
            record("2026-10-03T00:00:02Z", "response_item", {
                "type": "custom_tool_call", "call_id": "same"})),
    session(record("2026-10-03T00:00:00Z", "session_meta", {}),
            record("2026-10-03T00:00:02Z", "response_item", {
                "type": "custom_tool_call", "call_id": "one"}),
            record("2026-10-03T00:00:01Z", "response_item", {
                "type": "custom_tool_call_output", "call_id": "one"})),
])
def test_invalid_session_fails_without_partial_measurement(raw):
    with pytest.raises(cost.CostError, match="COST_SESSION_INVALID"):
        cost.summarize_session(raw)


def test_cumulative_token_counter_cannot_decrease():
    items = [json.loads(line) for line in sample_session().splitlines()]
    items[-1]["payload"]["thread_token_usage"]["total_tokens"] = 1
    with pytest.raises(cost.CostError, match="COST_SESSION_INVALID"):
        cost.summarize_session(session(*items))


def test_explicit_file_is_owned_regular_bounded_and_not_symlink(tmp_path, monkeypatch):
    original = tmp_path / "session.jsonl"
    original.write_bytes(sample_session())
    original.chmod(0o664)  # Real Codex session files can be group-writable.
    assert cost.read_file(original, cost.MAX_SESSION_BYTES) == sample_session()
    link = tmp_path / "link.jsonl"
    link.symlink_to(original)
    with pytest.raises(cost.CostError, match="COST_FILE_UNAVAILABLE"):
        cost.read_file(link, cost.MAX_SESSION_BYTES)
    with pytest.raises(cost.CostError, match="COST_FILE_TOO_LARGE"):
        cost.read_file(original, 10)
    original.chmod(0o666)
    with pytest.raises(cost.CostError, match="COST_FILE_UNSAFE"):
        cost.read_file(original, cost.MAX_SESSION_BYTES)
    original.chmod(0o664)
    monkeypatch.setattr(cost.os, "geteuid", lambda: -1)
    with pytest.raises(cost.CostError, match="COST_FILE_UNSAFE"):
        cost.read_file(original, cost.MAX_SESSION_BYTES)


def test_cli_reads_explicit_telemetry_and_transcript_without_writing(git_repo, tmp_path):
    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    begun = subprocess.run(
        [sys.executable, str(cli), "begin", "--base", "master",
         "--runtime", "codex", "--round", "1"],
        cwd=git_repo, capture_output=True, text=True, check=False,
    )
    assert begun.returncode == 0, begun.stderr
    pending = json.loads(begun.stdout)
    run_id = pending["telemetry"]["run_id"]
    telemetry_path = git_repo / ".review/telemetry.json"
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(sample_session())
    before = telemetry_path.stat()
    contents = telemetry_path.read_bytes()
    names = set(telemetry_path.parent.iterdir())
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--telemetry", str(telemetry_path),
         "--run-id", run_id, "--transcript", f"B={transcript}"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stderr == ""
    value = json.loads(result.stdout)
    assert value["provenance"] == "user_supplied_transcripts_unbound"
    assert value["transcript_scope"] == "entire_supplied_session"
    assert value["gate_evidence"] is False
    assert value["binding"]["diff_sha256"] == pending["snapshot"]["diff_sha256"]
    assert value["reviewers"]["B"]["native_tool_call_count_observed"] == 2
    assert value["reviewers"]["B"]["reviewer_total_ms"] is None
    assert "SECRET" not in result.stdout
    assert telemetry_path.read_bytes() == contents
    assert telemetry_path.stat().st_mtime_ns == before.st_mtime_ns
    assert set(telemetry_path.parent.iterdir()) == names


def test_telemetry_requires_private_mode(tmp_path):
    telemetry_path = tmp_path / "telemetry.json"
    telemetry_path.write_text('{"schema": 3, "runs": []}')
    telemetry_path.chmod(0o644)
    with pytest.raises(cost.CostError, match="COST_FILE_UNSAFE"):
        cost.summarize(telemetry_path, "a" * 32, [f"B={tmp_path / 'session.jsonl'}"])
