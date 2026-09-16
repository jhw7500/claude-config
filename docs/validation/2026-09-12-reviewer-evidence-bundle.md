# Reviewer evidence bundle validation

> Historical contract-1 evidence only. The current implementation uses evidence contract 2 and
> `node-sandbox-v1`; none of the native timing/count results or the 3849-test run below establishes
> current acceptance. A fresh contract-2 native comparison, full suite, and whole-branch review
> remain required after the final implementation commit.

The contract-1 completed pairs preserved claim outcomes and normalized blockers while
avoiding deterministic validations. Bundle B was 6.589 seconds slower for the
normal fixture and 9.570 seconds slower for the blocker fixture. Reuse varied by
fixture. The CI-lock whole-repository suite passed 3849 tests. Final document
verification and whole-branch review were pending. This section is retained as
historical design evidence, not an overall acceptance PASS or a guaranteed saving claim.

## Fixed comparison

| Identity | Value |
| --- | --- |
| Source | jhw-notion PR #140 |
| Reviewed HEAD | `47df3a80bfb97778812b3c094957701cb335687b` |
| Base | `b4bf525e50e5117833cb1d245357a0a85008408c` |
| Merge-base | `439ddc35170df27bf74a056f48f28e7c07e4c384` |
| Historical diff SHA-256 | `d2314a60f32b1ad04ebd644cc5879f58b48b85d865254e60e313a1f07052cbd9` |

The installed diff recipe reproduced the historical digest exactly. Source was
isolated in local clones, preserving the original repository. Both arms use the
same final disposable installed candidate, current B guide/schema, gpt-6-astra
with medium effort, Node22.23.1/npm10.9.8, installed Vitest4.1.11 and fixed claims.
Every B view starts without dependencies or generated build outputs. Only the
bundle arms receive evidence. This is a current-time historical-snapshot
comparison, not a replay of old elapsed time. The candidate is uncommitted and
installed only in a disposable prefix, not global PR authority.

## Observed native results

| Observation | Independent normal | Bundle normal | Independent blocker | Bundle blocker |
| --- | ---: | ---: | ---: | ---: |
| Native elapsed, seconds | 237.296 | 243.885 | 276.442 | 286.012 |
| Original validations executed | 10 | 8 | 11 | 7 |
| Verifier commands executed | 0 | 1 | 0 | 1 |
| Inspection commands executed | 19 | 18 | 27 | 14 |
| Total commands executed | 29 | 27 | 38 | 22 |
| Pre-execution argument rejections | 1 | 1 | 0 | 0 |
| Total top-level shell requests | 30 | 28 | 38 | 22 |
| C001-C009 | Supported | Supported | Supported | Supported |
| C010 necessity of every lockfile change | Unverified | Unverified | Unverified | Unverified |
| C011 false required typecheck assertion | Not in fixture | Not in fixture | Refuted | Refuted |
| Findings | None | None | One HIGH | Same normalized HIGH |

Counts come from completed per-arm transcript audits, including expanded loops,
empty polling and explicit argument rejections, rather than report lengths.
All four completed arms have zero missing observations and zero observer
bypasses. Child process counts are not measured. Both independent arms initially
ran MCP smoke while the full suite rebuilt/cleaned `dist`, causing exit1; the
identical post-suite retry exited0. Both attempts are counted and preserved.
Thus the normal original-command reduction of two includes one fewer MCP
attempt; only typecheck is a demonstrated avoided deterministic validation.

Bundle normal had four eligible and two always-fresh rejected entries. It
explicitly reused E001 build and E002 typecheck, both cited by claims, but still
ran a fresh build for local MCP artifacts. Eligible E003 full-suite evidence was
not used: B ran the complete suite again, without an explicit final-report reason.
The report contains eight fresh executions. The single native verifier command
took 484.495 ms; the separate post-review telemetry summary's bundle verification
took 759.748 ms. Neither is an estimate of saved time. The one normal pair gives
no measured elapsed speedup and is not a repeatability or general performance
estimate.

