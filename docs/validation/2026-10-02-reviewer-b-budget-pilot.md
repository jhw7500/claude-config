# Reviewer B budget pilot calibration (#114)

This records the evidence used to choose provisional Reviewer B limits. It is
not a measurement of the new report-contract-6 reviewer, and it does not claim
an elapsed-time or token saving.

| Prior observation | Reviewer B result | Relevance |
| --- | --- | --- |
| [#111 native lockfile baseline](2026-09-09-pre-pr-tribunal-validation-telemetry.md) | `reviewer_total=216,384 ms` | Controller-observed dispatch-to-terminal time; internal commands unknown. |
| [#113 normal bundle arm](2026-09-12-reviewer-evidence-bundle.md) | 9 supported claims, 1 unverified, 8 fresh report executions, 27 audited commands, 243.885 s | Ordinary 10-supported/8-fresh-non-blocker, 270 s soft/300 s target admits this known shape. |
| [#113 blocker bundle arm](2026-09-12-reviewer-evidence-bundle.md) | Same HIGH blocker and 11 claim outcomes as independent arm; 7 fresh report executions, 22 audited commands, 286.012 s | High-risk 16-supported/12-fresh-non-blocker, 480 s soft/600 s target leaves room for the observed blocker case. |
| [#114 historical #125 account](https://github.com/jhw7500/claude-config/issues/114) | 15 claims, 17 executions, about 30 min | Historical comparison target, not a matched current-contract run. |

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
