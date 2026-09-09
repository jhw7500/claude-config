# Tribunal validation and telemetry rollout: measured evidence

Date: 2026-09-09. Scope: #109, #111 and #112. This records a real native
lockfile-only observation separately from synthetic probe and clock tests.

## Successful native lockfile baseline

The controller installed the package and shared Skill into a disposable HOME
and used an independent disposable Git repository. Its only committed feature
change was `package-lock.json`. Three native reviewers used separate clean
detached views and their own projected context and installed role/schema
references. Existing separate agent threads were reused at native capacity.
No live GitHub operation or credential-store access was used for this fixture.

| Binding | Value |
| --- | --- |
| Feature commit | `1ad6cae0a552764795bc95d83c6cc464651dead8` |
| Base and merge-base | `033c30ab0177da448ccbdb65d1cc5945811e4926` |
| Changed path | `package-lock.json` |
| Diff SHA-256 | `629cab43618a272e9aa3e7a0fcdc843baa02c5bde4af6f229d176e5ed0194366` |
| Contract | `report_text=2, diff_recipe=1, telemetry_schema=1` |

The installed CLI measured each lifecycle span. All three dispatch acceptance
signals were unavailable; the three dispatch spans retain observed nonzero
durations, outcome `incomplete`, and reason `RUNTIME_SIGNAL_UNAVAILABLE`.
`reviewer_total` therefore measures dispatch-to-terminal time. No unavailable
interval was replaced with zero.

All A/B/C store receipts matched their separate pre-final stored validation
digests. Immediately before finalization, descriptor checks independently
verified every report was current-user-owned, regular, non-symlink, and exact
mode `0600`. Finalization returned pass with zero blockers. All detached views
were removed without force; exact report files were retained.

The largest available bounded Reviewer B stage was `reviewer_total`,
216,384 ms. These are controller-observed elapsed times, including scheduling
and tool handoff overhead. They do not identify an internal Reviewer B command
or establish isolated CLI execution cost. Optimization comparisons must retain
the same snapshot and contract and identify any runtime-orchestration changes.

Exact sanitized summary returned for this successful run:

```json
{"schema":1,"binding":{"contract":{"report_text":2,"diff_recipe":1,"telemetry_schema":1},"diff_sha256":"629cab43618a272e9aa3e7a0fcdc843baa02c5bde4af6f229d176e5ed0194366"},"reviewers":{"A":{"total_ms":80495},"B":{"total_ms":216384},"C":{"total_ms":143216}},"stages":{"snapshot_preflight":{"count":1,"total_ms":42},"view_create":{"count":3,"total_ms":64385},"reviewer_dispatch_wait":{"count":3,"total_ms":440107},"reviewer_total":{"count":3,"total_ms":440095},"report_store":{"count":3,"total_ms":151920},"report_validation":{"count":6,"total_ms":110335},"finalize":{"count":1,"total_ms":51857},"view_cleanup":{"count":3,"total_ms":38260}},"outcomes":{"success":20,"failure":0,"timeout":0,"incomplete":3,"clock_anomaly":0},"telemetry_incomplete":false,"anomaly_reason_codes":[],"early_detection":null}
```

The 23 stage occurrences equal 20 successful plus three incomplete terminal
outcomes: no span remains running. Six report-validation spans include three
immediate checks and three separate pre-final checks. The synthetic probe
below records its pre-final checks inside its finalize span instead.

## Earlier failed native attempt

An earlier independent fixture had feature commit
`f013de77e28105c3fe4346c86d7045e91ed7aa98`, base/merge-base
`f3b19a9500ca1d1d4ac4ebc95a33f031f842167e`, and the same diff digest and
contract. Reviewer C allocation hit the native thread limit. A/B exact reports
were securely stored; A validated and B returned `REPORT_SCHEMA_INVALID`.
No finalizer was invoked. All three created views were removed without force,
and the run closed with failure and no running spans. The pending verdict and
reports were preserved; the successful baseline used a new independent fixture.

This failed run recorded one snapshot preflight, three view creations, three
dispatch waits, three reviewer totals, two stores, two validations, and three
cleanups. Its outcomes were 14 successes and three failures, with
`telemetry_incomplete=false`. Sanitized early detection identified reviewer B
at elapsed 632,111 ms and the last terminal milestone at 632,414 ms, yielding
`wait_all_delay_ms=303`. This is failure-path evidence, not the successful
performance baseline.