Bundle blocker reused and claim-cited all four eligible entries: build,
typecheck, full test and false-typecheck. It still built fresh for local MCP
artifacts, but avoided fresh typecheck, full test and false-typecheck commands.
Its four-command original-validation reduction also includes one fewer MCP
attempt. Both audits ran fresh. Seven fresh report executions remain. The
verifier command took 436.810 ms; the separate summary verification took
723.686 ms. The long E003 excerpt, including exactly 2045 dots, passed sealed
entry-field authentication unchanged. More reuse and fewer executed commands
did not produce lower native elapsed in this run. There is one sequential
sample per fixture/arm, so neither pair supports statistical speedup inference.

The normal pair supports the same nine claims: disclosed dependency preparation,
zero current production/full audit findings, build, test-inclusive typecheck,
73 test files/2045 tests, built MCP initialization/ping/listTools, focused V8
coverage smoke, and unchanged runtime sources. C010 remains unverified in both.
Historical full-test stderr also contains a DEP0137/file-descriptor garbage
collection warning and an unawaited Vitest assertion warning. Those warnings
remain in the exact captures; the commands still exited0 with the reported
73 files/2045 tests. They are separate from the clean current 3849-test Python
suite below and do not change the native findings or counts.
Independent blocker additionally refutes C011 with actual TS5023/exit1 and
finding `B-R1-001`, HIGH, `mcp-server/package.json:12`. Bundle blocker preserves
all eleven claim outcomes and that finding's normalized identity: HIGH,
the same path, C011, the same failed command and exit1. Its report also uses
`B-R1-001` at line12; rationale and acceptance-condition wording differ naturally.

Each of the four native terminal reports was preserved byte-for-byte, matched to
its native session response, sealed and stored-validated. These are B-only
native rounds: A/C were not dispatched and no native round was finalized PASS.
The separate installed automated integration exercises the complete A/B/C
lifecycle and post-seal mutation rejection.

## Preparation cost and reproducibility

The first complete test exited0 in 42.525 seconds but correctly declined reuse
with `EVIDENCE_ENVIRONMENT_CHANGED`: Vitest created an empty
`node_modules/.vite-temp` even with `--no-cache`. Full-tree reconstruction without
that sole new directory reproduced the prior dependency hash exactly; including
it reproduced the prepared hash. No cache exclusion or relaxed proof was added.
Earlier build/typecheck receipts were excluded from freeze and all selected
validations were recaptured against the stable complete tree. Both bundle
controllers have that same tree; B views still prepare their own dependencies.

| Capture work | Attempts | Sum of command seconds | Sum of CLI seconds |
| --- | ---: | ---: | ---: |
| Selected bundle | 6 | 59.606 | 64.658 |
| Unselected calibration attempts | 3 | 49.671 | 52.477 |
| All attempts | 9 | 109.278 | 117.135 |

These costs are outside native B dispatch-to-completion duration, not hidden as
free workflow savings. They are sums of observed command/CLI work, not total
preparation wall time; freeze, projection, copies and controller reasoning are
additional. Recovery dependency preparation also took 1.385 seconds. The current
test invocation deliberately uses `--no-cache --reporter=dot`, so its timing does
not recreate historical plain `npm test` timing.

The separate wording experiment used five fresh old-guide and five fresh
updated-guide gpt-6-astra/medium samples. All old-guide samples selected fresh
typecheck plus audit; all updated-guide samples selected authenticated typecheck
reuse with provenance plus fresh audit. These hypothetical action selections
are guidance evidence only, not historical execution or timing measurements.

## Evidence index

