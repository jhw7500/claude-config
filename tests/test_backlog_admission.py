import copy
import hashlib
import multiprocessing
import os
import signal
import stat
import time
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


def test_acquire_rejects_worktree_alias_without_canonicalizing_scope(admission_fixture) -> None:
    state_root, worktree, record = admission_fixture
    alias = worktree.parent / "alias"
    alias.symlink_to(worktree, target_is_directory=True)
    aliased = copy.deepcopy(record)
    aliased["owner"]["worktree_root"] = str(alias)

    with pytest.raises(CheckpointError, match="canonical"):
        acquire_admission(state_root, alias, aliased)

    assert not _active_path(state_root, worktree).exists()


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


def test_release_accepts_owned_terminal_checkpoint_observations(tmp_path: Path) -> None:
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

    release_admission(state_root, worktree, record)

    assert not _active_path(state_root, worktree).exists()


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
    assert active.read_bytes() == canonical_bytes(successor)
    assert archive.read_bytes() == old_bytes
    assert snapshot_copy.read_bytes() == Path(successor["checkpoint_path"]).read_bytes()
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600


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
    [("archive_after_link", RUN_ID), ("publish_after_replace", NEXT_RUN_ID)],
)
def test_reconcile_crash_keeps_old_or_successor_active(
    admission_fixture, boundary: str, expected_run: str
) -> None:
    state_root, worktree, record = admission_fixture
    acquire_admission(state_root, worktree, record)
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
    with pytest.raises(CheckpointError, match="PAUSED|stale"):
        reconcile_admission(state_root, worktree, record, successor, proof)

