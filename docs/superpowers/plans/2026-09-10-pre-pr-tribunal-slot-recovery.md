# Pre-PR Tribunal Slot Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 유효한 reviewer report를 slot별로 즉시 봉인하고, 형식 오류·timeout·agent 장애가 난 slot만 재실행하면서 실제 blocker와 증거 무결성은 계속 fail-closed로 유지한다.

**Architecture:** #109의 descriptor-anchored report store, strict validation, telemetry 구현을 #124 branch에 먼저 stack한다. Verdict schema v2가 `pending`과 `sealed` reviewer slot, installed contract binding, exact-byte receipt를 정본으로 보관하며, 원자적 submit 경로가 유효한 report를 내용과 무관하게 즉시 봉인한다. Legacy v1 pending state는 A/B/C 전체를 한 번에 검사하는 비선택적 migration으로만 v2에 올리고, finalizer는 세 sealed canonical file을 persisted receipt와 다시 인증한다.

**Tech Stack:** Python 3.10 standard library, pytest 9, Git CLI, descriptor-relative POSIX filesystem operations, Markdown skill contract tests, Claude Code/Codex native reviewer orchestration.

**Spec:** `docs/superpowers/specs/2026-09-10-pre-pr-tribunal-slot-recovery-design.md`

## Global Constraints

- 모든 개발 shell 명령은 `rtk`로 시작한다.
- 현재 #124 Task worktree와 `task/7f238aad8dd8-jhw7500-claude-config-124` branch에서만 변경한다.
- #109 worktree, 그 안의 `.review` state, branch pointer와 installed runtime은 수정하지 않는다.
- 선행 구현은 #109 HEAD `fae27ef988f199eccc777022f3252f3872719c8a`이며, 구현 시작 시 #124에 non-fast-forward merge한다.
- #124가 review·merge·install되기 전에는 새 recovery 기능으로 #109 tribunal을 복구하지 않는다.
- reviewer 역할은 정확히 A/B/C 세 개이며, 세 slot 모두 유효하지 않으면 PASS/FAIL을 만들지 않는다.
- `CRITICAL` 또는 `HIGH` finding 하나라도 있으면 최종 verdict는 FAIL이다. `2/3` pass를 도입하지 않는다.
- 유효한 report는 finding 내용과 관계없이 즉시 sealed되고 같은 round에서 교체하거나 pending으로 되돌릴 수 없다.
- canonical report와 실패 attempt evidence는 current-user-owned, non-symlink regular file이며 ambient `umask`와 무관하게 정확히 mode `0600`이다.
- `finalize` 직전에 A/B/C 각각의 type, owner, mode, exact raw digest와 persisted receipt를 독립적으로 다시 검증한다.
- snapshot, context, installed contract, owner/type/mode 또는 digest drift는 fail-closed다.
- telemetry 오류와 terminal reviewer worktree cleanup 실패는 warning이며 verdict를 바꾸지 않는다.
- reviewer process 생존 여부, worktree identity 또는 cleanup target이 불확실하면 warning으로 낮추지 않고 중단한다.
- 형식 오류는 같은 역할의 format-only retry, timeout/process/dispatch 장애는 같은 역할의 fresh replacement만 허용한다.
- 한 자동 recovery invocation은 slot당 최초 시도 포함 최대 3회이며, 소진해도 sealed peer와 round는 보존한다.
- 실패 attempt evidence는 reviewer별 최근 3개만 보존하고 raw body를 telemetry에 넣지 않는다.
- installed runtime의 report text/diff/verdict contract가 정본이다. candidate branch contract를 reviewer에게 적용하지 않는다.
- v1 terminal verdict는 read-only 호환하며 자동 rewrite하지 않는다.
- v1 pending migration은 reviewer subset을 받지 않고 A/B/C 전체를 검사한다. provenance가 부족한 report는 재사용하지 않는다.
- peer report, peer finding과 peer status는 다른 reviewer context에 포함하지 않는다.
- 새 third-party dependency, permission, credential, secret, environment variable 또는 remote endpoint를 추가하지 않는다.
- 각 구현 Task는 RED 확인, 최소 구현, focused PASS, commit 순서를 지킨다.

Before Task 1, verify the approved design/plan branch and the untouched dependency:

```bash
rtk git status --short --branch
rtk git rev-parse HEAD
rtk git -C /home/jhw/ai/opencode/worktrees/jhw-control/wt-a1a55d42e2c9-jhw7500-claude-config-109 rev-parse HEAD
rtk git merge-base --is-ancestor df1285dbb7655717af3d60c7fc098715a29203dd fae27ef988f199eccc777022f3252f3872719c8a
```

Expected: #124 is clean at the committed plan HEAD; #109 prints exactly
`fae27ef988f199eccc777022f3252f3872719c8a`; the ancestry command exits 0.
If #109 moved or the ancestry check fails, stop without merging.

## Planned File Structure

```text
hooks/pre_pr_tribunal/
  model.py             # verdict schema v2 types plus v1 read compatibility
  review_context.py    # installed contract and deterministic reviewer context digest
  review_store.py      # existing descriptor-anchored private file primitives
  attempt_store.py     # bounded exact failed-attempt evidence and safe rotation
  verdict_store.py     # submit/seal/failure/migration/finalize state transitions
  telemetry.py         # existing observation ledger plus locked legacy provenance reader
  cli.py               # submit, record-failure, migrate, status and authenticated finalize
skills/pre-pr-tribunal/
  SKILL.md             # pending-slot dispatch/retry/replacement and warning boundaries
  references/report-schema.md # sealed receipt and installed-contract output contract
scripts/
  install-pre-pr-tribunal.py   # deterministic installation of new package modules
  probe-pre-pr-tribunal.py     # installed selective-recovery and tamper canaries
tests/pre_pr_tribunal/
  test_model_store.py    # v1/v2 parse, transitions, submit, migration and finalize
  test_review_context.py # deterministic projection/digest and peer isolation
  test_attempt_store.py  # exact private attempt evidence and bounded rotation
  test_review_store.py   # shared primitive regression coverage
  test_telemetry.py      # non-gating behavior and legacy provenance lookup
  test_skill_contract.py # selective retry/replacement and cleanup warning workflow
  test_installer.py      # deterministic package membership
  test_probe_harness.py  # installed end-to-end lifecycle and tamper behavior
hooks/README.md          # operator commands, codes and legacy migration boundary
docs/validation/
  2026-09-10-pre-pr-tribunal-slot-recovery.md # sanitized verification evidence
```

---

### Task 1: Stack the trusted #109 implementation foundation

**Files:**

- Merge: commit `fae27ef988f199eccc777022f3252f3872719c8a`
- Preserve: `docs/superpowers/specs/2026-09-10-pre-pr-tribunal-slot-recovery-design.md`
- Preserve: `docs/superpowers/plans/2026-09-10-pre-pr-tribunal-slot-recovery.md`

**Interfaces:**

- Consumes: #109's `review_store.py`, `telemetry.py`, `store_reviewer_report`, `validate_stored_reviewer_report`, report validation CLI and installed probe.
- Produces: one #124 merge commit with the complete #109 implementation as an ancestor.
- Stop condition: any merge conflict, dependency HEAD mismatch or baseline regression stops this Task; do not edit the #109 worktree to resolve it.

- [ ] **Step 1: Record the two branch tips and prove the shared base**

```bash
rtk git rev-parse HEAD
rtk git rev-parse fae27ef988f199eccc777022f3252f3872719c8a
rtk git merge-base HEAD fae27ef988f199eccc777022f3252f3872719c8a
```

