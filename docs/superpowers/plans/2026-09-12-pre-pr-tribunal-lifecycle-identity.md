# Pre-PR Tribunal Lifecycle Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind every new tribunal verdict and telemetry observation to one durable lifecycle ID so eviction or missing begin telemetry can never reuse stale request history.

**Architecture:** Verdict schema v3 owns a random 128-bit lifecycle ID and telemetry schema v3 copies it into every bound run. Resume accounting selects exact-ID history and propagates incomplete history forward; new-round state is derived instead of duplicated, while schema-v3 terminal runs require monotonic end time. Explicit v1/v2 pending migrations preserve only evidence that can be revalidated and never infer old telemetry identity.

**Tech Stack:** Python 3 standard library, strict JSON dataclasses/parsers, descriptor-anchored `.review` storage, pytest, GitHub-oriented installed skill/probe fixtures.

**Spec:** `docs/superpowers/specs/2026-09-12-pre-pr-tribunal-lifecycle-identity-design.md`

## Global Constraints

- Verdict and telemetry v1/v2 inputs remain readable without automatic rewrite.
- New verdict and telemetry writers emit schema 3 only.
- `lifecycle_id` is exactly 32 lowercase hexadecimal characters generated internally with `secrets.token_hex(16)`.
- Telemetry remains observation-only and never changes report, receipt, verdict gate, or recovery authority.
- A missing current-lifecycle telemetry record yields `accounting_complete=false` and `null` request-derived counts.
- Schema-v3 terminal runs require `ended_monotonic_ns`; schema-v3 running runs require it to be `null`.
- Existing ledger limits remain 16 runs and 128 spans per run.
- Report schema, three-reviewer membership, and CRITICAL/HIGH blocker semantics remain unchanged.
- Every production behavior change follows RED, GREEN, REFACTOR in that order.
- Do not run Yocto or BitBake commands.

## File Structure

- `hooks/pre_pr_tribunal/model.py`: verdict schema constants, lifecycle ID model, and strict wire serialization.
- `hooks/pre_pr_tribunal/verdict_store.py`: v1/v2/v3 parsing, v3 creation, state preservation, and explicit pending migrations.
- `hooks/pre_pr_tribunal/review_context.py`: current installed contract binding to verdict schema 3.
- `hooks/pre_pr_tribunal/gate.py`: mixed-slot schema v2/v3 read behavior without weakening current-contract enforcement.
- `hooks/pre_pr_tribunal/telemetry.py`: telemetry schema v3 wire rules, lifecycle-bound history, and monotonic closure.
- `hooks/pre_pr_tribunal/cli.py`: begin binding, status projection, and `migrate-v2-pending` routing.
- `skills/pre-pr-tribunal/SKILL.md`: operator migration and lifecycle accounting contract.
- `skills/pre-pr-tribunal/references/report-schema.md`: public verdict schema/status examples.
- `scripts/probe-pre-pr-tribunal.py`: installed-runtime v3 lifecycle and migration probes.
- `tests/pre_pr_tribunal/test_model_store.py`: verdict identity, transition, migration, and failure atomicity.
- `tests/pre_pr_tribunal/test_review_context.py`: contract/context compatibility assertions.
- `tests/pre_pr_tribunal/test_gate_adapters.py`: v3 gate projection and legacy reader behavior.
- `tests/pre_pr_tribunal/test_telemetry.py`: telemetry v3 parser/serializer/monotonic invariants.
- `tests/pre_pr_tribunal/test_recovery_telemetry.py`: stale-history, missing-marker, and eviction regressions.
- `tests/pre_pr_tribunal/test_probe_harness.py`: installed lifecycle behavior.
- `tests/pre_pr_tribunal/test_skill_contract.py`: executable documentation contract.

---

### Task 1: Add verdict schema v3 lifecycle identity

**Files:**
- Modify: `hooks/pre_pr_tribunal/model.py:15-18,274-304,376-454`
- Modify: `hooks/pre_pr_tribunal/verdict_store.py:205-355,389-615,780-820,1040-1185`
- Modify: `hooks/pre_pr_tribunal/review_context.py:21-35`
- Modify: `hooks/pre_pr_tribunal/gate.py:200-225`
- Modify: `hooks/pre_pr_tribunal/cli.py:105-155`
- Modify: `hooks/pre_pr_tribunal/telemetry.py:500-585`
- Test: `tests/pre_pr_tribunal/test_model_store.py:1520-1740`
- Test: `tests/pre_pr_tribunal/test_review_context.py:120-205`
- Test: `tests/pre_pr_tribunal/test_gate_adapters.py:130-185`

