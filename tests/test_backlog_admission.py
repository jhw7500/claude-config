import copy
import hashlib
import json
import multiprocessing
import os
import signal
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.backlog_checkpoint.admission import (
    acquire_admission,
    reconcile_admission,
    release_admission,
)
from scripts.backlog_checkpoint import admission
from scripts.backlog_checkpoint.codec import (
    CheckpointError,
    CheckpointIndeterminateError,
    canonical_bytes,
    decode_snapshot,
    sha256_bytes,
)
from scripts.backlog_checkpoint.storage import read_snapshot, write_evidence, write_snapshot
from scripts.backlog_checkpoint.git_evidence import bind_commit, collect_sources


RUN_ID = "0123456789abcdef0123456789abcdef"
NEXT_RUN_ID = "fedcba9876543210fedcba9876543210"
ZERO40 = "0" * 40
ONE40 = "1" * 40
ZERO64 = "0" * 64


def _root_key(worktree: Path) -> str:
    return hashlib.sha256(os.path.realpath(worktree).encode()).hexdigest()


def _owner(worktree: Path, *, claim: str = "claim-31", task_owner: str = "owner-a") -> dict:
    return {
        "issue_url": "https://github.com/jhw7500/claude-config/issues/31",
        "task_id": "task-31",
        "claim_id": claim,
        "task_owner": task_owner,
        "branch": "task/issue-31",
        "worktree_root": str(worktree),
        "session_fingerprint": ZERO64,
    }


def _snapshot(
    worktree: Path,
    run_id: str,
    owner: dict,
    evidence: dict,
    *,
    terminal: bool = False,
) -> dict:
    phase = "READY_FOR_TRIBUNAL" if terminal else "IMPLEMENT"
    return {
        "schema": 1,
        "run_id": run_id,
        "generation": 0,
        "phase": phase,
        "phase_status": "verified" if terminal else "started",
        "attempt": 0,
        "next_action": "STOP" if terminal else "INDEPENDENT_REVIEW",
        "blockers": [],
        "supervisor_identity": {
            "runtime": "codex",
            "origin_adapter": "codex",
            "host": "fixture-host",
            "session_fingerprint": owner["session_fingerprint"],
        },
        "binding": {
            "issue_url": owner["issue_url"],
            "task_id": owner["task_id"],
            "claim_id": owner["claim_id"],
            "repo_root": str(worktree.parent),
            "worktree_root": str(worktree),
        },
        "git": {
            "branch": owner["branch"],
            "base_head": ZERO40,
            "observed_head": ONE40,
            "source_count": 1,
            "path_set_digest": ZERO64,
            "change_digest": ZERO64,
            "commit_parent": None,
            "result_commit": None,
        },
        "child": {
            "child_id": None,
            "phase": phase,
            "attempt": 0,
            "observed_status": "quiescent" if terminal else "running",
        },
        "evidence": evidence,
    }


def _write_checkpoint(
    state_root: Path,
    worktree: Path,
    run_id: str,
    owner: dict,
    manifest: dict,
    *,
    terminal: bool = False,
    name: str | None = None,
) -> tuple[Path, dict, bytes]:
    reference = write_evidence(state_root, run_id, manifest)
    path = worktree / (name or f"HANDOFF.backlog-{run_id}.md")
    stored = write_snapshot(path, _snapshot(worktree, run_id, owner, reference, terminal=terminal), None)
    data = path.read_bytes()
    return path, stored, data


def _record(worktree: Path, run_id: str, owner: dict, path: Path, snapshot: dict, data: bytes) -> dict:
    return {
        "schema": 1,
        "run_id": run_id,
        "root_key": _root_key(worktree),
        "checkpoint_path": str(path),
        "owner": owner,
    }


@pytest.fixture
def admission_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    owner = _owner(worktree)
    path, snapshot, data = _write_checkpoint(state_root, worktree, RUN_ID, owner, {})
    return state_root, worktree, _record(worktree, RUN_ID, owner, path, snapshot, data)


def _active_path(state_root: Path, worktree: Path) -> Path:
    return state_root / "backlog-drain" / "admissions" / f"{_root_key(worktree)}.json"


def _proof(expected: dict, successor: dict, old_data: bytes) -> dict:
    return {
        "schema": 1,
        "recovery_result": "succeeded",
        "prior": {
            "admission_sha256": sha256_bytes(canonical_bytes(expected)),
            "snapshot_sha256": sha256_bytes(old_data),
            "snapshot_generation": decode_snapshot(old_data)["generation"],
        },
        "successor": {
            "snapshot_sha256": sha256_bytes(Path(successor["checkpoint_path"]).read_bytes()),
            "snapshot_generation": read_snapshot(successor["checkpoint_path"])["generation"],
        },
        "quiescence": {
            "children": "quiescent",
            "tools": "quiescent",
            "writers": "quiescent",
        },
        "git": {
            "branch": successor["owner"]["branch"],
            "worktree_root": successor["owner"]["worktree_root"],
            "observed_head": ONE40,
            "change_digest": ZERO64,
        },
    }


