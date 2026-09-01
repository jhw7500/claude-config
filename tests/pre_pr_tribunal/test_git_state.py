import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from pre_pr_tribunal.git_state import (
    GitStateError,
    assert_auto_fix_scope,
    capture_snapshot,
    snapshot_matches,
)


GIT = "/usr/bin/git"
GIT_ENV = dict(
    os.environ,
    LC_ALL="C",
    LANG="C",
    GIT_PAGER="cat",
    GIT_OPTIONAL_LOCKS="0",
)


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        [GIT, "-C", str(repo), *args],
        input=input_bytes,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=GIT_ENV,
    ).stdout


def _git_text(repo: Path, *args: str, input_text: str | None = None) -> str:
    value = None if input_text is None else input_text.encode("utf-8")
    return _git(repo, *args, input_bytes=value).decode("ascii").strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "commit", "-qm", message)


def _make_current_head_the_base(repo: Path) -> None:
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")


def test_snapshot_binds_repository_base_head_merge_base_diff_and_paths(git_repo):
    first = capture_snapshot(git_repo, "master", now=lambda: "2026-09-01T00:00:00Z")
    second = capture_snapshot(
        git_repo / ".git" / "..",
        "master",
        now=lambda: "2026-09-01T00:00:00Z",
    )

    assert first == second
    assert first.repository == "jhw7500/claude-config"
    assert first.base_ref == "master"
    assert len(first.base_sha) == 40
    assert len(first.head_sha) == 40
    assert len(first.merge_base_sha) == 40
    assert len(first.diff_sha256) == 64
    assert first.diff_sha256 == first.diff_sha256.lower()
    assert [(item.status, item.path, item.old_path) for item in first.paths] == [
        ("M", "tracked.txt", None)
    ]
    assert first.initial_paths == ("tracked.txt",)
    assert first.to_json() == {
        "schema": 1,
        "repository": "jhw7500/claude-config",
        "base": {"ref": "master", "sha": first.base_sha},
        "head_sha": first.head_sha,
        "merge_base_sha": first.merge_base_sha,
        "diff_sha256": first.diff_sha256,
        "paths": [{"status": "M", "path": "tracked.txt"}],
        "initial_paths": ["tracked.txt"],
        "created_at": "2026-09-01T00:00:00Z",
    }


def test_digest_hashes_the_exact_documented_binary_diff_bytes(git_repo):
    snapshot = capture_snapshot(git_repo, "master")
    exact_diff = subprocess.check_output(
        [
            GIT,
            "-C",
            str(git_repo),
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--full-index",
            snapshot.merge_base_sha + "..HEAD",
        ],
        env=GIT_ENV,
    )

    assert snapshot.diff_sha256 == hashlib.sha256(exact_diff).hexdigest()


def test_dirty_detached_and_missing_remote_base_fail_closed(git_repo):
    (git_repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(GitStateError, match="^WORKTREE_DIRTY$"):
        capture_snapshot(git_repo, "master")

    (git_repo / "dirty.txt").unlink()
    _git(git_repo, "checkout", "--detach", "-q")
    with pytest.raises(GitStateError, match="^DETACHED_HEAD$"):
        capture_snapshot(git_repo, "master")

    _git(git_repo, "checkout", "-q", "feature")
    with pytest.raises(GitStateError, match="^BASE_INVALID$"):
        capture_snapshot(git_repo, "missing")


@pytest.mark.parametrize(
    "base",
    ["", "--upload-pack=x", "-master", "../master", "master^{commit}", "main..x"],
)
def test_invalid_base_is_rejected_before_remote_ref_resolution(git_repo, base):
    with pytest.raises(GitStateError, match="^BASE_INVALID$"):
        capture_snapshot(git_repo, base)


def test_non_git_cwd_and_bare_repository_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(GitStateError, match="^NOT_GIT_REPOSITORY$"):
        capture_snapshot(outside, "master")

    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "--bare", "-q", str(bare))
    with pytest.raises(GitStateError, match="^NOT_GIT_REPOSITORY$"):
        capture_snapshot(bare, "master")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/jhw7500/claude-config",
        "https://github.com/jhw7500/claude-config.git",
        "git@github.com:jhw7500/claude-config",
        "git@github.com:jhw7500/claude-config.git",
        "ssh://git@github.com/jhw7500/claude-config",
        "ssh://git@github.com/jhw7500/claude-config.git",
    ],
)
def test_supported_github_origin_shapes_are_canonicalized(git_repo, url):
    _git(git_repo, "remote", "set-url", "origin", url)

    assert capture_snapshot(git_repo, "master").repository == ("jhw7500/claude-config")


