# jhw-control host launcher v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the secure-store-only host launcher with checkout-resolver error projection and a strictly validated `task finish` command, while closing the installer PATH escape and aligning producer guidance with the simplified Task workflow.

**Architecture:** Keep the Python launcher as a closed command supervisor: it resolves credentials only from the three approved stores, runs a hidden preflight before each lifecycle mutation, and projects bounded command-specific envelopes. Version 3 is additive over the v2 `task start` projection and adds only `task finish`; the Bash installer keeps `/usr/bin:/bin` for its complete lifetime and discovers RTK by direct executable probes rather than command lookup.

**Tech Stack:** Python 3.10 standard library, Bash, pytest, shellcheck.

**Spec:** `jhw7500/jhw-notion@09d6f19:docs/superpowers/specs/2026-08-26-task-start-checkout-resolution-design.md`; related local launcher design: `docs/superpowers/specs/2026-08-26-jhw-control-host-design.md`.

## Global Constraints

- Every command in this environment starts with `rtk`; run producer commands from the `claude-config` worktree root.
- Never read, print, log, migrate, or fall back to credential values. Project and Notion credentials remain Secret Service-only; the repository credential remains GitHub CLI keyring-only.
- Contract v3 commands are exactly `unlock`, `preflight`, `portfolio status`, `task start`, and `task finish`; do not expose another lifecycle command or a generic proxy.
- Preserve the v2 `task start` success projection and explicit caller-coordinate binding byte-for-byte. Resolver starts use the exact pair `--resolve-from-checkout true`.
- Both `task start` and `task finish` run the same hidden preflight before mutation. A failed preflight returns without invoking the requested lifecycle command.
- Output remains one duplicate-free JSON document, stdout-only on success, stderr-only on failure, bounded to 12 KiB, path-safe, and scanned for credential/protected-path canaries before projection.
- Finish success binds the requested Task, Claim, and status. Finish reasons are projected only through the code-specific closed map from the approved spec.
- `install.sh` keeps `PATH=/usr/bin:/bin` until exit and never executes a user-local helper to detect its presence.
- Producer v3 must pass focused/full gates and be reinstalled before the consumer resolver is activated.

## Planned File Structure

```text
scripts/jhw-control-host.py       v3 command, success, error, and hidden-preflight contract
tests/test_jhw_control_host.py    isolated launcher and producer-guidance contract tests
install.sh                        fixed system-PATH installer and direct RTK discovery
tests/test_installer_private_config.py
                                  temporary-HOME installer poison tests
claude-md/global-guidance.md      global Task nudge and secure lifecycle entrypoints
README.md                         operator provisioning, contract, rollout, and recovery guide
```

---

### Task 1: Preserve checkout-resolver Task start errors

**Files:**
- Modify: `scripts/jhw-control-host.py:84-111`
- Modify: `tests/test_jhw_control_host.py`

**Interfaces:**
- Consumes: existing `_allowed_invocation()`, `_validate_task_start_result()`, and `_validate_error_result()` contracts.
- Produces: `task start` safe projection for `PROJECT_REPOSITORY_NOT_FOUND` and `PROJECT_REPOSITORY_AMBIGUOUS`, both with exit 1.

- [ ] **Step 1: Add failing resolver-forwarding and error tests**

Add a dedicated test for both complete registration shapes. The fake runner uses exact tuple lookup, so install the existing raw downstream Task-start envelope under each new resolver argv before calling `run_secure()`:

