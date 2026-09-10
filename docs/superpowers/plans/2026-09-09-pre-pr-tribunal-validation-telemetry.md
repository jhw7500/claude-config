# Pre-PR Tribunal Validation and Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 실행 excerpt의 LF/TAB 계약을 바로잡고, reviewer별 exact-byte secure report 저장과 즉시 strict validation을 추가하며, gate 의미를 바꾸지 않는 snapshot-bound latency telemetry를 구축한다.

**Architecture:** 기존 `parse_reviewer_report`를 유일한 report 판정기로 유지하고, 공용 `.review` descriptor store 위에 exact `0600` report lifecycle과 별도 telemetry ledger를 둔다. CLI는 report store/validation과 bounded telemetry span 명령을 제공하고, canonical Skill이 Claude Code와 Codex에서 reviewer terminal 순서대로 이 명령을 호출한다. Verdict와 PR hook은 telemetry를 읽지 않으며 `finalize`는 조기 검증 결과를 신뢰하지 않고 A/B/C를 다시 전부 연다.

**Tech Stack:** Python 3.10 standard library, pytest 9, Git CLI, Claude Code/Codex native reviewer orchestration, Markdown contract tests.

**Spec:** `docs/superpowers/specs/2026-09-09-pre-pr-tribunal-validation-telemetry-design.md`

## Global Constraints

- 모든 개발 shell 명령은 `rtk`로 시작한다.
- 기존 JHW Task worktree와 branch를 계속 사용하고 별도 checkout이나 worktree를 만들지 않는다.
- report와 verdict JSON shape는 schema 1을 유지한다.
- decoded LF(U+000A)와 TAB(U+0009)은 `stdout_excerpt`와 `stderr_excerpt`에서만 허용한다.
- 두 excerpt의 LF/TAB을 제외한 모든 `Cc`/`Cs`는 계속 `TEXT_INVALID`다.
- command, finding/claim text, path/ref/id의 기존 strict text 의미를 넓히지 않는다.
- secret, absolute home path, UTF-8/NFC와 기존 byte/count bound를 완화하지 않는다.
- reviewer terminal response는 trim, Unicode normalization, code-fence 제거 또는 JSON 재직렬화 없이 exact bytes로 보존한다.
- canonical report는 current-user-owned regular non-symlink이고 ambient `umask`와 무관하게 정확히 mode `0600`이어야 한다.
- controller는 A/B/C 각각의 type, owner, mode와 digest를 `finalize` 직전에 확인하며 하나라도 실패하면 `finalize`를 호출하지 않는다.
- immediate validation은 report, `.review/verdict.json`과 reviewer slot을 수정하지 않는다.
- `finalize_round`는 A/B/C raw bytes를 다시 읽어 같은 strict parser로 전부 검증한다.
- telemetry는 `.review/telemetry.json`에만 저장하고 raw command/output, secret, 자유 형식 오류문, environment value와 absolute home path를 포함하지 않는다.
- telemetry file은 current-user-owned regular non-symlink, exact mode `0600`, 최대 2 MiB, 최대 16 runs, run당 최대 128 spans다.
- telemetry 오류, 부재와 clock anomaly는 verdict, gate status 또는 PR hook 결정을 바꾸지 않는다.
- reviewer 수, 역할, CRITICAL/HIGH 기준, 최대 3 rounds와 peer isolation을 바꾸지 않는다.
- #113, #114, #115, #121의 evidence bundle, budget, selective rerun, prompt experiment를 구현하지 않는다.
- 새 third-party dependency, network upload, permission, secret 또는 environment variable을 추가하지 않는다.
- 각 Task는 RED 확인, 최소 구현, focused PASS, commit 순서를 지킨다.

Before Task 1, record a fresh clean baseline:

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal tests/runtime_hook_installer
rtk python3 -m pytest -q
rtk git status --short --branch
```

Expected: both pytest commands exit 0; the branch contains only the committed design and plan history; the worktree is clean.

## Planned File Structure

```text
hooks/pre_pr_tribunal/
  __init__.py          # existing public package exports remain unchanged
  model.py             # excerpt-only text policy and report-byte validation wrapper
  git_state.py         # one documented diff recipe and sanitized reproduction contract
  review_store.py      # new shared descriptor-anchored .review storage primitives
  verdict_store.py     # verdict state plus canonical report store/read operations
  telemetry.py         # new strict bounded telemetry model, transitions, storage, summary
  cli.py               # report store/validate and telemetry lifecycle commands
skills/pre-pr-tribunal/
  SKILL.md             # terminal-order storage/validation, pre-final checks, telemetry spans
  references/report-schema.md # LF/TAB JSON transport and exact-byte rules
  references/reviewer-a.md     # standalone corrected output contract
  references/reviewer-b.md     # standalone corrected output contract
  references/reviewer-c.md     # standalone corrected output contract
scripts/
  install-pre-pr-tribunal.py   # install the two new package modules
  probe-pre-pr-tribunal.py     # exercise installed secure report and telemetry CLI
tests/pre_pr_tribunal/
  test_model_store.py    # parser/report lifecycle/CLI parity and no-mutation tests
  test_git_state.py      # diff contract reproduction
  test_review_store.py   # new generic private store characterization tests
  test_telemetry.py      # new telemetry schema/state/storage/CLI tests
  test_skill_contract.py # both runtime contracts and JSON examples
  test_installer.py      # deterministic package file set
  test_probe_harness.py  # installed CLI/report/telemetry probe behavior
hooks/README.md          # operator commands, errors and recovery boundary
docs/validation/
  2026-09-09-pre-pr-tribunal-validation-telemetry.md # sanitized verification and baseline evidence
```

---

### Task 1: Excerpt-only text policy와 재현 가능한 diff contract

**Files:**

- Modify: `hooks/pre_pr_tribunal/model.py:10-20, 374-390, 605-694`
- Modify: `hooks/pre_pr_tribunal/git_state.py:45-83, 554-573`
- Modify: `hooks/pre_pr_tribunal/cli.py:13-45, 60-105`
- Test: `tests/pre_pr_tribunal/test_model_store.py:155-335, 580-615`
- Test: `tests/pre_pr_tribunal/test_git_state.py:100-130`

**Interfaces:**

- Consumes: report schema 1 decoded strings and a `Snapshot` with merge-base/head SHA.
- Produces: `REPORT_TEXT_CONTRACT_VERSION = 2`, `DIFF_RECIPE_VERSION = 1`, `diff_contract(snapshot: Snapshot) -> dict[str, object]`.
- Preserves: `_text()` remains strict for every existing caller; `_command()`, `_path()`, finding/claim parsing and all secret/home-path scans retain their current behavior.

- [ ] **Step 1: LF/TAB acceptance와 strict-field/control rejection RED tests를 작성한다**

Add these focused cases to `tests/pre_pr_tribunal/test_model_store.py`:

```python
@pytest.mark.parametrize("field", ("stdout_excerpt", "stderr_excerpt"))
@pytest.mark.parametrize("value", ("first\nsecond", "name\tvalue", "first\n\tsecond"))
def test_execution_excerpts_accept_only_lf_and_tab(snapshot, field, value):
    item = execution()
    item[field] = value
    parsed = parse_reviewer_report(
        json.dumps(report(snapshot, "A", executions=[item])).encode(),
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=snapshot,
    )
    assert getattr(parsed.executions[0], field) == value


