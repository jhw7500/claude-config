# Pre-PR tribunal selective slot recovery validation

Date: 2026-09-10

## Revisions and safety boundary

- Original master base: `df1285dbb7655717af3d60c7fc098715a29203dd`
- Prior full-suite implementation HEAD: `e37694d39fcf50233d8f6437d236a150b70401fe`
- Receipt-binding fix HEAD: `520e505295be7e96cb92057dcd339dd78a5fd4a2`
- Final reviewed and full-suite-tested implementation HEAD: `a5d045e8f26d7c7fff6aca7ba5b834ad3e81840e`
- The root controller rechecked `e37694d39fcf50233d8f6437d236a150b70401fe` unchanged and the tracked worktree clean immediately after that full run. The 3517-test result below does not prove the later receipt-binding fix.
- Evidence is committed separately after testing; the evidence commit itself is not represented as tested implementation code.
- All installed canaries used pytest-created temporary homes and repositories with the probe's masked environment and process containment.
- The user's real installed runtime, runtime configuration, credentials, and #109 `.review` state were untouched. No install, PR, push, claim, or remote reviewer operation was performed.
- Evidence is sanitized: it contains no credentials, absolute home paths, raw reviewer reports, or telemetry bodies.

## Final reviewed implementation verification

The root controller verified the exact HEAD `a5d045e8f26d7c7fff6aca7ba5b834ad3e81840e` before and after the final full run, with clean post-run status. All results in this section apply to that frozen implementation; this later evidence-only document commit is not represented as the tested code SHA.

| Command | Result |
| --- | --- |
| `rtk proxy .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q --junitxml=.superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/final-reviewed-pytest.xml` | 3543 passed in 429.34s (0:07:09); exit 0; JUnit errors 0, failures 0, skipped 0 |
| `rtk proxy .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m py_compile hooks/pre_pr_tribunal/*.py scripts/install-pre-pr-tribunal.py scripts/probe-pre-pr-tribunal.py` | Exit 0 |
| `rtk git diff --check df1285dbb7655717af3d60c7fc098715a29203dd HEAD` | Exit 0 |
| `rtk git merge-base --is-ancestor fae27ef988f199eccc777022f3252f3872719c8a HEAD` | Exit 0 |

The same full-run JUnit artifact includes 1460 `tests.pre_pr_tribunal` cases (summed testcase time 286.839s) and 16 `tests.runtime_hook_installer` cases (summed testcase time 0.417s). These are included-group counts and testcase-time sums, not additional test executions or wall-clock durations.

The final broad source review at `5a60252` returned two Important findings and one Minor finding. The one batched fix at `a5d045e` received scoped re-review approval: all three findings addressed, no new breakage or deferred items. This implementation review is not an actual pre-PR tribunal verdict.

| Finding | Final behavior |
| --- | --- |
| Missing pre-seal reviewer-local decision closure | Submission validates the role's responses and replacement references before publication/sealing. Corrected fresh content can retry that pending role; sealed peers stay intact. Finalization retains full closure revalidation. Invalid existing canonical orphans remain hard integrity stops outside fresh-input retry handling. |
| Incomplete report-content allowlist | Finding schema, bounded finding/execution/claim count limits, missing decision responses, and invalid replacement references enter the exact bounded evidence and same-role retry path. Legacy migration preserves invalid content and leaves every role pending. Byte/count limits and state/storage/snapshot/contract/ownership/digest integrity checks remain enforced. |
| Replacement/resume view bookkeeping | Both allocation paths explicitly repeat step 5's immediate `CREATED_VIEWS` registration, bound-HEAD/clean/absent-`.review` checks before dispatch, and started-handle tracking. |

## Final batched fix RED/GREEN evidence

The initial selected RED run had 22 failures and 6 passes. Six native content cases first failed in fixture setup because the HIGH B peer lacked required execution evidence; those setup failures are not product RED. After correcting the fixture, all six independently failed because the pending failure count remained 0. The other 16 original failures directly reproduced missing pre-seal validation, legacy migration aborts, and installed C retry aborts.

