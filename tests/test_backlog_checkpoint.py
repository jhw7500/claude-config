import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from scripts.backlog_checkpoint import storage
from scripts.backlog_checkpoint.codec import (
    CheckpointError,
    CheckpointIndeterminateError,
    canonical_bytes,
    decode_snapshot,
    encode_snapshot,
    sha256_bytes,
    supervisor_fingerprint,
    validate_snapshot,
)
from scripts.backlog_checkpoint.storage import (
    read_evidence,
    read_snapshot,
    write_evidence,
    write_snapshot,
)


RUN_ID = "0123456789abcdef0123456789abcdef"
ZERO40 = "0" * 40
ONE40 = "1" * 40
ZERO64 = "0" * 64
ONE64 = "1" * 64


def valid_snapshot() -> dict:
    return {
        "schema": 1,
        "run_id": RUN_ID,
        "generation": 0,
        "phase": "IMPLEMENT",
        "phase_status": "started",
        "attempt": 0,
        "next_action": "INDEPENDENT_REVIEW",
        "blockers": [],
        "supervisor_identity": {
            "runtime": "codex",
            "origin_adapter": "codex",
            "host": "build-host",
            "session_fingerprint": ZERO64,
        },
        "binding": {
            "issue_url": "https://github.com/jhw7500/claude-config/issues/31",
            "task_id": "task-31",
            "claim_id": "claim-31",
            "repo_root": "/srv/claude-config",
            "worktree_root": "/srv/claude-config-31",
        },
        "git": {
            "branch": "task/issue-31",
            "base_head": ZERO40,
            "observed_head": ONE40,
            "source_count": 4,
            "path_set_digest": ZERO64,
            "change_digest": ONE64,
            "commit_parent": None,
            "result_commit": None,
        },
        "child": {
            "child_id": None,
            "phase": "IMPLEMENT",
            "attempt": 0,
            "observed_status": "quiescent",
        },
        "evidence": {
            "ref": "evidence/" + ZERO64 + ".json",
            "sha256": ZERO64,
            "size": 0,
        },
    }


def replace(snapshot: dict, dotted_key: str, value: object) -> dict:
    copied = json.loads(json.dumps(snapshot))
    target = copied
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    return copied


def nested_list(depth: int) -> list:
    value: list = []
    for _ in range(depth - 1):
        value = [value]
    return value


def test_canonical_bytes_and_digest_use_the_declared_json_encoding() -> None:
    data = canonical_bytes({"z": "한", "a": [1, None]})

    assert data == b'{"a":[1,null],"z":"\xed\x95\x9c"}'
    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()


def test_canonical_bytes_rejects_non_finite_numbers() -> None:
    with pytest.raises(CheckpointError):
        canonical_bytes({"value": float("nan")})


def test_canonical_bytes_accepts_depth_64_and_rejects_depth_65() -> None:
    assert canonical_bytes(nested_list(64))

    with pytest.raises(CheckpointError):
        canonical_bytes(nested_list(65))


def test_decode_snapshot_normalizes_parser_recursion_error() -> None:
    record = b"[" * 2_000 + b"]" * 2_000
    encoded = b"# Backlog checkpoint schema 1\n```json\n" + record + b"\n```\n"

    with pytest.raises(CheckpointError):
        decode_snapshot(encoded)


def test_supervisor_fingerprint_hashes_the_literal_identity_tuple() -> None:
    assert supervisor_fingerprint("codex", "session-7", "host-a") == (
        "c1e21e05b0c4b7278f175f8cc9f5813a9bb576e9ce87f60c9e372daed923a9e3"
    )


def test_snapshot_round_trip_is_four_lines_with_one_json_record() -> None:
    encoded = encode_snapshot(valid_snapshot())

    assert encoded.startswith(b"# Backlog checkpoint schema 1\n```json\n{")
    assert encoded.endswith(b"\n```\n")
    assert len(encoded.splitlines()) == 4
    assert decode_snapshot(encoded) == valid_snapshot()


