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
new-handle requests, not necessarily three. A confirmed zero means stop before
fresh-view allocation or fresh dispatch. An explicit same-handle resume follows
the retained-format-handle checks below instead; zero fresh capacity alone does
not reject a supported same-handle continuation. An unavailable/inconclusive
optional query means capacity is unknown: report that
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
   Valid HIGH/CRITICAL reports seal and remain reusable. While automatic attempts
   remain, a format error retains its handle/view for bounded same-handle format
   retry; do not close that handle to free capacity during this retry path.
   At format-retry exhaustion, a known-terminal handle follows step 4 instead.
   Never repurpose a reviewer handle for another role.
3. Wait in calls of at most 60 seconds with progress updates, using a cumulative
   capacity-recovery wait budget of 600 seconds per invocation. A wait-interface
   timeout does not establish terminal failure. At budget exhaustion, suspend
   non-pass, retain live handles/views and evidence, and use the skill's incomplete
   telemetry-close path. Do not cancel, duplicate, or clean a live reviewer.
   Unknown liveness remains an integrity stop.
4. For a sealed role, preserve the complete private response and authenticated
   receipt before supported release of its terminal handle. Also release owned
   terminal operationally failed handles and terminal handles whose format-only
   retry budget is exhausted, when supported. Preserve their exact private
   responses and failure evidence outside the view before release or cleanup.
   Release is one bounded attempt per eligible handle; failure or an
   unavailable release tool is a capacity observation, not a new verdict gate.
   It supplies no release evidence; do not retry close indefinitely.
   For an exhausted pending format role, select view disposition by the observed
   handle state:

   | Handle state | View disposition |
   | --- | --- |
   | Confirmed released, or known terminal and no longer callable | One bounded non-force cleanup; later fresh pending-only recovery requires capacity |
   | Owned terminal handle remains retry-capable; release unavailable or unsuccessful | Retain the exact registered handle/view and tracking for explicit same-handle resume below |
   | Release result or follow-up capability unresolved | Preserve handle/view tracking and evidence for reconciliation; assume neither release nor callability |

   Clean verified views of sealed or terminal operationally failed roles and
   unused views at most once, non-force. Keep a terminal/unsealed format-retry
   view intact while an automatic retry remains or the table retains it for
   explicit resume. Refusal preserves a view as a warning. Terminal close is
   separate from view cleanup. Neither step authorizes discarding report evidence.
   An exhausted format role stays pending with its existing format error and
   cumulative attempt history; do not add `record-failure` for exhaustion.
   Release grants no fourth request: drain started peers, then return
   `REVIEWER_UNAVAILABLE` for explicit pending-only resume without finalizing.
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

## Explicit resume of a retained format-retry handle

Apply the skill's pending-round status, snapshot/contract, ownership, and context
checks first. Retained tracking includes the role, exact handle and registered
view, bound snapshot, failure evidence, and prior release/cleanup attempts.
Preserve these mappings across invocations; do not reset them during setup.

- For a still-running handle, use the existing wait/suspension path, not a new
  request. Unknown liveness or identity remains an integrity stop.
- For a retained terminal format-retry handle, reverify the same controlling
  ownership and role, and require supported evidence that the exact handle can
  accept follow-up in its existing cwd without a fresh dispatch. Independently
  verify that exact registered detached view still has the bound HEAD, empty
  `git status --porcelain -uall`, and no `.review`. If the view is dirty, missing,
  or unverifiable, preserve remaining evidence and stop; do not repair or
  recreate its cwd to make reuse possible. Unresolved follow-up capability also
  stops this recovery without assuming release or recording a process failure.
- When those checks pass, send a complete format-only request to that same
  handle, with only its own role prompt, shared snapshot, installed report
  schema, and projected context. No new view, spawn, or fresh-slot query is
  required for this supported continuation. Explicit resume starts a new
  per-invocation budget: this follow-up is request 1 of at most 3; persisted
  attempt history remains cumulative. This is not an automatic fourth request
  in the exhausted invocation. Apply every step 6 exact-byte acceptance check.
- A confirmed released or no-longer-callable terminal handle instead takes the
  ordinary fresh pending-only recovery path, subject to fresh capacity. Preserve
  evidence and prior cleanup/release-attempt tracking; do not repeat those
  operations or convert missing capabilities into an operational failure.

## If continuation is unavailable

Return `REVIEWER_UNAVAILABLE` for an unresolved capacity stop. If a request was
actually rejected, retain `DISPATCH_FAILED` on that pending slot; a planned wave
with no rejected request records no dispatch failure. Report together:

- Exact sealed/pending roles and any retained live or terminal format-retry
  handle/view coordinates, with the reason for preservation.
- The observed capacity rejection, missing capability or exhausted budget, and
  whether release is supported, explicitly automatic, or unknown.
- The supported operator path: explicitly resume a retained format-retry handle
  through the checks above, or release capacity through the runtime's supported
  controls (by its owner for unrelated work) and explicitly resume pending roles
  with fresh reviewers. If neither path is available, say so; do not invent an
  executable close command or promise that waiting will free capacity.

On explicit resume, re-inspect capabilities and reconcile retained handles before
fresh dispatch; absence of a capacity-query API alone still does not block an
initial attempt. Snapshot/contract or ownership changes require user judgment.
Moving the controlling work to another session requires a separately authorized
Task/Claim Handoff/takeover; it is not pending-review resume. Never launch extra
sessions to evade limits, increase quotas, reinstall a candidate, force cleanup,
or edit native state to recover capacity.
