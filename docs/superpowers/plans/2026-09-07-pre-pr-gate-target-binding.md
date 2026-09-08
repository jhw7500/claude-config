# Pre-PR Gate Target Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a passing Tribunal verdict authorize only a canonical PR command bound to the reviewed repository, base, branch head, and unchanged snapshot.

**Architecture:** Preserve the existing bounded scanner and two-stage gate. The first scan identifies executable candidates and unsafe shell contexts; after reading the verdict, a policy scan requires its literal base and rejects target overrides. The adapters fail closed before decoding any payload larger than 1 MiB.

**Tech Stack:** Python 3 standard library, pytest, GitHub CLI command grammar, Claude/Codex PreToolUse JSON.

**Spec:** `docs/superpowers/specs/2026-09-07-pre-pr-gate-target-binding-design.md`

## Global Constraints

- Change only the five Round 3 HIGH findings and their contracts/tests.
- Add no dependency, permission, environment variable, secret, remote endpoint, wrapper, or release step.
- Keep parser limits at 256 KiB, 4096 tokens, and recursion depth 16.
- Keep hook input limit at exactly 1 MiB.
- Preserve `NO_MATCH` and silent adapter behavior for reliably unrelated in-limit input.
- Require a literal PR base equal to `verdict.base_ref`; forbid repo/head overrides.
- Preserve the exhausted Tribunal evidence intact until it is recoverably archived.

---

### Task 1: Canonical shell context and target policy

**Files:**
- Modify: `hooks/pre_pr_tribunal/shell_scan.py`
- Test: `tests/pre_pr_tribunal/test_shell_scan.py`
- Test: `tests/pre_pr_tribunal/test_gate_adapters.py`

**Interfaces:**
- Consumes: existing `scan_pr_create(command: str) -> ScanResult` and verdict `base_ref`.
- Produces: `scan_pr_create(command: str, *, expected_base: str | None = None) -> ScanResult`; `PR_CREATE` means one safe candidate and, when supplied, an exactly matching explicit base.

- [x] **Step 1: Write failing context tests**

Add literal table cases asserting `AMBIGUOUS_CANDIDATE` for:

```python
[
    "cd ../other && gh pr create --base master",
    "git commit -am stale && gh pr create --base master",
    "gh pr create --base master >result.txt",
    'gh pr create --base master --title "$(git rev-parse HEAD)"',
    "env -C ../other gh pr create --base master",
    "time -o timing.txt gh pr create --base master",
]
```

Name the protected mutation: removing compound/nested/redirection context
tracking would make each test incorrectly return `PR_CREATE`.

- [x] **Step 2: Run context tests and verify RED**

Run: `rtk pytest -q tests/pre_pr_tribunal/test_shell_scan.py -k 'unsafe_context'`

Expected: failures showing existing `PR_CREATE` results.

- [x] **Step 3: Implement minimal context classification**

Make `_scan_parsed_context` distinguish one non-empty simple segment from
compound or nested execution. After `_scan_simple_command` finds a candidate,
raise `ScanFailure("UNSAFE_PR_CONTEXT")` when another segment, a nested command,
or a redirection can execute. Make `_skip_env` reject chdir options for a later
candidate and `_skip_time` reject output-file options for a later candidate.

- [x] **Step 4: Run the context tests and complete scanner suite**

Run: `rtk pytest -q tests/pre_pr_tribunal/test_shell_scan.py`

Expected: all scanner tests pass after updating former compound-command
positive fixtures to the new ambiguous contract.

- [x] **Step 5: Write failing target-binding tests**

Add direct scanner tests using `expected_base="master"` and passing-verdict gate
tests for these literal cases:

```python
allowed = "/usr/bin/gh pr create --base master --fill"
denied = [
    "gh pr create",
    "GH_REPO=other/repo gh pr create --base master",
    "gh -R other/repo pr create --base master",
    "gh pr create --base other",
    "gh pr create --base master --head other:branch",
]
```

Also use `monkeypatch.setenv("GH_REPO", "other/repo")` to prove inherited target
state is denied. The protected mutation is omission of any target source from
the policy scan.

- [x] **Step 6: Run target tests and verify RED**

