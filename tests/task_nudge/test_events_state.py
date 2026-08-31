from concurrent.futures import ThreadPoolExecutor
import os

import pytest


def test_claude_event_uses_file_path(core, tmp_path):
    payload = {
        "session_id": "session/opaque:value",
        "cwd": str(tmp_path),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "src" / "app.py")},
    }

    event = core.parse_claude_event(payload)

    assert event.runtime is core.Runtime.CLAUDE
    assert event.session_id == "session/opaque:value"
    assert event.target_paths == (tmp_path / "src" / "app.py",)


def test_codex_apply_patch_extracts_every_target(core, tmp_path):
    payload = {
        "session_id": "01a-test",
        "cwd": str(tmp_path),
        "tool_name": "apply_patch",
        "tool_input": {
            "command": "\n".join(
                [
                    "*** Begin Patch",
                    "*** Update File: src/a.py",
                    "*** Add File: src/b.py",
                    "*** Delete File: src/c.py",
                    "*** Move to: src/d.py",
                    "*** End Patch",
                ]
            )
        },
    }

    event = core.parse_codex_event(payload)

    assert event.target_paths == (
        tmp_path / "src" / "a.py",
        tmp_path / "src" / "b.py",
        tmp_path / "src" / "c.py",
        tmp_path / "src" / "d.py",
    )


def test_unparseable_patch_is_conservatively_a_candidate(core, tmp_path):
    payload = {
        "session_id": "01a-test",
        "cwd": str(tmp_path),
        "tool_name": "apply_patch",
        "tool_input": {"command": "not a patch header"},
    }

    assert core.parse_codex_event(payload).target_paths == ()


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        ("parse_claude_event", []),
        ("parse_codex_event", []),
        ("parse_claude_event", {"session_id": "a", "cwd": "/tmp", "tool_name": "Edit", "tool_input": []}),
        ("parse_codex_event", {"session_id": "a", "cwd": "/tmp", "tool_name": "Write", "tool_input": []}),
        ("parse_claude_event", {"cwd": "/tmp", "tool_name": "Edit", "tool_input": {"file_path": "/tmp/a"}}),
        ("parse_codex_event", {"session_id": "a", "tool_name": "Write", "tool_input": {"file_path": "/tmp/a"}}),
        ("parse_claude_event", {"session_id": "a", "cwd": "relative", "tool_name": "Edit", "tool_input": {"file_path": "/tmp/a"}}),
        ("parse_codex_event", {"session_id": "a", "cwd": "/tmp", "tool_name": "Bash", "tool_input": {"file_path": "/tmp/a"}}),
        ("parse_claude_event", {"session_id": "bad" + chr(10), "cwd": "/tmp", "tool_name": "Edit", "tool_input": {"file_path": "/tmp/a"}}),
        ("parse_codex_event", {"session_id": "a", "cwd": "/tmp" + chr(0), "tool_name": "Write", "tool_input": {"file_path": "/tmp/a"}}),
        ("parse_codex_event", {"session_id": "a", "cwd": "/tmp", "tool_name": "apply_patch", "tool_input": {"command": "*** Update File: src/" + chr(9) + "app.py"}}),
    ],
)
def test_invalid_event_payloads_raise_bounded_error(core, parser, payload):
    with pytest.raises(core.NudgeError, match="HOOK_INPUT_INVALID") as error:
        getattr(core, parser)(payload)
    assert error.value.reason == "HOOK_INPUT_INVALID"


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/settings.json",
        ".codex/config.toml",
        "repo/.omc/state.json",
        "repo/memory/context.md",
        "repo/HANDOFF.session.md",
    ],
)
def test_support_paths_are_skipped(core, tmp_path, relative):
    home = tmp_path / "home"
    home.mkdir()
    event = core.HookEvent(core.Runtime.CODEX, "session-a", home / "repo", "Write", (home / relative,))

    assert core.should_skip_event(event, home, {"TMPDIR": str(home / "scratch")})


def test_project_markdown_is_not_blanket_skipped(core, tmp_path):
    home = tmp_path / "home"
    target = home / "repo" / "docs" / "architecture.md"
    event = core.HookEvent(core.Runtime.CODEX, "session-a", target.parent, "Write", (target,))

    assert not core.should_skip_event(event, home, {"TMPDIR": str(home / "scratch")})