def _make_successor(state_root: Path, worktree: Path, expected: dict) -> tuple[dict, dict]:
    old_data = Path(expected["checkpoint_path"]).read_bytes()
    owner = _owner(worktree, claim="claim-31-successor", task_owner="owner-b")
    placeholder_prior = {
        "prior_run_id": expected["run_id"],
        "prior_snapshot_sha256": sha256_bytes(old_data),
        "prior_snapshot_generation": decode_snapshot(old_data)["generation"],
    }
    path, snapshot, data = _write_checkpoint(
        state_root,
        worktree,
        NEXT_RUN_ID,
        owner,
        {"admission_successor": placeholder_prior},
    )
    successor = _record(worktree, NEXT_RUN_ID, owner, path, snapshot, data)
    return successor, _proof(expected, successor, old_data)


def test_acquire_is_exclusive_and_creates_private_regular_state(admission_fixture) -> None:
    state_root, worktree, record = admission_fixture

    assert acquire_admission(state_root, worktree, record) == record
    active = _active_path(state_root, worktree)
    assert active.read_bytes() == canonical_bytes(record)
    assert stat.S_ISREG(active.stat().st_mode)
    assert stat.S_IMODE(active.stat().st_mode) == 0o600
    assert stat.S_IMODE(active.parent.stat().st_mode) == 0o700

    with pytest.raises(CheckpointError, match="active"):
        acquire_admission(state_root, worktree, record)


def test_fresh_admission_hierarchy_syncs_child_metadata_before_parent_links(
    admission_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, worktree, record = admission_fixture
    events: list[str] = []
    real_fsync = os.fsync

    def record_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append(f"fsync:{Path(os.readlink(f'/proc/self/fd/{fd}')).name}")
        real_fsync(fd)

    monkeypatch.setattr(admission.os, "fsync", record_fsync)

    acquire_admission(state_root, worktree, record)

    assert events[:8] == [
        "fsync:backlog-drain",
        f"fsync:{state_root.name}",
        "fsync:admissions",
        "fsync:backlog-drain",
        "fsync:admission-archive",
        "fsync:backlog-drain",
        "fsync:admission-snapshots",
        "fsync:backlog-drain",
    ]


def test_fresh_admission_hierarchy_parent_sync_failure_prevents_active_record(
    admission_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, worktree, record = admission_fixture
    backlog = state_root / "backlog-drain"
    admissions = backlog / "admissions"
    real_fsync = os.fsync
    admissions_synced = False

    def fail_backlog_link_sync(fd: int) -> None:
        nonlocal admissions_synced
        target = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if target == admissions:
            admissions_synced = True
        if admissions_synced and target == backlog:
            raise OSError("simulated admission hierarchy parent sync failure")
        real_fsync(fd)

    monkeypatch.setattr(admission.os, "fsync", fail_backlog_link_sync)

    with pytest.raises(CheckpointError, match="directory"):
        acquire_admission(state_root, worktree, record)

    assert not _active_path(state_root, worktree).exists()


def test_acquire_rejects_worktree_alias_without_canonicalizing_scope(admission_fixture) -> None:
    state_root, worktree, record = admission_fixture
    alias = worktree.parent / "alias"
    alias.symlink_to(worktree, target_is_directory=True)
    aliased = copy.deepcopy(record)
    aliased["owner"]["worktree_root"] = str(alias)

    with pytest.raises(CheckpointError, match="canonical"):
        acquire_admission(state_root, alias, aliased)

    assert not _active_path(state_root, worktree).exists()


def test_acquire_rejects_symlinked_state_root_ancestor(admission_fixture) -> None:
    # Wrong branch: accepting admission storage after following an ancestor alias.
    state_root, worktree, record = admission_fixture
    alias = state_root.parent / "state-alias"
    alias.symlink_to(state_root.parent, target_is_directory=True)
    aliased_root = alias / state_root.name

    with pytest.raises(CheckpointError):
        acquire_admission(aliased_root, worktree, record)

    assert not _active_path(state_root, worktree).exists()


def test_acquire_rejects_repointed_state_root_alias_between_invocations(
    admission_fixture, tmp_path: Path
) -> None:
    # Wrong branch: treating a repointed ancestor alias as either physical state root.
    state_root, worktree, record = admission_fixture
    other_parent = tmp_path / "other"
    other_parent.mkdir(mode=0o700)
    other_root = other_parent / state_root.name
    other_root.mkdir(mode=0o700)
    alias = tmp_path / "state-parent-alias"
    alias.symlink_to(state_root.parent, target_is_directory=True)

    with pytest.raises(CheckpointError):
        acquire_admission(alias / state_root.name, worktree, record)
    alias.unlink()
    alias.symlink_to(other_parent, target_is_directory=True)
    with pytest.raises(CheckpointError):
        acquire_admission(alias / state_root.name, worktree, record)

    assert not _active_path(state_root, worktree).exists()
    assert not _active_path(other_root, worktree).exists()


@pytest.mark.parametrize("spelling", ["dot", "repeated", "trailing"])
def test_acquire_rejects_nonexact_lexical_worktree_spelling(
    admission_fixture, spelling: str
) -> None:
    state_root, worktree, record = admission_fixture
    if spelling == "dot":
        supplied = f"{worktree.parent}/./{worktree.name}"
    elif spelling == "repeated":
        supplied = f"{worktree.parent}//{worktree.name}"
    else:
        supplied = f"{worktree}/"

    with pytest.raises(CheckpointError, match="exact|canonical"):
        acquire_admission(state_root, supplied, record)

    assert not _active_path(state_root, worktree).exists()


def test_acquire_rejects_unicode_control_in_observation(admission_fixture) -> None:
    state_root, worktree, record = admission_fixture
    malformed = copy.deepcopy(record)
    malformed["owner"]["task_owner"] = "owner\u0085hidden"

    with pytest.raises(CheckpointError, match="unsafe"):
        acquire_admission(state_root, worktree, malformed)

    assert not _active_path(state_root, worktree).exists()


def test_acquire_rejects_symlink_at_active_record(admission_fixture) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    active = _active_path(state_root, worktree)
    target = active.parent / "target.json"
    target.write_bytes(active.read_bytes())
    target.chmod(0o600)
    active.unlink()
    active.symlink_to(target)

    with pytest.raises(CheckpointError):
        acquire_admission(state_root, worktree, record)


def test_release_rejects_stale_or_foreign_record_without_removing_active(admission_fixture) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    foreign = copy.deepcopy(record)
    foreign["owner"]["task_owner"] = "foreign-owner"

    with pytest.raises(CheckpointError, match="stale"):
        release_admission(state_root, worktree, foreign)

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)


def test_release_requires_terminal_snapshot_and_explicit_quiescence(admission_fixture) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)

    with pytest.raises(CheckpointError, match="terminal"):
        release_admission(state_root, worktree, record)

    assert _active_path(state_root, worktree).exists()


