# Codex MCP Transition Truth and Deadline Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the remaining Task 6 journal-truth and absolute-deadline safety failures without changing the public CLI, persisted schema, or signal authority model.

**Architecture:** Extract deterministic journal recovery into a pure `transition_truth` module and place every time-bounded state boundary behind a resource-aware `DeadlineIO` gateway. Integrate those components into `StateStore`, then move pidfd preparation inside cleanup's per-action accounting boundary so every post-delivery exit returns a complete nonzero result.

**Tech Stack:** Python 3.10, Linux procfs/pidfd and descriptor-relative filesystem APIs, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-31-codex-mcp-transition-truth-deadline-design.md`

## Global Constraints

- Python 3.10 is the syntax and runtime floor.
- Preserve journal, receipt, session, process, and signal-intent schema version 1.
- Preserve `StateStore.transition()` and the existing CLI/Hook public signatures.
- `audit` and cleanup dry-run remain strictly read-only.
- Hook failures remain silent and return exit code 0.
- Automatic cleanup sends only SIGTERM; SIGKILL remains confirmed force-only.
- The final lexical root validation is immediately followed by the prepared pidfd send; no clock read, callback, or state boundary may intervene.
- One absolute monotonic deadline is passed unchanged through every bounded helper. After expiration, no new external boundary starts; only in-memory accounting and descriptor/lock finalization (`close` or releasing an already-held `flock`) are permitted.
- A post-effect failure retains durable no-replay truth and returns an unavailable/nonzero result with the complete exact action set accounted.
- Do not mutate live HOME/Codex configuration, run systemd apply, or signal a real/nonfixture process.
- Use disposable private state, fake clocks/backends, and exact fixture identities/pidfds with safe cleanup.
- Every shell command starts with `rtk`; every hand-written edit uses `apply_patch`.

## File Map

- `codex-mcp-ownership/codex_mcp_ownership/transition_truth.py`: canonical transition ID derivation and the pure recovery truth table.
- `codex-mcp-ownership/codex_mcp_ownership/deadline_io.py`: absolute deadline budget and typed, resource-aware boundary calls.
- `codex-mcp-ownership/codex_mcp_ownership/state.py`: private-path validation and transition/journal/receipt orchestration using both extracted components.
- `codex-mcp-ownership/codex_mcp_ownership/cleanup.py`: exact per-action pidfd preparation, delivery, and outcome accounting.
- `codex-mcp-ownership/codex_mcp_ownership/hook.py`: only minimal exception wiring if the StateStore integration requires it.
- `tests/codex_mcp_ownership/test_transition_truth.py`: pure truth-table and transition-ID tests.
- `tests/codex_mcp_ownership/test_deadline_io.py`: real boundary and descriptor-lifetime tests.
- `tests/codex_mcp_ownership/test_state.py`: journal/raw/receipt contradiction and bounded-I/O integration schedules.
- `tests/codex_mcp_ownership/test_cleanup.py`: second-pidfd and complete partial-accounting schedules.
- `tests/codex_mcp_ownership/test_hook_cli.py`: bounded fallback regression only if Hook wiring changes.

---

### Task 1: Pure Transition Recovery Truth

**Files:**
- Create: `codex-mcp-ownership/codex_mcp_ownership/transition_truth.py`
- Create: `tests/codex_mcp_ownership/test_transition_truth.py`

**Interfaces:**
- Consumes: schema-1 journal fields `phase`, `record_kind`, `record_key`, `expected_digest`, `updated_digest`, and `event`.
- Produces: `RecoveryDecision`, `RecoveryEvidence`, `RecoveryContradiction`, `derive_transition_id(...) -> str`, and `decide_recovery(...) -> RecoveryDecision`.

- [ ] **Step 1: Write the truth-table and fixed transition-ID tests**

Create `tests/codex_mcp_ownership/test_transition_truth.py` with literal outcomes and a hand-fixed SHA-256 value:

```python
import pytest

from codex_mcp_ownership.transition_truth import (
    RecoveryContradiction,
    RecoveryDecision,
    RecoveryEvidence,
    decide_recovery,
    derive_transition_id,
)


@pytest.mark.parametrize(
    ("current", "phase", "has_receipt", "expected"),
    [
        ("e", "prepared", False, RecoveryDecision.DISCARD_PREPARED),
        ("u", "prepared", False, RecoveryDecision.FINALIZE_UPDATED),
        ("u", "committed", False, RecoveryDecision.FINALIZE_UPDATED),
        ("u", "prepared", True, RecoveryDecision.ALREADY_RECEIPTED),
        ("u", "committed", True, RecoveryDecision.ALREADY_RECEIPTED),
    ],
)
def test_recovery_truth_table_accepts_only_consistent_states(
    current, phase, has_receipt, expected
):
    evidence = RecoveryEvidence(
        phase=phase,
        current_digest=current,
        expected_digest="e",
        updated_digest="u",
        has_matching_receipt=has_receipt,
    )
    assert decide_recovery(evidence) is expected


@pytest.mark.parametrize(
    ("current", "phase", "has_receipt"),
    [
        ("e", "committed", False),
        ("e", "committed", True),
        ("e", "prepared", True),
        ("third", "prepared", False),
        ("third", "committed", True),
    ],
)
def test_recovery_truth_table_rejects_semantic_contradictions(
    current, phase, has_receipt
):
    evidence = RecoveryEvidence(
        phase=phase,
        current_digest=current,
        expected_digest="e",
        updated_digest="u",
        has_matching_receipt=has_receipt,
    )
    with pytest.raises(RecoveryContradiction):
        decide_recovery(evidence)


def test_recovery_truth_table_rejects_same_digest_and_unknown_phase():
    same = RecoveryEvidence("prepared", "e", "e", "e", False)
    unknown = RecoveryEvidence("invented", "e", "e", "u", False)
    with pytest.raises(RecoveryContradiction):
        decide_recovery(same)
    with pytest.raises(RecoveryContradiction):
        decide_recovery(unknown)