Private evidence root: `.review/benchmarks/2026-09-12-issue113-ht84d27f/`.
`installed-candidate.json` records all 21 installed module hashes/modes and tested
guide/helper hashes. `recovered-workspace.json` binds the recomputed snapshot and
tool versions; `frozen-bundle.json` identifies bundle
`4702c2b967616664c4cdc5f6f7b6b0c0fa0ee3477018930920dcbf2d5923875a`.
`environment-calibration.json`, `capture-costs.json` and `guidance-summary.json`
support the separate observations above. `comparison.json` records both complete
pairs and confirms unchanged candidate runtime/guide bytes. Each `native/<arm>/` contains
`acceptance.json`, `transcript-audit.json`, `report.observation.json`, unchanged
`report.json`, sealed/stored validation receipts and exact command captures.
The completed transcript audits supersede the earlier pending-audit note inside
acceptance summaries.

| Exact native report | SHA-256 |
| --- | --- |
| independent-normal | `fb74716c53688c645d378e7bef281825db6fa7dbfb8b4d4fa8c46ff1bb48c7f3` |
| bundle-normal | `4085fd2fa305689cf2f7ce3358338b0fa18687966ce4bf6df2d75635d9624258` |
| independent-blocker | `4e4efa63edae16e69e7b2198e47ba01afe30fc284587c68ba281bd4ea55733f0` |
| bundle-blocker | `c99769a9142281087f92c9dc47ea1c570d2921edca3b9d9974c30114b9e7cfc3` |

## Reproducible command observations

Run `scripts/measure-pre-pr-evidence.py` with `/usr/bin/python3`. Supply:

| Argument | Meaning |
| --- | --- |
| `--runtime` | Absolute disposable installed `pre_pr_tribunal` package directory |
| `--view` | Explicit clean historical view root |
| `--artifacts` | Existing private `0700` directory or a new directory under an existing parent |
| `--timeout` | Positive bounded seconds, maximum 3600; default 300 |
| `--case` | One code-owned validation from the table below |
| `--category inspection -- <argv>` | Caller-selected inspection command from the view root |
| `--category verification -- <argv>` | Caller-selected installed verifier command from the view root |

Select Node v22.23.1/npm 10.9.8 on PATH before invocation. This historical helper
uses the explicit contract-1 installed runtime's
`sanitized_environment('node-lock-v1')` and bounded `run_owned`. The wrapper is
identical in both arms and does not grant execution permission or provide a
sandbox. It is not a contract-2 reusable-evidence path. Run helper, installed
runtime and private context files under paths compatible with the existing
home-path policy.

| Case | Command in `mcp-server`, except runtime-scope |
| --- | --- |
| install | `npm ci --ignore-scripts --no-audit --no-fund` |
| audit-prod | `npm audit --omit=dev --json` |
| audit-full | `npm audit --json` |
| build | `npm run build` |
| typecheck | `npm run typecheck` |
| test | `npm test -- --no-cache --reporter=dot` |
| coverage-smoke | `npm test -- --no-cache --reporter=dot --coverage src/__tests__/config.test.ts` |
| mcp-smoke | Fixed SDK Client/StdioClientTransport probe of built `dist/index.js`: initialize, ping, listTools, close |
| runtime-scope | `git diff --name-only <fixed merge-base>..<fixed HEAD> -- mcp-server/src` from root |
| false-typecheck | `npm run typecheck -- --not-a-real-option-issue113` |

The MCP child receives only the non-secret `benchmark-placeholder` Notion key;
it executes no Notion tool. Build locally if `dist` is absent and count that
fresh build. Do not transfer unauthenticated generated outputs into B.

After argv preflight succeeds, each command attempt gets a random private
invocation directory with an initial and then completed `observation.json`.
Explicit requests rejected during argv preflight create no command record;
the transcript audit accounts for those requests separately. The completed
record includes exact argv, category, relative cwd, monotonic start/end/duration,
wall timestamps, actual exit and failure reason. Accepted captures additionally
have exact `stdout.bin` and `stderr.bin`, all regular current-user-owned
non-symlink files explicitly set to `0600`. Rejected/timeout captures have no
report execution. A nonzero ordinary command exit remains actual evidence and
does not itself mean capture rejection. Helper exit 0 means accepted capture;
inspect `execution.exit_code` to judge the validation.