| Phase | Command | Result |
| --- | --- | --- |
| Initial RED | `rtk proxy .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_model_store.py tests/pre_pr_tribunal/test_probe_harness.py -k 'bounded_content_errors or only_originating_reviewer or orphan_content_error or valid_peers_when_c_retries' --tb=short` | 22 failed, 6 passed, 465 deselected in 22.85s; six setup failures qualified above |
| Native content RED after fixture correction | `rtk proxy .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'bounded_content_errors and False' --tb=short` | 6 failed, 360 deselected in 1.11s; actual cumulative attempt count 0 instead of 1 |
| Combined GREEN | Same command as initial RED, after the fix | 28 passed, 465 deselected in 27.91s |
| Existing installed integrity/recovery canaries | `rtk proxy .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_probe_harness.py -k 'submit_receipt_status_inconsistency or resumes_only_pending or migrates_unproven or seals_blocker or rechecks_each_report or telemetry_failure or contract_drift' --tb=short` | 11 passed, 116 deselected in 30.11s |
| Covering suites | `rtk proxy .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_model_store.py tests/pre_pr_tribunal/test_attempt_store.py tests/pre_pr_tribunal/test_review_context.py tests/pre_pr_tribunal/test_skill_contract.py --tb=short` | 439 passed, 1 failed in 66.17s; the only failure was a case-sensitive documentation marker |
| Covering documentation correction | `rtk proxy .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_skill_contract.py --tb=short` | 30 passed in 2.64s after restoring `Then dispatch` |

The full 3543-pass result above covers the final documentation correction and all runtime/test changes. Fresh malformed reports remain pending with exact evidence; corrected same-role submission succeeds at cumulative attempt 2. Existing orphan content/closure failures preserve canonical bytes, verdict, and counters without creating fresh failure evidence. The installed canaries dispatch A/B once and retry only C for malformed finding schemas and all three bounded count failures.

## Historical focused verification

| Command | Stable result |
| --- | --- |
| `rtk .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_probe_harness.py -k 'submits_validates or preserves_valid_peers or resumes_only_pending or migrates_unproven or seals_blocker or rechecks_each_report or telemetry_failure or contract_drift'` | 12 passed, 108 deselected in 31.61s |
| `rtk .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_installer.py tests/pre_pr_tribunal/test_install_integration.py` | 86 passed in 1.87s |
| `rtk .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_probe_harness.py` | 120 passed in 128.12s |
| `rtk .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_probe_harness.py -k 'resumes_only_pending_c_and_reuses_native_sealed_peers'` | 1 passed, 119 deselected in 3.83s |

## Historical full-suite verification

These earlier full-run results apply specifically to the pre-fix implementation HEAD `e37694d39fcf50233d8f6437d236a150b70401fe`. They are historical evidence, not proof of the later closure/content-policy fix; final reviewed-code evidence appears above.

| Command | Result |
| --- | --- |
| `rtk proxy .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q --junitxml=.superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/final-pytest.xml` | 3517 passed in 351.15s (0:05:51); exit 0; JUnit errors 0, failures 0, skipped 0 |
| `rtk proxy .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m py_compile hooks/pre_pr_tribunal/*.py scripts/install-pre-pr-tribunal.py scripts/probe-pre-pr-tribunal.py` | Exit 0 |
| `rtk git diff --check df1285dbb7655717af3d60c7fc098715a29203dd HEAD` | Exit 0; no whitespace errors |
| `rtk git merge-base --is-ancestor fae27ef988f199eccc777022f3252f3872719c8a HEAD` | Exit 0; #109 dependency is present in ancestry |

The full repository run is also the final coverage of `tests/pre_pr_tribunal` and `tests/runtime_hook_installer`; it was intentionally not duplicated as an immediately preceding aggregate directory run.

The JUnit artifact from that single full run contains 1434 `tests.pre_pr_tribunal` cases (summed testcase time 228.025s) and 16 `tests.runtime_hook_installer` cases (summed testcase time 0.433s). These are included-group counts and testcase-time sums, not separate executions or wall-clock durations.

The subsequent root run at unchanged `5a6025265675f2c6bdfc907d8096f7e901fb92b1` passed 3520 tests in 391.11s (0:06:31), exit 0, with JUnit errors/failures/skips all 0. Its command was `rtk proxy .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q --junitxml=.superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/final-fix1-pytest.xml`. This also predates the final closure/content-policy correction and is not relabeled as its validation.

## Receipt-binding fix verification

| Phase | Command | Result |
| --- | --- | --- |
| RED: digest and numeric attempt divergence | `rtk .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_probe_harness.py -k 'submit_receipt_status_inconsistency'` | 2 failed, 120 deselected in 5.63s; both incorrectly finalized before the fix |
| RED: boolean attempt divergence | `rtk .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_probe_harness.py -k 'submit_receipt_status_inconsistency and boolean_attempt'` | 1 failed, 122 deselected in 5.28s; `True` incorrectly matched attempt 1 before strict typing |
| GREEN at `520e505295be7e96cb92057dcd339dd78a5fd4a2` | `rtk .superpowers/sdd/2026-09-10-pre-pr-tribunal-slot-recovery/test-venv/bin/python -m pytest -q tests/pre_pr_tribunal/test_probe_harness.py -k 'submit_receipt_status_inconsistency or submits_validates_and_finalizes_exact_reports or preserves_valid_peers_when_c_retries or resumes_only_pending_c_and_reuses_native_sealed_peers'` | 8 passed, 115 deselected in 25.78s |

