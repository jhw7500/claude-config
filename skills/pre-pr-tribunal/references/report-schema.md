# Strict Tribunal Report and Decision Schema

Reviewer reports are strict JSON objects with exactly `schema`, `reviewer`, `round`, `snapshot`, `status`, `findings`, `executions`, `claims`, and `prior_decisions`; Reviewer B additionally requires `coverage`. Report size is at most 128 KiB; evidence text is at most 8 KiB; commands and repository-relative paths are at most 4 KiB. Each reviewer has at most 128 findings, 128 executions, 128 claims, and 128 prior decision responses. Decision files contain at most 384 decisions.

The strict parser is authoritative for accepted report text. Every JSON-decoded string must already be Unicode NFC. A physical unescaped newline is invalid JSON; JSON escapes decode to LF and TAB, which are allowed only in `stdout_excerpt` and `stderr_excerpt`. CR, NUL, ESC, DEL, every other `Cc`, and all `Cs` remain invalid. Reports must not be altered: do not trim and do not reserialize the received bytes; compute `capture_sha256` from the full original capture. Emit the final report as one physical line of minified JSON. Examples below are pretty-printed only for readability.

IDs are round-local. Decisions are written after a round and enter only through the following round's `begin --decisions`. A prior decision may be acknowledged only by the originating reviewer. Evidence excerpts are sanitized and bounded; raw secret-bearing output, tokens, private keys, credentials, and absolute home paths invalidate evidence.

A finding path may be any normalized repository-relative path, including outside the diff. Finding scope does not authorize source changes: only controller automatic fixes are limited to round-1 `initial_paths`.

## Controller receipt and recovery contract

The installed CLI and its installed contract binding are the source of truth. Do not self-install or execute candidate source during the tribunal. `submit-report`'s `--run-id` and `--attempt` need an installed runtime from this change or later; an older installed package rejects them at parse time with `PRE_PR_TRIBUNAL:USAGE` on stderr and exit status 2, which is the parser's ordinary usage code rather than one of the report codes above, so nothing is read or stored; `/usr/bin/python3 scripts/install-pre-pr-tribunal.py` resolves it by reinstalling the package and the skill together. Current verdict schema 5 uses report text contract 11, diff recipe 1 and evidence contract 2; reviewer report JSON still has `schema: 1`. Native schema-5 slots are `disabled`, `pending`, or `sealed`; only `submit-report` can turn an active pending slot into a sealed slot. Disabled slots reject context, report, failure, and validation operations. `store-report`, including its legacy replacement option, is v1-only.

`status.verdict_schema` selects the workflow. Schema 1 requires an explicit all-slot `migrate-legacy-pending`; compatible schema 2 requires an explicit `migrate-v2-pending`; schemas 3 and 4 have no automatic current-contract migration; schema 5 is current. Neither migration accepts a reviewer subset and both migrate conservatively to iterative A/B/C review. Legacy schema-1 migration preserves available exact raw evidence but leaves slots pending when receipt provenance is unavailable; `LEGACY_PROVENANCE_UNAVAILABLE` never means a fictional native receipt was adopted. Schema-2 migration requires its report/diff contract to match the installed runtime: historical report-contract-2 rounds fail with `CONTRACT_DRIFT`. Compatible migration validates sealed evidence and assigns a new lifecycle identity, so its first telemetry resume reports prior request accounting unknown. Preserve incompatible old rounds for explicit abandonment/restart judgment; readable historical state never upgrades pending authority.

The following stored-verdict excerpt records the schema-5 lifecycle identity for
internal telemetry binding. It is not reviewer report content or review
authority input.

<!-- v5-stored-verdict -->
```json
{
  "schema": 5,
  "lifecycle_id": "0123456789abcdef0123456789abcdef",
  "evidence_contract": 2,
  "evidence_binding": null,
  "evidence_fallback_reason": null,
  "validation": {
    "phase": "fix_verification",
    "requires_full_suite": true,
    "full_suite_receipt_sha256": null,
    "escalation_reason": null,
    "failure_code": null,
    "full_suite_recipe": {
      "kind": "python-pytest-v1",
      "profile": "python-v1",
      "cwd": ".",
      "argv": ["python3", "-I", "-S", "-c", "import os, sys; os.execv(sys.executable, [sys.executable, '-m', 'pytest', '-q'])"],
      "command": "python3 -m pytest -q"
    }
  },
  "policy": {
    "risk_floor": 50,
    "effective_intensity": 50,
    "mode": "single"
  }
}
```

