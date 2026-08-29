# Codex MCP Ownership and Safe Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Record exact Codex session-to-stdio-MCP ownership on Linux and terminate only revalidated orphans after a grace period.

**Architecture:** A transparent supervisor wraps each opted-in Codex stdio MCP while Codex SessionStart and SessionEnd command hooks maintain private session leases. A local classifier joins leases to exact Linux process identities; a dry-run-first CLI and a 60-second user-systemd scavenger use pidfds to send automatic SIGTERM only after the owner is gone, 120 seconds have elapsed, and the identity has been revalidated.

**Tech Stack:** Python 3.10+, Linux procfs and pidfd, Codex hooks and MCP configuration, tomlkit 0.15.1 for setup-time lossless TOML edits, Bash, systemd user units, pytest.

**Spec:** docs/superpowers/specs/2026-08-29-codex-mcp-ownership-design.md

## Global Constraints

- Scope is Linux Codex stdio MCP only. Do not modify Claude MCP configuration or manage HTTP MCP servers.
- Default owner-loss grace is exactly 120 seconds. The user-systemd timer interval is exactly 60 seconds. Shutdown grace before stubborn classification is exactly 10 seconds.
- Automatic cleanup sends SIGTERM only. SIGKILL requires the manual force path, a fresh exact-identity check, and a short-lived confirmation token.
- Unknown, ambiguous, unmanaged, active, and explicit shared processes receive no automatic signal.
- Process identity is boot ID + PID + procfs start ticks + executable device/inode. A pidfd is mandatory for every automatic signal.
- Runtime wrapper, hook, classifier, and scavenger use the Python standard library only. tomlkit 0.15.1 is imported only by setup and rollback code.
- Python 3.10 is the CI floor. Do not use StrEnum, tomllib without a fallback path, or syntax introduced after Python 3.10.
- User config is the default target. A project config is touched only through an explicit --project PATH.
- audit and dry-run cleanup perform no tool-authored filesystem writes, including event-log writes.
- Never store or print environment values, full command lines, MCP protocol data, or transcript contents.
- All state directories are mode 0700 and all state, transaction, backup, and installed executable files are mode 0600 or 0500 as specified.
- Configuration writes use private-file validation, a kernel flock, inode/hash compare-and-swap, same-directory temporary files, file fsync, atomic rename, and directory fsync.
- The ownership state lock is local kernel flock state. It does not use Project Control Registry, Claim, writer ownership, force-end, or takeover.
- The Project Control worktree is already isolated. Do not create a second worktree.
- Every terminal command in this repository starts with rtk. Use apply_patch for hand-authored file changes.
- Each implementation task ends in a focused local commit. Do not push, open a PR, merge, or deploy without a fresh Project Control task assert-owner gate.
- Official contracts: https://learn.chatgpt.com/codex/hooks and https://developers.openai.com/codex/extend/mcp.
- Setup dependency source: tomlkit 0.15.1, Python >=3.9, from https://pypi.org/project/tomlkit/.

---

## File Structure

~~~text
codex-mcp-ownership/
  entry.py
  requirements-setup.lock
  README.md
  codex_mcp_ownership/
    __init__.py
    clock.py
    model.py
    procfs.py
    state.py
    classify.py
    cleanup.py
    supervisor.py
    hook.py
    cli.py
    config_edit.py
    install.py
  systemd/
    codex-mcp-ownership-cleanup.service.template
    codex-mcp-ownership-cleanup.timer.template
scripts/
  setup-codex-mcp-ownership.sh
tests/
  codex_mcp_ownership/
    conftest.py
    helpers.py
    fixtures/
      fake_mcp.py
      owner_harness.py
    test_procfs.py
    test_state.py
    test_classify.py
    test_cleanup.py
    test_supervisor.py
    test_hook_cli.py
    test_config_edit.py
    test_install.py
    test_e2e.py
requirements-test.in
requirements-test.lock
tests/test_pytest_workflow.py
README.md
~~~

Responsibilities:

- model.py owns immutable schemas and JSON-safe serialization. It performs no I/O.
- clock.py owns the injected wall/boot-time contract so grace decisions never depend on wall-clock jumps.
- procfs.py is the only module that reads procfs or opens pidfds.
- state.py owns private directories, atomic JSON, flock, corruption reporting/quarantine, and mutation-only event logging.
- classify.py owns owner association, state transitions, grace calculations, and audit snapshots. It sends no signals.
- cleanup.py converts an audit snapshot into exact signal actions, revalidates pidfds, records TERM/force results, and never discovers targets by name.
- supervisor.py transparently runs one stdio MCP and records its process identity and owner association.
- hook.py turns Codex lifecycle JSON into session leases and requests a bounded scavenger run.
- cli.py exposes audit, cleanup, explain, hook, and supervise command surfaces and redacted output.
- config_edit.py performs pure lossless TOML and additive hooks.json transformations.
- install.py performs preflight, private transactions, installed-runtime copies, validation, systemd activation, and rollback.
- entry.py is the single stable Python entry point used by the CLI, Codex hooks, and wrapped MCP commands.
- setup-codex-mcp-ownership.sh is the opt-in shell entry, management-venv bootstrap, and mode dispatcher.

Shared test interfaces in tests/codex_mcp_ownership/helpers.py and conftest.py:

- FakeClock implements Clock and advance(seconds).
- FakeProcTree builds procfs fixtures and returns a LinuxProcfs through fake_proc and procfs fixtures.
- sample_lease, matching_lease, sample_process, and process are immutable model fixtures.
- ClassificationScenario.classify(owner_state, host_live, elapsed) builds a complete live-evidence case; scenario and snapshot_for expose it to classification tests.
- FakeStateStore tracks mutation_count without touching HOME; store and state_snapshot expose it.
- FakeSignalBackend records open/send/close; fake_signaler and unavailable_signaler cover supported and unsupported pidfd paths.
- hook_runtime, cli_runner, supervisor_command, installer_fixture, and e2e expose only subprocesses and paths created under pytest tmp_path.
- Every subprocess fixture records PID plus start ticks and removes only that exact identity in try/finally teardown.

All commands below run from the repository root.

---

### Task 1: Exact Linux Process Identity

**Files:**
- Create: codex-mcp-ownership/codex_mcp_ownership/__init__.py
- Create: codex-mcp-ownership/codex_mcp_ownership/clock.py
- Create: codex-mcp-ownership/codex_mcp_ownership/model.py
- Create: codex-mcp-ownership/codex_mcp_ownership/procfs.py
- Create: tests/codex_mcp_ownership/conftest.py
- Create: tests/codex_mcp_ownership/helpers.py
- Create: tests/codex_mcp_ownership/test_procfs.py

**Interfaces:**
- Produces: model.ProcessIdentity with stable_key() -> str, to_dict() -> dict[str, object], and from_dict(data) -> ProcessIdentity.
- Produces: model.ObservedTime, model.SessionLease, and model.ManagedProcess.
- Produces: clock.Clock protocol with wall_iso() -> str and boottime() -> float, plus clock.SystemClock.
- Produces: procfs.LinuxProcfs.identity(pid: int) -> ProcessIdentity | None.
- Produces: procfs.LinuxProcfs.ancestor_chain(pid: int) -> tuple[ProcessIdentity, ...].
- Produces: procfs.LinuxProcfs.group_members(pgid: int) -> tuple[ProcessIdentity, ...].
- Produces: procfs.LinuxProcfs.rss_kib(identity: ProcessIdentity) -> int | None.
- Produces: procfs.LinuxProcfs.open_pidfd(identity: ProcessIdentity) -> int.
- Consumes: no feature code.

- [ ] **Step 1: Add import wiring and failing procfs parsing tests**

Create conftest.py with the exact package-root insertion:

~~~python
from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "codex-mcp-ownership"
sys.path.insert(0, str(PACKAGE_ROOT))
~~~