@pytest.mark.parametrize(
    "data",
    [
        b"not markdown",
        b"# Backlog checkpoint schema 1\n```json\n{}\n```\ntrailing",
        b"# Backlog checkpoint schema 1\n```json\n{\"schema\":1,\"schema\":1}\n```\n",
        b"# Backlog checkpoint schema 1\n```json\n{\"schema\":1}\xff\n```\n",
    ],
)
def test_decode_snapshot_rejects_malformed_duplicate_or_non_utf8_input(
    data: bytes,
) -> None:
    with pytest.raises(CheckpointError):
        decode_snapshot(data)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("generation", True),
        ("phase", "FINISHED"),
        ("phase_status", "done"),
        ("attempt", 2),
        ("next_action", "rm -rf /"),
        ("supervisor_identity.runtime", "shell"),
        ("child.observed_status", "lost"),
        ("git.source_count", True),
        ("evidence.size", 1_048_577),
    ],
)
def test_validate_snapshot_rejects_bool_integer_unknown_enum_and_caps(
    key: str, value: object
) -> None:
    with pytest.raises(CheckpointError):
        validate_snapshot(replace(valid_snapshot(), key, value))


def test_validate_snapshot_rejects_unknown_keys() -> None:
    snapshot = valid_snapshot()
    snapshot["command"] = "git commit"

    with pytest.raises(CheckpointError):
        validate_snapshot(snapshot)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("run_id", "A" * 32),
        ("supervisor_identity.host", "h" * 256),
        ("binding.repo_root", "relative/repo"),
        ("binding.worktree_root", "/srv/../escape"),
        ("binding.repo_root", "/srv/control\u0085escape"),
        ("evidence.ref", "../manifest.json"),
        ("evidence.ref", "/absolute/manifest.json"),
        ("evidence.ref", "evidence/" + ONE64 + ".json"),
        ("git.base_head", "g" * 40),
    ],
)
def test_validate_snapshot_rejects_bad_identifiers_paths_and_lengths(
    key: str, value: object
) -> None:
    with pytest.raises(CheckpointError):
        validate_snapshot(replace(valid_snapshot(), key, value))


def test_validate_snapshot_requires_matching_runtime_and_origin_adapter() -> None:
    with pytest.raises(CheckpointError):
        validate_snapshot(
            replace(valid_snapshot(), "supervisor_identity.origin_adapter", "claude")
        )


def test_maximum_valid_snapshot_stays_inside_the_12_kib_budget() -> None:
    maximum = replace(valid_snapshot(), "blockers", ["x" * 128] * 16)
    maximum["binding"]["repo_root"] = "/" + "r" * 2046
    maximum["binding"]["worktree_root"] = "/" + "w" * 2046
    maximum["binding"]["issue_url"] = "i" * 256
    maximum["binding"]["task_id"] = "t" * 128
    maximum["binding"]["claim_id"] = "c" * 128
    maximum["git"]["branch"] = "b" * 256
    maximum["child"]["child_id"] = "d" * 256

    encoded = encode_snapshot(maximum)

    assert len(encoded) <= 12 * 1024
    assert len(encoded.splitlines()) == 4


def test_write_snapshot_creates_generation_zero_and_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / f"HANDOFF.backlog-{RUN_ID}.md"

    stored = write_snapshot(path, valid_snapshot(), expected_generation=None)

    assert stored["generation"] == 0
    assert read_snapshot(path) == stored
    file_stat = path.stat()
    assert stat.S_IMODE(file_stat.st_mode) == 0o600
    assert file_stat.st_uid == os.getuid()


def test_write_snapshot_updates_by_exact_generation_and_rejects_stale_cas(
    tmp_path: Path,
) -> None:
    path = tmp_path / f"HANDOFF.backlog-{RUN_ID}.md"
    write_snapshot(path, valid_snapshot(), expected_generation=None)

    updated = write_snapshot(
        path,
        replace(valid_snapshot(), "phase", "INDEPENDENT_REVIEW"),
        expected_generation=0,
    )

    assert updated["generation"] == 1
    with pytest.raises(CheckpointError):
        write_snapshot(path, valid_snapshot(), expected_generation=0)
    assert read_snapshot(path) == updated


