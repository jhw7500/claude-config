# jhw-control-host v4 Task Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the secure-store-only host from two Task mutations to the complete ten-command canonical Task lifecycle while removing duplicated downstream Task result/error schema authority.

**Architecture:** Keep `jhw-control-host.py` as the credential broker, trusted child supervisor, bounded JSON envelope validator, and sensitive-output filter. Expose an exact Task subcommand allowlist, classify only mutation versus read-only preflight behavior locally, preserve the v3 start/finish projections plus a four-coordinate child-start projection, and canonicalize other Task results without command-specific schemas. Accept bounded stable downstream errors without duplicating code/reason/exit maps, retaining only validated workflow coordinates.

**Tech Stack:** Python 3.10 standard library, pytest, Bash installer, shellcheck.

**Spec:** `docs/superpowers/specs/2026-08-27-jhw-control-host-v4-lifecycle-design.md`

## Global Constraints

- Every shell command in this environment starts with `rtk`; run producer commands from the `claude-config` #28 worktree root.
- Never read, print, log, migrate, or fall back to credential values. Project and Notion credentials remain Secret Service-only; the repository credential remains GitHub CLI keyring-only.
- Contract v4 command families are exactly `unlock`, `preflight`, `portfolio status`, `task start`, `task child-start`, `task contract`, `task completion-ready`, `task promote`, `task status`, `task handoff`, `task finish`, `task recover`, and `task assert-owner`.
- The host never exposes a generic passthrough and rejects any Task subcommand outside the exact inventory before config or credential access.
- Hidden preflight applies to `start`, `child-start`, `contract`, `completion-ready`, `promote`, `finish`, and recover actions `force-end|takeover|cleanup`; it does not apply to `status`, `handoff`, `assert-owner`, or recover action `status`.
- A malformed, missing, or duplicate recover `--action status` is classified as mutation so it cannot bypass preflight.
- Output remains one duplicate-free JSON document, stdout-only on success, stderr-only on failure, bounded to 12 KiB, canonicalized, and scanned for credentials plus protected raw/encoded paths before projection.
- `task start` and `task finish` retain their v3 public projections and caller-coordinate binding while tolerating additive downstream fields. `task child-start` returns the same four public coordinates as start.
- Other Task success results are safe canonical JSON object pass-through; the downstream CLI remains the detailed schema authority.
- Errors preserve exit `1|2|4|75|78`, a code matching `[A-Z][A-Z0-9_]{1,63}`, and an optional reason matching `[a-z][a-z0-9_]{0,63}`. The host does not maintain code allowlists or code-to-exit tables.
- Only bounded canonical `conflicting_claim`, `retained_claim`, and `retained_task` coordinates may survive from error metadata; other downstream detail is dropped.
- Producer merge, stable-checkout installation, and clean-environment contract/preflight validation must finish before any `jhw-notion` consumer conversion is activated.

## Planned File Structure

```text
scripts/jhw-control-host.py       v4 inventory, preflight classifier, Task envelope projection
tests/test_jhw_control_host.py    exact inventory, lifecycle routing, envelope, and security tests
README.md                         v4 operator contract and producer-first rollout guide
docs/superpowers/specs/2026-08-27-jhw-control-host-v4-lifecycle-design.md
                                  approved design status
```

---

### Task 1: Publish the exact v4 Task command inventory

**Files:**
- Modify: `scripts/jhw-control-host.py:37-160,326-334,1138-1148`
- Modify: `tests/test_jhw_control_host.py:133-206,2037-2056`

**Interfaces:**
- Consumes: existing `CONTRACT`, `_allowed_invocation()`, and early `run_program()` allowlist gate.
- Produces: ordered `TASK_SUBCOMMANDS: tuple[str, ...]`, `TASK_SUBCOMMAND_SET: frozenset[str]`, contract version 4, and exact allowlist behavior before credential access.

- [ ] **Step 1: Change the contract expectation and add an exact Task-subcommand gate test**

Define the test-side inventory independently from the launcher:

```python
V4_COMMANDS = [
    "unlock",
    "preflight",
    "portfolio status",
    "task start",
    "task child-start",
    "task contract",
    "task completion-ready",
    "task promote",
    "task status",
    "task handoff",
    "task finish",
    "task recover",
    "task assert-owner",
]

V4_TASK_SUBCOMMANDS = [
    "start", "child-start", "contract", "completion-ready", "promote",
    "status", "handoff", "finish", "recover", "assert-owner",
]
```

Update both startup contract tests to require version `4` and `V4_COMMANDS`. Add:

```python
@pytest.mark.parametrize("subcommand", V4_TASK_SUBCOMMANDS)
def test_v4_task_inventory_is_allowlisted(subcommand: str, launcher: ModuleType) -> None:
    assert launcher._allowed_invocation(["task", subcommand]) is True


@pytest.mark.parametrize("argv", [
    ["task"],
    ["task", "cancel"],
    ["task", "switch"],
    ["task", "start-extra"],
    ["portfolio", "export"],
])
def test_outside_v4_inventory_is_rejected_before_config_or_provider(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
) -> None:
    def unexpected_runner(*_args, **_kwargs):
        raise AssertionError("rejected command must not read a provider or run a child")

    result = launcher.run_program(
        argv,
        home=tmp_path / "missing-home",
        environment={"GH_TOKEN": "ambient-must-not-appear"},
        uid=os.getuid(),
        command_runner=unexpected_runner,
    )

    assert result.returncode == 2
    assert json.loads(result.stderr) == {"error": {"code": "INVALID_ARGUMENT"}}
```

