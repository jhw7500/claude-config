import hashlib
import os
from pathlib import Path
import subprocess
import time

import pytest

from pre_pr_tribunal import git_state
from pre_pr_tribunal.git_state import (
    GitStateError,
    assert_auto_fix_scope,
    capture_snapshot,
    diff_contract,
    snapshot_matches,
)
from pre_pr_tribunal.verdict_store import begin_round


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


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def _inject_after_initial_origin(monkeypatch, mutation) -> None:
    original = git_state._run_git
    injected = False

    def run(cwd, *arguments):
        nonlocal injected
        result = original(cwd, *arguments)
        if not injected and arguments == ("remote", "get-url", "origin"):
            injected = True
            mutation()
        return result

    monkeypatch.setattr(git_state, "_run_git", run)


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
    assert first.head_ref == "refs/heads/feature"
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
        "head_ref": "refs/heads/feature",
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
    assert not any(
        "/home/" in value or "/Users/" in value
        for value in contract["environment"].values()
    )


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
    [
        "",
        "--upload-pack=x",
        "-master",
        "../master",
        "master^{commit}",
        "main..x",
        "ma\u0301ster",
    ],
)
def test_invalid_base_is_rejected_before_remote_ref_resolution(git_repo, base):
    with pytest.raises(GitStateError, match="^BASE_INVALID$"):
        capture_snapshot(git_repo, base)


def test_non_nfc_symbolic_head_is_rejected_before_verdict_write(git_repo):
    _git(git_repo, "branch", "-m", "fe\u0301ature")

    with pytest.raises(GitStateError, match="^GIT_STATE_INVALID$"):
        capture_snapshot(git_repo, "master")


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


def test_origin_gh_default_repository_is_accepted(git_repo):
    _git(git_repo, "config", "--local", "remote.origin.gh-resolved", "base")

    assert capture_snapshot(git_repo, "master").repository == (
        "jhw7500/claude-config"
    )


@pytest.mark.parametrize("scope", ("global", "system"))
def test_effective_url_rewrite_to_other_repository_is_rejected(
    git_repo, tmp_path, monkeypatch, scope
):
    isolated_home = tmp_path / "git-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_home / "xdg"))
    config = (
        isolated_home / ".gitconfig"
        if scope == "global"
        else tmp_path / "system.gitconfig"
    )
    config.write_text(
        '[url "https://github.com/other/repository.git"]\n'
        "\tinsteadOf = https://github.com/jhw7500/claude-config.git\n",
        encoding="utf-8",
    )
    if scope == "system":
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))

    with pytest.raises(GitStateError, match="^REPOSITORY_UNSUPPORTED$"):
        capture_snapshot(git_repo, "master")


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("remote.other.gh-resolved", "base"),
        ("remote.origin.gh-resolved", "other"),
    ),
)
def test_non_origin_gh_default_repository_is_rejected(git_repo, key, value):
    _git(
        git_repo,
        "remote",
        "add",
        "other",
        "https://github.com/other/repository.git",
    )
    _git(git_repo, "config", "--local", key, value)

    with pytest.raises(GitStateError, match="^REPOSITORY_UNSUPPORTED$"):
        capture_snapshot(git_repo, "master")


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


@pytest.mark.parametrize(
    "path", ["line\nbreak.txt", "tab\tpath.txt", "cafe\u0301.txt"]
)
def test_verdict_incompatible_path_text_is_rejected(git_repo, path):
    (git_repo / path).write_text("new\n", encoding="utf-8")
    _git(git_repo, "add", "--", path)
    _commit(git_repo, "add incompatible path")

    with pytest.raises(GitStateError, match="^PATH_INVALID$"):
        capture_snapshot(git_repo, "master")


def test_initial_path_count_cannot_exceed_verdict_parser_limit(git_repo):
    _make_current_head_the_base(git_repo)
    for index in range(1025):
        (git_repo / f"path-{index:04d}.txt").write_text("x\n", encoding="ascii")
    _git(git_repo, "add", ".")
    _commit(git_repo, "add too many paths")

    with pytest.raises(GitStateError, match="^GIT_STATE_INVALID$"):
        capture_snapshot(git_repo, "master")


