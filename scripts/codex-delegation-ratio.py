#!/usr/bin/env python3
"""Codex rollout의 관측 호출을 집계한다. Claude 실험과 분리된 서술 지표다."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = os.path.expanduser("~/.codex/sessions")
CALLS = {"function_call", "custom_tool_call"}
OUTPUTS = {"function_call_output", "custom_tool_call_output"}
IGNORED_ENVELOPES = {"session_meta", "event_msg", "turn_context", "world_state", "compacted",
                     "token_usage_record", "inter_agent_communication_metadata"}
IGNORED_ITEMS = {"message", "agent_message", "reasoning", "additional_tools", "compaction"}
MAIN_SOURCES = {"cli", "vscode", "exec", "mcp", "app_server", "app-server"}
NATIVE_NAMESPACES = {None, "functions", "collaboration"}
DIRECT_NAMES = {"exec_command", "write_stdin", "shell", "shell_command", "read_file", "list_dir",
                "list_directory", "glob", "grep", "search_query", "open", "click", "find",
                "view_image", "read_mcp_resource", "list_mcp_resources", "list_mcp_resource_templates",
                "codebase_search", "ToolSearch", "WebSearch", "WebFetch"}
MCP_INFO = re.compile(r"search|fetch|retrieve|recall|query", re.IGNORECASE)
NUMBERS = ("main_tool_calls", "sub_tool_calls", "direct", "delegate", "delegate_spawn",
           "delegate_followup", "opaque_calls", "other_calls", "main_bytes", "sub_bytes")


class HeldFile(ValueError):
    """소유 구간을 확정할 수 없는 기록을 main으로 추측하지 않는다."""


def parse_ts(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    return result if result.tzinfo is not None else None


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def natural(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def supported_subagent(source):
    if isinstance(source, str):
        return source in {"review", "compact", "memory_consolidation"}
    if not isinstance(source, dict) or len(source) != 1:
        return False
    if "other" in source:
        return nonempty(source["other"])
    spawn = source.get("thread_spawn")
    return (isinstance(spawn, dict) and nonempty(spawn.get("parent_thread_id"))
            and natural(spawn.get("depth")))


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def tool_name(payload):
    name, namespace = payload.get("name"), payload.get("namespace")
    if not nonempty(name) or namespace is not None and not nonempty(namespace):
        raise ValueError("invalid tool name or namespace")
    if namespace is None and "." in name:
        namespace, name = name.rsplit(".", 1)
    elif namespace and name.startswith(namespace + "."):
        name = name[len(namespace) + 1:]
    return namespace, name


def category(row):
    namespace, name = row["namespace"], row["name"]
    if namespace in NATIVE_NAMESPACES:
        if name == "spawn_agent":
            return "delegate_spawn"
        if name == "followup_task":
            return "delegate_followup"
        if name == "exec":
            return "opaque_calls"
        if name in DIRECT_NAMES:
            return "direct"
    if namespace == "web" and name in {"run", "search", "open", "find", "click"}:
        return "direct"
    if ((name.startswith("mcp__") and MCP_INFO.search(name.rsplit("__", 1)[-1]))
            or isinstance(namespace, str) and namespace.startswith("mcp__") and MCP_INFO.search(name)):
        return "direct"
    return "other_calls"


def event_item(payload, stamp, coverage):
    item_type = payload.get("type")
    flavor = "custom" if item_type.startswith("custom_") else "function"
    cid = payload.get("call_id")
    if not nonempty(cid):
        coverage["outputs_missing_call_id" if item_type in OUTPUTS else "invalid_items"] += 1
        return None
    if item_type in CALLS:
        namespace, name = tool_name(payload)
        args = payload.get("input" if flavor == "custom" else "arguments")
        if not isinstance(args, str):
            raise ValueError("invalid tool arguments")
        return {"event": "call", "flavor": flavor, "call_id": cid, "timestamp": stamp,
                "name": name, "namespace": namespace,
                "signature": fingerprint([flavor, namespace, name, args])}
    value = payload.get("output")
    if not isinstance(value, (str, list)):
        raise ValueError("invalid tool output")
    size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    item_id = payload.get("id")
    if item_id is not None and not nonempty(item_id):
        raise ValueError("invalid output ID")
    if item_id is None:
        coverage["idless_outputs"] += 1
    return {"event": "output", "flavor": flavor, "call_id": cid, "timestamp": stamp,
            "item_id": item_id, "bytes": size,
            "signature": fingerprint([flavor, cid, value])}


def ownership(meta):
    if (not isinstance(meta, dict) or not nonempty(meta.get("id"))
            or not nonempty(meta.get("session_id", meta["id"]))):
        raise HeldFile("unknown_metadata")
    source = meta.get("source")
    if isinstance(source, str) and source in MAIN_SOURCES:
        kind = "main"
    elif (isinstance(source, dict) and set(source) == {"subagent"}
          and supported_subagent(source["subagent"])):
        kind = "sub"
    else:
        raise HeldFile("unknown_source")
    parent = meta.get("parent_thread_id")
    if parent is not None and not nonempty(parent):
        raise HeldFile("unknown_metadata")
    if kind == "sub" and isinstance(source["subagent"], dict):
        spawn = source["subagent"].get("thread_spawn")
        if spawn is not None and parent is not None and parent != spawn["parent_thread_id"]:
            raise HeldFile("unknown_metadata")
    start = meta.get("subagent_history_start_ordinal")
    if start is not None and not natural(start):
        raise HeldFile("unknown_boundary")
    base = meta.get("history_base")
    if base is not None and not (
        isinstance(base, dict) and nonempty(base.get("thread_id"))
        and natural(base.get("end_ordinal_exclusive"))
        and natural(base.get("end_byte_offset"))
    ):
        raise HeldFile("unknown_boundary")
    cutoff = meta.get("forked_from_ordinal_exclusive")
    if cutoff is not None and not natural(cutoff):
        raise HeldFile("unknown_boundary")
    fork = meta.get("forked_from_id")
    if cutoff is not None and fork is None:
        raise HeldFile("unknown_boundary")
    # The parent's cutoff locates a copied main prefix, not a child projection.
    # A history_base file already contains only its physical local suffix.
    if kind == "main" and base is None and cutoff is not None:
        start = cutoff
    if fork is not None and (not nonempty(fork) or start is None and base is None):
        raise HeldFile("unknown_boundary")
    return kind, start


def rollout_paths(root, coverage):
    def failed(_error):
        coverage["read_errors"] += 1

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        real_dirs = []
        for name in sorted(dirs):
            if (Path(directory) / name).is_symlink():
                coverage["symlinks_skipped"] += 1
            else:
                real_dirs.append(name)
        dirs[:] = real_dirs
        for name in sorted(files):
            if not name.endswith(".jsonl"):
                continue
            path = Path(directory) / name
            coverage["files_seen"] += 1
            try:
                mode = path.lstat().st_mode
            except OSError:
                coverage["read_errors"] += 1
                continue
            if stat.S_ISLNK(mode):
                coverage["symlinks_skipped"] += 1
            elif not stat.S_ISREG(mode):
                coverage["nonregular_files_skipped"] += 1
            else:
                yield path


def load_rollout(path, coverage):
    """첫 metadata와 own 구간의 작은 호출 레코드만 보존한다."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise HeldFile("unreadable_file")
        meta = None
        rows = []
        before_meta = False
        for raw in stream:
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError, RecursionError):
                coverage["malformed_records"] += 1
                continue
            if not isinstance(record, dict):
                coverage["malformed_records"] += 1
                continue
            payload = record.get("payload")
            record_type = record.get("type")
            if record_type == "session_meta" and meta is None:
                kind, start = ownership(payload)
                meta = payload
                if before_meta:
                    raise HeldFile("unknown_metadata")
                continue
            if meta is None:
                if record_type == "response_item":
                    before_meta = True
                continue
            ordinal = record.get("ordinal")
            if start is not None:
                if natural(ordinal) and ordinal < start:
                    coverage["inherited_records_skipped"] += 1
                    continue
                if (record_type == "response_item" and isinstance(payload, dict)
                        and isinstance(payload.get("type"), str)
                        and payload["type"] in CALLS | OUTPUTS and not natural(ordinal)):
                    raise HeldFile("unknown_boundary")
            if record_type != "response_item":
                if not isinstance(record_type, str) or record_type not in IGNORED_ENVELOPES:
                    coverage["unsupported_records"] += 1
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
                coverage["invalid_items"] += 1
                continue
            item_type = payload["type"]
            if item_type not in CALLS | OUTPUTS:
                if item_type not in IGNORED_ITEMS:
                    coverage["unsupported_records"] += 1
                continue
            stamp = parse_ts(record.get("timestamp"))
            if stamp is None:
                coverage["invalid_items"] += 1
                continue
            try:
                item = event_item(payload, stamp, coverage)
            except (ValueError, UnicodeError, RecursionError):
                coverage["invalid_items"] += 1
                continue
            if item is not None:
                rows.append(item)
        if meta is None:
            raise HeldFile("unknown_metadata")
        if meta.get("history_base") is not None:
            coverage["history_base_files"] += 1
        return meta["id"], kind, rows


