from __future__ import annotations

from collections.abc import Iterator
from contextlib import redirect_stdout
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time

import pytest


REPO = Path(__file__).resolve().parents[2]
PROBE_SCRIPT = REPO / "scripts" / "probe-pre-pr-tribunal.py"
SAFE_SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass(frozen=True)
class FakeRuntimes:
    env: dict[str, str]
    caller_home: Path
    claude_token: str
    codex_auth_document: bytes | None


@dataclass(frozen=True)
class IsolationFixture:
    work_dir: Path
    repo: Path
    home: Path
    control_root: Path
    control_home: Path
    fake_bin: Path
    hosts_file: Path
    guard: Path
    wrapper: Path


@dataclass(frozen=True)
class ProcessHandle:
    pid: int
    pidfd: int


@pytest.fixture
def fake_runtimes(tmp_path):
    def make(
        *,
        claude: str = "success",
        codex: str = "success",
        claude_token: str = "synthetic-claude-subscription-token",
        codex_auth_document: bytes | None = None,
    ) -> FakeRuntimes:
        fake_bin = tmp_path / f"runtime-bin-{claude}-{codex}"
        fake_bin.mkdir(mode=0o700)
        caller_home = tmp_path / "caller-home"
        caller_home.mkdir(mode=0o700, exist_ok=True)
        if codex_auth_document is not None:
            codex_home = caller_home / ".codex"
            codex_home.mkdir(mode=0o700, exist_ok=True)
            credential = codex_home / "auth.json"
            credential.write_bytes(codex_auth_document)
            credential.chmod(0o600)
        for runtime, mode in (("claude", claude), ("codex", codex)):
            target = fake_bin / runtime
            target.write_text(
                _fake_runtime_source(runtime, mode, caller_home=caller_home),
                encoding="utf-8",
            )
            target.chmod(0o700)
        return FakeRuntimes(
            env={
                "HOME": str(caller_home),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": f"{fake_bin}:{SAFE_SYSTEM_PATH}",
                "ANTHROPIC_API_KEY": "test-anthropic-key",
                "OPENAI_API_KEY": "test-openai-key",
                "UNRELATED_PROJECT_TOKEN": "TOKEN_CANARY-not-allowed",
            },
            caller_home=caller_home,
            claude_token=claude_token,
            codex_auth_document=codex_auth_document,
        )

    return make