def test_begin_round_never_persists_a_parser_incompatible_snapshot(git_repo):
    path = "line\nbreak.txt"
    (git_repo / path).write_text("new\n", encoding="utf-8")
    _git(git_repo, "add", "--", path)
    _commit(git_repo, "add incompatible path")

    with pytest.raises(GitStateError, match="^PATH_INVALID$"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1)
    assert not (git_repo / ".review/verdict.json").exists()


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


def test_snapshot_staleness_detects_symbolic_branch_change_at_same_commit(git_repo):
    before = capture_snapshot(git_repo, "master")
    _git(git_repo, "branch", "alternate")
    _git(git_repo, "checkout", "-q", "alternate")

    after = capture_snapshot(git_repo, "master")

    assert after.head_sha == before.head_sha
    assert after.diff_sha256 == before.diff_sha256
    assert after.head_ref == "refs/heads/alternate"
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


@pytest.mark.parametrize("redirect", ["", ">&2"])
def test_git_output_cap_terminates_process_group_before_descendant_runs(
    git_repo, tmp_path, monkeypatch, redirect
):
    sentinel = tmp_path / "overflow-survived"
    command = tmp_path / "stream-output.sh"
    _write_executable(
        command,
        "#!/bin/sh\n"
        '( /bin/sleep 0.4; : > "$CAP_SENTINEL" ) &\n'
        f"/usr/bin/head -c 4096 /dev/zero {redirect}\n"
        "/bin/sleep 0.8\n",
    )
    _git(git_repo, "config", "alias.stream-output", f"!{command}")
    monkeypatch.setenv("CAP_SENTINEL", str(sentinel))
    monkeypatch.setattr(git_state, "MAX_GIT_STDOUT_BYTES", 128)
    monkeypatch.setattr(git_state, "MAX_GIT_STDERR_BYTES", 128)
    monkeypatch.setattr(git_state, "GIT_TIMEOUT_SECONDS", 2)

    with pytest.raises(GitStateError, match="^GIT_OUTPUT_LIMIT$"):
        git_state._run_git(git_repo, "stream-output")

    time.sleep(0.55)
    assert not sentinel.exists()


def test_git_timeout_terminates_process_group_before_descendant_runs(
    git_repo, tmp_path, monkeypatch
):
    sentinel = tmp_path / "timeout-survived"
    command = tmp_path / "slow-command.sh"
    _write_executable(
        command,
        '#!/bin/sh\n( /bin/sleep 0.4; : > "$CAP_SENTINEL" ) &\n/bin/sleep 2\n',
    )
    _git(git_repo, "config", "alias.slow-command", f"!{command}")
    monkeypatch.setenv("CAP_SENTINEL", str(sentinel))
    monkeypatch.setattr(git_state, "GIT_TIMEOUT_SECONDS", 0.1)

    with pytest.raises(GitStateError, match="^GIT_COMMAND_FAILED$"):
        git_state._run_git(git_repo, "slow-command")

    time.sleep(0.55)
    assert not sentinel.exists()


class _RecordingPipe:
    def __init__(self, events, name):
        self.events = events
        self.name = name

    def close(self):
        self.events.append(("close", self.name))


class _RecordingProcess:
    pid = 4242

    def __init__(self, events):
        self.events = events
        self.stdout = _RecordingPipe(events, "stdout")
        self.stderr = _RecordingPipe(events, "stderr")

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        return -9

    def kill(self):
        self.events.append(("kill-process", None))


