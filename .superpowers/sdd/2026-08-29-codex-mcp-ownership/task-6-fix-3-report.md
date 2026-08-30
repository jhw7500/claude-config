# Task 6 Fix Round 3 Report

## Outcome

Resolved the consolidated CRITICAL, four HIGH, and four MEDIUM findings from
`task-6-rereview-2.md`, starting from
`7c57ff0412b3d1b1c4986c19a9a516f0586782a3`. Cleanup now carries one pinned
root/revision/session lineage from the CLI audit through each delivery window,
keeps cleanup-owned TERM/force receipts separate from supervisor records, and
uses state-linked crash-recoverable event journals for Hook and cleanup
transitions.

## Finding resolution matrix

| Finding | Resolution | Behavioral evidence |
| --- | --- | --- |
| CRITICAL 1: one authority lineage | Apply captures an exact pinned root token, monotonic ledger revision, complete session digest, process set, and lease set. Every session/process/intent writer increments the revision under the same flock. Each send window rechecks root inode and lexical binding, revision, complete session set, process, owner generation, action evidence, and force evidence while holding the pinned root through the prepared pidfd send. | Replacement root with identical records, lexical rebind during intent save, and a new competing lease after pidfd preparation all produce zero delivery. |
| HIGH 1: cross-writer TERM durability | Cleanup-owned revisioned `SignalIntent` records are keyed by exact process generation, owner generation, full identity set, and action. Classification overlays these receipts; supervisor terminal writes load-current and merge only supervisor-owned fields. Pending/conflict receipts remain conservatively non-eligible and never force-escalate. | A real stale supervisor save cannot erase TERM authority; clean, partial, indeterminate, send-failure, terminal, and persistence-conflict branches retain explicit durable truth. |
| HIGH 2: absolute fallback deadline | One absolute monotonic deadline flows through state snapshot and recovery, audit/RSS, planning, backend construction, identity observation, pidfd open, final CAS/lock/send, post-observation, reconciliation, after-audit, and Hook diagnostics. Locks use remaining-budget timeouts. After a delivered action, only nonblocking minimal receipt/report work remains and no second pidfd begins. | Deterministic observe, RSS, open, intent, post-observation, plan, backend, audit, and CAS crossings stop at the next boundary; post-observation expiry preserves the first receipt and opens no second pidfd. |
| HIGH 3: partial force truth | Force receipts persist each delivered identity. Mid-classification TTL/authority expiry stops remaining actions, preserves attempted/terminated/survived/skipped outcomes, emits one fixed `cleanup_force_partial` event, and returns a nonzero partial report. | A two-identity stubborn target delivers one SIGKILL, expires before the second, reports attempted=1/survived=1/skipped=1, and records exactly one partial event. |
| HIGH 4: crash-truthful event journal | Hook and cleanup transitions prepare a journal containing target kind/key, exact expected/new digests, event ID, and redacted event. Recovery emits only when current equals the new digest, discards when current equals expected, and reports no event on conflict. Dedup scans the active log plus all retained rotations. | Stage-before-state interruption emits no event; committed-write-reported-failure recovers one event; append failure retries once; a rotated logged event is not duplicated; Hook append failure later recovers exactly one ordered lifecycle event. |
| MEDIUM 1: quarantine | Corrupt records move with one same-filesystem cross-directory `rename` to bounded random collision-safe destinations, followed by fsync of source and quarantine directories. There is no hard-link interval or overwrite of an enumerated destination. | Injected rename failure leaves one linked, diagnosable source; retry quarantines it; two common-prefix oversized records receive distinct destinations; root rebind cannot move replacement data. |
| MEDIUM 2: Hook combined failure | Durable state and ordered lifecycle journal preparation occur under one short lock. An outer `finally` guarantees notifier handling even when lock exit raises; notifier false/exception triggers exactly one bounded fallback and fixed redacted diagnostics within remaining budget. | Lock-exit plus notifier failure still invokes one fallback; Start/End event order is stable; notifier, fallback, source, and exception canaries do not leak. |
| MEDIUM 3: force token totality | Encoded/decoded byte, JSON depth, and node limits precede use. Unicode scalar, UTF-8, JSON, value/type/overflow/recursion, canonicalization, and numeric conversion failures normalize to `InvalidForceConfirmation`. | Deep, oversized, huge-number, invalid-scalar, and numeric-overflow tokens produce the constant failure surface, no traceback, and no backend construction. |
| MEDIUM 4: reporting and compatibility | Cleanup reports deterministic before/after counts and classifications, explicit authority loss and partial force, and `preflight_*` labels for snapshot-only fields. Missing prior-v1 `owner_generation` loads as `None` and therefore classifies conservatively instead of quarantining. | Authority loss exposes no stale after evidence; human output labels partial force; report dictionaries contain no unprefixed snapshot-only fields; a prior valid v1 process loads unknown without quarantine. |

