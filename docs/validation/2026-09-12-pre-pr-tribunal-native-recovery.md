# Native full-panel and selective recovery measurement

Date: 2026-09-12. Follow-up evidence for #115 and #111 after PR #134 merged.

One matched lockfile fixture produced **258,221 ms** for a fresh A/B/C panel
and **183,275 ms** for B-only recovery with A/C preserved: an observed reduction
of **74,946 ms (29.0%)**. Both finalizers returned PASS with zero blockers.
The four reviews used actual native model calls. The malformed input was
deliberately injected, and the selective telemetry request counts remain
unknown because the fixture seed has no native dispatch history of its own.
The observed 3-to-1 call count comes from native dispatch records, independently
of those unknown telemetry fields.

This is one workload comparison on a small fixture, not a statistical estimate
or a reproduction of the entire historical jhw-notion #125 dependency change.
Both arms use the same merged implementation; the full-panel arm represents
the work of rerunning three reviewers, not execution of a previous release.

| Condition | Value |
| --- | --- |
| Installed source commit | `3febc63d9764555c2eb83302c4f0032436379ece` |
| Fixture HEAD | `e776c44cd348f1124e36b7a90d5b49eb9e89bf55` |
| Fixture base / merge-base | `04736abddc4b916ad541aaceb89f2ab3e4e8183a` |
| Base argument | `master` |
| Named branch | `benchmark-lockfile` |
| Changed path | `package-lock.json` |
| Diff SHA-256 | `4cbba8b7a36ce211657caac8540e6bae81f23576ab0837cc150db93ae9b1ff68` |
| Contracts | report text 2; diff recipe 1; verdict schema 3; telemetry schema 3 |
| Native runtime | Codex CLI 0.154.0; `collaboration.spawn_agent`; `fork_turns="none"` |
| Actual reviewer model / effort | `gpt-6-astra` / `xhigh`, verified for every reviewer |
| Model selection | Inherited native settings; no model or effort override |
| Execution order | Full panel, then selective recovery |
| Scheduling policy | Dispatch all required independent roles when capacity permits |

The fixture changes the locked semver version from 7.6.2 to 7.6.3, including
its resolved URL and integrity, while preserving the manifest and dependency
topology. Construction read the version-specific public npm registry metadata.
Reviewers worked offline and made no source writes or package installations.
Both B reports supported the structural claims and explicitly left registry
provenance / archive authenticity unverified within that offline scope.

The package was installed in a disposable prefix. All 14 installed Python
modules matched the merged source bytes. The global installation was untouched.
Each native reviewer received its own clean detached view at the fixture HEAD,
its role/schema references, and only its projected context. All A/B/C projected
context files were byte-identical between arms. The controller supplied the
same B prompt with only the view/context paths changed; persisted dispatch
message bodies are opaque, so no independent prompt-byte hash match is claimed.
Role/schema reference hashes are retained with the evidence.

## Experiment and timings

The full arm began in an independent repository without review evidence and
dispatched A/B/C concurrently. A and C completed and sealed before B. Their
exact native responses were also submitted as the seed of the separate
selective repository, whose snapshot and contexts were identical. An additional
one-byte input, hexadecimal `7b`, was deliberately submitted as B and rejected
with `JSON_INVALID`. No native response was corrupted or repaired. This seed
invocation was closed as a failure and its preparation time was excluded from
the recovery measurement. The full arm finished before selective recovery
started. Recovery used `telemetry-resume`, created only B's view, and dispatched
one fresh B reviewer. No verdict or ledger was reset.

```text
Full control:   empty evidence -> native A + B + C -> PASS
Selective seed: native A/C evidence + injected B error -> A/C sealed, B pending
Selective run:  preserve A/C -> native B -> verify A/B/C -> PASS
```

| Measurement | Full panel | B-only recovery |
| --- | ---: | ---: |
| Native dispatch calls, observed directly | 3 | 1 |
| Native format/operational retries | 0 | 0 |
| Reused sealed roles | 0 | 2 |
| Invocation elapsed, ms | 258221 | 183275 |
| Sum of controller reviewer spans, ms | 451983 | 180530 |
| Sum of native task durations, ms | 356952 | 145956 |
| Final verdict | PASS | PASS |
| Blocking findings | 0 | 0 |

Invocation elapsed uses the installed CLI's real monotonic entry-to-close
measurement, including view work, dispatch, controller scheduling, validation,
and finalization. Overlapping reviewer durations must not be added to estimate
wall-clock latency. No clock was injected.

| Native reviewer | Native task duration, ms | Controller dispatch-to-terminal span, ms |
| --- | ---: | ---: |
| Full A | 122237 | 147342 |
| Full B | 192215 | 229380 |
| Full C | 42500 | 75261 |
| Selective B | 145956 | 180530 |

Native task durations come from each reviewer's supported runtime completion
record. Its final message was byte-identical to the controller's preserved
terminal response. These durations include the reviewer's reasoning and tools;
they are not isolated inference or CPU time. A separate dispatch acceptance
timing signal was not used: each dispatch span remains `incomplete` with
`RUNTIME_SIGNAL_UNAVAILABLE`, retaining its measured duration. The reviewer
total spans include the controller's terminal-response handling delay.

The full panel was dispatched in A/B/C order with ordinary controller handoff
gaps. B's native duration itself differed by 46,259 ms between arms. Model
variation, dispatch order, cache effects, and concurrent host activity were
not controlled statistically. Therefore neither the observed 29.0% elapsed
reduction nor the 59.1% reduction in summed native task duration establishes a
general speedup. The directly observed call reduction is 66.7% for this recovery
shape. No earlier benchmark duration was substituted into this comparison.

## Telemetry observation gap