def test_group_kill_precedes_first_wait_and_reap(monkeypatch):
    events = []
    process = _RecordingProcess(events)
    monkeypatch.setattr(
        git_state.os,
        "killpg",
        lambda pid, sig: events.append(("kill-group", sig)),
    )
    monkeypatch.setattr(
        git_state.time,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    git_state._terminate_process_group(process)

    first_wait = next(index for index, event in enumerate(events) if event[0] == "wait")
    group_kill = events.index(("kill-group", git_state.signal.SIGKILL))
    assert events[:3] == [
        ("kill-group", git_state.signal.SIGTERM),
        ("sleep", git_state.GIT_TERMINATION_GRACE_SECONDS),
        ("kill-group", git_state.signal.SIGKILL),
    ]
    assert group_kill < first_wait
    assert not any(event[0] == "kill-group" for event in events[first_wait + 1 :])
    assert events[-2:] == [("close", "stdout"), ("close", "stderr")]


def test_selector_constructor_failure_is_stable_and_prevents_spawn(monkeypatch):
    spawn_calls = []

    def fail_selector():
        raise RuntimeError("selector constructor canary")

    def record_spawn(*args, **kwargs):
        spawn_calls.append((args, kwargs))
        raise AssertionError("child must not spawn")

    monkeypatch.setattr(git_state.selectors, "DefaultSelector", fail_selector)
    monkeypatch.setattr(git_state.subprocess, "Popen", record_spawn)

    with pytest.raises(GitStateError, match="^GIT_COMMAND_FAILED$"):
        git_state._run_git(Path.cwd(), "version")

    assert spawn_calls == []


class _RegistrationFailureSelector:
    def __init__(self):
        self.closed = False

    def register(self, *args, **kwargs):
        raise RuntimeError("selector registration canary")

    def close(self):
        self.closed = True


def test_selector_registration_failure_cleans_real_git_process_group(
    git_repo, tmp_path, monkeypatch
):
    sentinel = tmp_path / "registration-survived"
    command = tmp_path / "registration-child.sh"
    _write_executable(
        command,
        '#!/bin/sh\n( /bin/sleep 0.4; : > "$CAP_SENTINEL" ) &\n/bin/sleep 0.8\n',
    )
    _git(git_repo, "config", "alias.registration-child", f"!{command}")
    selector = _RegistrationFailureSelector()
    monkeypatch.setenv("CAP_SENTINEL", str(sentinel))
    monkeypatch.setattr(
        git_state.selectors,
        "DefaultSelector",
        lambda: selector,
    )

    error = None
    try:
        git_state._run_git(git_repo, "registration-child")
    except Exception as caught:
        error = caught
    time.sleep(0.55)

    assert isinstance(error, GitStateError)
    assert str(error) == "GIT_COMMAND_FAILED"
    assert selector.closed
    assert not sentinel.exists()


class _ReapedThenFailedProcess:
    pid = 4343

    def __init__(self, events):
        self.events = events
        self.returncode = None
        self.stdout = _RecordingPipe(events, "stdout")
        self.stderr = _RecordingPipe(events, "stderr")
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.returncode = 0
        raise RuntimeError("async wait canary")


class _CompletedPipesSelector:
    def __init__(self, events):
        self.events = events
        self.pipes = []
        self.closed = False

    def register(self, pipe, *args):
        self.pipes.append(pipe)

    def get_map(self):
        while self.pipes:
            self.pipes.pop().close()
        return {}

    def close(self):
        self.closed = True
        self.events.append(("close", "selector"))


def test_wait_exception_after_reap_never_signals_process_group(monkeypatch):
    events = []
    process = _ReapedThenFailedProcess(events)
    selector = _CompletedPipesSelector(events)
    monkeypatch.setattr(
        git_state.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        git_state.selectors,
        "DefaultSelector",
        lambda: selector,
    )
    monkeypatch.setattr(
        git_state.os,
        "killpg",
        lambda pid, sig: events.append(("kill-group", sig)),
    )

    with pytest.raises(GitStateError, match="^GIT_COMMAND_FAILED$"):
        git_state._run_git(Path.cwd(), "version")

    assert process.returncode == 0
    assert process.wait_calls == 1
    assert not any(event[0] == "kill-group" for event in events)
    assert set(events[:2]) == {("close", "stdout"), ("close", "stderr")}
    assert events[-1] == ("close", "selector")


class _ValueErrorPath:
    def __fspath__(self):
        raise ValueError("cwd canary must not escape")


@pytest.mark.parametrize(
    ("cwd", "code"),
    [
        ("nul\x00cwd", "PATH_INVALID"),
        ("surrogate\udcffcwd", "PATH_INVALID"),
        (_ValueErrorPath(), "NOT_GIT_REPOSITORY"),
    ],
)
def test_malformed_cwd_is_normalized_to_a_stable_code(cwd, code):
    with pytest.raises(GitStateError, match=f"^{code}$"):
        capture_snapshot(cwd, "master")


def test_repo_local_fsmonitor_is_disabled_without_hiding_origin(git_repo, tmp_path):
    sentinel = tmp_path / "fsmonitor-ran"
    fsmonitor = tmp_path / "fsmonitor.sh"
    _write_executable(
        fsmonitor,
        f"#!/bin/sh\n: > '{sentinel}'\n",
    )
    _git(git_repo, "config", "core.fsmonitor", str(fsmonitor))

    snapshot = capture_snapshot(git_repo, "master")

    assert snapshot.repository == "jhw7500/claude-config"
    assert not sentinel.exists()


def test_mid_capture_symbolic_head_change_fails_closed(git_repo, monkeypatch):
    def change_branch():
        _git(git_repo, "update-ref", "refs/heads/other", "HEAD")
        _git(git_repo, "symbolic-ref", "HEAD", "refs/heads/other")

    _inject_after_initial_origin(monkeypatch, change_branch)

    with pytest.raises(GitStateError, match="^SNAPSHOT_CHANGED$"):
        capture_snapshot(git_repo, "master")


def test_mid_capture_head_sha_change_fails_closed(git_repo, monkeypatch):
    def move_head():
        head = _git_text(git_repo, "rev-parse", "HEAD^{commit}")
        tree = _git_text(git_repo, "rev-parse", head + "^{tree}")
        moved = _git_text(
            git_repo,
            "commit-tree",
            tree,
            "-p",
            head,
            input_text="move head\n",
        )
        _git(git_repo, "update-ref", "HEAD", moved)

    _inject_after_initial_origin(monkeypatch, move_head)

    with pytest.raises(GitStateError, match="^SNAPSHOT_CHANGED$"):
        capture_snapshot(git_repo, "master")


def test_mid_capture_remote_base_change_fails_closed(git_repo, monkeypatch):
    def move_base():
        base = _git_text(git_repo, "rev-parse", "refs/remotes/origin/master^{commit}")
        tree = _git_text(git_repo, "rev-parse", base + "^{tree}")
        moved = _git_text(
            git_repo,
            "commit-tree",
            tree,
            "-p",
            base,
            input_text="move base during capture\n",
        )
        _git(git_repo, "update-ref", "refs/remotes/origin/master", moved)

    _inject_after_initial_origin(monkeypatch, move_base)

    with pytest.raises(GitStateError, match="^SNAPSHOT_CHANGED$"):
        capture_snapshot(git_repo, "master")


def test_mid_capture_origin_change_fails_closed(git_repo, monkeypatch):
    def move_origin():
        _git(
            git_repo,
            "remote",
            "set-url",
            "origin",
            "https://github.com/other/repository.git",
        )

    _inject_after_initial_origin(monkeypatch, move_origin)

    with pytest.raises(GitStateError, match="^SNAPSHOT_CHANGED$"):
        capture_snapshot(git_repo, "master")


def test_mid_capture_dirty_worktree_remains_worktree_dirty(git_repo, monkeypatch):
    original = git_state._run_git
    injected = False

    def dirty_worktree():
        (git_repo / "late-dirty.txt").write_text("dirty\n", encoding="utf-8")

    def run(cwd, *arguments):
        nonlocal injected
        result = original(cwd, *arguments)
        if not injected and arguments[:2] == ("diff", "--binary"):
            injected = True
            dirty_worktree()
        return result

    monkeypatch.setattr(git_state, "_run_git", run)

    with pytest.raises(GitStateError, match="^WORKTREE_DIRTY$"):
        capture_snapshot(git_repo, "master")


def test_both_diff_commands_use_immutable_head_sha_during_drift(git_repo, monkeypatch):
    original = git_state._run_git
    initial_head = _git_text(git_repo, "rev-parse", "HEAD^{commit}")
    base = _git_text(git_repo, "rev-parse", "refs/remotes/origin/master^{commit}")
    merge_base = _git_text(git_repo, "merge-base", base, initial_head)
    ranges = []
    injected = False

    def run(cwd, *arguments):
        nonlocal injected
        result = original(cwd, *arguments)
        if arguments and arguments[0] == "diff":
            ranges.append(arguments[-1])
        if not injected and arguments[:4] == (
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
        ):
            injected = True
            tree = _git_text(git_repo, "rev-parse", initial_head + "^{tree}")
            moved = _git_text(
                git_repo,
                "commit-tree",
                tree,
                "-p",
                initial_head,
                input_text="move between diffs\n",
            )
            _git(git_repo, "update-ref", "HEAD", moved)
        return result

    monkeypatch.setattr(git_state, "_run_git", run)

    with pytest.raises(GitStateError, match="^SNAPSHOT_CHANGED$"):
        capture_snapshot(git_repo, "master")

    assert ranges == [
        f"{merge_base}..{initial_head}",
        f"{merge_base}..{initial_head}",
    ]