## Synthetic installed probe evidence

The probe's pass-verdict helper produces synthetic report bytes solely to
exercise installed storage, strict validation, digest receipts, finalization,
and telemetry. Its reviewer totals cover synthetic report production; they
are not native reviewer latency measurements. Public canary output and existing
runtime isolation/cleanup behavior remain unchanged.

The CR negative test uses actual installed subprocesses, storage, parser, and
ledger with injected CLI clocks. A's original CR-bearing response remains
byte-for-byte intact with exact `0600`; validation returns `TEXT_INVALID`.
Already-started synthetic peers reach terminal state, all three reports are
validated, no finalizer is invoked, and the verdict remains in progress.
The injected last-peer gap is 20,000 ms and the sanitized
`early_detection.wait_all_delay_ms` is exactly 20,000. These deterministic
clock values are synthetic, not observed native durations.

Exact sanitized summary from the controlled-clock run (zero durations below
are explicitly injected clock values, never substitutes for missing native
dispatch signals):

```json
{"schema":1,"binding":{"contract":{"report_text":2,"diff_recipe":1,"telemetry_schema":1},"diff_sha256":"fd62a8509b81f87b0c3de4101313e4c184978e2eb98fde0bcdfc1f590bb6f7de"},"reviewers":{"A":{"total_ms":0},"B":{"total_ms":0},"C":{"total_ms":20000}},"stages":{"snapshot_preflight":{"count":1,"total_ms":0},"reviewer_total":{"count":3,"total_ms":20000},"report_store":{"count":3,"total_ms":0},"report_validation":{"count":3,"total_ms":0}},"outcomes":{"success":9,"failure":1,"timeout":0,"incomplete":0,"clock_anomaly":0},"telemetry_incomplete":false,"anomaly_reason_codes":[],"early_detection":{"reviewer":"A","reason_code":"TEXT_INVALID","detected_elapsed_ms":0,"all_reviewers_terminal_elapsed_ms":20000,"wait_all_delay_ms":20000}}
```

Tests also cover umasks `000`, `022`, and `077`, matching captured/store/validation
SHA-256 receipts, changes after pre-final validation (digest, mode, symlink),
and malformed telemetry preserving the primary pass result.

## Verification

Only verification invocations and bounded results are recorded here; reviewer
commands, report bodies, raw output, tokens, absolute home paths, and environment
values are omitted.

| Verification invocation | Result |
| --- | --- |
| `rtk python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py tests/pre_pr_tribunal/test_probe_harness.py` | Initial RED: exit 1; 4 failed, 190 passed, 43.82s |
| `rtk python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py tests/pre_pr_tribunal/test_install_integration.py tests/pre_pr_tribunal/test_probe_harness.py` | GREEN: exit 0; 202 passed, 111.46s |
| `rtk python3 -m pytest -q tests/pre_pr_tribunal tests/runtime_hook_installer` | Exit 0; 1,214 passed, 209.61s |
| `rtk python3 -m pytest -q` | Default path: exit 2; collection fails because `slack_bolt` is unavailable |
| Same full-suite invocation with the existing workspace-local Python dependency projection | Exit 0; 3,281 passed, 298.39s |
| `rtk bash -n install.sh hooks/*.sh scripts/*.sh scripts/lib/*.sh` | Exit 0 |
| `rtk shellcheck -x -s bash -S error install.sh hooks/*.sh scripts/*.sh scripts/lib/*.sh` | Exit 0 |
| `rtk git diff --check` | Exit 0 after document creation |

The corrected CR fixture and positive lifecycle cases were also rerun against
the original base helper in memory, without modifying the working source:
four expected failures, 112 deselected, 1.08s. A separate pre-final whitespace
mutation first failed with an unexpected finalization, then passed after the
controller's final descriptor digest check was added.

Targeted contract searches completed successfully for text/diff versions,
secure storage and receipt validation, all nine telemetry stages, and explicit
scope boundaries. Parser/store/telemetry/Skill tests cover their respective
contracts; the installed probe adds lifecycle and negative-path integration.

