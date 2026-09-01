# Pre-PR tribunal canary checkpoint — 2026-09-01

## Status

**BLOCKED — no real-runtime success is claimed.** The fake-runtime harness and repository
regressions pass, but the isolated Claude canary could not authenticate from the temporary
runtime configuration. Real Claude and Codex PASS evidence is still required before Task 8 can
be considered complete. Execution stopped before Codex; no fallback to live runtime config,
credential files, or real `gh` was attempted.

Fix Round 1 strengthened the isolation code and fake-runtime evidence, but it was deliberately not
revalidated against either real runtime because both allowlisted API keys remain absent. The code
correction does not turn this checkpoint into a real-runtime PASS.

Caller environment allowlist presence at the checkpoint was:

- `ANTHROPIC_API_KEY`: absent
- `OPENAI_API_KEY`: absent

No credential value was read or recorded.

## Initial checkpoint passing evidence

- Carried HOME-delimiter breaker: the new full-parser RED reproduced eight direct-path bypasses;
  the focused endpoint/direct/post-delimiter matrix is now 28 passed and the complete model/store
  module is 140 passed.
- Probe harness initial RED: 15 failures while the probe script was absent.
- Fake runtimes plus direct adapters: 90 passed in 10.01 seconds. Both runtimes read the installed
  hook configuration, require missing-verdict deny/count 0 and pass-verdict allow/count 1, and
  independently require the generated fake `gh` to be the first PATH resolution.
- Related tribunal installer, shared installer, integration, model, and task-nudge regression:
  462 passed in 18.16 seconds.
- Full repository regression: 2541 passed in 172.24 seconds, exit 0.
- Bash syntax, ShellCheck error-level, Python compilation, Git whitespace check, and shared Skill
  validation all exited 0; the Skill validator reported `Skill is valid!`.

## Fix Round 1 passing local evidence

- Strict RED after the review findings: 25 failed and 18 passed. The failures covered missing
  bwrap enforcement, system-`gh`/hook adversaries, exact fake-call validation, and incomplete
  sanitized failure facts; the legacy cap, timeout, work-directory, and raw-output checks stayed
  green.
- The actual bubblewrap verifier and stable missing-bubblewrap classification selector is 2 passed.
  Inside the verified boundary it exercises read-only root/disposable writes, absolute system
  `gh`, PATH reset, `command -p`, exact/nonexact guard payloads, loopback GitHub resolution, and a
  loopback client attempt. Provider networking remains shared and is not claimed as isolated.
- Fake-runtime harness: 43 passed. Harness plus direct gate adapters: 114 passed in 28.75 seconds.
  Fake `gh` requires exact argv, exact repository cwd, and one valid call; wrong or duplicate calls
  use a separate invalid ledger and block.
- Failure reports retain phase, parse-valid/denied state, valid and invalid call counts, exit class,
  capture hashes, and coarse sensitivity categories. High-risk literal leakage remains
  `SENSITIVE_OUTPUT`; an auth-marked nonzero with only disposable/generic path metadata remains
  `AUTH_UNAVAILABLE` with separate sensitivity facts.
- Related tribunal, shared installer, and task-nudge regression: 742 passed in 80.38 seconds.
- Fresh full repository regression: 2565 passed in 160.19 seconds, exit 0.
- Bash syntax, ShellCheck error-level, Python compilation, Git whitespace check, and shared Skill
  validation all exited 0; the Skill validator reported `Skill is valid!`.
- No real Claude/Codex process, real GitHub endpoint, live config, or credential file was used in
  this fix round.

## Original Claude canary result

The probe returned process exit 1 and this sanitized result for `claude/2.1.252`:

- overall status: `BLOCKED`
- runtime status: `SENSITIVE_OUTPUT`
- phase: `missing_verdict`
- exit class: `NONZERO`
- stdout SHA-256: `795fdf37c50bac468fbe69dc7616c949fadb3d8a070b7c45e8aa6a7c62876531`
- stderr SHA-256: `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- version-capture SHA-256: `075f89d9e8ba9627a8b065859efe408d9e6ad287d5b48efd0610f6cab692f870`

No raw child output, prompt, environment, credential, or absolute path is retained here.

## Controller-authorized coarse diagnosis

One Claude-only diagnostic rerun retained the original fail-closed matchers and emitted booleans
only. The child returned numeric exit 1 with exit class `NONZERO`; stdout was valid JSON, contained
an authentication marker and disposable probe-home/repository/work-directory paths, contained no
deny event, and fake-`gh` count remained 0. It did not contain `TOKEN_CANARY`, caller HOME, the
source repository path, fake-bin path, or an exact API-key value. Stderr was empty and matched none
of those categories.

The diagnostic instrumentation was removed immediately. The original fake harness/direct-adapter
suite passed again afterward. The path/auth/sensitivity contract was not weakened, Codex was not
run, and this checkpoint does not satisfy the required real-runtime PASS gate.
