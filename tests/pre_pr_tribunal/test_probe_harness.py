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
        for runtime, mode in (("claude", claude), ("codex", codex)):
            target = fake_bin / runtime
            target.write_text(_fake_runtime_source(runtime, mode), encoding="utf-8")
            target.chmod(0o700)
        return FakeRuntimes(
            env={
                "HOME": str(tmp_path / "caller-home"),
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


def _fake_runtime_source(runtime: str, mode: str) -> str:
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
        ALLOWED = {{
            "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "CLAUDE_CONFIG_DIR", "CODEX_HOME",
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
        if MODE == "nonzero":
            print(json.dumps({{"type": "runtime_failure"}}))
            raise SystemExit(7)
        if MODE == "malformed":
            print("not-json")
            raise SystemExit(0)

        home = Path(os.environ["HOME"])
        prompt = (
            "Use the shell tool exactly once to run: "
            "gh pr create --title canary --body canary"
        )
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

        def commands(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if key == "command" and isinstance(child, str):
                        yield child
                    else:
                        yield from commands(child)
            elif isinstance(node, list):
                for child in node:
                    yield from commands(child)

        suffix = f"{{RUNTIME}}_hook.py"
        hook_command = next(item for item in commands(value) if item.endswith(suffix))
        payload = {{
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash" if RUNTIME == "claude" else "exec_command",
            "cwd": os.getcwd(),
            "tool_input": {{"command": "gh pr create --title canary --body canary"}},
        }}
        hook = subprocess.run(
            shlex.split(os.path.expandvars(hook_command)),
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            env=dict(os.environ),
        )
        denied = bool(hook.stdout)
        if not denied and MODE != "pass_absent":
            calls = 2 if MODE == "pass_duplicate" else 1
            for _ in range(calls):
                subprocess.run(
                    ["gh", "pr", "create", "--title", "canary", "--body", "canary"],
                    check=True,
                    env=dict(os.environ),
                )
        elif denied and MODE == "denied_canary":
            subprocess.run(
                ["gh", "pr", "create", "--title", "canary", "--body", "canary"],
                check=True,
                env=dict(os.environ),
            )
        event = json.loads(hook.stdout) if hook.stdout else {{"decision": "allow"}}
        print(json.dumps({{"type": "hook_result", "event": event}}))
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
    assert report["claude"]["missing_verdict"] == {
        "denied": True,
        "canary_count": 0,
    }
    assert report["claude"]["pass_verdict"] == {
        "denied": False,
        "canary_count": 1,
    }
    assert report["codex"]["missing_verdict"] == {
        "denied": True,
        "canary_count": 0,
    }
    assert report["codex"]["pass_verdict"] == {
        "denied": False,
        "canary_count": 1,
    }
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


def test_probe_timeout_covers_descendants_holding_capture_pipes(
    fake_runtimes, tmp_path, monkeypatch
):
    module = _load_probe_module()
    monkeypatch.setattr(module, "RUNTIME_TIMEOUT_SECONDS", 0.1)
    fake = fake_runtimes(claude="orphaned_pipe")
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    work_dir = tmp_path / "probe"
    stdout = io.StringIO()
    started = time.monotonic()
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
    elapsed = time.monotonic() - started

    assert exit_code != 0
    assert json.loads(stdout.getvalue())["claude"]["status"] == "TIMEOUT"
    assert elapsed < 0.9
    assert not work_dir.exists()


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