Create helpers.py with FakeClock, FakeProcTree, and write_proc_entry(root, pid, stat_line, exe_target, status_text="VmRSS: 0 kB\n"). FakeClock exposes wall_iso(), boottime(), and advance(seconds). FakeProcTree exposes identity(pid), write_start_ticks(pid, value), and rss_kib(identity). Add a fake_proc pytest fixture in conftest.py that returns FakeProcTree. In test_procfs.py, cover a comm containing a closing parenthesis:

~~~python
def test_parse_stat_uses_last_closing_parenthesis():
    raw = (
        "321 (node worker) extra) S 77 88 88 0 -1 0 0 0 0 0 "
        "0 0 0 0 20 0 1 0 424242 0 0"
    )
    parsed = procfs.parse_stat(raw)
    assert parsed.ppid == 77
    assert parsed.pgid == 88
    assert parsed.start_ticks == 424242
~~~

Also assert that malformed stat data raises procfs.ProcfsFormatError rather than producing a partial identity.

- [ ] **Step 2: Run the focused test and verify red**

Run:

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_procfs.py -q
~~~

Expected: FAIL because codex_mcp_ownership.procfs does not exist.

- [ ] **Step 3: Implement immutable models and stat parsing**

Define Python 3.10-compatible dataclasses in model.py. Use typing.Literal for state names and tuple fields so records cannot be mutated after classification.

Use these exact core fields:

~~~python
@dataclass(frozen=True)
class ProcessIdentity:
    boot_id: str
    pid: int
    ppid: int
    pgid: int
    start_ticks: int
    exe_dev: int
    exe_ino: int
    exe_name: str


@dataclass(frozen=True)
class ObservedTime:
    wall_iso: str
    boot_id: str
    boottime: float


@dataclass(frozen=True)
class SessionLease:
    schema_version: int
    session_id: str
    cwd: str
    source: str
    host_keys: tuple[str, ...]
    state: Literal["active", "ended"]
    observed: ObservedTime
    ended: ObservedTime | None = None


@dataclass(frozen=True)
class ManagedProcess:
    schema_version: int
    record_id: str
    scope: str
    server: str
    cwd: str
    wrapper: ProcessIdentity
    child: ProcessIdentity | None
    members: tuple[ProcessIdentity, ...]
    pgid: int
    host_keys: frozenset[str]
    spawned: ObservedTime
    owner_session_id: str | None = None
    shared_owner: str | None = None
    first_owner_gone_boot: float | None = None
    term_sent_boot: float | None = None
    exit_code: int | None = None
~~~

Implement the proc stat parser around the last closing parenthesis:

~~~python
def parse_stat(raw: str) -> ProcStat:
    left = raw.find("(")
    right = raw.rfind(")")
    if left <= 0 or right <= left:
        raise ProcfsFormatError("invalid proc stat framing")
    fields = raw[right + 1 :].strip().split()
    if len(fields) < 20:
        raise ProcfsFormatError("incomplete proc stat")
    try:
        return ProcStat(
            ppid=int(fields[1]),
            pgid=int(fields[2]),
            start_ticks=int(fields[19]),
        )
    except ValueError as error:
        raise ProcfsFormatError("non-integer proc stat field") from error
~~~

ProcessIdentity.stable_key() must hash canonical JSON containing only boot_id, pid, start_ticks, exe_dev, and exe_ino. Do not include ppid, pgid, cwd, or executable name in the identity key.

Every persisted model implements strict to_dict() and from_dict(): reject missing keys, extra keys, bool values where integers are required, non-finite floats, and schema versions other than 1. JSON serialization uses sort_keys=True, separators=(",", ":"), ensure_ascii=False.

SystemClock.boottime() uses time.clock_gettime(time.CLOCK_BOOTTIME). Persist grace timestamps as boot-relative seconds and pair them with boot ID; use UTC wall time only for human display.

- [ ] **Step 4: Implement LinuxProcfs exact reads**

LinuxProcfs accepts proc_root and boot_id_path constructor arguments for fixtures. identity() reads stat, follows proc/PID/exe only for stat metadata, and returns None on disappearance. It must read stat once before executable metadata and once after; mismatched PID/start_ticks returns None.

Use this final comparison:

~~~python
before = parse_stat(self._read_text(base / "stat"))
exe_stat = (base / "exe").stat()
after = parse_stat(self._read_text(base / "stat"))
if before != after:
    return None
~~~

ancestor_chain() follows exact ppid identities with a visited-PID set and a maximum of 128 hops. group_members() scans numeric directories and includes only identities whose live pgid equals the requested pgid. rss_kib() parses exactly the VmRSS line and revalidates the identity after reading.

- [ ] **Step 5: Add identity, ancestry, RSS, and PID-reuse tests**

Add tests that prove:

~~~python
def test_identity_changes_when_pid_is_reused(fake_proc):
    first = fake_proc.identity(321)
    fake_proc.write_start_ticks(321, first.start_ticks + 1)
    second = fake_proc.identity(321)
    assert first.stable_key() != second.stable_key()


def test_rss_rejects_reused_identity(fake_proc):
    identity = fake_proc.identity(321)
    fake_proc.write_start_ticks(321, identity.start_ticks + 1)
    assert fake_proc.rss_kib(identity) is None
~~~

Also cover a missing parent, ancestry cycle, executable inode change, boot ID change, nonnumeric proc entries, and a valid group with two members.

- [ ] **Step 6: Run Task 1 tests**

Run:

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_procfs.py -q
rtk python3 -m compileall -q codex-mcp-ownership/codex_mcp_ownership
~~~

Expected: all focused tests pass and compileall exits 0.

- [ ] **Step 7: Commit Task 1**

~~~bash
rtk git add codex-mcp-ownership/codex_mcp_ownership tests/codex_mcp_ownership
rtk git commit -m "feat(codex): add exact MCP process identities"
~~~

---

### Task 2: Private Ownership State Store

**Files:**
- Create: codex-mcp-ownership/codex_mcp_ownership/state.py
- Create: tests/codex_mcp_ownership/test_state.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/model.py
- Modify: tests/codex_mcp_ownership/helpers.py

**Interfaces:**
- Consumes: all Task 1 dataclasses and stable identity keys.
- Produces: state.StateStore(root: Path, read_only: bool = False, lock_timeout: float = 2.0).
- Produces: StateStore.load_sessions() -> tuple[SessionLease, ...].
- Produces: StateStore.load_processes() -> tuple[ManagedProcess, ...].
- Produces: save_session(), save_process(), remove_process(), append_event(), and locked().
- Produces: state.StateCorruption and state.UnsafeStatePath exceptions.
- Constants: EVENT_LOG_MAX_BYTES = 1_048_576, EVENT_LOG_BACKUPS = 3, EVENT_LOG_RETENTION_SECONDS = 2_592_000, and TRANSACTION_RETENTION = 3.

- [ ] **Step 1: Write failing private-state tests**

Add tests for an absent read-only root, hashed session filenames, deterministic modes, and atomic JSON:

~~~python
def test_read_only_store_does_not_create_root(tmp_path):
    root = tmp_path / "missing"
    store = state.StateStore(root, read_only=True)
    assert store.load_sessions() == ()
    assert not root.exists()


def test_session_filename_is_hash_not_untrusted_id(tmp_path, sample_lease):
    store = state.StateStore(tmp_path / "state")
    store.save_session(sample_lease)
    files = list((store.root / "sessions").iterdir())
    assert len(files) == 1
    assert sample_lease.session_id not in files[0].name
    assert files[0].stat().st_mode & 0o777 == 0o600
~~~

Add a session ID containing slash and a control character and assert validation fails before any directory is created.

- [ ] **Step 2: Run state tests and verify red**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_state.py -q
~~~

Expected: FAIL because StateStore is absent.

- [ ] **Step 3: Implement secure root and atomic JSON writes**

Implement session_key() as SHA-256 over validated UTF-8 bytes. Accept 1 through 128 characters from letters, digits, dot, underscore, hyphen, and colon; reject every other value.

Atomic writes must use a directory fd and these flags:

~~~python
flags = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
~~~