Replace the existing `test_non_allowlisted_command_stops_before_config_or_provider` argv list so it no longer treats `task status`, `task recover`, or `task handoff` as forbidden. Its forbidden cases become exactly `[]`, `["--help"]`, `["task"]`, `["task", "cancel"]`, `["task", "switch"]`, `["board", "status"]`, and `["project", "register"]`.

- [ ] **Step 2: Run the contract and inventory tests to verify RED**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'contract_needs_no_config or startup_ignores or v4_task_inventory or outside_v4_inventory'`

Expected: version/list assertions fail and the eight new Task subcommands are rejected.

- [ ] **Step 3: Implement one ordered inventory used by contract and allowlist**

Replace the Task command literals with:

```python
TASK_SUBCOMMANDS = (
    "start",
    "child-start",
    "contract",
    "completion-ready",
    "promote",
    "status",
    "handoff",
    "finish",
    "recover",
    "assert-owner",
)
TASK_SUBCOMMAND_SET = frozenset(TASK_SUBCOMMANDS)

CONTRACT = {
    "commands": [
        "unlock",
        "preflight",
        "portfolio status",
        *(f"task {subcommand}" for subcommand in TASK_SUBCOMMANDS),
    ],
    "credential_policy": "secure-store-only",
    "name": "jhw-control-host",
    "version": 4,
}
```

Use the same set in the gate:

```python
def _allowed_invocation(argv: Sequence[str]) -> bool:
    values = tuple(argv)
    return (
        values == ("unlock",)
        or values == ("preflight",)
        or values[:2] == ("portfolio", "status")
        or (
            len(values) >= 2
            and values[0] == "task"
            and values[1] in TASK_SUBCOMMAND_SET
        )
    )
```

The launcher must not validate each command's flags; malformed flags reach the fixed downstream CLI only after the command family and security gates pass.

- [ ] **Step 4: Run the focused inventory tests**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'contract_needs_no_config or startup_ignores or v4_task_inventory or outside_v4_inventory'`

Expected: all selected tests PASS.

- [ ] **Step 5: Commit the v4 public inventory**

```bash
rtk git add scripts/jhw-control-host.py tests/test_jhw_control_host.py
rtk git commit -m "feat(host): expose v4 task inventory"
```

---

### Task 2: Classify lifecycle mutations for hidden preflight

**Files:**
- Modify: `scripts/jhw-control-host.py:1138-1148,1794-1811`
- Modify: `tests/test_jhw_control_host.py:1240-1358,1822-1860,2037-2079`

**Interfaces:**
- Consumes: `TASK_SUBCOMMAND_SET`, child environment construction, `_control_call()`, and `_program_result()`.
- Produces: `_task_requires_preflight(argv: Sequence[str]) -> bool` and exact hidden-preflight call order.

- [ ] **Step 1: Add the complete pure classification truth table**

```python
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["task", "start"], True),
        (["task", "child-start"], True),
        (["task", "contract"], True),
        (["task", "completion-ready"], True),
        (["task", "promote"], True),
        (["task", "finish"], True),
        (["task", "recover", "--task", TASK_ID, "--expect", CLAIM_ID, "--action", "force-end"], True),
        (["task", "recover", "--task", TASK_ID, "--expect", CLAIM_ID, "--action", "takeover"], True),
        (["task", "recover", "--task", TASK_ID, "--expect", CLAIM_ID, "--action", "cleanup"], True),
        (["task", "status", "--task", TASK_ID], False),
        (["task", "handoff", "--task", TASK_ID], False),
        (["task", "assert-owner", "--task", TASK_ID, "--claim", CLAIM_ID], False),
        (["task", "recover", "--task", TASK_ID, "--expect", CLAIM_ID, "--action", "status"], False),
        (["task", "recover", "--task", TASK_ID, "--expect", CLAIM_ID], True),
        (["task", "recover", "--action", "status", "--action", "status"], True),
        (["task", "recover", "--action", "unknown"], True),
    ],
)
def test_task_hidden_preflight_classification(
    launcher: ModuleType,
    argv: list[str],
    expected: bool,
) -> None:
    assert launcher._task_requires_preflight(argv) is expected
```

- [ ] **Step 2: Add integration tests for preflight failure and read-only direct execution**

Use representative mutation and read-only invocations:

```python
@pytest.mark.parametrize("argv", [
    ["task", "start"],
    ["task", "child-start"],
    ["task", "contract"],
    ["task", "completion-ready"],
    ["task", "promote"],
    ["task", "finish"],
    ["task", "recover", "--action", "cleanup"],
])
def test_v4_mutations_stop_after_failed_hidden_preflight(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
) -> None:
    runner = FakeCommandRunner(launcher)
    failure = launcher.CommandResult(
        78, b"", b'{"error":{"code":"PREFLIGHT_UNAVAILABLE"}}\n',
    )
    runner.control_results[("preflight",)] = failure

    result = run_secure(launcher, tmp_path, argv, runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "PREFLIGHT_UNAVAILABLE"}}
    assert [call["argv"][2:] for call in runner.calls[2:]] == [("preflight",)]


@pytest.mark.parametrize("argv", [
    ["task", "status", "--task", TASK_ID],
    ["task", "handoff", "--task", TASK_ID],
    ["task", "assert-owner", "--task", TASK_ID, "--claim", CLAIM_ID],
    ["task", "recover", "--task", TASK_ID, "--expect", CLAIM_ID, "--action", "status"],
])
def test_v4_read_only_commands_skip_hidden_preflight(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.control_results[tuple(argv)] = launcher.CommandResult(
        1, b"", b'{"error":{"code":"SAFE_READ_FAILURE"}}\n',
    )

    run_secure(launcher, tmp_path, argv, runner)

    assert [call["argv"][2:] for call in runner.calls[2:]] == [tuple(argv)]
```

