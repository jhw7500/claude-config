# Strict Tribunal Report and Decision Schema

Reviewer reports are strict JSON objects with exactly `schema`, `reviewer`, `round`, `snapshot`, `status`, `findings`, `executions`, `claims`, and `prior_decisions`. Report size is at most 128 KiB; evidence text is at most 8 KiB; commands and repository-relative paths are at most 4 KiB. Each reviewer has at most 128 findings, 128 executions, 128 claims, and 128 prior decision responses. Decision files contain at most 384 decisions.

The strict parser is authoritative for accepted report text. Every JSON-decoded string must already be Unicode NFC. A physical unescaped newline is invalid JSON; JSON escapes decode to LF and TAB, which are allowed only in `stdout_excerpt` and `stderr_excerpt`. CR, NUL, ESC, DEL, every other `Cc`, and all `Cs` remain invalid. Reports must not be altered: do not trim and do not reserialize the received bytes; compute `capture_sha256` from the full original capture. Emit the final report as one physical line of minified JSON. Examples below are pretty-printed only for readability.

IDs are round-local. Decisions are written after a round and enter only through the following round's `begin --decisions`. A prior decision may be acknowledged only by the originating reviewer. Evidence excerpts are sanitized and bounded; raw secret-bearing output, tokens, private keys, credentials, and absolute home paths invalidate evidence.

A finding path may be any normalized repository-relative path, including outside the diff. Finding scope does not authorize source changes: only controller automatic fixes are limited to round-1 `initial_paths`.

## Controller receipt and recovery contract

The installed CLI and its installed contract binding are the source of truth. Do not self-install or execute candidate source during the tribunal. Native v2 slots are either `pending` or `sealed`; only `submit-report` can turn a pending native slot into a sealed slot. `store-report`, including its legacy replacement option, is v1-only.

`status.verdict_schema` selects the workflow: schema 2 exposes authoritative slot state and receipt fields; schema 1 must be passed once through all-slot `migrate-legacy-pending`, which accepts no reviewer subset. Legacy migration preserves available exact raw evidence but leaves slots pending when receipt provenance is unavailable; `LEGACY_PROVENANCE_UNAVAILABLE` never means a fictional native receipt was adopted.

<!-- v2-status -->
```json
{
  "round": 1,
  "gate_status": "in_progress",
  "blocking_count": 0,
  "verdict_path": ".review/verdict.json",
  "verdict_schema": 2,
  "reviewers": {
    "A": {
      "state": "sealed",
      "attempt_count": 1,
      "last_error": null,
      "raw_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "context_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "report_contract_version": 2,
      "provenance": "native_submit"
    },
    "B": {
      "state": "pending",
      "attempt_count": 1,
      "last_error": "REVIEWER_TIMEOUT"
    },
    "C": {
      "state": "pending",
      "attempt_count": 0,
      "last_error": null
    }
  }
}
```

<!-- v2-submit-receipt -->
```json
{
  "reviewer": "A",
  "round": 1,
  "state": "sealed",
  "raw_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "context_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "report_contract_version": 2,
  "attempt": 1,
  "provenance": "native_submit"
}
```

The exact controller command shapes are below. `submit-report` reads exact report bytes on stdin; it does not normalize or repair them, and canonical accepted report files remain runtime-managed. `record-failure` has a strict operational-reason whitelist and returns only `state`, cumulative `attempt_count`, and `last_error`. Pathless `finalize` authenticates all three sealed receipts. `validate-report --source stored` is read-only and must be run for A/B/C immediately before finalization.

<!-- controller-command-examples -->
| Purpose | Exact CLI arguments |
| --- | --- |
| inspect authoritative state | `status` |
| project one pending role | `context --reviewer A` |
| seal one valid response | `submit-report --reviewer A` |
| record dispatch failure | `record-failure --reviewer A --reason DISPATCH_FAILED` |
| record process failure | `record-failure --reviewer A --reason REVIEWER_FAILED` |
| record true terminal timeout | `record-failure --reviewer A --reason REVIEWER_TIMEOUT` |
| migrate an authentic v1 all-pending round | `migrate-legacy-pending` |
| authenticate one stored receipt | `validate-report --reviewer A --source stored` |
| authenticate all three and aggregate | `finalize` |
<!-- controller-command-examples-end -->

### Format failures

`JSON_INVALID`, `REPORT_TOO_LARGE`, `TEXT_INVALID`, `REPORT_SCHEMA_INVALID`, `REPORT_REVIEWER_MISMATCH`, `REPORT_ROUND_MISMATCH`, `REPORT_SNAPSHOT_MISMATCH`, and `REPORT_NOT_TERMINAL` describe rejected report content. Preserve the full original response in a controller-private current-user-owned regular non-symlink exact-`0600` file, independent of ambient `umask`; do not trim, reserialize, repair, or silently truncate it. A format-only retry stays on the same reviewer handle when that handle can accept a follow-up.