Expected: #124's HEAD contains the approved spec and this plan; the dependency hash is
exact; merge-base is `df1285dbb7655717af3d60c7fc098715a29203dd`.

- [ ] **Step 2: Merge #109 into #124 without changing the #109 branch**

```bash
rtk git merge --no-ff fae27ef988f199eccc777022f3252f3872719c8a -m "merge: stack tribunal validation foundation"
```

Expected: merge exits 0 and creates one merge commit. On conflict, abort this Task and
report the conflicted paths; do not install, cherry-pick around, or modify #109.

- [ ] **Step 3: Run the dependency's focused baseline**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal tests/runtime_hook_installer
```

Expected: PASS with the #109 exact-store, validation, telemetry, installer and probe tests.

- [ ] **Step 4: Run the complete repository baseline**

```bash
rtk python3 -m pytest -q
rtk git status --short --branch
```

Expected: PASS; only committed history is present and the #124 worktree is clean.

---

### Task 2: Add verdict schema v2 reviewer slots with v1 read compatibility

**Files:**

- Modify: `hooks/pre_pr_tribunal/model.py:15-17, 219-335`
- Modify: `hooks/pre_pr_tribunal/verdict_store.py:44-51, 188-381, 697-804`
- Test: `tests/pre_pr_tribunal/test_model_store.py:810-854, 1120-1360`

**Interfaces:**

- Consumes: report/snapshot schema 1 and existing `ReviewerReport`.
- Produces: `VERDICT_SCHEMA_VERSION = 2`, `ContractBinding`, `ReportReceipt`, and a `ReviewerSlot` whose v2 states are `pending|sealed`.
- Preserves: `SCHEMA_VERSION = 1` remains the snapshot/report schema; v1 terminal verdicts remain readable and are never serialized unless explicitly migrated.

- [ ] **Step 1: Write RED tests for independent schema versions and mixed slots**

Add tests with these exact assertions:

```python
def test_verdict_schema_two_does_not_change_report_or_snapshot_schema(git_repo):
    legacy = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    pending = _new_v2_pending(
        legacy.snapshot,
        runtime="codex",
        initial_paths=legacy.initial_paths,
        round_number=1,
        decisions=(),
        history=(),
        contract=ContractBinding(
            REPORT_TEXT_CONTRACT_VERSION, DIFF_RECIPE_VERSION, 2
        ),
    )
    assert pending.schema == VERDICT_SCHEMA_VERSION == 2
    assert pending.snapshot.schema == SCHEMA_VERSION == 1
    assert all(slot.status == "pending" for slot in pending.reviewers.values())
    assert pending.contract.to_json() == {
        "report_text": REPORT_TEXT_CONTRACT_VERSION,
        "diff_recipe": DIFF_RECIPE_VERSION,
        "verdict_schema": 2,
    }


