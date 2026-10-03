# Offline Tribunal native-cost summary (#114)

`scripts/tribunal-native-cost.py` reads one explicit tribunal telemetry file and
one or more explicit Codex reviewer JSONL files. It does not discover sessions,
create a review lock, modify the repository, or affect Tribunal verdicts.

```sh
python3 scripts/tribunal-native-cost.py \
  --telemetry /absolute/repo/.review/telemetry.json \
  --run-id 0123456789abcdef0123456789abcdef \
  --transcript A=/absolute/path/to/reviewer-a.jsonl \
  --transcript B=/absolute/path/to/reviewer-b.jsonl
```

The JSON output records the telemetry run's HEAD, diff digest, contract,
invocation duration, and a reviewer-total duration only when exactly one span
exists for that supplied reviewer's transcript. A missing or clock-anomalous
duration remains `null`; multiple spans for one reviewer fail with
`COST_REVIEWER_ATTEMPT_AMBIGUOUS` because one transcript cannot be assigned to
one of several attempts. Per supplied transcript it reports observed top-level
`custom_tool_call` count, matched
call/output count, summed and overlap-deduplicated tool wait, and the **last**
cumulative `thread_token_usage` counters. Missing token records and incomplete
call/output pairing produce `null` metrics rather than invented zeros. The tool
never prints tool arguments, tool outputs, message bodies, or file paths.
Counts cover the entire supplied session, not nested shell commands or an
automatically isolated Tribunal time window. `telemetry_outcome` is the
telemetry run outcome, not a PR gate verdict.

Transcript association is caller-provided, not authenticated by the Tribunal.
The `provenance` field says `user_supplied_transcripts_unbound` and
`gate_evidence` is always `false`. Use these results only for offline cost
comparison; do not add them to report-budget enforcement, telemetry's
`total_command_count`, or PR pass/fail decisions. A comparison across different
HEADs, diffs, rounds, or workloads is observational and cannot establish a
causal improvement.