The fix retains each new transient submit receipt, requires a strict positive non-boolean integer attempt, compares its digest and attempt directly with final status, and validates stored bytes against that same retained digest. It does not change the installed runtime API.

## Installed canary outcomes

| Case | Outcome |
| --- | --- |
| Fresh native lifecycle under umasks `000`, `022`, and `077` | A/B/C exact bytes submitted and sealed mode `0600`; stored digests matched persisted receipts; authenticated pathless finalize returned PASS. |
| C format failure | A/B stayed sealed at attempt 1; only C regenerated; C sealed at cumulative attempt 2; final status PASS. |
| Invocation-local exhaustion and resume | First invocation attempted C exactly three times and preserved sealed A/B bytes; next invocation dispatched only C at cumulative attempt 4 and returned PASS. |
| Legacy v1 pending A/B canonical reports | Migration preserved exact A/B evidence with `LEGACY_PROVENANCE_UNAVAILABLE`; no legacy bytes were adopted as sealed; A/B/C were regenerated at attempts 2/2/1 and returned PASS. |
| A HIGH blocker | A remained sealed, all three slots completed, and final status was FAIL with blocking count 1. |
| Sealed byte, mode, or symlink tamper after stored validation | Installed authenticated finalize rejected each case with stable `REPORT_BYTES_MISMATCH` or `FILE_UNSAFE`; verdict stayed in progress. |
| Corrupt telemetry ledger | Primary native lifecycle still returned PASS; bounded warning projection reported `TELEMETRY_INVALID`; corrupt telemetry body was not emitted. |
| Installed contract mismatch | The next pending-slot context operation returned stable `CONTRACT_DRIFT`; no false success was claimed. |
| Submit receipt/status inconsistency | Digest, numeric-attempt, and boolean-attempt divergence returned stable `REPORT_RECEIPT_MISMATCH` before finalize at fix HEAD `520e505295be7e96cb92057dcd339dd78a5fd4a2`. |
| Deterministic package rollout | Every planned and installed Python source digest matched its source digest; `attempt_store.py` and `review_context.py` were explicitly present. |

## Task 7 consuming-agent behavior evidence

The fresh guide consumer at guide commit `26ca78c` was given only the current guide and independent simulated scenarios. This is a bounded behavior check, not a statistical reliability or wording-proof claim.

| Case | Observed outcome |
| --- | --- |
| Legacy/native selection | Read bounded status first, migrated all-slot v1 only, never inferred sealing from valid files, and selected only resulting pending roles. |
| Sealed A/B plus pending C | Reused A/B including A's HIGH, dispatched only C, required all three sealed, and preserved the blocker. |
| Cleanup and telemetry warnings | Kept known-terminal exact-target cleanup refusal and telemetry errors as warnings, did not rerun reviewers, and continued to integrity checks/finalize. |
| Digest tamper | Stopped before finalize without replacing or rerunning the sealed slot. |
| Wait-interface expiry | Continued waiting on the same live C handle; did not record failure, dispatch a duplicate, clean up, or finalize 2/3. |

## Controller ruling ledger

Each ruling below retains the controller's reason and explicit cost/tradeoff so the decisions survive scratch cleanup.