def test_schema_two_parser_accepts_mixed_pending_and_sealed_slots(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A")).encode()
    parsed_report = parse_reviewer_report(
        raw,
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=pending.snapshot,
    )
    sealed = replace(
        pending,
        schema=VERDICT_SCHEMA_VERSION,
        contract=ContractBinding(
            REPORT_TEXT_CONTRACT_VERSION, DIFF_RECIPE_VERSION, 2
        ),
        reviewers={
            "A": ReviewerSlot(
                "sealed",
                report=parsed_report,
                receipt=ReportReceipt(
                    reviewer=Reviewer.A,
                    round=1,
                    path=".review/inbox/round-1/A.json",
                    raw_sha256=hashlib.sha256(raw).hexdigest(),
                    context_sha256="2" * 64,
                    report_contract_version=REPORT_TEXT_CONTRACT_VERSION,
                    attempt=1,
                    provenance="native_submit",
                ),
            ),
            "B": ReviewerSlot("pending", attempt_count=1, last_error="JSON_INVALID"),
            "C": ReviewerSlot("pending"),
        },
    )
    verdict_path = write_json(git_repo / ".review/verdict.json", sealed.to_json())
    verdict_path.chmod(0o600)
    parsed = read_verdict(git_repo)
    assert parsed.reviewers["A"].status == "sealed"
    assert parsed.reviewers["B"].last_error == "JSON_INVALID"
```

Also add negative cases for a sealed slot without report/receipt, a pending slot with a
report, an invalid provenance, an attempt below 1, `pass|fail` with any pending slot, and
`in_progress` with nonzero blocker count.

- [ ] **Step 2: Write RED compatibility tests for v1 terminal and pending verdicts**

Use the existing round/report helpers to produce authentic v1 bytes and assert:

```python
def test_schema_one_terminal_verdict_remains_readable_without_rewrite(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    finalize_round(
        git_repo, reviewer_paths=report_paths(git_repo, pending.snapshot), now=NOW
    )
    path = git_repo / ".review/verdict.json"
    before = path.read_bytes()
    parsed = read_verdict(git_repo)
    assert parsed.schema == 1
    assert parsed.gate.status is GateStatus.PASS
    assert path.read_bytes() == before


def test_schema_one_pending_is_readable_but_new_submit_requires_migration(git_repo):
    begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert read_verdict(git_repo).schema == 1
    with pytest.raises(SchemaError, match="^LEGACY_ADOPTION_REQUIRED$"):
        require_v2_in_progress(read_verdict(git_repo))
```

- [ ] **Step 3: Run the schema tests to verify RED**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'verdict_schema_two or schema_two_parser or schema_one_terminal or schema_one_pending'
```

Expected: FAIL because v2 constants/types and mixed-slot parsing do not exist.

- [ ] **Step 4: Implement the v2 types without changing report schema 1**

In `model.py`, retain `SCHEMA_VERSION = 1` and add these exact domains:

```python
VERDICT_SCHEMA_VERSION = 2
SUPPORTED_VERDICT_SCHEMAS = frozenset((1, VERDICT_SCHEMA_VERSION))
RECEIPT_PROVENANCE = frozenset(("native_submit", "legacy_telemetry_v1"))


@dataclass(frozen=True)
class ContractBinding:
    report_text: int
    diff_recipe: int
    verdict_schema: int

    def to_json(self) -> dict[str, int]:
        return {
            "report_text": self.report_text,
            "diff_recipe": self.diff_recipe,
            "verdict_schema": self.verdict_schema,
        }


@dataclass(frozen=True)
class ReportReceipt:
    reviewer: Reviewer
    round: int
    path: str
    raw_sha256: str
    context_sha256: str | None = None
    report_contract_version: int | None = None
    attempt: int | None = None
    provenance: str | None = None


@dataclass(frozen=True)
class ReviewerSlot:
    # Internal attribute name retained for compatibility; v2 wire key is "state".
    status: str
    report: ReviewerReport | None = None
    receipt: ReportReceipt | None = None
    attempt_count: int = 0
    last_error: str | None = None
```

The v2 wire shapes are exact:

```json
{"state":"pending","attempt_count":0,"last_error":null}
```

```json
{"state":"sealed","report":{"status":"complete","findings":[],"executions":[],"claims":[],"prior_decisions":[]},"receipt":{"raw_sha256":"1111111111111111111111111111111111111111111111111111111111111111","context_sha256":"2222222222222222222222222222222222222222222222222222222222222222","report_contract_version":2,"attempt":1,"provenance":"native_submit"},"attempt_count":1,"last_error":null}
```

`ReportReceipt` moves from `verdict_store.py` to `model.py`, while `verdict_store.py`
re-exports the imported name so #109 callers remain compatible. The first four fields retain
the #109 store receipt. A v2 sealed slot requires every optional field to be non-null and to
match the enclosing reviewer, round and canonical path. Its sealed JSON projection emits
only `raw_sha256`, `context_sha256`, `report_contract_version`, `attempt` and `provenance`
because reviewer, round and path are fixed by the enclosing verdict.

`ReviewerSlot.status` is retained as the internal Python attribute to minimize changes to v1
callers. `ReviewerSlot.to_json(verdict_schema=2)` emits it as wire key `state`; v1 continues
to emit the historical wire key `status`. `Verdict.to_json()` always passes `self.schema` to
the slot serializer.

Add `contract: ContractBinding | None = None` to `Verdict`. `Verdict.snapshot` must pass
`SCHEMA_VERSION` as the first `Snapshot` argument, never `self.schema`.

- [ ] **Step 5: Split v1 and v2 verdict parsing and enforce their invariants**

In `verdict_store.py`, rename the existing body to `_parse_verdict_v1(data)` and add
`_parse_verdict_v2(data)`. Route only after strict top-level JSON loading:

```python
def _parse_verdict(raw: bytes) -> Verdict:
    data = m._load_json(raw, limit=m.MAX_VERDICT_BYTES, too_large="VERDICT_TOO_LARGE")
    if not isinstance(data, dict) or type(data.get("schema")) is not int:
        raise SchemaError("VERDICT_INVALID")
    if data["schema"] == 1:
        return _parse_verdict_v1(data)
    if data["schema"] == m.VERDICT_SCHEMA_VERSION:
        return _parse_verdict_v2(data)
    raise SchemaError("VERDICT_SCHEMA_UNSUPPORTED")
```

For v2 require the added top-level `contract`, allow any mixture of `pending|sealed` while
gate is `in_progress`, require all sealed for `pass|fail`, and compute blocker count only
for terminal states. Keep the existing closure validation for terminal v1 and v2.

- [ ] **Step 6: Keep the production writer on v1 until submit/finalize are both ready**

Do not change `begin_round` or `finalize_round` in this Task. Add one internal constructor for
later Tasks without calling it from the production lifecycle:

```python
def _new_v2_pending(
    snapshot: Snapshot,
    *,
    runtime: str,
    initial_paths: Sequence[str],
    round_number: int,
    decisions: Sequence[Decision],
    history: Sequence[RoundSummary],
    contract: ContractBinding,
) -> Verdict:
    return Verdict(
        m.VERDICT_SCHEMA_VERSION,
        snapshot.repository,
        snapshot.base_ref,
        snapshot.base_sha,
        snapshot.head_ref,
        snapshot.head_sha,
        snapshot.merge_base_sha,
        snapshot.diff_sha256,
        tuple(initial_paths),
        round_number,
        runtime,
        {key: ReviewerSlot("pending") for key in "ABC"},
        tuple(decisions),
        tuple(history),
        GateSummary(GateStatus.IN_PROGRESS, 0),
        snapshot.created_at,
        contract,
    )
```

The existing v1 begin/finalize suite must remain green at this commit. Task 5 switches the
writer to `_new_v2_pending` in the same commit that teaches finalization to consume sealed
v2 slots. `require_v2_in_progress(verdict)` raises exactly `LEGACY_ADOPTION_REQUIRED` for a
v1 pending verdict and is used by the new mutation interfaces only.

- [ ] **Step 7: Run focused and existing state-machine tests**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'schema or verdict or round or closure or blocker'
```

Expected: PASS, including existing three-round and originating-reviewer closure cases.

- [ ] **Step 8: Commit the schema unit**

```bash
rtk git add hooks/pre_pr_tribunal/model.py hooks/pre_pr_tribunal/verdict_store.py tests/pre_pr_tribunal/test_model_store.py
rtk git commit -m "feat: add tribunal reviewer slot state"
```

---

### Task 3: Bind every slot to the installed reviewer context contract

**Files:**

- Create: `hooks/pre_pr_tribunal/review_context.py`
- Create: `tests/pre_pr_tribunal/test_review_context.py`
- Modify: `hooks/pre_pr_tribunal/cli.py:253-301, 330-390`
- Modify: `hooks/pre_pr_tribunal/verdict_store.py:536-570, 697-804`
- Modify: `scripts/install-pre-pr-tribunal.py:42-57`
- Modify: `tests/pre_pr_tribunal/test_installer.py:131-167`

**Interfaces:**

- Consumes: `Verdict`, `Reviewer`, `REPORT_TEXT_CONTRACT_VERSION`, `DIFF_RECIPE_VERSION` and `diff_contract`.
- Produces: `current_contract_binding() -> ContractBinding`, `reviewer_context_body(verdict, reviewer) -> dict[str, object]`, `context_sha256(verdict, reviewer) -> str`, `reviewer_context_envelope(verdict, reviewer) -> dict[str, object]`.
- Preserves: each projection contains only the addressed reviewer's prior findings/decisions and no peer report/status.

- [ ] **Step 1: Write deterministic context and peer-isolation RED tests**

Create `test_review_context.py`. Its local `_v2_pending` helper calls `begin_round` to obtain
a real snapshot, then uses `_new_v2_pending` with `current_contract_binding()`. Its local
`_sealed_slot` helper parses one real report and constructs a `native_submit` receipt with
the raw digest, a fixed valid context digest and attempt 1. Use those helpers in:

```python
def _v2_pending(git_repo):
    legacy = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    return _new_v2_pending(
        legacy.snapshot,
        runtime="codex",
        initial_paths=legacy.initial_paths,
        round_number=1,
        decisions=(),
        history=(),
        contract=current_contract_binding(),
    )


def _sealed_slot(verdict, reviewer):
    value = {
        "schema": 1,
        "reviewer": reviewer.value,
        "round": verdict.round,
        "snapshot": {
            "head_sha": verdict.head_sha,
            "diff_sha256": verdict.diff_sha256,
        },
        "status": "complete",
        "findings": [],
        "executions": [],
        "claims": [],
        "prior_decisions": [],
    }
    raw = json.dumps(value, separators=(",", ":")).encode()
    parsed = parse_reviewer_report(
        raw,
        expected_reviewer=reviewer,
        expected_round=verdict.round,
        snapshot=verdict.snapshot,
    )
    return ReviewerSlot(
        "sealed",
        report=parsed,
        receipt=ReportReceipt(
            reviewer=reviewer,
            round=verdict.round,
            path=f".review/inbox/round-{verdict.round}/{reviewer.value}.json",
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            context_sha256="2" * 64,
            report_contract_version=REPORT_TEXT_CONTRACT_VERSION,
            attempt=1,
            provenance="native_submit",
        ),
        attempt_count=1,
    )


def test_context_digest_is_over_canonical_body_and_is_stable(git_repo):
    pending_verdict = _v2_pending(git_repo)
    body = reviewer_context_body(pending_verdict, Reviewer.A)
    canonical = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    envelope = reviewer_context_envelope(pending_verdict, Reviewer.A)
    assert envelope == {
        **body,
        "context_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    assert context_sha256(pending_verdict, Reviewer.A) == envelope["context_sha256"]


def test_sealing_peer_does_not_change_pending_reviewer_context(git_repo):
    pending_verdict = _v2_pending(git_repo)
    before = context_sha256(pending_verdict, Reviewer.C)
    changed = replace(
        pending_verdict,
        reviewers={
            **pending_verdict.reviewers,
            "A": _sealed_slot(pending_verdict, Reviewer.A),
        },
    )
    assert context_sha256(changed, Reviewer.C) == before
    body = reviewer_context_body(changed, Reviewer.C)
    assert "reviewers" not in body
    assert all(item["reviewer"] == "C" for item in body["own_prior_findings"])
    assert all(
        item["finding_ref"]["reviewer"] == "C" for item in body["own_decisions"]
    )


def test_context_rejects_installed_contract_drift(git_repo):
    pending_verdict = _v2_pending(git_repo)
    drifted = replace(
        pending_verdict,
        contract=replace(pending_verdict.contract, report_text=999),
    )
    with pytest.raises(SchemaError, match="^CONTRACT_DRIFT$"):
        reviewer_context_envelope(drifted, Reviewer.A)
```

The `_v2_pending` and `_sealed_slot` helpers live only in this test module; production code
receives the explicit `Verdict` and `Reviewer` values shown above.

- [ ] **Step 2: Run the context tests to verify RED**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_review_context.py
```

Expected: collection FAILS because `pre_pr_tribunal.review_context` does not exist.

- [ ] **Step 3: Extract the context projection and canonical digest**

Move `_snapshot`, `_context_decision` and `_context` semantics out of `cli.py`. Implement:

```python
def current_contract_binding() -> ContractBinding:
    return ContractBinding(
        report_text=REPORT_TEXT_CONTRACT_VERSION,
        diff_recipe=DIFF_RECIPE_VERSION,
        verdict_schema=VERDICT_SCHEMA_VERSION,
    )


def context_sha256(verdict: Verdict, reviewer: Reviewer) -> str:
    body = reviewer_context_body(verdict, reviewer)
    raw = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def reviewer_context_envelope(
    verdict: Verdict, reviewer: Reviewer
) -> dict[str, object]:
    if verdict.contract != current_contract_binding():
        raise SchemaError("CONTRACT_DRIFT")
    if verdict.reviewers[reviewer.value].status != "pending":
        raise SchemaError("REVIEWER_SLOT_SEALED")
    body = reviewer_context_body(verdict, reviewer)
    return {**body, "context_sha256": context_sha256(verdict, reviewer)}
```

The body retains the existing schema 1, snapshot, own history/decisions, limits, report
text contract and reproducible diff contract. It never serializes `verdict.reviewers`.

- [ ] **Step 4: Wire begin/context to the one installed binding**

Import `current_contract_binding` in `verdict_store.py` and remove its temporary Task 2
definition. Change CLI `context` to emit `reviewer_context_envelope(read_verdict(cwd), reviewer)`.
Add `review_context.py` to `PACKAGE_NAMES` and the deterministic installer expectation.

- [ ] **Step 5: Run context, CLI and installer tests**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_review_context.py
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'cli_json_only or context'
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py -k 'package or source'
```

Expected: PASS; the installed package plan includes `review_context.py` exactly once.

- [ ] **Step 6: Commit context binding**

```bash
rtk git add hooks/pre_pr_tribunal/review_context.py hooks/pre_pr_tribunal/cli.py hooks/pre_pr_tribunal/verdict_store.py scripts/install-pre-pr-tribunal.py tests/pre_pr_tribunal/test_review_context.py tests/pre_pr_tribunal/test_model_store.py tests/pre_pr_tribunal/test_installer.py
rtk git commit -m "feat: bind tribunal slots to installed context"
```

---

### Task 4: Store bounded failure evidence and atomically submit/seal one report

**Files:**

- Create: `hooks/pre_pr_tribunal/attempt_store.py`
- Create: `tests/pre_pr_tribunal/test_attempt_store.py`
- Modify: `hooks/pre_pr_tribunal/review_store.py:91-215, 338-411`
- Modify: `hooks/pre_pr_tribunal/verdict_store.py:44-51, 556-664`
- Modify: `tests/pre_pr_tribunal/test_model_store.py:854-1035`
- Modify: `scripts/install-pre-pr-tribunal.py:42-57`
- Modify: `tests/pre_pr_tribunal/test_installer.py:131-167`

**Interfaces:**

- Consumes: v2 in-progress verdict, a `Reviewer`, exact raw bytes and installed context binding.
- Produces: `append_attempt_evidence`, `submit_reviewer_report`, `record_reviewer_failure`, immutable persisted receipts and recoverable orphan publication behavior.
- Stable failures: `REVIEWER_SLOT_SEALED`, `CONTRACT_DRIFT`, `SNAPSHOT_CHANGED`, `REVIEWER_FAILURE_INVALID`, `ATTEMPT_EVIDENCE_UNSAFE`, existing parser codes, `FILE_UNSAFE`, `REPORT_WRITE_FAILED`, `VERDICT_WRITE_FAILED`.

- [ ] **Step 1: Write exact-private and bounded-rotation RED tests**

Create `test_attempt_store.py` and use the real descriptor lock:

```python
@pytest.mark.parametrize("mask", (0o000, 0o022, 0o077))
def test_failed_attempt_preserves_exact_bytes_and_private_metadata(git_repo, mask):
    raw = b'{"schema":1\r'
    previous = os.umask(mask)
    try:
        with locked_review(git_repo, create=True) as review_fd:
            evidence = append_attempt_evidence(
                review_fd,
                round_number=1,
                reviewer=Reviewer.C,
                sequence=1,
                reason_code="JSON_INVALID",
                raw=raw,
            )
    finally:
        os.umask(previous)
    raw_path = git_repo / evidence.raw_path
    meta_path = git_repo / evidence.metadata_path
    assert raw_path.read_bytes() == raw
    assert stat.S_IMODE(raw_path.lstat().st_mode) == 0o600
    assert stat.S_IMODE(meta_path.lstat().st_mode) == 0o600
    assert json.loads(meta_path.read_bytes()) == {
        "attempt": 1,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "reason_code": "JSON_INVALID",
        "reviewer": "C",
        "round": 1,
    }
```

Add cases for no-raw timeout metadata, symlink/FIFO/wrong-owner rejection, sequence reuse,
and sequences 1..5 retaining only pairs 3..5. Inject failure before rotation and assert no
unowned or unsafe name is unlinked.

- [ ] **Step 2: Write submit/seal and failure-recording RED tests**

Add to `test_model_store.py`:

```python
def test_submit_valid_report_seals_only_its_slot(git_repo):
    pending = write_v2_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode() + b"\n"
    receipt = submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    stored = read_verdict(git_repo)
    assert stored.reviewers["A"].status == "sealed"
    assert stored.reviewers["B"].status == stored.reviewers["C"].status == "pending"
    assert stored.reviewers["A"].receipt == receipt
    assert receipt.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.context_sha256 == context_sha256(pending, Reviewer.A)
    assert (git_repo / ".review/inbox/round-1/A.json").read_bytes() == raw


def test_submit_malformed_report_records_attempt_without_touching_peers(git_repo):
    write_v2_pending(git_repo)
    with pytest.raises(SchemaError, match="^JSON_INVALID$"):
        submit_reviewer_report(git_repo, reviewer=Reviewer.C, raw=b'{"schema":1', now=NOW)
    stored = read_verdict(git_repo)
    assert stored.reviewers["C"].attempt_count == 1
    assert stored.reviewers["C"].last_error == "JSON_INVALID"
    assert all(stored.reviewers[key].status == "pending" for key in "ABC")


def test_record_timeout_changes_only_pending_slot(git_repo):
    write_v2_pending(git_repo)
    slot = record_reviewer_failure(
        git_repo, reviewer=Reviewer.B, reason_code="REVIEWER_TIMEOUT"
    )
    assert slot.attempt_count == 1 and slot.last_error == "REVIEWER_TIMEOUT"
```

Add this test helper beside the existing `report_paths` helper so Task 4 can exercise the
v2-only mutation before Task 5 switches the production `begin_round` writer:

```python
def write_v2_pending(git_repo):
    legacy = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    pending = _new_v2_pending(
        legacy.snapshot,
        runtime="codex",
        initial_paths=legacy.initial_paths,
        round_number=1,
        decisions=(),
        history=(),
        contract=current_contract_binding(),
    )
    write_json(git_repo / ".review/verdict.json", pending.to_json()).chmod(0o600)
    return read_verdict(git_repo)
```

Add regressions proving a blocker-containing valid report seals, sealed report replacement
returns `REVIEWER_SLOT_SEALED`, contract/snapshot drift leaves all bytes unchanged, and a
canonical report published before an injected verdict-write failure is automatically sealed
on the next submit without replacing its valid bytes.

- [ ] **Step 3: Run the attempt and submit tests to verify RED**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_attempt_store.py
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'submit_valid or submit_malformed or record_timeout or blocker_containing or published_before'
```

Expected: collection/execution FAILS because attempt and submit interfaces do not exist.

- [ ] **Step 4: Implement the bounded attempt store**

In `attempt_store.py` define:

```python
MAX_RETAINED_ATTEMPTS = 3
OPERATIONAL_FAILURE_CODES = frozenset((
    "DISPATCH_FAILED",
    "REVIEWER_FAILED",
    "REVIEWER_TIMEOUT",
))

REPORT_RETRYABLE_CODES = frozenset((
    "BEHAVIOR_EVIDENCE_REQUIRED",
    "CLAIM_EVIDENCE_REQUIRED",
    "CLAIM_ID_DUPLICATE",
    "CLAIM_SCHEMA_INVALID",
    "EVIDENCE_SECRET_DETECTED",
    "EXECUTION_ID_DUPLICATE",
    "EXECUTION_ID_INVALID",
    "EXECUTION_REFERENCE_INVALID",
    "EXECUTION_SCHEMA_INVALID",
    "FINDING_ID_DUPLICATE",
    "FINDING_ID_INVALID",
    "FINDING_REVIEWER_MISMATCH",
    "JSON_DUPLICATE_KEY",
    "JSON_INVALID",
    "PATH_INVALID",
    "PRIOR_DECISION_RESPONSE_INVALID",
    "REPLACEMENT_FINDING_REQUIRED",
    "REPORT_NOT_TERMINAL",
    "REPORT_REVIEWER_MISMATCH",
    "REPORT_ROUND_MISMATCH",
    "REPORT_SCHEMA_INVALID",
    "REPORT_SNAPSHOT_MISMATCH",
    "REPORT_TOO_LARGE",
    "TEXT_INVALID",
    "TEXT_TOO_LARGE",
))


@dataclass(frozen=True)
class AttemptEvidence:
    reviewer: Reviewer
    round: int
    sequence: int
    reason_code: str
    raw_sha256: str | None
    raw_path: str | None
    metadata_path: str
```

Expose `append_attempt_evidence(review_fd: int, *, round_number: int, reviewer: Reviewer,
sequence: int, reason_code: str, raw: bytes | None) -> AttemptEvidence`. The implementation
creates exact `0700` directories, publishes
`attempt-N.raw` and `attempt-N.meta.json` by calling `atomic_create_bytes` with
`exact_mode=0o600`, and rotates only metadata-proven oldest complete pairs after reopening
and verifying their type, owner, mode and reviewer/round/sequence. It never follows links or
deletes an unresolved name. The body performs descriptor-relative create, verify, fsync and
rotate operations characterized by the tests and does not use pathname-recursive deletion.

- [ ] **Step 5: Implement submit and operational failure state transitions**

Expose in `verdict_store.py`:

```python
def submit_reviewer_report(
    cwd: Path,
    *,
    reviewer: Reviewer,
    raw: bytes,
    now: Callable[[], str] = utc_now,
) -> ReportReceipt:
    """Validate exact bytes, publish the canonical report, and seal one v2 slot."""


def record_reviewer_failure(
    cwd: Path, *, reviewer: Reviewer, reason_code: str
) -> ReviewerSlot:
    """Persist one bounded operational failure for a still-pending v2 slot."""
```

Under one `.review` lock, both functions require v2 `IN_PROGRESS`, a pending target slot,
matching snapshot and `current_contract_binding()`. `submit_reviewer_report` validates raw
bytes before canonical publication. Catch only codes in `REPORT_RETRYABLE_CODES`; such a
parser failure appends exact attempt evidence, increments only that slot's `attempt_count`,
sets `last_error` to the parser code, atomically persists the verdict, then re-raises the
parser code. Storage, snapshot, contract and verdict errors bypass that catch and remain
integrity failures. Success publishes canonical `X.json` mode `0600`, then persists
`ReportReceipt(reviewer, round, canonical_path, raw_sha256, context_sha256,
REPORT_TEXT_CONTRACT_VERSION, attempt_count + 1, "native_submit")` and clears
`last_error`.

If the slot is pending but a safe canonical file already exists, validate it first. A valid
file is sealed regardless of its finding contents and regardless of newly supplied bytes;
an invalid/unsafe file is never overwritten by normal submit. This is the crash-recovery path
for canonical publication followed by verdict-write failure.

`record_reviewer_failure` accepts only `DISPATCH_FAILED`, `REVIEWER_FAILED`, or
`REVIEWER_TIMEOUT`, records metadata with `raw=None`, increments that slot, and never changes
a peer or a sealed slot.

- [ ] **Step 6: Add the module to installation and run focused tests**

Add `attempt_store.py` to `PACKAGE_NAMES`, then run:

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_attempt_store.py
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_review_store.py
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'submit or reviewer_failure or store_reviewer_report'
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py -k 'package or source'
```

Expected: PASS; existing #109 compatibility store tests remain green.

- [ ] **Step 7: Commit per-slot submission**

```bash
rtk git add hooks/pre_pr_tribunal/attempt_store.py hooks/pre_pr_tribunal/review_store.py hooks/pre_pr_tribunal/verdict_store.py scripts/install-pre-pr-tribunal.py tests/pre_pr_tribunal/test_attempt_store.py tests/pre_pr_tribunal/test_model_store.py tests/pre_pr_tribunal/test_review_store.py tests/pre_pr_tribunal/test_installer.py
rtk git commit -m "feat: seal valid tribunal reports per slot"
```

---

### Task 5: Authenticate finalization and migrate legacy pending rounds non-selectively

**Files:**

- Modify: `hooks/pre_pr_tribunal/telemetry.py:147-370`
- Modify: `hooks/pre_pr_tribunal/attempt_store.py`
- Modify: `hooks/pre_pr_tribunal/verdict_store.py:664-935`
- Modify: `tests/pre_pr_tribunal/test_model_store.py:972-1360`
- Modify: `tests/pre_pr_tribunal/test_telemetry.py:225-321, 581-656`

**Interfaces:**

- Consumes: three v2 sealed slots, canonical report files, v1 pending verdict and optional safe v1 telemetry provenance.
- Produces: `finalize_round(cwd, *, reviewer_paths=None, now=utc_now)`, `migrate_legacy_pending_round(cwd) -> LegacyMigrationResult`, and locked telemetry lookup.
- Preserves: v1 terminal read compatibility; telemetry corruption never changes a new v2 verdict or blocks its normal submit/finalize path.

- [ ] **Step 1: Write authenticated-finalize RED tests**

Add cases with this core assertion:

```python
@pytest.mark.parametrize("tamper", ("bytes", "mode", "symlink"))
def test_finalize_authenticates_every_sealed_receipt(git_repo, tamper):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    for key in "ABC":
        submit_reviewer_report(
            git_repo,
            reviewer=Reviewer(key),
            raw=json.dumps(report(pending.snapshot, key)).encode(),
            now=NOW,
        )
    target = git_repo / ".review/inbox/round-1/A.json"
    if tamper == "bytes":
        target.write_bytes(target.read_bytes() + b" ")
    elif tamper == "mode":
        target.chmod(0o644)
    else:
        original = target.with_suffix(".original")
        target.rename(original)
        target.symlink_to(original.name)
    expected = "REPORT_BYTES_MISMATCH" if tamper == "bytes" else "FILE_UNSAFE"
    with pytest.raises(SchemaError, match=f"^{expected}$"):
        finalize_round(git_repo, now=NOW)
    assert read_verdict(git_repo).gate.status is GateStatus.IN_PROGRESS
```

Add `A sealed/B sealed/C pending -> ROUND_NOT_READY`, three clean reports -> PASS,
one sealed HIGH -> FAIL, closure response mismatch -> existing closure code, and supplied
path alias -> `REPORT_PATH_INVALID`.

- [ ] **Step 2: Write legacy all-slot migration RED tests**

Add `write_legacy_pending_with_reports(git_repo, valid, malformed, telemetry_valid)` beside
the existing report helpers. It starts from a real snapshot, writes the strict v1 all-pending
wire shape (`schema: 1`, no `contract`, and `{"status":"pending"}` for A/B/C), stores each
requested canonical report through `store_reviewer_report`, and creates one bound telemetry
run through `create_run`/`bind_run`. For every name in `telemetry_valid`, it starts and
finishes successful `REPORT_STORE` and `REPORT_VALIDATION` spans with reviewer attempt 1;
malformed reviewers receive exact `b'{"schema":1'` bytes and no successful validation span.
Use that helper in:

```python
def test_legacy_migration_adopts_all_proven_valid_slots_in_one_batch(git_repo):
    write_legacy_pending_with_reports(
        git_repo, valid=("A", "B"), malformed=("C",), telemetry_valid=("A", "B")
    )
    result = migrate_legacy_pending_round(git_repo)
    assert result.reviewers == {
        "A": "sealed",
        "B": "sealed",
        "C": "pending:JSON_INVALID",
    }
    migrated = read_verdict(git_repo)
    assert migrated.schema == 2
    assert [migrated.reviewers[key].status for key in "ABC"] == [
        "sealed", "sealed", "pending"
    ]
    assert all(
        migrated.reviewers[key].receipt.provenance == "legacy_telemetry_v1"
        for key in "AB"
    )
```

Add cases proving the command has no reviewer/subset parameter, a valid report without a
matching successful validation span becomes pending, corrupted/missing telemetry adopts zero
but still migrates safely, blocker-containing valid A is adopted, unsafe canonical metadata
stops before verdict mutation, and terminal v1 returns `LEGACY_MIGRATION_NOT_ALLOWED`.

- [ ] **Step 3: Run finalize/migration tests to verify RED**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'finalize_authenticates or round_not_ready or legacy_migration'
```

Expected: FAIL because finalizer still consumes an all-pending v1 shape and migration does
not exist.

- [ ] **Step 4: Switch new rounds to v2 and authenticate canonical receipts**

In `begin_round`, replace the old inline schema-1 `Verdict` pending construction with the
Task 2 `_new_v2_pending` constructor, passing the captured snapshot, runtime, initial paths,
round number, decisions, history and `current_contract_binding()`. Round 2/3 continue to
derive history and decisions from the previous terminal verdict; a legacy v1 pending round
is never silently restarted or rewritten by `begin_round`.

Replace the v2 finalizer's caller-selected report loading with:

```python
def _read_sealed_report(
    review_fd: int, verdict: Verdict, reviewer: Reviewer
) -> ReviewerReport:
    slot = verdict.reviewers[reviewer.value]
    if slot.status != "sealed" or slot.report is None or slot.receipt is None:
        raise SchemaError("ROUND_NOT_READY")
    raw = _read_canonical_report(review_fd, verdict.round, reviewer, exact_mode=0o600)
    if hashlib.sha256(raw).hexdigest() != slot.receipt.raw_sha256:
        raise SchemaError("REPORT_BYTES_MISMATCH")
    parsed, digest = m.validate_report_bytes(
        raw,
        expected_reviewer=reviewer,
        expected_round=verdict.round,
        snapshot=verdict.snapshot,
    )
    if digest != slot.receipt.raw_sha256 or parsed != slot.report:
        raise SchemaError("REPORT_RECEIPT_MISMATCH")
    if slot.receipt.context_sha256 != context_sha256(verdict, reviewer):
        raise SchemaError("CONTEXT_DRIFT")
    return parsed
```

`finalize_round` captures and compares the snapshot, requires the stored contract to equal
the installed contract, calls `_read_sealed_report` independently for A/B/C, then performs
existing closure and CRITICAL/HIGH aggregation before one atomic terminal verdict write.
Accept `reviewer_paths: Mapping[str, Path] | None = None` only for CLI compatibility; when
provided, require exactly A/B/C and each resolved path to equal its fixed canonical path.

- [ ] **Step 5: Add a descriptor-locked legacy telemetry provenance reader**

In `telemetry.py` expose:

```python
def validated_reviewer_provenance(
    review_fd: int, *, verdict: Verdict, reviewer: Reviewer
) -> bool:
    """Return only whether safe v1 telemetry proves store+validation for this binding."""
```

It reads `telemetry.json` through the already-held review descriptor, maps missing/invalid
telemetry to `False`, and returns true only for a run with exact repository/base/head/
merge-base/diff, round, producer runtime, `report_text == 2`, `diff_recipe == 1`, and
successful `REPORT_STORE` plus `REPORT_VALIDATION` spans for that reviewer. It does not
return raw spans or make telemetry a dependency of normal v2 operations.

- [ ] **Step 6: Implement non-selective legacy migration**

Define:

```python
@dataclass(frozen=True)
class LegacyMigrationResult:
    round: int
    reviewers: Mapping[str, str]


def migrate_legacy_pending_round(cwd: Path) -> LegacyMigrationResult:
    """Inspect A/B/C together and atomically publish one v2 pending verdict."""
```

Under one `.review` lock, require a v1 `IN_PROGRESS` all-pending verdict and unchanged
snapshot. Pre-scan all three fixed canonical paths. A report becomes sealed only when file
metadata is safe, strict validation succeeds and `validated_reviewer_provenance` is true.
Its receipt uses the exact raw digest, deterministic current context digest, report contract
2, next attempt number and provenance `legacy_telemetry_v1`.

Missing, invalid or insufficient-provenance files become pending with a stable reason. Move
each safe rejected canonical file by descriptor-relative atomic rename into its attempt
evidence location before allowing a future submit; never overwrite it. Any symlink, FIFO,
wrong owner/mode, rename ambiguity or unresolved target stops the whole migration before
writing v2 verdict. The function takes no reviewer collection and always returns A/B/C.

- [ ] **Step 7: Run finalization, migration and telemetry regressions**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'finalize or migration or closure or round'
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_telemetry.py
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_attempt_store.py
```

Expected: PASS; telemetry corruption remains non-gating for ordinary v2 lifecycle.

- [ ] **Step 8: Commit authenticated finalization and migration**

```bash
rtk git add hooks/pre_pr_tribunal/telemetry.py hooks/pre_pr_tribunal/attempt_store.py hooks/pre_pr_tribunal/verdict_store.py tests/pre_pr_tribunal/test_model_store.py tests/pre_pr_tribunal/test_telemetry.py tests/pre_pr_tribunal/test_attempt_store.py
rtk git commit -m "feat: authenticate and migrate tribunal slots"
```

---

### Task 6: Expose selective recovery through the CLI

**Files:**

- Modify: `hooks/pre_pr_tribunal/cli.py:74-112, 253-390`
- Modify: `tests/pre_pr_tribunal/test_model_store.py:1360-end`

**Interfaces:**

- Consumes: exact stdin report bytes, stable reviewer failure codes and current verdict.
- Produces: `submit-report --reviewer X`, `record-failure --reviewer X --reason CODE`, `migrate-legacy-pending`, bounded per-slot `status`, and receipt-authenticated `finalize`.
- Preserves: one-line JSON success output, one bounded `PRE_PR_TRIBUNAL:` stable-code failure line, usage exit 2, read-only `validate-report`, and v1-only compatibility for `store-report`.

- [ ] **Step 1: Write CLI RED tests for submit, retry state and status**

Add subprocess tests with exact projections:

```python
def test_cli_submit_seals_one_slot_and_status_exposes_no_report_body(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode()
    submitted = run_cli_bytes(git_repo, "submit-report", "--reviewer", "A", input=raw)
    payload = json.loads(submitted.stdout)
    assert submitted.returncode == 0 and payload["state"] == "sealed"
    assert payload["reviewer"] == "A"
    assert payload["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    status = json.loads(run_cli_bytes(git_repo, "status").stdout)
    assert status["reviewers"] == {
        "A": {
            "state": "sealed",
            "attempt_count": 1,
            "last_error": None,
            "raw_sha256": payload["raw_sha256"],
            "context_sha256": payload["context_sha256"],
            "report_contract_version": REPORT_TEXT_CONTRACT_VERSION,
            "provenance": "native_submit",
        },
        "B": {"state": "pending", "attempt_count": 0, "last_error": None},
        "C": {"state": "pending", "attempt_count": 0, "last_error": None},
    }
    assert "findings" not in json.dumps(status)
```

Add malformed stdin returning its parser code while status shows only the failed slot,
`record-failure` whitelist/usage tests, `context` rejection for sealed A, migration output
containing exactly A/B/C stable states, finalize without path flags, all-or-none legacy path
flags, and every command's output byte bound.

- [ ] **Step 2: Run the new CLI tests to verify RED**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'cli_submit or cli_record_failure or cli_migrate or cli_status_slots or cli_finalize_without'
```

Expected: FAIL with unknown commands or old status shape.

- [ ] **Step 3: Add exact parser surfaces and projections**

Extend `_parser()` with:

```python
submit = commands.add_parser("submit-report", add_help=False)
submit.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
failure = commands.add_parser("record-failure", add_help=False)
failure.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
failure.add_argument(
    "--reason",
    required=True,
    choices=("DISPATCH_FAILED", "REVIEWER_FAILED", "REVIEWER_TIMEOUT"),
)
commands.add_parser("migrate-legacy-pending", add_help=False)
```

Make the existing three finalize paths optional but require all-or-none when supplied.
Successful `submit-report` returns reviewer, round, `state: sealed`, raw/context digests,
contract version, attempt and provenance. `record-failure` returns only pending state, attempt
count and last error. `status` returns those fields for every slot and adds the persisted
receipt fields for sealed slots, never report contents or findings. Migration returns round
plus exactly A/B/C result codes.

- [ ] **Step 4: Retain compatibility commands without allowing v2 bypass**

Keep `validate-report --source stored` read-only for pending or sealed canonical reports and
return its raw digest. Keep `store-report [--replace-pending-recovery]` only for v1 pending
state so an old operator receives a bounded migration path; on v2 return
`LEGACY_COMMAND_NOT_ALLOWED`. It must never create an unsealed canonical report in v2.

- [ ] **Step 5: Run CLI and complete model-store tests**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py
```

Expected: PASS with both existing bounded error behavior and the new slot projections.

- [ ] **Step 6: Commit the CLI contract**

```bash
rtk git add hooks/pre_pr_tribunal/cli.py tests/pre_pr_tribunal/test_model_store.py
rtk git commit -m "feat: expose selective tribunal recovery CLI"
```

---

### Task 7: Rewrite the controller Skill around sealed pending slots

**Files:**

- Modify: `skills/pre-pr-tribunal/SKILL.md:10-130`
- Modify: `skills/pre-pr-tribunal/references/report-schema.md`
- Modify: `tests/pre_pr_tribunal/test_skill_contract.py:66-424`
- Modify: `hooks/README.md`

**Interfaces:**

- Consumes: `status`, `context`, `submit-report`, `record-failure`,
  `migrate-legacy-pending`, `validate-report --source stored`, `finalize`.
- Produces: controller procedure that dispatches only pending roles, seals every valid result
  immediately, retries only the failed role and treats observational failures as warnings.
- Required sub-skill: before editing `SKILL.md`, read and apply
  `superpowers:writing-skills` so baseline and pressure scenarios precede wording changes.

- [ ] **Step 1: Record the current skill baseline and write pressure RED tests**

First run the existing contract test unchanged:

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_skill_contract.py
```

Then replace the old full-panel recovery assertions with explicit scenarios:

```python
def test_pending_recovery_dispatches_only_pending_slots_and_never_replaces_sealed():
    match = re.search(
        r"<!-- pending-recovery-contract -->([\s\S]*?)"
        r"<!-- pending-recovery-contract-end -->",
        text("SKILL.md"),
    )
    assert match is not None
    body = match.group(1)
    assert "status" in body
    assert "migrate-legacy-pending" in body
    assert "dispatch only reviewers whose slot is `pending`" in body
    assert "never rerun or replace a `sealed` slot" in body
    assert "submit-report --reviewer X" in body
    assert re.search(r"fresh rerun.{0,120}A, B, and C", body, re.DOTALL) is None


def test_operational_failure_policy_does_not_weaken_the_gate():
    skill = text("SKILL.md")
    assert "format-only retry" in skill
    assert "same reviewer handle" in skill
    assert "same-role fresh replacement" in skill
    assert "maximum 3 attempts" in skill
    assert "CRITICAL" in skill and "HIGH" in skill
    assert "never pass with only two sealed slots" in skill
    assert "REVIEWER_UNAVAILABLE" in skill


def test_observation_and_terminal_cleanup_failures_are_warnings_only():
    skill = text("SKILL.md")
    assert "telemetry failure is an observation warning" in skill
    assert "terminal cleanup refusal does not change the verdict" in skill
    assert "uncertain reviewer process" in skill
    assert "uncertain worktree identity" in skill
```

Also assert the sequence `terminal bytes -> submit-report -> sealed receipt`, a format error
redispatches the same role prompt, runtime failure creates a new detached view for the same
role, peer outputs remain undisclosed, and finalization follows three pre-final validations.

- [ ] **Step 2: Run the pressure tests to verify RED**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_skill_contract.py -k 'pending_recovery or operational_failure or observation_and_terminal or submit'
```

Expected: FAIL because the #109 Skill still requires a fresh complete panel and cleanup
success before finalization.

- [ ] **Step 3: Replace the normal response path with immediate submit/seal**

Update the required sequence so the controller handles each terminal reviewer independently:

1. pipe exact response bytes directly to `submit-report --reviewer X`;
2. retain the returned receipt and never parse/reserialize the response;
3. on success mark X sealed and never dispatch X again in that round;
4. on a format code, keep the verified view and send a complete format-only retry to the same
   reviewer handle; if that handle cannot accept a follow-up, record `REVIEWER_FAILED` and
   use the replacement path rather than pretending it is the same reviewer;
5. on timeout/process/dispatch failure, call `record-failure`, then create a fresh view and
   dispatch a same-role replacement;
6. do not cancel or reveal status to already-running peers;
7. maintain a controller-local `ATTEMPTS_THIS_INVOCATION[X]` counter, stop automatic attempts
   for X after 3 in the invocation, return `REVIEWER_UNAVAILABLE`,
   and preserve every sealed peer for explicit resume.

The Skill must list format codes separately from operational codes and must never repair JSON
in the controller.

- [ ] **Step 4: Replace full-panel pending recovery with status-driven recovery**

The marked recovery section must require:

```text
status -> optional migrate-legacy-pending -> snapshot/contract check
       -> context only for pending slots -> dispatch only pending roles
       -> submit each valid terminal response -> validate all sealed slots -> finalize
```

It must not call `begin`, reset verdict, delete `.review`, rerun sealed peers, or expose a
reviewer subset to legacy migration. A v1 migration with A/B sealed and C pending dispatches
only C after migration.

- [ ] **Step 5: Separate warning cleanup from integrity stop conditions**

After every terminal reviewer, attempt non-force cleanup. If the process is known terminal,
the exact controller-created view is identified and the cleanup command merely refuses,
record `view_cleanup` warning and continue. If process state or view identity is uncertain,
stop before finalization. Never use `--force` and never delete a non-empty `VIEW_ROOT`.

Telemetry start/finish/recover/close failures are retained as bounded observation gaps and do
not replace submit/finalize results. Pre-final type/owner/mode/digest validation and snapshot/
contract checks remain mandatory.

- [ ] **Step 6: Update report reference and operator documentation**

Document v2 pending/sealed shapes, receipt fields, installed contract source of truth, new
commands, stable errors, migration all-slot behavior and the distinction among substantive
blocker, integrity stop, retryable reviewer failure and observational warning. Remove the old
instruction that every recovery reruns A/B/C.

- [ ] **Step 7: Run all skill and shell-contract tests**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_skill_contract.py tests/pre_pr_tribunal/test_shell_scan.py tests/pre_pr_tribunal/test_gate_adapters.py
```

Expected: PASS; all reviewer prompts remain standalone and peer-private.

- [ ] **Step 8: Commit the controller contract**

```bash
rtk git add skills/pre-pr-tribunal/SKILL.md skills/pre-pr-tribunal/references/report-schema.md hooks/README.md tests/pre_pr_tribunal/test_skill_contract.py
rtk git commit -m "docs: recover only pending tribunal slots"
```

---

### Task 8: Exercise the installed runtime and publish verification evidence

**Files:**

- Modify: `scripts/probe-pre-pr-tribunal.py:1300-1700`
- Modify: `tests/pre_pr_tribunal/test_probe_harness.py:696-852`
- Modify: `tests/pre_pr_tribunal/test_install_integration.py`
- Modify: `tests/pre_pr_tribunal/test_installer.py`
- Create: `docs/validation/2026-09-10-pre-pr-tribunal-slot-recovery.md`

**Interfaces:**

- Consumes: source installation plan and the new installed CLI/Skill contract.
- Produces: isolated installed canaries for selective retry, legacy migration, authenticated
  finalization, telemetry warning behavior and deterministic package rollout.
- Stop condition: do not install into the user's real `$HOME`; all canaries use pytest temp
  homes or a `mktemp -d` target and retain sanitized outputs only.

- [ ] **Step 1: Write installed selective-recovery RED tests**

Replace the probe's old store/validate/finalize lifecycle with `submit-report`, and add:

```python
def test_installed_probe_preserves_valid_peers_when_c_retries(tmp_path, monkeypatch):
    module, home, repo, cli = _installed_lifecycle(tmp_path)
    attempts = {"A": 0, "B": 0, "C": 0}

    def report_factory(reviewer, attempt, snapshot):
        attempts[reviewer] += 1
        if reviewer == "C" and attempt == 1:
            return b'{"schema":1'
        return module._synthetic_report_bytes(snapshot, reviewer)

    result = module._create_pass_verdict(
        cli,
        repo,
        home,
        "codex",
        report_factory=report_factory,
    )
    assert attempts == {"A": 1, "B": 1, "C": 2}
    assert result["status"]["gate_status"] == "pass"
    assert result["status"]["reviewers"]["A"]["attempt_count"] == 1
    assert result["status"]["reviewers"]["C"]["attempt_count"] == 2
```

Add installed tests for A/B legacy adoption plus C-only execution, blocker A sealing and FAIL,
sealed file bytes/mode/symlink tamper before finalize, telemetry corruption still PASS,
wrong installed package contract returning `CONTRACT_DRIFT`, and every installed source file
matching the planned source digest.

- [ ] **Step 2: Run installed tests to verify RED**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_probe_harness.py -k 'preserves_valid_peers or legacy_adoption or sealed or contract_drift'
```

Expected: FAIL because the probe still exercises #109's all-panel path.

- [ ] **Step 3: Update the isolated probe lifecycle**

Extract the existing inline empty-report construction into
`_synthetic_report_bytes(reviewer: str, attempt: int, snapshot: Mapping[str, object]) -> bytes`;
the attempt is validated as positive but does not alter a normal report. Make
`_create_pass_verdict` accept an optional `report_factory` with that signature and default it
to `_synthetic_report_bytes`. Submit each exact generated
response immediately. On a parser failure for C, regenerate only C and resubmit it; do not
call begin again or rewrite A/B.
Before finalization call stored validation for A/B/C and compare returned digests with the
persisted receipts/status projection. Invoke `finalize` without caller-selected paths.
Return the final bounded `status` projection alongside the existing `begin`,
`telemetry_summary` and `telemetry_gaps` fields so the harness can assert slot attempts.

Keep the existing telemetry failure injection and require the primary lifecycle to finish.
All runtime invocations continue using the probe's masked temp home, sealed credentials and
existing process containment.

- [ ] **Step 4: Run installer and probe suites**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py tests/pre_pr_tribunal/test_install_integration.py
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_probe_harness.py
```

Expected: PASS; installation contains `review_context.py` and `attempt_store.py`, and no real
runtime configuration is changed.

- [ ] **Step 5: Run the complete tribunal and repository suites**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal tests/runtime_hook_installer
rtk python3 -m pytest -q
rtk python3 -m py_compile hooks/pre_pr_tribunal/*.py scripts/install-pre-pr-tribunal.py scripts/probe-pre-pr-tribunal.py
rtk git diff --check
```

Expected: every command exits 0.

- [ ] **Step 6: Record sanitized validation evidence**

Create `docs/validation/2026-09-10-pre-pr-tribunal-slot-recovery.md` containing:

- exact branch HEAD and base commit;
- focused test commands and pass counts;
- complete tribunal and repository pass counts/durations;
- installed canary cases and stable outcomes;
- explicit statement that the user's real installed runtime and #109 `.review` were untouched;
- `git diff --check` and `py_compile` exit status;
- no credentials, absolute home paths, raw reviewer reports or telemetry bodies.

- [ ] **Step 7: Commit probe and evidence**

```bash
rtk git add scripts/probe-pre-pr-tribunal.py tests/pre_pr_tribunal/test_probe_harness.py tests/pre_pr_tribunal/test_install_integration.py tests/pre_pr_tribunal/test_installer.py docs/validation/2026-09-10-pre-pr-tribunal-slot-recovery.md
rtk git commit -m "test: verify selective tribunal recovery rollout"
```

- [ ] **Step 8: Verify the implementation branch handoff state**

```bash
rtk git status --short --branch
rtk git log --oneline --decorate --max-count=12
rtk git diff --check df1285dbb7655717af3d60c7fc098715a29203dd HEAD
```

Expected: clean #124 branch, #109 dependency present in ancestry, all planned implementation
commits visible, and no whitespace error. Do not install, push, create a PR or alter #109 in
this Task; those actions remain under their separate ownership/review gates.