def test_transition_id_matches_schema_one_canonical_fixture():
    event = {
        "schema_version": 1,
        "event": "owner_loss_observed",
        "reason_codes": ["owner_session_ended"],
    }
    assert derive_transition_id(
        "processes", "1" * 64, "2" * 64, "3" * 64, event
    ) == "c18cca6e7e8ef9ab2b68480c5ea2fe384d59552eb26089ebc5b2aa63d293e44a"
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
rtk python3 -m pytest -q tests/codex_mcp_ownership/test_transition_truth.py
```

Expected: collection fails with `ModuleNotFoundError: codex_mcp_ownership.transition_truth`.

- [ ] **Step 3: Implement the complete pure module**

Create `transition_truth.py` with no filesystem or `StateStore` import:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Mapping


class RecoveryDecision(Enum):
    DISCARD_PREPARED = "discard_prepared"
    FINALIZE_UPDATED = "finalize_updated"
    ALREADY_RECEIPTED = "already_receipted"


class RecoveryContradiction(ValueError):
    """Journal phase, raw state, and receipt evidence cannot all be true."""


@dataclass(frozen=True)
class RecoveryEvidence:
    phase: str
    current_digest: str
    expected_digest: str
    updated_digest: str
    has_matching_receipt: bool


def _canonical_json(value: object) -> bytes:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return rendered.encode("utf-8") + b"\n"


def derive_transition_id(
    record_kind: str,
    record_key: str,
    expected_digest: str,
    updated_digest: str,
    event_without_id: Mapping[str, object],
) -> str:
    event = dict(event_without_id)
    event.pop("event_id", None)
    payload = {
        "record_kind": record_kind,
        "record_key": record_key,
        "expected_digest": expected_digest,
        "updated_digest": updated_digest,
        "event": event,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def decide_recovery(evidence: RecoveryEvidence) -> RecoveryDecision:
    if evidence.phase not in {"prepared", "committed"}:
        raise RecoveryContradiction("unknown journal phase")
    if evidence.expected_digest == evidence.updated_digest:
        raise RecoveryContradiction("transition digests are equal")
    if evidence.current_digest == evidence.expected_digest:
        if evidence.phase == "prepared" and not evidence.has_matching_receipt:
            return RecoveryDecision.DISCARD_PREPARED
        raise RecoveryContradiction("expected state contradicts commit evidence")
    if evidence.current_digest != evidence.updated_digest:
        raise RecoveryContradiction("current state matches neither transition digest")
    if evidence.has_matching_receipt:
        return RecoveryDecision.ALREADY_RECEIPTED
    return RecoveryDecision.FINALIZE_UPDATED
```

- [ ] **Step 4: Run RED tests to verify GREEN**

Run:

```bash
rtk python3 -m pytest -q tests/codex_mcp_ownership/test_transition_truth.py
```

Expected: all truth-table and ID tests pass.

- [ ] **Step 5: Run formatting and Python 3.10 compilation checks**

Run:

```bash
rtk ruff check codex-mcp-ownership/codex_mcp_ownership/transition_truth.py tests/codex_mcp_ownership/test_transition_truth.py
rtk ruff format --check codex-mcp-ownership/codex_mcp_ownership/transition_truth.py tests/codex_mcp_ownership/test_transition_truth.py
rtk python3 -m compileall -q codex-mcp-ownership/codex_mcp_ownership/transition_truth.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit Task 1**

```bash
rtk git add codex-mcp-ownership/codex_mcp_ownership/transition_truth.py tests/codex_mcp_ownership/test_transition_truth.py
rtk git commit -m "feat(codex-mcp): define transition recovery truth"
```

---

### Task 2: Resource-Aware Deadline I/O Gateway

**Files:**
- Create: `codex-mcp-ownership/codex_mcp_ownership/deadline_io.py`
- Create: `tests/codex_mcp_ownership/test_deadline_io.py`

**Interfaces:**
- Consumes: one absolute `deadline: float | None` and injected `monotonic: Callable[[], float]`.
- Produces: `OperationDeadlineExceeded`, `DirectoryCapacityExceeded`, `DeadlineBudget`, and `DeadlineIO` typed boundary methods for descriptor duplication, seek, lock, file, and directory operations.

- [ ] **Step 1: Write RED tests for before/after checks, descriptor ownership, and bounded iteration**

Create `tests/codex_mcp_ownership/test_deadline_io.py`:

```python
import fcntl
import os

import pytest

from codex_mcp_ownership import deadline_io


