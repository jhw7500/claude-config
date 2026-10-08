# Reviewer B budget: matched-snapshot comparison (2026-10-07)

## Question

Does the revised Reviewer B budget (budget contract 7 counting rules plus the
contract 8 native-call self-budget) reduce Reviewer B cost relative to the
first pilot budget (budget contract 6), on the same snapshot, controller
(Claude Code), and reviewer model, without losing the high-risk blocker? This is
the matched-snapshot current-contract comparison that
`2026-10-02-reviewer-b-budget-pilot.md` requires before axis A of #114 can be
called complete. Both arms carry a budget; no budget-free control was run.

The two arms are different Tribunal runtimes, not one runtime with only the
budget varied. Besides budget contracts 7 and 8, report contracts 9 to 11 add a
validation-phase projection (`validation.phase`, `validation.full_suite_recipe`)
to Reviewer B's instructions and report schema. A cost difference between the
arms therefore cannot be attributed to contracts 7/8 alone.

## Setup

- Snapshot: jhw7500/jhw-notion PR #140, head `47df3a80bfb97778812b3c094957701cb335687b`,
  base `b4bf525e50e5117833cb1d245357a0a85008408c`, merge base
  `439ddc35170df27bf74a056f48f28e7c07e4c384`, diff SHA-256
  `d2314a60f32b1ad04ebd644cc5879f58b48b85d865254e60e313a1f07052cbd9`
  (recomputed locally and by both runtimes' `policy-preview`). Default policy:
  floor 100, iterative, reviewers A (opus) and B (sonnet).
- Control arm C6: runtime `44d7a399a6c11ab8f77d0c97bb6c7a26e9c823ad`
  (report contract 6, budget contract 6). This is the first pilot budget, which
  replaced report contract 5 in `c436881` (an ancestor of this runtime; the
  same change is `c98b525` on master). Its high-risk profile allows 16 verified
  (supported or refuted) claims and 12 fresh executions, with a 480-second soft
  stop and a 600-second self-enforced report deadline; C6-1's projected
  `review_budget` carried these values.
- Treatment arm C11: runtime `d9b0f123b90df1bc3738d9f49242a5e4ea0bbcb3`
  (report contract 11, budget contract 7, native-call budget contract 8). Its
  high-risk profile allows 16 supported claims, 12 fresh non-blocker executions,
  and 40 top-level native tool calls, with the same time limits.
- Each runtime was exported with `git archive` and installed with
  `scripts/install-pre-pr-tribunal.py` into a fresh temporary HOME. A digest of
  the real HOME's tribunal surfaces (`.claude/settings.json`,
  `.codex/hooks.json`, both skill links, the installed package) was unchanged
  across both installs; a planted change in a probe directory changed the
  digest, so the check could detect a change. The live pin was not moved.
- Controller: Claude Code with native `Agent` reviewers. Each run used a fresh
  clone, a fresh detached view at the bound head, the arm's own role reference
  and report schema (passed by absolute path and SHA-256), and no evidence
  bundle. Runs were sequential and interleaved: C6-1, C11-1, C6-2, C11-2, then
  one full A+B round on C11. All runs happened on 2026-10-07 between 07:44Z and
  08:10Z.

## Metrics

Per run: harness-reported reviewer usage (`duration_ms`, `tool_uses`,
`subagent_tokens`), telemetry `reviewers.B.total_ms`, and counts from the sealed
report. Every report was extracted from the transcript JSONL (not the
completion notification), sealed with `submit-report`, and its receipt
`raw_sha256` matched the private response file in every run.
`scripts/tribunal-native-cost.py` reads Codex transcripts only, so it was not
used for these Claude runs.

## Results: B-only runs

| Run | Contract | duration_ms | tool_uses | tokens | telemetry total_ms | claims supported/refuted/unverified | executions | blocker |
|---|---|---|---|---|---|---|---|---|
| C6-1 | 6 | 211457 | 11 | 107702 | 233692 | 2/1/0 | 3 | HIGH |
| C11-1 | 11 | 214978 | 13 | 114520 | 225376 | 2/1/0 | 4 | HIGH |
| C6-2 | 6 | 204262 | 12 | 104478 | 200104 | 2/1/0 | 3 | HIGH |
| C11-2 | 11 | 232828 | 13 | 113632 | 237449 | 2/1/1 | 5 | HIGH |

Means: C6 207859.5 ms, 11.5 tool calls, 106090 tokens; C11 223903 ms, 13 tool
calls, 114076 tokens. C11 is higher by 7.7% (duration), 13.0% (tool calls), and
7.5% (tokens). The pre-registered reduction rule (both C11 runs below the C6
minimum on at least two of the three metrics, with the canary holding in all
four runs) is not met: no C11 run is below the C6 minimum on any metric.

