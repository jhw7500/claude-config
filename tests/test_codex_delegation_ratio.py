"""Codex 계측의 회귀: 부모 복사본·event mirror를 새 실행으로 세지 않는다."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/codex-delegation-ratio.py"
TS = datetime(2026, 8, 20, 12).astimezone().isoformat()


def rec(kind, payload, ordinal=None, ts=TS):
    value = {"type": kind, "timestamp": ts, "payload": payload}
    if ordinal is not None:
        value["ordinal"] = ordinal
    return value


def metadata(thread="main", source="cli", **extra):
    return rec("session_meta", {
        "id": thread, "session_id": thread, "source": source,
        "timestamp": TS, "cli_version": "0.166.0", **extra,
    }, 0)


def child(thread="child", **extra):
    return metadata(thread, {"subagent": {"thread_spawn": {
        "parent_thread_id": "main", "depth": 1,
    }}}, session_id="main", parent_thread_id="main", **extra)


def call(name="exec_command", cid="c1", ordinal=1, namespace="functions", **extra):
    return rec("response_item", {
        "type": "function_call", "name": name, "namespace": namespace,
        "call_id": cid, "arguments": "{}", **extra,
    }, ordinal)


def write_rollout(root, name, rows):
    path = root / "2026/08/20" / ("rollout-" + name + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(
        x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in rows
    ) + "\n")
    return path


def run(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        text=True, capture_output=True, timeout=10,
    )


def report(root, *args):
    result = run(root, *(args or ("--overall",)), "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_main_and_subagent_calls_are_separate(tmp_path):
    write_rollout(tmp_path, "main", [metadata(), call(), call(cid="c2", ordinal=2)])
    write_rollout(tmp_path, "child", [child(), call(cid="sub")])
    data = report(tmp_path)
    assert data["provider"] == "codex"
    assert data["descriptive_only"] is True
    assert data["overall"]["main_tool_calls"] == 2
    assert data["overall"]["sub_tool_calls"] == 1
    assert data["overall"]["sessions"] == 1
    assert data["overall"]["subagent_sessions"] == 1


def test_first_metadata_survives_inherited_parent_metadata(tmp_path):
    write_rollout(tmp_path, "child", [
        child(forked_from_id="main", subagent_history_start_ordinal=3),
        rec("session_meta", metadata()["payload"], 1),
        call(cid="inherited", ordinal=2), call(cid="own", ordinal=3),
    ])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 0
    assert data["overall"]["sub_tool_calls"] == 1
    assert data["coverage"]["inherited_records_skipped"] == 2


@pytest.mark.parametrize("sub", [False, True])
def test_any_fork_without_own_boundary_is_held(tmp_path, sub):
    m = child(forked_from_id="parent") if sub else metadata(forked_from_id="parent")
    write_rollout(tmp_path, "fork", [m, call(cid="ambiguous")])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 0
    assert data["overall"]["sub_tool_calls"] == 0
    assert data["coverage"]["files_held"] == 1
    assert data["coverage"]["complete"] is False


def test_main_fork_also_excludes_inherited_prefix(tmp_path):
    write_rollout(tmp_path, "fork", [
        metadata(forked_from_id="parent", subagent_history_start_ordinal=2),
        call(cid="parent", ordinal=1), call(cid="own", ordinal=2),
    ])
    assert report(tmp_path)["overall"]["main_tool_calls"] == 1


def test_history_base_counts_physical_suffix_without_following_reference(tmp_path):
    outside = tmp_path.parent / "not-readable-prefix.jsonl"
    outside.write_text("not a Codex rollout")
    write_rollout(tmp_path, "fork", [
        metadata(forked_from_id="parent", history_base={
            "thread_id": "../../not-readable-prefix", "end_ordinal_exclusive": 40,
            "end_byte_offset": 200,
        }), call(cid="own", ordinal=40),
    ])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 1
    assert data["coverage"]["history_base_files"] == 1
    assert outside.read_text() == "not a Codex rollout"


@pytest.mark.parametrize("source", [None, [], {"worker": "unknown"}, "unknown"])
def test_unknown_source_is_not_assumed_main(tmp_path, source):
    write_rollout(tmp_path, "unknown", [metadata(source=source), call()])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 0
    assert data["coverage"]["files_held"] == 1


def test_missing_metadata_is_not_a_valid_zero_session(tmp_path):
    write_rollout(tmp_path, "missing", ["{bad", "[]", call()])
    data = report(tmp_path)
    assert data["coverage"]["files_held"] == 1
    assert data["coverage"]["complete"] is False


def test_nonfork_legacy_records_without_ordinals_are_supported(tmp_path):
    row = call()
    row.pop("ordinal")
    write_rollout(tmp_path, "legacy", [metadata(), row])
    assert report(tmp_path)["overall"]["main_tool_calls"] == 1


def test_missing_ordinal_in_owned_fork_holds_the_file(tmp_path):
    row = call()
    row.pop("ordinal")
    write_rollout(tmp_path, "fork", [
        child(forked_from_id="main", subagent_history_start_ordinal=2), row,
        call(cid="other", ordinal=2),
    ])
    data = report(tmp_path)
    assert data["overall"]["sub_tool_calls"] == 0
    assert data["coverage"]["files_held"] == 1


def test_symlink_files_and_directories_are_not_traversed(tmp_path):
    root = tmp_path / "root"
    good = write_rollout(root, "main", [metadata(), call()])
    outside = write_rollout(tmp_path / "outside", "extra", [metadata("extra"), call()])
    (good.parent / "linked.jsonl").symlink_to(outside)
    (root / "linked-dir").symlink_to(outside.parent, target_is_directory=True)
    data = report(root)
    assert data["overall"]["main_tool_calls"] == 1
    assert data["coverage"]["symlinks_skipped"] == 2


def test_fifo_does_not_block_the_reader(tmp_path):
    path = write_rollout(tmp_path, "main", [metadata(), call()])
    os.mkfifo(path.parent / "fifo.jsonl")
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 1
    assert data["coverage"]["nonregular_files_skipped"] == 1


def test_empty_root_is_distinct_from_missing_root(tmp_path):
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 0
    assert data["coverage"]["files_seen"] == 0
    assert data["coverage"]["complete"] is True
    result = run(tmp_path / "missing", "--overall")
    assert result.returncode != 0
    assert "not found" in result.stderr


def output(value="ok", cid="c1", item="o1", ordinal=2, custom=False, ts=TS):
    payload = {"type": "custom_tool_call_output" if custom else "function_call_output",
               "call_id": cid, "output": value}
    if item is not None:
        payload["id"] = item
    return rec("response_item", payload, ordinal, ts)


def test_namespaces_classification_and_communication_are_distinct(tmp_path):
    rows = [metadata(), call(),
            call("spawn_agent", "spawn", namespace="collaboration"),
            call("followup_task", "follow", namespace="collaboration"),
            call("send_message", "message", namespace="collaboration"),
            call("spawn_agent", "rogue", namespace="rogue"),
            call("exec", "opaque", type="custom_tool_call", input="tools.spawn_agent()"),
            call("mcp__notion__query", "query", namespace=None),
            call("mcp__query_admin__delete_index", "delete", namespace=None),
            call("apply_patch", "edit")]
    write_rollout(tmp_path, "main", rows)
    value = report(tmp_path)["overall"]
    assert value["main_tool_calls"] == 9
    assert value["direct"] == 2
    assert value["delegate_spawn"] == 1
    assert value["delegate_followup"] == 1
    assert value["delegate"] == 2
    assert value["opaque_calls"] == 1
    assert value["other_calls"] == 4
    assert value["delegation_pct"] == 22.22


def test_opaque_calls_remain_in_delegation_denominator(tmp_path):
    rows = [metadata(), call("spawn_agent", "s", namespace="collaboration")]
    rows += [call("exec", f"w{i}", type="custom_tool_call", input="batch()") for i in range(99)]
    write_rollout(tmp_path, "main", rows)
    value = report(tmp_path)["overall"]
    assert value["main_tool_calls"] == 100
    assert value["opaque_calls"] == 99
    assert value["delegation_pct"] == 1.0


def test_recorded_utf8_bytes_are_separate_for_main_and_child(tmp_path):
    write_rollout(tmp_path, "main", [metadata(), call(), output("한")])
    write_rollout(tmp_path, "child", [child(), call(), output("aaa")])
    value = report(tmp_path)["overall"]
    assert value["main_bytes"] == 5  # JSON quotes + UTF-8 한
    assert value["sub_bytes"] == 5
    assert value["offload_pct"] == 50.0


def test_structured_custom_output_is_measured(tmp_path):
    write_rollout(tmp_path, "main", [
        metadata(), call("exec", type="custom_tool_call", input="text()"),
        output([{"type": "input_text", "text": "한"}], custom=True),
    ])
    assert report(tmp_path)["overall"]["main_bytes"] == 39


def test_event_mirrors_and_api_request_copies_are_not_calls(tmp_path):
    native = call()
    write_rollout(tmp_path, "main", [metadata(), native,
        rec("event_msg", {"type": "item_completed", "item": native["payload"]}),
        rec("api-request", {"messages": [native]})])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 1
    assert data["coverage"]["unsupported_records"] == 1
    assert data["coverage"]["complete"] is False


def test_duplicate_rollout_copies_count_calls_and_outputs_once(tmp_path):
    rows = [metadata(), call(), output()]
    write_rollout(tmp_path, "a", rows)
    write_rollout(tmp_path, "b", rows)
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 1
    assert data["overall"]["main_bytes"] == 4
    assert data["coverage"]["duplicate_calls"] == 1
    assert data["coverage"]["duplicate_outputs"] == 1


def test_conflicting_duplicate_call_is_withheld_not_first_wins(tmp_path):
    write_rollout(tmp_path, "main", [metadata(), call(), call("read_file"), output()])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 0
    assert data["overall"]["main_bytes"] == 0
    assert data["coverage"]["conflicting_calls"] == 1
    assert data["coverage"]["complete"] is False


def test_multiple_output_fragments_are_not_duplicate_calls(tmp_path):
    write_rollout(tmp_path, "main", [metadata(), call(), output("one"),
        output("one"), output("two", item="o2", ordinal=3)])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 1
    assert data["overall"]["main_bytes"] == 10
    assert data["coverage"]["duplicate_outputs"] == 1


def test_conflicting_output_item_is_not_double_counted(tmp_path):
    write_rollout(tmp_path, "main", [metadata(), call(), output("one"), output("two")])
    data = report(tmp_path)
    assert data["overall"]["main_bytes"] == 0
    assert data["coverage"]["conflicting_outputs"] == 1
    assert data["coverage"]["complete"] is False


def test_missing_call_id_and_orphan_outputs_are_visible(tmp_path):
    missing = output()
    missing["payload"].pop("call_id")
    write_rollout(tmp_path, "main", [metadata(), missing, output(cid="missing")])
    data = report(tmp_path)
    assert data["overall"]["main_bytes"] == 0
    assert data["coverage"]["outputs_missing_call_id"] == 1
    assert data["coverage"]["orphan_outputs"] == 1
    assert data["coverage"]["complete"] is False


def test_idless_legacy_output_uses_fingerprint_deduplication(tmp_path):
    write_rollout(tmp_path, "main", [metadata(), call(), output(item=None), output(item=None)])
    data = report(tmp_path)
    assert data["overall"]["main_bytes"] == 4
    assert data["coverage"]["duplicate_outputs"] == 1
    assert data["coverage"]["idless_outputs"] == 2


def test_output_inside_window_can_match_earlier_call(tmp_path):
    old = datetime(2026, 8, 19, 23).astimezone().isoformat()
    early = call()
    early["timestamp"] = old
    write_rollout(tmp_path, "main", [metadata(), early, output()])
    data = report(tmp_path, "--since", "2026-08-20")
    assert data["overall"]["main_tool_calls"] == 0
    assert data["overall"]["main_bytes"] == 4
    assert data["days"][0]["day"] == "2026-08-20"


def test_since_precedence_and_local_midnight_buckets(tmp_path):
    earlier = call(cid="old")
    earlier["timestamp"] = datetime(2026, 8, 19, 23).astimezone().isoformat()
    write_rollout(tmp_path, "main", [metadata(), earlier, call()])
    data = report(tmp_path, "--overall", "--since", "2026-08-20")
    assert data["overall"]["main_tool_calls"] == 1
    assert [x["day"] for x in data["days"]] == ["2026-08-20"]
    assert [x["day"] for x in report(tmp_path)["days"]] == ["2026-08-19", "2026-08-20"]


@pytest.mark.parametrize("bad", [None, "bad", "2026-08-20T12:00:00"])
def test_invalid_timestamps_are_reported_without_losing_valid_calls(tmp_path, bad):
    invalid = call(cid="bad")
    invalid["timestamp"] = bad
    write_rollout(tmp_path, "main", [metadata(), invalid, call()])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 1
    assert data["coverage"]["invalid_items"] == 1


def test_bad_nested_shapes_do_not_crash(tmp_path):
    bad = call()
    bad["payload"]["type"] = []
    bad_ns = call(cid="bad-ns", namespace=[])
    write_rollout(tmp_path, "main", [metadata(), bad, bad_ns, call()])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 1
    assert data["coverage"]["invalid_items"] == 2


@pytest.mark.parametrize("value", [0, False, {"unexpected": "shape"}])
def test_unsupported_output_shapes_are_visible(tmp_path, value):
    write_rollout(tmp_path, "main", [metadata(), call(), output(value)])
    data = report(tmp_path)
    assert data["overall"]["main_bytes"] == 0
    assert data["coverage"]["invalid_items"] == 1


@pytest.mark.parametrize("days", ["0", "-1", "abc"])
def test_invalid_days_rejected(tmp_path, days):
    result = run(tmp_path, "--days", days)
    assert result.returncode != 0
    assert "--days" in result.stderr


def test_invalid_since_exits_nonzero(tmp_path):
    result = run(tmp_path, "--since", "8/24")
    assert result.returncode != 0
    assert "YYYY-MM-DD" in result.stderr


@pytest.mark.parametrize("kind", ["web_search_call", "local_shell_call", "image_generation_call"])
def test_unhandled_builtin_calls_cannot_claim_complete_coverage(tmp_path, kind):
    write_rollout(tmp_path, "main", [metadata(), rec("response_item", {"type": kind})])
    data = report(tmp_path)
    assert data["coverage"]["unsupported_records"] == 1
    assert data["coverage"]["complete"] is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_unreadable_root_is_an_error_not_a_successful_empty_report(tmp_path):
    root = tmp_path / "locked"
    root.mkdir()
    root.chmod(0)
    try:
        result = run(root, "--overall")
        assert result.returncode != 0
        assert not result.stdout
    finally:
        root.chmod(0o700)


@pytest.mark.parametrize("variant", [
    {}, "future_variant", {"thread_spawn": []}, {"thread_spawn": {}},
    {"thread_spawn": {"parent_thread_id": [], "depth": 1}},
    {"thread_spawn": {"parent_thread_id": "main", "depth": True}},
    {"thread_spawn": {"parent_thread_id": "main", "depth": "1"}},
    {"other": []}, {"review": {}, "compact": {}},
])
def test_malformed_subagent_provenance_is_held_instead_of_inflating_offload(tmp_path, variant):
    write_rollout(tmp_path, "bad", [metadata("child", {"subagent": variant}), call(), output("hello")])
    data = report(tmp_path)
    assert data["overall"]["sub_tool_calls"] == 0
    assert data["overall"]["sub_bytes"] == 0
    assert data["coverage"]["files_held"] == 1
    assert data["coverage"]["complete"] is False


@pytest.mark.parametrize("variant", ["review", "compact", "memory_consolidation", {"other": "background"}])
def test_supported_parentless_subagent_variants_are_preserved(tmp_path, variant):
    write_rollout(tmp_path, "sub", [metadata("child", {"subagent": variant}), call(), output("hello")])
    data = report(tmp_path)
    assert data["overall"]["sub_tool_calls"] == 1
    assert data["overall"]["sub_bytes"] == 7
    assert data["coverage"]["files_held"] == 0


def test_deeply_nested_record_does_not_abort_other_valid_records(tmp_path):
    deep = '{"type":"event_msg","payload":' + '[' * 1200 + '0' + ']' * 1200 + '}'
    write_rollout(tmp_path, "main", [metadata(), call(), deep, call(cid="next")])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 2
    assert data["coverage"]["malformed_records"] == 1
    assert data["coverage"]["complete"] is False


def test_near_limit_output_cannot_discard_a_valid_neighbor(tmp_path):
    deep_output = ('{"type":"response_item","timestamp":' + json.dumps(TS)
                   + ',"payload":{"type":"function_call_output","call_id":"c1","id":"deep","output":'
                   + '[' * 990 + '0' + ']' * 990 + '}}')
    write_rollout(tmp_path, "main", [metadata(), call(), deep_output])
    data = report(tmp_path)
    assert data["overall"]["main_tool_calls"] == 1