<!-- v5-status -->
```json
{
  "round": 1,
  "gate_status": "in_progress",
  "blocking_count": 0,
  "verdict_path": ".review/verdict.json",
  "verdict_schema": 5,
  "active_reviewers": ["A", "B"],
  "validation": {
    "phase": "fix_verification",
    "requires_full_suite": true,
    "full_suite_receipt_sha256": null,
    "escalation_reason": null,
    "failure_code": null,
    "full_suite_recipe": {
      "kind": "python-pytest-v1",
      "profile": "python-v1",
      "cwd": ".",
      "argv": ["python3", "-I", "-S", "-c", "import os, sys; os.execv(sys.executable, [sys.executable, '-m', 'pytest', '-q'])"],
      "command": "python3 -m pytest -q"
    }
  },
  "reviewers": {
    "A": {
      "state": "sealed",
      "attempt_count": 1,
      "last_error": null,
      "raw_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "context_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "report_contract_version": 11,
      "provenance": "native_submit"
    },
    "B": {
      "state": "pending",
      "attempt_count": 1,
      "last_error": "REVIEWER_TIMEOUT"
    },
    "C": {
      "state": "disabled",
      "attempt_count": 0,
      "last_error": null
    }
  }
}
```

<!-- v5-submit-receipt -->
```json
{
  "reviewer": "A",
  "round": 1,
  "state": "sealed",
  "raw_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "context_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "report_contract_version": 11,
  "attempt": 1,
  "provenance": "native_submit"
}
```

The exact controller command shapes are below. `submit-report` reads exact report bytes on stdin; it does not normalize or repair them, and canonical accepted report files remain runtime-managed. After an interrupted publication it may seal the existing canonical orphan instead of the new stdin bytes. REQUIRED receipt acceptance check, including pending recovery: `receipt.raw_sha256 == SHA256(exact_private_terminal_response_bytes)`, before accepting success or proceeding to stored validation. A mismatch is a controller `REPORT_BYTES_MISMATCH` integrity stop: preserve the new private response, old canonical report, and returned receipt without retry, replacement, or finalization. Stored-validation agreement with the receipt alone cannot establish this input binding. `record-failure` has a strict operational-reason whitelist and returns only `state`, cumulative `attempt_count`, and `last_error`. Pathless `finalize` authenticates every active sealed receipt. `validate-report --source stored` is read-only and must be run for each active reviewer immediately before finalization. A follow-up lifecycle begins in `fix_verification`; provisional PASS also requires `final-validation-seal` to execute the verdict-bound recipe itself and authenticate the resulting fresh receipt against the exact current HEAD, canonical diff, contract, source/tool environment, and capture. No receipt digest is accepted from the caller.

<!-- controller-command-examples -->
| Purpose | Exact CLI arguments |
| --- | --- |
| inspect authoritative state | `status` |
| project one pending role | `context --reviewer A` |
| seal one valid response | `submit-report --reviewer A --run-id "$RUN_ID" --attempt "$ATTEMPT"` |
| record dispatch failure | `record-failure --reviewer A --reason DISPATCH_FAILED` |
| record process failure | `record-failure --reviewer A --reason REVIEWER_FAILED` |
| record true terminal timeout | `record-failure --reviewer A --reason REVIEWER_TIMEOUT` |
| migrate an authentic v1 all-pending round (Node example) | `migrate-legacy-pending --full-suite-kind node-npm-test-v1 --full-suite-cwd .` |
| migrate an authentic v2 pending round (Node example) | `migrate-v2-pending --full-suite-kind node-npm-test-v1 --full-suite-cwd .` |
| preview a recipe-bound follow-up | `re-review-preview --base "$BASE" --runtime "$RUNTIME" --round "$ROUND" --full-suite-kind "$FULL_SUITE_KIND" --full-suite-cwd "$FULL_SUITE_CWD"` |
| authenticate one stored receipt | `validate-report --reviewer A --source stored` |
| execute and bind the one final full suite | `final-validation-seal --timeout 900` |
| inspect an interrupted suite reservation | `final-validation-reservation-status` |
| archive a stopped reservation after explicit recovery judgment | `final-validation-recover --reservation-sha256 "$SHA256" --confirm-process-tree-stopped` |
| authenticate active reviewers and aggregate | `finalize` |
<!-- controller-command-examples-end -->