@pytest.mark.parametrize("control", ("\r", "\x00", "\x1b", "\x7f"))
def test_execution_excerpts_reject_every_other_cc(snapshot, control):
    item = execution()
    item["stdout_excerpt"] = "left" + control + "right"
    with pytest.raises(SchemaError, match="^TEXT_INVALID$"):
        parse_reviewer_report(
            json.dumps(report(snapshot, "A", executions=[item])).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


@pytest.mark.parametrize("control", ("\n", "\t"))
def test_non_excerpt_text_keeps_rejecting_lf_and_tab(snapshot, control):
    item = execution(command="python3" + control + "-V")
    with pytest.raises(SchemaError, match="^TEXT_INVALID$"):
        parse_reviewer_report(
            json.dumps(report(snapshot, "A", executions=[item])).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )
```

Extend the last test with separate mutations for finding `title`, `rationale`,
`acceptance_condition`, claim `statement`/`reason`, and repository-relative `path`; every
mutation must raise exactly `TEXT_INVALID` or the existing path wrapper code without accepting the control.

- [ ] **Step 2: Parser tests가 현재 `TEXT_INVALID`로 RED인지 확인한다**

Run:

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'execution_excerpts or non_excerpt_text'
```

Expected: the LF/TAB acceptance parameters fail at `model._text`; all other-control and non-excerpt cases already pass.

- [ ] **Step 3: 전용 excerpt validator를 최소 구현한다**

In `model.py`, keep `_text` unchanged and replace only `_evidence`'s decoded-text entry with:

```python
REPORT_TEXT_CONTRACT_VERSION = 2
_EXCERPT_CONTROLS = frozenset(("\n", "\t"))


def _execution_excerpt(value: object) -> str:
    if not isinstance(value, str):
        raise SchemaError("TEXT_INVALID")
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeEncodeError:
        raise SchemaError("TEXT_INVALID") from None
    if size > MAX_EVIDENCE_TEXT_BYTES:
        raise SchemaError("TEXT_TOO_LARGE")
    if unicodedata.normalize("NFC", value) != value or any(
        unicodedata.category(character) in {"Cc", "Cs"}
        and character not in _EXCERPT_CONTROLS
        for character in value
    ):
        raise SchemaError("TEXT_INVALID")
    if _SECRET.search(value) or _contains_home_path(value):
        raise SchemaError("EVIDENCE_SECRET_DETECTED")
    return value
```

Call `_execution_excerpt` only for `stdout_excerpt` and `stderr_excerpt`. Keep decision executions on the same `_parse_execution` path so fixed/rebutted evidence gets the identical exception.

- [ ] **Step 4: Diff recipe contract RED tests를 작성한다**

Add to `tests/pre_pr_tribunal/test_git_state.py`:

```python
def test_diff_contract_reproduces_snapshot_digest(git_repo):
    snapshot = capture_snapshot(git_repo, "master")
    contract = diff_contract(snapshot)
    assert contract["version"] == 1
    assert contract["digest"] == "sha256"
    assert contract["clear_inherited_prefixes"] == ["GIT_"]
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(contract["environment"])
    exact = subprocess.check_output(
        [GIT, "-C", str(git_repo), *contract["arguments"]],
        env=environment,
    )
    assert hashlib.sha256(exact).hexdigest() == snapshot.diff_sha256
    assert not any("/home/" in value or "/Users/" in value for value in contract["environment"].values())
```

Update the CLI context test to require exact top-level `contract` and `diff_contract` values without exposing reviewer peers or execution content.

- [ ] **Step 5: 한 constant가 capture와 context를 동시에 구동하도록 구현한다**

In `git_state.py`:

```python
DIFF_RECIPE_VERSION = 1
DIFF_ARGUMENTS = ("diff", "--binary", "--no-ext-diff", "--no-textconv", "--full-index")
_SANITIZED_GIT_ENVIRONMENT = {
    "LC_ALL": "C",
    "LANG": "C",
    "GIT_PAGER": "cat",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.fsmonitor",
    "GIT_CONFIG_VALUE_0": "false",
}


def diff_contract(snapshot: Snapshot) -> dict[str, object]:
    revision = f"{snapshot.merge_base_sha}..{snapshot.head_sha}"
    return {
        "version": DIFF_RECIPE_VERSION,
        "digest": "sha256",
        "arguments": [*DIFF_ARGUMENTS, revision],
        "clear_inherited_prefixes": ["GIT_"],
        "environment": dict(_SANITIZED_GIT_ENVIRONMENT),
    }
```

Use `_SANITIZED_GIT_ENVIRONMENT` inside `_git_environment()` and `DIFF_ARGUMENTS` inside `capture_snapshot()`. Add this to `cli._context()`:

```python
"contract": {
    "report_text": REPORT_TEXT_CONTRACT_VERSION,
    "diff_recipe": DIFF_RECIPE_VERSION,
},
"diff_contract": diff_contract(verdict.snapshot),
```

- [ ] **Step 6: Focused parser/diff/context tests를 통과시킨다**

Run:

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'execution_excerpts or non_excerpt_text or report_and_evidence or cli_json_only'
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_git_state.py -k 'digest or diff_contract'
```

Expected: both commands PASS; the existing secret/home-path and byte-limit cases remain green.

- [ ] **Step 7: Parser/diff contract 단위를 커밋한다**

```bash
rtk git add hooks/pre_pr_tribunal/model.py hooks/pre_pr_tribunal/git_state.py hooks/pre_pr_tribunal/cli.py tests/pre_pr_tribunal/test_model_store.py tests/pre_pr_tribunal/test_git_state.py
rtk git commit -m "fix: align tribunal excerpt and diff contracts"
```

---

### Task 2: 공용 descriptor-anchored review store 추출

**Files:**

- Create: `hooks/pre_pr_tribunal/review_store.py`
- Create: `tests/pre_pr_tribunal/test_review_store.py`
- Modify: `hooks/pre_pr_tribunal/verdict_store.py:35-280, 585-710`
- Modify: `scripts/install-pre-pr-tribunal.py:30-55`
- Modify: `tests/pre_pr_tribunal/test_installer.py:15-45, 125-165`
- Test: `tests/pre_pr_tribunal/test_model_store.py:801-935`

**Interfaces:**

- Consumes: repository cwd and fixed child names below gitignored `.review`.
- Produces: `repository_root`, `preflight_review_directory`, `check_ignored`, `locked_review`, `open_directory`, `safe_file`, `read_named_file`, `atomic_replace_bytes`, `atomic_create_bytes`.
- Preserves: existing verdict/lock mode, nonblocking lock, symlink/owner checks, error codes, fsync and atomic rollback behavior.

- [ ] **Step 1: Generic store interface와 no-replace atomic creation RED tests를 작성한다**

Create `tests/pre_pr_tribunal/test_review_store.py` with these contracts:

```python
import os
from pathlib import Path
import stat

import pytest

from pre_pr_tribunal.model import SchemaError
from pre_pr_tribunal.review_store import atomic_create_bytes, locked_review, open_directory


def test_atomic_create_publishes_complete_exact_private_file(git_repo):
    payload = b'{"raw":"first\\nsecond"}\n'
    with locked_review(git_repo, create=True) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=True, code="FILE_UNSAFE")
        try:
            digest = atomic_create_bytes(
                inbox_fd,
                "A.json",
                payload,
                maximum=1024,
                exists="REPORT_FILE_EXISTS",
                unsafe="FILE_UNSAFE",
                exact_mode=0o600,
            )
        finally:
            os.close(inbox_fd)
    target = git_repo / ".review/inbox/A.json"
    assert target.read_bytes() == payload
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert len(digest) == 64


def test_atomic_create_refuses_existing_target_without_changing_bytes(git_repo):
    target = git_repo / ".review/inbox/A.json"
    target.parent.mkdir(parents=True, mode=0o700)
    target.write_bytes(b"keep")
    target.chmod(0o600)
    with locked_review(git_repo, create=False) as review_fd:
        inbox_fd = open_directory(review_fd, "inbox", create=False, code="FILE_UNSAFE")
        try:
            with pytest.raises(SchemaError, match="^REPORT_FILE_EXISTS$"):
                atomic_create_bytes(
                    inbox_fd, "A.json", b"replace", maximum=1024,
                    exists="REPORT_FILE_EXISTS", unsafe="FILE_UNSAFE", exact_mode=0o600,
                )
        finally:
            os.close(inbox_fd)
    assert target.read_bytes() == b"keep"
```

Add explicit cases for symlink target, FIFO target, oversized bytes, `umask` 000/022/077, injected failure before publish, and wrong descriptor mode. Every case must assert no completed target and no `.tmp.*` artifact after failure.

Add `review_store.py` to the test-local `PACKAGE_NAMES` and assert the installer plans it as mode `0600`; this test must fail until the production package list is updated.

- [ ] **Step 2: New module 부재로 RED인지 확인한다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_review_store.py
```

