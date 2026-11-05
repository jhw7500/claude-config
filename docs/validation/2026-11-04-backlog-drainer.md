# Backlog drainer v3 validation — 2026-11-04

## Tribunal round-1 follow-up — 2026-11-05

This follow-up supersedes the current-readiness statements in the historical sections below. Those earlier measurements describe the pre-Tribunal source and do not verify these fixes. The finalized round-1 verdict remains FAIL until a separately approved re-review acknowledges the changes; this document does not turn the old verdict into PASS.

- **A-R1-001:** release now requires non-null parent/result coordinates, the exact source manifest and commit binding in authenticated evidence, matching snapshot counts/digests, and a fresh branch/worktree inspection plus `bind_commit` before removing the active admission. Invalid evidence, a changed source/HEAD/branch, and JSON boolean/integer substitutions preserve admission.
- **A-R1-002:** storage validates the exact absolute canonical root-level run slot and preserves run ID, supervisor identity, all binding fields, branch and frozen base HEAD across CAS. Changed identity or path cannot replace the old bytes. A normal COMMIT-to-READY update and separately created successor files remain supported.
- **A-R1-003:** collection and final binding reject assume-unchanged/skip-worktree index flags and compare the visible index before/after collection. No index flags or source bytes are changed by rejection.

The corrected boundaries were first exercised by failing regressions; the raw RED traces are preserved privately. Current support claims use the fresh, reproducible commands below. The final focused command is:

```text
rtk proxy env -u LD_LIBRARY_PATH "$HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python" -m pytest -q tests/test_backlog_admission.py tests/test_backlog_checkpoint.py tests/test_backlog_cli.py tests/test_backlog_git_evidence.py tests/test_backlog_install.py tests/test_backlog_queue.py
201 passed in 9.28s
```

Ruff F/E9 and in-memory compilation of all 15 backlog Python files pass; `git diff --check` passes. The existing global Ruff executable was used because the test virtual environment has no Ruff module. No dependency was installed.

A pure reference-skill application control using the old protocol omitted `source_manifest` and `commit_binding` and found no explicit immutable CAS fields. A fresh consumer of the updated reference produced all three required evidence fields and all 12 immutable identity fields. These are reference-application checks, not native interruption/recovery proof.

Final full-suite result: PASS, exit 0; `5411 passed in 230.77s (0:03:50)`, driver elapsed 230.94 seconds.

```text
rtk proxy env -u LD_LIBRARY_PATH "$HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python" -m pytest -q -n 8
```

The command ran from the implementation worktree on parent HEAD `3c09cde6d380900b8202e9a89968817c4e036e71` plus tested working-change digest `04ccbb465cb7e2d95145a7919656fef1a1d5d11106c3ae33e13a8aeaab7eee97`. All 18 frozen execution/test/install/guidance hashes matched after the command. The raw combined log is 6156 bytes, SHA-256 `7e53707baf6f84034ec034c716030276780e680a6710e896ba1622045df7cb44`. The private input, result and log are `.superpowers/sdd/2026-11-04-backlog-drainer/review-fix-r1/full-suite-{input.json,result.json,log}`. Only this result paragraph and historical-scope wording changed after the suite; executable, test and guidance bytes remained frozen.

Native C3 remains NOT PASS: no new native experiment was run, and the independently observable child-owned tool-terminal receipt / Claude bridge is still unavailable on the observed surface. The previous full-suite result is historical; the follow-up full-suite command and source hashes will be recorded separately. No actual-HOME deployment, push, PR, merge, Task finish or Claim lifecycle mutation is performed here.

## Historical pre-Tribunal validation (2026-11-04)

Validated base HEAD: `15c0ab85d5b6c216505a68f5e2513499e78951f1`
Validated working-change digest: `ea5616dc79c56e60bc88910347f7ab43ab57e9043fe89df8351b7d39d190a088`
Issue: `https://github.com/jhw7500/claude-config/issues/31`

## Outcome and claim boundary

The final working source is validated by targeted suites, a post-residual whole-project suite, real temporary Git repositories, owned process-crash fixtures, temporary-HOME discovery, and an isolated existing-hook coexistence experiment. The parent drained the final whole-project command with exit 0: `5370 passed in 225.12s`, driver elapsed 225.27 seconds, and the raw log is 6076 bytes. The result binds to working-change digest `ea5616dc79c56e60bc88910347f7ab43ab57e9043fe89df8351b7d39d190a088` on base HEAD `15c0ab85d5b6c216505a68f5e2513499e78951f1`; all 16 execution/test hashes and both guidance hashes matched the frozen input after execution. This is a working-source attestation, not a bare-HEAD-only claim.