def test_unknown_or_project_candidate_never_skips(core, tmp_path):
    home = tmp_path / "home"
    no_target = core.HookEvent(core.Runtime.CODEX, "session-a", home / "repo", "Write", ())
    mixed = core.HookEvent(
        core.Runtime.CODEX,
        "session-a",
        home / "repo",
        "Write",
        (home / ".claude/settings.json", home / "repo/app.py"),
    )

    assert not core.should_skip_event(no_target, home, {"TMPDIR": str(home / "scratch")})
    assert not core.should_skip_event(mixed, home, {"TMPDIR": str(home / "scratch")})


def test_marker_hashes_opaque_session_and_only_one_caller_wins(core, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    env = {"XDG_RUNTIME_DIR": str(runtime)}

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: core.claim_session_marker(core.Runtime.CODEX, "../opaque/session", env=env), range(32)))

    assert results.count(core.MarkerClaim.CLAIMED) == 1
    assert results.count(core.MarkerClaim.ALREADY_DONE) == 31
    marker_names = [path.name for path in runtime.rglob("*") if path.is_file()]
    assert marker_names and all("opaque" not in name and "/" not in name for name in marker_names)


def test_marker_is_private_and_namespaces_runtimes(core, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    env = {"XDG_RUNTIME_DIR": str(runtime)}

    assert core.claim_session_marker(core.Runtime.CLAUDE, "same", env=env) is core.MarkerClaim.CLAIMED
    assert core.claim_session_marker(core.Runtime.CODEX, "same", env=env) is core.MarkerClaim.CLAIMED
    assert core.claim_session_marker(core.Runtime.CLAUDE, "same", env=env) is core.MarkerClaim.ALREADY_DONE
    state_dirs = [path for path in runtime.iterdir() if path.is_dir()]
    assert len(state_dirs) == 1
    assert os.stat(state_dirs[0]).st_mode & 0o777 == 0o700
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in state_dirs[0].iterdir())


def test_unsafe_state_roots_fail_closed(core, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o777)
    runtime.chmod(0o777)
    fallback = tmp_path / "fallback"
    fallback.mkdir(mode=0o777)
    fallback.chmod(0o777)

    result = core.claim_session_marker(core.Runtime.CODEX, "session-a", env={"XDG_RUNTIME_DIR": str(runtime), "TMPDIR": str(fallback)})

    assert result is core.MarkerClaim.UNAVAILABLE


def test_sticky_shared_tmp_root_creates_uid_private_fallback(core, tmp_path):
    shared_tmp = tmp_path / "shared-tmp"
    shared_tmp.mkdir(mode=0o777)
    shared_tmp.chmod(0o1777)

    assert core.claim_session_marker(core.Runtime.CODEX, "fallback", env={"TMPDIR": str(shared_tmp)}) is core.MarkerClaim.CLAIMED
    assert core.claim_session_marker(core.Runtime.CODEX, "fallback", env={"TMPDIR": str(shared_tmp)}) is core.MarkerClaim.ALREADY_DONE
    state_dir = shared_tmp / f"task-nudge-{os.getuid()}"
    assert state_dir.is_dir()
    assert os.stat(state_dir).st_mode & 0o777 == 0o700