Run: `rtk pytest -q tests/pre_pr_tribunal/test_shell_scan.py tests/pre_pr_tribunal/test_gate_adapters.py -k 'target_binding or exact_current_terminal_pass'`

Expected: unsafe commands still pass or lack the expected-base interface.

- [x] **Step 7: Implement target policy and gate rescan**

Thread `expected_base` through `_scan_context`, `_scan_parsed_context`,
`_scan_simple_command`, and `_scan_gh`. Require one literal matching base during
the bound scan; reject repo/config/host/head flags, target-changing leading or
`env` assignments, and free-standing dynamic argv. In `gate.py`, check inherited
target-changing variables and rescan the original command against
`verdict.base_ref` before returning PASS; map every unsafe bound result to
`COMMAND_AMBIGUOUS`.

- [x] **Step 8: Run target and full gate/scanner tests**

Run: `rtk pytest -q tests/pre_pr_tribunal/test_shell_scan.py tests/pre_pr_tribunal/test_gate_adapters.py`

Expected: all tests pass.

- [x] **Step 9: Commit canonical envelope**

```bash
rtk git add hooks/pre_pr_tribunal/shell_scan.py hooks/pre_pr_tribunal/gate.py tests/pre_pr_tribunal/test_shell_scan.py tests/pre_pr_tribunal/test_gate_adapters.py
rtk git commit -m "fix: bind PR gate to reviewed target"
```

### Task 2: ANSI-C and GNU env split-string recognition

**Files:**
- Modify: `hooks/pre_pr_tribunal/shell_scan.py`
- Test: `tests/pre_pr_tribunal/test_shell_scan.py`
- Test: `tests/pre_pr_tribunal/test_gate_adapters.py`

**Interfaces:**
- Consumes: bounded `_Word` metadata, `_could_form_pr_create`, and recursive context budgets.
- Produces: no executable ANSI-C or GNU split-string PR candidate may return `NO_MATCH`.

- [x] **Step 1: Write failing alternate-argv tests**

Add literal cases:

```python
[
    "g$'\\x68' p$'\\x72' c$'\\x72'eate --base master",
    "bash -c $'g\\x68 pr create --base master'",
    "env -S 'gh pr create --base master'",
    "env --split-string='gh pr create --base master'",
]
```

Assert each result is `AMBIGUOUS_CANDIDATE` or `PR_CREATE`, never `NO_MATCH`.
The protected mutations are the current literal-only `pr/create` check and the
absence of split-string parsing.

- [x] **Step 2: Run alternate-argv tests and verify RED**

Run: `rtk pytest -q tests/pre_pr_tribunal/test_shell_scan.py -k 'alternate_argv'`

Expected: four `NO_MATCH` failures.

- [x] **Step 3: Implement minimal fail-closed recognition**

Use `_could_form_pr_create` when the executable is an ANSI-C dynamic word, mark
a dynamic shell `-c` script with a possible candidate ambiguous, and recognize
`-S`, `--split-string`, and their attached value forms in `_skip_env`. Scan the
split operand with the shared parser budget and raise a bounded split-string
failure when it constructs a candidate.

- [x] **Step 4: Run scanner and gate suites**

Run: `rtk pytest -q tests/pre_pr_tribunal/test_shell_scan.py tests/pre_pr_tribunal/test_gate_adapters.py`

Expected: all tests pass, including unrelated quoted data and limit cases.

- [x] **Step 5: Commit alternate argv fixes**

```bash
rtk git add hooks/pre_pr_tribunal/shell_scan.py tests/pre_pr_tribunal/test_shell_scan.py tests/pre_pr_tribunal/test_gate_adapters.py
rtk git commit -m "fix: close alternate PR argv bypasses"
```

### Task 3: Oversized payload fail-closed boundary

**Files:**
- Modify: `hooks/pre_pr_tribunal/hook_common.py`
- Test: `tests/pre_pr_tribunal/test_gate_adapters.py`

**Interfaces:**
- Consumes: `MAX_STDIN_BYTES` and `deny_output(GateCode.COMMAND_AMBIGUOUS)`.
- Produces: an adapter-native bounded deny for every input larger than exactly 1 MiB.

- [x] **Step 1: Replace the oversized silent expectation with boundary tests**