Expected: collection FAILS because `pre_pr_tribunal.review_store` does not exist.

- [ ] **Step 3: 기존 secure primitives를 의미 변화 없이 이동한다**

Move the corresponding implementations from `verdict_store.py` and expose this exact package-internal shape in `review_store.py`:

```text
repository_root(cwd: Path) -> Path
preflight_review_directory(root: Path) -> None
check_ignored(root: Path) -> None
safe_directory(fd: int, code: str) -> None
safe_file(fd: int, code: str, *, exact_mode: int | None = None) -> os.stat_result
open_directory(parent_fd: int, name: str, *, create: bool, code: str) -> int
locked_review(root: Path, *, create: bool) -> Iterator[int]
read_named_file(parent_fd: int, name: str, *, maximum: int, missing: str,
                unsafe: str, exact_mode: int | None = None) -> bytes
atomic_replace_bytes(parent_fd: int, name: str, raw: bytes, *, maximum: int,
                     too_large: str, unsafe: str,
                     exact_mode: int = 0o600) -> None
atomic_create_bytes(parent_fd: int, name: str, raw: bytes, *, maximum: int,
                    exists: str, unsafe: str,
                    exact_mode: int = 0o600) -> str
```

`atomic_create_bytes` writes a random owner-private temporary inode, calls `fchmod(exact_mode)`, writes in a bounded loop, `fsync`s, verifies metadata, publishes with a same-directory no-replace hard link, unlinks the temporary name, `fsync`s the directory, reopens the final name with `O_NOFOLLOW`, compares inode and SHA-256, then returns the digest. It maps collision to `exists` and every unsafe file/type/link transition to `unsafe`.

- [ ] **Step 4: Verdict store를 imports로 전환하고 serialization만 남긴다**

Replace moved private helpers with imports in `verdict_store.py`. Reimplement `_atomic_write` as JSON serialization plus:

```python
atomic_replace_bytes(
    review_fd,
    "verdict.json",
    raw,
    maximum=m.MAX_VERDICT_BYTES,
    too_large="VERDICT_TOO_LARGE",
    unsafe="VERDICT_FILE_UNSAFE",
    exact_mode=0o600,
)
```

Do not change `_invalidate_round_inputs`, `_read_input`, begin/finalize state transitions or their stable codes in this Task.

Add `review_store.py` to production `PACKAGE_NAMES` in the same commit so every installed `verdict_store.py` has its imported dependency.

- [ ] **Step 5: Store characterization와 기존 verdict tests를 통과시킨다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_review_store.py
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'store or verdict or round or finalize'
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py
```

Expected: all three commands PASS; atomic report helper is tested but not used by the CLI yet, and the installed package is dependency-complete.

- [ ] **Step 6: Storage extraction을 커밋한다**

```bash
rtk git add hooks/pre_pr_tribunal/review_store.py hooks/pre_pr_tribunal/verdict_store.py scripts/install-pre-pr-tribunal.py tests/pre_pr_tribunal/test_review_store.py tests/pre_pr_tribunal/test_model_store.py tests/pre_pr_tribunal/test_installer.py
rtk git commit -m "refactor: share private tribunal storage primitives"
```

---

### Task 3: Exact report store와 shared validation core

**Files:**

- Modify: `hooks/pre_pr_tribunal/model.py:824-925`
- Modify: `hooks/pre_pr_tribunal/verdict_store.py:620-710, 909-975`
- Test: `tests/pre_pr_tribunal/test_model_store.py:155-335, 700-850`

**Interfaces:**

- Consumes: current in-progress verdict, `Reviewer`, bounded raw report bytes.
- Produces: `validate_report_bytes -> tuple[ReviewerReport, str]`, immutable `ReportReceipt`, `store_reviewer_report`, `validate_stored_reviewer_report`; store defaults to no-replace and has an explicit pending-recovery replace switch.
- Preserves: `parse_reviewer_report` remains public and authoritative; finalizer closure/blocker logic remains unchanged.

- [ ] **Step 1: Pure validation parity와 no-mutation RED tests를 작성한다**

Add the wrapper contract:

```python
def test_validate_report_bytes_returns_parser_result_and_raw_digest(snapshot):
    raw = json.dumps(report(snapshot, "A"), separators=(",", ":")).encode() + b"\n"
    parsed, digest = validate_report_bytes(
        raw,
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=snapshot,
    )
    assert parsed == parse_reviewer_report(
        raw, expected_reviewer=Reviewer.A, expected_round=1, snapshot=snapshot
    )
    assert digest == hashlib.sha256(raw).hexdigest()
```

Parameterize malformed JSON, `TEXT_INVALID`, reviewer, round and snapshot mismatch and assert the wrapper raises the exact same code as `parse_reviewer_report`.

- [ ] **Step 2: Exact store, mode, overwrite and invalid-byte preservation RED tests를 작성한다**

Use `begin_round` and this shape:

```python
@pytest.mark.parametrize("mask", (0o000, 0o022, 0o077))
def test_store_reviewer_report_preserves_bytes_and_forces_mode(git_repo, mask):
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode() + b"\n"
    previous = os.umask(mask)
    try:
        receipt = store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
    finally:
        os.umask(previous)
    target = git_repo / ".review/inbox/round-1/A.json"
    assert target.read_bytes() == raw
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert receipt.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.path == ".review/inbox/round-1/A.json"
```

Add cases proving invalid `b'{"schema":1'` is still stored byte-for-byte, a second normal store raises `REPORT_FILE_EXISTS`, and a symlink/FIFO/exact mode other than `0600` raises `FILE_UNSAFE`. Add `test_explicit_full_panel_recovery_replace_preserves_each_new_response_exactly`: prepare a `fresh` mapping keyed by `Reviewer.A`, `Reviewer.B`, and `Reviewer.C`, then loop over that mapping and call `store_reviewer_report(git_repo, reviewer=reviewer, raw=fresh[reviewer], replace_pending_recovery=True)` only after the complete set exists. Assert each canonical file equals its corresponding new bytes with mode `0600`. Save verdict bytes before every validation/recovery replace and assert they are unchanged afterward.

- [ ] **Step 3: Tests가 missing interfaces로 RED인지 확인한다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'validate_report_bytes or store_reviewer_report'
```

Expected: collection or execution FAILS because the new functions and `ReportReceipt` do not exist.

- [ ] **Step 4: Shared validation wrapper와 receipt를 구현한다**

In `model.py`:

```python
def validate_report_bytes(
    raw: bytes,
    *,
    expected_reviewer: Reviewer,
    expected_round: int,
    snapshot: Snapshot,
) -> tuple[ReviewerReport, str]:
    report = parse_reviewer_report(
        raw,
        expected_reviewer=expected_reviewer,
        expected_round=expected_round,
        snapshot=snapshot,
    )
    return report, hashlib.sha256(raw).hexdigest()
```

In `verdict_store.py`:

```text
@dataclass(frozen=True)
class ReportReceipt:
    reviewer: Reviewer
    round: int
    path: str
    raw_sha256: str

store_reviewer_report(cwd: Path, *, reviewer: Reviewer, raw: bytes,
                      replace_pending_recovery: bool = False) -> ReportReceipt
validate_stored_reviewer_report(cwd: Path, *, reviewer: Reviewer
                                ) -> tuple[ReviewerReport, str]
```

Both public functions resolve/check/lock `.review`, require an all-pending `IN_PROGRESS` verdict and derive `.review/inbox/round-N/X.json` from it. Store creates `inbox` and `round-N` with mode `0700`; normal mode calls `atomic_create_bytes`, while explicit full-panel pending recovery uses `atomic_replace_bytes` only after the Skill has obtained all three fresh terminal responses. Stored validation reads with `exact_mode=0o600`, captures a fresh snapshot, requires `_snapshot_equal`, then calls `validate_report_bytes`.

- [ ] **Step 5: Finalizer에 exact report mode와 shared wrapper를 적용한다**

Add `exact_mode: int | None = None` to `_read_input`, pass it only for A/B/C report reads, and keep decision reads backward compatible. Finalizer becomes:

```python
raw = _read_input(
    root,
    review_fd,
    reviewer_paths[key],
    round_number=pending.round,
    basename=f"{key}.json",
    maximum=m.MAX_REPORT_BYTES,
    missing="REVIEWER_REPORT_MISSING",
    path_code="REPORT_PATH_INVALID",
    exact_mode=0o600,
)
reports[key], _raw_sha256 = m.validate_report_bytes(
    raw,
    expected_reviewer=Reviewer(key),
    expected_round=pending.round,
    snapshot=snapshot,
)
```

Add a regression that changes a valid report from `0600` to `0400`; it must now fail `FILE_UNSAFE`, proving the requirement is exact mode rather than only no group/other bits.

- [ ] **Step 6: Report lifecycle focused tests를 통과시킨다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'validate_report_bytes or store_reviewer_report or finalize_rejects_report or pending_round'
```

Expected: PASS with exact digest/mode and unchanged verdict assertions.

- [ ] **Step 7: Report core를 커밋한다**

```bash
rtk git add hooks/pre_pr_tribunal/model.py hooks/pre_pr_tribunal/verdict_store.py tests/pre_pr_tribunal/test_model_store.py
rtk git commit -m "feat: secure and prevalidate reviewer reports"
```

---

### Task 4: Report store/validate CLI와 finalization 전 확인 표면

**Files:**

- Modify: `hooks/pre_pr_tribunal/cli.py:38-150`
- Test: `tests/pre_pr_tribunal/test_model_store.py:1364-1515`

**Interfaces:**

- Consumes: raw stdin for `store-report`/stdin validation and current verdict for reviewer/round/snapshot.
- Produces: `store-report --reviewer [--replace-pending-recovery]`, `validate-report --reviewer --source stdin|stored`.
- Preserves: all success output is bounded one-line JSON; all domain failure output is one bounded `PRE_PR_TRIBUNAL:<CODE>` line; existing finalize/status payloads do not expose reports.

- [ ] **Step 1: End-to-end CLI RED tests를 작성한다**

Create a helper that runs the CLI with bytes and add this path:

```python
def run_cli_bytes(git_repo, *arguments, input=b""):
    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    return subprocess.run(
        [sys.executable, str(cli), *arguments],
        cwd=git_repo,
        input=input,
        capture_output=True,
        check=False,
    )


def test_cli_stores_and_validates_exact_report_without_mutating_verdict(git_repo):
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode() + b"\n"
    verdict_path = git_repo / ".review/verdict.json"
    before = verdict_path.read_bytes()
    stored = run_cli_bytes(git_repo, "store-report", "--reviewer", "A", input=raw)
    stored_payload = json.loads(stored.stdout)
    assert stored.returncode == 0 and stored.stderr == b""
    assert stored_payload == {
        "reviewer": "A",
        "round": 1,
        "status": "stored",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }
    validated = run_cli_bytes(
        git_repo, "validate-report", "--reviewer", "A", "--source", "stored"
    )
    assert json.loads(validated.stdout)["raw_sha256"] == stored_payload["raw_sha256"]
    assert verdict_path.read_bytes() == before
    assert (git_repo / ".review/inbox/round-1/A.json").read_bytes() == raw
```

Add stdin validation of the same bytes, invalid JSON returning `JSON_INVALID` while the stored raw file remains unchanged, wrong reviewer/source usage returning exit 2 `PRE_PR_TRIBUNAL:USAGE`, and stored mode rejecting `0644`, symlink and swapped content. Add an explicit recovery test that prepares all three fresh responses, invokes `store-report --reviewer X --replace-pending-recovery` for A/B/C, and proves normal invocation still refuses each existing target.

- [ ] **Step 2: CLI tests가 unknown commands로 RED인지 확인한다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'cli_stores or cli_validate'
```

Expected: FAIL with `PRE_PR_TRIBUNAL:USAGE` because neither command exists.

- [ ] **Step 3: Bounded stdin reader와 command parsers를 구현한다**

Add:

```python
store = commands.add_parser("store-report", add_help=False)
store.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
store.add_argument("--replace-pending-recovery", action="store_true")
validate = commands.add_parser("validate-report", add_help=False)
validate.add_argument("--reviewer", required=True, choices=("A", "B", "C"))
validate.add_argument("--source", required=True, choices=("stdin", "stored"))


def _report_stdin() -> bytes:
    return sys.stdin.buffer.read(MAX_REPORT_BYTES + 1)
```

`store-report` passes `_report_stdin()` unchanged and the recovery flag to `store_reviewer_report`. `validate-report --source stdin` reads the current verdict once, captures the bound snapshot, and calls `validate_report_bytes`; `--source stored` calls `validate_stored_reviewer_report` without reading stdin. The Skill is the authority that permits the recovery flag only after explicit user intervention and a complete fresh A/B/C rerun; the CLI still enforces unchanged snapshot and all-pending verdict.

- [ ] **Step 4: Success projections를 exact shape로 구현한다**

Use one helper for both exact projections:

```python
def _report_projection(
    reviewer: Reviewer, round_number: int, status: str, digest: str
) -> dict[str, object]:
    if status not in {"stored", "valid"}:
        raise TribunalError("VERDICT_INVALID")
    return {
        "reviewer": reviewer.value,
        "round": round_number,
        "status": status,
        "raw_sha256": digest,
    }
```

Do not include report path, content, finding count, execution count, command, environment or file metadata. Oversized stdin reaches the existing `REPORT_TOO_LARGE` code instead of being truncated into valid JSON.

- [ ] **Step 5: CLI and full model/store tests를 통과시킨다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'cli or validate_report or store_reviewer or finalize'
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py
```

Expected: both commands PASS; existing begin/context/finalize/status error formatting remains bounded.

- [ ] **Step 6: Report CLI를 커밋한다**

```bash
rtk git add hooks/pre_pr_tribunal/cli.py tests/pre_pr_tribunal/test_model_store.py
rtk git commit -m "feat: add immediate tribunal report validation CLI"
```

---

### Task 5: Strict bounded telemetry ledger와 safe persistence

**Files:**

- Create: `hooks/pre_pr_tribunal/telemetry.py`
- Create: `tests/pre_pr_tribunal/test_telemetry.py`
- Modify: `scripts/install-pre-pr-tribunal.py:30-55`
- Modify: `tests/pre_pr_tribunal/test_installer.py:15-45, 125-165`
- Reuse: `hooks/pre_pr_tribunal/review_store.py`
- Reuse: `hooks/pre_pr_tribunal/model.py`
- Reuse: `hooks/pre_pr_tribunal/git_state.py`

**Interfaces:**

- Consumes: repository root, runtime/round/base request, optional snapshot binding, fixed clocks and stable reason codes.
- Produces: strict `TelemetryLedger`, `TelemetryRun`, `TelemetrySpan`; `create_run`, `record_candidate`, `bind_run`, `start_span`, `finish_span`, `recover_run`, `close_run`, `read_ledger`, `summarize_run`.
- Bounds: schema 1, file 2 MiB, 16 runs, 128 spans per run, 64-byte reason code, 32-hex random IDs.

- [ ] **Step 1: Telemetry model/state transition RED tests를 작성한다**

Create `tests/pre_pr_tribunal/test_telemetry.py` with deterministic time/token injections:

```python
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from pre_pr_tribunal.git_state import capture_snapshot
from pre_pr_tribunal.model import Reviewer, SchemaError
from pre_pr_tribunal.telemetry import (
    TelemetryOutcome,
    TelemetryStage,
    bind_run,
    close_run,
    create_run,
    finish_span,
    read_ledger,
    recover_run,
    start_span,
    summarize_run,
)


def NOW():
    return "2026-09-09T00:00:00Z"