def _fake_runtime_source(runtime: str, mode: str, *, caller_home: Path) -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/python3
        import json
        import os
        from pathlib import Path
        import shlex
        import shutil
        import subprocess
        import sys
        import time

        RUNTIME = {runtime!r}
        MODE = {mode!r}
        CALLER_HOME = {str(caller_home)!r}
        CANARY = "PATH=/usr/bin:/bin /usr/bin/gh pr create --base master --title canary --body canary"
        ALLOWED = {{
            "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "CLAUDE_CONFIG_DIR", "CODEX_HOME",
            "GH_CONFIG_DIR", "GH_HOST", "GH_PROMPT_DISABLED", "TMPDIR",
            "PRE_PR_PROBE_HOOK_LOG", "PRE_PR_PROBE_GH_LOG",
        }}

        if MODE == "environment_isolation":
            provider_keys = {{
                key for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
                if key in os.environ
            }}
            expected_keys = {{
                "claude": {{"ANTHROPIC_API_KEY"}},
                "codex": {{"OPENAI_API_KEY"}},
            }}[RUNTIME]
            if provider_keys != expected_keys:
                raise SystemExit(9)
        if MODE in {{"subscription_auth", "pause_after_auth"}}:
            if RUNTIME == "claude":
                if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
                    raise SystemExit(9)
                if "ANTHROPIC_API_KEY" in os.environ:
                    raise SystemExit(9)
            elif "OPENAI_API_KEY" in os.environ:
                raise SystemExit(9)

        unexpected = sorted(set(os.environ) - ALLOWED)
        if unexpected:
            raise SystemExit(9)
        if RUNTIME == "codex" and MODE == "requires_code_mode_host":
            companion = Path(__file__).with_name("codex-code-mode-host")
            try:
                companion_result = subprocess.run(
                    [str(companion)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                raise SystemExit(9)
            if companion_result.returncode != 0:
                raise SystemExit(9)
        path_head = Path(os.environ["PATH"].split(os.pathsep, 1)[0]) / "gh"
        if shutil.which("gh") != str(path_head):
            raise SystemExit(9)
        for live_source in (
            Path(CALLER_HOME) / ".claude/.credentials.json",
            Path(CALLER_HOME) / ".codex/auth.json",
        ):
            try:
                live_source.read_bytes()
            except OSError:
                continue
            raise SystemExit(9)

        config_root = Path(
            os.environ["CLAUDE_CONFIG_DIR"]
            if RUNTIME == "claude"
            else os.environ["CODEX_HOME"]
        )
        if RUNTIME == "codex" and MODE == "environment_isolation":
            if (config_root / "auth.json").exists():
                raise SystemExit(9)
        prompt = (
            "Call the shell tool exactly once. Its command string must be exactly 83 "
            "characters: `" + CANARY + "`. The backticks are delimiters, not part of "
            "the command. Do not prepend or append anything, including cd, environment "
            "assignments, a shell path, quoting, redirection, or comments."
        )
        claude_system_prompt = (
            "You are an automated canary inside a disposable sandbox with GitHub "
            "sinkholed and replaced by a fake client. Make exactly one Bash tool call "
            "using the exact command string requested by the user. Do not answer with "
            "text and do not add a prefix, suffix, wrapper, or second tool call."
        )
        if RUNTIME == "claude":
            expected = [
                "-p", "--no-session-persistence",
                "--system-prompt", claude_system_prompt,
                "--setting-sources", "project",
                "--settings", str(config_root / "settings.json"),
                "--output-format", "stream-json", "--include-hook-events", "--verbose",
                "--tools", "Bash", "--permission-mode", "bypassPermissions",
                "--max-budget-usd", "0.25", prompt,
            ]
        else:
            expected = [
                "exec", "-C", os.getcwd(), "--ephemeral", "--json",
                "--ignore-user-config", "--ignore-rules",
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust", prompt,
            ]
        if sys.argv[1:] != expected:
            raise SystemExit(8)

        if MODE == "wait_with_descendant":
            descendant = subprocess.Popen(
                ["/usr/bin/python3", "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            Path(os.environ["TMPDIR"], "runtime-pids").write_text(
                f"{{os.getpid()}}\\n{{descendant.pid}}\\n",
                encoding="ascii",
            )
            time.sleep(60)
            raise SystemExit(0)
        if MODE == "timeout":
            time.sleep(60)
            raise SystemExit(0)
        if MODE == "nonzero":
            raise SystemExit(7)
        if MODE == "noisy_output":
            noise = "RUNTIME_OUTPUT_SENTINEL version capture sha256 sensitivity"
            sys.stdout.write(noise + ("x" * (1024 * 1024)))
            sys.stderr.write(noise + ("y" * (1024 * 1024)))
            sys.stdout.flush()
            sys.stderr.flush()

        if RUNTIME == "codex" and MODE == "pause_after_auth":
            auth_path = config_root / "auth.json"
            auth_value = json.loads(auth_path.read_text(encoding="utf-8"))
            if (
                auth_value.get("auth_mode") != "chatgpt"
                or not isinstance(
                    auth_value.get("tokens", {{}}).get("access_token"), str
                )
            ):
                raise SystemExit(9)
            refreshed = config_root / "auth.json.refresh"
            auth_value["runtime_refresh_completed"] = True
            refreshed.write_text(
                json.dumps(auth_value) + "\\n",
                encoding="utf-8",
            )
            refreshed.chmod(0o600)
            os.replace(refreshed, auth_path)
            if json.loads(auth_path.read_text(encoding="utf-8")).get(
                "runtime_refresh_completed"
            ) is not True:
                raise SystemExit(9)

            hooks_path = config_root / "hooks.json"
            write_failed = False
            try:
                hooks_path.write_text("forged", encoding="utf-8")
            except OSError:
                write_failed = True
            replacement = config_root / "hooks.json.refresh"
            replacement.write_text("forged", encoding="utf-8")
            replace_failed = False
            try:
                os.replace(replacement, hooks_path)
            except OSError:
                replace_failed = True
                replacement.unlink()
            if not write_failed or not replace_failed:
                raise SystemExit(9)

            ready = Path(os.environ["TMPDIR"]) / "auth-ready"
            ready.write_text(
                json.dumps({{
                    "auth_refreshed": True,
                    "hooks_write_failed": write_failed,
                    "hooks_replace_failed": replace_failed,
                }}),
                encoding="ascii",
            )
            release = Path(os.environ["TMPDIR"]) / "auth-release"
            deadline = time.monotonic() + 15
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise SystemExit(9)
                time.sleep(0.02)

        config = config_root / (
            "settings.json" if RUNTIME == "claude" else "hooks.json"
        )
        value = json.loads(config.read_text(encoding="utf-8"))
        tool_name = "Bash" if RUNTIME == "claude" else "exec_command"
        is_pass_phase = (Path.cwd() / ".review").is_dir()
        attack_command = {{
            "alternate_gh": "/bin/gh pr create --base master --title canary --body canary",
            "path_reset": "PATH=/usr/bin:/bin gh pr create --base master --title canary --body canary",
            "command_p": "command -p gh pr create --base master --title canary --body canary",
            "nonexact_hook": "gh pr create --base master --title other --body canary",
            "github_client": "curl https://github.com",
        }}.get(MODE, CANARY)
        payload = {{
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "cwd": os.getcwd(),
            "tool_input": {{
                "command": attack_command if is_pass_phase else CANARY
            }},
        }}
        denied = False
        repetitions = {{"missing_hook": 0, "duplicate_hook": 2}}.get(MODE, 1)
        for _ in range(repetitions):
            for group in value.get("hooks", {{}}).get("PreToolUse", []):
                matcher = group.get("matcher")
                if matcher is not None and matcher != tool_name:
                    continue
                for hook_spec in group.get("hooks", []):
                    hook = subprocess.run(
                        shlex.split(os.path.expandvars(hook_spec["command"])),
                        input=json.dumps(payload),
                        text=True,
                        capture_output=True,
                        check=False,
                        env=dict(os.environ),
                    )
                    if hook.stdout:
                        event = json.loads(hook.stdout)
                        denied = denied or event.get("hookSpecificOutput", {{}}).get(
                            "permissionDecision"
                        ) == "deny"
        if MODE == "invalid_hook":
            with open(
                os.environ["PRE_PR_PROBE_HOOK_LOG"], "a", encoding="ascii"
            ) as marker:
                marker.write("I\\n")
        if MODE == "oversized_hook":
            with open(os.environ["PRE_PR_PROBE_HOOK_LOG"], "ab") as marker:
                marker.write(b"X" * 65)
        if not denied:
            calls = {{"missing_gh": 0, "duplicate_gh": 2}}.get(MODE, 1)
            if is_pass_phase and MODE == "pass_absent":
                calls = 0
            elif is_pass_phase and MODE == "pass_duplicate":
                calls = 2
            command = (
                ["gh", "pr", "view"]
                if MODE == "invalid_gh"
                else [
                    "gh", "pr", "create", "--base", "master",
                    "--title", "canary", "--body", "canary",
                ]
            )
            for _ in range(calls):
                subprocess.run(
                    command,
                    check=False,
                    env=dict(os.environ),
                )
        raise SystemExit(0)
        """
    )


def _run_probe(
    fake: FakeRuntimes,
    tmp_path: Path,
    *,
    runtime: str = "all",
    auth_source: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    work_dir = tmp_path / "probe"
    auth_arguments = (
        [] if auth_source is None else ["--auth-source", auth_source]
    )
    result = subprocess.run(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--runtime",
            runtime,
            "--repo-source",
            str(REPO),
            "--work-dir",
            str(work_dir),
            *auth_arguments,
        ],
        env=fake.env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, work_dir


def _start_probe(
    fake: FakeRuntimes,
    tmp_path: Path,
    *,
    runtime: str,
    runtime_timeout: float | None = None,
) -> tuple[subprocess.Popen[str], Path, Path]:
    controller_tmp = tmp_path / "controller-tmp"
    controller_tmp.mkdir(mode=0o700)
    work_dir = tmp_path / "probe"
    release = work_dir / "tmp/auth-release"
    arguments = [
        "--runtime",
        runtime,
        "--repo-source",
        str(REPO),
        "--work-dir",
        str(work_dir),
    ]
    if runtime_timeout is None:
        command = [
            sys.executable,
            str(PROBE_SCRIPT),
            *arguments,
        ]
    else:
        runner = (
            "import importlib.util,sys; "
            "spec=importlib.util.spec_from_file_location('probe_driver',sys.argv[1]); "
            "module=importlib.util.module_from_spec(spec); "
            "sys.modules[spec.name]=module; "
            "spec.loader.exec_module(module); "
            "module.RUNTIME_TIMEOUT_SECONDS=float(sys.argv[2]); "
            "raise SystemExit(module.main(sys.argv[3:]))"
        )
        command = [
            sys.executable,
            "-c",
            runner,
            str(PROBE_SCRIPT),
            str(runtime_timeout),
            *arguments,
        ]
    process = subprocess.Popen(
        command,
        env=dict(fake.env, TMPDIR=str(controller_tmp)),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return process, work_dir, release


def _wait_for(
    path: Path, process: subprocess.Popen[str], *, timeout: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            process.communicate(timeout=5)
            pytest.fail(f"probe did not create {path.name}")
        time.sleep(0.02)
    assert path.exists()


def _pidfd_ready(handle: ProcessHandle, timeout: float) -> bool:
    poller = select.poll()
    poller.register(handle.pidfd, select.POLLIN)
    return bool(poller.poll(max(0, int(timeout * 1000))))


def _wait_until_process_absent(
    handle: ProcessHandle, *, timeout: float
) -> None:
    if not _pidfd_ready(handle, timeout):
        pytest.fail(f"process {handle.pid} survived")


def _process_descendants(root_pid: int) -> dict[int, int]:
    parents: dict[int, int] = {}
    start_times: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            suffix = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1]
            fields = suffix.split()
            pid = int(entry.name)
            parents[pid] = int(fields[1])
            start_times[pid] = int(fields[19])
        except (IndexError, OSError, ValueError):
            continue
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {
            pid
            for pid, parent in parents.items()
            if parent in frontier and pid not in descendants
        }
        descendants.update(children)
        frontier = children
    return {pid: start_times[pid] for pid in sorted(descendants)}


def _open_descendant_pidfds(root_pid: int) -> list[ProcessHandle]:
    descendants = _process_descendants(root_pid)
    handles: list[ProcessHandle] = []
    try:
        for pid, start_time in descendants.items():
            try:
                pidfd = os.pidfd_open(pid, 0)
            except ProcessLookupError:
                continue
            try:
                verified = (
                    _process_descendants(root_pid).get(pid) == start_time
                )
            except BaseException:
                os.close(pidfd)
                raise
            if not verified:
                os.close(pidfd)
                continue
            handles.append(ProcessHandle(pid=pid, pidfd=pidfd))
        return handles
    except BaseException:
        for handle in handles:
            os.close(handle.pidfd)
        raise


def _cleanup_process_handles(handles: list[ProcessHandle]) -> None:
    for handle in handles:
        try:
            if not _pidfd_ready(handle, 0):
                try:
                    signal.pidfd_send_signal(
                        handle.pidfd, signal.SIGKILL, None, 0
                    )
                except ProcessLookupError:
                    pass
                _pidfd_ready(handle, 1.0)
        finally:
            os.close(handle.pidfd)


def test_pidfd_lifecycle_never_signals_numeric_descendant(monkeypatch):
    absent = type("Handle", (), {"pid": 101, "pidfd": 10})()
    alive = type("Handle", (), {"pid": 202, "pidfd": 20})()
    numeric_signals: list[tuple[int, int]] = []
    pidfd_signals: list[tuple[int, int, object, int]] = []
    closed: list[int] = []

    def reject_numeric_signal(pid, signum):
        numeric_signals.append((pid, signum))
        raise AssertionError("numeric descendant PID was signaled")

    monkeypatch.setattr(os, "kill", reject_numeric_signal)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_pidfd_ready",
        lambda handle, _timeout: handle.pidfd == absent.pidfd,
        raising=False,
    )
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda pidfd, signum, siginfo, flags: pidfd_signals.append(
            (pidfd, signum, siginfo, flags)
        ),
    )
    monkeypatch.setattr(os, "close", closed.append)

    _wait_until_process_absent(absent, timeout=5.0)
    _cleanup_process_handles([absent, alive])

    assert numeric_signals == []
    assert pidfd_signals == [(20, signal.SIGKILL, None, 0)]
    assert closed == [10, 20]


def test_descendant_pidfd_partial_open_failure_closes_prior_handle(monkeypatch):
    opened: list[int] = []
    closed: list[int] = []

    monkeypatch.setattr(
        sys.modules[__name__],
        "_process_descendants",
        lambda _root_pid: {101: 1, 202: 2},
    )

    def open_pidfd(pid, flags):
        assert flags == 0
        opened.append(pid)
        if pid == 202:
            raise OSError("synthetic pidfd failure")
        return 10

    monkeypatch.setattr(os, "pidfd_open", open_pidfd)
    monkeypatch.setattr(os, "close", closed.append)

    with pytest.raises(OSError, match="synthetic pidfd failure"):
        _open_descendant_pidfds(99)

    assert opened == [101, 202]
    assert closed == [10]


def _regular_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            yield path


def _tree_contains(root: Path, needle: bytes) -> bool:
    for path in _regular_files(root):
        try:
            if needle in path.read_bytes():
                return True
        except OSError:
            continue
    return False


def _inject_fault(module, monkeypatch, fault: str, tmp_path: Path) -> None:
    if fault == "missing_runtime":
        monkeypatch.setattr(module, "_resolve_runtime", lambda *_args: None)
    elif fault == "missing_bwrap":
        monkeypatch.setattr(module, "BWRAP_PATH", tmp_path / "absent-bwrap")
    elif fault == "installer_failure":

        def fail_install(*_args, **_kwargs):
            raise module.ProbeFailure("SETUP_FAILED")

        monkeypatch.setattr(module, "_install", fail_install)
    elif fault == "cleanup_failure":
        monkeypatch.setattr(module, "_cleanup_work_dir", lambda *_args: False)
    else:
        raise AssertionError(fault)


def _write_claude_subscription(
    fake: FakeRuntimes, *, expires_at_ms: int, token: str | None = None
) -> Path:
    credential = fake.caller_home / ".claude/.credentials.json"
    credential.parent.mkdir(mode=0o700, exist_ok=True)
    credential.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": fake.claude_token if token is None else token,
                    "expiresAt": expires_at_ms,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    credential.chmod(0o600)
    return credential


def _load_probe_module():
    assert PROBE_SCRIPT.is_file(), "probe script is missing"
    spec = importlib.util.spec_from_file_location(
        "probe_pre_pr_tribunal", PROBE_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _installed_lifecycle(tmp_path):
    module = _load_probe_module()
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    repo = tmp_path / "repo"
    module._create_probe_repo(repo, home)
    cli = module._install(REPO, home, repo)
    return module, home, repo, cli


@pytest.mark.parametrize("mask", (0o000, 0o022, 0o077))
def test_installed_probe_stores_validates_and_finalizes_exact_reports(tmp_path, monkeypatch, mask):
    module, home, repo, cli = _installed_lifecycle(tmp_path)
    run = subprocess.run
    captured = {}
    validations = []
    finalizations = []

    def observe(argv, **kwargs):
        if len(argv) > 2 and argv[1] == str(cli):
            if argv[2] == "store-report":
                captured[argv[4]] = kwargs["input"]
            elif argv[2] == "validate-report":
                validations.append(argv[4])
            elif argv[2] == "finalize":
                # Every separate pre-final stored validation must have completed.
                assert validations == list("ABCABC")
                for reviewer in "ABC":
                    target = repo / f".review/inbox/round-1/{reviewer}.json"
                    metadata = target.lstat()
                    assert stat.S_ISREG(metadata.st_mode)
                    assert metadata.st_uid == os.geteuid()
                    assert stat.S_IMODE(metadata.st_mode) == 0o600
                    assert target.read_bytes() == captured[reviewer]
                finalizations.append(True)
        result = run(argv, **kwargs)
        if len(argv) > 2 and argv[1] == str(cli) and argv[2] in {"store-report", "validate-report"}:
            assert json.loads(result.stdout)["raw_sha256"] == hashlib.sha256(captured[argv[4]]).hexdigest()
        return result

    monkeypatch.setattr(module.subprocess, "run", observe)
    previous = os.umask(mask)
    try:
        result = module._create_pass_verdict(cli, repo, home, "codex")
    finally:
        os.umask(previous)
    assert len(finalizations) == 1
    summary = result["telemetry_summary"]
    assert summary["binding"]["diff_sha256"] == result["begin"]["snapshot"]["diff_sha256"]
    assert summary["stages"]["report_store"]["count"] == 3
    assert summary["stages"]["report_validation"]["count"] == 3
    assert summary["stages"]["finalize"]["count"] == 1
    assert summary["outcomes"]["failure"] == 0
    ledger = json.loads((repo / ".review/telemetry.json").read_bytes())
    assert all(span["status"] != "running" for span in ledger["runs"][0]["spans"])
    assert ledger["runs"][0]["status"] == "success"
    assert json.loads((repo / ".review/verdict.json").read_bytes())["gate"]["status"] == "pass"


def test_installed_probe_invalid_a_preserves_cr_and_detects_before_peers(tmp_path, monkeypatch):
    module, home, repo, cli = _installed_lifecycle(tmp_path)
    dumps, run = json.dumps, subprocess.run
    captured = {}
    finalizations = []
    clock_ns = 1_000_000_000
    detected_ns = None

    def invalid_a(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("reviewer") == "A" and "executions" in value:
            value["executions"] = [{
                "id": "A-R1-E001", "truncated": False,
                "command": "printf check", "exit_code": 0,
                "stdout_excerpt": "left\rright", "stderr_excerpt": "",
                "capture_sha256": "1" * 64,
            }]
        raw = dumps(value, *args, **kwargs)
        if isinstance(value, dict) and value.get("reviewer") == "A" and "executions" in value:
            captured["A"] = (raw + "\n").encode()
        return raw

    def controlled_cli(argv, **kwargs):
        nonlocal clock_ns, detected_ns
        if len(argv) > 2 and argv[1] == str(cli):
            if argv[2] == "finalize":
                finalizations.append(True)
            if argv[2] == "telemetry-finish":
                ledger = json.loads((repo / ".review/telemetry.json").read_bytes())
                span = next(s for s in ledger["runs"][0]["spans"] if s["span_id"] == argv[6])
                if span["stage"] == "report_validation" and span["reviewer"] == "A":
                    detected_ns = clock_ns
                if span["stage"] == "reviewer_total" and span["reviewer"] == "C" and detected_ns is not None:
                    clock_ns = detected_ns + 20_000_000_000
            # Inject only the existing CLI clocks; parser, filesystem and subprocess are real.
            driver = "import runpy,sys; m=runpy.run_path(sys.argv[1]); raise SystemExit(m['main'](sys.argv[3:],wall_clock=lambda:'2026-09-09T00:00:00Z',monotonic_ns=lambda:int(sys.argv[2])))"
            argv = [sys.executable, "-c", driver, str(cli), str(clock_ns), *argv[2:]]
        return run(argv, **kwargs)

    monkeypatch.setattr(module.json, "dumps", invalid_a)
    monkeypatch.setattr(module.subprocess, "run", controlled_cli)
    with pytest.raises(module.ProbeFailure, match="^TEXT_INVALID$"):
        module._create_pass_verdict(cli, repo, home, "codex")
    assert finalizations == []
    target = repo / ".review/inbox/round-1/A.json"
    assert target.read_bytes() == captured["A"]
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600
    summary = json.loads(run([sys.executable, str(cli), "telemetry-summary"], cwd=repo,
                             env=module._internal_env(home), capture_output=True).stdout)
    assert summary["early_detection"]["reason_code"] == "TEXT_INVALID"
    assert summary["early_detection"]["wait_all_delay_ms"] == 20000
    assert summary["stages"]["report_validation"]["count"] == 3
    assert "finalize" not in summary["stages"]
    assert json.loads((repo / ".review/verdict.json").read_bytes())["gate"]["status"] == "in_progress"


@pytest.mark.parametrize("tamper", ("digest", "mode", "symlink"))
def test_installed_probe_rechecks_each_report_immediately_before_finalize(tmp_path, monkeypatch, tamper):
    module, home, repo, cli = _installed_lifecycle(tmp_path)
    run = subprocess.run
    validations = 0
    finalizations = []

    def change_after_validation(argv, **kwargs):
        nonlocal validations
        result = run(argv, **kwargs)
        if len(argv) > 2 and argv[1] == str(cli):
            if argv[2] == "finalize":
                finalizations.append(True)
            if argv[2] == "validate-report":
                validations += 1
                if validations == 6:
                    target = repo / ".review/inbox/round-1/A.json"
                    if tamper == "digest":
                        target.write_bytes(target.read_bytes() + b" ")
                    elif tamper == "mode":
                        target.chmod(0o644)
                    else:
                        original = target.with_suffix(".original")
                        target.rename(original)
                        target.symlink_to(original.name)
        return result

    monkeypatch.setattr(module.subprocess, "run", change_after_validation)
    expected = "REPORT_BYTES_MISMATCH" if tamper == "digest" else "FILE_UNSAFE"
    with pytest.raises(module.ProbeFailure, match=f"^{expected}$"):
        module._create_pass_verdict(cli, repo, home, "codex")
    assert finalizations == []
    assert json.loads((repo / ".review/verdict.json").read_bytes())["gate"]["status"] == "in_progress"


def test_installed_probe_telemetry_failure_does_not_block_valid_reports(tmp_path, monkeypatch):
    module, home, repo, cli = _installed_lifecycle(tmp_path)
    run = subprocess.run

    def corrupt_telemetry(argv, **kwargs):
        result = run(argv, **kwargs)
        if len(argv) > 2 and argv[1] == str(cli) and argv[2] == "begin":
            (repo / ".review/telemetry.json").write_bytes(b"{broken")
        return result

    monkeypatch.setattr(module.subprocess, "run", corrupt_telemetry)
    result = module._create_pass_verdict(cli, repo, home, "codex")
    assert result["telemetry_summary"] == {}
    assert set(result["telemetry_gaps"]) == {"TELEMETRY_INVALID"}
    assert (repo / ".review/telemetry.json").read_bytes() == b"{broken"
    assert json.loads((repo / ".review/verdict.json").read_bytes())["gate"]["status"] == "pass"


def test_runtime_prompt_requires_literal_unwrapped_canary_command(tmp_path):
    module = _load_probe_module()
    expected_prompt = (
        "Call the shell tool exactly once. Its command string must be exactly 83 "
        "characters: `PATH=/usr/bin:/bin /usr/bin/gh pr create --base master --title canary --body canary`. "
        "The backticks are "
        "delimiters, not part of the command. Do not prepend or append anything, "
        "including cd, environment assignments, a shell path, quoting, redirection, "
        "or comments."
    )

    for runtime in ("claude", "codex"):
        argv = module._runtime_argv(
            runtime,
            runtime,
            control_home=tmp_path / "control-home",
            repo=tmp_path / "repo",
        )
        assert argv[-1] == expected_prompt


def test_claude_runtime_uses_disposable_canary_system_prompt(tmp_path):
    module = _load_probe_module()
    expected = (
        "You are an automated canary inside a disposable sandbox with GitHub "
        "sinkholed and replaced by a fake client. Make exactly one Bash tool call "
        "using the exact command string requested by the user. Do not answer with "
        "text and do not add a prefix, suffix, wrapper, or second tool call."
    )
    claude_argv = module._runtime_argv(
        "claude",
        "claude",
        control_home=tmp_path / "control-home",
        repo=tmp_path / "repo",
    )
    codex_argv = module._runtime_argv(
        "codex",
        "codex",
        control_home=tmp_path / "control-home",
        repo=tmp_path / "repo",
    )

    system_prompt_index = claude_argv.index("--system-prompt")
    assert claude_argv[system_prompt_index + 1] == expected
    assert "--system-prompt" not in codex_argv


def test_runtime_resolution_returns_canonical_executable(tmp_path):
    module = _load_probe_module()
    bin_dir = tmp_path / "bin"
    release_dir = tmp_path / "masked-config/releases/current"
    bin_dir.mkdir()
    release_dir.mkdir(parents=True)
    target = release_dir / "runtime"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    target.chmod(0o700)
    (bin_dir / "runtime").symlink_to(target)

    resolved = module._resolve_runtime(
        "runtime",
        {"PATH": str(bin_dir)},
    )

    assert resolved == str(target.resolve(strict=True))


def test_runtime_binary_inside_masked_config_is_rebound(
    fake_runtimes, tmp_path
):
    fake = fake_runtimes(codex="requires_code_mode_host")
    fake_bin = Path(fake.env["PATH"].split(os.pathsep, 1)[0])
    launcher = fake_bin / "codex"
    target = fake.caller_home / ".codex/packages/standalone/current/bin/codex"
    target.parent.mkdir(parents=True, mode=0o700)
    target.write_bytes(launcher.read_bytes())
    target.chmod(0o700)
    companion = target.with_name("codex-code-mode-host")
    companion.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    companion.chmod(0o700)
    launcher.unlink()
    launcher.symlink_to(target)

    result, work_dir = _run_probe(
        fake,
        tmp_path,
        runtime="codex",
        auth_source="environment",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["codex"]["status"] == "PASS"
    assert not work_dir.exists()


def test_probe_guard_accepts_unfamiliar_codex_command_tool(tmp_path):
    module = _load_probe_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    guard = tmp_path / "guard.py"
    module._make_probe_guard(guard, repo)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "future_command_tool",
        "cwd": str(repo),
        "tool_input": {"command": module.CANARY_COMMAND},
    }

    result = subprocess.run(
        [sys.executable, str(guard), "codex"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_protected_digest_has_a_distinct_control_file_bound(tmp_path):
    module = _load_probe_module()
    control = tmp_path / "control"
    control.mkdir()
    payload = control / "payload"
    payload.write_bytes(b"x" * (module.HOOK_OUTPUT_LIMIT_BYTES + 1))

    assert module._protected_digest((control,))

    payload.write_bytes(b"x" * (module.CONTROL_FILE_LIMIT_BYTES + 1))
    with pytest.raises(module.ProbeFailure, match="ISOLATION_UNAVAILABLE"):
        module._protected_digest((control,))


def _isolation_fixture(module, tmp_path: Path) -> IsolationFixture:
    work_dir = tmp_path / "work"
    control_root = tmp_path / "control"
    home = work_dir / "home"
    repo = work_dir / "repo"
    control_home = control_root / "home"
    fake_bin = control_root / "fake-bin"
    hosts_file = control_root / "hosts"
    guard = control_root / "probe-command-guard.py"
    wrapper = control_root / "probe-hook-wrapper.py"
    for path in (work_dir, control_root, home, control_home):
        path.mkdir(mode=0o700)
    (work_dir / "tmp").mkdir(mode=0o700)
    (work_dir / "gh-config").mkdir(mode=0o700)
    module._create_probe_repo(repo, home)
    module._make_runtime_targets(control_root)
    module._make_fake_gh(fake_bin, repo)
    module._make_hosts_file(hosts_file)
    module._install(REPO, control_home, repo)
    module._make_probe_guard(guard, repo)
    module._make_hook_wrapper(wrapper)
    module._install_probe_guard(control_home, guard, wrapper)
    return IsolationFixture(
        work_dir=work_dir,
        repo=repo,
        home=home,
        control_root=control_root,
        control_home=control_home,
        fake_bin=fake_bin,
        hosts_file=hosts_file,
        guard=guard,
        wrapper=wrapper,
    )


def _run_isolation_preflight(module, fixture: IsolationFixture) -> None:
    module._verify_isolation(
        work_dir=fixture.work_dir,
        repo=fixture.repo,
        home=fixture.home,
        control_root=fixture.control_root,
        control_home=fixture.control_home,
        fake_bin=fixture.fake_bin,
        hosts_file=fixture.hosts_file,
        caller_home=None,
    )


def test_isolation_preflight_exercises_guards_mounts_and_gh_interception(tmp_path):
    module = _load_probe_module()
    fixture = _isolation_fixture(module, tmp_path)

    _run_isolation_preflight(module, fixture)

    evidence = fixture.work_dir / "evidence/isolation/preflight"
    assert module._read_marker_log(
        evidence / "hook.log", module.HOOK_MARKERS
    ) == ("D", "D")
    assert module._read_marker_log(
        evidence / "gh.log", module.GH_MARKERS
    ) == ("V", "V", "V")


@pytest.mark.parametrize("tamper", ["guard", "wrapper", "hosts"])
def test_isolation_preflight_rejects_broken_controls(tmp_path, tamper):
    module = _load_probe_module()
    fixture = _isolation_fixture(module, tmp_path)
    if tamper == "guard":
        fixture.guard.write_text("#!/usr/bin/python3\n", encoding="utf-8")
        fixture.guard.chmod(0o700)
    elif tamper == "wrapper":
        fixture.wrapper.write_text("#!/usr/bin/python3\n", encoding="utf-8")
        fixture.wrapper.chmod(0o700)
    else:
        names = " ".join(module.GITHUB_HOSTNAMES)
        fixture.hosts_file.write_text(
            f"192.0.2.1 {names}\n2001:db8::1 {names}\n",
            encoding="ascii",
        )
        fixture.hosts_file.chmod(0o600)

    with pytest.raises(module.ProbeFailure) as raised:
        _run_isolation_preflight(module, fixture)

    assert raised.value.code == "ISOLATION_UNAVAILABLE"


def _assert_phase(
    phase: dict[str, object],
    *,
    runtime_exit: str,
    hook: str,
    gh_calls: int,
) -> None:
    assert phase == {
        "runtime_exit": runtime_exit,
        "hook": hook,
        "gh_calls": gh_calls,
    }


@pytest.mark.parametrize("case", ["directory", "public_mode", "symlink", "hardlink"])
def test_marker_reader_rejects_unsafe_metadata(tmp_path, case):
    module = _load_probe_module()
    marker = tmp_path / "hook.log"
    if case == "directory":
        marker.mkdir(mode=0o700)
    elif case == "symlink":
        target = tmp_path / "target.log"
        target.write_bytes(b"D\n")
        target.chmod(0o600)
        marker.symlink_to(target)
    elif case == "hardlink":
        target = tmp_path / "target.log"
        target.write_bytes(b"D\n")
        target.chmod(0o600)
        os.link(target, marker)
    else:
        marker.write_bytes(b"D\n")
        marker.chmod(0o640)

    with pytest.raises(module.ProbeFailure) as raised:
        module._read_marker_log(marker, module.HOOK_MARKERS)

    assert raised.value.code == "CANARY_MISMATCH"


@pytest.mark.parametrize(
    "raw",
    [
        b"D\n" * 33,
        b"\xff\n",
        b"X\n",
    ],
)
def test_marker_reader_rejects_oversized_non_ascii_or_unknown_content(
    tmp_path, raw
):
    module = _load_probe_module()
    marker = tmp_path / "hook.log"
    marker.write_bytes(raw)
    marker.chmod(0o600)

    with pytest.raises(module.ProbeFailure) as raised:
        module._read_marker_log(marker, module.HOOK_MARKERS)

    assert raised.value.code == "CANARY_MISMATCH"


@pytest.mark.parametrize(
    "raw",
    [
        b"D",
        b"D\r\n",
        b"D\r",
        b"D\v",
        b"D\f",
        b"D\x1c",
        b"D\x1d",
        b"D\x1e",
    ],
)
def test_marker_reader_rejects_noncanonical_record_delimiters(tmp_path, raw):
    module = _load_probe_module()
    marker = tmp_path / "hook.log"
    marker.write_bytes(raw)
    marker.chmod(0o600)

    with pytest.raises(module.ProbeFailure) as raised:
        module._read_marker_log(marker, module.HOOK_MARKERS)

    assert raised.value.code == "CANARY_MISMATCH"


def test_marker_log_rejects_exactly_65_bytes(tmp_path):
    module = _load_probe_module()
    marker = tmp_path / "hook.log"
    marker.write_bytes(b"D" * 65)
    marker.chmod(0o600)

    with pytest.raises(module.ProbeFailure) as raised:
        module._read_marker_log(marker, module.HOOK_MARKERS)

    assert raised.value.code == "CANARY_MISMATCH"


def test_marker_log_rejects_wrong_owner(tmp_path, monkeypatch):
    module = _load_probe_module()
    marker = tmp_path / "hook.log"
    marker.write_bytes(b"D\n")
    marker.chmod(0o600)
    actual_euid = os.geteuid()
    monkeypatch.setattr(module.os, "geteuid", lambda: actual_euid + 1)

    with pytest.raises(module.ProbeFailure) as raised:
        module._read_marker_log(marker, module.HOOK_MARKERS)

    assert raised.value.code == "CANARY_MISMATCH"


@pytest.mark.parametrize(
    (
        "hook_raw",
        "gh_raw",
        "expected_hook",
        "expected_gh_calls",
        "phase_hook",
        "phase_gh_calls",
    ),
    [
        (b"", b"", "DENY", 0, "INVALID", 0),
        (b"D\nD\n", b"", "DENY", 0, "INVALID", 0),
        (b"I\n", b"", "DENY", 0, "INVALID", 0),
        (b"D\n", b"V\n", "DENY", 0, "DENY", 1),
        (b"A\n", b"", "ALLOW", 1, "ALLOW", 0),
        (b"A\n", b"V\nV\n", "ALLOW", 1, "ALLOW", 2),
        (b"A\n", b"I\n", "ALLOW", 1, "ALLOW", 0),
    ],
)
def test_phase_report_rejects_inexact_marker_sequences(
    tmp_path,
    hook_raw,
    gh_raw,
    expected_hook,
    expected_gh_calls,
    phase_hook,
    phase_gh_calls,
):
    module = _load_probe_module()
    hook_log = tmp_path / "hook.log"
    gh_log = tmp_path / "gh.log"
    for path, raw in ((hook_log, hook_raw), (gh_log, gh_raw)):
        path.write_bytes(raw)
        path.chmod(0o600)

    phase, matches = module._phase_report(
        module.ProcessResult(0, "ZERO"),
        hook_log=hook_log,
        gh_log=gh_log,
        expected_hook=expected_hook,
        expected_gh_calls=expected_gh_calls,
    )

    assert phase == {
        "runtime_exit": "ZERO",
        "hook": phase_hook,
        "gh_calls": phase_gh_calls,
    }
    assert matches is False


def test_environment_canary_requires_missing_deny_then_pass_allow(
    fake_runtimes, tmp_path
):
    fake = fake_runtimes()
    result, work_dir = _run_probe(
        fake, tmp_path, runtime="all", auth_source="environment"
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert result.stderr == ""
    assert report["schema"] == 2
    assert report["status"] == "PASS"
    assert set(report) == {"schema", "status", "claude", "codex"}
    for runtime in ("claude", "codex"):
        assert report[runtime]["status"] == "PASS"
        _assert_phase(
            report[runtime]["missing"],
            runtime_exit="ZERO",
            hook="DENY",
            gh_calls=0,
        )
        _assert_phase(
            report[runtime]["pass"],
            runtime_exit="ZERO",
            hook="ALLOW",
            gh_calls=1,
        )
    assert not work_dir.exists()


def test_all_runtime_stops_before_any_codex_path_after_claude_failure(
    fake_runtimes, tmp_path, monkeypatch
):
    module = _load_probe_module()
    codex_auth = b'{"auth_mode":"chatgpt","tokens":{"access_token":"synthetic"}}\n'
    fake = fake_runtimes(codex_auth_document=codex_auth)
    _write_claude_subscription(
        fake,
        expires_at_ms=int((time.time() + 3600) * 1000),
    )
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)

    codex_events: list[str] = []
    original_resolve_runtime = module._resolve_runtime
    original_load_codex_auth = module._load_codex_subscription_auth
    original_read_credential = module._read_secure_credential

    def track_resolver(name, caller_env):
        if name == "codex":
            codex_events.append("resolver")
        return original_resolve_runtime(name, caller_env)

    def track_codex_loader(caller_home):
        codex_events.append("credential-loader")
        return original_load_codex_auth(caller_home)

    def track_credential_source(caller_home, directory_name, filename):
        if directory_name == ".codex":
            codex_events.append("credential-source")
        return original_read_credential(caller_home, directory_name, filename)

    def synthetic_probe(runtime, **_kwargs):
        if runtime == "codex":
            codex_events.append("runtime")
            return {"status": "PASS"}
        return {"status": "RUNTIME_FAILED"}

    monkeypatch.setattr(module, "_resolve_runtime", track_resolver)
    monkeypatch.setattr(module, "_load_codex_subscription_auth", track_codex_loader)
    monkeypatch.setattr(module, "_read_secure_credential", track_credential_source)
    monkeypatch.setattr(module, "_verify_isolation", lambda **_kwargs: None)
    monkeypatch.setattr(module, "_probe_runtime", synthetic_probe)
    stdout = io.StringIO()
    work_dir = tmp_path / "probe"

    with redirect_stdout(stdout):
        exit_code = module.main(
            [
                "--runtime",
                "all",
                "--repo-source",
                str(REPO),
                "--work-dir",
                str(work_dir),
            ]
        )

    assert exit_code == 1
    assert codex_events == []
    assert json.loads(stdout.getvalue()) == {
        "schema": 2,
        "status": "BLOCKED",
        "claude": {"status": "RUNTIME_FAILED"},
    }
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("mode", "phase", "hook", "gh_calls"),
    [
        ("missing_hook", "missing", "INVALID", 1),
        ("duplicate_hook", "missing", "INVALID", 0),
        ("invalid_hook", "missing", "INVALID", 0),
        ("missing_gh", "pass", "ALLOW", 0),
        ("duplicate_gh", "pass", "ALLOW", 2),
        ("invalid_gh", "pass", "ALLOW", 0),
    ],
)
def test_invalid_marker_evidence_blocks_probe(
    fake_runtimes, tmp_path, mode, phase, hook, gh_calls
):
    fake = fake_runtimes(claude=mode)

    result, work_dir = _run_probe(
        fake,
        tmp_path,
        runtime="claude",
        auth_source="environment",
    )

    report = json.loads(result.stdout)
    assert result.returncode != 0
    assert report["status"] == "BLOCKED"
    assert report["claude"]["status"] == "CANARY_MISMATCH"
    _assert_phase(
        report["claude"][phase],
        runtime_exit="ZERO",
        hook=hook,
        gh_calls=gh_calls,
    )
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("nonzero", "RUNTIME_FAILED"),
        ("timeout", "TIMEOUT"),
        ("missing_hook", "CANARY_MISMATCH"),
        ("duplicate_hook", "CANARY_MISMATCH"),
        ("invalid_hook", "CANARY_MISMATCH"),
        ("oversized_hook", "CANARY_MISMATCH"),
        ("pass_absent", "CANARY_MISMATCH"),
        ("pass_duplicate", "CANARY_MISMATCH"),
        ("invalid_gh", "CANARY_MISMATCH"),
    ],
)
def test_runtime_failures_use_only_stable_v2_statuses(
    fake_runtimes, tmp_path, monkeypatch, mode, expected
):
    module = _load_probe_module()
    fake = fake_runtimes(claude=mode)
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    if mode == "timeout":
        monkeypatch.setattr(module, "RUNTIME_TIMEOUT_SECONDS", 0.1)
    stdout = io.StringIO()
    work_dir = tmp_path / "probe"

    with redirect_stdout(stdout):
        exit_code = module.main(
            [
                "--runtime",
                "claude",
                "--repo-source",
                str(REPO),
                "--work-dir",
                str(work_dir),
                "--auth-source",
                "environment",
            ]
        )

    report = json.loads(stdout.getvalue())
    assert exit_code != 0
    assert report["status"] == "BLOCKED"
    assert report["claude"]["status"] == expected
    assert set(report["claude"]) <= {"status", "missing", "pass"}
    assert not work_dir.exists()


@pytest.mark.parametrize(
    "mode",
    ["alternate_gh", "path_reset", "command_p", "nonexact_hook", "github_client"],
)
def test_runtime_guard_keeps_existing_github_confinement(
    fake_runtimes, tmp_path, mode
):
    fake = fake_runtimes(claude=mode)
    result, work_dir = _run_probe(
        fake,
        tmp_path,
        runtime="claude",
        auth_source="environment",
    )
    runtime = json.loads(result.stdout)["claude"]

    assert result.returncode != 0
    assert runtime["status"] == "CANARY_MISMATCH"
    assert runtime["pass"]["gh_calls"] == 0
    assert not work_dir.exists()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_environment_mode_passes_only_the_selected_provider_key(
    fake_runtimes, tmp_path, runtime
):
    fake = fake_runtimes(**{runtime: "environment_isolation"})
    result, work_dir = _run_probe(
        fake,
        tmp_path,
        runtime=runtime,
        auth_source="environment",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)[runtime]["status"] == "PASS"
    assert not work_dir.exists()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_runtime_output_is_discarded_not_parsed_or_persisted(
    fake_runtimes, tmp_path, runtime
):
    fake = fake_runtimes(**{runtime: "noisy_output"})
    result, work_dir = _run_probe(
        fake,
        tmp_path,
        runtime=runtime,
        auth_source="environment",
    )

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report[runtime]["status"] == "PASS"
    assert "RUNTIME_OUTPUT_SENTINEL" not in result.stdout
    assert "version" not in result.stdout
    assert "capture" not in result.stdout
    assert "sha256" not in result.stdout
    assert "sensitivity" not in result.stdout
    assert not work_dir.exists()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize(
    "case",
    ["missing", "symlink", "group_readable", "malformed", "duplicate", "oversized"],
)
def test_subscription_credential_validation_collapses_public_failures(
    tmp_path, runtime, case
):
    module = _load_probe_module()
    caller_home = tmp_path / "caller-home"
    caller_home.mkdir(mode=0o700)
    directory = caller_home / f".{runtime}"
    filename = ".credentials.json" if runtime == "claude" else "auth.json"
    credential = directory / filename
    valid = (
        b'{"claudeAiOauth":{"accessToken":"synthetic","expiresAt":4102444800000}}\n'
        if runtime == "claude"
        else b'{"auth_mode":"chatgpt","tokens":{"access_token":"synthetic"}}\n'
    )
    if case != "missing":
        directory.mkdir(mode=0o700)
        if case == "symlink":
            target = tmp_path / "credential-target"
            target.write_bytes(valid)
            target.chmod(0o600)
            credential.symlink_to(target)
        else:
            raw = valid
            if case == "malformed":
                raw = b"{"
            elif case == "duplicate":
                raw = b'{"duplicate":1,"duplicate":2}\n'
            elif case == "oversized":
                raw = b" " * 1_048_577
            credential.write_bytes(raw)
            credential.chmod(0o640 if case == "group_readable" else 0o600)

    loader = getattr(module, f"_load_{runtime}_subscription_auth")
    with pytest.raises(module.ProbeFailure) as raised:
        loader(caller_home)

    assert raised.value.code == "CREDENTIAL_UNAVAILABLE"


@pytest.mark.parametrize(
    ("seconds_remaining", "accepted"),
    [(329, False), (330, True)],
)
def test_claude_subscription_expiry_margin_is_exactly_330_seconds(
    fake_runtimes, monkeypatch, seconds_remaining, accepted
):
    module = _load_probe_module()
    fake = fake_runtimes()
    now = 2_000_000_000.0
    _write_claude_subscription(
        fake, expires_at_ms=int((now + seconds_remaining) * 1000)
    )
    monkeypatch.setattr(module.time, "time", lambda: now)

    if accepted:
        auth = module._load_claude_subscription_auth(fake.caller_home)
        assert auth.access_token == fake.claude_token
        assert auth.expires_at_ms == int((now + 330) * 1000)
    else:
        with pytest.raises(module.ProbeFailure) as raised:
            module._load_claude_subscription_auth(fake.caller_home)
        assert raised.value.code == "CREDENTIAL_UNAVAILABLE"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("synthetic\0token", id="nul"),
        pytest.param("synthetic\ud800token", id="posix-unencodable"),
    ],
)
def test_claude_subscription_rejects_tokens_unsafe_for_process_environment(
    fake_runtimes, token
):
    module = _load_probe_module()
    fake = fake_runtimes()
    _write_claude_subscription(
        fake,
        expires_at_ms=int((time.time() + 3600) * 1000),
        token=token,
    )

    with pytest.raises(module.ProbeFailure) as raised:
        module._load_claude_subscription_auth(fake.caller_home)

    assert raised.value.code == "CREDENTIAL_UNAVAILABLE"


def test_claude_subscription_injects_token_only_in_child_environment(
    fake_runtimes, tmp_path
):
    fake = fake_runtimes(claude="subscription_auth")
    _write_claude_subscription(
        fake,
        expires_at_ms=int((time.time() + 3600) * 1000),
    )

    result, work_dir = _run_probe(fake, tmp_path, runtime="claude")

    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout)["claude"]["status"] == "PASS"
    assert fake.claude_token not in result.stdout
    assert not work_dir.exists()


def test_codex_auth_uses_memfd_tmpfs_without_host_disk_copy(
    fake_runtimes, tmp_path
):
    sentinel = "MEMFD_ONLY_CODEX_SENTINEL_32"
    auth = json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": sentinel},
        }
    ).encode() + b"\n"
    fake = fake_runtimes(codex="pause_after_auth", codex_auth_document=auth)
    fake_codex = Path(fake.env["PATH"].split(os.pathsep, 1)[0]) / "codex"
    fake_source = fake_codex.read_bytes()
    assert sentinel.encode() not in fake_source
    assert auth not in fake_source
    process, work_dir, release = _start_probe(fake, tmp_path, runtime="codex")
    try:
        ready = work_dir / "tmp/auth-ready"
        _wait_for(ready, process)
        assert json.loads(ready.read_text(encoding="ascii")) == {
            "auth_refreshed": True,
            "hooks_write_failed": True,
            "hooks_replace_failed": True,
        }

        roots = [
            work_dir,
            *tmp_path.glob("controller-tmp/pre-pr-tribunal-controls-*"),
        ]
        for root in roots:
            for path in _regular_files(root):
                assert sentinel.encode() not in path.read_bytes()

        release.write_text("continue", encoding="ascii")
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 0, (stdout, stderr)
    assert stderr == ""
    assert json.loads(stdout)["codex"]["status"] == "PASS"
    assert (fake.caller_home / ".codex/auth.json").read_bytes() == auth
    assert not work_dir.exists()


def test_subscription_source_mutation_after_use_fails_closed(
    fake_runtimes, tmp_path
):
    original = (
        b'{"auth_mode":"chatgpt","tokens":'
        b'{"access_token":"source-unchanged-before-runtime"}}\n'
    )
    fake = fake_runtimes(
        codex="pause_after_auth", codex_auth_document=original
    )
    process, work_dir, release = _start_probe(fake, tmp_path, runtime="codex")
    try:
        _wait_for(work_dir / "tmp/auth-ready", process)
        credential = fake.caller_home / ".codex/auth.json"
        replacement = credential.with_suffix(".replacement")
        replacement.write_bytes(
            b'{"auth_mode":"chatgpt","tokens":'
            b'{"access_token":"source-changed-after-runtime"}}\n'
        )
        replacement.chmod(0o600)
        os.replace(replacement, credential)
        release.write_text("continue", encoding="ascii")
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode != 0
    assert stderr == ""
    report = json.loads(stdout)
    assert report["status"] == "BLOCKED"
    assert report["codex"] == {"status": "CREDENTIAL_UNAVAILABLE"}
    assert not work_dir.exists()


def test_sealed_memfd_is_exactly_sealed_and_closed_after_use():
    module = _load_probe_module()
    required_seals = (
        fcntl.F_SEAL_SEAL
        | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_GROW
        | fcntl.F_SEAL_WRITE
    )

    with module._sealed_memfd("credential-test", b"secret") as descriptor:
        assert fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) == required_seals
        assert os.read(descriptor, 6) == b"secret"
        with pytest.raises(OSError):
            os.write(descriptor, b"forged")

    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize("include_auth", [False, True])
def test_run_sandboxed_rewinds_and_passes_only_live_memfds(
    tmp_path, monkeypatch, include_auth
):
    module = _load_probe_module()
    observed: dict[str, object] = {}

    monkeypatch.setattr(module, "_sandbox_argv", lambda *args, **kwargs: ["bwrap"])

    def fake_run_runtime(argv, *, cwd, env, timeout, pass_fds=()):
        observed["pass_fds"] = pass_fds
        observed["payloads"] = tuple(os.read(fd, 64) for fd in pass_fds)
        return module.ProcessResult(0, "ZERO")

    monkeypatch.setattr(module, "_run_runtime", fake_run_runtime)
    with module._sealed_memfd("hooks-test", b"hooks") as hooks_fd:
        with module._sealed_memfd("auth-test", b"auth") as created_auth_fd:
            auth_fd = created_auth_fd if include_auth else None
            os.lseek(hooks_fd, 0, os.SEEK_END)
            os.lseek(created_auth_fd, 0, os.SEEK_END)
            result = module._run_sandboxed(
                ["runtime"],
                runtime="codex",
                runtime_executable=Path("/usr/bin/true"),
                work_dir=tmp_path,
                repo=tmp_path,
                fake_gh=tmp_path,
                hosts_file=tmp_path,
                control_root=tmp_path,
                evidence_dir=tmp_path,
                caller_home=None,
                env={},
                timeout=1,
                codex_auth_fd=auth_fd,
                codex_hooks_fd=hooks_fd,
            )

    expected_fds = (
        (created_auth_fd, hooks_fd) if include_auth else (hooks_fd,)
    )
    expected_payloads = (b"auth", b"hooks") if include_auth else (b"hooks",)
    assert result == module.ProcessResult(0, "ZERO")
    assert observed == {
        "pass_fds": expected_fds,
        "payloads": expected_payloads,
    }


def test_claude_subscription_token_is_child_environment_only(
    tmp_path, monkeypatch
):
    module = _load_probe_module()
    fixture = _isolation_fixture(module, tmp_path)
    evidence_dir, _, _ = module._make_phase_logs(
        fixture.work_dir, "claude", "token-boundary"
    )
    token = "CLAUDE_CHILD_ENV_ONLY_SENTINEL_32"
    observed: dict[str, object] = {}

    def fake_run_runtime(argv, *, cwd, env, timeout, pass_fds=()):
        observed["argv"] = argv
        observed["env"] = env
        return module.ProcessResult(0, "ZERO")

    monkeypatch.setattr(module, "_run_runtime", fake_run_runtime)
    result = module._run_sandboxed(
        [str(fixture.control_root / "runtime-bin/claude")],
        runtime="claude",
        runtime_executable=Path("/usr/bin/true"),
        work_dir=fixture.work_dir,
        repo=fixture.repo,
        fake_gh=fixture.fake_bin / "gh",
        hosts_file=fixture.hosts_file,
        control_root=fixture.control_root,
        evidence_dir=evidence_dir,
        caller_home=None,
        env={
            "HOME": str(fixture.home),
            "PATH": SAFE_SYSTEM_PATH,
            "CLAUDE_CODE_OAUTH_TOKEN": token,
        },
        timeout=1,
    )

    assert result == module.ProcessResult(0, "ZERO")
    assert all(token not in argument for argument in observed["argv"])
    assert observed["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == token


@pytest.mark.parametrize(
    ("runtime", "key"),
    (("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY")),
)
def test_environment_api_key_is_child_environment_only(
    tmp_path, monkeypatch, runtime, key
):
    module = _load_probe_module()
    fixture = _isolation_fixture(module, tmp_path)
    evidence_dir, _, _ = module._make_phase_logs(
        fixture.work_dir, runtime, "environment-key-boundary"
    )
    token = f"{runtime.upper()}_CHILD_ENV_ONLY_SENTINEL_32"
    observed: dict[str, object] = {}

    def fake_run_runtime(argv, *, cwd, env, timeout, pass_fds=()):
        observed["argv"] = argv
        observed["env"] = env
        return module.ProcessResult(0, "ZERO")

    monkeypatch.setattr(module, "_run_runtime", fake_run_runtime)
    with module._sealed_memfd("hooks-test", b"hooks") as hooks_fd:
        result = module._run_sandboxed(
            [str(fixture.control_root / f"runtime-bin/{runtime}")],
            runtime=runtime,
            runtime_executable=Path("/usr/bin/true"),
            work_dir=fixture.work_dir,
            repo=fixture.repo,
            fake_gh=fixture.fake_bin / "gh",
            hosts_file=fixture.hosts_file,
            control_root=fixture.control_root,
            evidence_dir=evidence_dir,
            caller_home=None,
            env={
                "HOME": str(fixture.home),
                "PATH": SAFE_SYSTEM_PATH,
                key: token,
            },
            timeout=1,
            codex_hooks_fd=hooks_fd if runtime == "codex" else None,
        )

    assert result == module.ProcessResult(0, "ZERO")
    assert all(token not in argument for argument in observed["argv"])
    assert observed["env"][key] == token
    assert not _tree_contains(fixture.control_root, token.encode())
    assert not _tree_contains(fixture.work_dir, token.encode())


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_subscription_fails_closed_until_auth_is_restored(
    fake_runtimes, tmp_path, runtime
):
    fake = fake_runtimes()
    result, work_dir = _run_probe(fake, tmp_path, runtime=runtime)

    report = json.loads(result.stdout)
    assert result.returncode != 0
    assert report["schema"] == 2
    assert report["status"] == "BLOCKED"
    assert report[runtime] == {"status": "CREDENTIAL_UNAVAILABLE"}
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("missing_runtime", "RUNTIME_UNAVAILABLE"),
        ("missing_bwrap", "ISOLATION_UNAVAILABLE"),
        ("installer_failure", "SETUP_FAILED"),
        ("cleanup_failure", "CLEANUP_FAILED"),
    ],
)
def test_top_level_failures_use_stable_v2_status(
    fake_runtimes, tmp_path, monkeypatch, fault, expected
):
    module = _load_probe_module()
    fake = fake_runtimes()
    work_dir = tmp_path / "probe"
    control_root = tmp_path / "control"

    def make_control_root(*_args, **_kwargs):
        control_root.mkdir(mode=0o700)
        return str(control_root)

    monkeypatch.setattr(module.tempfile, "mkdtemp", make_control_root)
    _inject_fault(module, monkeypatch, fault, tmp_path)
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    stdout = io.StringIO()
    try:
        with redirect_stdout(stdout):
            exit_code = module.main(
                [
                    "--runtime",
                    "claude",
                    "--repo-source",
                    str(REPO),
                    "--work-dir",
                    str(work_dir),
                    "--auth-source",
                    "environment",
                ]
            )

        assert exit_code != 0
        report = json.loads(stdout.getvalue())
        observed = report.get("claude", report)["status"]
        assert observed == expected
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(control_root, ignore_errors=True)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ((0, "ZERO"), None),
        ((7, "NONZERO"), "RUNTIME_FAILED"),
        ((7, "TIMEOUT"), "TIMEOUT"),
    ],
)
def test_runtime_failure_prioritizes_timeout(result, expected):
    module = _load_probe_module()

    observed = module._runtime_failure(module.ProcessResult(*result))

    assert observed == expected


def test_control_digest_mismatch_uses_stable_v2_status(tmp_path, monkeypatch):
    module = _load_probe_module()
    work_dir = tmp_path / "work"
    repo = work_dir / "repo"
    home = work_dir / "home"
    control_root = tmp_path / "control"
    control_home = control_root / "home"
    fake_bin = control_root / "fake-bin"
    hosts_file = control_root / "hosts"
    for path in (repo, home, control_home, fake_bin):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    module._make_runtime_targets(control_root)

    monkeypatch.setattr(module, "_remove_review", lambda *_args: None)
    monkeypatch.setattr(module, "_create_pass_verdict", lambda *_args: None)

    def successful_phase(*_args, env, **_kwargs):
        Path(env["PRE_PR_PROBE_HOOK_LOG"]).write_text("D\n", encoding="ascii")
        return module.ProcessResult(0, "ZERO")

    monkeypatch.setattr(module, "_run_sandboxed", successful_phase)
    monkeypatch.setattr(module, "_protected_digest", lambda *_args: "tampered")

    report = module._probe_runtime(
        "claude",
        executable="/usr/bin/true",
        auth_source="environment",
        claude_subscription_token=None,
        codex_auth_fd=None,
        codex_hooks_fd=42,
        caller_env={"ANTHROPIC_API_KEY": "synthetic"},
        caller_home=None,
        work_dir=work_dir,
        home=home,
        control_root=control_root,
        control_home=control_home,
        repo=repo,
        fake_bin=fake_bin,
        hosts_file=hosts_file,
        cli=tmp_path / "cli",
        control_sha256="expected",
    )

    assert report["status"] == "CANARY_MISMATCH"


def test_timeout_stop_process_group_does_not_signal_reaped_group(monkeypatch):
    module = _load_probe_module()
    process = subprocess.Popen(
        ["/usr/bin/python3", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    original_killpg = module.os.killpg
    signals: list[int] = []

    def record_killpg(pid, signum):
        assert pid == process.pid
        signals.append(signum)
        return original_killpg(pid, signum)

    monkeypatch.setattr(module.os, "killpg", record_killpg)
    try:
        module._stop_process_group(process)
    finally:
        if process.poll() is None:
            original_killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert signals == [signal.SIGTERM]
    assert process.returncode == -signal.SIGTERM


def test_runtime_timeout_reaps_its_own_process_group(tmp_path):
    module = _load_probe_module()
    pid_file = tmp_path / "runtime-pids"
    source = textwrap.dedent(
        """\
        import os
        from pathlib import Path
        import subprocess
        import time

        child = subprocess.Popen(
            ["/usr/bin/python3", "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path(os.environ["PID_FILE"]).write_text(
            f"{os.getpid()}\\n{child.pid}\\n", encoding="ascii"
        )
        time.sleep(60)
        """
    )

    result = module._run_runtime(
        ["/usr/bin/python3", "-c", source],
        cwd=tmp_path,
        env={
            "PATH": SAFE_SYSTEM_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PID_FILE": str(pid_file),
        },
        timeout=0.5,
    )

    assert result.exit_class == "TIMEOUT"
    assert len(pid_file.read_text().splitlines()) == 2


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_default_signal_semantics_leave_no_runtime_or_credential_copy(
    fake_runtimes, tmp_path, signum
):
    sentinel = b"MEMFD_ONLY_CODEX_SENTINEL_32"
    auth = (
        b'{"auth_mode":"chatgpt","tokens":{"access_token":"'
        + sentinel
        + b'"}}\n'
    )
    fake = fake_runtimes(
        codex="wait_with_descendant",
        codex_auth_document=auth,
    )
    process, work_dir, _release = _start_probe(fake, tmp_path, runtime="codex")
    pid_file = work_dir / "tmp/runtime-pids"
    handles: list[ProcessHandle] = []
    try:
        _wait_for(pid_file, process)
        assert len(pid_file.read_text().splitlines()) == 2
        handles = _open_descendant_pidfds(process.pid)
        assert len(handles) >= 2

        os.kill(process.pid, signum)
        stdout, stderr = process.communicate(timeout=15)

        assert process.returncode != 0
        assert stderr == ""
        if signum == signal.SIGINT:
            assert json.loads(stdout) == {"schema": 2, "status": "RUNTIME_FAILED"}
            assert not work_dir.exists()
        else:
            assert stdout == ""
        for handle in handles:
            _wait_until_process_absent(handle, timeout=5.0)
        assert not _tree_contains(work_dir, sentinel)
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        _cleanup_process_handles(handles)
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(tmp_path / "controller-tmp", ignore_errors=True)


def test_timeout_leaves_no_runtime_or_credential_copy(
    fake_runtimes, tmp_path
):
    sentinel = b"MEMFD_ONLY_CODEX_TIMEOUT_SENTINEL_32"
    auth = (
        b'{"auth_mode":"chatgpt","tokens":{"access_token":"'
        + sentinel
        + b'"}}\n'
    )
    fake = fake_runtimes(
        codex="wait_with_descendant",
        codex_auth_document=auth,
    )
    process, work_dir, _release = _start_probe(
        fake,
        tmp_path,
        runtime="codex",
        runtime_timeout=1.0,
    )
    pid_file = work_dir / "tmp/runtime-pids"
    handles: list[ProcessHandle] = []
    try:
        _wait_for(pid_file, process)
        assert len(pid_file.read_text().splitlines()) == 2
        handles = _open_descendant_pidfds(process.pid)
        assert len(handles) >= 2
        stdout, stderr = process.communicate(timeout=15)

        assert process.returncode != 0
        assert stderr == ""
        report = json.loads(stdout)
        assert report["status"] == "BLOCKED"
        assert report["codex"] == {"status": "TIMEOUT"}
        for handle in handles:
            _wait_until_process_absent(handle, timeout=5.0)
        assert not _tree_contains(work_dir, sentinel)
        assert not work_dir.exists()
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        _cleanup_process_handles(handles)
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(tmp_path / "controller-tmp", ignore_errors=True)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("nonzero", "RUNTIME_FAILED"), ("timeout", "TIMEOUT")],
)
def test_runtime_failure_precedes_marker_evaluation(
    fake_runtimes, tmp_path, monkeypatch, mode, expected
):
    module = _load_probe_module()
    fake = fake_runtimes(claude=mode)
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    if mode == "timeout":
        monkeypatch.setattr(module, "RUNTIME_TIMEOUT_SECONDS", 0.1)
    stdout = io.StringIO()
    work_dir = tmp_path / "probe"

    with redirect_stdout(stdout):
        exit_code = module.main(
            [
                "--runtime",
                "claude",
                "--repo-source",
                str(REPO),
                "--work-dir",
                str(work_dir),
                "--auth-source",
                "environment",
            ]
        )

    report = json.loads(stdout.getvalue())
    assert exit_code != 0
    assert report["claude"]["status"] == expected
    assert not work_dir.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["issue", "create"],
        ["pr", "create", "--title", "canary", "--body"],
        ["pr", "create", "--body", "canary", "--title", "canary"],
    ],
)
def test_fake_gh_records_invalid_calls(tmp_path, arguments):
    module = _load_probe_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_bin = tmp_path / "fake-bin"
    marker = tmp_path / "gh.log"
    marker.write_bytes(b"")
    marker.chmod(0o600)
    module._make_fake_gh(fake_bin, repo)

    result = subprocess.run(
        [str(fake_bin / "gh"), *arguments],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env={
            "PATH": SAFE_SYSTEM_PATH,
            "PRE_PR_PROBE_GH_LOG": str(marker),
        },
    )

    assert result.returncode == 64
    assert result.stdout == ""
    assert result.stderr == ""
    assert module._read_marker_log(marker, module.GH_MARKERS) == ("I",)


def test_probe_refuses_unsafe_supplied_work_directory(fake_runtimes, tmp_path):
    fake = fake_runtimes()
    work_dir = tmp_path / "existing"
    work_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--runtime",
            "claude",
            "--repo-source",
            str(REPO),
            "--work-dir",
            str(work_dir),
            "--auth-source",
            "environment",
        ],
        env=fake.env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert json.loads(result.stdout) == {"schema": 2, "status": "INVALID_WORK_DIR"}
    assert work_dir.is_dir()
