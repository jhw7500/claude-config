from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
import importlib.util
import io
import json
import os
from pathlib import Path
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


@pytest.fixture
def fake_runtimes(tmp_path):
    def make(*, claude: str = "success", codex: str = "success") -> FakeRuntimes:
        fake_bin = tmp_path / f"runtime-bin-{claude}-{codex}"
        fake_bin.mkdir(mode=0o700)
        caller_home = tmp_path / "caller-home"
        for runtime, mode in (("claude", claude), ("codex", codex)):
            target = fake_bin / runtime
            target.write_text(
                _fake_runtime_source(
                    runtime,
                    mode,
                    caller_home=caller_home,
                    repo_source=REPO,
                ),
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
            }
        )

    (tmp_path / "caller-home").mkdir(mode=0o700)
    return make


def _fake_runtime_source(
    runtime: str,
    mode: str,
    *,
    caller_home: Path,
    repo_source: Path,
) -> str:
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
        REPO_SOURCE = {str(repo_source)!r}
        CANARY = "gh pr create --title canary --body canary"
        ALLOWED = {{
            "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "CLAUDE_CONFIG_DIR", "CODEX_HOME",
            "GH_CONFIG_DIR", "GH_HOST", "GH_PROMPT_DISABLED", "TMPDIR",
        }}

        if "--version" in sys.argv[1:]:
            print(f"{{RUNTIME}} 9.9.9")
            raise SystemExit(0)

        unexpected = sorted(set(os.environ) - ALLOWED)
        if unexpected:
            print(json.dumps({{"type": "environment_error", "count": len(unexpected)}}))
            raise SystemExit(9)
        path_head = Path(os.environ["PATH"].split(os.pathsep, 1)[0]) / "gh"
        if shutil.which("gh") != str(path_head):
            print(json.dumps({{"type": "path_order_error"}}))
            raise SystemExit(9)
        if MODE == "timeout":
            time.sleep(60)
            raise SystemExit(0)
        if MODE == "huge_output":
            sys.stdout.write("x" * (1024 * 1024))
            sys.stdout.flush()
            time.sleep(60)
            raise SystemExit(0)
        if MODE == "orphaned_pipe":
            subprocess.Popen(
                ["/usr/bin/python3", "-c", "import time; time.sleep(0.6)"],
                env=dict(os.environ),
            )
            raise SystemExit(0)
        if MODE == "auth_unavailable":
            print("Authentication required", file=sys.stderr)
            raise SystemExit(3)
        if MODE == "auth_sensitive":
            print("Authentication required")
            print(os.environ["HOME"])
            print("/home/probe-metadata")
            raise SystemExit(3)
        if MODE == "auth_token_leak":
            print("Authentication required TOKEN_CANARY")
            raise SystemExit(3)
        if MODE == "auth_caller_home_leak":
            print("Authentication required " + CALLER_HOME)
            raise SystemExit(3)
        if MODE == "auth_repo_source_leak":
            print("Authentication required " + REPO_SOURCE)
            raise SystemExit(3)
        if MODE == "auth_api_key_leak":
            print("Authentication required " + os.environ["ANTHROPIC_API_KEY"])
            raise SystemExit(3)
        if MODE == "nonzero":
            print(json.dumps({{"type": "runtime_failure"}}))
            raise SystemExit(7)
        if MODE == "malformed":
            print("not-json")
            raise SystemExit(0)

        home = Path(os.environ["HOME"])
        prompt = "Use the shell tool exactly once to run: " + CANARY
        if RUNTIME == "claude":
            expected = [
                "-p", "--no-session-persistence", "--setting-sources", "project",
                "--settings", str(home / ".claude" / "settings.json"),
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
            print(json.dumps({{"type": "argument_error"}}))
            raise SystemExit(8)
        config = (
            home / ".claude" / "settings.json"
            if RUNTIME == "claude"
            else Path(os.environ["CODEX_HOME"]) / "hooks.json"
        )
        value = json.loads(config.read_text(encoding="utf-8"))

        if MODE in {{"malformed_after_valid", "nonzero_after_valid"}}:
            subprocess.run(
                ["gh", "pr", "create", "--title", "canary", "--body", "canary"],
                check=False,
                env=dict(os.environ),
            )
            if MODE.startswith("malformed"):
                print("not-json")
                raise SystemExit(0)
            print(json.dumps({{"type": "runtime_failure"}}))
            raise SystemExit(7)
        if MODE in {{"malformed_after_invalid", "nonzero_after_invalid"}}:
            subprocess.run(
                ["gh", "pr", "view"],
                check=False,
                env=dict(os.environ),
            )
            if MODE.startswith("malformed"):
                print("not-json")
                raise SystemExit(0)
            print(json.dumps({{"type": "runtime_failure"}}))
            raise SystemExit(7)

        command = {{
            "absolute_gh": "/usr/bin/gh pr create --title canary --body canary",
            "path_reset": "PATH=/usr/bin:/bin gh pr create --title canary --body canary",
            "command_p": "command -p gh pr create --title canary --body canary",
            "nonexact_hook": "gh pr create --title other --body canary",
            "github_client": "curl https://github.com",
        }}.get(MODE, CANARY)
        payload = {{
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash" if RUNTIME == "claude" else "exec_command",
            "cwd": os.getcwd(),
            "tool_input": {{"command": command}},
        }}
        groups = value.get("hooks", {{}}).get("PreToolUse", [])
        events = []
        denied = False
        for group in groups:
            matcher = group.get("matcher")
            if matcher is not None and matcher != payload["tool_name"]:
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
                    events.append(event)
                    decision = event.get("hookSpecificOutput", {{}}).get(
                        "permissionDecision"
                    )
                    denied = denied or decision == "deny"
        if not denied and MODE != "pass_absent":
            calls = 2 if MODE == "pass_duplicate" else 1
            for _ in range(calls):
                subprocess.run(
                    ["gh", "pr", "create", "--title", "canary", "--body", "canary"],
                    check=False,
                    env=dict(os.environ),
                )
        elif denied and MODE == "denied_canary":
            subprocess.run(
                ["gh", "pr", "create", "--title", "canary", "--body", "canary"],
                check=False,
                env=dict(os.environ),
            )
        print(json.dumps({{"type": "hook_result", "events": events}}))
        if MODE == "token_leak":
            print("TOKEN_CANARY")
        if MODE == "path_leak":
            print(os.environ["HOME"])
        """
    )


def _run_probe(
    fake: FakeRuntimes,
    tmp_path: Path,
    *,
    runtime: str = "all",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    work_dir = tmp_path / "probe"
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
        ],
        env=fake.env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, work_dir


def _load_probe_module():
    assert PROBE_SCRIPT.is_file(), "probe script is missing"
    spec = importlib.util.spec_from_file_location("probe_pre_pr_tribunal", PROBE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assert_phase_counts(
    phase: dict[str, object],
    *,
    denied: bool,
    canary_count: int,
    invalid_call_count: int,
) -> None:
    assert {
        key: phase[key]
        for key in ("denied", "canary_count", "invalid_call_count")
    } == {
        "denied": denied,
        "canary_count": canary_count,
        "invalid_call_count": invalid_call_count,
    }


def test_probe_requires_block_then_allow_for_each_runtime(
    fake_runtimes, tmp_path
):
    sibling = tmp_path / "must-survive"
    sibling.write_text("sentinel", encoding="utf-8")
    fake = fake_runtimes()
    result, work_dir = _run_probe(fake, tmp_path)

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert result.stderr == ""
    assert report["status"] == "PASS"
    _assert_phase_counts(
        report["claude"]["missing_verdict"],
        denied=True,
        canary_count=0,
        invalid_call_count=0,
    )
    _assert_phase_counts(
        report["claude"]["pass_verdict"],
        denied=False,
        canary_count=1,
        invalid_call_count=0,
    )
    _assert_phase_counts(
        report["codex"]["missing_verdict"],
        denied=True,
        canary_count=0,
        invalid_call_count=0,
    )
    _assert_phase_counts(
        report["codex"]["pass_verdict"],
        denied=False,
        canary_count=1,
        invalid_call_count=0,
    )
    assert sibling.read_text(encoding="utf-8") == "sentinel"
    assert not work_dir.exists()
    serialized = result.stdout
    assert "gh pr create" not in serialized
    assert "TOKEN_CANARY" not in serialized
    assert str(REPO) not in serialized
    assert str(tmp_path) not in serialized
    assert "test-anthropic-key" not in serialized
    assert "test-openai-key" not in serialized


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("auth_unavailable", "AUTH_UNAVAILABLE"),
        ("nonzero", "RUNTIME_NONZERO"),
        ("malformed", "MALFORMED_OUTPUT"),
        ("denied_canary", "CANARY_MISMATCH"),
        ("pass_absent", "CANARY_MISMATCH"),
        ("pass_duplicate", "CANARY_MISMATCH"),
        ("token_leak", "SENSITIVE_OUTPUT"),
        ("path_leak", "SENSITIVE_OUTPUT"),
    ],
)
def test_probe_blocks_runtime_failures_and_never_returns_raw_output(
    fake_runtimes, tmp_path, mode, expected
):
    fake = fake_runtimes(claude=mode)
    result, work_dir = _run_probe(fake, tmp_path, runtime="claude")

    report = json.loads(result.stdout)
    assert result.returncode != 0
    assert result.stderr == ""
    assert report["status"] == "BLOCKED"
    assert report["claude"]["status"] == expected
    assert not work_dir.exists()
    assert mode not in result.stdout
    assert "TOKEN_CANARY" not in result.stdout
    assert str(tmp_path) not in result.stdout
    assert "Authentication required" not in result.stdout
    assert "runtime_failure" not in result.stdout


def test_probe_hard_caps_runtime_output(fake_runtimes, tmp_path):
    fake = fake_runtimes(claude="huge_output")
    result, work_dir = _run_probe(fake, tmp_path, runtime="claude")

    report = json.loads(result.stdout)
    assert result.returncode != 0
    assert report["claude"]["status"] == "OUTPUT_LIMIT"
    assert len(result.stdout.encode("utf-8")) < 16 * 1024
    assert not work_dir.exists()


def test_probe_times_out_runtime_and_cleans_only_its_work_dir(
    fake_runtimes, tmp_path, monkeypatch
):
    module = _load_probe_module()
    monkeypatch.setattr(module, "RUNTIME_TIMEOUT_SECONDS", 0.1)
    fake = fake_runtimes(claude="timeout")
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    outside = tmp_path / "outside"
    outside.write_text("preserve", encoding="utf-8")
    work_dir = tmp_path / "probe"
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = module.main(
            [
                "--runtime",
                "claude",
                "--repo-source",
                str(REPO),
                "--work-dir",
                str(work_dir),
            ]
        )

    report = json.loads(stdout.getvalue())
    assert exit_code != 0
    assert report["claude"]["status"] == "TIMEOUT"
    assert outside.read_text(encoding="utf-8") == "preserve"
    assert not work_dir.exists()


def test_probe_timeout_covers_descendants_holding_capture_pipes(tmp_path):
    module = _load_probe_module()
    source = (
        "import subprocess; "
        "subprocess.Popen(['/usr/bin/python3', '-c', "
        "'import time; time.sleep(0.6)'])"
    )
    started = time.monotonic()
    capture = module._run_bounded(
        ["/usr/bin/python3", "-c", source],
        cwd=tmp_path,
        env={"PATH": SAFE_SYSTEM_PATH},
        timeout=0.1,
    )
    elapsed = time.monotonic() - started

    assert capture.exit_class == "TIMEOUT"
    assert elapsed < 0.9


def test_probe_default_work_dir_uses_mkdtemp_and_is_cleaned(
    fake_runtimes, tmp_path, monkeypatch
):
    module = _load_probe_module()
    fake = fake_runtimes(claude="success")
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    allocated = tmp_path / "default-probe"

    def allocate(*, prefix):
        assert prefix == "pre-pr-tribunal-probe-"
        allocated.mkdir(mode=0o700)
        return str(allocated)

    monkeypatch.setattr(module.tempfile, "mkdtemp", allocate)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = module.main(
            ["--runtime", "claude", "--repo-source", str(REPO)]
        )

    assert exit_code == 0
    assert json.loads(stdout.getvalue())["status"] == "PASS"
    assert not allocated.exists()


@pytest.mark.parametrize(
    "target", ["existing", "root", "home", "repo", "relative"]
)
def test_probe_refuses_unsafe_supplied_work_directories(
    fake_runtimes, tmp_path, target
):
    fake = fake_runtimes()
    if target == "existing":
        work_dir = tmp_path / "existing"
        work_dir.mkdir()
    elif target == "root":
        work_dir = Path("/")
    elif target == "home":
        work_dir = Path(fake.env["HOME"])
    elif target == "repo":
        work_dir = REPO
    else:
        work_dir = Path("relative-probe")
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
        ],
        env=fake.env,
        text=True,
        capture_output=True,
        check=False,
    )

    report = json.loads(result.stdout)
    assert result.returncode != 0
    assert result.stderr == ""
    assert report == {"schema": 1, "status": "INVALID_WORK_DIR"}
    if target == "existing":
        assert work_dir.is_dir()


def test_probe_refuses_an_absent_path_equal_to_caller_home(fake_runtimes, tmp_path):
    fake = fake_runtimes()
    absent_home = tmp_path / "absent-caller-home"
    environment = dict(fake.env, HOME=str(absent_home))
    result = subprocess.run(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--runtime",
            "claude",
            "--repo-source",
            str(REPO),
            "--work-dir",
            str(absent_home),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert json.loads(result.stdout) == {"schema": 1, "status": "INVALID_WORK_DIR"}
    assert not absent_home.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["issue", "create"],
        ["pr", "create", "--title", "canary", "--body"],
        ["pr", "create", "--title", "canary", "--body", "canary", "extra"],
        ["pr", "create", "--body", "canary", "--title", "canary"],
    ],
)
def test_fake_gh_rejects_nonexact_argv_and_records_invalid_call(tmp_path, arguments):
    module = _load_probe_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_bin = tmp_path / "fake-bin"
    count_file = tmp_path / "valid-count"
    invalid_file = tmp_path / "invalid-count"
    module._make_fake_gh(fake_bin, count_file, invalid_file, repo)

    result = subprocess.run(
        [str(fake_bin / "gh"), *arguments],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert not count_file.exists()
    assert invalid_file.read_text(encoding="ascii").splitlines() == ["1"]


def test_fake_gh_rejects_wrong_cwd_and_second_exact_call(tmp_path):
    module = _load_probe_module()
    repo = tmp_path / "repo"
    wrong_repo = tmp_path / "wrong-repo"
    repo.mkdir()
    wrong_repo.mkdir()
    fake_bin = tmp_path / "fake-bin"
    count_file = tmp_path / "valid-count"
    invalid_file = tmp_path / "invalid-count"
    module._make_fake_gh(fake_bin, count_file, invalid_file, repo)
    command = [
        str(fake_bin / "gh"),
        "pr",
        "create",
        "--title",
        "canary",
        "--body",
        "canary",
    ]

    wrong_cwd = subprocess.run(command, cwd=wrong_repo, check=False)
    first = subprocess.run(command, cwd=repo, check=False)
    duplicate = subprocess.run(command, cwd=repo, check=False)

    assert wrong_cwd.returncode != 0
    assert first.returncode == 0
    assert duplicate.returncode != 0
    assert count_file.read_text(encoding="ascii").splitlines() == ["1"]
    assert invalid_file.read_text(encoding="ascii").splitlines() == ["1", "1"]


def test_probe_guard_allows_only_exact_command_in_exact_cwd(tmp_path):
    module = _load_probe_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    guard = tmp_path / "guard.py"
    module._make_probe_guard(guard, repo)
    base = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "cwd": str(repo),
        "tool_input": {
            "command": "gh pr create --title canary --body canary",
        },
    }

    exact = subprocess.run(
        [sys.executable, str(guard)],
        input=json.dumps(base),
        text=True,
        capture_output=True,
        check=False,
    )
    wrong_command = json.loads(json.dumps(base))
    wrong_command["tool_input"]["command"] = (
        "gh pr create --title other --body canary"
    )
    rejected_command = subprocess.run(
        [sys.executable, str(guard)],
        input=json.dumps(wrong_command),
        text=True,
        capture_output=True,
        check=False,
    )
    wrong_cwd = dict(base, cwd=str(tmp_path))
    rejected_cwd = subprocess.run(
        [sys.executable, str(guard)],
        input=json.dumps(wrong_cwd),
        text=True,
        capture_output=True,
        check=False,
    )

    assert exact.returncode == 0
    assert exact.stdout == ""
    for rejected in (rejected_command, rejected_cwd):
        event = json.loads(rejected.stdout)
        assert rejected.returncode == 0
        assert event["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "PRE-PR-PROBE:COMMAND_REJECTED" in event["hookSpecificOutput"][
            "permissionDecisionReason"
        ]


def test_isolation_verifier_intercepts_system_gh_and_sinkholes_github(tmp_path):
    module = _load_probe_module()
    work_dir = tmp_path / "boundary"
    repo = work_dir / "repo"
    home = work_dir / "home"
    fake_bin = work_dir / "fake-bin"
    work_dir.mkdir()
    repo.mkdir()
    home.mkdir()
    count_file = work_dir / "valid-count"
    invalid_file = work_dir / "invalid-count"
    hosts_file = work_dir / "hosts"
    guard = work_dir / "guard.py"
    module._make_fake_gh(fake_bin, count_file, invalid_file, repo)
    module._make_hosts_file(hosts_file)
    module._make_probe_guard(guard, repo)

    module._verify_isolation(
        work_dir=work_dir,
        repo=repo,
        home=home,
        fake_gh=fake_bin / "gh",
        count_file=count_file,
        invalid_file=invalid_file,
        hosts_file=hosts_file,
        guard=guard,
    )

    assert not count_file.exists()
    assert not invalid_file.exists()


def test_missing_required_bwrap_is_stable_isolation_unavailable(
    fake_runtimes, tmp_path, monkeypatch
):
    module = _load_probe_module()
    monkeypatch.setattr(module, "BWRAP_PATH", tmp_path / "missing-bwrap")
    fake = fake_runtimes()
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    work_dir = tmp_path / "probe"
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = module.main(
            [
                "--runtime",
                "claude",
                "--repo-source",
                str(REPO),
                "--work-dir",
                str(work_dir),
            ]
        )

    assert exit_code != 0
    assert json.loads(stdout.getvalue()) == {
        "schema": 1,
        "status": "ISOLATION_UNAVAILABLE",
    }
    assert not work_dir.exists()


@pytest.mark.parametrize(
    "mode",
    ["absolute_gh", "path_reset", "command_p", "nonexact_hook", "github_client"],
)
def test_runtime_guard_denies_adversarial_shell_commands(
    fake_runtimes, tmp_path, mode
):
    fake = fake_runtimes(claude=mode)
    result, work_dir = _run_probe(fake, tmp_path, runtime="claude")

    report = json.loads(result.stdout)
    assert result.returncode != 0
    assert report["claude"]["status"] == "CANARY_MISMATCH"
    _assert_phase_counts(
        report["claude"]["pass_verdict"],
        denied=True,
        canary_count=0,
        invalid_call_count=0,
    )
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("mode", "status", "parse_valid", "canary_count", "invalid_call_count"),
    [
        ("malformed", "MALFORMED_OUTPUT", False, 0, 0),
        ("malformed_after_valid", "MALFORMED_OUTPUT", False, 1, 0),
        ("malformed_after_invalid", "MALFORMED_OUTPUT", False, 0, 1),
        ("nonzero", "RUNTIME_NONZERO", True, 0, 0),
        ("nonzero_after_valid", "RUNTIME_NONZERO", True, 1, 0),
        ("nonzero_after_invalid", "RUNTIME_NONZERO", True, 0, 1),
    ],
)
def test_phase_failure_retains_sanitized_facts(
    fake_runtimes,
    tmp_path,
    mode,
    status,
    parse_valid,
    canary_count,
    invalid_call_count,
):
    fake = fake_runtimes(claude=mode)
    result, work_dir = _run_probe(fake, tmp_path, runtime="claude")

    runtime = json.loads(result.stdout)["claude"]
    phase = runtime["missing_verdict"]
    assert runtime["status"] == status
    assert runtime["phase"] == "missing_verdict"
    assert phase["parse_valid"] is parse_valid
    assert phase["denied"] is False
    assert phase["canary_count"] == canary_count
    assert phase["invalid_call_count"] == invalid_call_count
    assert phase["capture"]["exit_class"] in {"ZERO", "NONZERO"}
    assert len(phase["capture"]["stdout_sha256"]) == 64
    assert len(phase["capture"]["stderr_sha256"]) == 64
    assert not work_dir.exists()


def test_auth_nonzero_precedes_disposable_sensitivity_and_retains_reason(
    fake_runtimes, tmp_path
):
    fake = fake_runtimes(claude="auth_sensitive")
    result, work_dir = _run_probe(fake, tmp_path, runtime="claude")

    runtime = json.loads(result.stdout)["claude"]
    sensitivity = runtime["missing_verdict"]["sensitivity"]
    assert runtime["status"] == "AUTH_UNAVAILABLE"
    assert runtime["phase"] == "missing_verdict"
    assert sensitivity["detected"] is True
    assert sensitivity["high_risk"] is False
    assert set(sensitivity["reasons"]) == {"DISPOSABLE_PATH", "GENERIC_HOME"}
    assert not work_dir.exists()


@pytest.mark.parametrize(
    "mode",
    [
        "auth_token_leak",
        "auth_caller_home_leak",
        "auth_repo_source_leak",
        "auth_api_key_leak",
    ],
)
def test_high_risk_leak_precedes_auth_classification(fake_runtimes, tmp_path, mode):
    fake = fake_runtimes(claude=mode)
    result, work_dir = _run_probe(fake, tmp_path, runtime="claude")

    runtime = json.loads(result.stdout)["claude"]
    sensitivity = runtime["missing_verdict"]["sensitivity"]
    assert runtime["status"] == "SENSITIVE_OUTPUT"
    assert sensitivity["detected"] is True
    assert sensitivity["high_risk"] is True
    assert not work_dir.exists()
