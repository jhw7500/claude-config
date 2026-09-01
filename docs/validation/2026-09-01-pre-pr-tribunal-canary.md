# Pre-PR tribunal canary checkpoint — 2026-09-01

## Status

**BLOCKED — no real-runtime success is claimed.** Real Claude and Codex PASS evidence is still
required before Task 8 can be considered complete. The one-time authorized Fix Round 6 closes two
additional local subscription-auth/security review findings, but controller review is required before
either real runtime is run. No real runtime, provider, or GitHub endpoint was invoked in this
implementation round.

Fix Round 1 strengthened the isolation code and fake-runtime evidence, but it was deliberately not
revalidated against either real runtime because both allowlisted API keys remain absent. The code
correction does not turn this checkpoint into a real-runtime PASS.

Fix Round 2 removed forgeable file ledgers and protected control leaves, but its scoped re-review
found that writable ancestor aliases could still replace those leaves. Fix Round 3 separates the
entire immutable control root from writable runtime state and closes that local boundary. Neither
round was revalidated against a real runtime because the same allowlisted-key blocker remains. The
checkpoint is still AUTH-BLOCKED.

Fix Round 4 removes the earlier API-key-only contradiction. Default auth now securely reads the
caller Claude OAuth credential just in time and stages an exact disposable Codex subscription cache
copy, while masking both live runtime config directories in the sandbox. It never forwards API keys
unless `--auth-source environment` is explicitly selected, never mutates live sources, and never
places credential bytes in the immutable control root/digest. This is implementation evidence only,
not a real-runtime PASS.

Historical caller environment allowlist presence at the original checkpoint was:

- `ANTHROPIC_API_KEY`: absent
- `OPENAI_API_KEY`: absent

No credential value was read or recorded in that original checkpoint. Fix Round 4 tests use only
disposable fixture credentials; no live credential was inspected during implementation.

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

## Fix Round 2 passing local evidence

- Strict review-finding RED: 12 failed and 37 passed. The failures fixed Codex command-less,
  non-shell, and unknown-tool fail-open behavior; writable source aliases for the guard, fake `gh`,
  hosts, hook configs, and installed package; and writable-file call-count forgery. Existing
  failure-fact, auth-precedence, timeout, cap, cleanup, and fake-`gh` contract tests remained green.
- The first implementation run returned 10 failed and 39 passed. The only remaining cause was the
  trusted verdict-builder creating package bytecode between phase hashes. Disabling bytecode writes
  in the harness's internal environment removed that harness-owned mutation without weakening the
  runtime boundary.
- Final fake-runtime harness: 49 passed in 14.30 seconds. Harness plus direct adapters: 120 passed
  in 20.21 seconds. The actual bubblewrap verifier attempts write/unlink/rename through every
  protected worktree source path and proves all protected inode/content hashes remain unchanged.
- Fake-call evidence now terminates in an abstract Unix socket owned by the harness and is counted
  in harness memory. Writable legacy count-shaped files do not affect valid or invalid counts;
  duplicate and malformed fake calls still block.
- Related tribunal, shared-installer, and task-nudge regression: 748 passed in 49.88 seconds.
- Fresh full repository regression: 2571 passed in 149.28 seconds, exit 0.
- Bash syntax, ShellCheck error-level, changed-file Python compilation, Git whitespace check, and
  shared Skill validation all exited 0; the Skill validator reported `Skill is valid!`.
- Neither real runtime was executed, no real GitHub endpoint was contacted, and no credential or
  live runtime config was read. This code correction is not a real-runtime PASS.

## Fix Round 3 passing local evidence

- Initial full focused RED: 5 failed and 47 passed. A subsequent focused selector that made the
  installed Skill-symlink escape explicit returned 6 failed and 47 deselected. The failures showed
  that both runtime configs, every installed hook command, and fake `gh` still had writable-state
  ancestors; only one temporary root was allocated; and the verifier had neither an external
  control-root contract nor a nested-root rejection.
- The complete fake client, hosts source, guard, installed hook package, and both hook configs now
  live below a second owner-private controller root outside runtime work state. Bubblewrap mounts
  that whole root read-only. Runtime PATH, Claude settings/config, Codex home, and installed hook
  commands use only absolute paths into that root. Runtime-irrelevant Skill symlinks are removed
  before whole-root hashing so no control dependency escapes the root.