@pytest.fixture
def terminal_admission_fixture(tmp_path: Path) -> tuple:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def git(*args: str) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=worktree, env=env
        ).decode().strip()

    git("init", "-q", "-b", "task/issue-31")
    git("config", "user.name", "Fixture User")
    git("config", "user.email", "fixture@example.invalid")
    source = worktree / "source.py"
    source.write_text("before\n")
    git("add", "source.py")
    git("commit", "-qm", "base")
    source.write_text("reviewed\n")
    manifest = collect_sources(str(worktree), ["source.py"])
    git("add", "source.py")
    git("commit", "-qm", "reviewed")
    committed = git("rev-parse", "HEAD")
    binding = bind_commit(str(worktree), manifest["parent_head"], manifest, committed)
    owner = _owner(worktree)
    snapshot = _snapshot(worktree, RUN_ID, owner, {}, terminal=True)
    snapshot["git"].update(
        binding, base_head=manifest["parent_head"], observed_head=committed
    )
    evidence = {
        "admission_release": {
            "schema": 1, "run_id": RUN_ID, "task_owner": owner["task_owner"],
            "session_fingerprint": owner["session_fingerprint"],
            "snapshot_generation": 0, "tools_quiescent": True, "writers_quiescent": True,
        },
        "source_manifest": manifest,
        "commit_binding": binding,
    }
    return state_root, worktree, owner, snapshot, evidence, git


def _admit_terminal(fixture: tuple) -> tuple[Path, Path, dict]:
    state_root, worktree, owner, snapshot, evidence, _ = fixture
    snapshot["evidence"] = write_evidence(state_root, RUN_ID, evidence)
    path = worktree / f"HANDOFF.backlog-{RUN_ID}.md"
    stored = write_snapshot(path, snapshot, None)
    record = _record(worktree, RUN_ID, owner, path, stored, path.read_bytes())
    acquire_admission(state_root, worktree, record)
    return state_root, worktree, record


def test_release_accepts_freshly_verified_commit_binding(terminal_admission_fixture) -> None:
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)

    release_admission(state_root, worktree, record)

    assert not _active_path(state_root, worktree).exists()


def _release_intent_path(state_root: Path, worktree: Path) -> Path:
    return state_root / "backlog-drain" / "admissions" / f"{_root_key(worktree)}.release"


def test_release_cleanup_archives_checkpoint_and_allows_new_bootstrap(
    terminal_admission_fixture,
) -> None:
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)
    checkpoint = Path(record["checkpoint_path"])
    old_bytes = checkpoint.read_bytes()

    release_admission(state_root, worktree, record)

    assert not checkpoint.exists()
    assert not _active_path(state_root, worktree).exists()
    assert not _release_intent_path(state_root, worktree).exists()
    archived = state_root / "backlog-drain" / "admission-snapshots" / (
        f"{record['root_key']}-{sha256_bytes(old_bytes)}.md"
    )
    admission_copy = state_root / "backlog-drain" / "admission-archive" / (
        f"{record['root_key']}-{sha256_bytes(canonical_bytes(record))}.json"
    )
    assert archived.read_bytes() == old_bytes
    assert admission_copy.read_bytes() == canonical_bytes(record)
    for path in (archived, admission_copy):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_uid == os.getuid()
    assert collect_sources(str(worktree), [])["count"] == 0