Full-panel request accounting is complete. The synthetic selective seed stores
real A/C report bytes and the injected B fault without corresponding dispatch
spans in that repository. Its history therefore correctly fails the prior
request-history completeness check. Recovery retains
`PRIOR_REQUEST_HISTORY_INCOMPLETE` and does not infer missing request counts.

| Recovery summary field | Full panel | Selective |
| --- | --- | --- |
| `kind` | `new_round` | `resume` |
| `reused_slot_count` | 0 | 2 |
| `requested_slot_count` | 3 | null |
| `rerun_slot_count` | 0 | null |
| `dispatch_request_count` | 3 | null |
| `retry_request_count` | 0 | null |
| `accounting_complete` | true | false |
| `telemetry_incomplete` | false | true |
| `invocation_elapsed_ms` | 258221 | 183275 |
| Clock anomaly reason codes | empty | empty |

The native dispatch records independently establish three calls versus one
and zero native retries; these facts do not repair or replace the ledger's
nulls. All spans and invocations are terminal. Both elapsed durations remain
available without a clock anomaly. This experiment supplies native timing
evidence, but does not claim a complete-history native recovery telemetry test.

## Validation and retained evidence

Every original native response was saved without trimming or reserialization
to a controller-owned non-symlink regular file, explicitly set to mode `0600`.
Each input SHA-256 matched its submission receipt and immediate stored
validation. Immediately before each finalizer, all three stored reports were
independently checked again for type, current ownership, exact mode, bytes,
receipt digest, snapshot, and contract. Both finalizers passed only after all
three slots were sealed. The selective A/C bytes and complete receipts remained
identical to their seed values. B's cumulative attempt count became 2, comprising
one injected malformed submission and one actual successful native response.

All four reviewer views were removed once, without force, after their reviewers
were terminal. No supported explicit handle-release tool was exposed; this was
recorded as a runtime limitation. The fourth fresh native dispatch was accepted
without a capacity rejection. No unrelated handle or runtime state was changed.

A pre-measurement `begin --base origin/master` was rejected with `BASE_INVALID`
before any verdict or model dispatch. Its terminal failure record is preserved.
Using the CLI's correct `--base master` argument began the measured full run;
the failed preflight duration was not included.

The task worktree and main checkout each retain 57 exact evidence files plus a
checksum manifest at `.review/benchmarks/2026-09-12-native-latency/`. The main
checkout copy preserves the evidence when the task worktree is later removed.
All copied files are regular,
current-user-owned, and exact mode `0600`. They include the fixture Git bundle,
four private native reports, the separate injected fault, both verdicts and
telemetry ledgers, submission receipts and pre-final checks, projected contexts,
installed modules/references, native timing metadata, and controller scripts.
The evidence manifest SHA-256 is
`20896a59f5d4bd3d39f78eb45a54e5e39b96a700c22b89d8b915c39d38a65d72`.

Production code was unchanged during measurement. No full test suite was rerun
for this evidence-only follow-up; validation exercised the installed CLI,
native reports, exact-byte comparisons, finalizers, and artifact checks above.

## Acceptance evidence for #115

A subsequent completion audit ran 18 selected test functions and their
parameterized cases against implementation HEAD
`b0b1f56aa02651df50a54e12d01bd25645d3b6aa`: **55 passed in 23.91s**.
The selection covers the conditions below, the installed full/selective
comparison for both runtime contracts, and rejected-submission early detection.
The exact pytest argv and outputs are retained separately in
`.review/benchmarks/2026-09-12-native-latency-closeout/verification.json`,
`verification.stdout`, and `verification.stderr`.

| Issue condition | Evidence | Interpretation |
| --- | --- | --- |
| A/C valid, B malformed: request B only | Native comparison above; `test_installed_full_panel_and_selective_recovery_comparison` | A/C receipts preserved; one native B recovery request |
| Reject snapshot drift | `test_submit_and_reviewer_failure_drift_preserve_all_bytes` | Changed snapshot rejected without rewriting evidence |
| Reject versioned contract drift | Same test; `test_context_rejects_installed_contract_drift` | Installed report/context contract versions are authoritative |
| Detect report/digest tampering | `test_finalize_authenticates_every_sealed_receipt` | Bytes, owner, mode, symlink, context, parsed content, and contract cases |
| Recover interrupted publication | `test_submit_report_published_before_verdict_failure_is_sealed_without_replacement` | Original canonical bytes survive a failed verdict write and retry |
| Preserve peer isolation | `test_sealing_peer_does_not_change_pending_reviewer_context`; role-context Skill tests | Operational context/view isolation; no OS sandbox guarantee |
| Block incomplete panel | `test_v2_round_not_ready_and_successful_terminal_roundtrip`; `test_in_progress_review_is_incomplete` | Two sealed roles cannot finalize or satisfy the PR gate |
| Preserve blockers and round limit | `test_v2_terminal_aggregates_every_sealed_report`; gate blocker/round-three tests | HIGH/CRITICAL aggregation and three-round exhaustion remain enforced |
| Measure recovery time with #111 telemetry | This native comparison; `test_resume_records_reuse_and_local_retry_without_rewriting_receipts`; early-detection test | Actual elapsed comparison and direct call counts are distinct from the native seed's unknown telemetry accounting |

Versioned contract checks do not establish automatic invalidation for arbitrary
unversioned edits to role-prompt Markdown. The installed contract and explicit
context projection define the runtime boundary; the measurement additionally
retains reference-file hashes. Likewise, the lockfile fixture and its injected
format error establish the measured recovery shape with the limits stated
above, rather than a complete replay of the historical #125 repository.

PR #134's merged state and successful Pytest/reviewer checks were re-read during
this audit. The report is the remaining repository artifact to integrate;
tracker closure and Task release are subsequent explicit workflow actions.