```python
@pytest.mark.parametrize(
    "argv",
    [
        [
            "task", "start", "--resolve-from-checkout", "true",
            "--repo-path", "/fixture/source",
            "--issue-url", "https://github.com/example/control/issues/28",
            "--issue-node-id", "I_kwDOControl28",
            "--issue-revision", "issue-revision-28",
            "--session", "codex-resolved-formal",
        ],
        [
            "task", "start", "--resolve-from-checkout", "true",
            "--repo-path", "/fixture/source",
            "--temp-alias", "control-resolver",
            "--goal", "resolve checkout coordinates",
            "--done", "unique Project selected",
            "--scope", "Task registration",
            "--session", "codex-resolved-temporary",
        ],
    ],
)
def test_resolver_start_forwards_complete_registration_argv(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
) -> None:
    runner = FakeCommandRunner(launcher)
    raw_upstream = runner.control_results[
        ("task", "start", "--issue", "https://example.test/issues/28")
    ]
    runner.control_results[tuple(argv)] = raw_upstream

    result = run_secure(launcher, tmp_path, argv, runner)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "command": "task start",
        "result": {
            "branch": TASK_BRANCH,
            "claim_id": CLAIM_ID,
            "task_id": TASK_ID,
            "worktree_ref": WORKTREE_REF,
        },
    }
    assert [call["argv"][2:] for call in runner.calls[2:]] == [
        ("preflight",), tuple(argv),
    ]
```

Extend the reachable command-error table with these exact cases:

```python
(
    ["task", "start", "--resolve-from-checkout", "true"],
    "PROJECT_REPOSITORY_NOT_FOUND",
    1,
),
(
    ["task", "start", "--resolve-from-checkout", "true"],
    "PROJECT_REPOSITORY_AMBIGUOUS",
    1,
),
```

Keep the existing explicit `--project`/`--repo-id` mismatch tests unchanged so optional caller binding remains covered.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'resolver or reachable_command_errors or allowed_commands'`

Expected: the two new downstream codes are converted to `CONTROL_OUTPUT_INVALID`; resolver success forwarding already reaches the v2 projection.

- [ ] **Step 3: Add only the two stable codes**

```python
"task start": COMMON_CONTROL_ERROR_CODES
| {
    # existing task-start codes stay unchanged
    "PROJECT_REPOSITORY_NOT_FOUND",
    "PROJECT_REPOSITORY_AMBIGUOUS",
},
```

Do not parse the resolver flag in the launcher and do not require caller Project/Repository IDs; downstream owns coordinate resolution and `_validate_task_start_result()` validates the returned Task/Claim relationship.

- [ ] **Step 4: Run the focused launcher suite**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py`

Expected: all launcher tests PASS.

- [ ] **Step 5: Commit the resolver projection**

```bash
rtk git add scripts/jhw-control-host.py tests/test_jhw_control_host.py
rtk git commit -m "feat: accept resolver task-start errors"
```

---

### Task 2: Add the strictly projected v3 Task finish command

**Files:**
- Modify: `scripts/jhw-control-host.py:40-111,299-304,1109-1117,1370-1534,1641-1710`
- Modify: `tests/test_jhw_control_host.py`

**Interfaces:**
- Consumes: `COMMON_CONTROL_ERROR_CODES`, `_requested_id()`, `_timestamp()`, `_exact_object()`, `_program_result()`, and the existing hidden-preflight path.
- Produces: `CONTRACT["version"] == 3`, `_validate_task_finish_result(value, request)`, `TASK_FINISH_ERROR_REASONS`, and safe `task finish` forwarding.

- [ ] **Step 1: Pin the v3 contract and allowlist with failing tests**

Change the expected contract fixture to:

```python
{
    "commands": ["unlock", "preflight", "portfolio status", "task start", "task finish"],
    "credential_policy": "secure-store-only",
    "name": "jhw-control-host",
    "version": 3,
}
```

Add a test proving `task finish` reaches no provider when an earlier hidden preflight fails, and reaches exactly `preflight` then `task finish` when it succeeds. Keep `task status`, `task recover`, `task handoff`, and arbitrary commands in the provider-before-rejection table.

- [ ] **Step 2: Pin strict finish success envelopes with failing tests**

Use the existing canonical IDs and add these success cases:

```python
FINISH_REQUEST = (
    "task", "finish", "--task", TASK_ID, "--claim", CLAIM_ID,
    "--status", "handoff", "--validation", "pytest: pass",
)
FINISH_RESULT = {
    "task_id": TASK_ID,
    "claim_id": CLAIM_ID,
    "status": "handoff",
    "released_at": "2026-08-26T00:00:00Z",
    "worktree_removed": False,
    "handoff_pointer": f"handoffs/{TASK_ID}/{CLAIM_ID}.md",
}
```

