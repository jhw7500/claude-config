# Native dispatch capacity and partial recovery

Controller-only procedure. Do not send this resource ledger, peer status, or peer
reports to reviewers. Keep the existing slot/receipt schema and integrity gates.

## Before begin or allocating views

Inspect the tools actually exposed in this invocation, including their documented
acceptance, wait, terminal-response, and release semantics. Required native
dispatch and terminal-result access must exist. Feature flags and V1/V2 labels
are not capacity evidence. Never invent a tool or parameter.

If a supported bounded capacity query exists, call it once. A documented bounded
reservation may be used only with known release semantics; retain its exact
owned token and release unused reservations on every exit. A reservation backing
a live reviewer is not unused. Positive capacity permits that many concurrent
requests, not necessarily three. A confirmed zero means stop before allocation
and explain how supported capacity release followed by explicit resume can help.
An unavailable/inconclusive optional query means capacity is unknown: report that
limitation and attempt native dispatch; it is not a permanent preflight blocker.

Record the applicable lifecycle evidence, not a quota inferred from agent lists:

| Exposed lifecycle | Evidence permitting another bounded wave |
| --- | --- |
| Terminal close/release | Successful supported release of this controller's terminal handle |
| Guaranteed auto-eviction | The exposed runtime contract explicitly guarantees capacity release at the observed terminal event |
| Unknown lifecycle | A supported query/reservation later confirms positive capacity; terminal status alone is insufficient |

Completed entries may or may not consume capacity. Never close unrelated agents,
infer capacity from their count, or manipulate runtime configuration/raw state.

## Bounded waves and capacity rejection

When preflight confirms fewer available slots than pending roles, dispatch only
that many roles, then enter step 2 below to drain and advance the planned wave.
No rejection is needed for this route and unrequested roles have no failure.
Step 4's terminal-release rules also apply on an ordinary all-success run, even
when no wave or dispatch rejection occurred.

Step 1 applies only to an explicit native capacity/agent-thread-limit rejection
that proves the request accepted no handle. A transport timeout or ambiguous
acceptance is an integrity stop, not evidence of capacity failure. Mere absence
of a handle is insufficient. Other setup failures retain the skill's stop path.

1. Pause further dispatch. Track each role as never requested, rejected without a
   handle, running, terminal/unsealed, or sealed, alongside its exact created view
   and handle when present. Preserve `CREATED_VIEWS` and `STARTED_REVIEWERS` across
   waves; never overwrite a live mapping. Count the rejected request in
   `ATTEMPTS_THIS_INVOCATION[X]` and record `DISPATCH_FAILED` exactly once with
   `record-failure --reviewer X --reason DISPATCH_FAILED`.
   Never record failure for an unrequested peer.
2. Drain started reviewers independently, accepting exact valid responses through
   the skill's private-file, submit-receipt digest, and stored-validation checks.
   Valid HIGH/CRITICAL reports seal and remain reusable. A format error retains
   its handle/view for bounded same-handle format retry; do not close that handle
   to free capacity. Never repurpose a reviewer handle for another role.
3. Wait in calls of at most 60 seconds with progress updates, using a cumulative
   capacity-recovery wait budget of 600 seconds per invocation. A wait-interface
   timeout does not establish terminal failure. At budget exhaustion, suspend
   non-pass, retain live handles/views and evidence, and use the skill's incomplete
   telemetry-close path. Do not cancel, duplicate, or clean a live reviewer.
   Unknown liveness remains an integrity stop.
4. For a sealed role, preserve the complete private response and authenticated
   receipt before supported release of its terminal handle. Also release owned
   terminal operationally failed handles when supported, retaining their failure
   evidence. Release is one bounded attempt per eligible handle; failure or an
   unavailable release tool is a capacity observation, not a new verdict gate.
   It supplies no release evidence; do not retry close indefinitely. Cleanup each
   verified view whose role sealed or operationally failed, and unused views, at
   most once, non-force. Keep a terminal/unsealed format-retry view intact.
   Refusal preserves a view as a warning. Terminal close is separate from view
   cleanup. Neither step authorizes discarding report evidence.
5. After draining the current wave, continue only when the lifecycle table gives
   new capacity evidence. If a supported query exists, it may be checked once
   after that drain. Each release/terminal-capacity event is consumed once to
   bound the next wave: one released handle permits at most one new request
   unless a supported query/reservation confirms a larger count. A rejection
   pauses the wave again and cannot reuse the same evidence for another retry.
   No progress means stop, even with attempts left.
   With zero started roles there is nothing owned to drain/release; a later
   supported positive query/reservation is needed for automatic continuation.
6. Before continuation, run `status` and recheck the same snapshot/contract and
   sealed receipts. Request only pending roles with no live/uncertain handle,
   within the existing maximum three requests per role per invocation. Generate
   only their projected contexts and fresh verified detached views. Remove or
   preserve unused old views once, never reuse them as a fresh replacement.
   Start independent native roles in the newly supported capacity, with no peer
   report or slot-status disclosure. Do not run `begin`, reset `.review`, change
   the installed contract, or rerun a sealed reviewer.

When all three roles are sealed, perform every immediate pre-final check and
pathless `finalize`. Preserved blockers still yield a non-pass verdict; there is
no two-of-three pass. Capacity recovery does not downgrade an integrity stop.

## If continuation is unavailable

Return `REVIEWER_UNAVAILABLE` for an unresolved capacity stop. If a request was
actually rejected, retain `DISPATCH_FAILED` on that pending slot; a planned wave
with no rejected request records no dispatch failure. Report together:

- Exact sealed/pending roles and any retained live handle/view coordinates.
- The observed capacity rejection, missing capability or exhausted budget, and
  whether release is supported, explicitly automatic, or unknown.
- The supported operator path: release capacity through the runtime's supported
  controls (by its owner for unrelated work), then explicitly resume this same
  bound pending round. If those controls are not exposed, say so; do not invent
  an executable close command or promise that waiting will free capacity.

On explicit resume, re-inspect capabilities and reconcile retained handles before
fresh dispatch; absence of a capacity-query API alone still does not block an
initial attempt. Snapshot/contract or ownership changes require user judgment.
Moving the controlling work to another session requires a separately authorized
Task/Claim Handoff/takeover; it is not pending-review resume. Never launch extra
sessions to evade limits, increase quotas, reinstall a candidate, force cleanup,
or edit native state to recover capacity.