@pytest.mark.parametrize("boundary", ["intent_file", "intent_directory"])
def test_release_cleanup_intent_failure_preserves_old_state(
    terminal_admission_fixture, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)
    checkpoint = Path(record["checkpoint_path"])
    old_bytes = checkpoint.read_bytes()
    intent = _release_intent_path(state_root, worktree)
    real_fsync = admission.os.fsync

    def fail_intent_sync(fd: int) -> None:
        target = os.readlink(f"/proc/self/fd/{fd}")
        fail_file = boundary == "intent_file" and target == str(intent)
        fail_directory = boundary == "intent_directory" and target == str(intent.parent) and intent.exists()
        if fail_file or fail_directory:
            raise OSError("fixture release intent fsync failure")
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(admission.os, "fsync", fail_intent_sync)
        with pytest.raises(CheckpointError, match="release intent|indeterminate|PAUSED"):
            release_admission(state_root, worktree, record)

    assert checkpoint.read_bytes() == old_bytes
    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)
    assert intent.is_file()
    with pytest.raises(CheckpointError, match="PAUSED"):
        release_admission(state_root, worktree, record)
    successor, proof = _make_successor(state_root, worktree, record)
    with pytest.raises(CheckpointError, match="PAUSED"):
        reconcile_admission(state_root, worktree, record, successor, proof)


@pytest.mark.parametrize("boundary", ["checkpoint", "active"])
def test_release_cleanup_unlink_ambiguity_blocks_new_admission(
    terminal_admission_fixture, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)
    checkpoint = Path(record["checkpoint_path"])
    old_bytes = checkpoint.read_bytes()
    active = _active_path(state_root, worktree)
    target = checkpoint.name if boundary == "checkpoint" else active.name
    real_unlink = admission.os.unlink

    def unlink_then_fail(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if path == target:
            raise OSError("fixture release unlink result is uncertain")
        return result

    os.supports_dir_fd.add(unlink_then_fail)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(admission.os, "unlink", unlink_then_fail)
            with pytest.raises(CheckpointIndeterminateError, match="retirement|release"):
                release_admission(state_root, worktree, record)
    finally:
        os.supports_dir_fd.discard(unlink_then_fail)

    archived = state_root / "backlog-drain" / "admission-snapshots" / (
        f"{record['root_key']}-{sha256_bytes(old_bytes)}.md"
    )
    assert archived.read_bytes() == old_bytes
    assert not checkpoint.exists()
    assert active.exists() is (boundary == "checkpoint")
    assert _release_intent_path(state_root, worktree).is_file()
    with pytest.raises(CheckpointError, match="PAUSED"):
        acquire_admission(state_root, worktree, record)


def test_release_cleanup_preserves_unrelated_handoff(
    terminal_admission_fixture,
) -> None:
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)
    checkpoint = Path(record["checkpoint_path"])
    original = checkpoint.read_bytes()
    unrelated = worktree / "HANDOFF.backlog-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.md"
    unrelated.write_bytes(b"unrelated\n")

    with pytest.raises(CheckpointError, match="outside the source allowlist"):
        release_admission(state_root, worktree, record)

    assert unrelated.read_bytes() == b"unrelated\n"
    assert checkpoint.read_bytes() == original
    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)
    assert not _release_intent_path(state_root, worktree).exists()


def test_release_cleanup_rechecks_checkpoint_after_intent(
    terminal_admission_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)
    checkpoint = Path(record["checkpoint_path"])
    real_exclusive = admission._exclusive_active
    changed = b""

    def prepare_then_change(directory_fd: int, name: str, data: bytes) -> None:
        nonlocal changed
        real_exclusive(directory_fd, name, data)
        if name.endswith(".release"):
            current = read_snapshot(checkpoint)
            current["attempt"] = 1
            current["child"]["attempt"] = 1
            write_snapshot(checkpoint, current, expected_generation=0)
            changed = checkpoint.read_bytes()

    monkeypatch.setattr(admission, "_exclusive_active", prepare_then_change)
    with pytest.raises(CheckpointError, match="changed before retirement"):
        release_admission(state_root, worktree, record)

    assert changed and checkpoint.read_bytes() == changed
    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)
    assert _release_intent_path(state_root, worktree).is_file()


def _crash_release(state_root: str, worktree: str, record: dict, boundary: str) -> None:
    real_unlink = admission.os.unlink
    target = Path(record["checkpoint_path"]).name if boundary == "checkpoint" else f"{record['root_key']}.json"

    def stop_after_unlink(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if path == target:
            os.kill(os.getpid(), signal.SIGSTOP)
        return result

    admission.os.unlink = stop_after_unlink
    os.supports_dir_fd.add(stop_after_unlink)
    release_admission(state_root, worktree, record)


@pytest.mark.parametrize("boundary", ["checkpoint", "active"])
def test_release_cleanup_process_death_keeps_pause_intent(
    terminal_admission_fixture, boundary: str,
) -> None:
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)
    checkpoint = Path(record["checkpoint_path"])
    old_bytes = checkpoint.read_bytes()
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_crash_release, args=(str(state_root), str(worktree), record, boundary))
    process.start()
    _kill_owned_stopped_child(process)

    archived = state_root / "backlog-drain" / "admission-snapshots" / (
        f"{record['root_key']}-{sha256_bytes(old_bytes)}.md"
    )
    assert archived.read_bytes() == old_bytes
    assert not checkpoint.exists()
    assert _active_path(state_root, worktree).exists() is (boundary == "checkpoint")
    assert _release_intent_path(state_root, worktree).is_file()
    with pytest.raises(CheckpointError, match="PAUSED"):
        acquire_admission(state_root, worktree, record)