**Interfaces:**
- Consumes: existing `Verdict`, `ContractBinding`, `ReviewerSlot`, `_atomic_write`, and strict JSON helpers.
- Produces: `VERDICT_SCHEMA_VERSION = 3`, `MIXED_SLOT_VERDICT_SCHEMAS = frozenset((2, 3))`, `Verdict.lifecycle_id: str | None`, and `begin_round(cwd: Path, *, base: str, runtime: str, round_number: int, decisions_path: Path | None = None, now: Callable[[], str] = utc_now, token_hex: Callable[[int], str] = secrets.token_hex) -> Verdict`.
- Produces: `require_current_in_progress(verdict: Verdict) -> Verdict`; schema 1 raises `LEGACY_ADOPTION_REQUIRED`, schema 2 raises `V2_MIGRATION_REQUIRED`, and non-pending schema 3 raises `ROUND_NOT_IN_PROGRESS`. Replace the old `require_v2_in_progress` imports and callers, including telemetry resume, in the same commit.

- [ ] **Step 1: Write failing lifecycle serialization tests**

Add tests with literal expected values:

```python
def test_new_verdict_owns_lifecycle_id_and_preserves_it_through_finalize(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW,
        token_hex=lambda size: "a" * (size * 2),
    )
    assert pending.schema == VERDICT_SCHEMA_VERSION == 3
    assert pending.lifecycle_id == "a" * 32
    assert pending.to_json()["lifecycle_id"] == "a" * 32
    paths = report_paths(git_repo, pending.snapshot)
    final = finalize_round(git_repo, reviewer_paths=paths, now=NOW)
    assert final.lifecycle_id == "a" * 32
    assert read_verdict(git_repo).lifecycle_id == "a" * 32


@pytest.mark.parametrize("value", (None, "A" * 32, "a" * 31, "g" * 32, 7))
def test_schema_three_rejects_invalid_lifecycle_id(git_repo, value):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW,
        token_hex=lambda _size: "b" * 32,
    )
    raw = pending.to_json()
    raw["lifecycle_id"] = value
    write_json(git_repo / ".review/verdict.json", raw)
    with pytest.raises(SchemaError, match="^VERDICT_INVALID$"):
        read_verdict(git_repo)
```

Extend state-transition tests so `record_reviewer_failure`, `submit_reviewer_report`, and round finalization all retain the same literal ID. Add a legacy fixture assertion that a schema-2 verdict parses with `lifecycle_id is None` and its raw bytes do not change.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
rtk pytest -q \
  tests/pre_pr_tribunal/test_model_store.py::test_new_verdict_owns_lifecycle_id_and_preserves_it_through_finalize \
  tests/pre_pr_tribunal/test_model_store.py::test_schema_three_rejects_invalid_lifecycle_id
```

Expected: FAIL because verdict schema is still 2, `begin_round` does not accept `token_hex`, and `Verdict` has no lifecycle field.

- [ ] **Step 3: Implement the strict schema-v3 verdict model**

Use separate constants so schema-2 mixed-slot records remain parseable:

```python
SCHEMA_VERSION = 1
SLOT_VERDICT_SCHEMA_VERSION = 2
VERDICT_SCHEMA_VERSION = 3
MIXED_SLOT_VERDICT_SCHEMAS = frozenset((SLOT_VERDICT_SCHEMA_VERSION, VERDICT_SCHEMA_VERSION))
SUPPORTED_VERDICT_SCHEMAS = frozenset((SCHEMA_VERSION, *MIXED_SLOT_VERDICT_SCHEMAS))
_LIFECYCLE_ID = re.compile(r"[0-9a-f]{32}\Z")
```

Append this optional compatibility field after the existing `contract` field so legacy positional constructors remain readable:

```python
lifecycle_id: str | None = None
```

For schema 3, require a current contract and `_LIFECYCLE_ID.fullmatch(lifecycle_id)`. For schema 2, require the existing contract and `lifecycle_id is None`. For schema 1, require both `contract` and `lifecycle_id` to be `None`. Serialize `lifecycle_id` only for schema 3. Make `ReviewerSlot.to_json` accept both mixed-slot schema versions.

Refactor `_parse_verdict_v2` into `_parse_mixed_verdict(data, *, schema)` and add `lifecycle_id` to `_VerdictFields`; v2 supplies `None`, v3 strictly parses the required field. Keep the slot and receipt parser shared so no report semantics fork.

Generate IDs only inside `begin_round`:

```python
def _lifecycle_id(token_hex: Callable[[int], str]) -> str:
    try:
        value = token_hex(16)
    except Exception:
        raise SchemaError("VERDICT_INVALID") from None
    if not isinstance(value, str) or m._LIFECYCLE_ID.fullmatch(value) is None:
        raise SchemaError("VERDICT_INVALID")
    return value