- [ ] **Step 3: Run the classifier and routing tests to verify RED**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'hidden_preflight_classification or v4_mutations or v4_read_only'`

Expected: `_task_requires_preflight` is absent; mutation commands other than start/finish skip preflight.

- [ ] **Step 4: Implement the conservative classifier**

```python
MUTATING_TASK_SUBCOMMANDS = frozenset({
    "start", "child-start", "contract", "completion-ready", "promote", "finish",
})


def _task_requires_preflight(argv: Sequence[str]) -> bool:
    values = tuple(argv)
    if len(values) < 2 or values[0] != "task":
        return False
    if values[1] in MUTATING_TASK_SUBCOMMANDS:
        return True
    if values[1] != "recover":
        return False
    action_positions = [
        index for index, value in enumerate(values)
        if value == "--action"
    ]
    return not (
        len(action_positions) == 1
        and action_positions[0] + 1 < len(values)
        and values[action_positions[0] + 1] == "status"
    )
```

Replace the start/finish literal branch in `run_program()` with:

```python
if _task_requires_preflight(argv):
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

- [ ] **Step 5: Run routing tests and the existing start/finish preflight regressions**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'preflight_classification or v4_mutations or v4_read_only or task_finish_runs_hidden or hidden_preflight_failure'`

Expected: all selected tests PASS and mutation call order remains `preflight` then requested Task command.

- [ ] **Step 6: Commit preflight classification**

```bash
rtk git add scripts/jhw-control-host.py tests/test_jhw_control_host.py
rtk git commit -m "feat(host): classify task lifecycle mutations"
```

---

### Task 3: Generalize safe Task success envelopes

**Files:**
- Modify: `scripts/jhw-control-host.py:240-241,934-938,1160-1530,1591-1630`
- Modify: `tests/test_jhw_control_host.py:380-598,1293-1358,1558-1937`

**Interfaces:**
- Consumes: `_control_json()`, `_exact_object()`, `_canonical_id()`, `_worktree_coordinates()`, `_validate_task_start_result()`, `_validate_task_finish_result()`, `_output_warnings()`, and the pre-projection sensitive scan.
- Produces: `_required_object(value, required)`, additive-safe start/finish validation, child-start four-coordinate projection, and generic Task object canonicalization.

- [ ] **Step 1: Add fixtures for every new Task success command**

Add a helper that installs one downstream success result in the fake runner:

```python
def task_success(
    launcher: ModuleType,
    command: tuple[str, ...],
    result: dict[str, object],
):
    return launcher.CommandResult(
        0,
        json.dumps(
            {"command": " ".join(command[:2]), "result": result},
            separators=(",", ":"),
        ).encode() + b"\n",
        b"",
    )
```

Use these requests/results for generic pass-through:

```python
GENERIC_TASK_SUCCESSES = [
    (("task", "contract", "--task", TASK_ID), {"task": {"task_id": TASK_ID}, "future_safe": True}),
    (("task", "completion-ready", "--task", TASK_ID, "--claim", CLAIM_ID), {"task_id": TASK_ID, "claim_id": CLAIM_ID, "recorded_at": "2026-08-27T00:00:00Z"}),
    (("task", "promote", "--task", TASK_ID), {"task": {"task_id": TASK_ID, "kind": "formal"}}),
    (("task", "status", "--task", TASK_ID), {"task": {"task_id": TASK_ID}, "claim": None}),
    (("task", "handoff", "--task", TASK_ID), {"handoff_pointer": f"handoffs/{TASK_ID}/{CLAIM_ID}.md", "truncated": False}),
    (("task", "recover", "--task", TASK_ID, "--expect", CLAIM_ID, "--action", "status"), {"kind": "status", "task_id": TASK_ID, "claim_id": CLAIM_ID}),
    (("task", "assert-owner", "--task", TASK_ID, "--claim", CLAIM_ID), {"owned": True, "claim": {"task_id": TASK_ID, "claim_id": CLAIM_ID}}),
]
```

- [ ] **Step 2: Add failing generic, child-start, and additive compatibility tests**

```python
@pytest.mark.parametrize(("command", "upstream_result"), GENERIC_TASK_SUCCESSES)
def test_v4_generic_task_results_are_canonicalized_without_schema_duplication(
    launcher: ModuleType,
    tmp_path: Path,
    command: tuple[str, ...],
    upstream_result: dict[str, object],
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.control_results[command] = task_success(launcher, command, upstream_result)

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "command": " ".join(command[:2]),
        "result": upstream_result,
    }