def test_release_cleanup_final_sync_error_is_indeterminate(
    terminal_admission_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)
    intent = _release_intent_path(state_root, worktree)
    checkpoint = Path(record["checkpoint_path"])
    real_unlink = admission.os.unlink
    real_fsync = admission.os.fsync
    intent_removed = False

    def mark_intent_removal(path, *args, **kwargs):
        nonlocal intent_removed
        result = real_unlink(path, *args, **kwargs)
        if path == intent.name:
            intent_removed = True
        return result

    def fail_after_intent_removal(fd: int) -> None:
        if intent_removed and os.readlink(f"/proc/self/fd/{fd}") == str(intent.parent):
            raise OSError("fixture final release fsync failure")
        real_fsync(fd)

    os.supports_dir_fd.add(mark_intent_removal)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(admission.os, "unlink", mark_intent_removal)
            patch.setattr(admission.os, "fsync", fail_after_intent_removal)
            with pytest.raises(CheckpointIndeterminateError, match="release"):
                release_admission(state_root, worktree, record)
    finally:
        os.supports_dir_fd.discard(mark_intent_removal)
    assert not checkpoint.exists()
    assert not _active_path(state_root, worktree).exists()


def test_release_reads_evidence_from_retained_state_root(
    terminal_admission_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Wrong branch: reopening release evidence through a replaced state-root pathname.
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)
    retained_root = state_root.with_name("retained-state")
    real_locked_checkpoint = admission._locked_checkpoint
    repointed = False

    @contextmanager
    def repoint_before_checkpoint_yield(current_record):
        nonlocal repointed
        with real_locked_checkpoint(current_record) as value:
            if not repointed:
                repointed = True
                state_root.rename(retained_root)
                state_root.mkdir(mode=0o700)
            yield value

    monkeypatch.setattr(admission, "_locked_checkpoint", repoint_before_checkpoint_yield)

    release_admission(state_root, worktree, record)

    assert repointed is True
    assert not _active_path(retained_root, worktree).exists()
    assert not (state_root / "backlog-drain").exists()


def test_commit_observations_can_advance_in_the_same_checkpoint_before_release(
    terminal_admission_fixture,
) -> None:
    state_root, worktree, owner, terminal, evidence, _ = terminal_admission_fixture
    path = worktree / f"HANDOFF.backlog-{RUN_ID}.md"
    started = copy.deepcopy(terminal)
    started.update(phase="COMMIT", phase_status="started", next_action="READY_FOR_TRIBUNAL")
    started["git"].update(commit_parent=None, result_commit=None)
    started["child"]["phase"] = "COMMIT"
    started["evidence"] = write_evidence(state_root, RUN_ID, {"source_manifest": evidence["source_manifest"]})
    stored = write_snapshot(path, started, None)
    record = _record(worktree, RUN_ID, owner, path, stored, path.read_bytes())
    acquire_admission(state_root, worktree, record)
    evidence["admission_release"]["snapshot_generation"] = 1
    terminal["evidence"] = write_evidence(state_root, RUN_ID, evidence)

    assert write_snapshot(path, terminal, 0)["generation"] == 1
    release_admission(state_root, worktree, record)

    assert not _active_path(state_root, worktree).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("commit_parent", None), ("result_commit", None), ("base_head", ZERO40),
     ("observed_head", ZERO40), ("source_count", 0),
     ("path_set_digest", ZERO64), ("change_digest", ZERO64)],
)
def test_release_preserves_admission_when_snapshot_git_binding_differs(
    terminal_admission_fixture, field: str, value: object
) -> None:
    terminal_admission_fixture[3]["git"][field] = value
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)

    with pytest.raises(CheckpointError, match="commit|binding"):
        release_admission(state_root, worktree, record)

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)


@pytest.mark.parametrize("missing", ["source_manifest", "commit_binding"])
def test_release_requires_authenticated_manifest_and_binding(
    terminal_admission_fixture, missing: str
) -> None:
    terminal_admission_fixture[4].pop(missing)
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)

    with pytest.raises(CheckpointError, match="manifest|binding"):
        release_admission(state_root, worktree, record)

    assert _active_path(state_root, worktree).exists()


@pytest.mark.parametrize(
    ("field", "value"), [("schema", True), ("tools_quiescent", 1), ("writers_quiescent", 1)]
)
def test_release_rejects_type_coercion_in_quiescence_evidence(
    terminal_admission_fixture, field: str, value: object
) -> None:
    terminal_admission_fixture[4]["admission_release"][field] = value
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)

    with pytest.raises(CheckpointError, match="quiescence"):
        release_admission(state_root, worktree, record)

    assert _active_path(state_root, worktree).exists()


@pytest.mark.parametrize("changed", ["source", "head", "branch", "binding-evidence", "manifest-evidence"])
def test_release_revalidates_live_git_and_recorded_evidence(
    terminal_admission_fixture, changed: str
) -> None:
    evidence = terminal_admission_fixture[4]
    if changed == "binding-evidence":
        evidence["commit_binding"]["source_count"] = True
    elif changed == "manifest-evidence":
        evidence["source_manifest"]["sources"][0]["sha256"] = ZERO64
    state_root, worktree, record = _admit_terminal(terminal_admission_fixture)
    git = terminal_admission_fixture[5]
    if changed == "source":
        (worktree / "source.py").write_text("after verification\n")
    elif changed == "head":
        git("commit", "--allow-empty", "-qm", "advanced")
    elif changed == "branch":
        git("checkout", "-qb", "other-branch")

    with pytest.raises(CheckpointError):
        release_admission(state_root, worktree, record)

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)