Test completed and abandoned results without `handoff_pointer`; test exact `cleanup_error: "WORKTREE_CLEANUP_FAILED"` only when `worktree_removed` is false. Parameterize wrong Task, Claim, status, timestamp, boolean, absolute/other-generation Handoff pointer, arbitrary cleanup text, missing field, and extra field; every malformed case must return `CONTROL_OUTPUT_INVALID` without its payload.

For a valid finish success, add both supported outer warnings and assert neither the canonical result nor either warning is dropped:

```python
def test_task_finish_success_preserves_outer_warnings(launcher: ModuleType, tmp_path: Path) -> None:
    runner = FakeCommandRunner(launcher)
    payload = {
        "command": "task finish",
        "result": FINISH_RESULT,
        "journal_warning": {"code": "JOURNAL_WRITE_FAILED"},
        "registration_record_warning": {"code": "REGISTRATION_RECORD_UNWRITABLE"},
    }
    runner.control_results[FINISH_REQUEST] = launcher.CommandResult(
        0, json.dumps(payload, separators=(",", ":")).encode() + b"\n", b"",
    )
    result = run_secure(launcher, tmp_path, list(FINISH_REQUEST), runner)
    assert json.loads(result.stdout) == {
        "command": "task finish",
        "journal_warning": {"code": "JOURNAL_WRITE_FAILED"},
        "registration_record_warning": {"code": "REGISTRATION_RECORD_UNWRITABLE"},
        "result": FINISH_RESULT,
    }
```

- [ ] **Step 3: Pin the closed finish error/reason map with failing tests**

```python
TASK_FINISH_ERROR_REASONS = {
    "HANDOFF_RETRY_CONFLICT": frozenset({
        "invalid_git_state_line", "duplicate_git_state_key",
        "unexpected_git_state_key", "missing_git_state_key",
        "invalid_git_state_count", "missing_git_identity",
        "invalid_dirty_digest", "legacy_dirty_evidence_ambiguous",
        "git_identity_changed", "dirty_delta_changed",
        "handoff_metadata_mismatch", "retry_fields_changed",
    }),
    "INVALID_WORKTREE_INSPECTION": frozenset({"duplicate_dirty_files"}),
    "WORKTREE_DIRTY": frozenset({"handoff_copy_not_plain_file"}),
}
```

For every pair, assert exit 1 and exact `{code, reason}` projection. Assert an unknown reason, a registered reason paired with the wrong code, `reason` on another code, an unknown error code, `conflicting_claim`, and `retained_claim` all fail as `CONTROL_OUTPUT_INVALID`.

Pin the complete command-specific literal set independently of the implementation. Keep the groups aligned with the real server boundaries: finish preconditions, Handoff validation, and worktree inspection/release. The fake runner below proves launcher projection only; this grouped map is the auditable server call-site inventory.