def begin_round(
    cwd: Path,
    *,
    base: str,
    runtime: str,
    round_number: int,
    decisions_path: Path | None = None,
    now: Callable[[], str] = utc_now,
    token_hex: Callable[[int], str] = secrets.token_hex,
) -> Verdict:
    # Preserve the current preflight and round-transition checks, then construct:
    pending = _new_current_pending(
        snapshot,
        runtime=runtime,
        initial_paths=initial_paths,
        round_number=round_number,
        decisions=decisions,
        history=history,
        contract=current_contract_binding(),
        lifecycle_id=_lifecycle_id(token_hex),
    )
```

Rename `_new_v2_pending` to `_new_current_pending` and make it require `lifecycle_id`. Replace exact `schema == 2` slot checks in model, gate, status-supporting core, and context code with `schema in MIXED_SLOT_VERDICT_SCHEMAS` where the wire shape is shared. Keep installed-contract equality checks exact so an unmigrated v2 pending verdict cannot enter v3 mutation paths.

Update CLI `_status` to project reviewer slots for both schemas in `MIXED_SLOT_VERDICT_SCHEMAS`. This keeps the first schema-v3 commit usable while retaining schema-1's legacy status shape.

Update telemetry resume's internal verdict-store import and call to `require_current_in_progress`; do not change its marker-based history algorithm in this task. Update direct test imports from the old helper name at the same time.

- [ ] **Step 4: Run verdict, context, and gate tests and verify GREEN**

Run:

```bash
rtk pytest -q \
  tests/pre_pr_tribunal/test_model_store.py \
  tests/pre_pr_tribunal/test_review_context.py \
  tests/pre_pr_tribunal/test_gate_adapters.py
```

Expected: PASS with new schema-3 assertions and unchanged v1/v2 reader fixtures.

- [ ] **Step 5: Commit the verdict identity unit**

```bash
rtk git add \
  hooks/pre_pr_tribunal/model.py \
  hooks/pre_pr_tribunal/verdict_store.py \
  hooks/pre_pr_tribunal/review_context.py \
  hooks/pre_pr_tribunal/gate.py \
  hooks/pre_pr_tribunal/cli.py \
  hooks/pre_pr_tribunal/telemetry.py \
  tests/pre_pr_tribunal/test_model_store.py \
  tests/pre_pr_tribunal/test_review_context.py \
  tests/pre_pr_tribunal/test_gate_adapters.py
rtk git commit -m "feat(tribunal): bind verdict lifecycle identity"
```

### Task 2: Add explicit pending migrations to schema v3

**Files:**
- Modify: `hooks/pre_pr_tribunal/verdict_store.py:780-820,1320-1435`
- Modify: `hooks/pre_pr_tribunal/cli.py:80-125,350-390`
- Test: `tests/pre_pr_tribunal/test_model_store.py:3280-3410`

**Interfaces:**
- Consumes: Task 1 `Verdict.lifecycle_id`, `_new_current_pending`, `_read_sealed_report`, `_snapshot_equal`, and `_lifecycle_id`.
- Produces: `migrate_v2_pending_round(cwd: Path, *, token_hex=secrets.token_hex) -> PendingMigrationResult`.
- Produces: `migrate_legacy_pending_round(cwd: Path, *, token_hex=secrets.token_hex) -> LegacyMigrationResult`, retaining its existing result and CLI shapes while adding the schema-v3 lifecycle internally.
- Produces: CLI command `migrate-v2-pending` returning integer `round`, exact A/B/C state strings in `reviewers`, and literal `telemetry_history="unknown"`.

- [ ] **Step 1: Write failing preservation and refusal tests**

Create one v2 mixed verdict with A sealed, B pending after one failure, and C pending. Write A's exact canonical bytes with mode `0600`, set its receipt digest and real `context_sha256`, then assert:

```python
def test_migrate_v2_pending_preserves_authenticated_slots_and_attempts(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW,
        token_hex=lambda _size: "a" * 32,
    )
    raw = json.dumps(report(pending.snapshot, "A")).encode()
    submit_reviewer_report(
        git_repo, reviewer=Reviewer.A, raw=raw, now=NOW,
    )
    with pytest.raises(SchemaError, match="^JSON_INVALID$"):
        submit_reviewer_report(
            git_repo, reviewer=Reviewer.B, raw=b"{", now=NOW,
        )
    native = read_verdict(git_repo)
    legacy = replace(
        native,
        schema=2,
        contract=replace(native.contract, verdict_schema=2),
        lifecycle_id=None,
    )
    write_json(git_repo / ".review/verdict.json", legacy.to_json())
    canonical = git_repo / ".review/inbox/round-1/A.json"
    result = migrate_v2_pending_round(
        git_repo, token_hex=lambda _size: "c" * 32,
    )
    migrated = read_verdict(git_repo)
    assert result.telemetry_history == "unknown"
    assert migrated.schema == 3
    assert migrated.lifecycle_id == "c" * 32
    assert migrated.reviewers["A"] == legacy.reviewers["A"]
    assert migrated.reviewers["B"].attempt_count == 1
    assert migrated.reviewers["B"].last_error == "JSON_INVALID"
    assert canonical.read_bytes() == raw
    assert stat.S_IMODE(canonical.stat().st_mode) == 0o600