- The first targeted implementation run returned 4 failed and 2 passed. Rewriting the installer's
  `$HOME` adapter aliases to immutable absolute package paths made the actual boundary verifier
  green. The next run returned 3 failed and 3 passed because strict verdict reads require an
  O_RDWR/flock operation on `.review/lock`. The final boundary exposes only runtime HOME, TMPDIR,
  neutral GitHub config, and an existing pass-phase `.review` directory as writable submounts;
  repository and work-root ancestors remain read-only.
- Final boundary selector: 6 passed and 47 deselected. The verifier attempts creation and rename at
  the control root, fake-bin, runtime-config, and every installed-package ancestor; attempts leaf
  write/unlink/rename; invokes fake `gh` through absolute/PATH-reset/`command -p` paths; invokes the
  exact guard and installed package through both immutable configs; and requires the whole-root
  inode/content digest to remain unchanged. A nested control root fails with
  `ISOLATION_UNAVAILABLE` before runtime dispatch.
- Final fake-runtime harness: 53 passed in 23.85 seconds. Harness plus direct adapters: 124 passed
  in 25.15 seconds. Related tribunal/shared-installer/task-nudge regression: 752 passed in 59.73
  seconds. Fresh full repository regression: 2575 passed in 153.30 seconds, exit 0.
- Bash syntax, ShellCheck error-level, changed-file Python compilation, Git whitespace check, and
  shared Skill validation all exited 0; the Skill validator reported `Skill is valid!`.
- Neither real runtime was executed, no real GitHub endpoint was contacted, and no credential or
  live runtime config was read. Real Claude and Codex PASS evidence is still required.

## Fix Round 4 passing local evidence

- Initial subscription-auth RED: `26 failed, 54 passed in 37.68s`. The failures covered missing
  credential loading/staging, default API-key exposure, unsafe metadata and JSON/expiry cases,
  source preservation, digest exclusion, cleanup, and credential leak classification. Existing
  fake-GitHub isolation, guard, evidence, timeout, cap, and sanitized-output cases stayed green.
- Follow-up adversarial REDs made secret-prefix classification and the post-version refresh path
  explicit. In particular, a Codex version command that wrote and printed a newly rotated staged
  token returned `RUNTIME_UNAVAILABLE` instead of `SENSITIVE_OUTPUT` until captured-exception paths
  reread and classified the disposable stage.
- Final focused fake-runtime harness: `85 passed in 37.17s`. Harness plus direct adapters:
  `156 passed in 43.06s`. Related tribunal/shared-installer/task-nudge regression:
  `784 passed in 67.24s`.
- Fresh full repository regression: `2607 passed in 203.44s`, exit 0.
- Bash syntax, error-level ShellCheck, changed-file Python compilation, Git whitespace validation,
  and shared Skill validation all exited 0. The validator returned `Skill is valid!`.
- Tests use only disposable owner-private Claude/Codex credential fixtures. Neither real runtime
  was executed, no real GitHub endpoint was contacted, and no live credential value was inspected
  or copied. This remains a controller-review checkpoint, not real-runtime PASS evidence.

## Fix Round 5 passing local evidence

- Focused adversarial RED: `21 failed, 7 passed, 77 deselected in 12.39s`. Failures reproduced
  provider-key cross-exposure, short/structured token inventory gaps, the incomplete expiry budget,
  7-byte and escaped-raw-document leaks, zero-exit version derivation/control-breach precedence,
  cap-crossing hashes, and staged-work/control-root leaks after SIGINT/SIGTERM/KeyboardInterrupt.
- Final focused GREEN, including refreshed escaped raw auth and unclassifiable refreshed-stage cap
  coverage: `30 passed, 77 deselected in 14.68s`. Final harness plus direct Claude/Codex adapters:
  `178 passed in 62.48s`. The final complete related `tests/pre_pr_tribunal` suite:
  `570 passed in 95.74s`.
- Final fresh full repository regression: `2629 passed in 204.33s`, exit 0.
- Changed-Python compilation, Git whitespace validation, and the relevant shared Skill validator
  exited 0; the validator returned `Skill is valid!`. No shell file changed, so Bash/ShellCheck
  changed-shell gates were not applicable. Filename-only credential-shaped and live-auth-path scans
  over all changed tracked files plus the ignored report returned zero matches without printing values.
