from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess

import pytest

import scripts.backlog_checkpoint.git_evidence as git_evidence
from scripts.backlog_checkpoint.codec import CheckpointError, decode_snapshot
from scripts.backlog_checkpoint.git_evidence import (
    bind_commit,
    collect_sources,
    inspect_worktree,
)
from scripts.backlog_checkpoint.storage import write_snapshot


EMPTY_PATH_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
EMPTY_CHANGE_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name)
    return subprocess.run(
        ["git", *args],
        cwd=root,
        env=environment,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


def _write(root: Path, relative: str, data: bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Fixture User")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _write(root, "base.txt", b"base\n")
    _git(root, "add", "--", "base.txt")
    _git(root, "commit", "-m", "initial")
    return root


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").decode("ascii").strip()


def _commit_all(root: Path, message: str = "change") -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _head(root)


def _checkpoint(root: Path, run_id: str) -> Path:
    path = root / f"HANDOFF.backlog-{run_id}.md"
    snapshot = {
        "schema": 1,
        "run_id": run_id,
        "generation": 0,
        "phase": "IMPLEMENT",
        "phase_status": "started",
        "attempt": 0,
        "next_action": "INDEPENDENT_REVIEW",
        "blockers": [],
        "supervisor_identity": {
            "runtime": "codex",
            "origin_adapter": "codex",
            "host": "fixture",
            "session_fingerprint": "0" * 64,
        },
        "binding": {
            "issue_url": "https://github.com/o/r/issues/31",
            "task_id": "task-31",
            "claim_id": "claim-31",
            "repo_root": str(root),
            "worktree_root": str(root),
        },
        "git": {
            "branch": "main",
            "base_head": "0" * 40,
            "observed_head": _head(root),
            "source_count": 1,
            "path_set_digest": "0" * 64,
            "change_digest": "0" * 64,
            "commit_parent": None,
            "result_commit": None,
        },
        "child": {
            "child_id": None,
            "phase": "IMPLEMENT",
            "attempt": 0,
            "observed_status": "quiescent",
        },
        "evidence": {"ref": f"evidence/{'0' * 64}.json", "sha256": "0" * 64, "size": 0},
    }
    write_snapshot(path, snapshot, expected_generation=None)
    return path


def test_inspect_worktree_returns_exact_canonical_checkout(repo: Path) -> None:
    head = _head(repo)

    assert inspect_worktree(str(repo), "main", str(repo)) == {
        "root": str(repo),
        "realpath": str(repo),
        "branch": "main",
        "worktree_ref": "refs/heads/main",
        "head": head,
    }


@pytest.mark.parametrize("kind", ["alias", "child", "wrong_ref", "wrong_branch"])
def test_inspect_worktree_rejects_coordinate_mismatch(
    repo: Path, tmp_path: Path, kind: str
) -> None:
    root = str(repo)
    expected_ref = str(repo)
    expected_branch = "main"
    if kind == "alias":
        alias = tmp_path / "alias"
        alias.symlink_to(repo, target_is_directory=True)
        root = str(alias)
        expected_ref = str(alias)
    elif kind == "child":
        child = repo / "child"
        child.mkdir()
        root = str(child)
    elif kind == "wrong_ref":
        expected_ref = str(tmp_path / "other")
    else:
        expected_branch = "other"

    with pytest.raises(CheckpointError):
        inspect_worktree(root, expected_branch, expected_ref)


def test_inspect_worktree_rejects_duplicate_branch_checkout(
    repo: Path, tmp_path: Path
) -> None:
    sibling = tmp_path / "sibling"
    _git(repo, "worktree", "add", "--force", str(sibling), "main")

    with pytest.raises(CheckpointError, match="branch"):
        inspect_worktree(str(repo), "main", str(repo))


def test_collect_sources_records_literal_complete_worktree_state(repo: Path) -> None:
    _write(repo, "modified.txt", b"original\n")
    _write(repo, "deleted.txt", b"deleted\n")
    _write(repo, "renamed-old.txt", b"renamed\n")
    executable = _write(repo, "exec.sh", b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o644)
    _commit_all(repo, "source baseline")
    parent = _head(repo)

    _write(repo, "modified.txt", b"modified\n")
    (repo / "deleted.txt").unlink()
    (repo / "renamed-old.txt").rename(repo / "renamed-new.txt")
    executable.chmod(0o755)
    _write(repo, "add.txt", b"added\n")
    _write(repo, ":(glob)*.txt", b"literal\n")
    (repo / "symlink").symlink_to("modified.txt")

    manifest = collect_sources(
        str(repo),
        [
            "renamed-new.txt",
            "modified.txt",
            "future.txt",
            "deleted.txt",
            "exec.sh",
            "add.txt",
            "renamed-old.txt",
            "symlink",
            ":(glob)*.txt",
        ],
    )

    assert manifest == {
        "paths": [
            ":(glob)*.txt",
            "add.txt",
            "deleted.txt",
            "exec.sh",
            "modified.txt",
            "renamed-new.txt",
            "renamed-old.txt",
            "symlink",
        ],
        "count": 8,
        "path_set_digest": "7146a21e954c0d18319f6b22abbfcd60442ef238f43db5264a71a80513e90c4c",
        "change_digest": "b28484aeca44d7fd26a1e96c2980f32263a75ea421b2c8c639e6847e6df45e12",
        "sources": [
            {
                "change": "add",
                "mode": "100644",
                "path": ":(glob)*.txt",
                "sha256": "59b6b9ab8418bc639a3c27157a93a5f8554100cafd34532beea2b027f475acf6",
            },
            {
                "change": "add",
                "mode": "100644",
                "path": "add.txt",
                "sha256": "3428719b7688c78a0cc8ba4b9e80b4e464c815fbccfd4b20695a15ffcefc22af",
            },
            {"change": "delete", "mode": None, "path": "deleted.txt", "sha256": None},
            {
                "change": "modify",
                "mode": "100755",
                "path": "exec.sh",
                "sha256": "306c6ca7407560340797866e077e053627ad409277d1b9da58106fce4cf717cb",
            },
            {
                "change": "modify",
                "mode": "100644",
                "path": "modified.txt",
                "sha256": "4487e24377581c1a43c957c7700c8b49920de7b8500c05590cee74996ef73f42",
            },
            {
                "change": "add",
                "mode": "100644",
                "path": "renamed-new.txt",
                "sha256": "9841f7cf70d5e5b5ad1f5fab17bf790857a7f03f366deba825e3daa32eebc81d",
            },
            {
                "change": "delete",
                "mode": None,
                "path": "renamed-old.txt",
                "sha256": None,
            },
            {
                "change": "add",
                "mode": "120000",
                "path": "symlink",
                "sha256": "42035d7fd3b24da8cbe8d25a59fd5e01c39d1c045a6147ee9734502517632f8b",
            },
        ],
        "parent_head": parent,
    }


def test_collect_sources_omits_unchanged_and_future_allowed_paths(repo: Path) -> None:
    assert collect_sources(str(repo), ["base.txt", "future.txt"]) == {
        "paths": [],
        "count": 0,
        "path_set_digest": EMPTY_PATH_DIGEST,
        "change_digest": EMPTY_CHANGE_DIGEST,
        "sources": [],
        "parent_head": _head(repo),
    }


def test_exact_untracked_checkpoint_is_excluded_for_collect_and_bind(
    repo: Path,
) -> None:
    run_id = "0123456789abcdef0123456789abcdef"
    checkpoint = _checkpoint(repo, run_id)
    _write(repo, "base.txt", b"reviewed\n")

    status = set(_git(repo, "status", "--porcelain=v1", "-z").split(b"\0"))
    assert status == {
        b"",
        b" M base.txt",
        f"?? {checkpoint.name}".encode("ascii"),
    }

    manifest = collect_sources(str(repo), ["base.txt"], str(checkpoint))
    _git(repo, "add", "--", "base.txt")
    _git(repo, "commit", "-m", "change")
    commit_oid = _head(repo)

    assert (
        bind_commit(
            str(repo), manifest["parent_head"], manifest, commit_oid, str(checkpoint)
        )["result_commit"]
        == commit_oid
    )
    assert checkpoint.exists()


@pytest.mark.parametrize("kind", ["foreign", "extra", "tracked", "symlink", "mode"])
def test_checkpoint_exclusion_rejects_nonexact_runtime_artifact(
    repo: Path, tmp_path: Path, kind: str
) -> None:
    run_id = "0123456789abcdef0123456789abcdef"
    checkpoint = _checkpoint(repo, run_id)
    if kind == "foreign":
        checkpoint = tmp_path / checkpoint.name
        checkpoint.write_text("foreign\n", encoding="utf-8")
        checkpoint.chmod(0o600)
    elif kind == "extra":
        _write(repo, "HANDOFF.backlog-fedcba9876543210fedcba9876543210.md", b"extra\n")
    elif kind == "tracked":
        _git(repo, "add", "--", checkpoint.name)
        _git(repo, "commit", "-m", "tracked checkpoint")
    elif kind == "symlink":
        checkpoint.unlink()
        checkpoint.symlink_to(repo / "base.txt")
    else:
        checkpoint.chmod(0o644)
    _write(repo, "base.txt", b"reviewed\n")

    with pytest.raises(CheckpointError):
        collect_sources(str(repo), ["base.txt"], str(checkpoint))


def test_collect_rejects_checkpoint_changed_during_collection(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint(repo, "0123456789abcdef0123456789abcdef")
    _write(repo, "base.txt", b"reviewed\n")
    real_source_records = git_evidence._source_records
    calls = 0

    def change_checkpoint(*args, **kwargs):
        nonlocal calls
        result = real_source_records(*args, **kwargs)
        calls += 1
        if calls == 1:
            checkpoint.chmod(0o644)
        return result

    monkeypatch.setattr(git_evidence, "_source_records", change_checkpoint)

    with pytest.raises(CheckpointError, match="checkpoint"):
        collect_sources(str(repo), ["base.txt"], str(checkpoint))


def test_collect_rejects_checkpoint_aba_mutation_restored_between_validations(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint(repo, "0123456789abcdef0123456789abcdef")
    assert checkpoint.stat().st_uid == os.geteuid()
    original = checkpoint.read_bytes()
    mutated = original.replace(b'"host":"fixture"', b'"host":"changed"')
    assert mutated != original and len(mutated) == len(original)
    decode_snapshot(mutated)
    _write(repo, "base.txt", b"reviewed\n")
    real_source_records = git_evidence._source_records
    calls = 0

    def mutate_and_restore(*args, **kwargs):
        nonlocal calls
        result = real_source_records(*args, **kwargs)
        calls += 1
        if calls == 1:
            checkpoint.write_bytes(mutated)
            checkpoint.write_bytes(original)
            checkpoint.chmod(0o600)
            assert checkpoint.read_bytes() == original
        return result

    monkeypatch.setattr(git_evidence, "_source_records", mutate_and_restore)

    with pytest.raises(CheckpointError, match="checkpoint"):
        collect_sources(str(repo), ["base.txt"], str(checkpoint))


def test_collect_sources_rejects_unknown_dirty_before_reading_it(
    repo: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret"
    secret.write_bytes(b"must not be read")
    (repo / "unknown").symlink_to(secret)

    with pytest.raises(CheckpointError, match="outside the source allowlist"):
        collect_sources(str(repo), ["base.txt"])


def test_collect_sources_rejects_allowed_special_file(repo: Path) -> None:
    fifo = repo / "source.fifo"
    os.mkfifo(fifo)

    with pytest.raises(CheckpointError, match="unsupported"):
        collect_sources(str(repo), ["source.fifo"])


def test_collect_sources_ignores_poisoned_git_environment(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _git(outside, "init", "-b", "outside")
    monkeypatch.setenv("GIT_DIR", str(outside / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outside))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "poison-index"))

    assert collect_sources(str(repo), ["base.txt"])["parent_head"] == _head(repo)


@pytest.mark.parametrize("status", ["MM", "AM"])
def test_collect_sources_rejects_mixed_index_and_worktree_content(
    repo: Path, status: str
) -> None:
    path = "base.txt" if status == "MM" else "added.txt"
    _write(repo, path, b"staged\n")
    _git(repo, "add", "--", path)
    _write(repo, path, b"worktree\n")
    assert _git(repo, "status", "--porcelain=v1", "--", path).startswith(
        status.encode("ascii")
    )

    with pytest.raises(CheckpointError, match="mixed staged and worktree"):
        collect_sources(str(repo), [path])


@pytest.mark.parametrize("status", ["M ", " M"])
def test_collect_sources_accepts_pure_staged_or_worktree_change(
    repo: Path, status: str
) -> None:
    _write(repo, "base.txt", b"pure\n")
    if status == "M ":
        _git(repo, "add", "--", "base.txt")
    assert _git(repo, "status", "--porcelain=v1", "--", "base.txt").startswith(
        status.encode("ascii")
    )

    manifest = collect_sources(str(repo), ["base.txt"])

    assert manifest["sources"] == [
        {
            "change": "modify",
            "mode": "100644",
            "path": "base.txt",
            "sha256": "4fa72219681f01317b0950d822a2bdf3c34908ade11b32abce08d9444d290620",
        }
    ]


def test_bind_commit_accepts_current_unique_direct_exact_commit(repo: Path) -> None:
    _write(repo, "base.txt", b"changed\n")
    manifest = collect_sources(str(repo), ["base.txt"])
    commit_oid = _commit_all(repo)

    assert bind_commit(str(repo), manifest["parent_head"], manifest, commit_oid) == {
        "commit_parent": manifest["parent_head"],
        "result_commit": commit_oid,
        "source_count": 1,
        "path_set_digest": manifest["path_set_digest"],
        "change_digest": manifest["change_digest"],
    }


def test_bind_commit_rejects_boolean_manifest_count(repo: Path) -> None:
    _write(repo, "base.txt", b"changed\n")
    manifest = collect_sources(str(repo), ["base.txt"])
    commit_oid = _commit_all(repo)
    tampered = copy.deepcopy(manifest)
    tampered["count"] = True

    with pytest.raises(CheckpointError, match="manifest"):
        bind_commit(str(repo), manifest["parent_head"], tampered, commit_oid)


@pytest.mark.parametrize("mismatch", ["hook_blob", "extra_path"])
def test_bind_commit_rejects_commit_content_not_in_manifest(
    repo: Path, mismatch: str
) -> None:
    _write(repo, "base.txt", b"reviewed\n")
    manifest = collect_sources(str(repo), ["base.txt"])
    if mismatch == "hook_blob":
        _write(repo, "base.txt", b"hook changed\n")
    else:
        _write(repo, "extra.txt", b"extra\n")
    commit_oid = _commit_all(repo)

    with pytest.raises(CheckpointError, match="manifest"):
        bind_commit(str(repo), manifest["parent_head"], manifest, commit_oid)


def test_bind_commit_rejects_extra_path_before_reading_its_blob(repo: Path) -> None:
    _write(repo, "base.txt", b"reviewed\n")
    manifest = collect_sources(str(repo), ["base.txt"])
    _write(repo, "unowned-secret.txt", b"must not be hashed\n")
    commit_oid = _commit_all(repo)
    extra_oid = (
        _git(repo, "rev-parse", f"{commit_oid}:unowned-secret.txt")
        .decode("ascii")
        .strip()
    )
    object_path = repo / ".git" / "objects" / extra_oid[:2] / extra_oid[2:]
    object_path.unlink()

    with pytest.raises(CheckpointError, match="manifest"):
        bind_commit(str(repo), manifest["parent_head"], manifest, commit_oid)


def test_bind_commit_rejects_head_advanced_by_additional_commit(repo: Path) -> None:
    _write(repo, "base.txt", b"reviewed\n")
    manifest = collect_sources(str(repo), ["base.txt"])
    intended = _commit_all(repo, "intended")
    _write(repo, "later.txt", b"later\n")
    _commit_all(repo, "later")

    with pytest.raises(CheckpointError, match="current HEAD"):
        bind_commit(str(repo), manifest["parent_head"], manifest, intended)


def test_bind_commit_rechecks_head_after_final_collection(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo, "base.txt", b"reviewed\n")
    manifest = collect_sources(str(repo), ["base.txt"])
    intended = _commit_all(repo, "intended")
    tree = _git(repo, "rev-parse", f"{intended}^{{tree}}").decode("ascii").strip()
    later = (
        _git(
            repo,
            "commit-tree",
            tree,
            "-p",
            intended,
            input_bytes=b"later\n",
        )
        .decode("ascii")
        .strip()
    )
    original = git_evidence._matching_direct_children

    def advance_head(root: str, parent_head: str, checked_manifest: dict) -> list[str]:
        result = original(root, parent_head, checked_manifest)
        _git(repo, "update-ref", "refs/heads/main", later, intended)
        return result

    monkeypatch.setattr(git_evidence, "_matching_direct_children", advance_head)

    with pytest.raises(CheckpointError, match="current HEAD"):
        bind_commit(str(repo), manifest["parent_head"], manifest, intended)
    assert _head(repo) == later


def test_bind_commit_rejects_multiple_matching_direct_children(repo: Path) -> None:
    _write(repo, "base.txt", b"reviewed\n")
    manifest = collect_sources(str(repo), ["base.txt"])
    current = _commit_all(repo, "current")
    tree = _git(repo, "rev-parse", f"{current}^{{tree}}").decode("ascii").strip()
    sibling = (
        _git(
            repo,
            "commit-tree",
            tree,
            "-p",
            manifest["parent_head"],
            input_bytes=b"sibling\n",
        )
        .decode("ascii")
        .strip()
    )
    _git(repo, "update-ref", "refs/heads/sibling", sibling)

    with pytest.raises(CheckpointError, match="unique"):
        bind_commit(str(repo), manifest["parent_head"], manifest, current)


def test_bind_commit_ignores_replacement_refs(repo: Path) -> None:
    _write(repo, "base.txt", b"reviewed\n")
    manifest = collect_sources(str(repo), ["base.txt"])
    current = _commit_all(repo, "current")
    parent_tree = (
        _git(repo, "rev-parse", f"{manifest['parent_head']}^{{tree}}")
        .decode("ascii")
        .strip()
    )
    replacement = (
        _git(
            repo,
            "commit-tree",
            parent_tree,
            "-p",
            manifest["parent_head"],
            input_bytes=b"replacement\n",
        )
        .decode("ascii")
        .strip()
    )
    _git(repo, "replace", current, replacement)

    assert (
        bind_commit(str(repo), manifest["parent_head"], manifest, current)[
            "result_commit"
        ]
        == current
    )


def test_bind_commit_rejects_merge_commit(repo: Path) -> None:
    parent = _head(repo)
    _write(repo, "base.txt", b"reviewed\n")
    manifest = collect_sources(str(repo), ["base.txt"])
    regular = _commit_all(repo, "regular")
    tree = _git(repo, "rev-parse", f"{regular}^{{tree}}").decode("ascii").strip()
    second_parent = (
        _git(repo, "commit-tree", tree, "-p", parent, input_bytes=b"second parent\n")
        .decode("ascii")
        .strip()
    )
    merge = (
        _git(
            repo,
            "commit-tree",
            tree,
            "-p",
            parent,
            "-p",
            second_parent,
            input_bytes=b"merge\n",
        )
        .decode("ascii")
        .strip()
    )
    _git(repo, "update-ref", "refs/heads/main", merge)

    with pytest.raises(CheckpointError, match="single parent"):
        bind_commit(str(repo), manifest["parent_head"], manifest, merge)