Schema-1 through schema-4 verdicts remain readable without rewrite. Schema-4 evidence semantics remain verifiable as historical state, but current context, submit, stored validation, finalize and terminal gate authority reject its old contract as drift or stale authority. New rounds and successful migration commands write schema 5 with `evidence_contract: 2` and a snapshot-bound policy record. A compatible schema-2 migration validates every sealed report and receipt before one atomic verdict replacement; it never rewrites report bytes or infers pre-migration telemetry identity.

## Execution provenance under report contract 3 or later

Fresh executions keep the seven fields shown in the B example below. Only Reviewer B may add `evidence_ref`, and it is REQUIRED for reused evidence: exactly `{"bundle_sha256":"<64 lowercase hex>","entry_id":"E001"}` with an entry ID from E001 through E064. A/C executions and controller decision executions keep the legacy shape. The report parser accepts explicit B references under contract 3 or later; sealing then authenticates each against the round's selected evidence binding and exact verifier-returned execution fields. A reference alone grants no authority.

Report contract 4 and later require Reviewer B to submit at least one claim plus `coverage: {"complete": true, "primary_entry_paths": [...]}`. Each primary-entry-path item has exactly `path` and `claim_id`; paths and claim IDs are unique, normalized, and every claim ID must resolve to a B claim in the same report. This makes the reviewer's completeness assertion and entry-path-to-claim mapping explicit before sealing. An empty path list is valid only when the reviewer found no affected documented primary entry path; the non-empty claim requirement still prevents an empty empirical authorization. Contract 5 additionally requires a B report with any `refuted` claim to contain at least one `CRITICAL` or `HIGH` finding, so execution-backed contradiction cannot produce a PASS through an advisory-only report. Two independently deployed contract-6 variants differed on `reversal_cost`; historical reports with or without that field remain readable. Contract 7 requires every finding to include `reversal_cost`; `HIGH` and `CRITICAL` require a nonblank explanation of why fixing it after merge is costly, while `LOW` and `MEDIUM` may use an empty string. Missing or blank blocker cost is retryable `FINDING_SCHEMA_INVALID`. Contract 8 adds the calibrated `native_tool_calls` self-budget to B's private context without changing report bytes. Contract 9 adds the machine-readable `fix_verification`/`final_validation` lifecycle. Contract 10 binds one canonical full-suite recipe (`kind`, `profile`, `cwd`, exact capture `argv`, and human-readable command) into the re-review grant, verdict, status, and reviewer contexts. Contract 11 requires the first round to bind and authenticate an owned full-suite receipt before PASS, and records authenticated nonzero suite outcomes or explicitly abandoned interrupted attempts as non-PASS validation states. Historical contract-7 terminal context digests retain their previous budget shape, historical contract-8 contexts omit validation phase, and historical contract-9 contexts omit the recipe. In-progress rounds with an older contract fail current resume, submission, validation, and finalization with `CONTRACT_DRIFT`; they require an explicit abandonment/restart decision rather than reinterpretation under contract 11.

