"""Exclusive, observation-only backlog-drainer admission coordination."""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .codec import (
    MAX_SNAPSHOT_BYTES,
    CheckpointError,
    CheckpointIndeterminateError,
    canonical_bytes,
    decode_snapshot,
    sha256_bytes,
)
from .git_evidence import bind_commit, inspect_worktree
from .storage import (
    _check_owned_file,
    _existing_file_bytes,
    _lock_directory,
    _open_directory,
    _open_lock,
    _open_or_create_directory,
    _open_state_root,
    _read_evidence_at,
    _read_file_at,
)


_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ADMISSION_BYTES = 16 * 1024
_NOFOLLOW = os.O_NOFOLLOW


def _exact_dict(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise CheckpointError(f"{label} has missing or unknown keys")
    return value


def _string(value: object, label: str, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value:
        raise CheckpointError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise CheckpointError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum or any(
        unicodedata.category(character) == "Cc" for character in value
    ):
        raise CheckpointError(f"{label} is unsafe or too long")
    return value


def _hex(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise CheckpointError(f"{label} must be lowercase hexadecimal")
    return value


def _canonical_root(path_value: str | os.PathLike[str], label: str) -> Path:
    try:
        supplied = os.fspath(path_value)
    except TypeError as exc:
        raise CheckpointError(f"{label} must be an exact absolute path") from exc
    if not isinstance(supplied, str):
        raise CheckpointError(f"{label} must be an exact absolute path")
    if not os.path.isabs(supplied) or os.path.normpath(supplied) != supplied:
        raise CheckpointError(f"{label} must be an exact absolute path")
    if os.path.realpath(supplied) != supplied:
        raise CheckpointError(f"{label} must be the canonical physical path")
    path = Path(supplied)
    fd = _open_directory(path, private=False)
    os.close(fd)
    return path


def _validate_owner(value: object, worktree: Path) -> dict:
    owner = _exact_dict(
        value,
        {
            "issue_url",
            "task_id",
            "claim_id",
            "task_owner",
            "branch",
            "worktree_root",
            "session_fingerprint",
        },
        "owner",
    )
    for key in ("issue_url", "task_id", "claim_id", "task_owner", "branch"):
        _string(owner[key], f"owner.{key}", 512)
    if _string(owner["worktree_root"], "owner.worktree_root") != str(worktree):
        raise CheckpointError("owner worktree does not match the exact physical root")
    _hex(owner["session_fingerprint"], _HEX64, "owner.session_fingerprint")
    return owner


def _validate_record(record: object, worktree: Path) -> dict:
    value = _exact_dict(
        record,
        {"schema", "run_id", "root_key", "checkpoint_path", "owner"},
        "admission record",
    )
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise CheckpointError("admission schema must be integer 1")
    run_id = _hex(value["run_id"], _HEX32, "run_id")
    root_key = _hex(value["root_key"], _HEX64, "root_key")
    if root_key != sha256_bytes(str(worktree).encode("utf-8")):
        raise CheckpointError("root_key does not match the physical worktree")
    checkpoint = _string(value["checkpoint_path"], "checkpoint_path")
    expected_checkpoint = str(worktree / f"HANDOFF.backlog-{run_id}.md")
    if checkpoint != expected_checkpoint:
        raise CheckpointError("checkpoint_path is not the exact run checkpoint")
    _validate_owner(value["owner"], worktree)
    if len(canonical_bytes(value)) > _MAX_ADMISSION_BYTES:
        raise CheckpointError("admission record exceeds its size limit")
    return value


def _validate_snapshot_binding(snapshot: dict, record: dict) -> None:
    owner = record["owner"]
    binding = snapshot["binding"]
    if snapshot["run_id"] != record["run_id"]:
        raise CheckpointError("checkpoint run does not match admission")
    expected_binding = {
        "issue_url": owner["issue_url"],
        "task_id": owner["task_id"],
        "claim_id": owner["claim_id"],
        "worktree_root": owner["worktree_root"],
    }
    if any(binding[key] != expected for key, expected in expected_binding.items()):
        raise CheckpointError("checkpoint binding does not match admission owner")
    if snapshot["git"]["branch"] != owner["branch"]:
        raise CheckpointError("checkpoint branch does not match admission owner")
    if snapshot["supervisor_identity"]["session_fingerprint"] != owner["session_fingerprint"]:
        raise CheckpointError("checkpoint supervisor does not match admission owner")


@contextmanager
def _locked_checkpoint(record: dict) -> Iterator[tuple[bytes, dict]]:
    path = Path(record["checkpoint_path"])
    directory_fd = _open_directory(path.parent, private=False)
    lock_fd = -1
    try:
        lock_fd = _lock_directory(directory_fd)
        data = _read_file_at(directory_fd, path.name, MAX_SNAPSHOT_BYTES)
        snapshot = decode_snapshot(data)
        _validate_snapshot_binding(snapshot, record)
        yield data, snapshot
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(directory_fd)


def _open_admission_directories(state_root: str | os.PathLike[str]) -> tuple[int, int, int, int, int]:
    root_fd = _open_state_root(state_root)
    backlog_fd = admissions_fd = archive_fd = snapshots_fd = -1
    try:
        backlog_fd = _open_or_create_directory(root_fd, "backlog-drain")
        admissions_fd = _open_or_create_directory(backlog_fd, "admissions")
        archive_fd = _open_or_create_directory(backlog_fd, "admission-archive")
        snapshots_fd = _open_or_create_directory(backlog_fd, "admission-snapshots")
        return root_fd, backlog_fd, admissions_fd, archive_fd, snapshots_fd
    except Exception:
        for fd in (snapshots_fd, archive_fd, admissions_fd, backlog_fd, root_fd):
            if fd >= 0:
                os.close(fd)
        raise


def _close_directories(fds: tuple[int, ...]) -> None:
    for fd in reversed(fds):
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise CheckpointError("short admission write")
        view = view[written:]


def _exclusive_active(directory_fd: int, name: str, data: bytes) -> None:
    fd = -1
    published = False
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=directory_fd)
        published = True
        os.fchmod(fd, 0o600)
        _check_owned_file(fd)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.fsync(directory_fd)
    except FileExistsError as exc:
        raise CheckpointError("an active admission already exists") from exc
    except CheckpointError:
        raise
    except OSError as exc:
        if published:
            raise CheckpointIndeterminateError("admission creation durability is indeterminate") from exc
        raise CheckpointError("cannot create active admission") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _immutable_copy(directory_fd: int, name: str, data: bytes) -> None:
    existing = _existing_file_bytes(directory_fd, name, max(len(data), 1))
    if existing is not None:
        if existing != data:
            raise CheckpointError("immutable admission copy has different bytes")
        return
    temporary = f".{name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    fd = -1
    published = False
    preserve_temporary = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=directory_fd)
        os.fchmod(fd, 0o600)
        _check_owned_file(fd)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
            published = True
        except FileExistsError:
            if _read_file_at(directory_fd, name, max(len(data), 1)) != data:
                raise CheckpointError("immutable admission copy has different bytes")
            published = True
        os.fsync(directory_fd)
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except CheckpointError:
        raise
    except OSError as exc:
        if published:
            preserve_temporary = True
            raise CheckpointIndeterminateError("immutable admission copy durability is indeterminate") from exc
        raise CheckpointError("cannot create immutable admission copy") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if not preserve_temporary:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _active_bytes(admissions_fd: int, active_name: str) -> bytes:
    current = _existing_file_bytes(admissions_fd, active_name, _MAX_ADMISSION_BYTES)
    if current is None:
        raise CheckpointError("active admission is absent")
    return current


def _archive_and_retire_checkpoint(
    record: dict, expected_bytes: bytes, snapshots_fd: int, archive_name: str
) -> None:
    path = Path(record["checkpoint_path"])
    directory_fd = _open_directory(path.parent, private=False)
    lock_fd = -1
    try:
        lock_fd = _lock_directory(directory_fd)
        current = _read_file_at(directory_fd, path.name, MAX_SNAPSHOT_BYTES)
        snapshot = decode_snapshot(current)
        _validate_snapshot_binding(snapshot, record)
        if current != expected_bytes:
            raise CheckpointError("predecessor checkpoint changed before retirement")
        _immutable_copy(snapshots_fd, archive_name, current)
        if _read_file_at(directory_fd, path.name, MAX_SNAPSHOT_BYTES) != current:
            raise CheckpointError("predecessor checkpoint changed before retirement")
        try:
            os.unlink(path.name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError as exc:
            raise CheckpointIndeterminateError(
                "predecessor checkpoint retirement is indeterminate"
            ) from exc
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(directory_fd)


def _reject_incomplete(
    admissions_fd: int,
    next_name: str,
    archive_fd: int,
    snapshots_fd: int,
    key: str,
) -> None:
    if _existing_file_bytes(admissions_fd, f"{key}.release", _MAX_ADMISSION_BYTES) is not None:
        raise CheckpointError("incomplete admission release requires PAUSED reconciliation")
    if _existing_file_bytes(admissions_fd, next_name, _MAX_ADMISSION_BYTES) is not None:
        raise CheckpointError("incomplete successor admission requires PAUSED reconciliation")
    prefix = f".{key}-"
    for directory_fd in (archive_fd, snapshots_fd):
        if any(name.startswith(prefix) and ".tmp-" in name for name in os.listdir(directory_fd)):
            raise CheckpointError("incomplete admission copy requires PAUSED reconciliation")


def acquire_admission(
    state_root: str | os.PathLike[str], worktree_root: str | os.PathLike[str], record: dict
) -> dict:
    """Create the first admission; record fields are consistency observations only."""
    worktree = _canonical_root(worktree_root, "worktree_root")
    validated = _validate_record(record, worktree)
    data = canonical_bytes(validated)
    fds = _open_admission_directories(state_root)
    lock_fd = -1
    key = validated["root_key"]
    try:
        lock_fd = _open_lock(fds[2], f"{key}.lock")
        _reject_incomplete(fds[2], f"{key}.next", fds[3], fds[4], key)
        with _locked_checkpoint(validated):
            _exclusive_active(fds[2], f"{key}.json", data)
        return validated
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        _close_directories(fds)


def _validate_release(snapshot: dict, record: dict, state_root_fd: int) -> None:
    if not (
        snapshot["phase"] == "READY_FOR_TRIBUNAL"
        and snapshot["phase_status"] == "verified"
        and snapshot["next_action"] == "STOP"
        and snapshot["child"]["phase"] == snapshot["phase"]
        and snapshot["child"]["attempt"] == snapshot["attempt"]
        and snapshot["child"]["observed_status"] in {"quiescent", "completed"}
    ):
        raise CheckpointError("release requires an owned terminal checkpoint")
    git = snapshot["git"]
    if (
        git["commit_parent"] is None
        or git["result_commit"] is None
        or git["base_head"] != git["commit_parent"]
        or git["observed_head"] != git["result_commit"]
    ):
        raise CheckpointError("release requires a verified commit binding")
    manifest = _read_evidence_at(state_root_fd, record["run_id"], snapshot["evidence"])
    observation = _exact_dict(
        manifest.get("admission_release"),
        {"schema", "run_id", "task_owner", "session_fingerprint", "snapshot_generation", "tools_quiescent", "writers_quiescent"},
        "admission_release evidence",
    )
    owner = record["owner"]
    expected = {
        "schema": 1,
        "run_id": record["run_id"],
        "task_owner": owner["task_owner"],
        "session_fingerprint": owner["session_fingerprint"],
        "snapshot_generation": snapshot["generation"],
        "tools_quiescent": True,
        "writers_quiescent": True,
    }
    if canonical_bytes(observation) != canonical_bytes(expected):
        raise CheckpointError("release quiescence evidence does not match the current checkpoint")
    fields = {"commit_parent", "result_commit", "source_count", "path_set_digest", "change_digest"}
    recorded_binding = _exact_dict(manifest.get("commit_binding"), fields, "commit_binding")
    snapshot_binding = {field: git[field] for field in fields}
    if canonical_bytes(recorded_binding) != canonical_bytes(snapshot_binding):
        raise CheckpointError("release commit binding differs from its snapshot")
    source_manifest = manifest.get("source_manifest")
    if not isinstance(source_manifest, dict):
        raise CheckpointError("release requires an authenticated source manifest")
    root = owner["worktree_root"]
    inspected = inspect_worktree(root, owner["branch"], root)
    if inspected["head"] != git["observed_head"]:
        raise CheckpointError("release commit binding differs from the live HEAD")
    fresh_binding = bind_commit(
        root, git["base_head"], source_manifest, git["result_commit"], record["checkpoint_path"]
    )
    if canonical_bytes(fresh_binding) != canonical_bytes(recorded_binding):
        raise CheckpointError("release commit binding differs from the live source manifest")


def release_admission(
    state_root: str | os.PathLike[str], worktree_root: str | os.PathLike[str], expected_record: dict
) -> None:
    """Release only the exact caller-owned terminal admission."""
    worktree = _canonical_root(worktree_root, "worktree_root")
    expected = _validate_record(expected_record, worktree)
    expected_bytes = canonical_bytes(expected)
    fds = _open_admission_directories(state_root)
    lock_fd = -1
    key = expected["root_key"]
    active_name = f"{key}.json"
    try:
        lock_fd = _open_lock(fds[2], f"{key}.lock")
        _reject_incomplete(fds[2], f"{key}.next", fds[3], fds[4], key)
        if _active_bytes(fds[2], active_name) != expected_bytes:
            raise CheckpointError("stale or foreign admission record")
        with _locked_checkpoint(expected) as (checkpoint_bytes, snapshot):
            _validate_release(snapshot, expected, fds[0])
        _immutable_copy(
            fds[3], f"{key}-{sha256_bytes(expected_bytes)}.json", expected_bytes
        )
        release_name = f"{key}.release"
        try:
            _exclusive_active(fds[2], release_name, expected_bytes)
        except CheckpointIndeterminateError as exc:
            raise CheckpointIndeterminateError(
                "release intent durability is indeterminate; reconciliation is PAUSED"
            ) from exc
        except CheckpointError as exc:
            raise CheckpointError("release intent preparation failed; reconciliation is PAUSED") from exc
        try:
            if _active_bytes(fds[2], active_name) != expected_bytes:
                raise CheckpointError("active admission changed before checkpoint retirement")
            _archive_and_retire_checkpoint(
                expected,
                checkpoint_bytes,
                fds[4],
                f"{key}-{sha256_bytes(checkpoint_bytes)}.md",
            )
            if _active_bytes(fds[2], active_name) != expected_bytes:
                raise CheckpointError("active admission changed before release")
            os.unlink(active_name, dir_fd=fds[2])
            os.fsync(fds[2])
            os.unlink(release_name, dir_fd=fds[2])
            os.fsync(fds[2])
        except CheckpointError:
            raise
        except OSError as exc:
            raise CheckpointIndeterminateError("admission release durability is indeterminate") from exc
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        _close_directories(fds)


def _validate_proof(proof: object, expected: dict, successor: dict) -> dict:
    value = _exact_dict(proof, {"schema", "recovery_result", "prior", "successor", "quiescence", "git"}, "successor proof")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise CheckpointError("successor proof schema must be integer 1")
    if value["recovery_result"] != "succeeded":
        raise CheckpointError("successor proof requires a successful external recovery result")
    prior = _exact_dict(value["prior"], {"admission_sha256", "snapshot_sha256", "snapshot_generation"}, "successor proof prior")
    following = _exact_dict(value["successor"], {"snapshot_sha256", "snapshot_generation"}, "successor proof successor")
    for label, item in (("prior admission", prior["admission_sha256"]), ("prior snapshot", prior["snapshot_sha256"]), ("successor snapshot", following["snapshot_sha256"])):
        _hex(item, _HEX64, label)
    for label, item in (("prior snapshot generation", prior["snapshot_generation"]), ("successor snapshot generation", following["snapshot_generation"])):
        if type(item) is not int or item < 0:
            raise CheckpointError(f"{label} must be a non-negative integer")
    quiescence = _exact_dict(value["quiescence"], {"children", "tools", "writers"}, "successor proof quiescence")
    if any(item != "quiescent" for item in quiescence.values()):
        raise CheckpointError("successor proof requires explicit quiescence")
    git = _exact_dict(value["git"], {"branch", "worktree_root", "observed_head", "change_digest"}, "successor proof git")
    _hex(git["observed_head"], _HEX40, "successor proof observed_head")
    _hex(git["change_digest"], _HEX64, "successor proof change_digest")
    if git["branch"] != successor["owner"]["branch"] or git["worktree_root"] != successor["owner"]["worktree_root"]:
        raise CheckpointError("successor proof Git coordinates do not match")
    if prior["admission_sha256"] != sha256_bytes(canonical_bytes(expected)):
        raise CheckpointError("successor proof does not match the prior admission")
    return value


def _validate_successor_relationship(expected: dict, successor: dict) -> None:
    if expected["root_key"] != successor["root_key"]:
        raise CheckpointError("successor root_key differs from the active admission")
    for field in ("issue_url", "task_id", "worktree_root"):
        if expected["owner"][field] != successor["owner"][field]:
            raise CheckpointError(f"successor {field} differs from the active admission")
    if expected["run_id"] == successor["run_id"] or expected["owner"]["claim_id"] == successor["owner"]["claim_id"]:
        raise CheckpointError("successor requires a new run and Claim observation")


def reconcile_admission(
    state_root: str | os.PathLike[str],
    worktree_root: str | os.PathLike[str],
    expected_record: dict,
    successor_record: dict,
    proof: dict,
) -> dict:
    """Archive exact old bytes and atomically publish a proven successor observation."""
    worktree = _canonical_root(worktree_root, "worktree_root")
    expected = _validate_record(expected_record, worktree)
    successor = _validate_record(successor_record, worktree)
    _validate_successor_relationship(expected, successor)
    checked_proof = _validate_proof(proof, expected, successor)
    expected_bytes = canonical_bytes(expected)
    successor_bytes = canonical_bytes(successor)
    key = expected["root_key"]
    active_name = f"{key}.json"
    next_name = f"{key}.next"
    fds = _open_admission_directories(state_root)
    lock_fd = -1
    try:
        lock_fd = _open_lock(fds[2], f"{key}.lock")
        _reject_incomplete(fds[2], next_name, fds[3], fds[4], key)
        if _active_bytes(fds[2], active_name) != expected_bytes:
            raise CheckpointError("stale or foreign active admission")
        with _locked_checkpoint(expected) as (old_snapshot_bytes, old_snapshot):
            prior = checked_proof["prior"]
            if prior["snapshot_sha256"] != sha256_bytes(old_snapshot_bytes) or prior["snapshot_generation"] != old_snapshot["generation"]:
                raise CheckpointError("successor proof prior snapshot does not match")
        with _locked_checkpoint(successor) as (successor_snapshot_bytes, successor_snapshot):
            following = checked_proof["successor"]
            if (
                following["snapshot_sha256"] != sha256_bytes(successor_snapshot_bytes)
                or following["snapshot_generation"] != successor_snapshot["generation"]
                or checked_proof["git"]["observed_head"] != successor_snapshot["git"]["observed_head"]
                or checked_proof["git"]["change_digest"] != successor_snapshot["git"]["change_digest"]
            ):
                raise CheckpointError("successor proof snapshot or Git evidence does not match")
            manifest = _read_evidence_at(
                fds[0], successor["run_id"], successor_snapshot["evidence"]
            )
            successor_observation = _exact_dict(
                manifest.get("admission_successor"),
                {"prior_run_id", "prior_snapshot_sha256", "prior_snapshot_generation"},
                "admission_successor evidence",
            )
            if successor_observation != {
                "prior_run_id": expected["run_id"],
                "prior_snapshot_sha256": checked_proof["prior"]["snapshot_sha256"],
                "prior_snapshot_generation": checked_proof["prior"]["snapshot_generation"],
            }:
                raise CheckpointError("successor snapshot does not bind the prior checkpoint")
        archive_name = f"{key}-{sha256_bytes(expected_bytes)}.json"
        _immutable_copy(fds[3], archive_name, expected_bytes)
        _immutable_copy(
            fds[4],
            f"{key}-{sha256_bytes(successor_snapshot_bytes)}.md",
            successor_snapshot_bytes,
        )
        fd = -1
        try:
            fd = os.open(next_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=fds[2])
            os.fchmod(fd, 0o600)
            _check_owned_file(fd)
            _write_all(fd, successor_bytes)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.fsync(fds[2])
            if _active_bytes(fds[2], active_name) != expected_bytes:
                raise CheckpointError("active admission changed before successor publish")
            _archive_and_retire_checkpoint(
                expected,
                old_snapshot_bytes,
                fds[4],
                f"{key}-{sha256_bytes(old_snapshot_bytes)}.md",
            )
            if _active_bytes(fds[2], active_name) != expected_bytes:
                raise CheckpointError("active admission changed before successor publish")
            os.replace(next_name, active_name, src_dir_fd=fds[2], dst_dir_fd=fds[2])
            try:
                os.fsync(fds[2])
            except OSError as exc:
                raise CheckpointIndeterminateError("successor publish durability is indeterminate") from exc
        except CheckpointError:
            raise
        except OSError as exc:
            raise CheckpointError("successor temp preparation failed; reconciliation is PAUSED") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        return successor
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        _close_directories(fds)