```

Parameterize refusal over snapshot drift, report bytes, report mode, receipt digest, report-text contract, and diff-recipe contract. Capture verdict/report bytes before the command and require both to remain byte-identical after each stable error.

Add CLI shape tests:

```python
result = run_cli_bytes(git_repo, "migrate-v2-pending")
assert result.returncode == 0
assert json.loads(result.stdout) == {
    "round": 1,
    "reviewers": {"A": "sealed", "B": "pending", "C": "pending"},
    "telemetry_history": "unknown",
}
```

- [ ] **Step 2: Run migration tests and verify RED**

Run:

```bash
rtk pytest -q \
  tests/pre_pr_tribunal/test_model_store.py -k 'migrate_v2_pending or migration_refuses'
```

Expected: FAIL because the migration function and CLI command do not exist.

- [ ] **Step 3: Implement one-lock v2 migration**

Define a result type with exact fields:

```python
@dataclass(frozen=True)
class PendingMigrationResult:
    round: int
    reviewers: Mapping[str, str]
    telemetry_history: str
```

Inside `migrate_v2_pending_round`, under one `locked_review` scope:

1. Require schema 2 and `GateStatus.IN_PROGRESS`.
2. Capture and compare the current snapshot.
3. Require persisted `report_text` and `diff_recipe` to equal `current_contract_binding()`; allow only the expected verdict-schema difference.
4. Call `_read_sealed_report` for every sealed role before any write.
5. Preserve each pending slot object unchanged.
6. Generate the ID, `replace` schema/contract/lifecycle only, and perform one `_atomic_write`.

```python
migrated = replace(
    legacy,
    schema=m.VERDICT_SCHEMA_VERSION,
    contract=current_contract_binding(),
    lifecycle_id=_lifecycle_id(token_hex),
)
_atomic_write(review_fd, migrated)
return PendingMigrationResult(
    migrated.round,
    {key: migrated.reviewers[key].status for key in "ABC"},
    "unknown",
)
```

Update `migrate_legacy_pending_round` to create current schema 3 with a fresh ID after preserving unauthenticated bytes as attempt evidence. Do not adopt v1 canonical reports as sealed. Inject `token_hex` only as a keyword-only test seam in both migration functions and add a legacy migration assertion for the deterministic schema-v3 ID.

Add `migrate-v2-pending` to the parser without reviewer, path, or lifecycle-ID arguments and route it to the new core function.

- [ ] **Step 4: Run migration and model tests and verify GREEN**

Run:

```bash
rtk pytest -q tests/pre_pr_tribunal/test_model_store.py
```

Expected: PASS, including atomic failure preservation and legacy migration cases.

- [ ] **Step 5: Commit explicit migration**

```bash
rtk git add \
  hooks/pre_pr_tribunal/verdict_store.py \
  hooks/pre_pr_tribunal/cli.py \
  tests/pre_pr_tribunal/test_model_store.py
rtk git commit -m "feat(tribunal): migrate pending verdict identity"
```

### Task 3: Define the compatible telemetry schema v3 envelope

**Files:**
- Modify: `hooks/pre_pr_tribunal/telemetry.py:21-165,250-390,430-485,650-805`
- Modify: `hooks/pre_pr_tribunal/cli.py:175-190`
- Modify: `tests/pre_pr_tribunal/test_telemetry.py:1-130,560-625,760-940`
- Modify: `tests/pre_pr_tribunal/test_recovery_telemetry.py:300-345`

**Interfaces:**
- Consumes: lifecycle ID format from Task 1; do not import verdict-store mutation logic.
- Produces: `TELEMETRY_SCHEMA_VERSION = 3` and `TelemetryRun.lifecycle_id: str | None`.
- Produces: `bind_run(cwd, *, run_id, snapshot, lifecycle_id, invocation=None) -> TelemetryRun`.
- Preserves in this compatibility commit: schema-v2 and schema-v3 `Invocation("new_round", (), ())`; Task 4 removes the schema-v3 duplicate only after exact-ID history selection is active.
- Preserves: schema-v2 optional terminal monotonic end on read.

- [ ] **Step 1: Write failing schema-v3 parser and summary tests**

Update the `running_span` helper to bind with a literal ID and add:

```python
LIFECYCLE = "1" * 32