The final-fix working tree addresses the whole-branch I1/I2/M1/M2 findings: every validated private hierarchy entry, whether newly created or already visible, is synced child-before-parent before dependent publication; new runs require a clean empty-allowlist Git baseline while exact existing-run resume keeps owned-edit reconciliation; malformed manifest `change`/`mode` values produce bounded errors; and a representative 1000-source/200-test/100-review evidence fixture round-trips within the declared budgets. The focused six-file backlog suite is fresh for the residual bytes; no new native experiment was run and native C3 remains unsupported/`PAUSED`.

Native recovery is **not a full PASS**. Codex reached an actual pre-edit running barrier and was interrupted, but the parent could not independently observe the child-owned pending tool's terminal result. The run therefore stopped `PAUSED`; no replacement or post-edit case ran. The current Codex surface exposed no Claude native stop/terminal/non-fork bridge, so Claude is unsupported on this surface. Neither finding is a global claim about every Claude TUI or SDK.

This validation does not claim an end-to-end production backlog run, automatic Task/Claim lifecycle mutation, external release action, merge, Task finish, next-issue transition, or completion of the original broad unattended-backlog goal.

## Evidence layers and commands

Evidence is classified as follows: **ACTUAL** is a direct current-root Git/hook/native receipt; **TEST** is automated component or owned-fixture evidence; **ADVICE** defines a threshold but is not proof; **UNTESTED** identifies a real gap.

### Final post-residual whole-project suite

```text
env -u LD_LIBRARY_PATH /home/jhw/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python -m pytest -q -n 8
5370 passed in 225.12s (0:03:45)
```

The parent independently drained the command with exit 0. Driver elapsed time was 225.27 seconds. The private log was 6076 bytes. Structured result: `.superpowers/sdd/2026-11-04-backlog-drainer/residual-i1-full-pytest-result.json`; frozen input and source hashes: `.superpowers/sdd/2026-11-04-backlog-drainer/residual-i1-full-suite-input.json`. The result records `execution_sources_unchanged:true` and the exact working-change digest above.

Historical only: production HEAD plus the earlier integration state returned `5355 passed in 197.65s`; the first post-fix working source returned `5368 passed in 200.64s`. Both predate the residual existing-directory I1 correction and are not the final proof.

### Targeted and static evidence

- Final-fix backlog target: `158 passed in 3.67s` across all six `tests/test_backlog_*.py` files.
- Final completion-gate rerun of the same six files: `158 passed in 5.43s`.
- Residual-I1 completion target across the same six files: `160 passed in 5.14s`; the two new real-filesystem regressions separately returned `2 passed in 0.05s` after first producing `2 failed in 0.08s` on the skipped existing-directory branch.
- Final residual completion-gate rerun of the same six files: `160 passed in 5.35s`.
- Scoped residual review `.superpowers/sdd/2026-11-04-backlog-drainer/residual-i1-review-r1.md` marks I1 addressed, I2/M1/M2 unchanged and addressed, all four findings clear, and no new residual finding; reviewed storage SHA-256 is `be5a4a67f64e5d580dabe5bd696b99fd155bb7e8cf19db4d74638e1d52ffb761`.
- Direct boundary check with a current-user mode-0700 state root immediately below root-owned `/tmp`: evidence write/read passed. The helper did not require the external parent to share the state-root UID; private child UID/mode/no-follow validation remained active.
- Final-fix focused RED named eight failures for missing hierarchy fsync behavior and malformed manifest error normalization; after the minimal fixes, the same selected cases returned `13 passed in 0.77s`.
- Final-fix Ruff on both changed production modules and four directly changed test modules: `All checks passed!`; `git diff --check` also passed.
- Backlog target: `145 passed in 4.27s`.
- Git evidence target: `36 passed in 3.58s` using temporary real Git repositories.
- Queue target: `21 passed in 0.31s` using a local fake command, not live network.
- Relevant temporary-HOME installer target: `153 passed in 17.24s`.
- Before the EOF trim, Ruff F/E9, 15-file in-memory compile, `bash -n`, and ShellCheck `-x` passed.
- Whole-branch diff inspection found only one extra final blank line in `tests/test_backlog_admission.py`. The frozen before file had SHA-256 `fe19823dbd30d42d9dc63a6681e1f18254c643886a9455221da3016c7073f6f5` and size 19425. The edited file is exactly its first 19424 bytes, ends in one LF, and has SHA-256 `91c772af161487550fbbd5d9953a4010613ecc2e68765b59af78c3056d11962d`.
- After that one-byte, semantics-free trim: `pytest -q tests/test_backlog_admission.py` returned `20 passed in 0.24s`. The whole suite was not repeated solely for an EOF-only change.