Build a valid request whose serialized byte length is exactly
`MAX_PAYLOAD_BYTES` and assert normal gate handling. Add one byte and assert
both copied adapters emit a deny containing `COMMAND_AMBIGUOUS`, with no input
canary reflected in stdout or stderr.

- [x] **Step 2: Run the boundary tests and verify RED**

Run: `rtk pytest -q tests/pre_pr_tribunal/test_gate_adapters.py -k 'payload_byte_boundary'`

Expected: the over-limit case has empty stdout.

- [x] **Step 3: Implement early oversized denial**

In `adapter_main`, test `len(raw) > MAX_STDIN_BYTES` immediately after the
bounded read. Emit `deny_output(GateCode.COMMAND_AMBIGUOUS)` through the same
compact JSON path used by other denials, without decoding or reflecting `raw`.

- [x] **Step 4: Run adapter and Tribunal tests**

Run: `rtk pytest -q tests/pre_pr_tribunal/test_gate_adapters.py tests/pre_pr_tribunal`

Expected: all Tribunal tests pass.

- [x] **Step 5: Commit payload boundary**

```bash
rtk git add hooks/pre_pr_tribunal/hook_common.py tests/pre_pr_tribunal/test_gate_adapters.py
rtk git commit -m "fix: deny oversized tribunal hook payloads"
```

### Task 4: Contract alignment and verification

**Files:**
- Modify: `README.md`
- Modify: `hooks/README.md`
- Modify: `docs/superpowers/specs/2026-09-01-pre-pr-adversarial-tribunal-design.md`
- Modify: `docs/validation/2026-09-01-pre-pr-tribunal-canary.md`
- Modify: `docs/superpowers/specs/2026-09-07-pre-pr-gate-target-binding-design.md`
- Modify: `docs/superpowers/plans/2026-09-07-pre-pr-gate-target-binding.md`

**Interfaces:**
- Consumes: the implemented canonical command and adapter behavior.
- Produces: one consistent operator contract and reproducible validation record.

- [x] **Step 1: Align command examples and behavior tables**

Replace pass-path examples with `/usr/bin/gh pr create --base master`. State that
compound/prelude commands, repository/head overrides, dynamic target argv, and
oversized payloads receive bounded denial. Keep unrelated in-limit input silent.

- [x] **Step 2: Run contract and focused suites**

Run: `rtk pytest -q tests/pre_pr_tribunal/test_skill_contract.py tests/pre_pr_tribunal/test_shell_scan.py tests/pre_pr_tribunal/test_gate_adapters.py`

Expected: all tests pass.

- [x] **Step 3: Run repository verification**

Run the repository's complete pytest command after installing only its declared
test requirements if collection reports a missing declared package. Then rerun
the known intermittent timeout case separately if it alone fails, recording
both outputs without changing unrelated production code.

Expected: a clean complete run, or a separately demonstrated pre-existing
environmental/intermittent failure with all Tribunal suites green.

- [x] **Step 4: Commit documentation and validation record**

```bash
rtk git add README.md hooks/README.md docs/superpowers/specs/2026-09-01-pre-pr-adversarial-tribunal-design.md docs/validation/2026-09-01-pre-pr-tribunal-canary.md docs/superpowers/specs/2026-09-07-pre-pr-gate-target-binding-design.md docs/superpowers/plans/2026-09-07-pre-pr-gate-target-binding.md
rtk git commit -m "docs: define canonical tribunal PR command"
```

- [ ] **Step 5: Archive exhausted evidence and run a fresh Tribunal**

Move the complete ignored `.review` directory to a mode-0700 archive created
with `mktemp -d` outside the worktree; verify the archived verdict and inbox are
present before starting a new round 1. Follow the installed pre-pr-tribunal
skill with exactly three isolated native reviewers.

Expected: the old Round 3 FAIL remains recoverable and the new verdict is bound
to the final clean HEAD and diff SHA-256.

- [ ] **Step 6: Push and create the PR only on PASS**

Run the canonical command with the new verdict base:

```bash
rtk git push -u origin task/da281cdf8e81-jhw7500-claude-config-32
rtk /usr/bin/gh pr create --base master --fill
```

Expected: a PR URL only after the gate reports PASS. On FAIL, stop with the new
blockers and do not invoke `gh pr create`.