## Results: full A+B round on C11

`finalize` returned `fail` with one blocking finding (B HIGH). Reviewer A
reported the same issue as MEDIUM. The stored verdict is bound to head
`47df3a80…`, merge base `439ddc35…`, and diff `d2314a60…`. Usage: A 401852 ms,
23 tool calls, 157092 tokens; B 183970 ms, 12 tool calls, 99330 tokens.
Telemetry totals: A 385892 ms, B 184724 ms.

## Blocker canary

The plan's original canary (the #113 TS5023 refutation on
`mcp-server/package.json:12`) came from #113's fixed claim set and cannot recur
when B selects its own claims; C6-1 judged typecheck as passing. Before any C11
result was seen, the canary was redefined as "B reports a HIGH or CRITICAL
finding backed by a refuted claim". Every B run, including the full round, did
so: production `npm audit` reports four advisories (critical proxy-addr, high
`@modelcontextprotocol/sdk`, two moderate) against the committed lockfile.
Reviewer A queried the GitHub advisory API: all six advisories involved were
published between 2026-09-18 and 2026-10-06, after the snapshot commit. The
blocker therefore depends on live advisory data, which is why all runs were
executed back to back on the same day.

## Audited command trace

`tool_use` blocks in each reviewer transcript were counted and checked for
repository-writing git commands, edit tools, GitHub writes, references to the
live tribunal install, `~/.codex`, and other runs' directories.

| Run | Tool calls | Bash | Read | Violations |
|---|---|---|---|---|
| C6-1 | 11 | 10 | 1 | 0 |
| C11-1 | 13 | 12 | 1 | 0 |
| C6-2 | 12 | 11 | 1 | 0 |
| C11-2 | 13 | 12 | 1 | 0 |
| C11 full A | 23 | 23 | 0 | 0 |
| C11 full B | 12 | 12 | 0 | 0 |

Tool-call counts equal the harness `tool_uses`. A synthetic transcript with
six planted violations produced six flags and did not flag a read-only
`git diff`, so a zero count is a measured result, not a silent check.

## Interpretation

No reduction was measured. On this snapshot B used three or four claims, three
to five executions, and about 3.5 minutes under both contracts, far below the
high-risk caps both arms carry (16 claims, 12 fresh executions, 600 s). The caps
never bound, so this snapshot cannot show what the contract 7/8 revision saves;
"no reduction" here means the measurement could not answer the question, not
that the revision is ineffective. The C11 increase is consistent with its one or
two extra executions; with n = 2 per arm it may be within run-to-run variation.

Because both arms carry a budget, this comparison says nothing about the effect
of introducing a budget at all. That would need a report contract 5 control from
before the budget pilot.

The historical #125 observation recorded in #114 (15 claims, 17 executions,
"about 30 minutes") came from a 2026-09-07 round on head `513033f`, an earlier
head of the same #125 work, with a Codex controller and `gpt-5.6-sol`
reviewers. Reviewer B's elapsed time in that Codex transcript was 1,230,172 ms
(about 20.5 minutes); the 30 minutes was the round from `begin` to the first
`finalize` attempt. The observation predates the budget pilot (2026-10-02) and
differs in snapshot, controller, and model, so it is not comparable as a
speedup claim. The 2026-10-08 re-measurement on `513033f`
(`2026-10-08-reviewer-b-budget-remeasure.md`) found that even report contract 5,
which has no budget, stays at 3 to 4 claims under the Claude controller, so the
difference is not caused by a budget contract. Whether it comes from the
controller, the model, or Reviewer B instruction changes between the runtimes
is not determined.

## Status against the pilot criterion

Done: matched-snapshot current-contract comparison, audited command trace,
high-risk blocker, full-diff binding, and recorded reviewer calls, tokens, and
elapsed time. Not met: #114's condition that a reduction be measured on a
representative case; the measured result is no reduction on a snapshot where
the caps do not bind. A follow-up needs either a snapshot on which B reaches the
caps (with a report contract 5 control if the budget itself is to be measured),
or a decision to treat the budget as a ceiling rather than a cost reducer.

## Limitations

- n = 2 per arm, one snapshot, Claude runtime only.
- No budget-free control: both arms carry a Reviewer B budget.
- Different Tribunal runtimes: the arms also differ in non-budget Reviewer B
  instructions (the report contract 9 to 11 validation phase), so the comparison
  does not isolate the budget revision.
- Usage figures are harness-reported observations, not gate evidence.
- The blocker depends on live advisory data.
- The measured runtimes are exported installs, not the live pin
  (`9cc21c2`, contract 7).