def test_task_child_start_projects_only_four_coordinates(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    command = ("task", "child-start", "--parent", OTHER_TASK_ID)
    runner = FakeCommandRunner(launcher)
    raw_start = json.loads(
        runner.control_results[("task", "start", "--issue", "https://example.test/issues/28")].stdout
    )
    raw_start["command"] = "task child-start"
    raw_start["result"]["future_safe"] = {"ignored": True}
    runner.control_results[command] = launcher.CommandResult(
        0, json.dumps(raw_start, separators=(",", ":")).encode() + b"\n", b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert json.loads(result.stdout) == {
        "command": "task child-start",
        "result": {
            "task_id": TASK_ID,
            "claim_id": CLAIM_ID,
            "branch": TASK_BRANCH,
            "worktree_ref": WORKTREE_REF,
        },
    }
```

Change the existing extra-field start and finish cases from rejection to projection tests. Add `future_safe` at the top result level and inside start's `task` and `claim`; assert the public start/finish result remains unchanged.

Delete `test_task_start_accepts_supported_task_role_without_exposing_it` and `test_task_start_rejects_invalid_task_role_semantics`; `kind`, `task_role`, and `reused` are downstream-only schema fields, not public host coordinates. Replace them with:

```python
@pytest.mark.parametrize("downstream_only", [
    {"kind": "future-kind", "task_role": "future-role"},
    {"kind": None, "task_role": {"future": True}},
    {"future_task_field": ["safe", "json"]},
])
def test_task_start_ignores_downstream_only_task_fields(
    launcher: ModuleType,
    tmp_path: Path,
    downstream_only: dict[str, object],
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "start", "--issue", "https://example.test/issues/28")
    payload = json.loads(runner.control_results[command].stdout)
    payload["result"]["task"].update(downstream_only)
    payload["result"]["reused"] = {"future": "shape"}
    runner.control_results[command] = launcher.CommandResult(
        0, json.dumps(payload, separators=(",", ":")).encode() + b"\n", b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 0
    assert set(json.loads(result.stdout)["result"]) == {
        "task_id", "claim_id", "branch", "worktree_ref",
    }
```

- [ ] **Step 3: Add malformed generic envelope regressions**

```python
@pytest.mark.parametrize("upstream_result", [None, [], "text", 1, True])
def test_v4_generic_task_success_requires_a_result_object(
    launcher: ModuleType,
    tmp_path: Path,
    upstream_result: object,
) -> None:
    command = ("task", "status", "--task", TASK_ID)
    runner = FakeCommandRunner(launcher)
    runner.control_results[command] = launcher.CommandResult(
        0,
        json.dumps({"command": "task status", "result": upstream_result}).encode() + b"\n",
        b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}
```

Retain the existing duplicate JSON key, wrong command string, mixed stream, oversized output, credential/path canary, and unsupported outer-envelope field tests for all Task commands.

Add strict JSON-number coverage because Python's default decoder otherwise accepts non-standard constants:

```python
@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_v4_generic_task_result_rejects_non_json_numeric_constants(
    launcher: ModuleType,
    tmp_path: Path,
    constant: bytes,
) -> None:
    command = ("task", "status", "--task", TASK_ID)
    runner = FakeCommandRunner(launcher)
    runner.control_results[command] = launcher.CommandResult(
        0,
        b'{"command":"task status","result":{"value":' + constant + b"}}\n",
        b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}
```

- [ ] **Step 4: Run the new success tests to verify RED**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'v4_generic_task or child_start_projects or non_json_numeric or additive or projects_only_approved_fields or task_finish_rejects_malformed'`

Expected: new Task success commands return `CONTROL_OUTPUT_INVALID`; additive start/finish fields are rejected.

- [ ] **Step 5: Add a required-key object helper and make compatibility projections additive-safe**

```python
def _required_object(value: object, required: set[str]) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or not all(isinstance(key, str) for key in value)
        or not required.issubset(value)
    ):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return value
```

Change the three start object reads and the finish object read exactly as follows; the remainder of both validators stays unchanged:

```python
result = _required_object(
    value,
    {"task", "claim", "branch", "worktree_ref"},
)
task = _required_object(
    result.get("task"),
    {"task_id", "project_id", "repo_id"},
)
claim = _required_object(
    result.get("claim"),
    {
        "task_id", "claim_id", "project_id", "repo_id", "host",
        "branch", "worktree_ref", "started_at",
    },
)

result = _required_object(
    value,
    {"task_id", "claim_id", "status", "released_at", "worktree_removed"},
)
```

Remove the `task_kind`, `task_role`, and `reused` checks from `_validate_task_start_result()` because none is part of the public projection or coordinate binding. Continue reading optional `latest_handoff`, `cleanup_error`, and `handoff_pointer` through `.get()`/membership checks. This preserves Task/Claim/project/repository/worktree/host/request/timestamp relationships plus Handoff and cleanup checks while no longer duplicating downstream-only Task schema.

Make the shared JSON decoder/encoder standards-compliant before generic result pass-through:

```python
def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON numeric constant")


def _parse_json(payload: bytes) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise LauncherError("CREDENTIAL_PROVIDER_INVALID") from None


def _json_bytes(value: object) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return (encoded + "\n").encode()
```

- [ ] **Step 6: Dispatch the three Task success policies**

Add:

```python
def _validate_generic_task_result(value: object) -> dict[str, object]:
    return dict(_required_object(value, set()))
```

Then replace the Task success branch in `_validated_control_result()` with:

```python
if expected == "preflight":
    projected_result = _validate_preflight_result(payload.get("result"))
elif expected == "portfolio status":
    projected_result = _validate_portfolio_result(payload.get("result"))
elif expected in {"task start", "task child-start"}:
    projected_result = _validate_task_start_result(
        payload.get("result"),
        build_host=build_host,
        request=command,
    )
    if expected == "task child-start":
        projected_result.pop("latest_handoff", None)
elif expected == "task finish":
    projected_result = _validate_task_finish_result(payload.get("result"), request=command)
elif expected.startswith("task ") and expected.split(" ", 1)[1] in TASK_SUBCOMMAND_SET:
    projected_result = _validate_generic_task_result(payload.get("result"))
else:
    raise LauncherError("CONTROL_OUTPUT_INVALID")
```

The `task start`/`task child-start` shared validator must not require `--task`; child-start binds the returned Task and Claim internally and returns only the four canonical coordinates.

- [ ] **Step 7: Run the complete success/envelope/security slice**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'allowed_commands or task_start or task_finish or child_start or generic_task or json_stream or sensitive or encoded_or_cross_stream or paths_are_protected'`

Expected: all selected tests PASS; safe generic fields survive, start/finish/child-start projections stay bounded, and canary output is replaced with `SENSITIVE_OUTPUT_REJECTED`.

- [ ] **Step 8: Commit Task success generalization**

```bash
rtk git add scripts/jhw-control-host.py tests/test_jhw_control_host.py
rtk git commit -m "feat(host): generalize safe task results"
```

---

### Task 4: Replace duplicated error maps with one bounded error envelope

**Files:**
- Modify: `scripts/jhw-control-host.py:39-160,1520-1640`
- Modify: `tests/test_jhw_control_host.py:300-370,1939-2035,2081-2527`

**Interfaces:**
- Consumes: `_required_object()`, `_bounded_text()`, `_canonical_id()`, `_requested_id()`, `_worktree_coordinates()`, `_timestamp()`, and outer warning projection.
- Produces: `ERROR_CODE_RE`, `ERROR_REASON_RE`, generic `_validate_error_result(value, request)`, and direct preservation of any allowed downstream exit.

- [ ] **Step 1: Replace closed-map tests with stable-code and exit independence tests**

Remove test-side copies of `TASK_FINISH_ERROR_REASONS`, `TASK_FINISH_CODES_BY_CALL_SITE`, and exact command code maps. Delete `test_task_finish_error_allowlist_is_exact`, `test_every_task_finish_specific_error_projects`, and `test_task_finish_error_reasons_are_code_bound`. Replace `test_task_finish_rejects_invalid_error_shapes` and `test_error_output_uses_a_closed_command_specific_schema` with the lexical/metadata tests in Steps 1-3. Add:

```python
@pytest.mark.parametrize("returncode", [1, 2, 4, 75, 78])
@pytest.mark.parametrize("argv", [
    ["preflight"],
    ["portfolio", "status"],
    ["task", "start"],
    ["task", "status", "--task", TASK_ID],
    ["task", "finish", "--task", TASK_ID, "--claim", CLAIM_ID, "--status", "handoff"],
])
def test_stable_downstream_error_code_preserves_any_allowed_exit(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
    returncode: int,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.control_results[tuple(argv)] = launcher.CommandResult(
        returncode,
        b"",
        b'{"error":{"code":"FUTURE_STABLE_ERROR"}}\n',
    )
    if argv[:2] in [["task", "start"], ["task", "finish"]]:
        runner.control_results[("preflight",)] = launcher.CommandResult(0, PREFLIGHT_OUTPUT, b"")

    result = run_secure(launcher, tmp_path, argv, runner)

    assert result.returncode == returncode
    assert json.loads(result.stderr) == {"error": {"code": "FUTURE_STABLE_ERROR"}}
```

This test deliberately proves that the host no longer decides whether a code belongs to a command or exit class.

- [ ] **Step 2: Add exact format and detail-dropping tests**

```python
@pytest.mark.parametrize("error", [
    {"code": "lowercase"},
    {"code": "A"},
    {"code": "_LEADING"},
    {"code": "A" * 65},
    {"code": 7},
    {"code": "SAFE_CODE", "reason": "Uppercase"},
    {"code": "SAFE_CODE", "reason": "has-hyphen"},
    {"code": "SAFE_CODE", "reason": "a" * 65},
])
def test_error_code_and_reason_formats_are_bounded(
    launcher: ModuleType,
    tmp_path: Path,
    error: dict[str, object],
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "status", "--task", TASK_ID)
    runner.control_results[command] = launcher.CommandResult(
        1, b"", json.dumps({"error": error}).encode() + b"\n",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


def test_safe_reason_survives_and_unrecognized_details_are_dropped(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "status", "--task", TASK_ID)
    runner.control_results[command] = launcher.CommandResult(
        1,
        b"",
        b'{"error":{"code":"FUTURE_STABLE_ERROR","reason":"new_safe_reason","diagnostic":"drop-me","nested":{"drop":true}}}\n',
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert json.loads(result.stderr) == {
        "error": {"code": "FUTURE_STABLE_ERROR", "reason": "new_safe_reason"},
    }
```

- [ ] **Step 3: Add bounded workflow-coordinate metadata tests**

Keep the existing canonical conflict test and add retained Task coverage:

```python
def test_error_preserves_only_canonical_workflow_coordinates(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "child-start", "--parent", OTHER_TASK_ID)
    upstream_error = {
        "code": "TASK_SESSION_BUSY",
        "reason": "session_busy",
        "retained_claim": {"task_id": TASK_ID, "claim_id": CLAIM_ID, "state": "released", "host": "drop-me"},
        "retained_task": {"task_id": TASK_ID, "alias": "drop-me"},
        "diagnostic": "drop-me",
    }
    runner.control_results[command] = launcher.CommandResult(
        4, b"", json.dumps({"error": upstream_error}).encode() + b"\n",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert json.loads(result.stderr) == {
        "error": {
            "code": "TASK_SESSION_BUSY",
            "reason": "session_busy",
            "retained_claim": {"task_id": TASK_ID, "claim_id": CLAIM_ID, "state": "released"},
            "retained_task": {"task_id": TASK_ID},
        },
    }
```

Parameterize invalid IDs, invalid retained state, mismatched explicit `--task`, inconsistent conflict branch/worktree, and invalid timestamp; each must collapse to `CONTROL_OUTPUT_INVALID` without leaking the bad metadata. Remove the two `host` mutations from `test_task_conflict_error_requires_canonical_consistent_coordinates`: host is ignored downstream detail, so the existing `test_task_conflict_accepts_another_host_but_does_not_disclose_it` remains the positive regression proving it is not exposed.

- [ ] **Step 4: Run the new error tests to verify RED**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'stable_downstream_error or error_code_and_reason or unrecognized_details or workflow_coordinates or task_conflict or task_error_claims'`

Expected: unknown stable codes and reasons are rejected; `retained_task` is dropped or rejected; old code-to-exit checks reject valid new combinations.

- [ ] **Step 5: Define the stable lexical bounds and remove duplicated maps**

Delete `COMMON_CONTROL_ERROR_CODES`, `COMMAND_CONTROL_ERROR_CODES`, `TASK_FINISH_ERROR_REASONS`, `CONFLICT_EXIT_CODES`, `RETRY_EXIT_CODES`, `POLICY_EXIT_CODES`, and `_expected_error_returncode()`. Add:

```python
ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
ERROR_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
```

Keep launcher-generated error codes unchanged; these regexes validate only downstream `jhw-control` envelopes.

- [ ] **Step 6: Implement bounded metadata projection without code coupling**

Use `_required_object()` so safe future detail keys are ignored rather than rejected:

```python
def _validate_retained_claim(value: object, *, request: Sequence[str]) -> dict[str, object]:
    retained = _required_object(value, {"task_id", "claim_id", "state"})
    task_id = _canonical_id(retained["task_id"], TASK_ID_RE)
    requested_task = _requested_id(request, "--task", TASK_ID_RE)
    if requested_task is not None and task_id != requested_task:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    if retained["state"] not in {"active", "released"}:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return {
        "task_id": task_id,
        "claim_id": _canonical_id(retained["claim_id"], CLAIM_ID_RE),
        "state": retained["state"],
    }


def _validate_retained_task(value: object, *, request: Sequence[str]) -> dict[str, object]:
    retained = _required_object(value, {"task_id"})
    task_id = _canonical_id(retained["task_id"], TASK_ID_RE)
    requested_task = _requested_id(request, "--task", TASK_ID_RE)
    if requested_task is not None and task_id != requested_task:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return {"task_id": task_id}
```

Move conflict coordinate validation into this helper. `host` is downstream-only and is ignored if present; the public workflow coordinates remain required and internally consistent:

```python
def _validate_conflicting_claim(
    value: object,
    *,
    request: Sequence[str],
) -> dict[str, object]:
    conflict = _required_object(
        value,
        {"task_id", "claim_id", "branch", "worktree_ref", "started_at"},
    )
    task_id = _canonical_id(conflict["task_id"], TASK_ID_RE)
    requested_task = _requested_id(request, "--task", TASK_ID_RE)
    if requested_task is not None and task_id != requested_task:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    worktree_ref, branch = _worktree_coordinates(
        task_id,
        conflict["worktree_ref"],
        conflict["branch"],
    )
    return {
        "task_id": task_id,
        "claim_id": _canonical_id(conflict["claim_id"], CLAIM_ID_RE),
        "branch": branch,
        "worktree_ref": worktree_ref,
        "started_at": _timestamp(conflict["started_at"]),
    }
```

- [ ] **Step 7: Implement the generic downstream error validator**

```python
def _validate_error_result(value: object, *, request: Sequence[str]) -> dict[str, object]:
    error = _required_object(value, {"code"})
    code = error["code"]
    if not isinstance(code, str) or ERROR_CODE_RE.fullmatch(code) is None:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    projected: dict[str, object] = {"code": code}
    if "reason" in error:
        reason = error["reason"]
        if not isinstance(reason, str) or ERROR_REASON_RE.fullmatch(reason) is None:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        projected["reason"] = reason
    if "conflicting_claim" in error:
        projected["conflicting_claim"] = _validate_conflicting_claim(
            error["conflicting_claim"], request=request,
        )
    if "retained_claim" in error:
        projected["retained_claim"] = _validate_retained_claim(
            error["retained_claim"], request=request,
        )
    if "retained_task" in error:
        projected["retained_task"] = _validate_retained_task(
            error["retained_task"], request=request,
        )
    return projected
```

In `_validated_control_result()`, call this validator with `request=command`, keep the allowed exit set `{0,1,2,4,75,78}`, and return `ProgramResult(result.returncode, stderr=...)` without comparing code and exit.

- [ ] **Step 8: Run all error, warning, stream, and sensitive-output tests**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'error or warning or stream or sensitive or conflict or retained or output_too_large'`

Expected: all selected tests PASS; new stable codes/reasons survive, unsafe metadata and canaries fail closed, and outer warnings remain bounded.

- [ ] **Step 9: Run the complete launcher suite and compile gate**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py`

Run: `rtk python3 -m py_compile scripts/jhw-control-host.py`

Expected: launcher tests PASS and compilation exits 0.

- [ ] **Step 10: Commit the bounded generic error contract**

```bash
rtk git add scripts/jhw-control-host.py tests/test_jhw_control_host.py
rtk git commit -m "fix(host): defer task error schema to control"
```

---

### Task 5: Document the v4 operator contract

**Files:**
- Modify: `README.md:55-156`
- Modify: `tests/test_jhw_control_host.py:3108-3385`
- Modify: `docs/superpowers/specs/2026-08-27-jhw-control-host-v4-lifecycle-design.md:1-6`

**Interfaces:**
- Consumes: exact v4 contract, preflight classifier, three Task success policies, generic error policy, and producer-first rollout.
- Produces: an auditable v4 README inventory and approved design status.

- [ ] **Step 1: Replace the README test helper with exact v4 boundaries**

Rename `_assert_readme_v3_contract_boundaries()` to `_assert_readme_v4_contract_boundaries()` and require marker names `jhw-control-host-v4-operator-contract` and `jhw-control-host-v4-contract`. Parse the command row and assert:

```python
assert command_families == V4_COMMANDS
```

Require these behavior rows in the inventory:

```python
assert set(rows) == {
    "launcher command families",
    "hidden preflight mutations",
    "read-only without hidden preflight",
    "compatibility projections",
    "generic Task results",
    "downstream errors",
}
```

Assert exact values for the two classifier rows:

```python
assert re.findall(r"`([^`]+)`", rows["hidden preflight mutations"]) == [
    "task start", "task child-start", "task contract", "task completion-ready",
    "task promote", "task finish", "task recover --action force-end|takeover|cleanup",
]
assert re.findall(r"`([^`]+)`", rows["read-only without hidden preflight"]) == [
    "task status", "task handoff", "task assert-owner", "task recover --action status",
]
```

Keep the mutation tests that inject an extra launcher command outside the inventory and assert the helper rejects it.

- [ ] **Step 2: Run README contract tests to verify RED**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'readme_documents'`

Expected: README still advertises v3 and the five-command inventory.

- [ ] **Step 3: Rewrite only the managed operator-contract section**

Rename both managed markers from v3 to v4. Keep the Secret Service provisioning, trusted executable/ancestor, installer mode, and unlock paragraphs unchanged. Replace the opening and inventory with:

```markdown
`jhw-control-host`는 clean shell에서 Project Control 호출에 필요한 non-secret 좌표와 세 credential을
parent shell에 남기지 않고 child `jhw-control`에만 주입하는 **secure-store-only** launcher입니다.
contract v4는 아래 13개 command family만 공개합니다. lifecycle mutation은 hidden preflight 뒤에만
실행하고, 읽기 전용 진단은 preflight 장애 중에도 secure launcher 경계 안에서 실행합니다.

### v4 contract inventory

<!-- jhw-control-host-v4-contract:start -->
| Inventory | Exact v4 values |
| --- | --- |
| launcher command families | `unlock`, `preflight`, `portfolio status`, `task start`, `task child-start`, `task contract`, `task completion-ready`, `task promote`, `task status`, `task handoff`, `task finish`, `task recover`, `task assert-owner` |
| hidden preflight mutations | `task start`, `task child-start`, `task contract`, `task completion-ready`, `task promote`, `task finish`, `task recover --action force-end|takeover|cleanup` |
| read-only without hidden preflight | `task status`, `task handoff`, `task assert-owner`, `task recover --action status` |
| compatibility projections | `task start`, `task finish`, `task child-start` |
| generic Task results | canonical JSON object pass-through after common security validation |
| downstream errors | code `[A-Z][A-Z0-9_]{1,63}`, optional reason `[a-z][a-z0-9_]{0,63}`, exit `1|2|4|75|78` |
<!-- jhw-control-host-v4-contract:end -->
```

Replace the command example block with:

```bash
"$HOME/.local/bin/jhw-control-host" --contract
"$HOME/.local/bin/jhw-control-host" unlock
"$HOME/.local/bin/jhw-control-host" preflight
"$HOME/.local/bin/jhw-control-host" portfolio status
"$HOME/.local/bin/jhw-control-host" task start --resolve-from-checkout true <registration-args>
"$HOME/.local/bin/jhw-control-host" task child-start <child-args>
"$HOME/.local/bin/jhw-control-host" task contract <contract-args>
"$HOME/.local/bin/jhw-control-host" task completion-ready <evidence-args>
"$HOME/.local/bin/jhw-control-host" task promote <promotion-args>
"$HOME/.local/bin/jhw-control-host" task status --task <tsk-id>
"$HOME/.local/bin/jhw-control-host" task handoff --task <tsk-id>
"$HOME/.local/bin/jhw-control-host" task finish --task <tsk-id> --claim <clm-id> --status <completed|handoff|abandoned>
"$HOME/.local/bin/jhw-control-host" task recover --task <tsk-id> --expect <clm-id> --action <status|force-end|takeover|cleanup>
"$HOME/.local/bin/jhw-control-host" task assert-owner --task <tsk-id> --claim <clm-id>
```

Replace the v3 strict-schema paragraphs after the command block with this exact policy text:

```markdown
`task start`와 `task finish`는 v3 public projection과 caller-coordinate binding을 유지하고 안전한
additive downstream field는 무시합니다. `task child-start`는 `task_id`, `claim_id`, `branch`,
`worktree_ref` 네 좌표만 반환합니다. 나머지 Task command의 result object는 common envelope와
sensitive scan을 통과한 뒤 canonical JSON으로 다시 직렬화합니다. command별 상세 result schema는
`jhw-control` 한 곳에서 관리합니다.

downstream error는 stable code, optional bounded reason, 원래 exit를 보존합니다. host는 command별
code allowlist나 code-to-exit 표를 복제하지 않습니다. workflow 분기에 필요한 `conflicting_claim`,
`retained_claim`, `retained_task`는 canonical coordinate만 남기고 그 밖의 detail은 폐기합니다.

모든 child output은 최대 12 KiB, duplicate-free 단일 JSON, success stdout/error stderr, success command
binding을 만족해야 합니다. credential과 protected config/store/state/checkout path가 raw 또는 encoded
형태로 섞이면 `SENSITIVE_OUTPUT_REJECTED`로 전체 출력을 폐기합니다. raw `jhw-control task`, ambient
credential, 파일 credential fallback은 제공하지 않습니다.

producer rollout 순서는 `producer merge → install.sh 재실행 → clean-shell --contract/preflight
→ jhw-notion Task skill host-only 전환 → approved real Task migration`입니다.
```

- [ ] **Step 4: Run README and launcher contract tests**

Run: `rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'contract or readme_documents or global_task_guidance'`

Expected: all selected tests PASS and the global guidance remains host-only.

- [ ] **Step 5: Commit the approved design status and v4 operator docs**

```bash
rtk git add README.md tests/test_jhw_control_host.py docs/superpowers/specs/2026-08-27-jhw-control-host-v4-lifecycle-design.md
rtk git commit -m "docs(host): publish v4 lifecycle contract"
```

---

### Task 6: Run the producer release gate, review, integrate, and install

**Files:**
- Verify: `scripts/jhw-control-host.py`
- Verify: `tests/test_jhw_control_host.py`
- Verify: `README.md`
- Verify: `docs/superpowers/specs/2026-08-27-jhw-control-host-v4-lifecycle-design.md`
- Runtime installation after reviewed merge: `$HOME/.local/lib/jhw-control-host/jhw-control-host.py`

**Interfaces:**
- Consumes: Tasks 1-5 and existing atomic `install.sh` launcher installation.
- Produces: reviewed producer code on stable `master`, installed/source hash equality, clean v4 contract, and ready preflight for the later consumer plan.

- [ ] **Step 1: Run the complete producer gate**

Run: `rtk python3 -m pytest -q tests`

Run: `rtk shellcheck -x -s bash -S error install.sh scripts/lib/private-file.sh scripts/lib/link-safely.sh`

Run: `rtk python3 -m py_compile scripts/jhw-control-host.py`

Run: `rtk git diff --check`

Expected: all tests PASS and every static command exits 0.

- [ ] **Step 2: Inspect the final branch scope**

Run: `rtk git status --short --branch`

Run: `rtk git diff --stat master...HEAD`

Run: `rtk git diff --check master...HEAD`

Expected: only the #28 v4 launcher, tests, README, approved spec, and implementation-plan commits are present; the worktree is clean.

- [ ] **Step 3: Request one independent code/security review**

Invoke `superpowers:requesting-code-review` against `master...HEAD`. The reviewer must check exact command allowlisting before credential access, recover-action classification, removal of duplicated code/exit maps, additive result handling, coordinate metadata validation, and unchanged sensitive-output scanning.

Expected: no Critical, High, credential/path disclosure, mutation-without-preflight, inventory escape, or unresolved Important finding. Fix review findings with a failing regression test first, rerun Task 6 Step 1, and request re-review of the exact final diff.

- [ ] **Step 4: Integrate through the development-branch workflow**

Invoke `superpowers:finishing-a-development-branch`. Use the local merge path only after review and gates are clean. Preserve unrelated user files in `/home/jhw/ai/opencode/projects/claude-config`; do not reset or delete them.

After integration, run:

`rtk git -C /home/jhw/ai/opencode/projects/claude-config merge-base --is-ancestor task/11bc99e70133-jhw7500-claude-config-28 master`

Expected: exit 0.

- [ ] **Step 5: Revalidate producer files from stable master**

Run: `rtk git -C /home/jhw/ai/opencode/projects/claude-config branch --show-current`

Run: `rtk git -C /home/jhw/ai/opencode/projects/claude-config diff --exit-code HEAD -- install.sh scripts/jhw-control-host.py README.md tests/test_jhw_control_host.py`

Run: `rtk python3 -m pytest -q /home/jhw/ai/opencode/projects/claude-config/tests`

Run: `rtk python3 -m py_compile /home/jhw/ai/opencode/projects/claude-config/scripts/jhw-control-host.py`

Expected: branch is `master`, producer files match `HEAD`, and the post-merge test/compile gate passes.

- [ ] **Step 6: Install from stable master and verify the payload without credential output**

Run: `rtk /home/jhw/ai/opencode/projects/claude-config/install.sh`

Run: `rtk sha256sum /home/jhw/ai/opencode/projects/claude-config/scripts/jhw-control-host.py /home/jhw/.local/lib/jhw-control-host/jhw-control-host.py`

Expected: the two hashes are identical.

Run: `rtk /home/jhw/.local/bin/jhw-control-host --contract`

Expected: name `jhw-control-host`, version `4`, policy `secure-store-only`, and the exact `V4_COMMANDS` inventory.

- [ ] **Step 7: Run clean-environment preflight and stop at the producer boundary**

Run: `rtk env -i HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" PATH=/usr/bin:/bin /home/jhw/.local/bin/jhw-control-host preflight`

Expected: exit 0 with the bounded ready envelope. If the store is locked, ask the user to run `/home/jhw/.local/bin/jhw-control-host unlock` in their own terminal once; never inspect the entered password or stored values.

Stop condition: producer v4 is reviewed, merged, installed, hash-matched, and clean-preflight ready. Create the separate `jhw-notion` consumer plan only after this condition; do not execute #74 migration from the producer plan.