def test_bound_run_records_terminal_span_and_sanitized_summary(git_repo):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = create_run(
        git_repo,
        base_ref="master",
        runtime="codex",
        round_number=1,
        started_at="2026-09-09T00:00:00Z",
        started_monotonic_ns=1_000_000_000,
        token_hex=lambda _size: "1" * 32,
    )
    bind_run(git_repo, run_id=run.run_id, snapshot=snapshot)
    span = start_span(
        git_repo,
        run_id=run.run_id,
        stage=TelemetryStage.REVIEWER_TOTAL,
        reviewer=Reviewer.B,
        attempt=1,
        started_at="2026-09-09T00:00:01Z",
        started_monotonic_ns=2_000_000_000,
        token_hex=lambda _size: "2" * 32,
    )
    finish_span(
        git_repo,
        run_id=run.run_id,
        span_id=span.span_id,
        outcome=TelemetryOutcome.SUCCESS,
        reason_code=None,
        ended_at="2026-09-09T00:00:05Z",
        ended_monotonic_ns=6_000_000_000,
    )
    summary = summarize_run(git_repo, run_id=run.run_id)
    assert summary["reviewers"]["B"]["total_ms"] == 4000
    assert summary["outcomes"]["success"] == 1
    assert "command" not in json.dumps(summary)
    assert "/home/" not in json.dumps(summary)


def running_span(git_repo, *, started_at="2026-09-09T00:00:10Z",
                 started_monotonic_ns=10_000):
    snapshot = capture_snapshot(git_repo, "master", now=NOW)
    run = create_run(
        git_repo, base_ref="master", runtime="codex", round_number=1,
        started_at="2026-09-09T00:00:00Z", started_monotonic_ns=1,
        token_hex=lambda _size: "1" * 32,
    )
    bind_run(git_repo, run_id=run.run_id, snapshot=snapshot)
    span = start_span(
        git_repo, run_id=run.run_id,
        stage=TelemetryStage.REVIEWER_TOTAL, reviewer=Reviewer.B, attempt=1,
        started_at=started_at, started_monotonic_ns=started_monotonic_ns,
        token_hex=lambda _size: "2" * 32,
    )
    return run, span
```

Add strict parser cases for unknown/duplicate keys, invalid enum/reviewer/attempt/timestamp/ID/reason, more than 16 runs, more than 128 spans, and raw file size above 2 MiB.

- [ ] **Step 2: Failure, timeout, incomplete와 clock anomaly RED tests를 작성한다**

Use the `running_span` helper above for these exact outcomes:

```python
@pytest.mark.parametrize(
    ("outcome", "reason"),
    (
        (TelemetryOutcome.FAILURE, "REPORT_SCHEMA_INVALID"),
        (TelemetryOutcome.TIMEOUT, "REVIEWER_TIMEOUT"),
        (TelemetryOutcome.INCOMPLETE, "CONTROLLER_INTERRUPTED"),
    ),
)
def test_terminal_outcomes_preserve_stable_reason(git_repo, outcome, reason):
    run, span = running_span(git_repo)
    terminal = finish_span(
        git_repo, run_id=run.run_id, span_id=span.span_id,
        outcome=outcome, reason_code=reason,
        ended_at="2026-09-09T00:00:11Z", ended_monotonic_ns=1_000_010_000,
    )
    assert terminal.outcome is outcome
    assert terminal.reason_code == reason
    assert terminal.duration_ms == 1000


def test_clock_reversal_records_null_duration_and_anomaly(git_repo):
    run, span = running_span(git_repo)
    terminal = finish_span(
        git_repo, run_id=run.run_id, span_id=span.span_id,
        outcome=TelemetryOutcome.SUCCESS, reason_code=None,
        ended_at="2026-09-09T00:00:09Z", ended_monotonic_ns=9_999,
    )
    assert terminal.outcome is TelemetryOutcome.CLOCK_ANOMALY
    assert terminal.duration_ms is None
    assert terminal.reason_code == "TELEMETRY_CLOCK_ANOMALY"


def test_recover_closes_every_running_span_without_reopening_it(git_repo):
    run, first = running_span(git_repo)
    second = start_span(
        git_repo, run_id=run.run_id, stage=TelemetryStage.REPORT_VALIDATION,
        reviewer=Reviewer.A, attempt=1, started_at="2026-09-09T00:00:10Z",
        started_monotonic_ns=10_000, token_hex=lambda _size: "3" * 32,
    )
    assert recover_run(
        git_repo, run_id=run.run_id, ended_at="2026-09-09T00:00:12Z",
        ended_monotonic_ns=2_010_000,
    ) == 2
    stored = next(item for item in read_ledger(git_repo).runs if item.run_id == run.run_id)
    assert {item.outcome for item in stored.spans} == {TelemetryOutcome.INCOMPLETE}
    assert {item.reason_code for item in stored.spans} == {"CONTROLLER_INTERRUPTED"}
    for item in (first, second):
        with pytest.raises(SchemaError, match="^TELEMETRY_INVALID$"):
            finish_span(
                git_repo, run_id=run.run_id, span_id=item.span_id,
                outcome=TelemetryOutcome.SUCCESS, reason_code=None,
                ended_at="2026-09-09T00:00:13Z", ended_monotonic_ns=3_010_000,
            )
```

- [ ] **Step 3: Safe file/bound/pruning RED tests를 작성한다**

Add this mode test and neighboring named cases:

```python
@pytest.mark.parametrize("mask", (0o000, 0o022, 0o077))
def test_telemetry_file_is_exact_private_mode(git_repo, mask):
    previous = os.umask(mask)
    try:
        create_run(
            git_repo, base_ref="master", runtime="codex", round_number=1,
            started_at="2026-09-09T00:00:00Z", started_monotonic_ns=1,
        )
    finally:
        os.umask(previous)
    assert stat.S_IMODE((git_repo / ".review/telemetry.json").stat().st_mode) == 0o600


def test_telemetry_symlink_is_rejected_without_touching_target(git_repo, tmp_path):
    (git_repo / ".review").mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    (git_repo / ".review/telemetry.json").symlink_to(outside)
    with pytest.raises(SchemaError, match="^TELEMETRY_FILE_UNSAFE$"):
        read_ledger(git_repo)
    assert outside.read_bytes() == b"keep"
```

Name the remaining cases `test_telemetry_rejects_fifo_and_wrong_owner`,
`test_failed_atomic_update_preserves_previous_ledger`,
`test_seventeenth_run_prunes_only_oldest_closed_run`,
`test_running_and_incomplete_runs_are_never_pruned`, and
`test_span_limit_sets_reserved_incomplete_marker`. Each asserts the exact persisted run IDs and bytes, not only the raised code.

Add `telemetry.py` to the test-local installer `PACKAGE_NAMES` and assert it is planned as mode `0600`; the production package list update belongs to Step 7 of this Task.

- [ ] **Step 4: New module 부재로 RED인지 확인한다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_telemetry.py
```

Expected: collection FAILS because `pre_pr_tribunal.telemetry` does not exist.

- [ ] **Step 5: Exact telemetry domain types와 constants를 구현한다**

Start `telemetry.py` with:

```python
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import re
import secrets


TELEMETRY_SCHEMA_VERSION = 1
MAX_TELEMETRY_BYTES = 2 * 1024 * 1024
MAX_TELEMETRY_RUNS = 16
MAX_TELEMETRY_SPANS_PER_RUN = 128
MAX_REASON_CODE_BYTES = 64


class TelemetryStage(str, Enum):
    SNAPSHOT_PREFLIGHT = "snapshot_preflight"
    VIEW_CREATE = "view_create"
    REVIEWER_DISPATCH_WAIT = "reviewer_dispatch_wait"
    REVIEWER_TOTAL = "reviewer_total"
    REPORT_STORE = "report_store"
    REPORT_VALIDATION = "report_validation"
    FINALIZE = "finalize"
    VIEW_CLEANUP = "view_cleanup"
    RECOVERY_RETRY = "recovery_retry"


class TelemetryOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    INCOMPLETE = "incomplete"
    CLOCK_ANOMALY = "clock_anomaly"
```

Define the frozen persisted types with these exact fields and projections:

