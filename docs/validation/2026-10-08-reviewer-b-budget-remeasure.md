# Reviewer B budget: re-measurement on the #125 baseline snapshot (2026-10-08)

## Question

The 2026-10-07 comparison (`2026-10-07-reviewer-b-budget-matched.md`) could not
show a budget effect: Reviewer B never reached the caps, and both arms already
carried a budget. This re-measurement uses the snapshot that #114's completion
condition names ("#125 기준 snapshot"), where B historically produced 15 claims
and 17 executions, and adds a control with no budget. It asks whether the first
pilot budget (C5 to C6), the revised budget (C6 to C11), or both (C5 to C11)
reduce Reviewer B cost.

## Setup

- Snapshot: jhw7500/jhw-notion head `513033f76d06a9a12b292f0980cbb240c929173b`,
  base `main` = merge base `439ddc35170df27bf74a056f48f28e7c07e4c384`, diff
  SHA-256 `681d7279678646574aca934b5b21174e2bb823ef7f642f7f2f3606a02c3c9662`
  (`.gitignore`, `mcp-server/package-lock.json`). This is the head reviewed in
  the 2026-09-07 round that produced the historical observation; the diff
  SHA-256 matches that round. All three runtimes' `policy-preview` reported the
  same diff, floor 100, iterative mode, and B = sonnet.
- Arms, each installed with `scripts/install-pre-pr-tribunal.py` into its own
  temporary HOME (the real HOME's tribunal surfaces were unchanged; a planted
  change in a probe directory changed the digest):
  - C5: `fddfbd3c042cb75001df0a60256aa27aa81c9e84`, report contract 5, no
    budget. Its projected B context has no `review_budget`.
  - C6: `44d7a399a6c11ab8f77d0c97bb6c7a26e9c823ad`, report and budget
    contract 6.
  - C11: `d9b0f123b90df1bc3738d9f49242a5e4ea0bbcb3`, report contract 11, budget
    contract 7, native-call budget contract 8 (no Tribunal code changes up to
    master `c5d59c5`).
- Controller: Claude Code with native `Agent` reviewers (B = sonnet), a fresh
  clone and detached view per run, the arm's own role reference and report
  schema passed by path and SHA-256, and no evidence bundle. Order: C5-1, C6-1,
  C11-1, C5-2, C6-2, C11-2, all B-only, on 2026-10-08 between 02:15Z and 02:41Z.
- The decision rules below were written to the measurement plan before the
  first run.

## Decision rules (pre-registered)

- For each comparison X to Y: "reduction" if both Y runs are below the X
  minimum on at least two of duration, tool calls, and tokens; "increase" if
  both are above the X maximum on at least two; otherwise no conclusion.
- A C6 or C11 run "reaches a cap" if it has 16 supported claims, 12 fresh
  executions, any `BUDGET_EXHAUSTED` claim, or at least 600 seconds.
- Blocker preservation applies only if both C5 runs report a HIGH or CRITICAL
  finding backed by a refuted claim.

## Results

| Run | Contract | duration_ms | tool_uses | tokens | telemetry B ms | claims supported/refuted/unverified | executions | finding |
|---|---|---|---|---|---|---|---|---|
| C5-1 | 5 | 327948 | 11 | 108900 | 338621 | 2/0/2 | 4 | none |
| C6-1 | 6 | 208189 | 13 | 110527 | 213243 | 2/1/1 | 3 | HIGH |
| C11-1 | 11 | 176368 | 10 | 113619 | 178532 | 3/0/0 | 5 | MEDIUM |
| C5-2 | 5 | 46256 | 7 | 109759 | 54556 | 1/1/1 | 3 | HIGH |
| C6-2 | 6 | 273374 | 13 | 109215 | 276111 | 2/1/0 | 3 | HIGH |
| C11-2 | 11 | 266943 | 12 | 109351 | 272921 | 3/0/1 | 2 | none |

Means: C5 187102 ms, 9 tool calls, 109330 tokens; C6 240782 ms, 13, 109871;
C11 221656 ms, 11, 111485.

- C5 to C6, C6 to C11, and C5 to C11: no conclusion under the rules above.
- No C6 or C11 run reached a cap (at most 4 claims, 5 executions, 4 min 33 s;
  no `BUDGET_EXHAUSTED` claim).
- Blocker preservation: not applicable, because only one of the two C5 runs
  reported a HIGH finding.

## Validity

- Every report was extracted from the reviewer transcript (not the completion
  notification) and sealed with `submit-report`; the receipt `raw_sha256`
  matched the private response file in all six runs.
- A transcript audit found no repository-writing git command, edit tool,
  GitHub write, live tribunal install reference, `~/.codex` reference, or
  other-run reference. Per-run tool-call counts equal the harness `tool_uses`.
  A synthetic transcript with six planted violations produced six flags.

## Interpretation

Under the Claude controller with B = sonnet, Reviewer B stays small on this
snapshot even with no budget: C5 used 3 to 4 claims and 3 to 4 executions. The
historical 15 claims and 17 executions on the same snapshot came from a Codex
controller with `gpt-5.6-sol` reviewers and an older runtime, so that size is
more likely a property of controller and model than of the contract
(inference; this measurement does not separate those factors). In this
setting the caps have nothing to cut, so no budget saving can be shown, and
the budget acts only as an unexercised ceiling.

Tokens were 108.9k to 113.6k in all six runs, which suggests that fixed reading
cost (role reference, schema, context) dominates B's token use here
(inference).

## Blocker judgment varied across runs

All six runs observed the same live fact: production `npm audit` on the
committed lockfile reports four advisories (one critical, one high, two
moderate). The judgment of that fact differed by run:

- The claim that the audited vulnerabilities are resolved was refuted in C6-1,
  C5-2, and C6-2 (each with a HIGH finding), left unverified in C5-1 and C11-2
  (no finding), and not stated in C11-1, which instead supported a "16 to 8
  reduction" claim and reported a MEDIUM finding.
- With n = 2 per arm this cannot be attributed to the contracts. It shows that
  the blocker outcome on this snapshot depends on the run. The
  `REFUTED_CLAIM_REQUIRES_BLOCKER` rule (report contract 5 and later) requires
  a HIGH or CRITICAL finding whenever a claim is refuted, but it does not cover
  this variation, which is in whether the claim is refuted at all.

## Other observation

In C5-2, execution E001 (`npm ci`) did run, but its reported `capture_sha256`
(`e3b0c442…c0b7a55`) appears in no command output in the transcript; the
other 19 of 20 reported capture hashes do. The controller does not verify
reviewer-reported capture hashes, as #136 already records for execution fields
in general.

## Status against #114

- Done: the re-measurement on #114's baseline snapshot with a no-budget
  control, an audited command trace, receipts, and recorded calls, tokens, and
  elapsed time.
- Not met: "a reduction is measured on a representative case". Under the
  Claude controller there is no reduction to measure because pre-budget B is
  already small. The historical cost was observed under a Codex controller
  with `gpt-5.6-sol` reviewers, which this measurement did not cover.

## Limitations

- n = 2 per arm, one snapshot, Claude controller and sonnet only.
- Usage figures are harness-reported observations, not gate evidence.
- The blocker depends on live advisory data. The 2026-09-07 round reported no
  blocker on the same snapshot; the advisories involved were published from
  2026-09-18 onward, as the 2026-10-07 measurement recorded.
- B-only runs: no finalize, so no gate verdict was produced.