### Native and hook evidence separation

- **ACTUAL native:** `.superpowers/sdd/2026-11-04-backlog-drainer/native-cases/pre/context.json` and `snapshot-paused.md`.
- **ACTUAL hook fixture:** `task-6-precompact-coexistence.json`.
- **FAILED SETUP, not native proof:** `native-cases/pre-setup-failed/`; no native child or source fixture was created there.
- **ADVICE only:** `task-6-native-proof-advice.md`.
- **INVENTORY/PREPARATION only:** installed-version and runtime-preparation JSON files.

## C1–C14 results

### C1 — PASS (component)

Snapshot roundtrip, canonical JSON, generation CAS, bounded concurrent one-winner CAS, and ≤12 KiB budget are covered by `tests/test_backlog_checkpoint.py`. The final-fix fixture additionally performs actual evidence write/read and snapshot write/read with exactly 1000 source records, 200 test records, and 100 review records, asserting ≤1 MiB evidence, ≤12 KiB snapshot, and ≤15 snapshot lines.

### C2 — PASS (component)

Partial-write preservation, symlink/no-follow rejection, size/private-root/traversal/digest checks, and explicit post-publish indeterminate behavior passed. Fresh evidence and admission hierarchy tests verify child-directory sync before parent-directory sync, and injected parent-sync failures remain bounded before evidence or active-admission publication. Real-filesystem regressions additionally cover reopening a valid leftover after creator parent-sync failure and a concurrent opener while the creator is paused at that barrier; both openers establish their own child-plus-parent barrier. Existing private directories are validated rather than silently mode-repaired. Foreign UID is enforced by descriptor metadata but lacks a host-level foreign-UID fixture.

### C3 — NOT PASS

Codex actual pre-edit evidence was running → interrupt(previous running) → interrupted. Fixture PID/UID/cmdline, closed writer FD, and source absence were verified. Parent lookup of the child-owned pending tool failed at the namespace boundary, so no independent tool-terminal receipt exists. Exact pidfd cleanup and an empty process group prove cleanup only. The snapshot is `PAUSED` with `TOOL_SESSION_UNOBSERVABLE`; no source edit, replacement, post-edit case, follow-up, or old-child wake occurred.

Claude is unsupported on the current exposed surface because no Agent/TaskStop/same-run terminal/non-fork bridge was available. Installed CLI version alone is not proof, and no Claude model or SDK session ran. Cross-runtime C3 is therefore `NOT PASS`.

### C4 — PARTIAL

Supervisor/runtime consistency, stale/foreign admission, owner binding, and successor identity are tested. The actual pre-edit case stopped PAUSED without lifecycle mutation. No deliberate live supervisor/Claim switch was performed; recorded identity is observation, not Registry authority.

### C5 — PASS (component)

Unique direct commit adoption and content/path/head/multiple-child/merge/replacement-ref/final-recheck branches passed in `tests/test_backlog_git_evidence.py`. Normal and ignored public checkpoints passed collect→commit→bind.

### C6 — PASS (exact selection)

Exact checkpoint selection and foreign/extra/tracked/staged/staged-delete-recreate/symlink/mode rejection passed. The general compaction glob is not a recovery selector. No two-live-session native resume was performed.

### C7 — PASS

ABA, double collection, manifest mismatch, changed checkpoint, and final HEAD recheck tests passed. The final post-residual whole suite returned 5370 passes and remained bound to the unchanged 16 execution/test hashes and two guidance hashes. The historical 5355 and 5368 results are not reused as proof for the final working source.

### C8 — PASS for this run's stop boundary

