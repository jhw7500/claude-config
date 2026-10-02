# Reviewer B budget pilot calibration (#114)

This records the evidence used to choose provisional Reviewer B limits. It is
not a measurement of the new report-contract-6 reviewer, and it does not claim
an elapsed-time or token saving.

| Prior observation | Reviewer B result | Relevance |
| --- | --- | --- |
| [#111 native lockfile baseline](2026-09-09-pre-pr-tribunal-validation-telemetry.md) | `reviewer_total=216,384 ms` | Controller-observed dispatch-to-terminal time; internal commands unknown. |
| [#113 normal bundle arm](2026-09-12-reviewer-evidence-bundle.md) | 9 supported claims, 1 unverified, 8 fresh report executions, 27 audited commands, 243.885 s | Ordinary 10-verified/8-fresh, 270 s soft/300 s target admits this known shape. |
| [#113 blocker bundle arm](2026-09-12-reviewer-evidence-bundle.md) | Same HIGH blocker and 11 claim outcomes as independent arm; 7 fresh report executions, 22 audited commands, 286.012 s | High-risk 16-verified/12-fresh, 480 s soft/600 s target leaves room for the observed blocker case. |
| [#114 historical #125 account](https://github.com/jhw7500/claude-config/issues/114) | 15 claims, 17 executions, about 30 min | Historical comparison target, not a matched current-contract run. |

The contract counts `supported` plus `refuted` claims and fresh executions
present in the submitted report. Extra required claims remain `unverified`,
yielding an `INCONCLUSIVE` gate result. Authenticated reused executions do not
consume the fresh-report cap. These checks do **not** observe every native tool
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