```python
TASK_FINISH_CODES_BY_CALL_SITE = {
    "finish_preconditions": frozenset({
        "CLAIM_MISMATCH", "CLAIM_NOT_FOUND", "HOST_MISMATCH", "TASK_COMPLETED",
        "INVALID_CLOCK", "SOURCE_REVISION_MISMATCH", "INVALID_FINISH_OUTCOME",
    }),
    "handoff_validation": frozenset({
        "HANDOFF_MISSING", "HANDOFF_RETRY_CONFLICT", "INVALID_HANDOFF_EVIDENCE",
        "UNSAFE_HANDOFF_PATH",
    }),
    "worktree_inspection_and_release": frozenset({
        "INVALID_WORKTREE_INSPECTION", "WORKTREE_DIRTY", "WORKTREE_NOT_MAPPED",
        "WORKTREE_REMOVE_PENDING", "WORKTREE_REMOVED", "WORKTREE_PLAN_MISMATCH",
        "WORKTREE_CLAIM_MISMATCH", "WORKTREE_MAPPING_MISMATCH",
        "WORKTREE_BRANCH_MISMATCH", "WORKTREE_REPOSITORY_MISMATCH",
        "WORKTREE_CREATE_PENDING", "INVALID_WORKTREE_STATE", "INVALID_GIT_STATE",
        "INVALID_REPOSITORY_PATH", "UNSAFE_WORKTREE_PATH", "UNSAFE_WORKTREE_ROOT",
        "UNSAFE_STATE_PATH", "MUTATION_PATH_MISMATCH",
    }),
}
EXPECTED_TASK_FINISH_SPECIFIC_CODES = frozenset().union(
    *TASK_FINISH_CODES_BY_CALL_SITE.values()
)

def test_task_finish_error_allowlist_is_exact(launcher: ModuleType) -> None:
    assert launcher.COMMAND_CONTROL_ERROR_CODES["task finish"] == (
        launcher.COMMON_CONTROL_ERROR_CODES | EXPECTED_TASK_FINISH_SPECIFIC_CODES
    )

@pytest.mark.parametrize("code", sorted(EXPECTED_TASK_FINISH_SPECIFIC_CODES))
def test_every_task_finish_specific_error_projects(
    launcher: ModuleType,
    tmp_path: Path,
    code: str,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.control_results[FINISH_REQUEST] = launcher.CommandResult(
        4 if code in {"CLAIM_MISMATCH", "CLAIM_NOT_FOUND"} else 1,
        b"",
        json.dumps({"error": {"code": code}}, separators=(",", ":")).encode() + b"\n",
    )
    result = run_secure(launcher, tmp_path, list(FINISH_REQUEST), runner)
    assert result.returncode == (4 if code in {"CLAIM_MISMATCH", "CLAIM_NOT_FOUND"} else 1)
    assert json.loads(result.stderr) == {"error": {"code": code}}
```

For a valid finish error, also add both supported outer warnings and assert the error code/reason and warnings survive exactly:

```python
def test_task_finish_error_preserves_outer_warnings(launcher: ModuleType, tmp_path: Path) -> None:
    runner = FakeCommandRunner(launcher)
    payload = {
        "error": {"code": "WORKTREE_DIRTY", "reason": "handoff_copy_not_plain_file"},
        "journal_warning": {"code": "JOURNAL_WRITE_FAILED"},
        "registration_record_warning": {"code": "REGISTRATION_RECORD_UNREADABLE"},
    }
    runner.control_results[FINISH_REQUEST] = launcher.CommandResult(
        1, b"", json.dumps(payload, separators=(",", ":")).encode() + b"\n",
    )
    result = run_secure(launcher, tmp_path, list(FINISH_REQUEST), runner)
    assert result.returncode == 1
    assert json.loads(result.stderr) == payload
```

- [ ] **Step 4: Run the new finish tests and verify RED**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'contract or finish or hidden_preflight or non_allowlisted'`

Expected: `task finish` is rejected by `_allowed_invocation()` and contract version remains 2.

- [ ] **Step 5: Implement the command-specific finish contract**

Define a separate literal set rather than reusing the start set:

```python
COMMAND_CONTROL_ERROR_CODES["task finish"] = COMMON_CONTROL_ERROR_CODES | {
    "CLAIM_MISMATCH", "CLAIM_NOT_FOUND", "HOST_MISMATCH", "TASK_COMPLETED",
    "INVALID_CLOCK", "SOURCE_REVISION_MISMATCH", "INVALID_FINISH_OUTCOME",
    "HANDOFF_MISSING", "HANDOFF_RETRY_CONFLICT", "INVALID_HANDOFF_EVIDENCE",
    "UNSAFE_HANDOFF_PATH", "INVALID_WORKTREE_INSPECTION", "WORKTREE_DIRTY",
    "WORKTREE_NOT_MAPPED", "WORKTREE_REMOVE_PENDING", "WORKTREE_REMOVED",
    "WORKTREE_PLAN_MISMATCH", "WORKTREE_CLAIM_MISMATCH",
    "WORKTREE_MAPPING_MISMATCH", "WORKTREE_BRANCH_MISMATCH",
    "WORKTREE_REPOSITORY_MISMATCH", "WORKTREE_CREATE_PENDING",
    "INVALID_WORKTREE_STATE", "INVALID_GIT_STATE", "INVALID_REPOSITORY_PATH",
    "UNSAFE_WORKTREE_PATH", "UNSAFE_WORKTREE_ROOT", "UNSAFE_STATE_PATH",
    "MUTATION_PATH_MISMATCH",
}
```

Add `task finish` to `_allowed_invocation()` and expand the hidden-preflight condition to:

```python
if tuple(argv[:2]) in {("task", "start"), ("task", "finish")}:
    preflight = _program_result(
        _control_call(runner, selected_tools, ("preflight",), child_env),
        command=("preflight",),
        credentials=credentials,
        protected_paths=protected_paths,
        build_host=config["JHW_BUILD_HOST"],
    )
    if preflight.returncode != 0:
        return preflight