def test_schema_three_bound_run_records_lifecycle_id(git_repo):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = new_run(git_repo)
    bound = bind_run(
        git_repo, run_id=run.run_id, snapshot=snapshot,
        lifecycle_id=LIFECYCLE,
        invocation=telemetry_module.Invocation("new_round", (), ()),
    )
    raw = json.loads(ledger_path(git_repo).read_bytes())["runs"][0]
    assert raw["lifecycle_id"] == LIFECYCLE
    assert raw["invocation"] == {
        "kind": "new_round", "reused": [], "previously_attempted": [],
    }
    assert summarize_run(git_repo, run_id=bound.run_id)["recovery"]["kind"] == "new_round"


@pytest.mark.parametrize(
    ("status", "ended_at", "ended_monotonic_ns"),
    (("success", NOW(), None), ("running", None, 5)),
)
def test_schema_three_rejects_run_end_state_mismatch(
    git_repo, status, ended_at, ended_monotonic_ns,
):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = new_run(git_repo)
    bind_run(
        git_repo, run_id=run.run_id, snapshot=snapshot,
        lifecycle_id=LIFECYCLE,
    )
    close_run(
        git_repo, run_id=run.run_id, outcome=TelemetryOutcome.SUCCESS,
        reason_code=None, ended_at=NOW(), ended_monotonic_ns=2,
    )
    path = ledger_path(git_repo)
    value = json.loads(path.read_bytes())
    stored = value["runs"][0]
    stored["status"] = status
    stored["ended_at"] = ended_at
    stored["reason_code"] = None
    stored["ended_monotonic_ns"] = ended_monotonic_ns
    corrupted = json.dumps(value).encode()
    path.write_bytes(corrupted)
    with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
        read_ledger(git_repo)
    assert path.read_bytes() == corrupted
```

Add a schema-2 fixture whose terminal run has `ended_monotonic_ns=null`; assert it remains readable and byte-identical.

- [ ] **Step 2: Run telemetry wire tests and verify RED**

Run:

```bash
rtk pytest -q \
  tests/pre_pr_tribunal/test_telemetry.py -k \
  'schema_three_bound_run or schema_three_rejects_run_end or schema_two_terminal'
```

Expected: FAIL because writer schema is 2, lifecycle ID is absent, and schema-v3 end invariants are not implemented.

- [ ] **Step 3: Implement schema-dependent telemetry serialization**

Add `lifecycle_id` after the current defaulted `invocation` and `ended_monotonic_ns` fields:

```python
TELEMETRY_SCHEMA_VERSION = 3
SUPPORTED_TELEMETRY_SCHEMAS = frozenset((1, 2, 3))

@dataclass(frozen=True)
class TelemetryRun:
    lifecycle_id: str | None = None
```

For contract schema 3, serialize `invocation`, `ended_monotonic_ns`, and `lifecycle_id`. Strict parsing rules:

```python
if schema >= 3:
    _require(
        lifecycle_id is None
        if binding.status == "pending"
        else isinstance(lifecycle_id, str) and _LIFECYCLE_ID.fullmatch(lifecycle_id)
    )
    _require((outcome is None) == (ended_monotonic_ns is None))
```

For schemas 2 and 3 in this task, retain `new_round|resume` invocation parsing. Task 4 tightens schema 3 once history no longer needs the marker. Change `_parse_binding` and `_parse_ledger` to accept the explicit supported version set rather than `(1, current)`.

Make `bind_run` require an exact lifecycle ID for new writes and continue accepting the explicit invocation argument. Update `_begin_with_telemetry` to pass `verdict.lifecycle_id` together with the existing `Invocation("new_round", (), ())`. Update every direct schema-v3 `bind_run` test caller with a literal 32-hex lifecycle ID. Task 4 switches new-round binding and recovery derivation atomically.

Keep `close_run(cwd: Path, *, run_id: str, outcome: TelemetryOutcome, reason_code: str | None, ended_at: str, ended_monotonic_ns: int | None = None) -> TelemetryRun` for legacy schema-v2 programmatic callers, but require a non-null value when the selected run's contract schema is 3 before replacement. CLI already supplies the monotonic value.

Update every test or production caller that closes a schema-v3 run to pass `ended_monotonic_ns`. Retain one explicit schema-v2 compatibility fixture with a terminal `ended_monotonic_ns=null`; no current-writer test may rely on the legacy omission.

- [ ] **Step 4: Run the telemetry unit file and verify GREEN**

Run:

```bash
rtk pytest -q \
  tests/pre_pr_tribunal/test_telemetry.py \
  tests/pre_pr_tribunal/test_recovery_telemetry.py
```

Expected: PASS with schema 1/2 compatibility, schema 3 lifecycle binding, and schema 3 end-state strictness.

- [ ] **Step 5: Commit telemetry v3 wire support**

```bash
rtk git add \
  hooks/pre_pr_tribunal/telemetry.py \
  hooks/pre_pr_tribunal/cli.py \
  tests/pre_pr_tribunal/test_telemetry.py \
  tests/pre_pr_tribunal/test_recovery_telemetry.py