## RED to GREEN evidence

Reviewer schedules were committed as deterministic behavioral regressions. REDs
included missing deadline parameters at observe/RSS boundaries, work continuing
past post-observation expiry, absent partial-force event truth, a legacy
process-owned persistence-conflict path, missing human partial reporting, and a
supervisor merge retaining `association_pending`. Each failed for the intended
behavior and then passed after the minimum production change. Additional
authority-lineage, journal crash/recovery, root-rebind, generation, token,
quarantine, and Hook combined-failure schedules from the review are present in
the focused suite.

## Fresh verification

- state/classify/cleanup/supervisor/Hook-CLI focused matrix: **346 passed** before
  the final persistence-conflict regression; complete package verification after
  that regression: **381 passed**.
- complete repository suite: **1,612 passed**.
- Two earlier complete-suite attempts each exposed one existing live-process
  timing flake in the late-fork supervisor family; each exact parameter passed
  immediately on rerun, and the final complete suite passed without failure.
- Ruff check: PASS; Ruff format: all changed Python files formatted.
- `python3 -m compileall -q`: PASS.
- `git diff --check`: PASS.
- Production semantic-canary, traceback-emission, forbidden-marker, Hook/CLI
  direct-signal, and Hook/CLI sleep scans: zero findings.
- Policy scan confirms `OWNER_GRACE_SECONDS = 120.0`, fixed
  `/usr/bin/systemctl --user start --no-block`, and `shell=False`.
- Fixture leak scan found no fake MCP, late-fork, supervisor-fixture, or retained
  sleep process.

## Lock, signal, journal, and redaction evidence

Procfs traversal, RSS reads, pidfd preparation, and post-signal observation stay
outside the global flock. The final signal window performs only pinned-root and
exact-ledger reads, bounded intent writes, lexical/root/revision/session/process
revalidation, force TTL validation, and the already-open pidfd syscall. No
procfs traversal, RSS read, sleep, or wait occurs under that lock. A writer that
changes any authoritative lease or process advances the ledger revision and
invalidates delivery.

TERM intent is durable before delivery and lives outside `ManagedProcess`, so a
stale supervisor save cannot erase cleanup truth. Ambiguous receipts remain
pending/conflict and are deliberately ineligible for automatic repeat or force
escalation. Event recovery compares exact record digests and scans all retained
logs for its stable event ID before appending.

No raw session ID, cwd, source, command/arguments, token, corrupt payload,
exception, or semantic canary is rendered to CLI output, diagnostics, or events.
Tests use fixture procfs and injected signal backends only; no live HOME, Codex
configuration, systemd unit, production PID, or nonfixture process was changed
or signaled.

## Assumptions and residual risks

- Prior schema-v1 process records without generation evidence are valid but
  intentionally unknown until rewritten by a current authoritative supervisor.
- An ambiguous delivered intent requires later safe reconciliation; it never
  authorizes force escalation by itself.
- Python cannot preempt an external syscall already in progress. Boundary checks
  prevent starting the next operation, and the pidfd send itself occurs only
  after the final short locked validation.
- Real user-systemd integration remains intentionally uninvoked; fixed argv and
  failure behavior are fixture-tested at the subprocess boundary.
- The implementation commit is the single commit containing this report; its
  concrete SHA is reported in the handoff because a commit cannot embed its own
  content-addressed SHA.
