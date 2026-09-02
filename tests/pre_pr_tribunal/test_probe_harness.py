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

import pytest


REPO = Path(__file__).resolve().parents[2]
PROBE_SCRIPT = REPO / "scripts" / "probe-pre-pr-tribunal.py"
SAFE_SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass(frozen=True)
class FakeRuntimes:
    env: dict[str, str]


@pytest.fixture
def fake_runtimes(tmp_path):
    def make(
        *,
        claude: str = "success",
        codex: str = "success",
    ) -> FakeRuntimes:
        fake_bin = tmp_path / f"runtime-bin-{claude}-{codex}"
        fake_bin.mkdir(mode=0o700)
        caller_home = tmp_path / "caller-home"
        caller_home.mkdir(mode=0o700, exist_ok=True)
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
            }
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
        CANARY = "gh pr create --title canary --body canary"
        ALLOWED = {{
            "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
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

        unexpected = sorted(set(os.environ) - ALLOWED)
        if unexpected:
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
        prompt = "Use the shell tool exactly once to run: " + CANARY
        if RUNTIME == "claude":
            expected = [
                "-p", "--no-session-persistence", "--setting-sources", "project",
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

        config = config_root / (
            "settings.json" if RUNTIME == "claude" else "hooks.json"
        )
        value = json.loads(config.read_text(encoding="utf-8"))
        tool_name = "Bash" if RUNTIME == "claude" else "exec_command"
        payload = {{
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "cwd": os.getcwd(),
            "tool_input": {{"command": CANARY}},
        }}
        denied = False
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
        if not denied:
            subprocess.run(
                ["gh", "pr", "create", "--title", "canary", "--body", "canary"],
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