Write canonical JSON plus newline, fchmod 0600, fsync the file, replace within the same dirfd, and fsync the directory. Validate every existing directory as current-UID, real directory, mode 0700, and every file as current-UID regular file, nlink 1, mode 0600.

- [ ] **Step 4: Implement kernel-flock locking and mutation-only events**

StateStore.locked() opens state.lock with O_NOFOLLOW, validates it, and takes LOCK_EX | LOCK_NB until the injected monotonic deadline. It never unlinks state.lock.

append_event() accepts only an explicit allowlist:

~~~python
EVENT_FIELDS = {
    "schema_version",
    "event",
    "observed_wall",
    "server",
    "scope",
    "session_id",
    "process_key",
    "state",
    "reason_codes",
    "rss_kib",
}
~~~

Raise ValueError for command, args, env, transcript_path, or any unknown key. A read-only store rejects append_event() without creating files.

Before appending, rotate events.jsonl when the new record would exceed 1,048,576 bytes. Keep at most three numbered 0600 backups and remove backups older than 2,592,000 seconds only inside a mutating locked operation. Keep at most three completed transaction directories; never prune the transaction currently referenced by install state.

- [ ] **Step 5: Add corruption, symlink, hardlink, and contention tests**

Prove that read-only loads return a StateCorruption record without moving the corrupt file. In a mutating locked operation, quarantine it to a private corrupt directory using its content digest.

Add exact safety assertions:

~~~python
@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "mode"])
def test_unsafe_state_file_is_rejected(tmp_path, unsafe_kind):
    store, target = build_unsafe_store(tmp_path, unsafe_kind)
    with pytest.raises(state.UnsafeStatePath):
        store.load_processes()
    assert target.read_bytes() == b"sentinel"
~~~

Hold flock on a separate file descriptor and assert a second StateStore exits with state.StateLockTimeout and does not delete or replace the lock inode.

Fill the event log beyond 1 MiB with redacted fixture events and assert at most events.jsonl plus three backups exist, every file is 0600, the newest event is present, and an audit read does not rotate or prune. Create four completed transactions and assert only the newest three remain after the next mutating operation.

- [ ] **Step 6: Run Task 2 tests**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_state.py tests/codex_mcp_ownership/test_procfs.py -q
~~~

Expected: all tests pass.

- [ ] **Step 7: Commit Task 2**

~~~bash
rtk git add codex-mcp-ownership/codex_mcp_ownership/model.py codex-mcp-ownership/codex_mcp_ownership/state.py tests/codex_mcp_ownership
rtk git commit -m "feat(codex): persist private MCP ownership state"
~~~

---

### Task 3: Owner Association and State Classification

**Files:**
- Create: codex-mcp-ownership/codex_mcp_ownership/classify.py
- Create: tests/codex_mcp_ownership/test_classify.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/model.py

**Interfaces:**
- Consumes: SessionLease, ManagedProcess, LinuxProcfs, and StateStore.
- Produces: model.Association(kind, session_id, shared_owner, reason_codes).
- Produces: model.Classification(process, state, reason_codes, live_identities, grace_deadline_boot, eligible_term).
- Produces: model.AuditSnapshot(schema_version, generated, classifications, state_counts, process_count, rss_kib, ownership_coverage, corrupt_count).
- Produces: classify.associate_owner(process, leases, now_boot) -> Association.
- Produces: classify.classify_process(process, leases, procfs, now_boot, grace_seconds=120.0) -> Classification.
- Produces: classify.build_audit(store, procfs, clock) -> AuditSnapshot.
- Constants: ASSOCIATION_WINDOW_SECONDS = 30.0 and OWNER_GRACE_SECONDS = 120.0.

- [ ] **Step 1: Write failing unique and ambiguous association tests**

Use exact host identity overlap, normalized cwd, and observed boot time:

~~~python
def test_unique_matching_lease_becomes_owner(process, matching_lease):
    association = classify.associate_owner(
        process,
        (matching_lease,),
        now_boot=105.0,
    )
    assert association.kind == "session"
    assert association.session_id == matching_lease.session_id


def test_two_matching_leases_are_unknown(process, matching_lease):
    other = replace(matching_lease, session_id="thr_other")
    association = classify.associate_owner(
        process,
        (matching_lease, other),
        now_boot=105.0,
    )
    assert association.kind == "unknown"
    assert association.reason_codes == ("multiple_matching_sessions",)
~~~

Also require unknown for cwd mismatch, no exact host identity, or a spawn outside the 30-second window.

- [ ] **Step 2: Run association tests and verify red**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_classify.py -q
~~~

Expected: FAIL because classify.py is absent.

- [ ] **Step 3: Implement deterministic association**

Add these exact result shapes to model.py:

~~~python
@dataclass(frozen=True)
class Association:
    kind: Literal["session", "shared", "unknown"]
    session_id: str | None
    shared_owner: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    process: ManagedProcess
    state: Literal[
        "active", "shared", "exiting", "orphan",
        "unknown", "stubborn", "gone",
    ]
    reason_codes: tuple[str, ...]
    live_identities: tuple[ProcessIdentity, ...]
    grace_deadline_boot: float | None
    eligible_term: bool


@dataclass(frozen=True)
class AuditSnapshot:
    schema_version: int
    generated: ObservedTime
    classifications: tuple[Classification, ...]
    state_counts: tuple[tuple[str, int], ...]
    process_count: int
    rss_kib: int
    ownership_coverage: tuple[tuple[str, int], ...]
    corrupt_count: int
~~~

Normalize cwd with os.path.realpath only for comparison; never use payload cwd to construct a state path. A lease matches only when all predicates are true:

~~~python
same_host = bool(process.host_keys & frozenset(lease.host_keys))
same_cwd = os.path.realpath(process.cwd) == os.path.realpath(lease.cwd)
within_window = abs(process.spawned_boot - lease.observed_boot) <= 30.0
is_active = lease.state == "active"
~~~

Return session only for one match, shared only when process.shared_owner is explicitly set, and unknown otherwise. Do not choose the newest lease.

- [ ] **Step 4: Write failing state-transition tests**

Cover the approved matrix:

~~~python
@pytest.mark.parametrize(
    ("owner_state", "host_live", "elapsed", "expected"),
    [
        ("active", True, 999.0, "active"),
        ("ended", False, 0.0, "exiting"),
        ("ended", False, 119.9, "exiting"),
        ("ended", False, 120.0, "orphan"),
    ],
)
def test_owner_lifecycle(owner_state, host_live, elapsed, expected, scenario):
    result = scenario.classify(owner_state, host_live, elapsed)
    assert result.state == expected
~~~

Add explicit shared, unmanaged, missing identity, process gone, TERM sent less than 10 seconds ago, and TERM survivor at 10 seconds.

- [ ] **Step 5: Implement classification and audit metrics**

Classification reason_codes are stable snake_case strings. On first owner loss, return exiting with first_owner_gone_boot set in the proposed updated record; the caller persists it only in a mutating path. A read-only audit computes the proposal without writing.

build_audit() must:

1. load all records without creating state,
2. classify from live proc evidence,
3. count states,
4. sum RSS only for revalidated managed identities,
5. return corrupt-state findings as unknown entries,
6. compute ownership_coverage with managed, owned_or_shared, and unknown counts,
7. never call save_process(), append_event(), or quarantine().

- [ ] **Step 6: Run Task 3 tests**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_classify.py tests/codex_mcp_ownership/test_state.py tests/codex_mcp_ownership/test_procfs.py -q
~~~

Expected: all tests pass.

- [ ] **Step 7: Commit Task 3**

~~~bash
rtk git add codex-mcp-ownership/codex_mcp_ownership tests/codex_mcp_ownership
rtk git commit -m "feat(codex): classify MCP ownership evidence"
~~~

---

### Task 4: Revalidated Cleanup Engine

**Files:**
- Create: codex-mcp-ownership/codex_mcp_ownership/cleanup.py
- Create: tests/codex_mcp_ownership/test_cleanup.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/model.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/procfs.py