`execution` contains the six legacy execution fields excluding the
reviewer-assigned round-local `id`; these fields pass the installed report
parser. The capture hash is SHA-256(stdout bytes followed by stderr bytes).
These observation records are not native reviewer reports or evidence receipts.
Preserve native terminal response bytes separately, with externally anchored
digests and exact owner/type/mode checks; never rewrite a native report.

Count requested top-level command invocations, not every npm/Node child process.
Separate original-validation, verification and inspection categories; total
commands require transcript checking for bypasses and incomplete observations.
Missing observations or bypasses make accounting incomplete, not zero. Capture
duration and verifier duration are observations; previous capture durations do
not measure saved elapsed. Actual B elapsed/model/effort come from matched native
session observations.

## Whole-repository validation

The current contract-2 candidate worktree passed the unchanged CI dependency
lock on 2026-09-16 after the final process-containment fix: **3908 passed in
854.78s**, exit 0. The exact command was
`rtk proxy env -u LD_LIBRARY_PATH "$TASK_TEST_VENV/bin/python" -m pytest -q`
with the disposable Python 3.10.12 environment populated from
`requirements-test.lock --require-hashes`. This proves the present uncommitted
candidate's repository regression suite; it is not a substitute for the
post-final-commit native comparison required below.

The existing CI dependency lock passed: **3849 passed in 575.32s**, exit 0,
with wrapper elapsed 575773.659957ms. Exact evidence is
`.superpowers/sdd/2026-09-12-reviewer-evidence-bundle/full-suite-ci-lock.log`
and `full-suite-ci-lock.json`. There were no failures or skips, and the passing
output has no warning summary.

The first system-Python/pytest 9.0.3 attempt stopped during collection because
already-declared `slack_bolt` was unavailable; its failed log/metadata were
preserved. The standard venv path lacked ensurepip, so existing uv created a
private Python 3.10.12 environment and installed the unchanged
`requirements-test.lock` with `--require-hashes` (including pytest 9.1.1).
`LD_LIBRARY_PATH` was unset as in `.github/workflows/pytest.yml`. No source,
dependency declaration or global environment changed. Native timing had finished
before this suite ran.

To reproduce, set `TASK_TEST_VENV` to a private disposable directory, then run:

```sh
rtk proxy uv venv --python 3.10.12 "$TASK_TEST_VENV"
rtk proxy uv pip install --python "$TASK_TEST_VENV/bin/python" --require-hashes --requirement requirements-test.lock
rtk proxy env -u LD_LIBRARY_PATH "$TASK_TEST_VENV/bin/python" -m pytest -q
```

## Acceptance status

| Check | Status |
| --- | --- |
| Eligible/reused/claim-cited/fresh/rejected telemetry, unknown vs zero, lifecycle match | Focused local tests |
| Disposable installed CLI capture/freeze/begin/project/verify/A-B-C submit/finalize | Focused local test |
| Exact native-format report bytes and post-seal evidence mutation | Focused local test |
| Helper exact capture, fixed false exit, timeout, path and text rejection | Synthetic local tests |
| Current independent B vs bundle B: normal claim outcomes | Historical contract-1 result; contract-2 rerun pending |
| False required assertion: refutation and blocker identity preservation | Historical contract-1 result; contract-2 rerun pending |
| Command counts, native elapsed and matched model/environment | Historical four-arm audit only; contract-2 measurement pending |
| Local implementation independent task review | Contract-2 code defects resolved; final acceptance re-review pending native comparison |
| Whole repository pytest with unchanged CI lock | Current contract-2 candidate: 3908 passed in 854.78s |
| Scoped four-arm result-document review | Historical contract-1 review only |
| Final document verification and whole-branch review | Pending for contract 2 |