1. Ruling: Preserve strict receipt attempt/count consistency and set attempt_count=1 in sealed fixtures — the sample omission cannot override the persisted counter contract — wrong choice would require fixture/schema rework.
2. Ruling: Keep v1 context projection compatibility until Task 5 switches production writers; use strict v2 envelope for v2 only — maintains the plan's green intermediate commits — wrong choice would require transitional API rework.
3. Ruling: Convert legacy compatibility test setup to explicit authentic v1 fixtures when the writer switches, and adapt normal lifecycle helpers to submit v2 reports — final v2 behavior and v1 read compatibility are both required — wrong choice could mask a regression, so preserve existing behavioral assertions.
4. Ruling: Use consuming-agent pressure cases for changed skill behavior in addition to existing repository contract checks, without adding tests justified only by exact prose wording — the spec requires behavioral recovery evidence — wrong choice would require additional workflow evidence.
5. Ruling: Use the declared three-argument synthetic-report factory signature consistently in Task 8 fixtures — sample call is inconsistent with its own interface — wrong choice would require probe fixture adjustment.
6. Ruling: Partition Task 1 baseline into the focused directories and then the repository remainder with --ignore for those directories — the user's no-duplicate-check preference is satisfied while collecting the full suite — wrong choice could hide a cross-suite interaction, which the final full-suite run will still cover.
7. Ruling: Replace the plan's empty isolation fixture with mixed A/B/C prior history/decisions and exact C-only expectations — empty all(...) assertions cannot detect filter removal — wrong choice costs extra fixture maintenance; core peer-isolation coverage justifies it.
8. Ruling: Preserve exact failure input through MAX_REPORT_BYTES+1 (the CLI sentinel-inclusive read bound); reject larger direct API input with REPORT_TOO_LARGE without evidence/counter mutation. Never truncate evidence and call it exact — wrong choice would lose oversized direct-call evidence, but prevents unbounded storage and misleading provenance.
9. Ruling: Persisted attempt_count and receipt.attempt are cumulative strict integers, not capped at 3. The approved plan caps controller-local attempts per invocation at 3, and separately retains only 3 failed evidence records. Remove the erroneous schema cap and test a successful submission after three prior failures — wrong choice would require a future numeric/storage bound, but a lifetime cap would permanently block supported recovery.
10. Ruling: Hold verified file descriptors and recheck named inode/type/owner/mode immediately before rotation unlink; stop on substitution or unsafe/incomplete records. POSIX does not offer atomic unlink-by-inode against hostile same-UID substitution — wrong choice leaves a residual same-UID race rather than claiming an unsupported guarantee.
11. Ruling: A legally accepted new round initializes its reused evidence namespace by prevalidating all known complete target-round ABC records, then descriptor-check unlinking only those files under the existing begin lock; retain directories and refuse unknown/partial/unsafe targets. Pending resumes never call this reset. If no stored verdict exists but unexpected nonempty evidence does, stop rather than infer completion. This retains new-round count0/receipt1 semantics across cycles without new durable counters — wrong choice removes old completed-round failure evidence on an explicit new begin, but avoids unbounded archives, unverifiable cumulative counts, or permanent sequence collision. No live user review state is cleaned by this implementation task.
12. Ruling: Move the direct store_reviewer_report v1-only guard forward from Task6 into Task5's v2 writer switch. Both ordinary store and --replace-pending-recovery must return LEGACY_COMMAND_NOT_ALLOWED before v2 canonical mutation; Task6 retains public CLI coverage. The old replacement API could otherwise replace a valid native orphan before sealing — wrong choice moves a small API fence one commit early and requires explicit v1 fixtures, while preserving the already-approved final behavior.
13. Ruling: Legacy migration publishes v2 only after all safe evidence is preserved and canonical legacy files are descriptor-checked/removed. Add a narrow idempotent migration attempt1 helper: reuse only complete exact round/reviewer/digest/reason and safe-metadata evidence, recovering pending counters/reasons on retry after canonical removal. Never infer sealing authority or repair incomplete records. Publishing v2 first would expose unproven legacy canonical files to native orphan sealing — wrong choice adds narrow crash-recovery code, but prevents provenance laundering or lost bytes on a failed verdict write.
14. Ruling: Task5 must adapt gate.py alongside the v2 writer switch. Root's focused downstream-consumer inspection found gate.py requires slot.status == complete, which would reject every legitimate v2 sealed terminal verdict. Select the completed slot state by verdict schema, preserve v1 terminal acceptance, and reject mismatched installed contract on v2 as VERDICT_STALE. Add hook adapter coverage for native v2 PASS/FAIL and contract drift — wrong choice widens this task by a small compatibility bridge, but leaving the named consumer unchanged would ship a permanently blocking gate. Do not broaden this to a new post-finalization raw-file authentication policy.
15. Ruling: Historical v1 telemetry success spans cannot alone authorize adoption of current report bytes — TelemetrySpan carries no raw/context digest, and v1 store permits report replacement after those observations; the spec requires equivalent provenance — existing v1 reports remain safely preserved pending with a bounded provenance-unavailable reason unless a genuinely supported digest-bound proof is found. New v2 sealed-peer reuse remains mandatory. Wrong choice would forgo recoverable legacy work, but accepting these spans as byte proof would permit report substitution. Task 5/8 tests and operator docs must reflect the actual supported provenance, not synthesize an imaginary legacy receipt.
16. Ruling: On switching to v2 writers, begin cannot reset an existing in-progress round (especially mixed slots); require the supported resume/migration path — otherwise begin would discard sealed findings outside submit's protections — wrong choice may require an explicit future abandon/reset API, but must not silently discard accepted results.
17. Ruling: Keep record-failure success exactly state/attempt_count/last_error, and submit-report uses receipt field attempt plus the brief's reviewer/round identity. The controller already knows the requested role from the correlated call — wrong choice would require additive identity fields later, but avoids inventing output beyond the specified bounded projections.
18. Ruling: Add a bounded verdict_schema discriminator to status (1 or2), alongside native v2 slot projections. The planned status -> optional migration controller cannot distinguish an all-pending legacy verdict from fresh native v2 from the old four-field status. Preserve the snapshot/report schema1 domain; do not blindly change begin's envelope schema. Legacy status may retain its bounded legacy projection plus the discriminator without fabricating sealed receipts — wrong choice adds one stable output field, but avoids controller raw-state reads or guessing migration necessity.
19. Ruling: Honor the user's explicit no-duplicate-check preference by using the already captured old-guide pressure baseline and one fresh post-edit scenario consumer, plus source-contract checks, rather than a five-repetition wording microbenchmark. This tests the actual workflow defect without re-running completed work; do not claim statistical reliability or bulletproof wording — wrong choice provides less variance evidence, transparently bounded to the tested scenarios.
20. Ruling: A wait-tool timeout alone is not proof of terminal reviewer failure. Task7 must keep waiting on the same live handle or verify a supported terminal outcome before recording timeout/replacing/cleaning up; unknown process or view identity remains an integrity stop. The spec explicitly refuses warning downgrade for uncertain reviewer liveness — wrong choice may prolong a wait, but treating an observation timeout as death creates duplicate reviewers and cleanup races.
21. Ruling: At Task8, commit tested implementation before the validation-evidence document, then commit evidence separately. The document names the exact tested code HEAD/base, not an impossible self-referential final documentation commit — wrong choice adds one small evidence commit, but avoids claiming an untested or circular SHA as the tested implementation.
22. Ruling: Root will execute the final full repository suite once at Task8's frozen implementation HEAD; this run also covers complete tribunal/runtime-hook suites, with directory counts from its result artifact. Do not separately repeat both full focused directories immediately before the full suite. The implementer still runs new/changed probe/installer tests for GREEN — wrong choice may require richer result grouping, but avoids the user's explicitly unwanted redundant broad checks.
23. Ruling: Task8 may add only the root-anchored /.ai/handoff.md ignore entry for the already-identified generated private Project Control sidecar. Preserve its bytes, do not ignore the directory broadly or weaken dirty-worktree checks — wrong choice hides that one local path from default git status, but avoids discarding/publishing private handoff data or permanently blocking review on generated context.
24. Ruling: Validate each fresh report's reviewer-local decision responses and replacement references before canonical publication and irreversible sealing, then retain the full finalization closure recheck. Missing/invalid reviewer-local closure is bounded report-content failure, not a reason to seal an unrecoverable slot; existing orphan validation stays outside fresh-input retry handling — the original plan kept terminal validation without adapting the earlier acceptance boundary — wrong choice adds local validation and fixture changes, but prevents a permanently unfinalizable round without permitting sealed-report replacement or peer exposure.
25. Ruling: Include finding-schema and bounded finding/execution/claim count-limit failures, plus missing decision responses and invalid replacement references, in the explicit report-content retry/evidence policy across runtime, probe and guide. Keep byte/count limits enforced and storage/state/contract/snapshot/ownership/digest integrity failures hard — the sample allowlist and exclusion fixture contradicted the broader malformed-report recovery contract — wrong choice broadens which rejected bounded input gets retained and retried, but evidence remains size/retention limited and valid blocker reports are never replaced.

## Limitations and next gates

- The canaries use deterministic synthetic reports; they validate installed storage, state transitions, contract binding, authentication, and warning behavior, not reviewer-model quality.
- The Task 7 consumer check covers one fresh post-edit response across five scenarios and is not a statistical reliability claim.
- Historical results apply only to their named commits. The final reviewed implementation `a5d045e8f26d7c7fff6aca7ba5b834ad3e81840e` has the fresh 3543-test full result above; the later evidence-only commit is not a circular tested-code claim.
- This validation does not recover #109, install the branch, create or merge a PR, or assert that an actual pre-PR tribunal has passed. Those remain separate review and authorization gates.