The first lifecycle also binds `validation.phase: final_validation`, `requires_full_suite: true`, no receipt, and one canonical recipe; it cannot PASS until its owned suite succeeds. Every lifecycle started after a terminal verdict is bound by the one-shot re-review grant to `fix_verification` with the same required suite, no receipt, no escalation reason, and one canonical recipe. The default is `python-pytest-v1` at repository cwd `.`; Node repositories select `node-npm-test-v1` and an explicit cwd with the matching `--full-suite-kind` and `--full-suite-cwd` arguments on preview, grant, and begin. Reviewer contexts retain the complete base-to-HEAD diff and direct reviewers to finding reproduction and direct-impact tests. A reviewer report that records the bound suite command or a recognized whole-suite equivalent during `fix_verification` is rejected with retryable `FULL_SUITE_DURING_FIX_VERIFICATION`. Once every active report is sealed, a provisional PASS cannot finalize until `final-validation-seal` seals a successful suite receipt. The command accepts a timeout, not a receipt: it preflights the recipe, writes a private `0600` lifecycle-bound reservation under the review lock, then runs exactly the verdict-bound profile/cwd/argv as an owned process. A competing caller sees `FINAL_VALIDATION_RESERVED` before capture. The receipt is reauthenticated during sealing, finalization, and the terminal PR gate; an external or relabeled receipt, changed HEAD, diff, contract, environment, capture, or concurrent state change fails closed. An authenticated nonzero suite receipt instead closes the round immediately as `final_validation_failed`/FAIL. Successful and authenticated failed outcomes remove the reservation. Interrupted or unauthenticated captures leave it in place and require explicit recovery judgment; `final-validation-reservation-status` returns its digest and owner PID. After confirming that the original process tree is terminal, `final-validation-recover` requires that exact digest and `--confirm-process-tree-stopped`, refuses a live holder, and archives the marker. Pending HEAD or contract drift requires a separately approved `--abandon-pending` that closes only the old round as FAIL. Its file lock alone cannot prove that an orphaned child is gone. A blocker or unverified B claim can finalize non-pass without running the full suite. If cross-domain impact nevertheless justifies a full-suite run on a non-pass round, `--escalation-reason` is required and is retained in verdict, status, and telemetry. The sealed receipt is immutable for that lifecycle, so another seal attempt is `FINAL_VALIDATION_ALREADY_SEALED`.

The Reviewer B report-entry budget is an admission rule for contract 7 and later. Historical contract-6 terminal reports remain readable even when they exceed the positive-verification caps; the budget and its telemetry fields must not be applied retroactively. The native top-level call budget starts with contract 8 and is never projected into a historical contract-7 context. Read-only historical telemetry may reauthenticate reused evidence against the stored contract and current source/environment measurements, but never grants resume, stored-validation, finalization, or PR authority under an old contract.

For reuse, assign the round-local `id` and copy the trusted verifier's `execution` object unchanged. The command is `cd -- <quoted repository-relative cwd> && <shlex-joined argv>`. Report `capture_sha256` hashes stdout bytes followed by stderr bytes; the immutable store's framed capture has a different digest. Claims cite these report execution IDs only when the inspected command scope, exit, excerpts and truncation support their result. All unique reused entries and the subset cited by claims are distinct telemetry counts.

No bundle or invalid unused evidence permits independent fresh validation. A sealed reused reference makes the selected bundle and capture bytes dependencies: changed evidence rejects stored validation, recovery, finalize and the terminal gate. Preserve exact report bytes and do not replace that sealed report with a new fallback report.

### Format failures

`JSON_INVALID`, `REPORT_TOO_LARGE`, `TEXT_INVALID`, `REPORT_SCHEMA_INVALID`, `REPORT_REVIEWER_MISMATCH`, `REPORT_ROUND_MISMATCH`, `REPORT_SNAPSHOT_MISMATCH`, and `REPORT_NOT_TERMINAL` describe rejected report content. Preserve the full original response in a controller-private current-user-owned regular non-symlink exact-`0600` file, independent of ambient `umask`; do not trim, reserialize, repair, or silently truncate it. A format-only retry stays on the same reviewer handle when that handle can accept a follow-up.