```

- [ ] **Step 6: Implement strict success projection**

```python
def _validate_task_finish_result(value: object, *, request: Sequence[str]) -> dict[str, object]:
    result = _exact_object(
        value,
        {"task_id", "claim_id", "status", "released_at", "worktree_removed"},
        {"cleanup_error", "handoff_pointer"},
    )
    task_id = _canonical_id(result["task_id"], TASK_ID_RE)
    claim_id = _canonical_id(result["claim_id"], CLAIM_ID_RE)
    requested_task = _requested_id(request, "--task", TASK_ID_RE)
    requested_claim = _requested_id(request, "--claim", CLAIM_ID_RE)
    requested_status = _requested_literal(request, "--status", {"completed", "handoff", "abandoned"})
    if (task_id, claim_id, result["status"]) != (requested_task, requested_claim, requested_status):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    released_at = _timestamp(result["released_at"])
    removed = result["worktree_removed"]
    if not isinstance(removed, bool):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    cleanup = result.get("cleanup_error")
    pointer = result.get("handoff_pointer")
    projected: dict[str, object] = {
        "task_id": task_id,
        "claim_id": claim_id,
        "status": requested_status,
        "released_at": released_at,
        "worktree_removed": removed,
    }
    if requested_status == "handoff":
        expected_pointer = f"handoffs/{task_id}/{claim_id}.md"
        if removed or cleanup is not None or pointer != expected_pointer:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        projected["handoff_pointer"] = expected_pointer
    else:
        if pointer is not None:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        if cleanup is None:
            if not removed:
                raise LauncherError("CONTROL_OUTPUT_INVALID")
        else:
            if cleanup != "WORKTREE_CLEANUP_FAILED" or removed:
                raise LauncherError("CONTROL_OUTPUT_INVALID")
            projected["cleanup_error"] = cleanup
    return projected