def test_partial_snapshot_write_preserves_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / f"HANDOFF.backlog-{RUN_ID}.md"
    first = write_snapshot(path, valid_snapshot(), expected_generation=None)
    old_bytes = path.read_bytes()
    real_write = os.write
    write_calls = 0

    def partial_then_fail(fd: int, data: bytes | memoryview) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return real_write(fd, data[: len(data) // 2])
        raise OSError("simulated short device write")

    monkeypatch.setattr(storage.os, "write", partial_then_fail)

    with pytest.raises(CheckpointError):
        write_snapshot(
            path,
            replace(valid_snapshot(), "phase", "INDEPENDENT_REVIEW"),
            expected_generation=first["generation"],
        )

    assert path.read_bytes() == old_bytes
    assert read_snapshot(path) == first
    assert not list(tmp_path.glob(f".{path.name}.tmp-*"))


def test_directory_fsync_failure_after_publish_is_indeterminate_and_not_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / f"HANDOFF.backlog-{RUN_ID}.md"
    first = write_snapshot(path, valid_snapshot(), expected_generation=None)
    real_fsync = os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(storage.os, "fsync", fail_directory_fsync)

    with pytest.raises(CheckpointIndeterminateError):
        write_snapshot(
            path,
            replace(valid_snapshot(), "phase", "INDEPENDENT_REVIEW"),
            expected_generation=first["generation"],
        )

    installed = read_snapshot(path)
    assert installed["generation"] == 1
    assert installed["phase"] == "INDEPENDENT_REVIEW"


@pytest.mark.parametrize("missing_flag", [None, 0])
def test_missing_nofollow_capability_fails_before_snapshot_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_flag: object
) -> None:
    path = tmp_path / f"HANDOFF.backlog-{RUN_ID}.md"
    monkeypatch.setattr(storage, "_NOFOLLOW", missing_flag)

    with pytest.raises(CheckpointError):
        write_snapshot(path, valid_snapshot(), expected_generation=None)

    assert list(tmp_path.iterdir()) == []


def test_snapshot_storage_rejects_symlink_targets(tmp_path: Path) -> None:
    target = tmp_path / "outside.md"
    target.write_bytes(encode_snapshot(valid_snapshot()))
    target.chmod(0o600)
    assert read_snapshot(target) == valid_snapshot()
    path = tmp_path / f"HANDOFF.backlog-{RUN_ID}.md"
    path.symlink_to(target)

    with pytest.raises(CheckpointError):
        write_snapshot(path, valid_snapshot(), expected_generation=None)
    with pytest.raises(CheckpointError):
        read_snapshot(path)
    assert read_snapshot(target) == valid_snapshot()


def test_write_evidence_is_content_addressed_immutable_and_private(
    tmp_path: Path,
) -> None:
    value = {"kind": "source-manifest", "paths": ["a.py", "한글.py"]}

    reference = write_evidence(tmp_path, RUN_ID, value)
    repeated = write_evidence(tmp_path, RUN_ID, value)

    expected_bytes = b'{"kind":"source-manifest","paths":["a.py","\xed\x95\x9c\xea\xb8\x80.py"]}'
    expected_digest = hashlib.sha256(expected_bytes).hexdigest()
    assert reference == {
        "ref": f"evidence/{expected_digest}.json",
        "sha256": expected_digest,
        "size": len(expected_bytes),
    }
    assert repeated == reference
    assert read_evidence(tmp_path, RUN_ID, reference) == value
    evidence_path = tmp_path / "backlog-drain" / RUN_ID / reference["ref"]
    evidence_stat = evidence_path.stat()
    assert stat.S_IMODE(evidence_stat.st_mode) == 0o600
    assert evidence_stat.st_uid == os.getuid()


def test_evidence_directory_fsync_failure_after_publish_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = {"kind": "review"}
    data = b'{"kind":"review"}'
    digest = hashlib.sha256(data).hexdigest()
    real_fsync = os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(storage.os, "fsync", fail_directory_fsync)

    with pytest.raises(CheckpointIndeterminateError):
        write_evidence(tmp_path, RUN_ID, value)

    published = tmp_path / "backlog-drain" / RUN_ID / "evidence" / f"{digest}.json"
    assert published.read_bytes() == data
    monkeypatch.setattr(storage.os, "fsync", real_fsync)
    assert read_evidence(
        tmp_path,
        RUN_ID,
        {"ref": f"evidence/{digest}.json", "sha256": digest, "size": len(data)},
    ) == value


def test_evidence_collision_fsync_failure_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = {"kind": "collision"}
    data = b'{"kind":"collision"}'
    digest = hashlib.sha256(data).hexdigest()
    published = tmp_path / "backlog-drain" / RUN_ID / "evidence" / f"{digest}.json"
    real_fsync = os.fsync
    collision_created = False

    def collide_then_fail_directory_fsync(fd: int) -> None:
        nonlocal collision_created
        if stat.S_ISREG(os.fstat(fd).st_mode):
            real_fsync(fd)
            temporary = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if not collision_created and temporary.name.startswith(f".{digest}.json.tmp-"):
                os.link(temporary, published)
                collision_created = True
            return
        raise OSError("simulated collision directory fsync failure")

    monkeypatch.setattr(storage.os, "fsync", collide_then_fail_directory_fsync)

    with pytest.raises(CheckpointIndeterminateError):
        write_evidence(tmp_path, RUN_ID, value)

    assert collision_created
    assert published.read_bytes() == data
    monkeypatch.setattr(storage.os, "fsync", real_fsync)
    assert read_evidence(
        tmp_path,
        RUN_ID,
        {"ref": f"evidence/{digest}.json", "sha256": digest, "size": len(data)},
    ) == value


def test_evidence_collision_with_wrong_bytes_still_fails(tmp_path: Path) -> None:
    value = {"kind": "collision"}
    expected = b'{"kind":"collision"}'
    digest = hashlib.sha256(expected).hexdigest()
    evidence_dir = tmp_path / "backlog-drain" / RUN_ID / "evidence"
    evidence_dir.mkdir(parents=True, mode=0o700)
    published = evidence_dir / f"{digest}.json"
    published.write_bytes(b'{"kind":"tampered"}')
    published.chmod(0o600)

    with pytest.raises(CheckpointError):
        write_evidence(tmp_path, RUN_ID, value)

    assert published.read_bytes() == b'{"kind":"tampered"}'


def test_write_evidence_rejects_manifest_larger_than_one_mib(tmp_path: Path) -> None:
    with pytest.raises(CheckpointError):
        write_evidence(tmp_path, RUN_ID, {"data": "x" * 1_048_576})


def test_write_evidence_rejects_json_deeper_than_64_without_state_changes(
    tmp_path: Path,
) -> None:
    with pytest.raises(CheckpointError):
        write_evidence(tmp_path, RUN_ID, {"data": nested_list(64)})

    assert not (tmp_path / "backlog-drain").exists()


def test_write_evidence_requires_a_private_state_root(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)

    with pytest.raises(CheckpointError):
        write_evidence(tmp_path, RUN_ID, {"ok": True})


def test_read_evidence_rejects_manifest_larger_than_one_mib(tmp_path: Path) -> None:
    data = b'{"data":"' + b"x" * 1_048_576 + b'"}'
    digest = hashlib.sha256(data).hexdigest()
    evidence_dir = tmp_path / "backlog-drain" / RUN_ID / "evidence"
    evidence_dir.mkdir(parents=True, mode=0o700)
    path = evidence_dir / f"{digest}.json"
    path.write_bytes(data)
    path.chmod(0o600)

    with pytest.raises(CheckpointError):
        read_evidence(
            tmp_path,
            RUN_ID,
            {
                "ref": f"evidence/{digest}.json",
                "sha256": digest,
                "size": len(data),
            },
        )


def test_read_evidence_normalizes_parser_recursion_error(tmp_path: Path) -> None:
    data = b'{"data":' + b"[" * 2_000 + b"]" * 2_000 + b"}"
    digest = hashlib.sha256(data).hexdigest()
    evidence_dir = tmp_path / "backlog-drain" / RUN_ID / "evidence"
    evidence_dir.mkdir(parents=True, mode=0o700)
    path = evidence_dir / f"{digest}.json"
    path.write_bytes(data)
    path.chmod(0o600)

    with pytest.raises(CheckpointError):
        read_evidence(
            tmp_path,
            RUN_ID,
            {
                "ref": f"evidence/{digest}.json",
                "sha256": digest,
                "size": len(data),
            },
        )


@pytest.mark.parametrize(
    "reference",
    [
        {"ref": "../outside.json", "sha256": ZERO64, "size": 2},
        {"ref": "/tmp/outside.json", "sha256": ZERO64, "size": 2},
        {"ref": "evidence/file.json", "sha256": ZERO64, "size": True},
        {"ref": "evidence/file.json", "sha256": ZERO64, "size": 16_777_217},
        {"ref": "evidence/file.json", "sha256": ZERO64, "size": 2, "extra": 1},
    ],
)
def test_read_evidence_rejects_traversal_bad_types_caps_and_unknown_keys(
    tmp_path: Path, reference: dict
) -> None:
    with pytest.raises(CheckpointError):
        read_evidence(tmp_path, RUN_ID, reference)


def test_missing_evidence_read_does_not_create_state_directories(tmp_path: Path) -> None:
    reference = {
        "ref": f"evidence/{ZERO64}.json",
        "sha256": ZERO64,
        "size": 0,
    }

    with pytest.raises(CheckpointError):
        read_evidence(tmp_path, RUN_ID, reference)

    assert not (tmp_path / "backlog-drain").exists()


def test_read_evidence_rejects_symlink_and_digest_mismatch(tmp_path: Path) -> None:
    run_root = tmp_path / "backlog-drain" / RUN_ID
    evidence_dir = run_root / "evidence"
    evidence_dir.mkdir(parents=True, mode=0o700)
    outside_data = b'{"ok":true}'
    digest = hashlib.sha256(outside_data).hexdigest()
    outside = tmp_path / "outside.json"
    outside.write_bytes(outside_data)
    outside.chmod(0o600)
    assert outside.read_bytes() == outside_data
    linked = evidence_dir / f"{digest}.json"
    linked.symlink_to(outside)
    symlink_reference = {
        "ref": f"evidence/{digest}.json",
        "sha256": digest,
        "size": len(outside_data),
    }

    with pytest.raises(CheckpointError):
        read_evidence(tmp_path, RUN_ID, symlink_reference)

    reference = write_evidence(tmp_path, RUN_ID, {"other": True})
    wrong = dict(reference, sha256=ONE64)
    with pytest.raises(CheckpointError):
        read_evidence(tmp_path, RUN_ID, wrong)


@pytest.mark.parametrize(
    "raw_log",
    [
        {"ref": "../outside.log", "sha256": ZERO64, "size": 6},
        {"ref": "raw-logs/log.txt", "sha256": ZERO64, "size": True},
        {"ref": "raw-logs/log.txt", "sha256": ZERO64, "size": 16_777_217},
    ],
)
def test_read_evidence_rejects_unsafe_nested_raw_log_reference(
    tmp_path: Path, raw_log: dict
) -> None:
    reference = write_evidence(
        tmp_path,
        RUN_ID,
        {"raw_log": raw_log},
    )

    with pytest.raises(CheckpointError):
        read_evidence(tmp_path, RUN_ID, reference)


def test_read_evidence_verifies_nested_raw_log_bytes(tmp_path: Path) -> None:
    reference = write_evidence(
        tmp_path,
        RUN_ID,
        {
            "raw_log": {
                "ref": "raw-logs/test.log",
                "sha256": hashlib.sha256(b"expected").hexdigest(),
                "size": 8,
            }
        },
    )
    raw_dir = tmp_path / "backlog-drain" / RUN_ID / "raw-logs"
    raw_dir.mkdir(mode=0o700)
    raw_path = raw_dir / "test.log"
    raw_path.write_bytes(b"tampered")
    raw_path.chmod(0o600)

    with pytest.raises(CheckpointError):
        read_evidence(tmp_path, RUN_ID, reference)