```python
@dataclass(frozen=True)
class TelemetryBinding:
    status: str
    repository: str | None
    base_ref: str
    base_sha: str | None
    head_ref: str | None
    head_sha: str | None
    merge_base_sha: str | None
    diff_sha256: str | None
    contract: Mapping[str, int]

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status,
            "repository": self.repository,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "merge_base_sha": self.merge_base_sha,
            "diff_sha256": self.diff_sha256,
            "contract": dict(self.contract),
        }


@dataclass(frozen=True)
class TelemetrySpan:
    span_id: str
    stage: TelemetryStage
    reviewer: Reviewer | None
    attempt: int
    started_at: str
    ended_at: str | None
    started_monotonic_ns: int
    ended_monotonic_ns: int | None
    duration_ms: int | None
    outcome: TelemetryOutcome | None
    reason_code: str | None

    def to_json(self) -> dict[str, object]:
        return {
            "span_id": self.span_id,
            "stage": self.stage.value,
            "reviewer": self.reviewer.value if self.reviewer else None,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "started_monotonic_ns": self.started_monotonic_ns,
            "ended_monotonic_ns": self.ended_monotonic_ns,
            "duration_ms": self.duration_ms,
            "status": self.outcome.value if self.outcome else "running",
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class TelemetryRun:
    run_id: str
    runtime: str
    round: int
    binding: TelemetryBinding
    started_at: str
    ended_at: str | None
    started_monotonic_ns: int
    outcome: TelemetryOutcome | None
    reason_code: str | None
    started_late: bool
    telemetry_incomplete: bool
    telemetry_incomplete_reason: str | None
    spans: tuple[TelemetrySpan, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "runtime": self.runtime,
            "round": self.round,
            "binding": self.binding.to_json(),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "started_monotonic_ns": self.started_monotonic_ns,
            "status": self.outcome.value if self.outcome else "running",
            "reason_code": self.reason_code,
            "started_late": self.started_late,
            "telemetry_incomplete": self.telemetry_incomplete,
            "telemetry_incomplete_reason": self.telemetry_incomplete_reason,
            "spans": [item.to_json() for item in self.spans],
        }


@dataclass(frozen=True)
class TelemetryLedger:
    schema: int
    runs: tuple[TelemetryRun, ...]

    def to_json(self) -> dict[str, object]:
        return {"schema": self.schema, "runs": [item.to_json() for item in self.runs]}
```

Use exact-key strict parsers and `_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\\Z")`. `TelemetryBinding.status` is only `pending|bound`; a bound record requires every repository/SHA/ref field, while pending permits nullable candidate fields. Run/span `status` is `running` while `outcome is None`, otherwise it is the terminal outcome value. Success requires `reason_code is None`; failure, timeout, incomplete and clock anomaly require a stable reason. The binding contract is always:

```python
{
    "report_text": REPORT_TEXT_CONTRACT_VERSION,
    "diff_recipe": DIFF_RECIPE_VERSION,
    "telemetry_schema": TELEMETRY_SCHEMA_VERSION,
}
```

- [ ] **Step 6: Immutable transition functions와 duration rules를 구현한다**

Provide these exact signatures:

```text
create_run(cwd: Path, *, base_ref: str, runtime: str, round_number: int,
           started_at: str, started_monotonic_ns: int,
           token_hex: Callable[[int], str] = secrets.token_hex) -> TelemetryRun
record_candidate(cwd: Path, *, run_id: str, repository: str,
                 head_ref: str, head_sha: str) -> TelemetryRun
bind_run(cwd: Path, *, run_id: str, snapshot: Snapshot) -> TelemetryRun
start_span(cwd: Path, *, run_id: str, stage: TelemetryStage,
           reviewer: Reviewer | None, attempt: int, started_at: str,
           started_monotonic_ns: int,
           token_hex: Callable[[int], str] = secrets.token_hex) -> TelemetrySpan
finish_span(cwd: Path, *, run_id: str, span_id: str,
            outcome: TelemetryOutcome, reason_code: str | None,
            ended_at: str, ended_monotonic_ns: int) -> TelemetrySpan
recover_run(cwd: Path, *, run_id: str, ended_at: str,
            ended_monotonic_ns: int) -> int
close_run(cwd: Path, *, run_id: str, outcome: TelemetryOutcome,
          reason_code: str | None, ended_at: str) -> TelemetryRun
read_ledger(cwd: Path) -> TelemetryLedger
summarize_run(cwd: Path, *, run_id: str | None = None) -> dict[str, object]
```

Reviewer is required only for reviewer-dimension stages and forbidden for global stages. `duration_ms` is integer floor division of nonnegative monotonic nanoseconds. Either monotonic reversal or `ended_at < started_at` forces `CLOCK_ANOMALY`, `duration_ms=None`, and `TELEMETRY_CLOCK_ANOMALY` regardless of requested outcome.

- [ ] **Step 7: Ledger persistence, pruning과 sanitized summary를 구현한다**

Use `locked_review`, `read_named_file` and `atomic_replace_bytes` from `review_store.py`; never duplicate descriptor code. Missing telemetry means an empty ledger, while malformed/unsafe/oversized telemetry raises its bounded telemetry code and remains unchanged. Serialize with `ensure_ascii=False`, compact separators and exact `0600`.

Summary returns only schema, binding contract/diff SHA, reviewer totals, stage count/total, outcome counts, `telemetry_incomplete`, anomaly reason codes, and a bounded `early_detection` projection. For the first failed `report_validation`, derive `detected_elapsed_ms` from run-start monotonic time, derive `all_reviewers_terminal_elapsed_ms` from the latest terminal `reviewer_total`, and subtract them into `wait_all_delay_ms`. If either milestone is unavailable/anomalous, return null instead of 0. It must not return persisted monotonic values, wall timestamps, repository path, raw span IDs or free-form text.

Add `telemetry.py` to production `PACKAGE_NAMES` before running installer tests, so later CLI commits cannot install an unresolved import.