class SequenceClock:
    def __init__(self, values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


def test_expired_budget_starts_no_boundary(tmp_path, monkeypatch):
    calls = []
    original = deadline_io.os.mkdir

    def record(*args, **kwargs):
        calls.append("mkdir")
        return original(*args, **kwargs)

    monkeypatch.setattr(deadline_io.os, "mkdir", record)
    io = deadline_io.DeadlineIO(deadline_io.DeadlineBudget(0.5, lambda: 1.0))
    with pytest.raises(deadline_io.OperationDeadlineExceeded):
        io.mkdir(os.fspath(tmp_path / "never-created"), 0o700)
    assert calls == []


def test_open_fd_closes_handle_when_deadline_crosses_after_open(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    target.write_bytes(b"{}\n")
    opened = []
    original = deadline_io.os.open

    def capture(*args, **kwargs):
        fd = original(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(deadline_io.os, "open", capture)
    io = deadline_io.DeadlineIO(
        deadline_io.DeadlineBudget(0.5, SequenceClock([0.0, 1.0]))
    )
    with pytest.raises(deadline_io.OperationDeadlineExceeded):
        io.open_fd(os.fspath(target), os.O_RDONLY)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_dup_fd_closes_duplicate_when_deadline_crosses_after_dup(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    target.write_bytes(b"{}\n")
    source_fd = os.open(target, os.O_RDONLY)
    duplicated = []
    original = deadline_io.os.dup

    def capture(fd):
        duplicate = original(fd)
        duplicated.append(duplicate)
        return duplicate

    monkeypatch.setattr(deadline_io.os, "dup", capture)
    io = deadline_io.DeadlineIO(
        deadline_io.DeadlineBudget(0.5, SequenceClock([0.0, 1.0]))
    )
    try:
        with pytest.raises(deadline_io.OperationDeadlineExceeded):
            io.dup_fd(source_fd)
        assert len(duplicated) == 1
        with pytest.raises(OSError):
            os.fstat(duplicated[0])
    finally:
        os.close(source_fd)


def test_flock_is_released_when_deadline_crosses_after_acquire(tmp_path):
    target = tmp_path / "state.lock"
    target.write_bytes(b"")
    first = os.open(target, os.O_RDWR)
    second = os.open(target, os.O_RDWR)
    io = deadline_io.DeadlineIO(
        deadline_io.DeadlineBudget(0.5, SequenceClock([0.0, 1.0]))
    )
    try:
        with pytest.raises(deadline_io.OperationDeadlineExceeded):
            io.flock_exclusive_nonblocking(first)
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(second, fcntl.LOCK_UN)
    finally:
        os.close(second)
        os.close(first)


def test_expired_lseek_starts_no_boundary(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"{}\n")
    fd = os.open(target, os.O_RDONLY)
    calls = []
    original = deadline_io.os.lseek

    def record(*args):
        calls.append("lseek")
        return original(*args)

    monkeypatch.setattr(deadline_io.os, "lseek", record)
    io = deadline_io.DeadlineIO(deadline_io.DeadlineBudget(0.5, lambda: 1.0))
    try:
        with pytest.raises(deadline_io.OperationDeadlineExceeded):
            io.lseek(fd, 0, os.SEEK_SET)
        assert calls == []
    finally:
        os.close(fd)


def test_directory_names_stops_before_next_entry_after_expiry(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        io = deadline_io.DeadlineIO(
            deadline_io.DeadlineBudget(
                0.5,
                SequenceClock([0.0, 0.0, 0.0, 1.0]),
            )
        )
        with pytest.raises(deadline_io.OperationDeadlineExceeded):
            io.directory_names(fd, 8)
    finally:
        os.close(fd)


def test_directory_names_rejects_capacity_overflow(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        io = deadline_io.DeadlineIO(deadline_io.DeadlineBudget(None, lambda: 0.0))
        with pytest.raises(deadline_io.DirectoryCapacityExceeded):
            io.directory_names(fd, 2)
    finally:
        os.close(fd)
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
rtk python3 -m pytest -q tests/codex_mcp_ownership/test_deadline_io.py
```

Expected: collection fails with `ImportError` because `deadline_io` does not exist.

- [ ] **Step 3: Implement the budget and typed boundary gateway**

Create `deadline_io.py` with these exact semantics:

```python
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from typing import Callable, TypeVar


_T = TypeVar("_T")


class OperationDeadlineExceeded(RuntimeError):
    """The absolute deadline expired before the next bounded operation."""


class DirectoryCapacityExceeded(RuntimeError):
    """A bounded directory contains more entries than the caller permits."""


@dataclass(frozen=True)
class DeadlineBudget:
    deadline: float | None
    monotonic: Callable[[], float]

    def expired(self) -> bool:
        return self.deadline is not None and self.monotonic() >= self.deadline

    def check(self) -> None:
        if self.expired():
            raise OperationDeadlineExceeded("operation deadline exhausted")

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise OperationDeadlineExceeded("operation deadline exhausted")
        return remaining


class DeadlineIO:
    def __init__(self, budget: DeadlineBudget) -> None:
        self.budget = budget

    def _call(self, operation: Callable[..., _T], *args, **kwargs) -> _T:
        self.budget.check()
        result = operation(*args, **kwargs)
        self.budget.check()
        return result

    def open_fd(
        self,
        name: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        self.budget.check()
        fd = os.open(name, flags, mode, dir_fd=dir_fd)
        try:
            self.budget.check()
        except Exception:
            try:
                self.close_fd(fd)
            except OSError:
                pass
            raise
        return fd

    def dup_fd(self, fd: int) -> int:
        self.budget.check()
        duplicate = os.dup(fd)
        try:
            self.budget.check()
        except Exception:
            try:
                self.close_fd(duplicate)
            except OSError:
                pass
            raise
        return duplicate

    def read(self, fd: int, size: int) -> bytes:
        return self._call(os.read, fd, size)

    def write(self, fd: int, data: bytes) -> int:
        return self._call(os.write, fd, data)

    def lseek(self, fd: int, offset: int, whence: int) -> int:
        return self._call(os.lseek, fd, offset, whence)

    def flock_exclusive_nonblocking(self, fd: int) -> None:
        self.budget.check()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.budget.check()
        except Exception:
            try:
                self.unlock_fd(fd)
            except OSError:
                pass
            raise

    def fstat(self, fd: int) -> os.stat_result:
        return self._call(os.fstat, fd)

    def stat(
        self,
        name: str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = False,
    ) -> os.stat_result:
        return self._call(
            os.stat,
            name,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def mkdir(
        self,
        name: str,
        mode: int,
        *,
        dir_fd: int | None = None,
    ) -> None:
        self._call(os.mkdir, name, mode, dir_fd=dir_fd)

    def fchmod(self, fd: int, mode: int) -> None:
        self._call(os.fchmod, fd, mode)

    def fsync(self, fd: int) -> None:
        self._call(os.fsync, fd)

    def replace(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        self._call(
            os.replace,
            source,
            destination,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=destination_dir_fd,
        )

    def unlink(self, name: str, *, dir_fd: int) -> None:
        self._call(os.unlink, name, dir_fd=dir_fd)

    def directory_names(self, directory_fd: int, limit: int) -> tuple[str, ...]:
        self.budget.check()
        entries = os.scandir(directory_fd)
        try:
            self.budget.check()
            names = []
            iterator = iter(entries)
            while True:
                self.budget.check()
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                self.budget.check()
                if len(names) >= limit:
                    raise DirectoryCapacityExceeded("directory capacity exceeded")
                names.append(entry.name)
            self.budget.check()
            return tuple(sorted(names))
        finally:
            try:
                entries.close()
            except OSError:
                pass

    def close_fd(self, fd: int) -> None:
        os.close(fd)

    def unlock_fd(self, fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
```

- [ ] **Step 4: Run the new test to verify GREEN**

Run:

```bash
rtk python3 -m pytest -q tests/codex_mcp_ownership/test_deadline_io.py
```

Expected: all seven tests pass with no leaked descriptor or held lock.

- [ ] **Step 5: Run static checks**

```bash
rtk ruff check codex-mcp-ownership/codex_mcp_ownership/deadline_io.py tests/codex_mcp_ownership/test_deadline_io.py
rtk ruff format --check codex-mcp-ownership/codex_mcp_ownership/deadline_io.py tests/codex_mcp_ownership/test_deadline_io.py
rtk python3 -m compileall -q codex-mcp-ownership/codex_mcp_ownership/deadline_io.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit Task 2**

```bash
rtk git add codex-mcp-ownership/codex_mcp_ownership/deadline_io.py tests/codex_mcp_ownership/test_deadline_io.py
rtk git commit -m "feat(codex-mcp): centralize deadline boundaries"
```

---

### Task 3: Integrate the Journal Truth Table

**Files:**
- Modify: `codex-mcp-ownership/codex_mcp_ownership/state.py:1973-2025`
- Modify: `codex-mcp-ownership/codex_mcp_ownership/state.py:2295-2480`
- Modify: `tests/codex_mcp_ownership/test_state.py`

**Interfaces:**
- Consumes: Task 1's `derive_transition_id()`, `RecoveryEvidence`, `RecoveryDecision`, and `decide_recovery()`.
- Produces: schema-1 journal loading and recovery whose only decisions come from the pure truth table.

- [ ] **Step 1: Add RED integration tests for semantic contradiction and ID derivation**

Append tests using the existing `sample_process()`, `make_private_directory()`, `write_private_file()`, and `_owner_loss_event()` helpers:

```python
def _write_journal(store, event_id, journal):
    directory = store.root / "event-journal"
    make_private_directory(directory)
    write_private_file(
        directory / f"{event_id}.json",
        json.dumps(journal, sort_keys=True, separators=(",", ":")).encode() + b"\n",
    )


def test_committed_journal_with_expected_raw_state_is_corruption_without_event(
    tmp_path
):
    store = state.StateStore(tmp_path / "state")
    expected = sample_process()
    updated = replace(expected, first_owner_gone_boot=200.0)
    store.save_process(expected)
    event_id, journal = store._build_transition_journal(
        "processes",
        expected.wrapper.stable_key(),
        expected,
        updated,
        _owner_loss_event(expected, "semantic_contradiction"),
    )
    journal["phase"] = "committed"
    _write_journal(store, event_id, journal)

    with pytest.raises(state.StateCorruption):
        store.recover_transition_events()

    assert store.load_raw_process(expected.wrapper.stable_key()) == expected
    assert list((store.root / "event-receipts").glob("*.json")) == []
    assert not (store.root / "events.jsonl").exists()
    assert (store.root / "event-journal" / f"{event_id}.json").exists()


def test_receipt_with_expected_raw_state_is_corruption_and_preserves_evidence(
    tmp_path
):
    store = state.StateStore(tmp_path / "state")
    expected = sample_process()
    updated = replace(expected, first_owner_gone_boot=200.0)
    store.save_process(expected)
    event_id, journal = store._build_transition_journal(
        "processes",
        expected.wrapper.stable_key(),
        expected,
        updated,
        _owner_loss_event(expected, "receipt_contradiction"),
    )
    _write_journal(store, event_id, journal)
    receipt_dir = store.root / "event-receipts"
    make_private_directory(receipt_dir)
    receipt = {
        "schema_version": 1,
        "transition_id": event_id,
        "event_id": event_id,
        "committed_revision": store.ledger_revision(),
        "event": journal["event"],
    }
    write_private_file(
        receipt_dir / f"{event_id}.json",
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode() + b"\n",
    )

    with pytest.raises(state.StateCorruption):
        store.recover_transition_events()

    assert (receipt_dir / f"{event_id}.json").exists()
    assert (store.root / "event-journal" / f"{event_id}.json").exists()


def test_journal_event_id_must_match_derived_transition_id(tmp_path):
    store = state.StateStore(tmp_path / "state")
    expected = sample_process()
    updated = replace(expected, first_owner_gone_boot=200.0)
    store.save_process(expected)
    event_id, journal = store._build_transition_journal(
        "processes",
        expected.wrapper.stable_key(),
        expected,
        updated,
        _owner_loss_event(expected, "derived_id"),
    )
    journal["event"]["reason_codes"] = ["mutated_after_id_derivation"]
    _write_journal(store, event_id, journal)

    with pytest.raises(state.StateCorruption):
        store.recover_transition_events()

    assert (store.root / "event-journal" / f"{event_id}.json").exists()
```

- [ ] **Step 2: Run the three tests and verify RED**

Run:

```bash
rtk python3 -m pytest -q \
  tests/codex_mcp_ownership/test_state.py::test_committed_journal_with_expected_raw_state_is_corruption_without_event \
  tests/codex_mcp_ownership/test_state.py::test_receipt_with_expected_raw_state_is_corruption_and_preserves_evidence \
  tests/codex_mcp_ownership/test_state.py::test_journal_event_id_must_match_derived_transition_id
```

Expected: all three fail because phase and receipt are accepted independently and the ID is not recomputed.

- [ ] **Step 3: Make journal construction use the shared ID derivation**

Import Task 1's symbols and replace the inline SHA-256 payload in `_build_transition_journal()`:

```python
from .transition_truth import (
    RecoveryContradiction,
    RecoveryDecision,
    RecoveryEvidence,
    decide_recovery,
    derive_transition_id,
)


event_id = derive_transition_id(
    record_kind,
    record_key,
    expected_digest,
    updated_digest,
    event_payload,
)
```

Keep the existing `expected_digest == updated_digest` rejection and add `event_id` to `event_payload` only after derivation.

- [ ] **Step 4: Validate the derived ID during journal load**

After structural validation in `_load_journal_locked()`, derive from a copy without `event_id` and reject mismatch without mutating state:

```python
event_without_id = dict(payload["event"])
event_without_id.pop("event_id", None)
derived = derive_transition_id(
    payload["record_kind"],
    payload["record_key"],
    payload["expected_digest"],
    payload["updated_digest"],
    event_without_id,
)
if derived != event_id:
    raise StateCorruption(
        self.root / "event-journal" / (event_id + ".json"),
        hashlib.sha256(raw).hexdigest(),
    )
```

- [ ] **Step 5: Replace recovery's boolean `committed` expression with the truth table**

In `_recover_one_transition_locked()`, construct evidence and branch only on `RecoveryDecision`:

```python
try:
    decision = decide_recovery(
        RecoveryEvidence(
            phase=journal["phase"],
            current_digest=current_digest,
            expected_digest=journal["expected_digest"],
            updated_digest=journal["updated_digest"],
            has_matching_receipt=has_receipt,
        )
    )
except RecoveryContradiction as error:
    raise StateCorruption(
        self.root / "event-journal" / (event_id + ".json"),
        hashlib.sha256(_canonical_json(journal)).hexdigest(),
    ) from error

if decision is RecoveryDecision.FINALIZE_UPDATED:
    created = self._write_event_receipt_locked(
        root_fd,
        event_id,
        journal["event"],
        self.ledger_revision(deadline=deadline, monotonic=monotonic),
        deadline=deadline,
        monotonic=monotonic,
    )
    if created:
        self._append_event_locked(
            root_fd,
            _canonical_json(journal["event"]),
            deadline=deadline,
            monotonic=monotonic,
        )
    self._prune_event_receipts_locked(
        deadline=deadline,
        monotonic=monotonic,
    )
elif decision is RecoveryDecision.ALREADY_RECEIPTED:
    created = False
elif decision is RecoveryDecision.DISCARD_PREPARED:
    created = False
```

Only after a non-contradictory decision may the journal be unlinked and its directory fsynced. Return `decision is not RecoveryDecision.DISCARD_PREPARED`.

- [ ] **Step 6: Run the RED tests and existing journal/receipt regression slice**

Run:

```bash
rtk python3 -m pytest -q \
  tests/codex_mcp_ownership/test_transition_truth.py \
  tests/codex_mcp_ownership/test_state.py -k 'transition or journal or receipt or event_rotation'
```

Expected: the new contradiction tests and existing recovery/dedup tests pass.

- [ ] **Step 7: Run Task 3 static checks and commit**

```bash
rtk ruff check codex-mcp-ownership/codex_mcp_ownership/state.py codex-mcp-ownership/codex_mcp_ownership/transition_truth.py tests/codex_mcp_ownership/test_state.py tests/codex_mcp_ownership/test_transition_truth.py
rtk ruff format --check codex-mcp-ownership/codex_mcp_ownership/state.py codex-mcp-ownership/codex_mcp_ownership/transition_truth.py tests/codex_mcp_ownership/test_state.py tests/codex_mcp_ownership/test_transition_truth.py
rtk python3 -m compileall -q codex-mcp-ownership/codex_mcp_ownership/state.py codex-mcp-ownership/codex_mcp_ownership/transition_truth.py
rtk git diff --check
rtk git add codex-mcp-ownership/codex_mcp_ownership/state.py tests/codex_mcp_ownership/test_state.py
rtk git commit -m "fix(codex-mcp): enforce journal recovery truth"
```

---

### Task 4: Integrate DeadlineIO Across State Mutation and Recovery

**Files:**
- Modify: `codex-mcp-ownership/codex_mcp_ownership/state.py`
- Modify: `tests/codex_mcp_ownership/test_state.py`
- Modify only if imports require it: `codex-mcp-ownership/codex_mcp_ownership/classify.py`
- Modify only if imports require it: `codex-mcp-ownership/codex_mcp_ownership/cleanup.py`

**Interfaces:**
- Consumes: Task 2's `OperationDeadlineExceeded`, `DeadlineBudget`, `DeadlineIO`, and `DirectoryCapacityExceeded`.
- Produces: one unchanged absolute budget across transition/journal/receipt/ledger operations and no semantic boundary after expiration.

- [ ] **Step 1: Add RED schedules for compound directory creation, atomic-write cleanup, and post-effect recovery**

Add tests that advance a fake clock at the exact returned boundary and record later calls:

```python
def test_expiry_after_journal_directory_mkdir_starts_no_open_or_fsync(
    tmp_path, monkeypatch
):
    store = state.StateStore(tmp_path / "state")
    expected = sample_process()
    updated = replace(expected, first_owner_gone_boot=200.0)
    store.save_process(expected)
    now = [0.0]
    later = []
    original_mkdir = state.deadline_io.os.mkdir
    original_open = state.deadline_io.os.open
    original_fsync = state.deadline_io.os.fsync

    def expire_after_mkdir(*args, **kwargs):
        result = original_mkdir(*args, **kwargs)
        now[0] = 1.0
        return result

    def record_open(*args, **kwargs):
        if now[0] >= 0.5:
            later.append("open")
        return original_open(*args, **kwargs)

    def record_fsync(*args, **kwargs):
        if now[0] >= 0.5:
            later.append("fsync")
        return original_fsync(*args, **kwargs)

    monkeypatch.setattr(state.deadline_io.os, "mkdir", expire_after_mkdir)
    monkeypatch.setattr(state.deadline_io.os, "open", record_open)
    monkeypatch.setattr(state.deadline_io.os, "fsync", record_fsync)

    with pytest.raises(state.OperationDeadlineExceeded):
        store.transition(
            "processes",
            expected.wrapper.stable_key(),
            expected,
            updated,
            _owner_loss_event(expected, "mkdir_deadline"),
            deadline=0.5,
            monotonic=lambda: now[0],
        )
    assert later == []


def test_expiry_after_atomic_write_starts_no_fsync_stat_or_unlink(
    tmp_path, monkeypatch
):
    store = state.StateStore(tmp_path / "state")
    expected = sample_process()
    updated = replace(expected, first_owner_gone_boot=200.0)
    store.save_process(expected)
    now = [0.0]
    later = []
    original_write = state.DeadlineIO.write
    original_fsync = state.deadline_io.os.fsync
    original_stat = state.deadline_io.os.stat
    original_unlink = state.deadline_io.os.unlink

    def expire_after_write(io, fd, data):
        result = original_write(io, fd, data)
        now[0] = 1.0
        return result

    def record_fsync(*args, **kwargs):
        if now[0] >= 0.5:
            later.append("fsync")
        return original_fsync(*args, **kwargs)

    def record_stat(*args, **kwargs):
        if now[0] >= 0.5:
            later.append("stat")
        return original_stat(*args, **kwargs)

    def record_unlink(*args, **kwargs):
        if now[0] >= 0.5:
            later.append("unlink")
        return original_unlink(*args, **kwargs)

    monkeypatch.setattr(state.DeadlineIO, "write", expire_after_write)
    monkeypatch.setattr(state.deadline_io.os, "fsync", record_fsync)
    monkeypatch.setattr(state.deadline_io.os, "stat", record_stat)
    monkeypatch.setattr(state.deadline_io.os, "unlink", record_unlink)

    with pytest.raises(state.OperationDeadlineExceeded):
        store.transition(
            "processes",
            expected.wrapper.stable_key(),
            expected,
            updated,
            _owner_loss_event(expected, "write_deadline"),
            deadline=0.5,
            monotonic=lambda: now[0],
        )
    assert later == []


def test_post_effect_deadline_does_not_start_known_transition_recovery(
    tmp_path, monkeypatch
):
    store = state.StateStore(tmp_path / "state")
    expected = sample_process()
    updated = replace(expected, first_owner_gone_boot=200.0)
    store.save_process(expected)
    now = [0.0]
    effects = []

    def effect():
        effects.append("sent")
        now[0] = 1.0

    monkeypatch.setattr(
        store,
        "_recover_known_transition_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("expired effect must not start recovery")
        ),
    )
    with pytest.raises(state.PostEffectStateError):
        store.transition(
            "processes",
            expected.wrapper.stable_key(),
            expected,
            updated,
            _owner_loss_event(expected, "post_effect_deadline"),
            deadline=0.5,
            monotonic=lambda: now[0],
            effect=effect,
    )
    assert effects == ["sent"]
    assert store.load_raw_process(expected.wrapper.stable_key()) == expected
    assert len(list((store.root / "event-journal").glob("*.json"))) == 1
```

- [ ] **Step 2: Run the three schedules and verify RED**

```bash
rtk python3 -m pytest -q \
  tests/codex_mcp_ownership/test_state.py::test_expiry_after_journal_directory_mkdir_starts_no_open_or_fsync \
  tests/codex_mcp_ownership/test_state.py::test_expiry_after_atomic_write_starts_no_fsync_stat_or_unlink \
  tests/codex_mcp_ownership/test_state.py::test_post_effect_deadline_does_not_start_known_transition_recovery
```

Expected: current compound helpers start later boundaries or call deadline-free recovery.

- [ ] **Step 3: Re-export the shared deadline exception and create one I/O object per operation**

Remove the local exception class from `state.py` and import it so existing consumers continue to resolve `state.OperationDeadlineExceeded`:

```python
from . import deadline_io
from .deadline_io import (
    DeadlineBudget,
    DeadlineIO,
    DirectoryCapacityExceeded,
    OperationDeadlineExceeded,
)
```

At each deadline-aware public entry (`ledger_revision`, record loaders,
`append_event`, `transition`, and `recover_transition_events`), create exactly one
pair and pass it through private helpers without changing the public signature:

```python
budget = DeadlineBudget(deadline, monotonic)
io = DeadlineIO(budget)
```

Public writers/loaders that have no deadline parameter create the same pair with
`DeadlineBudget(None, time.monotonic)` and reuse it for their complete locked
operation. This keeps private helper signatures uniform without making an
unbounded call accidentally inherit another operation's budget.

Change `locked()` to accept the shared `io` internally. Use
`io.budget.remaining()` to cap condition waits and the nonblocking-flock retry
sleep against the same absolute operation deadline while retaining the separate
`StateLockTimeout` cap. Acquire through `io.flock_exclusive_nonblocking()` and
release through `io.unlock_fd()` plus `io.close_fd()` in `finally`. Remove
`_deadline_check()` and `_remaining_timeout()` once all callers use the shared
budget directly.

Do not create a new deadline, another `DeadlineBudget`/`DeadlineIO`, or use
`deadline=None` below that entry. In particular, bounded private helpers must
call private loading and ledger cores with the existing `io`; they must not
recurse through public wrappers that would allocate a fresh gateway.

- [ ] **Step 4: Convert the bounded helper call graph to typed DeadlineIO methods**

Thread `io: DeadlineIO` through these exact paths:

```text
deadline-aware public entry
  -> locked(io=io)
  -> _walk_root / _open_root / _create_lock_file / _validate_lock_binding
  -> _load_exact_record_locked_or_read_only / _load_records_locked_or_read_only
  -> transition
     -> _recover_before_write_locked
        -> _recover_transition_events_locked
        -> _recover_legacy_outbox_locked
        -> _prune_event_receipts_locked
     -> _validate_transition_authority_locked
     -> _write_transition_journal_locked
     -> _write_transition_record_locked
     -> _mark_transition_committed_locked
     -> _recover_known_transition_locked
        -> _recover_one_transition_locked
        -> _load_journal_locked
        -> _transition_record_digest_locked
        -> _load_event_receipt_locked / _write_event_receipt_locked
        -> _append_event_locked / _rotate_events_locked
        -> ledger revision read/write
  -> _atomic_json / _open_directory / _open_private_file
```

For every `open/dup/read/write/lseek/fstat/stat/mkdir/fchmod/fsync/replace/unlink/scandir/flock`
in this call graph, use the matching `DeadlineIO` method. Use
`io.directory_names()` instead of raw `listdir`/`scandir` in bounded maintenance.
Convert `DirectoryCapacityExceeded` to the existing redacted `StateCorruption`
at the caller that knows the private path. The sole boundary exception is the
already-approved final lexical root validation immediately before `effect()`;
its raw validation calls must have no clock read inserted between validation and
the effect.

`_open_root(io=io)` uses `io.dup_fd()` for a pinned root and validates that
duplicate with `io.fstat()`. Malformed-record fallback uses `io.lseek()` followed
by `io.read()`. A successful flock post-check failure is released inside
`DeadlineIO`; normal outer-lock teardown may only unlock/close resources it
already owns.

- [ ] **Step 5: Make directory creation and atomic writes stop cleanly at the boundary**

Implement both lexical root walking and private-directory creation without a
post-mkdir chmod. The mode is supplied to `io.mkdir()` and the next opened
descriptor is validated before use:

```python
try:
    return io.open_fd(name, flags, dir_fd=root_fd)
except FileNotFoundError:
    if not create:
        return None
    if not self._owns_lock():
        raise RuntimeError("private directory creation requires the state lock")
    io.mkdir(name, _DIRECTORY_MODE, dir_fd=root_fd)
    fd = io.open_fd(name, flags, dir_fd=root_fd)
    _validate_directory(io.fstat(fd), path)
    io.fsync(root_fd)
    return fd
```

In `_atomic_json()`, never start evidence cleanup after deadline expiration:

```python
fd: int | None = None
created: os.stat_result | None = None
try:
    fd = io.open_fd(temporary, flags, _FILE_MODE, dir_fd=directory_fd)
    created = io.fstat(fd)
    io.fchmod(fd, _FILE_MODE)
    _write_all_with_io(io, fd, data)
    io.fsync(fd)
    closing_fd = fd
    fd = None
    io.close_fd(closing_fd)
    try:
        target_fd = self._open_private_file(
            directory_fd,
            name,
            directory / name,
            io=io,
        )
    except FileNotFoundError:
        target_fd = None
    if target_fd is not None:
        io.close_fd(target_fd)
    io.replace(
        temporary,
        name,
        source_dir_fd=directory_fd,
        destination_dir_fd=directory_fd,
    )
    io.fsync(directory_fd)
except OperationDeadlineExceeded:
    if fd is not None:
        closing_fd = fd
        fd = None
        try:
            io.close_fd(closing_fd)
        except OSError:
            pass
    raise
except Exception:
    if fd is not None:
        closing_fd = fd
        fd = None
        try:
            io.close_fd(closing_fd)
        except OSError:
            pass
    if not io.budget.expired() and created is not None:
        try:
            current = io.stat(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            current = None
        if (
            current is not None
            and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino)
            and stat.S_ISREG(current.st_mode)
            and current.st_uid == os.getuid()
            and stat.S_IMODE(current.st_mode) == _FILE_MODE
            and current.st_nlink == 1
        ):
            io.unlink(temporary, dir_fd=directory_fd)
    raise
```

`_write_all_with_io()` loops until all bytes are written and calls `io.write()` once per iteration. A zero-byte write raises `OSError`.

Replace `_bump_ledger_revision_locked()`'s bespoke
`.ledger-revision-<token>` writer with this same `_atomic_json()` primitive at
the root directory. Revision calculation remains unchanged; only the write path
is unified so it has the same post-expiry and reconciliation semantics.

- [ ] **Step 6: Add bounded fresh-budget temp reconciliation**

Add `_reconcile_atomic_temps_locked(io)` and call it first in
`_recover_before_write_locked()`, before journal enumeration. Scan only the state
root and the six `_atomic_json()` directories (`sessions`, `processes`,
`signal-intents`, `force-receipts`, `event-journal`, `event-receipts`) with
`io.directory_names()`: use `TRANSITION_JOURNAL_LIMIT` for the journal and
`STATE_DIRECTORY_MAX_ENTRIES` for the root and other directories.

Recognize only full-match `\.tmp-[0-9a-f]{32}` names. Any name beginning with
`.tmp-` that does not full-match fails closed without deletion. Across the
sorted candidate set, inspect and remove at most 64 per maintenance pass. For
each candidate, use no-follow `io.stat()`, require a regular file, current uid,
mode `0o600`, and link count 1, then `io.unlink()` it; fsync only directories
actually changed. A nonconforming candidate remains as evidence and raises the
existing redacted `StateCorruption`.

Add a test that expires after a temporary journal write, verifies the temp
remains and no stat/unlink begins after expiration, then runs a fresh-budget
writer and verifies bounded removal occurs before journal recovery. Also cover
a malformed/symlink temp-like entry and assert it is preserved with corruption.

- [ ] **Step 7: Preserve the final root/effect zero-boundary and stop post-effect recovery on expiry**

The effect-bearing order must remain exactly:

```python
if before_effect is not None:
    before_effect()
    budget.check()
if effect is not None:
    self._effect_transition_ids.add(event_id)
    try:
        if expected_root_binding is not None:
            self.validate_root_binding(expected_root_binding)
        effect()
        effect_completed = True
    finally:
        self._effect_transition_ids.discard(event_id)
```

There is no budget check between `validate_root_binding()` and `effect()`. After `effect()` returns, a failed `budget.check()` raises `PostEffectStateError(record_persisted=False)` directly. For non-deadline failures, `_recover_known_transition_locked()` receives the original `io`; it never creates `deadline=None` recovery.

- [ ] **Step 8: Run state/deadline tests and the Task 1–4 regression slice**

```bash
rtk python3 -m pytest -q \
  tests/codex_mcp_ownership/test_deadline_io.py \
  tests/codex_mcp_ownership/test_transition_truth.py \
  tests/codex_mcp_ownership/test_state.py
rtk python3 -m pytest -q \
  tests/codex_mcp_ownership/test_procfs.py \
  tests/codex_mcp_ownership/test_classify.py \
  tests/codex_mcp_ownership/test_supervisor.py
```

Expected: all tests pass; no fake boundary starts after the injected expiration.

- [ ] **Step 9: Run static checks and commit Task 4**

```bash
rtk ruff check codex-mcp-ownership/codex_mcp_ownership/deadline_io.py codex-mcp-ownership/codex_mcp_ownership/state.py codex-mcp-ownership/codex_mcp_ownership/classify.py codex-mcp-ownership/codex_mcp_ownership/cleanup.py tests/codex_mcp_ownership/test_deadline_io.py tests/codex_mcp_ownership/test_state.py
rtk ruff format --check codex-mcp-ownership/codex_mcp_ownership/deadline_io.py codex-mcp-ownership/codex_mcp_ownership/state.py codex-mcp-ownership/codex_mcp_ownership/classify.py codex-mcp-ownership/codex_mcp_ownership/cleanup.py tests/codex_mcp_ownership/test_deadline_io.py tests/codex_mcp_ownership/test_state.py
rtk python3 -m compileall -q codex-mcp-ownership tests/codex_mcp_ownership
rtk git diff --check
rtk git add codex-mcp-ownership/codex_mcp_ownership/deadline_io.py codex-mcp-ownership/codex_mcp_ownership/state.py codex-mcp-ownership/codex_mcp_ownership/classify.py codex-mcp-ownership/codex_mcp_ownership/cleanup.py tests/codex_mcp_ownership/test_deadline_io.py tests/codex_mcp_ownership/test_state.py
rtk git commit -m "fix(codex-mcp): enforce absolute state deadlines"
```

Stage only files actually changed; do not make no-op edits to optional consumers.

---

### Task 5: Account for Deadline Expiry During Pidfd Preparation

**Files:**
- Modify: `codex-mcp-ownership/codex_mcp_ownership/cleanup.py:820-1235`
- Modify: `codex-mcp-ownership/codex_mcp_ownership/cleanup.py:1813-1860`
- Modify: `tests/codex_mcp_ownership/test_cleanup.py`
- Modify only if exception wiring changed: `codex-mcp-ownership/codex_mcp_ownership/hook.py`
- Modify only if Hook wiring changed: `tests/codex_mcp_ownership/test_hook_cli.py`

**Interfaces:**
- Consumes: unchanged `CleanupReport`, durable `SignalIntent.dispatch_keys`, `CleanupDeadlineExceeded`, and exact action ordering.
- Produces: a complete report for every deadline after the first irreversible effect, including expiry before or after a later pidfd open.

- [ ] **Step 1: Write RED second-pidfd expiry schedules**

Use the existing `_expand_stubborn_process()` helper and add a backend that can expire before or after returning the second pidfd:

```python
@pytest.mark.parametrize("expiry_point", ["before_second_open", "after_second_open"])
def test_force_second_pidfd_deadline_returns_complete_partial_report(
    stubborn_context, expiry_point
):
    store, tree, clock, _snapshot, process, _lease, _classification = stubborn_context
    snapshot, _second = _expand_stubborn_process(store, tree, clock, process)
    classification = snapshot.classifications[0]
    token = cleanup.issue_force_token(classification, clock)
    actions = cleanup.plan_cleanup(snapshot, force=True)
    now = [0.0]
    first_sent = [False]

    class ExpiringProcfs:
        def __init__(self, delegate):
            self.delegate = delegate

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def observe_identity(self, pid):
            result = self.delegate.observe_identity(pid)
            if (
                expiry_point == "before_second_open"
                and first_sent[0]
                and pid == actions[1].identity.pid
            ):
                now[0] = 1.0
            return result

    class ExpiringBackend(FakeSignalBackend):
        def __init__(self):
            super().__init__()
            self.opens = 0

        def open(self, identity):
            self.opens += 1
            pidfd = super().open(identity)
            if self.opens == 2 and expiry_point == "after_second_open":
                now[0] = 1.0
            return pidfd

        def send(self, pidfd, signum):
            super().send(pidfd, signum)
            first_sent[0] = True

    signaler = ExpiringBackend()
    report = cleanup.execute_cleanup(
        actions,
        store,
        ExpiringProcfs(tree),
        signaler,
        clock,
        apply=True,
        confirm_token=token,
        deadline=0.5,
        monotonic=lambda: now[0],
    )

    assert report.partial_force is True
    assert report.deadline_expired is True
    assert report.after_available is False
    assert report.attempted == 1
    assert len(report.outcomes) == len(actions)
    assert report.skipped == len(actions) - 1
    assert [call[0] for call in signaler.calls].count("send") == 1
    if expiry_point == "after_second_open":
        assert [call[0] for call in signaler.calls].count("close") == 2
    else:
        assert [call[0] for call in signaler.calls].count("close") == 1
```

Add the automatic TERM counterpart with a real two-identity orphan and expiry after
the second pidfd is opened:

```python
def test_term_second_pidfd_deadline_returns_unavailable_nonpartial_report(
    orphan_context
):
    store, tree, clock, _snapshot, process, _lease = orphan_context
    write_proc_entry(
        tree.proc_root,
        654,
        "654 (second) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 6540 0 0\n",
        tree.proc_root / "node",
        "VmRSS:\t48 kB\n",
    )
    second = tree.identity(654)
    assert second is not None
    expanded = replace(process, members=(process.wrapper, second))
    store.save_process(expanded)
    snapshot = classify.build_audit(store, tree, clock)
    actions = cleanup.plan_cleanup(snapshot)
    assert len(actions) == 2
    now = [0.0]

    class ExpiringBackend(FakeSignalBackend):
        def __init__(self):
            super().__init__()
            self.opens = 0

        def open(self, identity):
            self.opens += 1
            pidfd = super().open(identity)
            if self.opens == 2:
                now[0] = 1.0
            return pidfd

    signaler = ExpiringBackend()
    report = cleanup.execute_cleanup(
        actions,
        store,
        tree,
        signaler,
        clock,
        apply=True,
        deadline=0.5,
        monotonic=lambda: now[0],
    )

    assert report.partial_force is False
    assert report.deadline_expired is True
    assert report.after_available is False
    assert report.attempted == 1
    assert len(report.outcomes) == len(actions)
    assert [call[0] for call in signaler.calls].count("send") == 1
    assert [call[0] for call in signaler.calls].count("close") == 2
```

- [ ] **Step 2: Run the new schedules and verify RED**

```bash
rtk python3 -m pytest -q tests/codex_mcp_ownership/test_cleanup.py -k 'second_pidfd_deadline'
```

Expected: `CleanupDeadlineExceeded` escapes before a `CleanupReport` is returned.

- [ ] **Step 3: Move pidfd preparation inside the per-action accounting boundary**

Initialize `pidfd: int | None = None` before the action `try`, call `_prepare_exact_signal()` inside it, and preserve the existing outcome branches. The deadline branch must account for the current and all later actions:

Move the current outer `try` so it begins before `pidfd = None` and therefore
contains `_prepare_exact_signal()`, the prepared-outcome branch, and the existing
intent/dispatch/effect/post-signal block. Use this exact deadline handler:

```python
except CleanupDeadlineExceeded:
    deadline_expired = True
    if delivered_keys:
        if forced:
            partial_force = True
        for remaining in process_actions[index:]:
            skipped = CleanupOutcome(
                remaining,
                "skipped",
                (
                    "partial_force_deadline_exhausted"
                    if forced
                    else "cleanup_deadline_exhausted"
                ),
            )
            outcomes.append(skipped)
            group_outcomes.append(skipped)
        break
    raise
finally:
    if pidfd is not None:
        try:
            signaler.close(pidfd)
        except OSError:
            close_failed = True
```

Do not duplicate the current inner close. Close each acquired pidfd exactly once. Keep final root validation immediately adjacent to `signaler.send()` inside `StateStore.transition()`.

- [ ] **Step 4: Make report and CLI status explicit for every accounted expiry**

Use the existing `deadline_expired` and `after_available` fields. Once an expiry after a delivery is caught, skip final audit/procfs work, set `after_available=False`, retain the complete outcome vector, and let the existing CLI nonzero predicate consume `deadline_expired` or unavailable state. Do not append a new event after expiration; the durable dispatch record remains no-replay evidence.

- [ ] **Step 5: Run focused hostile and lifecycle suites**

```bash
rtk python3 -m pytest -q \
  tests/codex_mcp_ownership/test_transition_truth.py \
  tests/codex_mcp_ownership/test_deadline_io.py \
  tests/codex_mcp_ownership/test_state.py \
  tests/codex_mcp_ownership/test_cleanup.py \
  tests/codex_mcp_ownership/test_hook_cli.py \
  tests/codex_mcp_ownership/test_supervisor.py
```

Expected: all focused tests pass, including semantic contradiction, every boundary crossing, singleton/multi post-effect truth, and second-pidfd accounting.

- [ ] **Step 6: Run the ownership package and full repository suites**

```bash
rtk python3 -m pytest -q tests/codex_mcp_ownership
rtk env PYTHONPATH=.superpowers/sdd/2026-08-29-codex-mcp-ownership/baseline-deps python3 -m pytest -q
```

Expected: both suites pass. The pinned dependency command is authoritative if the host environment lacks `slack_bolt`.

- [ ] **Step 7: Run final static, privacy, and leak gates**

```bash
rtk ruff check codex-mcp-ownership/codex_mcp_ownership/transition_truth.py codex-mcp-ownership/codex_mcp_ownership/deadline_io.py codex-mcp-ownership/codex_mcp_ownership/state.py codex-mcp-ownership/codex_mcp_ownership/cleanup.py codex-mcp-ownership/codex_mcp_ownership/classify.py codex-mcp-ownership/codex_mcp_ownership/hook.py tests/codex_mcp_ownership/test_transition_truth.py tests/codex_mcp_ownership/test_deadline_io.py tests/codex_mcp_ownership/test_state.py tests/codex_mcp_ownership/test_cleanup.py tests/codex_mcp_ownership/test_hook_cli.py
rtk ruff format --check codex-mcp-ownership/codex_mcp_ownership/transition_truth.py codex-mcp-ownership/codex_mcp_ownership/deadline_io.py codex-mcp-ownership/codex_mcp_ownership/state.py codex-mcp-ownership/codex_mcp_ownership/cleanup.py codex-mcp-ownership/codex_mcp_ownership/classify.py codex-mcp-ownership/codex_mcp_ownership/hook.py tests/codex_mcp_ownership/test_transition_truth.py tests/codex_mcp_ownership/test_deadline_io.py tests/codex_mcp_ownership/test_state.py tests/codex_mcp_ownership/test_cleanup.py tests/codex_mcp_ownership/test_hook_cli.py
rtk python3 -m compileall -q codex-mcp-ownership tests/codex_mcp_ownership
rtk git diff --check
rtk rg -n 'SEMANTIC_CANARY|traceback\.print' codex-mcp-ownership/codex_mcp_ownership/transition_truth.py codex-mcp-ownership/codex_mcp_ownership/deadline_io.py codex-mcp-ownership/codex_mcp_ownership/state.py codex-mcp-ownership/codex_mcp_ownership/cleanup.py codex-mcp-ownership/codex_mcp_ownership/hook.py
rtk rg -n 'os\.kill\(|time\.sleep\(' codex-mcp-ownership/codex_mcp_ownership/hook.py codex-mcp-ownership/codex_mcp_ownership/cli.py
rtk rg -n '/usr/bin/systemctl|shell=False' codex-mcp-ownership/codex_mcp_ownership
rtk pgrep -af 'fake-mcp|late-fork|supervisor-fixture|descendant-fixture|ownership-fixture'
```

Expected: Ruff, format, compile, and diff checks pass; forbidden scans show no production canary, traceback, direct Hook signal, or sleep regression; systemctl remains fixed argv with `shell=False`; fixture process scan shows no leaked fixture.

- [ ] **Step 8: Commit Task 5**

```bash
rtk git add codex-mcp-ownership/codex_mcp_ownership/cleanup.py codex-mcp-ownership/codex_mcp_ownership/hook.py tests/codex_mcp_ownership/test_cleanup.py tests/codex_mcp_ownership/test_hook_cli.py
rtk git commit -m "fix(codex-mcp): account bounded pidfd preparation"
```

Stage only files actually changed. The task is complete only after an independent reviewer returns `SPEC PASS / QUALITY PASS / ARCHITECTURAL CLEAR` for the complete redesign range.