def bucket():
    return {**dict.fromkeys(NUMBERS, 0), "main_threads": set(), "sub_threads": set()}


def public_bucket(value):
    return {**{name: value[name] for name in NUMBERS},
            "sessions": len(value["main_threads"]),
            "subagent_sessions": len(value["sub_threads"]),
            "delegation_pct": round(100 * value["delegate"] / value["main_tool_calls"], 2)
            if value["main_tool_calls"] else 0.0,
            "offload_pct": round(100 * value["sub_bytes"] / (value["main_bytes"] + value["sub_bytes"]), 2)
            if value["main_bytes"] + value["sub_bytes"] else 0.0}


def register(store, conflicts, key, row, coverage):
    label = "calls" if row["event"] == "call" else "outputs"
    if key in conflicts:
        return
    previous = store.get(key)
    if previous is None:
        store[key] = row
    elif previous["signature"] != row["signature"]:
        coverage["conflicting_" + label] += 1
        conflicts.add(key)
        del store[key]
    else:
        coverage["duplicate_" + label] += 1
        if row["timestamp"] < previous["timestamp"]:
            store[key] = row


def collect(root, since_dt):
    path = Path(root)
    if not root or not path.is_dir():
        raise ValueError("root not found or not a directory")
    if path.is_symlink():
        raise ValueError("root must not be a symlink")
    with os.scandir(path):
        pass  # Unreadable root is an error, not an empty successful measurement.
    coverage = Counter({name: 0 for name in (
        "files_seen", "files_scanned", "files_held", "symlinks_skipped",
        "nonregular_files_skipped", "read_errors", "malformed_records",
        "invalid_items", "inherited_records_skipped", "history_base_files",
        "unsupported_records", "duplicate_calls", "duplicate_outputs",
        "conflicting_calls", "conflicting_outputs", "orphan_outputs",
        "outputs_missing_call_id", "idless_outputs", "conflicting_threads",
    )})
    overall = bucket()
    days = {}
    calls, outputs, roles = {}, {}, {}
    conflicting_calls, conflicting_outputs, conflicting_threads = set(), set(), set()
    for candidate in rollout_paths(path, coverage):
        try:
            thread, kind, rows = load_rollout(candidate, coverage)
        except (OSError, HeldFile) as error:
            coverage["files_held"] += 1
            coverage[str(error) if isinstance(error, HeldFile) else "read_errors"] += 1
            continue
        coverage["files_scanned"] += 1
        if thread in roles and roles[thread] != kind:
            conflicting_threads.add(thread)
        roles[thread] = kind
        for row in rows:
            row["thread"] = thread
            if row["event"] == "call":
                key = (thread, row["flavor"], row["call_id"])
                register(calls, conflicting_calls, key, row, coverage)
            else:
                key = (thread, "item", row["item_id"]) if row["item_id"] is not None else (
                    thread, "fallback", row["signature"])
                register(outputs, conflicting_outputs, key, row, coverage)
    coverage["conflicting_threads"] = len(conflicting_threads)
    for kind in ("main", "sub"):
        coverage[kind + "_threads"] = sum(
            role == kind and thread not in conflicting_threads for thread, role in roles.items()
        )

    def include(row):
        thread = row["thread"]
        stamp = row["timestamp"]
        if thread in conflicting_threads or since_dt is not None and stamp < since_dt:
            return
        kind = roles[thread]
        day = stamp.astimezone().strftime("%Y-%m-%d")
        for item in (overall, days.setdefault(day, bucket())):
            item[kind + "_threads"].add(thread)
            if row["event"] == "output":
                item[kind + "_bytes"] += row["bytes"]
            else:
                item[kind + "_tool_calls"] += 1
                if kind == "main":
                    label = category(row)
                    item[label] += 1
                    if label in {"delegate_spawn", "delegate_followup"}:
                        item["delegate"] += 1

    for row in calls.values():
        include(row)
    for row in outputs.values():
        key = (row["thread"], row["flavor"], row["call_id"])
        if key not in calls:
            coverage["orphan_outputs"] += 1
        else:
            include(row)
    coverage["complete"] = not any(coverage[name] for name in (
        "files_held", "read_errors", "malformed_records", "invalid_items",
        "symlinks_skipped", "nonregular_files_skipped",
        "unsupported_records", "conflicting_calls", "conflicting_outputs",
        "orphan_outputs", "outputs_missing_call_id", "idless_outputs", "conflicting_threads",
    ))
    return {"provider": "codex", "descriptive_only": True,
            "overall": public_bucket(overall),
            "days": [{"day": day, **public_bucket(value)} for day, value in sorted(days.items())],
            "coverage": dict(coverage)}


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--days must be a positive integer")
    if number <= 0:
        raise argparse.ArgumentTypeError("--days must be a positive integer")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=ROOT)
    parser.add_argument("--days", type=positive_int, default=14)
    parser.add_argument("--since", help="YYYY-MM-DD (local midnight)")
    parser.add_argument("--overall", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        if args.since:
            try:
                since_dt = datetime.strptime(args.since, "%Y-%m-%d").astimezone()
            except ValueError:
                raise ValueError("--since must use YYYY-MM-DD")
        elif args.overall:
            since_dt = None
        else:
            since_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
        result = collect(args.root, since_dt)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Codex · descriptive only · recorded tool calls / stored output bytes")
        if not args.overall:
            for row in result["days"]:
                print(row["day"] + " " + json.dumps({k: v for k, v in row.items() if k != "day"}))
        print(json.dumps(result["overall"], ensure_ascii=False))
        print("coverage: " + json.dumps(result["coverage"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