Bounded content failures also include `FINDING_SCHEMA_INVALID`, `FINDING_LIMIT_EXCEEDED`, `EXECUTION_LIMIT_EXCEEDED`, `CLAIM_LIMIT_EXCEEDED`, `CLAIM_COVERAGE_INVALID`, `REVIEW_BUDGET_EXCEEDED`, `PRIOR_DECISION_RESPONSE_MISSING`, `PRIOR_DECISION_RESPONSE_INVALID`, `REFUTED_CLAIM_REQUIRES_BLOCKER`, `REPLACEMENT_FINDING_REQUIRED`, and `REPLACEMENT_FINDING_INVALID`. Fresh submission checks the reviewer's own decision responses, refuted-claim blocker requirement, and replacement references before canonical publication and sealing. Rejected content remains pending with exact bounded failure evidence; corrected same-role submission leaves sealed peers unchanged. Finalization and persisted-verdict parsing repeat the full closure validation. Count/byte limits remain enforced, and invalid existing canonical orphans remain hard integrity failures outside fresh-input retry handling.

### Operational failures

Only true terminal `DISPATCH_FAILED`, `REVIEWER_FAILED`, and `REVIEWER_TIMEOUT` belong to `record-failure`. A wait-interface timeout with a still-running reviewer handle is not terminal: continue the same handle without recording failure, dispatching a replacement, or cleaning its view. Automatic request limits are controller-local per invocation; persisted `attempt_count` remains cumulative.

### Observation warnings

Telemetry failures and a non-force cleanup refusal for a known-terminal reviewer and verified exact controller-created view are bounded warnings. They do not alter a sealed receipt, report findings, or the gate verdict.

### Integrity stops

Uncertain process liveness, uncertain view identity, unsafe ownership/type/mode, changed report bytes or receipt digest, changed final-validation receipt/capture/environment, and changed snapshot or installed contract stop before `finalize`. Every active reviewer requires an independently revalidated sealed receipt; disabled roles never count toward a quorum. A valid HIGH or CRITICAL report seals and contributes its blocker to final aggregation rather than being replaced.

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
      "acceptance_condition": "Revalidate the target immediately before replacement.",
      "reversal_cost": "After merge, the unsafe replacement can corrupt persisted targets and require a coordinated recovery."
    }
  ],
  "executions": [],
  "claims": [],
  "prior_decisions": []
}
```

The current contract binds Reviewer B's `review_budget` to the snapshot risk floor, independently of requested intensity. Its `verified_claims` field is a compatibility name for the positive (`supported`) claim cap: `ordinary` permits 10 supported claims, 8 fresh non-blocker executions, and 32 top-level native tool calls; `high-risk` permits 16 supported claims, 12 fresh non-blocker executions, and 40 top-level native tool calls. Evidence-backed `refuted` claims and fresh executions cited by a refuted claim or CRITICAL/HIGH finding are exempt from the report-entry caps, but remain subject to the global 128-entry limits. Reused authenticated executions also do not consume the fresh cap. Exceeding either positive report-entry count is `REVIEW_BUDGET_EXCEEDED`. The reviewer selects required claims in source order but preserves known refutations and blocker evidence before positive verification. The report shape is unchanged: budget exhaustion leaves remaining unproven required claims `unverified` with reasons beginning `BUDGET_EXHAUSTED:`. Without a blocker, that yields `INCONCLUSIVE` and a blocking `VERIFICATION_INCOMPLETE` gate decision, not an execution-free finding; preserved HIGH/CRITICAL findings yield FAIL. The reviewer stops fresh work at 270/480 seconds or all tool work at 32/40 top-level calls, and returns before 300/600 seconds. The runtime enforces submitted report-entry counts only; actual tool-call count and elapsed time remain reviewer-governed and are audited offline. Telemetry's `native_tool_call_limit` records the selected ceiling but `total_command_count` remains unknown unless an external transcript audit supplies it. `verified_claim_count` includes both supported and refuted claims, so it may exceed the positive-claim cap when blockers are preserved. The `supported_claim_count` and `fresh_non_blocking_execution_count` telemetry fields are the counts directly comparable with `verified_claim_limit` and `fresh_execution_limit`; `fresh_execution_count` includes exempt blocker executions.

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
  "coverage": {
    "complete": true,
    "primary_entry_paths": [
      {"path": "scripts/example.py", "claim_id": "B-R1-C001"}
    ]
  },
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