**Interfaces:**
- Consumes: AuditSnapshot, Classification, ProcessIdentity, StateStore, and LinuxProcfs.
- Produces: model.CleanupAction, model.CleanupOutcome, and model.CleanupReport.
- Produces: cleanup.SignalBackend protocol with open(identity), send(pidfd, signum), and close(pidfd).
- Produces: cleanup.PidfdSignalBackend.
- Produces: cleanup.plan_cleanup(snapshot, force=False) -> tuple[CleanupAction, ...].
- Produces: cleanup.execute_cleanup(actions, store, procfs, signaler, clock, apply=False, confirm_token=None) -> CleanupReport.
- Produces: cleanup.issue_force_token(classification, clock) -> str.
- Constants: SHUTDOWN_GRACE_SECONDS = 10.0 and FORCE_TOKEN_TTL_SECONDS = 300.0.

- [ ] **Step 1: Write failing dry-run and eligibility tests**

Use a FakeSignalBackend that records every open and send:

~~~python
def test_dry_run_never_opens_pidfd(
    orphan_snapshot,
    fake_signaler,
    store,
    procfs,
    clock,
):
    report = cleanup.execute_cleanup(
        cleanup.plan_cleanup(orphan_snapshot),
        store,
        procfs,
        fake_signaler,
        clock,
        apply=False,
    )
    assert report.attempted == 0
    assert fake_signaler.calls == []
    assert store.mutation_count == 0


@pytest.mark.parametrize("state_name", ["active", "shared", "exiting", "unknown", "gone"])
def test_non_orphans_never_become_automatic_actions(snapshot_for, state_name):
    actions = cleanup.plan_cleanup(snapshot_for(state_name))
    assert actions == ()
~~~

- [ ] **Step 2: Run cleanup tests and verify red**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_cleanup.py -q
~~~

Expected: FAIL because cleanup.py is absent.

- [ ] **Step 3: Implement pidfd-only automatic TERM**

Add exact cleanup result models:

~~~python
@dataclass(frozen=True)
class CleanupAction:
    process_key: str
    identity: ProcessIdentity
    classification_state: str
    reason_codes: tuple[str, ...]
    force: bool = False


@dataclass(frozen=True)
class CleanupOutcome:
    action: CleanupAction
    status: Literal["terminated", "survived", "skipped"]
    reason: str


@dataclass(frozen=True)
class CleanupReport:
    before_count: int
    before_rss_kib: int
    after_count: int
    after_rss_kib: int
    attempted: int
    terminated: int
    survived: int
    skipped: int
    outcomes: tuple[CleanupOutcome, ...]
~~~

PidfdSignalBackend must require both os.pidfd_open and signal.pidfd_send_signal. If either is unavailable, raise PidfdUnavailable before mutation.

For every action:

~~~python
live = procfs.identity(action.identity.pid)
if live != action.identity:
    return CleanupOutcome(action, "skipped", "identity_changed")
pidfd = signaler.open(action.identity)
try:
    if procfs.identity(action.identity.pid) != action.identity:
        return CleanupOutcome(action, "skipped", "identity_changed_after_pidfd")
    signaler.send(pidfd, signal.SIGTERM)
finally:
    signaler.close(pidfd)
~~~

Never call os.kill(), os.killpg(), pkill, or a name-based process lookup from cleanup.py.

- [ ] **Step 4: Add PID reuse and partial-subtree tests**

Simulate identity changes before pidfd open and after pidfd open. Both cases must record skipped and send zero signals.

Construct a wrapper plus two descendants where one descendant has a new start tick. Assert TERM is sent only to exact identities and the reused PID is skipped.

- [ ] **Step 5: Implement stubborn force tokens**

The canonical force-token payload contains schema version, boot ID, all exact process stable keys, classification reason codes, issued_boot, and expires_boot. Hash the canonical JSON with SHA-256.

plan_cleanup(snapshot, force=True) returns force-marked actions only for stubborn classifications. execute_cleanup() rejects a force-marked action with a missing, expired, or mismatched token before opening pidfds. A valid force action uses SIGKILL only for a current stubborn classification and repeats the same pidfd revalidation used for TERM.

- [ ] **Step 6: Add force and unsupported-kernel tests**

Assert:

~~~python
def test_expired_force_token_sends_no_signal(
    stubborn_action,
    expired_token,
    store,
    procfs,
    fake_signaler,
    clock,
):
    with pytest.raises(cleanup.InvalidForceConfirmation):
        cleanup.execute_cleanup(
            (stubborn_action,),
            store,
            procfs,
            fake_signaler,
            clock,
            apply=True,
            confirm_token=expired_token,
        )
    assert fake_signaler.calls == []


def test_pidfd_unavailable_is_diagnostic_only(
    orphan_action,
    store,
    procfs,
    unavailable_signaler,
    clock,
):
    report = cleanup.execute_cleanup(
        (orphan_action,),
        store,
        procfs,
        unavailable_signaler,
        clock,
        apply=True,
    )
    assert report.skipped == 1
    assert report.outcomes[0].reason == "pidfd_unavailable"
~~~

- [ ] **Step 7: Run Task 4 tests**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_cleanup.py tests/codex_mcp_ownership/test_classify.py -q
~~~

Expected: all tests pass with zero real signal calls.

- [ ] **Step 8: Commit Task 4**

~~~bash
rtk git add codex-mcp-ownership/codex_mcp_ownership tests/codex_mcp_ownership
rtk git commit -m "feat(codex): terminate only revalidated MCP orphans"
~~~

---

### Task 5: Transparent MCP Supervisor

**Files:**
- Create: codex-mcp-ownership/codex_mcp_ownership/supervisor.py
- Create: tests/codex_mcp_ownership/fixtures/fake_mcp.py
- Create: tests/codex_mcp_ownership/test_supervisor.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/model.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/classify.py

**Interfaces:**
- Consumes: LinuxProcfs, StateStore, associate_owner(), and ManagedProcess.
- Produces: supervisor.SupervisorRequest(scope, server, command, args, cwd).
- Produces: supervisor.run_supervisor(request, store, procfs, clock) -> int.
- Produces: supervisor.forward_signal(child_pgid: int, signum: int) -> None for the live wrapper normal-exit path only.

- [ ] **Step 1: Write a failing stdio transparency test**

The fake MCP reads newline-delimited bytes and writes the same bytes back with no startup output:

~~~python
import sys

for line in sys.stdin.buffer:
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
~~~

The integration assertion is:

~~~python
def test_supervisor_preserves_stdio_and_exit_code(supervisor_command, tmp_path):
    result = subprocess.run(
        supervisor_command + [sys.executable, str(FAKE_MCP)],
        input=b'{"jsonrpc":"2.0"}\n',
        capture_output=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == b'{"jsonrpc":"2.0"}\n'
~~~

- [ ] **Step 2: Run supervisor tests and verify red**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_supervisor.py -q
~~~

Expected: FAIL because supervisor.py is absent.

- [ ] **Step 3: Implement inherited-stdio child execution**

Use subprocess.Popen with inherited descriptors and a new session:

~~~python
child = subprocess.Popen(
    [request.command, *request.args],
    stdin=None,
    stdout=None,
    stderr=None,
    cwd=request.cwd,
    start_new_session=True,
    close_fds=True,
)
~~~

Record wrapper and child identities, child pgid, safe server/scope, cwd, spawn times, and the wrapper host-chain keys. Do not record argv or environment.

Install handlers for SIGTERM, SIGINT, and SIGHUP that forward the same signal to the wrapper-created child group. This normal live-parent forwarding is separate from scavenger cleanup; the cleanup module remains pidfd-only.

- [ ] **Step 4: Implement bounded owner reconciliation**

Try association immediately, then at 50, 100, 200, 400, and 800 milliseconds using an injected sleeper. Stop on the first exact unique session/shared result. Persist unknown after the final attempt; a later classifier pass can reconcile it using the same predicates.

The wrapper must emit no stdout. Fatal startup messages go to stderr with server name and a reason code only.

- [ ] **Step 5: Add lifecycle, signal, and redaction tests**

Cover:

- child exit code 17 is returned as 17,
- unique session lease is recorded,
- two matching leases remain unknown,
- SIGTERM reaches the test child group,
- wrapper state reaches gone after child exit,
- a canary in child args never appears in state JSON, stdout, stderr, or events.

Use try/finally in every real-process test and kill only PIDs created by that test.

- [ ] **Step 6: Run Task 5 tests**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_supervisor.py tests/codex_mcp_ownership/test_classify.py -q
~~~

Expected: all tests pass and no fixture process remains.

- [ ] **Step 7: Commit Task 5**

~~~bash
rtk git add codex-mcp-ownership/codex_mcp_ownership tests/codex_mcp_ownership
rtk git commit -m "feat(codex): supervise stdio MCP processes"
~~~

---

### Task 6: Codex Lifecycle Hook and Operator CLI

**Files:**
- Create: codex-mcp-ownership/codex_mcp_ownership/hook.py
- Create: codex-mcp-ownership/codex_mcp_ownership/cli.py
- Create: codex-mcp-ownership/entry.py
- Create: tests/codex_mcp_ownership/test_hook_cli.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/state.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/classify.py

**Interfaces:**
- Consumes: StateStore, LinuxProcfs, build_audit(), execute_cleanup(), and run_supervisor().
- Produces: hook.handle_payload(payload, store, procfs, clock, notifier) -> None.
- Produces: hook.SystemdNotifier.request_cleanup() -> bool.
- Produces: cli.build_parser() -> argparse.ArgumentParser and cli.main(argv=None) -> int.
- entry.py calls cli.main() and is the installed stable entry point.

- [ ] **Step 1: Write failing Hook contract tests**

Exercise the official common fields and both events:

~~~python
def test_session_start_creates_active_lease(hook_runtime):
    hook_runtime.handle({
        "session_id": "thr_123",
        "cwd": "/workspace",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5",
    })
    lease = hook_runtime.only_lease()
    assert lease.session_id == "thr_123"
    assert lease.state == "active"


def test_session_end_marks_exact_lease_ended(hook_runtime):
    hook_runtime.start("thr_123")
    hook_runtime.handle({
        "session_id": "thr_123",
        "cwd": "/workspace",
        "hook_event_name": "SessionEnd",
        "reason": "other",
    })
    assert hook_runtime.only_lease().state == "ended"
~~~

Add malformed JSON, non-object JSON, missing fields, control characters, compact refresh, and repeated end. The executable Hook command must return 0 and print nothing in every failure-safe case.

- [ ] **Step 2: Run Hook tests and verify red**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_hook_cli.py -q
~~~

Expected: FAIL because hook.py and cli.py are absent.

- [ ] **Step 3: Implement Hook handling**

SessionStart stores an active lease with exact ancestor identities starting at os.getppid(). SessionEnd updates only the SHA-256-addressed matching lease.

SystemdNotifier calls this fixed command without a shell:

~~~python
[
    "/usr/bin/systemctl",
    "--user",
    "start",
    "--no-block",
    "codex-mcp-ownership-cleanup.service",
]
~~~

If /usr/bin/systemctl is absent or returns nonzero, SessionStart runs one bounded cleanup pass for already-expired prior records. It does not sleep or shorten the 120-second grace. SessionEnd records state and returns within the Hook timeout.

- [ ] **Step 4: Write failing CLI read-only and redaction tests**

Capture filesystem snapshots before and after:

~~~python
def test_audit_json_is_read_only(cli_runner, state_snapshot):
    before = state_snapshot()
    result = cli_runner("audit", "--json")
    after = state_snapshot()
    assert result.returncode == 0
    assert before == after
    assert json.loads(result.stdout)["schema_version"] == 1


def test_cleanup_without_apply_is_read_only(cli_runner, state_snapshot):
    before = state_snapshot()
    result = cli_runner("cleanup")
    assert result.returncode == 0
    assert state_snapshot() == before
~~~

Seed semantic canaries in fake env and args and assert they appear nowhere in human output, JSON output, or stderr.

- [ ] **Step 5: Implement CLI commands and metrics**

The parser has these exact subcommands:

~~~python
subparsers.add_parser("audit").add_argument("--json", action="store_true")
cleanup_parser = subparsers.add_parser("cleanup")
cleanup_parser.add_argument("--apply", action="store_true")
cleanup_parser.add_argument("--force", action="store_true")
cleanup_parser.add_argument("--confirm")
explain_parser = subparsers.add_parser("explain")
explain_parser.add_argument("pid", type=int)
subparsers.add_parser("hook")
supervise_parser = subparsers.add_parser("supervise")
supervise_parser.add_argument("--scope", required=True)
supervise_parser.add_argument("--server", required=True)
supervise_parser.add_argument("command", nargs=argparse.REMAINDER)
~~~

Human and JSON reports include state counts, total revalidated RSS KiB, attempted, terminated, survived, and skipped. audit returns exit 0 for findings; malformed state returns a nonzero diagnostic exit but still sends no signal. cleanup --force without --apply is a usage error.

entry.py contains only the stable dispatch:

~~~python
from codex_mcp_ownership.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
~~~

The supervise dispatcher requires the remainder to start with --, removes that one separator, rejects an empty command, and constructs SupervisorRequest without logging the remainder.

- [ ] **Step 6: Run Task 6 tests**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_hook_cli.py tests/codex_mcp_ownership/test_cleanup.py tests/codex_mcp_ownership/test_supervisor.py -q
rtk python3 -m compileall -q codex-mcp-ownership
~~~

Expected: all focused tests pass and compileall exits 0.

- [ ] **Step 7: Commit Task 6**

~~~bash
rtk git add codex-mcp-ownership tests/codex_mcp_ownership
rtk git commit -m "feat(codex): expose MCP ownership hooks and CLI"
~~~

---

### Task 7: Lossless Codex Config and Hook Transformations

**Files:**
- Create: codex-mcp-ownership/codex_mcp_ownership/config_edit.py
- Create: codex-mcp-ownership/requirements-setup.lock
- Create: tests/codex_mcp_ownership/test_config_edit.py
- Modify: requirements-test.in
- Modify: requirements-test.lock
- Modify: tests/test_pytest_workflow.py

**Interfaces:**
- Consumes: installed entry-point path and selected user/project config bytes.
- Produces: config_edit.ConfigPlan with before_bytes, after_bytes, before_sha256, after_sha256, targets, and redacted_summary().
- Produces: config_edit.JsonPlan with the same private-byte/hash contract for hooks.json.
- Produces: config_edit.ConfigTarget(scope, name, changed_fields, shared_owner).
- Produces: config_edit.plan_codex_config(raw, scope, entry_path, selected_names, shared_names) -> ConfigPlan.
- Produces: config_edit.plan_hooks_json(raw_or_none, hook_command) -> JsonPlan.
- Produces: config_edit.restore_plan(current, transaction) -> bytes or raises ConfigConflict.

- [ ] **Step 1a: Pin the setup-only dependency lock**

Create requirements-setup.lock with the two official PyPI hashes:

~~~text
tomlkit==0.15.1 \
    --hash=sha256:177a05aece5a8ca5266fd3c448abb47b8d352f09d477d3ca8332db4d89b24304 \
    --hash=sha256:e25bbf38843005246210a12982776f27f99cb9be67160e14434d0c0d21ee1e97
~~~

- [ ] **Step 1b: Add tomlkit to the repository test contract**

Add tomlkit==0.15.1 to requirements-test.in and update expected_lock_names in tests/test_pytest_workflow.py to contain tomlkit.

Regenerate the repository lock exactly as its header specifies:

~~~bash
rtk uv pip compile requirements-test.in --output-file requirements-test.lock --generate-hashes --python-version 3.10 --python-platform x86_64-unknown-linux-gnu
~~~

- [ ] **Step 1c: Refresh the active test environment**

Refresh the active test environment from the hashed lock before the first green run:

~~~bash
rtk python3 -m pip install --require-hashes --requirement requirements-test.lock
~~~

- [ ] **Step 2: Write failing lossless-transform tests**

Use a TOML fixture with comments, quoted names, multiline args, env, cwd, timeout, enabled, and an HTTP server. Assert exact preservation of every unrelated byte or parsed field:

~~~python
def test_wraps_only_stdio_command_and_args(sample_toml, entry_path):
    plan = config_edit.plan_codex_config(
        sample_toml,
        scope="user",
        entry_path=entry_path,
        selected_names=None,
        shared_names=frozenset(),
    )
    parsed = tomlkit.parse(plan.after_bytes.decode())
    stdio = parsed["mcp_servers"]["local stdio"]
    assert stdio["command"] == str(entry_path)
    assert list(stdio["args"][:5]) == [
        "supervise", "--scope", "user", "--server", "local stdio",
    ]
    assert parsed["mcp_servers"]["remote"]["url"] == "https://example.test/mcp"
    assert "# keep this comment" in plan.after_bytes.decode()
~~~

Assert env, cwd, startup_timeout_sec, enabled, enabled_tools, and disabled_tools are unchanged.

- [ ] **Step 3: Run config-edit tests and verify red**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_config_edit.py tests/test_pytest_workflow.py -q
~~~

Expected: FAIL because config_edit.py is absent or tomlkit is not yet locked in the active environment.

- [ ] **Step 4a: Add private planning dataclasses**

The planning dataclasses keep raw bytes out of repr:

~~~python
@dataclass(frozen=True)
class ConfigTarget:
    scope: str
    name: str
    changed_fields: tuple[str, ...]
    shared_owner: str | None


@dataclass(frozen=True)
class ConfigPlan:
    before_bytes: bytes = field(repr=False)
    after_bytes: bytes = field(repr=False)
    before_sha256: str
    after_sha256: str
    targets: tuple[ConfigTarget, ...]


@dataclass(frozen=True)
class JsonPlan:
    before_bytes: bytes | None = field(repr=False)
    after_bytes: bytes = field(repr=False)
    before_sha256: str | None
    after_sha256: str
    changed_events: tuple[str, ...]
~~~

- [ ] **Step 4b: Implement lossless stdio wrapping**

Import tomlkit only inside config_edit.py. Validate that command is a nonempty string and args is an array of strings before mutation. The wrapped shape is exact:

~~~python
wrapped_args = [
    "supervise",
    "--scope",
    scope,
    "--server",
    name,
    "--",
    original_command,
    *original_args,
]
server["command"] = str(entry_path)
server["args"] = wrapped_args
~~~

Reject an already wrapped entry with a different installed path, inline URL/command conflicts, duplicate semantic tables, or unsupported value types. Redacted summaries include only scope, server name, and changed field names.

- [ ] **Step 5: Implement additive hooks.json planning**

Generate exact command-hook groups while preserving existing groups:

~~~python
start_group = {
    "matcher": "startup|resume|clear|compact",
    "hooks": [{
        "type": "command",
        "command": hook_command,
        "timeout": 3,
    }],
}
end_group = {
    "hooks": [{
        "type": "command",
        "command": hook_command,
        "timeout": 3,
    }],
}
~~~

Deduplicate only the exact installed hook command. Do not remove or reorder other commands. Reject a top-level non-object, non-list event groups, or hook entries with incompatible shapes.

- [ ] **Step 6: Add idempotency, rollback, and secret-redaction tests**

Prove identical rerun yields after_bytes equal to input bytes. restore_plan() accepts only current bytes whose SHA-256 equals the recorded after hash and returns the exact before bytes. A one-byte concurrent edit raises ConfigConflict.

Seed a credential canary in env and original args. Assert ConfigPlan repr, redacted_summary(), stdout fixtures, and exceptions omit the canary.

- [ ] **Step 7: Run Task 7 tests and dependency contract**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_config_edit.py tests/test_pytest_workflow.py -q
rtk python3 -m pip install --dry-run --require-hashes --requirement codex-mcp-ownership/requirements-setup.lock
~~~

Expected: all tests pass; pip reports a valid fully hashed tomlkit 0.15.1 plan.

- [ ] **Step 8: Commit Task 7**

~~~bash
rtk git add codex-mcp-ownership requirements-test.in requirements-test.lock tests/codex_mcp_ownership/test_config_edit.py tests/test_pytest_workflow.py
rtk git commit -m "feat(codex): plan lossless MCP configuration"
~~~

---

### Task 8: Transactional Opt-In Installer and systemd Scavenger

**Files:**
- Create: codex-mcp-ownership/codex_mcp_ownership/install.py
- Create: codex-mcp-ownership/systemd/codex-mcp-ownership-cleanup.service.template
- Create: codex-mcp-ownership/systemd/codex-mcp-ownership-cleanup.timer.template
- Create: scripts/setup-codex-mcp-ownership.sh
- Create: tests/codex_mcp_ownership/test_install.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/cli.py
- Modify: codex-mcp-ownership/entry.py

**Interfaces:**
- Consumes: ConfigPlan, JsonPlan, StateStore transaction paths, and the package source tree.
- Produces: install.InstallPaths.from_home(home: Path) -> InstallPaths with exact config, hooks, lib, bin, unit, state, and transaction paths.
- Produces: install.InstallRequest(mode, servers, project, shared).
- Produces: install.preflight(request, paths, runner) -> InstallPlan.
- Produces: install.apply(plan, paths, runner) -> InstallResult.
- Produces: install.rollback(paths, runner) -> InstallResult.
- Produces: install.CommandRunner protocol for captured no-shell subprocess calls.
- Produces: shell modes --check, --apply, --rollback, --server NAME, --project PATH, and --shared SCOPE:NAME.

- [ ] **Step 1: Write failing check-mode security tests**

In a temporary HOME, place fake codex and systemctl executables in a validated private PATH. The fake codex returns JSON but logs calls to a fixture file.

Assert:

~~~python
def test_check_is_persistently_read_only(installer_fixture):
    before = installer_fixture.snapshot_home()
    result = installer_fixture.run("--check")
    assert result.returncode == 0
    assert installer_fixture.snapshot_home() == before
    assert "--json" in installer_fixture.codex_calls()


def test_check_redacts_config_values(installer_fixture):
    installer_fixture.write_config_with_canary()
    result = installer_fixture.run("--check")
    assert installer_fixture.canary not in result.stdout + result.stderr
~~~

Also reject a symlink config, hardlinked config, wrong owner fixture where supported, mode 0644, and an untrusted command PATH before any child process runs.

Add a fake Codex feature response with hooks disabled and assert --apply stops before staging runtime or config files. --check reports hooks_disabled without enabling the feature implicitly.

- [ ] **Step 2: Run installer tests and verify red**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_install.py -q
~~~

Expected: FAIL because install.py and setup-codex-mcp-ownership.sh are absent.

- [ ] **Step 3a: Add immutable installer request and result types**

Define immutable install result shapes:

~~~python
@dataclass(frozen=True)
class InstallRequest:
    mode: Literal["check", "apply", "rollback"]
    servers: tuple[str, ...]
    project: Path | None
    shared: tuple[str, ...]


@dataclass(frozen=True)
class InstallPlan:
    request: InstallRequest
    paths: InstallPaths
    config_plans: tuple[ConfigPlan, ...]
    hooks_plan: JsonPlan
    systemd_available: bool
    redacted_targets: tuple[ConfigTarget, ...]


@dataclass(frozen=True)
class InstallResult:
    code: str
    changed: bool
    degraded: bool
    pending_runtime_removal: bool


@dataclass(frozen=True)
class PublishedTarget:
    path: Path
    before_bytes: bytes | None = field(repr=False)
    after_sha256: str
    mode: int


@dataclass(frozen=True)
class TransactionManifest:
    transaction_id: str
    config_plans: tuple[ConfigPlan, ...]
    hooks_plan: JsonPlan
    target_paths: tuple[Path, ...]
    published_targets: tuple[PublishedTarget, ...] = ()
~~~

- [ ] **Step 3b: Resolve and validate stable install paths**

Use these destinations:

~~~text
$HOME/.local/lib/claude-config/codex-mcp-ownership/app
$HOME/.local/lib/claude-config/codex-mcp-ownership/venv
$HOME/.local/bin/codex-mcp-ownership
$HOME/.codex/hooks.json
$HOME/.config/systemd/user/codex-mcp-ownership-cleanup.service
$HOME/.config/systemd/user/codex-mcp-ownership-cleanup.timer
~~~

The generated bin launcher is mode 0500 and runs:

~~~sh
exec "$HOME/.local/lib/claude-config/codex-mcp-ownership/venv/bin/python" \
  -Es "$HOME/.local/lib/claude-config/codex-mcp-ownership/app/entry.py" "$@"
~~~

Before apply, validate the complete original/resolved path chains with the patterns in scripts/lib/private-file.sh. Capture config and hooks bytes, inode identity, and SHA-256.

- [ ] **Step 3c: Implement Codex capability preflight**

CommandRunner queries codex features list and requires the effective hooks row to be enabled before apply. It also queries codex mcp list --json from HOME for user scope or from the explicit trusted project path for project scope. Missing/unknown Hook capability blocks apply; check mode reports hooks_enabled, hooks_disabled, or hooks_unknown without changing config.

~~~python
features = runner.run([str(codex_bin), "features", "list"], cwd=scope_cwd)
servers = runner.run(
    [str(codex_bin), "mcp", "list", "--json"],
    cwd=scope_cwd,
)
if request.mode == "apply" and parse_hooks_state(features.stdout) is not True:
    raise InstallBlocked("hooks_not_enabled")
~~~

- [ ] **Step 4a: Stage the private runtime and management environment**

Create a private staging directory adjacent to the final lib directory. Build the venv there, run pip with --require-hashes against requirements-setup.lock, copy the application tree with deterministic modes, and fsync the staged files. No final path changes in this step.

Use a no-shell subprocess:

~~~python
runner.run([
    str(staged_venv_python),
    "-m",
    "pip",
    "install",
    "--require-hashes",
    "--requirement",
    str(requirements_lock),
])
~~~

- [ ] **Step 4b: Stage Hook, config, and systemd payloads**

Build JsonPlan and ConfigPlan from the preflight snapshots, render units with the exact final bin path, and write only private staged files. Store one private transaction manifest containing before/after hashes, modes, and explicit target paths; raw snapshots remain repr-hidden.

~~~python
transaction = TransactionManifest(
    transaction_id=secrets.token_hex(16),
    config_plans=config_plans,
    hooks_plan=hooks_plan,
    target_paths=tuple(plan.paths.mutable_targets()),
)
stage_transaction(transaction, staging_root, mode=0o600)
~~~

- [ ] **Step 4c: Publish runtime, Hook, and config under CAS**

Take the kernel lock, reopen target directories by dirfd, repeat inode/hash checks, publish the runtime, then hooks, then config with same-directory temp files, file fsync, atomic rename, and directory fsync. Release the lock before invoking Codex or systemd.

~~~python
with installer_lock(plan.paths.lock, timeout=2.0):
    verify_expected_snapshots(plan)
    publish_runtime(plan)
    publish_json_plan(plan.hooks_plan, plan.paths.hooks)
    for config_plan in plan.config_plans:
        publish_config_plan(config_plan)
~~~

- [ ] **Step 4d: Validate semantics and activate the timer**

Capture effective Codex MCP JSON and compare server type, wrapper command, preserved fields, and selected targets without printing values. Write units, run systemctl --user daemon-reload, and enable/start the timer as the final action.

~~~python
effective = runner.run(
    [str(codex_bin), "mcp", "list", "--json"],
    cwd=scope_cwd,
)
validate_effective_servers(effective.stdout, plan.redacted_targets)
runner.run(["/usr/bin/systemctl", "--user", "daemon-reload"])
runner.run([
    "/usr/bin/systemctl", "--user", "enable", "--now",
    "codex-mcp-ownership-cleanup.timer",
])
~~~

- [ ] **Step 4e: Implement transactional compensation**

Every stage records enough exact preimage data for rollback. If a failure occurs after publication, restore only files whose current hash still equals this transaction's expected hash. Report APPLIED_UNCONFIRMED when directory fsync fails after rename, matching the repository's existing MCP sync safety language.

~~~python
for published in reversed(transaction.published_targets):
    if sha256_path(published.path) == published.after_sha256:
        restore_atomic(published.path, published.before_bytes, published.mode)
    else:
        conflicts.append(published.path)
~~~

- [ ] **Step 5: Add failure-injection and conflict tests**

Parameterize failure after runtime publish, hooks publish, config publish, semantic validation, daemon-reload, and timer enable. Each test asserts exact original bytes and modes are restored.

Inject a concurrent one-byte config edit between plan and commit and assert:

~~~python
assert result.code == "CONFIG_CHANGED"
assert config_path.read_bytes() == user_edit
assert not fixture.signal_log.exists()
~~~

- [ ] **Step 6: Implement systemd units and degraded mode**

The service is Type=oneshot, UMask=0077, and invokes only:

~~~text
ExecStart=__BIN_PATH__ cleanup --apply
~~~

The timer contains OnBootSec=120s, OnUnitActiveSec=60s, AccuracySec=10s, and WantedBy=timers.target. It never includes --force.

When systemctl --user is unavailable, return success with degraded=true only after config and Hook validation; do not install a misleading enabled marker. SessionStart opportunistic cleanup remains the fallback.

- [ ] **Step 7: Implement rollback with active-process deferral**

Rollback first restores future config only when hashes match. If audit reports any active managed wrapper, keep Hook, timer, venv, and app files and return pending_runtime_removal=true. On a later rollback with zero live managed wrappers, disable the timer, remove only exact managed Hook groups and units, and remove the exact installed runtime tree.

Never recursively remove an unresolved path. Resolve and validate the explicit stable runtime directory before removal.

- [ ] **Step 8: Implement the shell entry safely**

The shell script uses set -euo pipefail, disables xtrace if inherited, sources scripts/lib/private-file.sh, validates PATH before command lookup, and invokes no eval. --check must not create a venv or cache. --apply bootstraps the private venv with uv when a validated uv exists, otherwise /usr/bin/python3 -m venv.

Add bash -O xpg_echo and PATH-canary tests so backslashes and attacker-controlled executables cannot change behavior.

- [ ] **Step 9: Run Task 8 tests and shell validation**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_install.py tests/codex_mcp_ownership/test_config_edit.py -q
rtk bash -n scripts/setup-codex-mcp-ownership.sh
rtk shellcheck -x -s bash -S error scripts/setup-codex-mcp-ownership.sh
~~~

Expected: all tests pass; bash and shellcheck exit 0.

- [ ] **Step 10: Commit Task 8**

~~~bash
rtk git add codex-mcp-ownership scripts/setup-codex-mcp-ownership.sh tests/codex_mcp_ownership
rtk git commit -m "feat(codex): install MCP ownership tracking safely"
~~~

---

### Task 9: End-to-End Convergence and Host-Safety Gates

**Files:**
- Create: tests/codex_mcp_ownership/fixtures/owner_harness.py
- Create: tests/codex_mcp_ownership/test_e2e.py
- Modify: tests/codex_mcp_ownership/helpers.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/supervisor.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/cleanup.py
- Modify: codex-mcp-ownership/codex_mcp_ownership/cli.py

**Interfaces:**
- Consumes: the installed-equivalent entry command, real LinuxProcfs, StateStore, Hook handler, supervisor, and cleanup service.
- Produces: controlled evidence that normal and forced owner exits converge without false signals.
- Produces: a completion-gate JSON field ownership_coverage with managed, owned_or_shared, and unknown counts.

- [ ] **Step 1: Write a failing normal-exit integration test**

owner_harness.py creates a SessionStart lease for its own exact host identity, spawns the supervisor plus fake MCP, writes a ready marker, then closes the MCP stdin normally.

The test asserts:

~~~python
def test_normal_owner_exit_converges_to_zero_managed_processes(e2e):
    result = e2e.run_normal_exit()
    assert result.wrapper_returncode == 0
    assert result.after.process_count == 0
    assert result.after.rss_kib == 0
    assert result.signals_sent == ()
~~~

- [ ] **Step 2: Run the normal-exit test and verify red**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_e2e.py::test_normal_owner_exit_converges_to_zero_managed_processes -q
~~~

Expected: FAIL because owner_harness.py and the e2e orchestration are absent.

- [ ] **Step 3: Implement normal-exit convergence**

When child exit is observed, supervisor records exit code, verifies the child identity is gone, and removes only the corresponding live-process record while retaining a bounded event entry. build_audit must count only live exact identities as process_count and RSS.

- [ ] **Step 4: Write a failing forced-owner-exit integration test**

The harness starts a TERM-responsive fake MCP, then the test sends SIGKILL only to the harness PID created by the fixture. Use an injected clock so 120 seconds advances without sleeping.

Assert the exact sequence:

~~~python
assert first.states == ("exiting",)
assert first.signals_sent == ()
clock.advance(119.9)
assert second.states == ("exiting",)
assert second.signals_sent == ()
clock.advance(0.1)
assert third.states == ("orphan",)
assert third.signals_sent == (signal.SIGTERM,)
assert final.process_count == 0
~~~

- [ ] **Step 5: Add hostile convergence fixtures**

Add active, shared, slow-exit, PID-reuse, same-cwd-two-session, and TERM-ignoring fixtures. For each, assert exact signal logs:

~~~python
@pytest.mark.parametrize(
    "scenario",
    ["active", "shared", "slow_exit", "pid_reuse", "ambiguous"],
)
def test_hostile_scenarios_never_receive_automatic_signal(e2e, scenario):
    result = e2e.run_hostile(scenario)
    assert result.signals_sent == ()
~~~

The TERM-ignoring fixture receives TERM once, becomes stubborn after 10 seconds, and never receives automatic SIGKILL.

- [ ] **Step 6: Add ownership coverage and RSS assertions**

audit JSON includes:

~~~json
{
  "ownership_coverage": {
    "managed": 3,
    "owned_or_shared": 3,
    "unknown": 0
  }
}
~~~

The controlled positive smoke fixture must be 100 percent owned/shared. A fixture with unknown remains safe but fails the completion-gate predicate. Compare reported RSS against live VmRSS sums before and after cleanup.

- [ ] **Step 7: Run Task 9 tests and process-leak check**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_e2e.py tests/codex_mcp_ownership/test_supervisor.py tests/codex_mcp_ownership/test_cleanup.py -q
~~~

Expected: all tests pass. The fixture teardown checks every recorded fake_mcp.py and owner_harness.py PID with exact start ticks and proves none remains.

- [ ] **Step 8: Commit Task 9**

~~~bash
rtk git add codex-mcp-ownership tests/codex_mcp_ownership
rtk git commit -m "test(codex): prove MCP cleanup convergence"
~~~

---

### Task 10: Operations Documentation and Full Verification

**Files:**
- Create: codex-mcp-ownership/README.md
- Modify: README.md
- Modify: tests/codex_mcp_ownership/test_install.py
- Modify: tests/codex_mcp_ownership/test_hook_cli.py

**Interfaces:**
- Consumes: all implemented command and setup surfaces.
- Produces: operator instructions for check, apply, new-session activation, audit, explain, dry-run cleanup, apply cleanup, force confirmation, timer diagnosis, degraded mode, conflict recovery, corrupt-state recovery, and rollback.
- Produces: final repository-wide verification evidence.

- [ ] **Step 1: Write failing documentation-contract tests**

Assert the component README contains exact safe commands and warnings:

~~~python
def test_operations_readme_documents_safe_flow():
    text = README_PATH.read_text(encoding="utf-8")
    required = {
        "setup-codex-mcp-ownership.sh --check",
        "setup-codex-mcp-ownership.sh --apply",
        "codex-mcp-ownership audit --json",
        "codex-mcp-ownership cleanup",
        "codex-mcp-ownership cleanup --apply",
        "codex-mcp-ownership explain",
        "setup-codex-mcp-ownership.sh --rollback",
        "unknown",
        "SIGKILL",
        "systemctl --user",
    }
    missing = {item for item in required if item not in text}
    assert not missing
~~~

Add a main README assertion that the top-level inventory links codex-mcp-ownership/README.md and calls setup opt-in.

- [ ] **Step 2: Run documentation tests and verify red**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_install.py tests/codex_mcp_ownership/test_hook_cli.py -q
~~~

Expected: FAIL because the operations README and top-level section are absent.

- [ ] **Step 3: Write the operations guide**

Document this exact sequence:

~~~bash
rtk bash scripts/setup-codex-mcp-ownership.sh --check
rtk bash scripts/setup-codex-mcp-ownership.sh --apply
rtk codex-mcp-ownership audit --json
rtk codex-mcp-ownership cleanup
rtk codex-mcp-ownership cleanup --apply
rtk codex-mcp-ownership explain 12345
rtk bash scripts/setup-codex-mcp-ownership.sh --rollback
~~~

Explain that apply affects new Codex sessions only, timer cleanup is automatic only after opt-in apply, unknown/shared are never auto-killed, force needs current evidence, and project config requires --project PATH.

Add diagnosis for systemctl --user status codex-mcp-ownership-cleanup.timer, degraded SessionStart behavior, CONFIG_CHANGED, APPLIED_UNCONFIRMED, unsafe state paths, corrupt ledger, and pending runtime removal.

- [ ] **Step 4: Add main README inventory and privacy statement**

Add one concise table row and one usage section. State that the local ownership ledger is unrelated to Project Control Registry and does not create a persistent writer Claim.

- [ ] **Step 5: Run the focused documentation and security tests**

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership -q
rtk python3 -m pytest tests/test_secure_setup_scripts.py tests/test_pytest_workflow.py -q
~~~

Expected: all focused tests pass.

- [ ] **Step 6: Run full repository verification**

~~~bash
rtk python3 -m pytest -q
rtk bash -n scripts/setup-codex-mcp-ownership.sh
rtk shellcheck -x -s bash -S error scripts/setup-codex-mcp-ownership.sh
rtk python3 -m compileall -q codex-mcp-ownership
rtk git diff --check
rtk git status --short
~~~

Expected:

- pytest reports zero failures,
- bash, shellcheck, compileall, and diff check exit 0,
- git status lists only the documentation changes for this task before commit,
- no live-host config, Hook, systemd unit, or process was mutated by tests.

- [ ] **Step 7: Perform the controlled opt-in smoke gate**

Run the disposable-HOME installer smoke through the fixture that supplies private config, fake codex, and fake systemctl:

~~~bash
rtk python3 -m pytest tests/codex_mcp_ownership/test_install.py::test_disposable_home_apply_audit_and_rollback -q
~~~

Stop and request separate user authorization before any live-home --apply. After an authorized live apply, start one new Codex session and require ownership_coverage.unknown to equal 0 for every managed stdio MCP before considering issue #67 complete. If any managed process is unknown, preserve diagnostic-only safety and keep the issue open.

- [ ] **Step 8: Commit Task 10**

~~~bash
rtk git add README.md codex-mcp-ownership/README.md tests/codex_mcp_ownership
rtk git commit -m "docs(codex): document MCP ownership operations"
~~~

- [ ] **Step 9: Request review and prepare lifecycle evidence**

Run the repository's code-review workflow, address every must-fix finding with fresh focused and full verification, then record:

- commit range,
- full pytest result,
- shellcheck result,
- controlled ownership coverage,
- normal and forced convergence counts/RSS,
- rollback test result,
- remaining unknown or degraded conditions.

Before any push or PR operation, run the active Project Control task assert-owner workflow immediately beforehand.