```

Add `_requested_literal()` beside `_requested_id()`. For handoff, require `worktree_removed is False`, no cleanup error, and the exact relative pointer. For completed/abandoned, reject a Handoff pointer; accept no cleanup or exact `WORKTREE_CLEANUP_FAILED` with `worktree_removed is False`.

```python
def _requested_literal(request: Sequence[str], flag: str, allowed: set[str]) -> str:
    positions = [index for index, value in enumerate(request) if value == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(request):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    selected = _bounded_text(request[positions[0] + 1], maximum=32)
    if selected not in allowed:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return selected
```

- [ ] **Step 7: Implement code-bound reason projection**

Define the exact `TASK_FINISH_ERROR_REASONS` mapping from Step 3 at module scope. Change `_validate_error_result()` so `reason` is accepted only for `command == "task finish"` and exact membership in `TASK_FINISH_ERROR_REASONS[code]`; preserve the current rejection for all start/preflight/portfolio errors. Finish never accepts conflict or retained-claim subobjects.

```python
if "reason" in error:
    reason = error["reason"]
    allowed_reasons = TASK_FINISH_ERROR_REASONS.get(str(code), frozenset())
    if command != "task finish" or not isinstance(reason, str) or reason not in allowed_reasons:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    projected["reason"] = reason
```

- [ ] **Step 8: Bump the contract and dispatch finish success**

```python
CONTRACT = {
    "commands": ["unlock", "preflight", "portfolio status", "task start", "task finish"],
    "credential_policy": "secure-store-only",
    "name": "jhw-control-host",
    "version": 3,
}
```

Dispatch `expected == "task finish"` to `_validate_task_finish_result()`. Keep warning projection, byte bounds, canary scans, and exit-code verification shared with existing commands.

- [ ] **Step 9: Run launcher tests and static validation**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py`

Run: `rtk python3 -m py_compile scripts/jhw-control-host.py`

Expected: all tests PASS and compilation exits 0.

- [ ] **Step 10: Commit launcher v3**

```bash
rtk git add scripts/jhw-control-host.py tests/test_jhw_control_host.py
rtk git commit -m "feat: add secure task-finish launcher"
```

---

### Task 3: Keep installer command selection on the system PATH

**Files:**
- Modify: `install.sh:1-20,82-88,130-145`
- Modify: `tests/test_installer_private_config.py`

**Interfaces:**
- Consumes: `PATH=/usr/bin:/bin`, `assert_trusted_command_path()`, and private path-chain helpers.
- Produces: direct, non-executing RTK presence detection while all installer commands continue to resolve from the fixed system PATH.

- [ ] **Step 1: Write executable-leaf and symlink-leaf poison regression tests**

Parameterize `entry_kind` as `"executable"` and `"symlink"`. Create `$HOME/.local/bin` with mode `0700`; for the executable case place logging delegators named `rtk`, `install`, `mv`, `ln`, `python3`, and `basename` directly in it. For the symlink case create those executable delegators under `tmp_path / "delegators"` and place same-named symlinks in `$HOME/.local/bin`. Every delegator appends its name to `PATH_CANARY_LOG` before invoking the corresponding system binary; the `rtk` delegator exits 97 if executed. Run the installer with a clean ambient PATH and assert:

```python
assert result.returncode == 0, result.stderr
assert "@RTK.md" in (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
assert not canary_log.exists()
assert installed.read_bytes() == (REPO / "scripts/jhw-control-host.py").read_bytes()
```

- [ ] **Step 2: Run the installer poison tests and verify RED**

Run: `rtk python3 -m pytest -q tests/test_installer_private_config.py -k 'path_canaries or rtk_detection'`

Expected: at least one user-local executable or symlink canary is executed after the current PATH prepend.

- [ ] **Step 3: Remove the PATH escape and probe RTK directly**

Delete the `$HOME/.local/bin` PATH prepend/export. Replace `command -v rtk` with non-executing probes:

```bash
RTK_AVAILABLE=0
for RTK_CANDIDATE in "$HOME/.local/bin/rtk" /usr/local/bin/rtk /usr/bin/rtk; do
  if [ -x "$RTK_CANDIDATE" ] && [ ! -d "$RTK_CANDIDATE" ]; then
    RTK_AVAILABLE=1
    break
  fi
done
if [ "$RTK_AVAILABLE" -eq 1 ]; then
  install_doc "$REPO_DIR/claude-md/RTK.md" ~/.claude/RTK.md
  IMPORTS="$IMPORTS
@RTK.md"; RTK="있음"
else
  rm -f ~/.claude/RTK.md 2>/dev/null; RTK="없음(skip)"
fi
```

Do not execute `RTK_CANDIDATE` and do not mutate PATH later in the script.

- [ ] **Step 4: Run installer and shell gates**

Run: `rtk python3 -m pytest -q tests/test_installer_private_config.py`

Run: `rtk shellcheck -x -s bash -S error install.sh scripts/lib/private-file.sh scripts/lib/link-safely.sh`

Expected: tests PASS and shellcheck exits 0.

- [ ] **Step 5: Commit the installer closure**

```bash
rtk git add install.sh tests/test_installer_private_config.py
rtk git commit -m "fix: keep installer on trusted system path"
```

---

### Task 4: Align producer guidance and operator documentation

**Files:**
- Modify: `claude-md/global-guidance.md:22-27`
- Modify: `README.md:50-120`
- Modify: `tests/test_jhw_control_host.py`

**Interfaces:**
- Consumes: launcher contract v3 and consumer resolver flag `--resolve-from-checkout true`.
- Produces: one producer-side workflow: visible preflight, resolver start for new Tasks, `--task` for resume, and host-launcher finish for release/switch.

- [ ] **Step 1: Tighten producer documentation contract tests**

Update `test_global_task_guidance_uses_only_installed_host_launcher` to require these literals:

```python
assert '"$HOME/.local/bin/jhw-control-host" preflight' in guidance
assert '"$HOME/.local/bin/jhw-control-host" task start' in guidance
assert '--resolve-from-checkout true' in guidance
assert '"$HOME/.local/bin/jhw-control-host" task finish' in guidance
assert 'jhw-control-host" portfolio status' not in guidance
assert 'jhw-control task start' not in guidance
assert 'jhw-control task finish' not in guidance
assert 'PROJECT_REPOSITORY_NOT_FOUND' in guidance
assert '정확한 Project Record에 Repository를 등록' in guidance
assert 'PROJECT_REPOSITORY_AMBIGUOUS' in guidance
assert 'Repository 연관을 하나로 축소' in guidance
assert 'Project를 임의 선택하거나 explicit mode로 자동 fallback하지 않는다' in guidance
```

Update the README contract test to require contract v3, secure `task finish`, optional caller-coordinate binding, producer-first reinstall, and no credential migration.

- [ ] **Step 2: Run documentation tests and verify RED**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'global_task_guidance or readme_documents'`

Expected: guidance still names portfolio coordinate traversal and README still advertises the v2 command list.

- [ ] **Step 3: Rewrite the global Task guidance**

Replace the coordinate lookup clause with this policy:

```text
등록 선택 시 절대경로 launcher preflight 성공 뒤, 신규 Formal/Temporary Task는 현재
checkout의 exact root와 `task start --resolve-from-checkout true`를 사용한다. 기존 Task
재개는 `task start --task`를 사용한다. Project/Repository ID를 추측하거나 portfolio
pagination으로 조합하지 않는다. finish/switch도 `jhw-control-host task finish`를 사용한다.
`PROJECT_REPOSITORY_NOT_FOUND`는 Repository를 정확한 Project Record에 등록한 뒤 재시도하고,
`PROJECT_REPOSITORY_AMBIGUOUS`는 Repository의 Project 연관을 하나로 축소한 뒤 재시도한다.
어느 경우에도 Project를 임의 선택하거나 explicit mode로 자동 fallback하지 않는다.
```

Retain one explicit Task proposal/approval, unlock-once guidance, no raw config/credential access, and no repeated nudge after opt-out.

- [ ] **Step 4: Update README v3 and rollout instructions**

Document the exact command list, hidden preflight for start/finish, finish projection fields/reasons, resolver error actions, and this deployment order:

```text
producer merge → install.sh 재실행 → clean-shell --contract/preflight
→ jhw-notion resolver/skill merge → approved real Task smoke
```

State that `--project`, `--repo-id`, and `--task` are optional caller bindings for start projection; absence does not weaken downstream Task/Claim coordinate validation.

- [ ] **Step 5: Run focused producer tests**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py tests/test_installer_private_config.py`

Expected: all focused tests PASS.

- [ ] **Step 6: Commit documentation and its contract**

```bash
rtk git add README.md claude-md/global-guidance.md tests/test_jhw_control_host.py
rtk git commit -m "docs: document v3 resolver and finish workflow"
```

---

### Task 5: Run the producer release gate and reinstall after merge

**Files:**
- Verify only: all files changed in Tasks 1-4
- Runtime mutation after reviewed merge: `$HOME/.local/bin/jhw-control-host` installed payload

**Interfaces:**
- Consumes: the four committed producer deliverables.
- Produces: a clean producer branch and, after the reviewed change reaches the stable `claude-config` checkout, an installed v3 launcher ready before consumer activation.

- [ ] **Step 1: Run the complete automated producer gate**

Run: `rtk python3 -m pytest -q tests`

Run: `rtk shellcheck -x -s bash -S error install.sh scripts/lib/private-file.sh scripts/lib/link-safely.sh`

Run: `rtk python3 -m py_compile scripts/jhw-control-host.py`

Run: `rtk git diff --check`

Expected: all tests PASS; static checks exit 0.

- [ ] **Step 2: Reconcile the current default branch without losing user work**

Run: `rtk git fetch origin master`

Run: `rtk git merge-tree HEAD origin/master`

Run: `rtk git merge-base --is-ancestor origin/master HEAD`

Expected: `merge-tree` has no conflict markers. If the ancestry check exits 1, run `rtk git merge --no-edit origin/master`; resolve any overlap from the approved spec without discarding either side or user work.

- [ ] **Step 3: Rerun every producer gate on the reconciled tree**

Run: `rtk python3 -m pytest -q tests`

Run: `rtk shellcheck -x -s bash -S error install.sh scripts/lib/private-file.sh scripts/lib/link-safely.sh`

Run: `rtk python3 -m py_compile scripts/jhw-control-host.py`

Run: `rtk git diff --check`

Expected: all tests PASS and every static check exits 0 after default-branch reconciliation.

- [ ] **Step 4: Verify final scope and obtain review on the final combined diff**

Run: `rtk git status --short`

Run: `rtk git diff --stat origin/master...HEAD`

Expected: only #28 launcher/installer/guidance/test/docs changes are present and the worktree is clean. Request an independent security/code review of `origin/master...HEAD`; stop on any Critical, High, credential/path leak, unknown-output acceptance, or unresolved Important finding.

- [ ] **Step 5: Integrate only the reviewed final tree**

Invoke `superpowers:finishing-a-development-branch` and use its local integration path only after Step 4 is clean. Do not install from the Task worktree and do not close #28 before the consumer smoke boundary is satisfied.

Keep the reviewed `task/11bc99e70133-jhw7500-claude-config-28` ref until Step 7 passes; branch deletion is deferred so the ancestry check remains exact.

After integration, run: `rtk git -C /home/jhw/ai/opencode/projects/claude-config merge-base --is-ancestor task/11bc99e70133-jhw7500-claude-config-28 master`

Expected: exit 0, proving the reviewed Task branch tip is contained in the stable default branch.

- [ ] **Step 6: Revalidate and install only from the stable checkout after merge**

Verify `/home/jhw/ai/opencode/projects/claude-config` is on `master`, contains the reviewed Task branch, and has no working-tree override of the producer files. Do not discard unrelated user changes and do not install if any producer file differs from `HEAD`.

Run: `rtk git -C /home/jhw/ai/opencode/projects/claude-config branch --show-current`

Run: `rtk git -C /home/jhw/ai/opencode/projects/claude-config diff --exit-code HEAD -- install.sh scripts/jhw-control-host.py claude-md/global-guidance.md README.md tests/test_jhw_control_host.py tests/test_installer_private_config.py`

Run: `rtk python3 -m pytest -q /home/jhw/ai/opencode/projects/claude-config/tests`

Run: `rtk shellcheck -x -s bash -S error /home/jhw/ai/opencode/projects/claude-config/install.sh /home/jhw/ai/opencode/projects/claude-config/scripts/lib/private-file.sh /home/jhw/ai/opencode/projects/claude-config/scripts/lib/link-safely.sh`

Run: `rtk python3 -m py_compile /home/jhw/ai/opencode/projects/claude-config/scripts/jhw-control-host.py`

Expected: branch is `master`, all six producer implementation/test paths match `HEAD`, and the post-merge full/static gate passes.

Run: `rtk /home/jhw/ai/opencode/projects/claude-config/install.sh`

Run: `rtk "$HOME/.local/bin/jhw-control-host" --contract`

Expected: contract name `jhw-control-host`, version 3, secure-store-only policy, and the exact five-command list. This installer operation reads no credentials.

- [ ] **Step 7: Run clean-shell preflight without exposing values**

Run: `rtk env -i HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" PATH=/usr/bin:/bin "$HOME/.local/bin/jhw-control-host" preflight`

Expected: exit 0 with the validated preflight envelope. If the keyring is locked, the user runs `jhw-control-host unlock` in their terminal once; do not inspect or print the stored values.

No additional commit is created for verification or installation.