rtk git commit -m "feat(tribunal): define telemetry schema three"
```

### Task 4: Bind recovery history to exact lifecycle identity

**Files:**
- Modify: `hooks/pre_pr_tribunal/telemetry.py:475-585`
- Modify: `hooks/pre_pr_tribunal/cli.py:135-205`
- Modify: `tests/pre_pr_tribunal/test_telemetry.py:300-450,560-625`
- Modify: `tests/pre_pr_tribunal/test_recovery_telemetry.py:1-360`

**Interfaces:**
- Consumes: Task 1 `Verdict.lifecycle_id` and Task 3 lifecycle-aware `bind_run`.
- Produces: `_prior_request_history(ledger, snapshot, round_number, lifecycle_id) -> tuple[set[Reviewer], bool]`.
- Produces: every schema-v3 resume run carrying the current verdict lifecycle ID and cumulative `previously_attempted` roles.
- Produces: schema-v3 new-round wire records with `invocation=null`; `_recovery` derives their `kind=new_round` in memory.

- [ ] **Step 1: Add the two reviewer reproductions as failing tests**

Keep the existing same-snapshot regression and add a marker-eviction test that deliberately gives the current marker the oldest wall time, fills the ledger with eligible terminal records plus a non-evictable old incomplete run, then performs two resumes. Assert the first append removes the marker and the second B request is still a rerun because the first resume carried the same ID and prior state.

Use this observable sequence; the round-2 fillers participate in eviction but cannot enter round-1 request history:

```python
def test_resume_ignores_old_requests_after_current_marker_is_evicted(git_repo):
    old = payload(
        git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1",
    )
    dispatch(git_repo, old["telemetry"]["run_id"], "B", 1)
    for role in "ABC":
        payload(git_repo, "submit-report", "--reviewer", role, raw=report(old, role))
    payload(git_repo, "finalize")
    payload(
        git_repo, "telemetry-close", "--run-id", old["telemetry"]["run_id"],
        "--outcome", "success",
    )

    current = payload(
        git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1",
    )
    for role in "AC":
        payload(git_repo, "submit-report", "--reviewer", role, raw=report(current, role))
    current_run_id = current["telemetry"]["run_id"]
    payload(
        git_repo, "telemetry-close", "--run-id", current_run_id,
        "--outcome", "success",
    )

    snapshot = capture_snapshot(git_repo, "master")
    for number in range(14):
        filler = telemetry.create_run(
            git_repo, base_ref="master", runtime="codex", round_number=2,
            started_at="2026-09-13T00:00:00Z", started_monotonic_ns=1,
            token_hex=lambda _size, value=number: f"{value + 1:032x}",
        )
        telemetry.bind_run(
            git_repo, run_id=filler.run_id, snapshot=snapshot,
            lifecycle_id=f"{number + 100:032x}",
            invocation=telemetry.Invocation("new_round", (), ()),
        )
        outcome = (
            telemetry.TelemetryOutcome.INCOMPLETE
            if number == 0 else telemetry.TelemetryOutcome.SUCCESS
        )
        telemetry.close_run(
            git_repo, run_id=filler.run_id, outcome=outcome,
            reason_code="CONTROLLER_INTERRUPTED" if number == 0 else None,
            ended_at="2026-09-13T00:00:01Z", ended_monotonic_ns=2,
        )

    path = git_repo / ".review/telemetry.json"
    ledger = json.loads(path.read_bytes())
    marker = next(item for item in ledger["runs"] if item["run_id"] == current_run_id)
    marker["started_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(ledger))
    path.chmod(0o600)

    first = payload(git_repo, "telemetry-resume", "--runtime", "codex")
    assert all(run.run_id != current_run_id for run in telemetry.read_ledger(git_repo).runs)
    payload(
        git_repo, "telemetry-close", "--run-id", first["run_id"],
        "--outcome", "success",
    )
    second = payload(git_repo, "telemetry-resume", "--runtime", "codex")
    dispatch(git_repo, second["run_id"], "B", 1)
    recovery = payload(
        git_repo, "telemetry-summary", "--run-id", second["run_id"],
    )["recovery"]
    assert recovery["accounting_complete"] is True
    assert recovery["rerun_slot_count"] == 0
```

Add the begin-telemetry failure regression at the CLI boundary:

```python
def test_resume_marks_history_unknown_when_current_begin_telemetry_is_missing(
    git_repo, monkeypatch,
):
    old = payload(
        git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1",
    )
    dispatch(git_repo, old["telemetry"]["run_id"], "B", 1)
    for role in "ABC":
        payload(git_repo, "submit-report", "--reviewer", role, raw=report(old, role))
    payload(git_repo, "finalize")
    payload(
        git_repo, "telemetry-close", "--run-id", old["telemetry"]["run_id"],
        "--outcome", "success",
    )
    original_create_run = telemetry.create_run
    monkeypatch.setattr(
        telemetry, "create_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SchemaError("TELEMETRY_TOO_LARGE")
        ),
    )
    verdict, projection = cli._begin_with_telemetry(
        git_repo,
        SimpleNamespace(base="master", runtime="codex", round=1, decisions=None),
    )
    assert projection == {
        "status": "unavailable", "reason_code": "TELEMETRY_TOO_LARGE",
    }
    monkeypatch.setattr(telemetry, "create_run", original_create_run)
    current = {"snapshot": verdict.snapshot.to_json()}
    for role in "AC":
        payload(git_repo, "submit-report", "--reviewer", role, raw=report(current, role))
    resumed = payload(git_repo, "telemetry-resume", "--runtime", "codex")
    dispatch(git_repo, resumed["run_id"], "B", 1)
    recovery = payload(
        git_repo, "telemetry-summary", "--run-id", resumed["run_id"],
    )["recovery"]
    assert recovery["accounting_complete"] is False
    assert recovery["rerun_slot_count"] is None