def test_release_rejects_terminal_labels_without_a_commit_binding(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    owner = _owner(worktree)
    release_observation = {
        "schema": 1,
        "run_id": RUN_ID,
        "task_owner": owner["task_owner"],
        "session_fingerprint": owner["session_fingerprint"],
        "snapshot_generation": 0,
        "tools_quiescent": True,
        "writers_quiescent": True,
    }
    path, snapshot, data = _write_checkpoint(
        state_root,
        worktree,
        RUN_ID,
        owner,
        {"admission_release": release_observation},
        terminal=True,
    )
    record = _record(worktree, RUN_ID, owner, path, snapshot, data)
    acquire_admission(state_root, worktree, record)

    with pytest.raises(CheckpointError, match="commit"):
        release_admission(state_root, worktree, record)

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)


@pytest.mark.parametrize(
    ("top_attempt", "child_phase", "child_attempt"),
    [(0, "COMMIT", 0), (1, "READY_FOR_TRIBUNAL", 0)],
)
def test_release_rejects_stale_child_phase_or_attempt_observation(
    admission_fixture, top_attempt: int, child_phase: str, child_attempt: int
) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    owner = record["owner"]
    release_observation = {
        "schema": 1,
        "run_id": RUN_ID,
        "task_owner": owner["task_owner"],
        "session_fingerprint": owner["session_fingerprint"],
        "snapshot_generation": 1,
        "tools_quiescent": True,
        "writers_quiescent": True,
    }
    reference = write_evidence(
        state_root, RUN_ID, {"admission_release": release_observation}
    )
    snapshot = read_snapshot(record["checkpoint_path"])
    snapshot.update(
        phase="READY_FOR_TRIBUNAL",
        phase_status="verified",
        attempt=top_attempt,
        next_action="STOP",
        evidence=reference,
    )
    snapshot["child"] = {
        "child_id": None,
        "phase": child_phase,
        "attempt": child_attempt,
        "observed_status": "quiescent",
    }
    write_snapshot(record["checkpoint_path"], snapshot, expected_generation=0)

    with pytest.raises(CheckpointError, match="terminal"):
        release_admission(state_root, worktree, record)

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)


def test_reconcile_requires_complete_success_proof(admission_fixture) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    successor, _ = _make_successor(state_root, worktree, record)

    with pytest.raises(CheckpointError, match="proof"):
        reconcile_admission(state_root, worktree, record, successor, {})

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)


def test_reconcile_archives_exact_old_bytes_and_publishes_successor(admission_fixture) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    old_snapshot_bytes = Path(record["checkpoint_path"]).read_bytes()
    successor, proof = _make_successor(state_root, worktree, record)
    old_bytes = canonical_bytes(record)

    assert reconcile_admission(state_root, worktree, record, successor, proof) == successor

    active = _active_path(state_root, worktree)
    archive = state_root / "backlog-drain" / "admission-archive" / (
        f"{record['root_key']}-{sha256_bytes(old_bytes)}.json"
    )
    snapshot_copy = state_root / "backlog-drain" / "admission-snapshots" / (
        f"{record['root_key']}-{proof['successor']['snapshot_sha256']}.md"
    )
    prior_snapshot_copy = state_root / "backlog-drain" / "admission-snapshots" / (
        f"{record['root_key']}-{proof['prior']['snapshot_sha256']}.md"
    )
    assert active.read_bytes() == canonical_bytes(successor)
    assert archive.read_bytes() == old_bytes
    assert snapshot_copy.read_bytes() == Path(successor["checkpoint_path"]).read_bytes()
    assert prior_snapshot_copy.read_bytes() == old_snapshot_bytes
    assert not Path(record["checkpoint_path"]).exists()
    assert Path(successor["checkpoint_path"]).is_file()
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(prior_snapshot_copy.stat().st_mode) == 0o600


def test_reconcile_retires_only_predecessor_before_successor_git_collection(
    admission_fixture,
) -> None:
    state_root, worktree, record = admission_fixture
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=worktree, env=environment
        ).decode().strip()

    git("init", "-q", "-b", "task/issue-31")
    git("config", "user.name", "Fixture User")
    git("config", "user.email", "fixture@example.invalid")
    (worktree / "source.py").write_text("unchanged\n")
    git("add", "source.py")
    git("commit", "-qm", "base")
    acquire_admission(state_root, worktree, record)
    successor, proof = _make_successor(state_root, worktree, record)

    assert reconcile_admission(state_root, worktree, record, successor, proof) == successor
    assert collect_sources(str(worktree), [], successor["checkpoint_path"])["count"] == 0

    foreign = worktree / "HANDOFF.backlog-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.md"
    foreign.write_text("foreign\n")
    with pytest.raises(CheckpointError, match="outside the source allowlist"):
        collect_sources(str(worktree), [], successor["checkpoint_path"])
    assert foreign.read_text() == "foreign\n"