Bounded content failures also include `FINDING_SCHEMA_INVALID`, `FINDING_LIMIT_EXCEEDED`, `EXECUTION_LIMIT_EXCEEDED`, `CLAIM_LIMIT_EXCEEDED`, `PRIOR_DECISION_RESPONSE_MISSING`, `PRIOR_DECISION_RESPONSE_INVALID`, `REPLACEMENT_FINDING_REQUIRED`, and `REPLACEMENT_FINDING_INVALID`. Fresh submission checks the reviewer's own decision responses and replacement references before canonical publication and sealing. Rejected content remains pending with exact bounded failure evidence; corrected same-role submission leaves sealed peers unchanged. Finalization repeats the full closure validation. Count/byte limits remain enforced, and invalid existing canonical orphans remain hard integrity failures outside fresh-input retry handling.

### Operational failures

Only true terminal `DISPATCH_FAILED`, `REVIEWER_FAILED`, and `REVIEWER_TIMEOUT` belong to `record-failure`. A wait-interface timeout with a still-running reviewer handle is not terminal: continue the same handle without recording failure, dispatching a replacement, or cleaning its view. Automatic request limits are controller-local per invocation; persisted `attempt_count` remains cumulative.

### Observation warnings

Telemetry failures and a non-force cleanup refusal for a known-terminal reviewer and verified exact controller-created view are bounded warnings. They do not alter a sealed receipt, report findings, or the gate verdict.

### Integrity stops

Uncertain process liveness, uncertain view identity, unsafe ownership/type/mode, changed report bytes or receipt digest, and changed snapshot or installed contract stop before `finalize`. Three independently revalidated sealed receipts are mandatory; there is no two-of-three pass. A valid HIGH or CRITICAL report seals and contributes its blocker to final aggregation rather than being replaced.

## Complete Reviewer A finding report

<!-- valid-reviewer-a -->
```json
{
  "schema": 1,
  "reviewer": "A",
  "round": 1,
  "snapshot": {
    "head_sha": "1111111111111111111111111111111111111111",
    "diff_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "status": "complete",
  "findings": [
    {
      "id": "A-R1-001",
      "reviewer": "A",
      "severity": "HIGH",
      "title": "Unsafe replacement order",
      "rationale": "The target can change after validation and before replacement.",
      "path": "src/example.py",
      "line": 12,
      "execution_ids": [],
      "acceptance_condition": "Revalidate the target immediately before replacement."
    }
  ],
  "executions": [],
  "claims": [],
  "prior_decisions": []
}
```

## Complete Reviewer B execution and claim report

<!-- valid-reviewer-b -->
```json
{
  "schema": 1,
  "reviewer": "B",
  "round": 1,
  "snapshot": {
    "head_sha": "1111111111111111111111111111111111111111",
    "diff_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "status": "complete",
  "findings": [],
  "executions": [
    {
      "id": "B-R1-E001",
      "command": "python3 -m pytest -q tests/example.py",
      "exit_code": 0,
      "stdout_excerpt": "col1\n\t1 passed",
      "stderr_excerpt": "",
      "capture_sha256": "c170a0864a6f10c7f15ff52e35b6a315716aea7e6c95ad3856f9349f05cd28be",
      "truncated": false
    }
  ],
  "claims": [
    {
      "id": "B-R1-C001",
      "statement": "The focused regression passes.",
      "result": "supported",
      "execution_ids": ["B-R1-E001"],
      "reason": ""
    }
  ],
  "prior_decisions": []
}
```

## Complete Reviewer C empty report

<!-- valid-reviewer-c -->
```json
{
  "schema": 1,
  "reviewer": "C",
  "round": 1,
  "snapshot": {
    "head_sha": "1111111111111111111111111111111111111111",
    "diff_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "status": "complete",
  "findings": [],
  "executions": [],
  "claims": [],
  "prior_decisions": []
}
```

## Complete fixed decision file

<!-- valid-fixed-decision -->
```json
[
  {
    "id": "D-R1-A-001",
    "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
    "disposition": "fixed",
    "rationale": "The target is now revalidated immediately before replacement.",
    "executions": [
      {
        "id": "D-R1-E001",
        "command": "python3 -m pytest -q tests/example.py",
        "exit_code": 0,
        "stdout_excerpt": "fixed",
        "stderr_excerpt": "",
        "capture_sha256": "992a93455c71fedd36ac9bbc439952c041cf61445958472af479269b8d873513",
        "truncated": false
      }
    ]
  }
]
```

## Complete rebutted decision file

<!-- valid-rebutted-decision -->
```json
[
  {
    "id": "D-R1-A-002",
    "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
    "disposition": "rebutted",
    "rationale": "Independent execution demonstrates that the target identity is checked before replacement.",
    "executions": [
      {
        "id": "D-R1-E002",
        "command": "python3 -m pytest -q tests/example.py",
        "exit_code": 0,
        "stdout_excerpt": "rebutted",
        "stderr_excerpt": "",
        "capture_sha256": "234425b18615354e404c1cf962b11d0accea1e5b6a17146af8a907b26c202154",
        "truncated": false
      }
    ]
  }
]
```