```

Import `SimpleNamespace` and the existing `pre_pr_tribunal.cli` module for this direct telemetry-failure injection. Keep report submission and resume on the real subprocess CLI boundary. Extract only deterministic fixture helpers that are shared by at least two tests.

- [ ] **Step 2: Run both regressions and verify RED**

Run:

```bash
rtk pytest -q \
  tests/pre_pr_tribunal/test_recovery_telemetry.py -k \
  'current_marker_is_evicted or current_begin_telemetry_is_missing'
```

Expected before production changes: the eviction case reuses stale history or loses prior attempts, and the missing-begin case reports `accounting_complete=true` with `rerun_slot_count=1`.

- [ ] **Step 3: Replace marker slicing with exact-ID selection**

Change history matching to require lifecycle identity:

```python
matching = [
    run for run in ledger.runs
    if run.round == round_number
    and run.lifecycle_id == lifecycle_id
    and run.binding.status == "bound"
    and all(
        getattr(run.binding, name) == getattr(expected, name)
        for name in (
            "repository", "base_ref", "base_sha", "head_ref", "head_sha",
            "merge_base_sha", "diff_sha256",
        )
    )
    and all(
        run.binding.contract[name] == expected.contract[name]
        for name in ("report_text", "diff_recipe")
    )
]
if not matching:
    return set(), False
```

Delete `lifecycle_start` and all dependency on `Invocation.kind == "new_round"` for boundary selection. Keep `_recovery(run)` consistency checks and propagate any prior incomplete state. Then require schema-v3 persisted invocation to be either `null` for a new round or `kind=resume`, and teach `_recovery` to synthesize the new-round invocation only in memory.

In `resume_run`, require the current schema-3 pending verdict, pass `verdict.lifecycle_id` into history lookup, and construct the new run with the same ID. Continue to union:

- persisted verdict `attempt_count` for pending slots;
- observed dispatch roles from same-ID prior runs;
- `previously_attempted` carried by same-ID resume runs.

In `_begin_with_telemetry`, bind with `verdict.lifecycle_id` and no new-round `Invocation`:

```python
telemetry.bind_run(
    cwd, run_id=run.run_id, snapshot=verdict.snapshot,
    lifecycle_id=verdict.lifecycle_id,
)
```

If create or bind fails, keep the existing unavailable projection and primary verdict behavior.

- [ ] **Step 4: Run recovery telemetry and focused integration tests**

Run:

```bash
rtk pytest -q \
  tests/pre_pr_tribunal/test_recovery_telemetry.py \
  tests/pre_pr_tribunal/test_telemetry.py
```

Expected: PASS, including prior request retention, incomplete propagation, lifecycle isolation, marker eviction, and missing-begin failure.

- [ ] **Step 5: Commit lifecycle-bound recovery**

```bash
rtk git add \
  hooks/pre_pr_tribunal/telemetry.py \
  hooks/pre_pr_tribunal/cli.py \
  tests/pre_pr_tribunal/test_telemetry.py \
  tests/pre_pr_tribunal/test_recovery_telemetry.py