- `OUTPUT_LIMIT` now always publishes both hashes as `WITHHELD`. Credential-backed children also
  withhold capture/version hashes because sub-8-byte prefixes are not safely classifiable. Initial
  and refreshed raw auth documents and JSON-escaped token representations remain bounded in memory;
  under-8-byte or structured token-like fields fail closed.
- Version output is classified after a staged-Codex refresh read and before version/hash derivation
  or control-digest evaluation. Catchable SIGINT/SIGTERM only raise a lifecycle termination signal;
  the outer `finally` performs deadline-bounded process/evidence shutdown and identity-confined,
  idempotent control/work cleanup.
- Explicit environment auth is still opt-in only. Claude children receive only the Anthropic key;
  Codex children receive only the OpenAI key. The centrally derived Claude validity margin is 450
  seconds: 420 seconds of configured child deadlines plus a 30-second scheduling cushion.
- All Round 5 tests use disposable synthetic credentials. No live credential/config was opened,
  no real Claude/Codex process or GitHub/provider endpoint was invoked, and no Step 9/JHW action ran.
  This remains a controller-review checkpoint, not real-runtime PASS evidence.

## Fix Round 6 passing local evidence

- The first strict RED was `6 failed, 107 deselected`: three upper/lower/mixed `\uXXXX` ASCII
  credential cases bypassed sensitivity, work/control allocator `BaseException` left roots, and an
  evidence-recorder constructor failure left its socket open. The expanded signal run contained six
  additional security reproductions for SIGINT/SIGTERM allocation handoff, recorder handoff, and
  mixed cleanup signals; two unrelated over-strict test-only version assertions in that run were
  corrected without changing production policy. Follow-up bounds REDs reproduced three unbalanced or
  cross-stream complexity gaps, and a final RED reproduced mismatched JSON structure handling.
- Final focused Round 6 selector: `33 passed, 107 deselected in 4.60s`. Complete fake-runtime probe
  harness: `140 passed in 51.28s`. Direct Claude/Codex adapters: `71 passed in 5.41s`. Complete
  related `tests/pre_pr_tribunal`: `603 passed in 69.72s`.
- Fresh full repository regression: `2662 passed in 186.82s`, exit 0. Changed-Python compilation,
  Git whitespace validation, and the relevant shared Skill validator exited 0; the validator returned
  `Skill is valid!`. No shell file changed, so Bash/ShellCheck changed-shell gates were not applicable.
- Count-only scanning of added lines found zero reusable credential-shaped literals and zero
  non-fixture credential JSON values. It printed no matched value. No newly added live-auth path
  reference was present.
- SIGINT/SIGTERM are blocked as a pair across work/control/evidence allocation through caller
  ownership publication and across child kill/reap plus final evidence/control/work cleanup. Prior
  handlers and the exact caller mask are restored only afterward; a signal consumed at the cleanup
  transition is replayed, while already-pending signals resume under the restored semantics. Allocator
  and recorder construction also clean locally on `KeyboardInterrupt`/`SystemExit` before publication.
- Credential values and raw auth documents remain the bounded inventory rather than an enumeration of
  JSON spellings. A single lexical pass over both bounded streams decodes every JSON string token,
  including keys, nested/list leaves, outer-encoded raw documents, mixed-case Unicode escapes, and
  valid surrogate pairs. The shared scan stops at 8,192 string/structure items, depth 64, or 128 KiB
  decoded text; malformed, mismatched, or over-bound suspicious JSON fails closed before verdict,
  version/hash, or control-breach precedence.
- Subscription-default/no-fallback, provider-env separation, 450-second Claude validity margin,
  source preservation, immutable controls/fake `gh`/guard, and `OUTPUT_LIMIT` hash withholding remain
  unchanged. `SIGKILL`, `os._exit`, kernel/power loss, and equivalent uncatchable termination remain
  explicit residuals.
- All Round 6 tests use disposable synthetic credentials and roots. No live credential/config was
  opened or mutated; no real Claude/Codex/`gh`, GitHub/provider endpoint, Step 9, JHW command,
  push/merge/PR action, or network probe ran. This remains a controller-review checkpoint, not
  real-runtime PASS evidence.

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