def test_relative_xdg_runtime_uses_safe_tmpdir_fallback(core, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir(mode=0o700)
    fallback.chmod(0o700)

    assert core.claim_session_marker(core.Runtime.CODEX, "fallback", env={"XDG_RUNTIME_DIR": "relative", "TMPDIR": str(fallback)}) is core.MarkerClaim.CLAIMED
    assert (fallback / f"task-nudge-{os.getuid()}").is_dir()


def test_relative_tmpdir_is_not_scratch_or_default_fallback(core, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    event = core.HookEvent(core.Runtime.CODEX, "relative-tmp", tmp_path / "repo", "Write", (tmp_path / "repo/app.py",))
    identity = core.RepositoryIdentity(tmp_path / "repo", "owner/repo")
    monkeypatch.setattr(core, "resolve_repository", lambda cwd, runner: identity)
    monkeypatch.setattr(core, "query_registration", lambda identity, home, runner: core.RegistrationResult(core.RegistrationStatus.UNREGISTERED, "owner/repo"))

    assert not core.should_skip_event(event, home, {"TMPDIR": "relative"})
    assert core.claim_session_marker(core.Runtime.CODEX, "relative-tmp", env={"TMPDIR": "relative"}) is core.MarkerClaim.UNAVAILABLE
    assert core.evaluate_event(event, home, {"TMPDIR": "relative"}, runner=object()) == core.RegistrationResult(core.RegistrationStatus.UNKNOWN, "owner/repo", "NUDGE_STATE_UNAVAILABLE")
    assert not list(tmp_path.rglob("task-nudge*"))


def test_symlinked_state_root_and_wrong_owner_fail_closed(core, tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    unsafe_fallback = tmp_path / "unsafe-fallback"
    unsafe_fallback.mkdir(mode=0o777)
    unsafe_fallback.chmod(0o777)
    assert core.claim_session_marker(core.Runtime.CODEX, "session-a", env={"XDG_RUNTIME_DIR": str(linked), "TMPDIR": str(unsafe_fallback)}) is core.MarkerClaim.UNAVAILABLE

    assert core.claim_session_marker(core.Runtime.CODEX, "marker-link", env={"XDG_RUNTIME_DIR": str(target)}) is core.MarkerClaim.CLAIMED
    marker = target / "task-nudge" / core.marker_name(core.Runtime.CODEX, "marker-link")
    marker.unlink()
    marker.symlink_to(tmp_path / "outside")
    assert core.claim_session_marker(core.Runtime.CODEX, "marker-link", env={"XDG_RUNTIME_DIR": str(target)}) is core.MarkerClaim.UNAVAILABLE

    current_uid = os.getuid()
    monkeypatch.setattr(core.os, "getuid", lambda: current_uid + 1)
    assert core.claim_session_marker(core.Runtime.CODEX, "session-b", env={"XDG_RUNTIME_DIR": str(target)}) is core.MarkerClaim.UNAVAILABLE


def test_evaluate_event_claims_only_normal_known_registration(core, tmp_path, monkeypatch):
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    event = core.HookEvent(core.Runtime.CODEX, "session-a", tmp_path / "repo", "Write", (tmp_path / "repo/app.py",))
    identity = core.RepositoryIdentity(tmp_path / "repo", "owner/repo")
    monkeypatch.setattr(core, "resolve_repository", lambda cwd, runner: identity)
    monkeypatch.setattr(core, "query_registration", lambda identity, home, runner: core.RegistrationResult(core.RegistrationStatus.REGISTERED, "owner/repo"))

    env = {"XDG_RUNTIME_DIR": str(runtime), "TMPDIR": str(home / "scratch")}
    assert core.evaluate_event(event, home, env, runner=object()) == core.RegistrationResult(core.RegistrationStatus.REGISTERED, "owner/repo")
    assert core.evaluate_event(event, home, env, runner=object()) is None


def test_evaluate_event_unknown_and_skip_do_not_create_marker(core, tmp_path, monkeypatch):
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    unknown = core.HookEvent(core.Runtime.CODEX, "unknown", tmp_path / "repo", "Write", (tmp_path / "repo/app.py",))
    skipped = core.HookEvent(core.Runtime.CODEX, "skipped", tmp_path / "repo", "Write", (home / ".codex/config.toml",))
    identity = core.RepositoryIdentity(tmp_path / "repo", "owner/repo")
    monkeypatch.setattr(core, "resolve_repository", lambda cwd, runner: identity)
    monkeypatch.setattr(core, "query_registration", lambda identity, home, runner: core.RegistrationResult(core.RegistrationStatus.UNKNOWN, "owner/repo", "PORTFOLIO_UNAVAILABLE"))

    env = {"XDG_RUNTIME_DIR": str(runtime), "TMPDIR": str(home / "scratch")}
    assert core.evaluate_event(unknown, home, env, runner=object()).reason == "PORTFOLIO_UNAVAILABLE"
    assert core.evaluate_event(skipped, home, env, runner=object()) is None
    assert not list(runtime.rglob("*"))


def test_evaluate_event_returns_state_unavailable_with_known_slug(core, tmp_path, monkeypatch):
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir(mode=0o777)
    runtime.chmod(0o777)
    event = core.HookEvent(core.Runtime.CODEX, "session-a", tmp_path / "repo", "Write", (tmp_path / "repo/app.py",))
    identity = core.RepositoryIdentity(tmp_path / "repo", "owner/repo")
    monkeypatch.setattr(core, "resolve_repository", lambda cwd, runner: identity)
    monkeypatch.setattr(core, "query_registration", lambda identity, home, runner: core.RegistrationResult(core.RegistrationStatus.UNREGISTERED, "owner/repo"))

    assert core.evaluate_event(event, home, {"XDG_RUNTIME_DIR": str(runtime), "TMPDIR": str(tmp_path / "missing")}, runner=object()) == core.RegistrationResult(core.RegistrationStatus.UNKNOWN, "owner/repo", "NUDGE_STATE_UNAVAILABLE")