rtk git commit -m "fix(tribunal): bind recovery to lifecycle"
```

### Task 5: Update the installed workflow contract and verify end to end

**Files:**
- Modify: `skills/pre-pr-tribunal/SKILL.md:82-115`
- Modify: `skills/pre-pr-tribunal/references/report-schema.md:1-45`
- Modify: `scripts/probe-pre-pr-tribunal.py:1420-1475`
- Modify: `tests/pre_pr_tribunal/test_probe_harness.py:760-835,920-1035`
- Modify: `tests/pre_pr_tribunal/test_skill_contract.py:400-490,560-690`
- Modify: `tests/pre_pr_tribunal/test_install_integration.py`

**Interfaces:**
- Consumes: schema-v3 CLI and migration behavior from Tasks 1-4.
- Produces: installed documentation and probe behavior that routes schema 1 to `migrate-legacy-pending`, schema 2 to `migrate-v2-pending`, and schema 3 directly to recovery.

- [ ] **Step 1: Write failing installed-contract tests**

Update the installed lifecycle expectations to literal schema 3 and assert the stored verdict lifecycle ID equals the bound telemetry run lifecycle ID:

```python
assert result["status"]["verdict_schema"] == 3
verdict = json.loads((repo / ".review/verdict.json").read_bytes())
ledger = json.loads((repo / ".review/telemetry.json").read_bytes())
assert verdict["lifecycle_id"] == ledger["runs"][0]["lifecycle_id"]
assert ledger["runs"][0]["invocation"] is None
assert result["telemetry_summary"]["recovery"]["kind"] == "new_round"
```

Add a probe fixture for a pending schema-2 mixed verdict. Assert the probe invokes `migrate-v2-pending` before `telemetry-resume`, retains sealed A, and reports recovery accounting unknown rather than dispatching A again.

Update skill contract tests to parse the documented schema routing and require exactly these command mappings:

```python
assert migration_command_by_schema == {
    1: "migrate-legacy-pending",
    2: "migrate-v2-pending",
    3: None,
}
```

- [ ] **Step 2: Run installed-contract tests and verify RED**

Run:

```bash
rtk pytest -q \
  tests/pre_pr_tribunal/test_probe_harness.py \
  tests/pre_pr_tribunal/test_skill_contract.py \
  tests/pre_pr_tribunal/test_install_integration.py
```

Expected: FAIL on schema-2-only documentation/probe assumptions and the missing v2 migration route.

- [ ] **Step 3: Update skill, reference, and probe behavior**

Document this explicit status routing without automatic mutation:

```text
verdict_schema == 1 -> operator runs migrate-legacy-pending, then status
verdict_schema == 2 -> operator runs migrate-v2-pending, then status
verdict_schema == 3 -> do not migrate
```

State that both migrations require explicit user intervention, schema-2 sealed evidence is preserved only after full receipt/file validation, and the first migrated lifecycle resume reports prior telemetry accounting unknown. Update report-schema examples to schema 3 with a 32-hex lifecycle ID while retaining a concise legacy compatibility section.

Modify `_exercise_selective_recovery` to branch on status schema exactly as documented. After migration, fetch status again and require schema 3 before telemetry resume or reviewer dispatch. Keep sealed-role filtering authoritative from the post-migration status.

Do not add lifecycle ID to reviewer prompts, report schema, or authority decisions; it is internal observation binding.

- [ ] **Step 4: Run the affected suite and diff validation**

Run:

```bash
rtk pytest -q \
  tests/pre_pr_tribunal/test_model_store.py \
  tests/pre_pr_tribunal/test_review_context.py \
  tests/pre_pr_tribunal/test_gate_adapters.py \
  tests/pre_pr_tribunal/test_telemetry.py \
  tests/pre_pr_tribunal/test_recovery_telemetry.py \
  tests/pre_pr_tribunal/test_probe_harness.py \
  tests/pre_pr_tribunal/test_skill_contract.py \
  tests/pre_pr_tribunal/test_install_integration.py
rtk git diff --check
```

Expected: all selected tests pass and diff check prints no errors.

- [ ] **Step 5: Run the full repository suite**

Run:

```bash
rtk pytest -q tests
```

Expected: all tests pass with no unexpected warnings or errors.

- [ ] **Step 6: Commit installed contract and probe integration**

```bash
rtk git add \
  skills/pre-pr-tribunal/SKILL.md \
  skills/pre-pr-tribunal/references/report-schema.md \
  scripts/probe-pre-pr-tribunal.py \
  tests/pre_pr_tribunal/test_probe_harness.py \
  tests/pre_pr_tribunal/test_skill_contract.py \
  tests/pre_pr_tribunal/test_install_integration.py
rtk git commit -m "docs(tribunal): route lifecycle schema migration"
```

- [ ] **Step 7: Capture final implementation evidence**

Run:

```bash
rtk git status --short --branch
rtk git log --oneline -8
rtk git diff --check origin/task/1b281bbe4a88-jhw7500-claude-config-115...HEAD
```

Expected: clean worktree, the design/plan plus five implementation commits, and no whitespace errors. Do not push, update the PR, invoke reviewers, or merge until the execution workflow reaches its explicit handoff checkpoint.