No external gate, merge, Task finish, or next issue was executed. Admission release requires exact terminal/quiescent evidence and rejects stale child phase/attempt. This is not a successful native production run through every phase.

### C9 — PASS (component)

Deterministic Kahn order, closed blockers, long chains, cross-edges, cycles/downstream reasons, invalid input, pagination failure, unsafe links, caps, deduplication, and blocker lookup passed in `tests/test_backlog_queue.py`. No live GitHub query was made.

### C10 — PASS for temporary HOME

Dual-runtime discovery/helper execution from foreign cwd, conflict preservation, and launcher-only exclusion passed. Actual HOME was intentionally untouched.

### C11 — PASS (component)

Canonical checkout, alias/child/wrong-ref/wrong-branch, duplicate branch, admission spelling, and foreign-cwd helper resolution passed. A malicious native child cwd escape was not run.

### C12 — PARTIAL

Exclusive admission/private state, stale release, terminal/quiescence, concurrent CAS, and canonical worktree tests passed. The actual pre-edit case used the current Task root and left source unchanged, but C3 stopped before replacement/serial-child recovery. Task authority remains external.

### C13 — PASS for coexistence

The isolated existing-hook experiment recorded three exit-0 cases: stale general handoff blocked, fresh backlog-named handoff allowed compaction, and both stale blocked with the backlog file freshest. This confirms filename-freshness interaction only; the drainer still uses an exact path. Hook semantics and actual HOME heartbeat were not changed.

### C14 — PASS (component)

Required proof, exact archive/publish, prior/quiescence rejection, pre/post-publish fsync, and owned-child archive/publish crash boundaries preserve old or successor active. No Registry-approved successor Task recovery was invoked.

## Overall verdict

The bounded v3 helper/workflow implementation is validated at working-change digest `ea5616dc79c56e60bc88910347f7ab43ab57e9043fe89df8351b7d39d190a088`: all original I1/I2/M1/M2 findings are clear, the focused backlog target is current at 160 passes, relevant static checks pass, and the final whole-project suite has 5370 passes with stable frozen source hashes. The residual review found no remaining implementation blocker. Final canonical commit/evidence binding remains parent-owned. Native recovery remains intentionally unavailable on the observed tool surfaces: Codex is `UNSUPPORTED_TOOL_TERMINAL_VISIBILITY / PAUSED`, Claude is unsupported on this surface, and cross-runtime C3 is `NOT PASS`. These accepted conditional limitations are reported, not upgraded by fixture cleanup, advice, version output, or synthetic status.

## Decision ledger

Every durable `Ruling:` is retained with the cost if wrong.