def test_reconcile_predecessor_retirement_failure_is_indeterminate_and_paused(
    admission_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    successor, proof = _make_successor(state_root, worktree, record)
    old_snapshot_bytes = Path(record["checkpoint_path"]).read_bytes()
    real_unlink = admission.os.unlink

    def unlink_then_fail(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if path == Path(record["checkpoint_path"]).name:
            raise OSError("fixture predecessor retirement result is uncertain")
        return result

    monkeypatch.setattr(admission.os, "unlink", unlink_then_fail)
    os.supports_dir_fd.add(unlink_then_fail)
    try:
        with pytest.raises(CheckpointIndeterminateError, match="retirement"):
            reconcile_admission(state_root, worktree, record, successor, proof)

        prior_snapshot_copy = state_root / "backlog-drain" / "admission-snapshots" / (
            f"{record['root_key']}-{proof['prior']['snapshot_sha256']}.md"
        )
        assert prior_snapshot_copy.read_bytes() == old_snapshot_bytes
        assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)
        assert not Path(record["checkpoint_path"]).exists()
        assert Path(successor["checkpoint_path"]).is_file()
        with pytest.raises(CheckpointError, match="PAUSED"):
            reconcile_admission(state_root, worktree, record, successor, proof)
    finally:
        os.supports_dir_fd.discard(unlink_then_fail)


def test_reconcile_does_not_retire_predecessor_changed_after_initial_validation(
    admission_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    successor, proof = _make_successor(state_root, worktree, record)
    real_immutable_copy = admission._immutable_copy
    changed_bytes = b""

    def mutate_prior_after_successor_archive(directory_fd: int, name: str, data: bytes) -> None:
        nonlocal changed_bytes
        real_immutable_copy(directory_fd, name, data)
        if name.endswith(f"-{proof['successor']['snapshot_sha256']}.md"):
            current = read_snapshot(record["checkpoint_path"])
            current["attempt"] = 1
            current["child"]["attempt"] = 1
            write_snapshot(record["checkpoint_path"], current, expected_generation=0)
            changed_bytes = Path(record["checkpoint_path"]).read_bytes()

    monkeypatch.setattr(admission, "_immutable_copy", mutate_prior_after_successor_archive)

    with pytest.raises(CheckpointError, match="changed before retirement"):
        reconcile_admission(state_root, worktree, record, successor, proof)

    assert changed_bytes
    assert Path(record["checkpoint_path"]).read_bytes() == changed_bytes
    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)
    prior_snapshot_copy = state_root / "backlog-drain" / "admission-snapshots" / (
        f"{record['root_key']}-{proof['prior']['snapshot_sha256']}.md"
    )
    assert not prior_snapshot_copy.exists()


def test_reconcile_reads_evidence_from_retained_state_root(
    admission_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Wrong branch: reopening successor evidence through a replaced state-root pathname.
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    successor, proof = _make_successor(state_root, worktree, record)
    retained_root = state_root.with_name("retained-state")
    real_locked_checkpoint = admission._locked_checkpoint
    repointed = False

    @contextmanager
    def repoint_before_checkpoint_yield(current_record):
        nonlocal repointed
        with real_locked_checkpoint(current_record) as value:
            if not repointed:
                repointed = True
                state_root.rename(retained_root)
                state_root.mkdir(mode=0o700)
            yield value

    monkeypatch.setattr(admission, "_locked_checkpoint", repoint_before_checkpoint_yield)

    assert reconcile_admission(state_root, worktree, record, successor, proof) == successor

    assert repointed is True
    assert json.loads(_active_path(retained_root, worktree).read_bytes())["run_id"] == NEXT_RUN_ID
    assert not (state_root / "backlog-drain").exists()


def test_reconcile_rejects_proof_for_different_prior_snapshot(admission_fixture) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    successor, proof = _make_successor(state_root, worktree, record)
    proof["prior"]["snapshot_sha256"] = "f" * 64

    with pytest.raises(CheckpointError, match="prior snapshot"):
        reconcile_admission(state_root, worktree, record, successor, proof)

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)


def test_reconcile_rejects_unhashable_quiescence_value_as_checkpoint_error(
    admission_fixture,
) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    successor, proof = _make_successor(state_root, worktree, record)
    proof["quiescence"]["tools"] = []

    with pytest.raises(CheckpointError, match="quiescence"):
        reconcile_admission(state_root, worktree, record, successor, proof)

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)


def test_reconcile_successor_temp_fsync_failure_pauses_with_old_active(
    admission_fixture, monkeypatch
) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    successor, proof = _make_successor(state_root, worktree, record)
    real_fsync = admission.os.fsync

    def fail_successor_temp(fd: int) -> None:
        target = os.readlink(f"/proc/self/fd/{fd}")
        if target.endswith(f"{record['root_key']}.next"):
            raise OSError("fixture successor temp fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(admission.os, "fsync", fail_successor_temp)

    with pytest.raises(CheckpointError, match="successor temp"):
        reconcile_admission(state_root, worktree, record, successor, proof)

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)
    with pytest.raises(CheckpointError, match="PAUSED"):
        reconcile_admission(state_root, worktree, record, successor, proof)


