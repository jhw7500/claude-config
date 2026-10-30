# Tribunal explicit auto-run implementation plan

> **For agentic workers:** Execute the approved issue scope with native bounded implementation and independent review. Use test-driven development for the CLI/state transition changes.

**Goal:** Add a separate explicit choice for automatic blocker fixes, validation, fix commits and re-review, capped at three rounds including the initial review.

**Architecture:** Retain the existing manual one-shot grants and all full-diff review checks. A separate private auto-run record binds the starting snapshot, policy, reviewers, repository/worktree, runtime and round budget. Each automatic transition revalidates the prior verdict, exact target HEAD/diff and declared fix commits under the existing review lock.

**Tech Stack:** Python 3.10, argparse, existing private JSON store, pytest; no new dependencies.

**Spec:** [Issue #258](https://github.com/jhw7500/claude-config/issues/258), selected by the user for implementation on 2026-10-30.

## Global constraints

- Default one-round behavior and manual re-review grants remain unchanged.
- Intensity and automatic execution consent are separate; plain `100` or general continuation is not consent.
- `0=off`, `50=single`, `100=iterative`; automatic execution requires iterative intensity 100 and a new run.
- Maximum three rounds including the first; no budget reset on cancellation, resume or same-snapshot retry.
- Preserve full base-to-HEAD review, exact PASS binding, independent reviewers, exact report bytes and owner-only `0600` file checks.
- Stop at PASS, INCONCLUSIVE, exhaustion, provider failure, unresolved pending/integrity errors, cancellation or scope drift.
- Automatic review consent does not authorize PR creation, merge or deployment. Do not install the candidate into a live runtime while developing it.

## Review focus

- An old pending/manual lifecycle must never acquire auto authority.
- A crash between verdict and auto-run writes must refuse inconsistent continuation and preserve reports.
- A foreign, unlisted commit or changed base/policy must stop before another review starts.
- A rebuttal without a code change must carry new verification evidence; repeating the same failed content is insufficient.
- Revocation, quota and report-integrity errors must preserve the accumulated budget and audit history.

## Task 1: Bound automatic consent and round transitions

**Files:** `hooks/pre_pr_tribunal/auto_run.py`, `verdict_store.py`, `cli.py`, `tests/pre_pr_tribunal/test_auto_run.py`.

**Interfaces:** `auto-run-preview`, `auto-run-grant`, `auto-run-status`, `auto-run-stop`; `begin` gains explicit `--auto-run`, `--expected-head` and repeatable `--fix-commit` arguments.

- [x] Write and run failing tests for explicit consent, FAIL/fix/PASS, manual denial and terminal/error boundaries in both runtimes.
- [x] Implement private locked state, initial binding, exact fix ancestry and per-round audit with fail-closed transitions.
- [x] Integrate begin/finalize without replacing manual grants or existing snapshot/decisions checks.
- [x] Run focused backend tests and existing grant/state regression tests.

## Task 2: Expose the choice in both controller instruction surfaces

**Files:** `skills/pre-pr-tribunal/SKILL.md`, `hooks/pre_pr_tribunal/prompt_hook.py`, `hooks/general-continuation-hook.py`, `hooks/README.md`, `claude-md/guidance/tribunal-review-units.md`, `tests/pre_pr_tribunal/test_prompt_hook.py`, existing skill contract tests as necessary.

- [x] Demonstrate missing automatic-choice guidance in the two runtime hook outputs and general continuation output.
- [x] Add a choice that discloses reviewer set, round limit, scope, stopping rules and measured/unknown cost before consent.
- [x] Document the CLI grant/continuation sequence, focused regression versus final validation, cancellation and immutable evidence.
- [x] Run hook and skill contract tests; confirm generic selection replies create no state.

## Task 3: Verify the integrated change

- [x] Run affected Tribunal tests, then the project test suite with available test dependencies.
- [x] Run syntax checks and `git diff --check`; preserve executable modes.
- [x] Obtain an independent review of approval boundaries, state transitions and instruction consistency, and fix actionable findings.
- [x] Report changed files, fresh verification evidence and any limits. Leave live runtimes unchanged.

## Verification evidence

- Test environment: Python 3.10.12; dependencies installed from `requirements-test.lock` with `uv pip install --require-hashes`.
- Full command: `rtk proxy env -u LD_LIBRARY_PATH PYTEST_XDIST_AUTO_NUM_WORKERS=8 /tmp/claude-config-issue258-tests-upmfpypj/venv/bin/python -m pytest -q -n auto`.
- Before the final review fixes: 4886 passed in 279.64 seconds.
- Final source: **4891 passed in 261.07 seconds**, exit 0; no failures or skips.
- Python 3.10 syntax parsing: all 11 changed/new Python files passed. `git diff --check` passed; tracked executable modes unchanged.
- Independent implementation review found three medium issues (terminal audit mutation, store capacity publication, direct terminal disclosure); all were fixed with observed RED/GREEN regressions and the scoped recheck approved them.
- Instruction QA exercised five default/automatic/failure/resume/exhaustion scenarios.
- Validation used temporary repository and reviewer fixtures. Live Tribunal providers, installed-runtime updates, commit and PR creation were not run.
