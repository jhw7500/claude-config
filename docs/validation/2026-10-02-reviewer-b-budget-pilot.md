# Reviewer B budget calibration (#114)

This records the evidence used to choose the Reviewer B report-contract-8
limits. Cross-snapshot observations do not establish an elapsed-time, command,
or token saving.

| Prior observation | Reviewer B result | Relevance |
| --- | --- | --- |
| [#111 native lockfile baseline](2026-09-09-pre-pr-tribunal-validation-telemetry.md) | `reviewer_total=216,384 ms` | Controller-observed dispatch-to-terminal time; internal commands unknown. |
| [#113 normal bundle arm](2026-09-12-reviewer-evidence-bundle.md) | 9 supported claims, 1 unverified, 8 fresh report executions, 27 audited commands, 243.885 s | Ordinary 10-supported/8-fresh-non-blocker, 270 s soft/300 s target admits this known shape. |
| [#113 blocker bundle arm](2026-09-12-reviewer-evidence-bundle.md) | Same HIGH blocker and 11 claim outcomes as independent arm; 7 fresh report executions, 22 audited commands, 286.012 s | High-risk 16-supported/12-fresh-non-blocker, 480 s soft/600 s target leaves room for the observed blocker case. |
| [#114 historical #125 account](https://github.com/jhw7500/claude-config/issues/114) | 15 claims, 17 executions, about 30 min | Historical comparison target, not a matched current-contract run. |

## Final calibration using the PR #185 offline audit

The merged offline native-cost tool was run against two caller-associated B
transcripts whose start times, bound HEADs, diff digests, and reviewer prompts
matched their controller telemetry. The association remains explicitly
unbound and non-gate evidence, as the tool reports.

| Observation | Supported claims | Report executions | Observed top-level native tool calls | Reviewer total | Last cumulative tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| Contract 6 PASS, `d35286a` | 8 | 4 | 35 | 499.929 s | 2,320,839 |
| Contract 7 PASS for PR #185, `ae8686b` | 8 | 2 | 8 | 160.929 s | 428,921 |

The two rows have different snapshots, diffs, instructions, and workloads, so
their differences are not a causal speedup claim. They do show that report
executions cannot stand in for all native tool calls: the first accepted B
report recorded four executions while its transcript contained 35 top-level
tool calls. The #113 transcript audits observed 28-30 top-level shell requests
for ordinary arms and 22-38 for blocker arms. The calibrated self-budget rounds
those observed envelopes up to 32 ordinary and 40 high-risk top-level native
tool calls. Nested shell commands are not counted separately.

The final profiles are therefore:

| Profile | Supported claims | Fresh non-blocker executions | Top-level native tool calls | Soft stop | Hard report deadline |
| --- | ---: | ---: | ---: | ---: | ---: |
| ordinary | 10 | 8 | 32 | 270 s | 300 s |
| high-risk | 16 | 12 | 40 | 480 s | 600 s |

The parser enforces the two report-entry counts. Reviewer B self-enforces the
native-call and time budgets; the offline tool audits supplied transcripts but
does not grant controller authority or alter a verdict. Refuted claims,
blocking evidence, and unverified required claims remain preserved as specified
below. No token cap is introduced because #111 and #113 have no comparable
native token records and the offline totals cover the entire supplied session.

## #183 post-calibration observation (not a speedup comparison)

The PR #185 offline tool audited caller-associated A/B transcripts for #183
round 3 (`ae8ab81`, diff `d4c7fd5`), which ended FAIL. Its association is
unbound and non-gate evidence; cumulative token counts cover each entire
supplied session. The controller telemetry recorded no reused evidence.

| Role | Reviewer total | Top-level native calls | Tool-wait union | Last cumulative tokens |
| --- | ---: | ---: | ---: | ---: |
| A | 679.958 s | 25 | 32.000 s | 1,908,608 |
| B | 501.722 s | 22 | 91.833 s | 1,662,267 |

The round took 1,474.402 s end-to-end. B supported all 7 required claims with
2 fresh report executions, but exceeded the 480 s high-risk soft stop while
remaining below its 600 s instructed report deadline. Its 22 observed tool
calls were below the role guide's nominal 40-call high-risk self-budget, but
the round's contract-7 context did not project `native_tool_calls`; no runtime
cap enforced that number. The report-entry and instructed tool-call limits
therefore did not establish lower elapsed time or token use in this round. In
particular, the 216.384 s #111 lockfile baseline and the #113 bundle arms have
different snapshots and workloads; none is a matched cost-reduction control
for #183.

The B transcript shows one broad three-module pytest request timed out after
180 s without becoming report evidence. Its accepted report instead cited a
152-test command that passed in 21.03 s and a focused 7-test command that
passed in 3.82 s. On the later uncommitted #183 fix, two controller-local
focused selections passed 27 tests in 10.45 s total (7.95 s and 2.50 s).
Those later results are pre-review checks, not authenticated evidence for a
new snapshot or a measured native-review speedup. Avoiding the failed broad
request is the concrete time-saving candidate; only an independent reviewer
on an authorized new lifecycle can establish its actual effect.
The historical #183 reports are bound to the old HEAD and diff, the #113 Node
bundle belongs to another snapshot, and `python-v1` cannot project arbitrary
pytest captures as reusable evidence. Eligible reuse for a changed #183
snapshot is therefore currently zero.

## Same-snapshot observations (not a controlled comparison)

On `c436881daf7874fb5da268e05ec43676f8c90768` with diff SHA-256
`fec643b0cf55182bbf97e6ced769e962235cac0a92eb66cc6ca532aaf79c2829`,
the installed contract-5 formal round ended `INCONCLUSIVE`. Its native B
response took 436,120 ms according to the closed controller telemetry and
contained 10 supported claims, 3 unverified claims, and 3 fresh report
executions. The complete round took 937,188 ms. There were no blocking
findings, but the unverified claims prevented PASS.

An isolated, non-authoritative native B dry-run used the candidate contract-6
prompt on that same commit and model. Controller dispatch-to-terminal observation
was at most 391 seconds (04:43:25-04:49:56 UTC). Its response contained 9
supported claims, 4 unverified claims, and 3 fresh report executions, within
the high-risk limits of 16 supported claims and 12 fresh non-blocker executions. The dry-run
did not submit to the installed CLI or alter the formal verdict. A broad test
batch timed out after 150 seconds and is not passing evidence; narrower batches
reported 33 and 10 passing tests. Separately, 14 candidate-contract tests
passed in an isolated local run.

These observations have different reviewer instructions and test selections.
Neither response supplies an audited total native command trace or token count,
and the candidate dry-run has no authenticated Tribunal receipt or telemetry
binding. They demonstrate one native response within the pilot's instructed
time and report-entry limits, but not a matched current-contract cost reduction,
general deadline enforcement, controller one-round compliance, or axis A
completion. The formal gate remains `INCONCLUSIVE` until a separately authorized
review of a new clean snapshot returns a different result.

The positive budget counts `supported` claims and fresh executions not cited by
refuted claims or CRITICAL/HIGH findings. Known evidence-backed refutations and
their blocker evidence remain in the report even when those pilot caps are
exhausted; all entries still count toward the global 128-entry limits. Other
required claims without proof remain `unverified`, yielding `INCONCLUSIVE`
unless preserved blockers make the result FAIL. Authenticated reused executions
do not consume the fresh-report cap. These checks do **not** observe every native tool
call, and reviewer time stops are instructions rather than controller-enforced
deadlines. The #113 paired arms reduced commands but did not reduce native
elapsed time; no speedup may be inferred from report-entry caps alone.

Codex `PreToolUse` hooks can deny supported local tool calls, but they are not
a hard Reviewer B command or wall-clock boundary. The documented hook path
excludes hosted tools, can have specialized paths that opt out, and may allow
a call through after a hook error or malformed response. A later
`write_stdin` poll also does not rerun `PreToolUse`. Subagent hooks expose an
`agent_id` at start, while the common tool-hook fields use the parent
`session_id` for subagents and do not identify the calling subagent. Thus a
repo hook cannot reliably meter B separately from A/C or force B to emit a
schema-valid terminal report at a deadline. Do not describe the pilot's
`hard_seconds` field as an externally enforced timeout. A true per-reviewer
hard cap requires a runtime-controlled execution surface with authenticated
reviewer identity and a terminal-report protocol; a tool-blocking hook alone
would conflict with the current no-disturbance rule for started reviewers.
See [Codex Hooks](https://learn.chatgpt.com/docs/hooks).

For a matching current lifecycle, `telemetry-summary.evidence` now reports
verified, unverified, and `BUDGET_EXHAUSTED:` claim counts alongside the selected
profile and report-entry limits. Before B seals or when lifecycle authentication
fails, these fields are `null`. `total_command_count` remains `null` because the
controller cannot reconstruct tool calls from a report; use a separately
audited native trace for that comparison.

Before calling axis A complete, run a matched-snapshot current-contract
comparison with an audited command trace, verify the high-risk blocker and
full-diff binding, and record reviewer calls, tokens where available, and
elapsed time. Do not compare the #111 lockfile interval directly to a different
repository/snapshot as a speedup claim.