| Contract | Production surface | Representative regression |
| --- | --- | --- |
| Excerpt-only LF/TAB | `model.py` | `test_execution_excerpts_accept_only_lf_and_tab`, `test_non_excerpt_text_keeps_rejecting_lf_and_tab` |
| Reproducible diff digest | `git_state.py`, CLI context | `test_diff_contract_reproduces_snapshot_digest` |
| Exact bytes and private report mode | `review_store.py`, `verdict_store.py` | `test_store_reviewer_report_preserves_bytes_and_forces_mode` |
| Shared validation and receipts | `model.py`, report CLI | `test_validate_report_bytes_raises_the_same_parser_code`, `test_installed_probe_stores_validates_and_finalizes_exact_reports` |
| Final safety and digest checks | Probe controller and finalizer | `test_installed_probe_rechecks_each_report_immediately_before_finalize` |
| Bound spans, recovery and anomalies | `telemetry.py`, telemetry CLI | `test_bound_run_records_terminal_span_and_sanitized_summary`, `test_recover_closes_every_running_span_without_reopening_it`, `test_clock_reversal_records_null_duration_and_anomaly` |
| Early error detection | Installed probe and telemetry summary | `test_installed_probe_invalid_a_preserves_cr_and_detects_before_peers` |
| Telemetry independence | Installed report lifecycle | `test_installed_probe_telemetry_failure_does_not_block_valid_reports` |
| Shared runtime controller guidance | Canonical Skill | `test_skill_records_every_required_telemetry_stage_without_making_it_a_gate` |

Issues #113, #114, #115, and #121 remain excluded: no evidence-bundle, budget,
selective-rerun, or prompt-experiment behavior was added.

## Post-review verification

After the final review fixes at `bf79876`, `7b6bd68`, and `458312a`, the following
checks ran against implementation HEAD `458312a5923671cace15453055de6b819608343c`.
Nine added cases cover the ignored artifact namespace, preservation of pending
recovery evidence on snapshot drift, and executable telemetry lifecycle commands.
The native fixture identities, measurements, and sanitized summaries above are
unchanged; these are fresh code and contract verification results.

| Verification invocation | Post-review result |
| --- | --- |
| `rtk proxy python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py tests/pre_pr_tribunal/test_install_integration.py tests/pre_pr_tribunal/test_probe_harness.py` | Exit 0; 202 passed, 112.29s |
| `rtk proxy python3 -m pytest -q tests/pre_pr_tribunal tests/runtime_hook_installer` | Exit 0; 1,223 passed, 241.01s |
| Full suite with the same workspace-local Python dependency projection | Exit 0; 3,290 passed, 361.41s |
| `bash -n` on each file in `install.sh hooks/*.sh scripts/*.sh scripts/lib/*.sh`, invoked through `rtk proxy` | Exit 0 |
| `rtk proxy shellcheck -x -s bash -S error install.sh hooks/*.sh scripts/*.sh scripts/lib/*.sh` | Exit 0 |
| `rtk proxy git diff --check` | Exit 0 |

## Post-rereview verification

The corrected namespace verification is recorded in implementation commit
`893f6e6696d326508273178c030188f01fc86e18`. The exact source and test contents
committed there passed the checks below, including 39 additional cases covering
wildcard negations, whole-parent ignore patterns, strict NUL records, canonical
child rechecks, and bounded Git input. The earlier post-review counts remain
historical; native fixture identities, measurements, and sanitized summaries
above are unchanged and were not rerun for this correction.

| Verification invocation | Post-rereview result |
| --- | --- |
| `rtk proxy python3 -m pytest -q tests/pre_pr_tribunal/test_telemetry.py tests/pre_pr_tribunal/test_git_state.py` | Exit 0; 260 passed, 46.41s |
| `rtk proxy python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py tests/pre_pr_tribunal/test_install_integration.py tests/pre_pr_tribunal/test_probe_harness.py` | Exit 0; 202 passed, 113.37s |
| `rtk proxy python3 -m pytest -q tests/pre_pr_tribunal tests/runtime_hook_installer` | Exit 0; 1,262 passed, 233.65s |
| Full suite with the same workspace-local Python dependency projection | Exit 0; 3,329 passed, 382.87s |
| `bash -n` on each file in `install.sh hooks/*.sh scripts/*.sh scripts/lib/*.sh`, invoked through `rtk proxy` | Exit 0 |
| `rtk proxy shellcheck -x -s bash -S error install.sh hooks/*.sh scripts/*.sh scripts/lib/*.sh` | Exit 0 |
| `rtk proxy git diff --check` | Exit 0 |