- [ ] **Step 8: Telemetry focused tests를 통과시킨다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_telemetry.py
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py
```

Expected: both commands PASS without sleep, network, environment mutation or real HOME writes.

- [ ] **Step 9: Telemetry model/store를 커밋한다**

```bash
rtk git add hooks/pre_pr_tribunal/telemetry.py scripts/install-pre-pr-tribunal.py tests/pre_pr_tribunal/test_telemetry.py tests/pre_pr_tribunal/test_installer.py
rtk git commit -m "feat: persist bounded tribunal latency telemetry"
```

---

### Task 6: Telemetry CLI와 controller-observed stage lifecycle

**Files:**

- Modify: `hooks/pre_pr_tribunal/cli.py:38-170`
- Modify: `hooks/pre_pr_tribunal/git_state.py:45-180`
- Test: `tests/pre_pr_tribunal/test_telemetry.py`
- Test: `tests/pre_pr_tribunal/test_model_store.py:1364-1515`

**Interfaces:**

- Consumes: telemetry run/span IDs, fixed stage/reviewer/outcome enums, current repository identity.
- Produces: begin-created `snapshot_preflight` run, `telemetry-start`, `telemetry-finish`, `telemetry-recover`, `telemetry-close`, `telemetry-summary`.
- Preserves: a telemetry failure never changes the primary command exit status, verdict bytes or gate decision.

- [ ] **Step 1: CLI telemetry lifecycle와 begin binding RED tests를 작성한다**

Add `wall_clock=utc_now` and `monotonic_ns=time.monotonic_ns` keyword-only injection seams to the CLI telemetry helpers. Use this subprocess helper for output-shape tests:

```python
def run_cli(git_repo, *arguments):
    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    return subprocess.run(
        [sys.executable, str(cli), *arguments],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_begin_returns_snapshot_bound_telemetry_run(git_repo):
    begun = run_cli(git_repo, "begin", "--base", "master", "--runtime", "codex", "--round", "1")
    payload = json.loads(begun.stdout)
    run_id = payload["telemetry"]["run_id"]
    assert payload["telemetry"]["status"] == "active"
    summary = run_cli(git_repo, "telemetry-summary", "--run-id", run_id)
    value = json.loads(summary.stdout)
    assert value["binding"]["diff_sha256"] == payload["snapshot"]["diff_sha256"]
    assert value["stages"]["snapshot_preflight"]["count"] == 1
```

Add subprocess tests that start/finish A `view_create`, record B timeout with reason `REVIEWER_TIMEOUT`, recover an unfinished C `reviewer_total`, close a run, and reject invalid stage/reviewer/outcome/ID through bounded usage or telemetry error codes.

Add a three-reviewer timing test where A report validation fails at elapsed 10 seconds and B/C terminate at elapsed 20/30 seconds. Assert `early_detection` contains reviewer A, the stable parser code, `detected_elapsed_ms=10000`, `all_reviewers_terminal_elapsed_ms=30000`, and `wait_all_delay_ms=20000`. Repeat with an incomplete C total and assert the last two values are null.

- [ ] **Step 2: Telemetry failure cannot alter tribunal result RED tests를 작성한다**

Prepare malformed, oversized, symlink and `0644` `.review/telemetry.json` cases. For each, run `begin`, report validation and finalization with the same inputs as a control repository. Assert identical primary exit code and identical verdict JSON; only the bounded `telemetry` projection may say `unavailable`. Run the existing gate adapter on the passing verdict and assert its result is byte-for-byte equal to control.

- [ ] **Step 3: Unknown commands로 RED인지 확인한다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_telemetry.py -k 'cli or tribunal_result'
```

Expected: FAIL because telemetry commands and begin telemetry projection are absent.

- [ ] **Step 4: Candidate identity를 bounded Git helper로 구현한다**

Add a frozen `TelemetryCandidate` and:

```text
@dataclass(frozen=True)
class TelemetryCandidate:
    repository: str
    head_ref: str
    head_sha: str

capture_telemetry_candidate(cwd: Path) -> TelemetryCandidate
```

Reuse `_git_environment`, existing origin canonicalization, `symbolic-ref --quiet HEAD` and `rev-parse HEAD`. Do not resolve/fetch base or compute diff in this helper. It exists only to enrich a pending run; `capture_snapshot` remains authoritative and `bind_run` replaces pending candidate fields exactly once.

- [ ] **Step 5: CLI begin을 best-effort telemetry wrapper로 감싼다**

At begin entry, capture wall/monotonic clocks, try `create_run`, record the candidate, and start `snapshot_preflight`. Run the existing `begin_round` unchanged. On success bind its snapshot and finish the span; on primary `TribunalError` finish/close the run with the same stable code before re-raising. Telemetry exceptions are caught separately and represented as:

```python
"telemetry": {
    "status": "active",
    "run_id": run.run_id,
}
```

or:

```python
"telemetry": {
    "status": "unavailable",
    "reason_code": bounded_telemetry_code,
}
```

Do not change the primary exit code or write a verdict based on either projection.

- [ ] **Step 6: External span commands를 exact argparse shape로 구현한다**

Add:

```text
telemetry-start   --run-id ID --stage STAGE [--reviewer A|B|C] --attempt N
telemetry-finish  --run-id ID --span-id ID --outcome OUTCOME [--reason-code CODE]
telemetry-recover --run-id ID
telemetry-close   --run-id ID --outcome OUTCOME [--reason-code CODE]
telemetry-summary [--run-id ID]
```

Start returns `run_id`, `span_id`, stage, reviewer and `status=running`. Finish returns terminal outcome and nullable duration. Recover returns only `run_id`, `recovered_count`. Close returns run status. Summary returns the sanitized projection from Task 5. CLI supplies current UTC and `time.monotonic_ns()`; caller cannot submit timestamps or duration.

- [ ] **Step 7: CLI telemetry and gate-independence tests를 통과시킨다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_telemetry.py
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py tests/pre_pr_tribunal/test_gate_adapters.py
```

Expected: both commands PASS, including malformed telemetry with unchanged gate outcomes.

- [ ] **Step 8: Telemetry CLI를 커밋한다**

```bash
rtk git add hooks/pre_pr_tribunal/cli.py hooks/pre_pr_tribunal/git_state.py tests/pre_pr_tribunal/test_telemetry.py tests/pre_pr_tribunal/test_model_store.py tests/pre_pr_tribunal/test_gate_adapters.py
rtk git commit -m "feat: expose tribunal telemetry lifecycle"
```

---

### Task 7: Claude/Codex Skill, reviewer schema와 operator 문서

**Files:**

- Modify: `skills/pre-pr-tribunal/SKILL.md:10-62`
- Modify: `skills/pre-pr-tribunal/references/report-schema.md:1-150`
- Modify: `skills/pre-pr-tribunal/references/reviewer-a.md:1-45`
- Modify: `skills/pre-pr-tribunal/references/reviewer-b.md:1-45`
- Modify: `skills/pre-pr-tribunal/references/reviewer-c.md:1-45`
- Modify: `hooks/README.md:115-225`
- Test: `tests/pre_pr_tribunal/test_skill_contract.py:45-120, 300-445`

**Interfaces:**

- Consumes: Task 4 report commands, Task 6 telemetry commands and existing 10-step tribunal state machine.
- Produces: one shared runtime-neutral controller contract with immediate per-reviewer validation and exact pre-final checks.
- Preserves: native Claude/Codex dispatch, three detached views, peer privacy, full-panel recovery, no force cleanup and maximum three rounds.

- [ ] **Step 1: New ordered controller contract RED tests를 작성한다**

Replace old Step 7 assertions and add exact required tokens:

```python
def test_skill_stores_and_validates_each_terminal_report_before_finalize():
    steps = numbered_steps(text("SKILL.md"))
    for token in (
        "store-report --reviewer",
        "validate-report --reviewer",
        "--source stored",
        "exact bytes",
        "0600",
        "current-user-owned",
        "regular",
        "non-symlink",
        "raw_sha256",
    ):
        assert token in steps[6] or token in steps[7]
    assert steps[7].index("validate-report") < steps[7].index("finalize --reviewer-a")
    assert "do not call `finalize`" in steps[7]


def test_skill_records_every_required_telemetry_stage_without_making_it_a_gate():
    skill = text("SKILL.md")
    for stage in (
        "snapshot_preflight", "view_create", "reviewer_dispatch_wait",
        "reviewer_total", "report_store", "report_validation", "finalize",
        "view_cleanup", "recovery_retry",
    ):
        assert stage in skill
    assert "telemetry" in skill and "does not change" in skill
```

Keep the exact ten numbered top-level steps. Add a failure-order test proving an invalid early A report does not disclose A to B/C, does not call finalize, waits for already-started peers, and still performs non-force cleanup.

- [ ] **Step 2: Corrected decoded-text documentation RED tests를 작성한다**

Replace the old assertions requiring all controls to be invalid. Require every reviewer reference and `report-schema.md` to state:

```python
for name in (*REFERENCES,):
    body = text(name)
    assert "stdout_excerpt" in body and "stderr_excerpt" in body
    assert "LF" in body and "TAB" in body
    assert "only" in body
    assert "CR" in body and "NUL" in body and "ESC" in body
    assert "do not trim" in body and "do not reserialize" in body
```

Change the documented Reviewer B example to `"stdout_excerpt": "col1\\n\\t1 passed"`; parse it with the real parser and assert the resulting Python string equals `"col1\n\t1 passed"`.

- [ ] **Step 3: Contract tests가 old instructions 때문에 RED인지 확인한다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_skill_contract.py
```

Expected: FAIL on write-after-all-terminal, one-line flattening, missing secure store/validation and missing telemetry stages.

- [ ] **Step 4: Ten-step Skill state machine을 terminal-order validation으로 수정한다**

Keep Steps 1-5 structurally intact. In Step 3 retain `begin` and capture its telemetry run ID. In Step 5 surround each view creation/dispatch with the exact telemetry stages. Rewrite Steps 6-7 so the controller:

1. waits for each started reviewer to become terminal without sharing peer output;
2. records reviewer total outcome;
3. sends that exact response bytes to `store-report --reviewer X` and retains `raw_sha256`; only explicit full-panel pending recovery adds `--replace-pending-recovery` after all three new responses are terminal;
4. immediately calls `validate-report --reviewer X --source stored`;
5. records `report_store` and `report_validation` success/failure/timeout;
6. waits for every already-started peer and cleans every created view without `--force`;
7. immediately before finalization, calls stored validation for A/B/C separately and compares each digest to its receipt;
8. verifies every file is current-user-owned regular non-symlink exact `0600`;
9. stops before `finalize` if any check fails;
10. otherwise records a finalize span and calls the unchanged all-three finalizer.

Telemetry command failure must be reported as an observation gap but must not turn an invalid report into pass or a valid report into fail. Preserve the pending-round rule that explicit recovery reruns the complete A/B/C panel, waits for all three fresh terminal responses before any recovery replacement, replaces all three exact inputs, and never reuses an earlier peer; #115 is not introduced.

- [ ] **Step 5: Reviewer/schema/operator docs를 같은 contract로 수정한다**

Remove the instruction to flatten with ` | ` and the claim that LF/TAB are always invalid. Explain JSON transport precisely: physical unescaped newline is invalid JSON, while JSON escapes decode to allowed LF/TAB only in stdout/stderr excerpts. State that CR, NUL, ESC, DEL, other `Cc`, all `Cs`, secret and absolute home paths remain invalid.

In `hooks/README.md`, document `store-report`, both validation sources, telemetry lifecycle/summary, exact file safety, the diff argument vector/environment contract, stable codes and the rule that telemetry is never a gate input.

- [ ] **Step 6: Skill/schema and parser tests를 통과시킨다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_skill_contract.py
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_model_store.py -k 'execution_excerpts or cli'
```

Expected: both commands PASS; all pretty-printed documented reports parse through the production parser.

- [ ] **Step 7: Shared runtime contract를 커밋한다**

```bash
rtk git add skills/pre-pr-tribunal/SKILL.md skills/pre-pr-tribunal/references/report-schema.md skills/pre-pr-tribunal/references/reviewer-a.md skills/pre-pr-tribunal/references/reviewer-b.md skills/pre-pr-tribunal/references/reviewer-c.md hooks/README.md tests/pre_pr_tribunal/test_skill_contract.py
rtk git commit -m "docs: enforce immediate secure tribunal validation"
```

---

### Task 8: Probe 통합, baseline evidence와 전체 검증

**Files:**

- Modify: `scripts/probe-pre-pr-tribunal.py:1320-1420`
- Modify: `tests/pre_pr_tribunal/test_probe_harness.py`
- Test: `tests/pre_pr_tribunal/test_install_integration.py`
- Test: `tests/pre_pr_tribunal/test_installer.py`
- Create: `docs/validation/2026-09-09-pre-pr-tribunal-validation-telemetry.md`

**Interfaces:**

- Consumes: complete package, Skill links, fake-runtime probe repositories and sanitized telemetry summary.
- Produces: installed CLI lifecycle proof and a reproducible lockfile-only baseline record; Tasks 2/5 already made the installer dependency-complete.
- Preserves: no real HOME, credential store, live GitHub state or source repository mutation in automated tests.

- [ ] **Step 1: Installed lifecycle probe RED test를 작성한다**

Update the fake pass-verdict helper to feed each raw report through installed `store-report`, call installed stored validation, compare the returned SHA-256 values, and only then finalize. Extend the harness assertion to require:

```python
assert telemetry_summary["binding"]["diff_sha256"] == begin_payload["snapshot"]["diff_sha256"]
assert telemetry_summary["stages"]["report_store"]["count"] == 3
assert telemetry_summary["stages"]["report_validation"]["count"] == 3
assert telemetry_summary["stages"]["finalize"]["count"] == 1
assert telemetry_summary["outcomes"]["failure"] == 0
```

Add one negative probe where A contains CR in `stdout_excerpt`; assert A is preserved exact `0600`, immediate validation returns `TEXT_INVALID`, the fake finalizer invocation counter remains zero, and the sanitized `early_detection.wait_all_delay_ms` equals the injected gap between A validation and the last peer terminal event.

- [ ] **Step 2: Probe test가 old direct-write helper로 RED인지 확인한다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py tests/pre_pr_tribunal/test_probe_harness.py
```

Expected: FAIL because the probe bypasses store/validation telemetry and does not return the required summary.

- [ ] **Step 3: Probe를 secure installed CLI lifecycle로 수정한다**

Replace direct report `_write` in `_create_pass_verdict` with installed CLI subprocess stdin, stored validation and digest comparison. Add telemetry start/finish calls around report store/validation and finalization; preserve existing fake runtime isolation, bounded output and cleanup.

- [ ] **Step 4: Installer/probe integration tests를 통과시킨다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal/test_installer.py tests/pre_pr_tribunal/test_install_integration.py tests/pre_pr_tribunal/test_probe_harness.py
```

Expected: PASS with package plan parity, atomic rollback and no temporary HOME artifact leaks.

- [ ] **Step 5: Lockfile-only before-optimization baseline을 수집한다**

Use a disposable Git fixture whose only committed feature change is one lockfile. Run the installed Skill once with telemetry enabled, then run:

```bash
rtk python3 hooks/pre_pr_tribunal/cli.py telemetry-summary --run-id "$RUN_ID"
```

Require a bound snapshot/contract, three reviewer totals, report-store/validation counts, finalize count and zero unreported running spans. If the runtime does not expose dispatch acceptance, retain `RUNTIME_SIGNAL_UNAVAILABLE` and the dispatch-to-terminal `reviewer_total`; do not record 0ms. Identify the largest Reviewer B stage from the returned bounded values. Run the controlled invalid-report case once and record its `early_detection` comparison so #112 has evidence against the previous wait-all detection point.

- [ ] **Step 6: Sanitized validation document를 실제 결과로 작성한다**

Use `apply_patch` to create the validation document only after Step 5. Record the fixture commit identity, contract tuple, exact sanitized summary JSON, focused/full test commands and exits, and the measured Reviewer B bottleneck stage. Do not include report bodies, commands, raw output, tokens, absolute home paths, environment values or invented duration numbers. Mark synthetic/fake-runtime evidence separately from the real lockfile-only observation.

- [ ] **Step 7: Full verification gate를 실행한다**

```bash
rtk python3 -m pytest -q tests/pre_pr_tribunal tests/runtime_hook_installer
rtk python3 -m pytest -q
rtk bash -n install.sh hooks/*.sh scripts/*.sh scripts/lib/*.sh
rtk shellcheck -x -s bash -S error install.sh hooks/*.sh scripts/*.sh scripts/lib/*.sh
rtk git diff --check
rtk git status --short --branch
```

Expected: every command exits 0; `git diff --check` is empty; status contains only the validation document and expected Task 8 source/test changes before commit.

- [ ] **Step 8: Spec coverage를 직접 대조한다**

Verify all of these with targeted searches and test names before staging:

```bash
rtk rg -n 'REPORT_TEXT_CONTRACT_VERSION|DIFF_RECIPE_VERSION|diff_contract' hooks/pre_pr_tribunal tests/pre_pr_tribunal
rtk rg -n 'store-report|validate-report|0600|current-user-owned|non-symlink|raw_sha256' skills/pre-pr-tribunal hooks/README.md tests/pre_pr_tribunal
rtk rg -n 'snapshot_preflight|view_create|reviewer_dispatch_wait|reviewer_total|report_store|report_validation|finalize|view_cleanup|recovery_retry' hooks/pre_pr_tribunal skills/pre-pr-tribunal tests/pre_pr_tribunal
rtk rg -n '#113|#114|#115|#121' docs/superpowers/specs/2026-09-09-pre-pr-tribunal-validation-telemetry-design.md docs/superpowers/plans/2026-09-09-pre-pr-tribunal-validation-telemetry.md
```

Expected: every in-scope contract maps to production code plus a test; excluded issues appear only as explicit scope boundaries, not production behavior.

- [ ] **Step 9: Integration and validation evidence를 커밋한다**

```bash
rtk git add scripts/probe-pre-pr-tribunal.py tests/pre_pr_tribunal/test_probe_harness.py docs/validation/2026-09-09-pre-pr-tribunal-validation-telemetry.md
rtk git commit -m "test: verify tribunal validation telemetry rollout"
```

After the commit, run:

```bash
rtk git status --short --branch
rtk git log --oneline --decorate -10
```

Expected: worktree clean and the eight implementation commits appear after the approved design/plan commits.