def test_reconcile_fsyncs_prepared_successor_before_retiring_predecessor(
    admission_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    successor, proof = _make_successor(state_root, worktree, record)
    old_snapshot_bytes = Path(record["checkpoint_path"]).read_bytes()
    next_path = (
        state_root
        / "backlog-drain"
        / "admissions"
        / f"{record['root_key']}.next"
    )
    real_fsync = admission.os.fsync

    def fail_pre_retirement_directory_fsync(fd: int) -> None:
        target = os.readlink(f"/proc/self/fd/{fd}")
        if target.endswith("/backlog-drain/admissions") and next_path.exists():
            raise OSError("fixture admissions directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(admission.os, "fsync", fail_pre_retirement_directory_fsync)

    with pytest.raises(CheckpointError, match="successor temp preparation.*PAUSED"):
        reconcile_admission(state_root, worktree, record, successor, proof)

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(record)
    assert Path(record["checkpoint_path"]).read_bytes() == old_snapshot_bytes
    assert Path(successor["checkpoint_path"]).is_file()
    assert next_path.is_file()
    monkeypatch.setattr(admission.os, "fsync", real_fsync)
    with pytest.raises(CheckpointError, match="PAUSED"):
        reconcile_admission(state_root, worktree, record, successor, proof)


def test_reconcile_post_publish_fsync_is_indeterminate_without_rollback(
    admission_fixture, monkeypatch
) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    successor, proof = _make_successor(state_root, worktree, record)
    real_replace = admission.os.replace
    real_fsync = admission.os.fsync
    published = False

    def mark_publish(*args, **kwargs):
        nonlocal published
        result = real_replace(*args, **kwargs)
        published = True
        return result

    def fail_after_publish(fd: int) -> None:
        if published:
            raise OSError("fixture directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(admission.os, "replace", mark_publish)
    monkeypatch.setattr(admission.os, "fsync", fail_after_publish)

    with pytest.raises(CheckpointIndeterminateError, match="publish durability"):
        reconcile_admission(state_root, worktree, record, successor, proof)

    assert _active_path(state_root, worktree).read_bytes() == canonical_bytes(successor)
    monkeypatch.setattr(admission.os, "fsync", real_fsync)
    with pytest.raises(CheckpointError, match="stale"):
        reconcile_admission(state_root, worktree, record, successor, proof)


def _crash_reconcile(
    state_root: str,
    worktree: str,
    expected: dict,
    successor: dict,
    proof: dict,
    boundary: str,
) -> None:
    from scripts.backlog_checkpoint import admission

    if boundary == "archive_after_link":
        real_link = admission.os.link

        def stop_after_link(*args, **kwargs):
            result = real_link(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGSTOP)
            return result

        admission.os.link = stop_after_link
        os.supports_dir_fd.add(stop_after_link)
        os.supports_follow_symlinks.add(stop_after_link)
    elif boundary == "publish_after_replace":
        real_replace = admission.os.replace

        def stop_after_replace(*args, **kwargs):
            result = real_replace(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGSTOP)
            return result

        admission.os.replace = stop_after_replace
    elif boundary == "retire_after_unlink":
        real_unlink = admission.os.unlink
        predecessor_name = Path(expected["checkpoint_path"]).name

        def stop_after_predecessor_unlink(path, *args, **kwargs):
            result = real_unlink(path, *args, **kwargs)
            if path == predecessor_name:
                os.kill(os.getpid(), signal.SIGSTOP)
            return result

        admission.os.unlink = stop_after_predecessor_unlink
        os.supports_dir_fd.add(stop_after_predecessor_unlink)
    reconcile_admission(state_root, worktree, expected, successor, proof)


def _kill_owned_stopped_child(process: multiprocessing.Process) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status_path = Path(f"/proc/{process.pid}/status")
        if status_path.exists():
            state = next(
                (line for line in status_path.read_text().splitlines() if line.startswith("State:")),
                "",
            )
            if "T (stopped)" in state:
                os.kill(process.pid, signal.SIGKILL)
                process.join(5)
                assert process.exitcode == -signal.SIGKILL
                return
        if not process.is_alive():
            break
        time.sleep(0.01)
    if process.is_alive():
        os.kill(process.pid, signal.SIGKILL)
        process.join(5)
    pytest.fail(f"owned fixture child did not stop at syscall boundary: {process.exitcode}")


@pytest.mark.parametrize(
    ("boundary", "expected_run"),
    [
        ("archive_after_link", RUN_ID),
        ("retire_after_unlink", RUN_ID),
        ("publish_after_replace", NEXT_RUN_ID),
    ],
)
def test_reconcile_crash_keeps_old_or_successor_active(
    admission_fixture, boundary: str, expected_run: str
) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
    old_snapshot_bytes = Path(record["checkpoint_path"]).read_bytes()
    successor, proof = _make_successor(state_root, worktree, record)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_reconcile,
        args=(str(state_root), str(worktree), record, successor, proof, boundary),
    )
    process.start()

    _kill_owned_stopped_child(process)

    active_record = __import__("json").loads(_active_path(state_root, worktree).read_bytes())
    assert active_record["run_id"] == expected_run
    if boundary == "retire_after_unlink":
        prior_snapshot_copy = state_root / "backlog-drain" / "admission-snapshots" / (
            f"{record['root_key']}-{proof['prior']['snapshot_sha256']}.md"
        )
        assert prior_snapshot_copy.read_bytes() == old_snapshot_bytes
        assert not Path(record["checkpoint_path"]).exists()
    with pytest.raises(CheckpointError, match="PAUSED|stale"):
        reconcile_admission(state_root, worktree, record, successor, proof)