1. Split implementation by responsibility, add no dependency, use explicit installed roles. **Cost:** runtime incompatibility or misrouted review.
2. Use targeted tests per slice and one fresh whole suite after integration. **Cost:** stale confidence or wasted runtime.
3. Parent commits after review; reviewer sees exact worker diff. **Cost:** unreviewed or wrong-byte commit.
4. Approved implementation authority persists within scope. **Cost:** needless approval churn or scope overrun if boundaries changed.
5. Native proof uses current actual Task root and isolated fixture; no fabricated Claim. **Cost:** invalid lifecycle evidence.
6. Treat the document-write hook misparse as a file-operation issue; do not change guards or take external action. **Cost:** guard bypass or unintended external action.
7. Retain fixed four-line Markdown v1 and test decode/roundtrip. **Cost:** compatibility migration or decoder ambiguity.
8. Post-rename directory-fsync failure is indeterminate; never auto-rollback; pre-publish failure preserves old bytes. **Cost:** destroyed generation or duplicate effects.
9. Reject Unicode Cc, cap nesting at 64, normalize recursion errors. **Cost:** path ambiguity/resource failure or over-tight rejection.
10. Release exact current snapshot with strict terminal/quiescent evidence; never release PAUSED; keep admission→snapshot lock order. **Cost:** foreign/stale release or race.
11. Fix the three concrete interface gaps together, including domain-error normalization, without unrelated cleanup. **Cost:** CLI error leakage or wider regression surface.
12. Terminal child slot matches phase/attempt and clears only after real prior quiescence; preserve old IDs in immutable evidence. **Cost:** fabricated READY state.
13. Allow one heavy protocol reference beside the short Skill and preflight installer targets. **Cost:** missing guidance or overlong discovery surface.
14. Exclude only one validated exact checkpoint; never glob/tracked/ignore mutation; reject unknown dirty paths. **Cost:** manifest contamination or hidden changes.
15. Retain four queue keys and deterministic `CLOSED_SATISFIED` reasons. **Cost:** consumer output migration only.
16. Support typed read-only repository collection and pure graph input; map runtime receipts precisely. **Cost:** CLI ambiguity, mutation surface, or false runtime promise.
17. Use validated parent-directory flock for snapshots, preserve admission→snapshot order, fail closed if unsupported. **Cost:** artifacts, CAS race, platform limitation, or coarser contention.
18. Untracked means absent from index and HEAD, including ignored files; reject tracked and staged-delete/recreated paths. **Cost:** rejected valid checkpoint or hidden tracked content.
19. Retain the private SDD workspace/raw native receipts required by the approved plan. Keep current-UID 0600 evidence private and copy no credential/raw session identifier into this document. **Cost:** local disk retention and later explicit cleanup; deleting now would erase the only evidence for native unsupported status. This retention is not hidden Task state.
20. Continue original I1 with the same fixer, minimal validated-existing-directory child-plus-parent fsync, and focused leftover/concurrent regressions; explicitly extend the generic one-scoped-review cap because a known load-bearing durability defect cannot be parked while claiming completion. Freeze I2/M1/M2 and guidance; add no general review or authority expansion. **Cost:** one additional focused review and a fresh full-suite pass.
21. Use the already-approved KeepBranch choice for finishing the development branch: stop at the verified commit with the Claim unchanged, without a redundant integration menu or new push/PR/merge/Task-finish action. Preserve the Task worktree and private evidence under prior authority. **Cost:** a later explicit integration decision remains necessary.

## Appendix: source and test citations

| Area | Production source | Direct tests / receipts |
| --- | --- | --- |
| Snapshot schema and encoding | `scripts/backlog_checkpoint/codec.py` | `tests/test_backlog_checkpoint.py` canonical, roundtrip, type, depth, budget cases |
| Atomic snapshot/evidence storage | `scripts/backlog_checkpoint/storage.py` | `tests/test_backlog_checkpoint.py` CAS, partial-write, fsync, symlink, evidence cases |
| Admission and successor recovery | `scripts/backlog_checkpoint/admission.py` | `tests/test_backlog_admission.py` exclusivity, release, reconcile, real-process crash cases |
| Git source and commit binding | `scripts/backlog_checkpoint/git_evidence.py` | `tests/test_backlog_git_evidence.py` canonical root, manifests, exact checkpoint, ABA, bind branches |
| Queue collection/ranking | `scripts/backlog_checkpoint/queue.py` | `tests/test_backlog_queue.py` ordering, cycles, pagination, caps, explicit blocker cases |
| Bounded CLI | `scripts/backlog_checkpoint/cli.py`, `scripts/backlog-checkpoint.py` | `tests/test_backlog_cli.py` grouped argv, input, exact snapshot, bounded errors |
| Temporary-HOME install | `scripts/install-backlog-drain.py`, `install.sh` | `tests/test_backlog_install.py` and relevant installer suites |
| Shared workflow contract | `skills/backlog-drain/SKILL.md`, `skills/backlog-drain/references/protocol.md` | Task 5 consumer-evaluation JSON is advisory behavior evidence, not native recovery proof |
| Existing compaction hook coexistence | `hooks/precompact-handoff.sh` | `.superpowers/sdd/2026-11-04-backlog-drainer/task-6-precompact-coexistence.json` |
| Actual native attempt | no production source mutation | `.superpowers/sdd/2026-11-04-backlog-drainer/native-cases/pre/context.json`, `snapshot-paused.md` |
| Full suite | repository-wide | `.superpowers/sdd/2026-11-04-backlog-drainer/residual-i1-full-pytest-result.json`, `residual-i1-full-suite-input.json`, and private raw log |

Design criteria are defined in `docs/superpowers/specs/2026-11-04-backlog-drainer-design.md`. Implementation sequence is in `docs/superpowers/plans/2026-11-04-backlog-drainer.md`. Task reports under the private SDD workspace preserve RED/GREEN and review receipts; they are supporting evidence, not substitutes for the actual tests and source cited above.
