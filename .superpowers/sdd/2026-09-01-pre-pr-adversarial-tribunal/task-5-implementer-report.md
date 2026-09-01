# Task 5 Implementer Report

## Scope and outcome

- Base HEAD: `f5667a9bdfaa44c5b1bbc8d34b649dcaad477ee2`
- Implemented the direct PR-create gate, bounded shared payload decoder, and thin Claude/Codex file adapters.
- Added the Task 5 gate/adapter matrix against fake local Git repositories and copied package paths.
- Did not change Task 2–4 behavior, installer files, live runtime configuration, user HOME, network state, or GitHub state.

## TDD evidence

Initial RED, before any production Task 5 file existed:

```text
PYTHONPATH=/tmp/claude-config-issue32-test-deps.YXp69B \
python3 -m pytest -q tests/pre_pr_tribunal/test_gate_adapters.py

ModuleNotFoundError: No module named 'pre_pr_tribunal.gate'
1 error during collection, exit 2
```

Two self-review fail-closed cases also received explicit mutation/RED evidence:

- Removing the adapter evaluation exception boundary exposed the evaluation canary and failed the new test; restoring the boundary passed it.
- Before the direct gate's unexpected-core boundary was added, an injected core exception escaped; the test failed, then passed after the bounded `VERDICT_INVALID` fallback was implemented.

Final Task 5 result: `60 passed`. Final Task 2–5 focused regression: `270 passed`.

## Implemented contracts

- Scanner runs first. Proven unrelated commands produce `NOT_PR_CREATE`; ambiguous executable candidates deny with `COMMAND_AMBIGUOUS` without touching repository or verdict state.
- Direct PR-create evaluation checks exact physical repository root, clean worktree, safe strict verdict read, current snapshot binding, reviewer terminal state, round/gate state, and blocker count in design order.
- Missing, unsafe, invalid, stale, dirty, incomplete, blockers-open, and round-limit states map only to fixed `GateCode` values. A valid current pass returns no adapter decision.
- The gate reuses the existing bounded Git runner through a narrow wrapper; it does not add a subprocess runner, shell execution, network call, `gh` call, or verdict JSON parser.
- Payload reads stop at 1 MiB + 1 byte. Strict JSON rejects duplicate keys, malformed/deep/non-object input, and alias collisions. Claude accepts exact `Bash`; Codex accepts any string tool name when its tool input has a string `command`.
- Copied-package subprocess tests remove repository `PYTHONPATH` and exercise `cli.py`, `claude_hook.py`, and `codex_hook.py` bootstrap behavior.
- Denies are compact UTF-8 JSON plus one newline. Unrelated/pass/invalid-payload paths are silent; both adapters return zero. Payload, command, verdict, absolute-path, exception, and secret canaries are not reflected.

## Contract ruling recorded

The existing strict verdict reader rejects schema-invalid mixed reviewer slots and out-of-range round 4 before a valid `Verdict` exists. Per controller ruling, these map to `VERDICT_INVALID`; the gate does not ad-hoc parse raw verdict JSON to manufacture a finer status. A schema-valid all-pending/in-progress verdict maps to `REVIEW_INCOMPLETE`.

Exact-root and dirty-before-verdict preflight have no Task 2–4 public composite API. Per controller ruling, `gate.py` narrowly reuses the existing bounded private `git_state` helpers rather than introducing a second Git runner. This is the only intentional private-API coupling and is covered by order tests.

## Files

- `hooks/pre_pr_tribunal/gate.py`
- `hooks/pre_pr_tribunal/hook_common.py`
- `hooks/pre_pr_tribunal/claude_hook.py`
- `hooks/pre_pr_tribunal/codex_hook.py`
- `tests/pre_pr_tribunal/test_gate_adapters.py`
- `.superpowers/sdd/2026-09-01-pre-pr-adversarial-tribunal/task-5-implementer-report.md`

## Self-review

- Scope: only requested Task 5 production/test files plus this required report.
- Security: bounded reads/output; descriptor-safe verdict reader remains authoritative; no raw exception or state content reaches output.
- Compatibility: adapters run from copied installed paths; Task 2–4 focused regression remains green.
- Concern: the narrow imports of private `git_state` helpers should be replaced by a public clean-root preflight only if a future task explicitly adds that API; duplicating Git execution here would be riskier and violates the current scope.