@pytest.mark.parametrize(
    "url",
    [
        "https://token@github.com/jhw7500/claude-config.git",
        "https://github.com/jhw7500/claude-config/extra",
        "https://github.com/jhw7500/claude-config.git?token=x",
        "https://github.com/jhw7500/claude-config.git#fragment",
        "https://gitlab.com/jhw7500/claude-config.git",
        "git@github.com:jhw7500/claude-config/extra.git",
        "ssh://root@github.com/jhw7500/claude-config.git",
        "ssh://git@github.com:22/jhw7500/claude-config.git",
        "ssh://git@github.com/jhw7500/claude-config/extra.git",
        "https://github.com/jhw7500/clau dé-config.git",
    ],
)
def test_unsupported_or_malformed_origin_is_rejected(git_repo, url):
    _git(git_repo, "remote", "set-url", "origin", url)

    with pytest.raises(GitStateError, match="^REPOSITORY_UNSUPPORTED$"):
        capture_snapshot(git_repo, "master")


def test_ignored_review_state_is_clean_but_untracked_source_is_dirty(git_repo):
    review = git_repo / ".review"
    review.mkdir()
    (review / "verdict.json").write_text("{}\n", encoding="utf-8")

    capture_snapshot(git_repo, "master")

    (git_repo / "source.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(GitStateError, match="^WORKTREE_DIRTY$"):
        capture_snapshot(git_repo, "master")


def test_newline_filename_is_nul_parsed_and_json_escaped(git_repo):
    path = "line\nbreak.txt"
    (git_repo / path).write_text("new\n", encoding="utf-8")
    _git(git_repo, "add", "--", path)
    _commit(git_repo, "add newline path")

    snapshot = capture_snapshot(git_repo, "master")
    encoded = json.dumps(snapshot.to_json(), ensure_ascii=False)

    assert [(item.status, item.path) for item in snapshot.paths] == [
        ("A", path),
        ("M", "tracked.txt"),
    ]
    assert snapshot.initial_paths == (path, "tracked.txt")
    assert "line\\nbreak.txt" in encoded
    assert json.loads(encoded)["initial_paths"][0] == path


def test_rename_binds_old_and_new_paths_in_utf8_byte_order(git_repo):
    old_path = "z-old.txt"
    new_path = "a-new.txt"
    (git_repo / old_path).write_text("same contents\n" * 20, encoding="utf-8")
    _git(git_repo, "add", "--", old_path)
    _commit(git_repo, "add rename source")
    _make_current_head_the_base(git_repo)

    _git(git_repo, "mv", "--", old_path, new_path)
    _commit(git_repo, "rename source")
    snapshot = capture_snapshot(git_repo, "master")

    assert [(item.status, item.path, item.old_path) for item in snapshot.paths] == [
        ("R100", new_path, old_path)
    ]
    assert snapshot.initial_paths == (new_path, old_path)


def test_deletion_is_bound(git_repo):
    _make_current_head_the_base(git_repo)
    _git(git_repo, "rm", "-q", "--", "tracked.txt")
    _commit(git_repo, "delete tracked file")

    snapshot = capture_snapshot(git_repo, "master")

    assert [(item.status, item.path) for item in snapshot.paths] == [
        ("D", "tracked.txt")
    ]


def test_executable_mode_only_change_is_bound(git_repo):
    _make_current_head_the_base(git_repo)
    (git_repo / "tracked.txt").chmod(0o755)
    _git(git_repo, "add", "--", "tracked.txt")
    _commit(git_repo, "make tracked executable")

    snapshot = capture_snapshot(git_repo, "master")

    assert [(item.status, item.path) for item in snapshot.paths] == [
        ("M", "tracked.txt")
    ]


def test_symlink_type_change_is_bound(git_repo):
    _make_current_head_the_base(git_repo)
    (git_repo / "tracked.txt").unlink()
    (git_repo / "tracked.txt").symlink_to("target.txt")
    _git(git_repo, "add", "--", "tracked.txt")
    _commit(git_repo, "replace file with symlink")

    snapshot = capture_snapshot(git_repo, "master")

    assert [(item.status, item.path) for item in snapshot.paths] == [
        ("T", "tracked.txt")
    ]


def test_local_submodule_gitlink_change_is_bound(git_repo, tmp_path):
    submodule = tmp_path / "submodule"
    submodule.mkdir()
    _git(submodule, "init", "-q", "-b", "main")
    _git(submodule, "config", "user.name", "Test")
    _git(submodule, "config", "user.email", "test@example.com")
    (submodule / "version.txt").write_text("one\n", encoding="utf-8")
    _git(submodule, "add", ".")
    _commit(submodule, "version one")
    first_commit = _git_text(submodule, "rev-parse", "HEAD")
    (submodule / "version.txt").write_text("two\n", encoding="utf-8")
    _git(submodule, "commit", "-qam", "version two")
    second_commit = _git_text(submodule, "rev-parse", "HEAD")

    _git(
        git_repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(submodule),
        "modules/local",
    )
    _git(git_repo / "modules" / "local", "checkout", "-q", first_commit)
    _git(git_repo, "add", ".gitmodules", "modules/local")
    _commit(git_repo, "add local submodule")
    _make_current_head_the_base(git_repo)

    _git(git_repo / "modules" / "local", "checkout", "-q", second_commit)
    _git(git_repo, "add", "modules/local")
    _commit(git_repo, "advance submodule")
    snapshot = capture_snapshot(git_repo, "master")

    assert [(item.status, item.path) for item in snapshot.paths] == [
        ("M", "modules/local")
    ]


def test_binary_bytes_are_in_the_binding_diff(git_repo):
    binary = git_repo / "binary.dat"
    binary.write_bytes(b"\x00\x01base\xff")
    _git(git_repo, "add", "--", "binary.dat")
    _commit(git_repo, "add binary")
    _make_current_head_the_base(git_repo)

    binary.write_bytes(b"\x00\x02feature\xfe")
    _git(git_repo, "add", "--", "binary.dat")
    _commit(git_repo, "change binary")
    snapshot = capture_snapshot(git_repo, "master")
    exact_diff = subprocess.check_output(
        [
            GIT,
            "-C",
            str(git_repo),
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--full-index",
            snapshot.merge_base_sha + "..HEAD",
        ],
        env=GIT_ENV,
    )

    assert [(item.status, item.path) for item in snapshot.paths] == [
        ("M", "binary.dat")
    ]
    assert b"GIT binary patch" in exact_diff
    assert snapshot.diff_sha256 == hashlib.sha256(exact_diff).hexdigest()


def test_non_utf8_and_disallowed_control_paths_are_rejected(git_repo):
    for raw_name in (b"bad-\xff.txt", b"bad-\x01.txt"):
        _make_current_head_the_base(git_repo)
        raw_path = os.fsencode(git_repo) + b"/" + raw_name
        descriptor = os.open(raw_path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, b"content\n")
        finally:
            os.close(descriptor)
        subprocess.run(
            [os.fsencode(GIT), b"-C", os.fsencode(git_repo), b"add", b"--", raw_name],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=GIT_ENV,
        )
        _commit(git_repo, "add invalid serialization path")

        with pytest.raises(GitStateError, match="^PATH_INVALID$"):
            capture_snapshot(git_repo, "master")


def test_snapshot_staleness_ignores_timestamp_but_detects_new_commit(git_repo):
    before = capture_snapshot(git_repo, "master", now=lambda: "2026-09-01T00:00:00Z")
    same_binding = capture_snapshot(
        git_repo, "master", now=lambda: "2026-09-01T00:00:01Z"
    )
    assert before != same_binding
    assert snapshot_matches(before, same_binding)

    (git_repo / "tracked.txt").write_text("feature two\n", encoding="utf-8")
    _git(git_repo, "commit", "-qam", "change feature again")
    after = capture_snapshot(git_repo, "master")

    assert after.head_sha != before.head_sha
    assert after.diff_sha256 != before.diff_sha256
    assert not snapshot_matches(before, after)


def test_snapshot_becomes_stale_when_remote_base_moves(git_repo):
    before = capture_snapshot(git_repo, "master")
    base = _git_text(git_repo, "rev-parse", "refs/remotes/origin/master^{commit}")
    tree = _git_text(git_repo, "rev-parse", base + "^{tree}")
    moved = _git_text(
        git_repo,
        "commit-tree",
        tree,
        "-p",
        base,
        input_text="moved base\n",
    )
    _git(git_repo, "update-ref", "refs/remotes/origin/master", moved)

    after = capture_snapshot(git_repo, "master")

    assert after.base_sha == moved
    assert not snapshot_matches(before, after)


def test_empty_committed_diff_is_rejected(git_repo):
    _make_current_head_the_base(git_repo)

    with pytest.raises(GitStateError, match="^EMPTY_DIFF$"):
        capture_snapshot(git_repo, "master")


def test_auto_fix_scope_rejects_any_path_outside_round_one(git_repo):
    snapshot = capture_snapshot(git_repo, "master")

    assert_auto_fix_scope(snapshot.initial_paths, ("tracked.txt",))
    assert_auto_fix_scope(snapshot.initial_paths, ())
    with pytest.raises(GitStateError, match="^AUTO_FIX_SCOPE_EXPANDED$"):
        assert_auto_fix_scope(snapshot.initial_paths, ("tracked.txt", "new.txt"))
    with pytest.raises(GitStateError, match="^AUTO_FIX_SCOPE_EXPANDED$"):
        assert_auto_fix_scope(snapshot.initial_paths, ("/absolute.txt",))
