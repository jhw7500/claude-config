from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
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
    codex_auth: bytes


@pytest.fixture
def fake_runtimes(tmp_path):
    def make(
        *,
        claude: str = "success",
        codex: str = "success",
        codex_auth_document: bytes | None = None,
    ) -> FakeRuntimes:
        fake_bin = tmp_path / f"runtime-bin-{claude}-{codex}"
        fake_bin.mkdir(mode=0o700)
        caller_home = tmp_path / "caller-home"
        claude_token = "fixture-claude-oauth-access-token"
        codex_auth = codex_auth_document or (
            b'{"auth_mode":"chatgpt","tokens":'
            b'{"access_token":"fixture-codex-access-token",'
            b'"refresh_token":"fixture-codex-refresh-token",'
            b'"id_token":"fixture-codex-id-token"}}\n'
        )
        claude_dir = caller_home / ".claude"
        codex_dir = caller_home / ".codex"
        claude_dir.mkdir(mode=0o700, exist_ok=True)
        codex_dir.mkdir(mode=0o700, exist_ok=True)
        claude_credentials = claude_dir / ".credentials.json"
        claude_credentials.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": claude_token,
                        "expiresAt": 4_102_444_800_000,
                    }
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        claude_credentials.chmod(0o600)
        codex_credentials = codex_dir / "auth.json"
        codex_credentials.write_bytes(codex_auth)
        codex_credentials.chmod(0o600)
        for runtime, mode in (("claude", claude), ("codex", codex)):
            target = fake_bin / runtime
            target.write_text(
                _fake_runtime_source(
                    runtime,
                    mode,
                    caller_home=caller_home,
                    repo_source=REPO,
                    claude_token=claude_token,
                    codex_auth=codex_auth,
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
            },
            caller_home=caller_home,
            claude_token=claude_token,
            codex_auth=codex_auth,
        )

    (tmp_path / "caller-home").mkdir(mode=0o700)
    return make


def _fake_runtime_source(
    runtime: str,
    mode: str,
    *,
    caller_home: Path,
    repo_source: Path,
    claude_token: str,
    codex_auth: bytes,
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
        CLAUDE_TOKEN = {claude_token!r}
        CODEX_AUTH = {codex_auth!r}
        REFRESHED_CODEX_TOKEN = "rotated-codex-access-token"
        CANARY = "gh pr create --title canary --body canary"
        ALLOWED = {{
            "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "CLAUDE_CONFIG_DIR", "CODEX_HOME",
            "GH_CONFIG_DIR", "GH_HOST", "GH_PROMPT_DISABLED", "TMPDIR",
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
            if (
                provider_keys != expected_keys
                or "CLAUDE_CODE_OAUTH_TOKEN" in os.environ
            ):
                print(json.dumps({{"type": "provider_auth_isolation_error"}}))
                raise SystemExit(9)

        if "--version" in sys.argv[1:]:
            if MODE == "signal_wait_version":
                marker = Path(os.environ["TMPDIR"]) / "signal-version-ready"
                marker.write_text("ready", encoding="ascii")
                time.sleep(60)
                raise SystemExit(0)
            if MODE == "version_initial_leak_zero":
                if RUNTIME == "claude":
                    secret = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
                else:
                    secret = json.loads(
                        (Path(os.environ["CODEX_HOME"]) / "auth.json").read_text()
                    )["tokens"]["access_token"]
                print(secret)
                print(f"{{RUNTIME}} 9.9.9")
                raise SystemExit(0)
            if MODE == "codex_version_refresh_leak_zero":
                refreshed = json.loads(CODEX_AUTH)
                refreshed["tokens"]["access_token"] = REFRESHED_CODEX_TOKEN
                auth_path = Path(os.environ["CODEX_HOME"]) / "auth.json"
                auth_path.write_text(
                    json.dumps(refreshed, separators=(",", ":")) + "\\n",
                    encoding="utf-8",
                )
                auth_path.chmod(0o600)
                print(REFRESHED_CODEX_TOKEN)
                print("codex 9.9.9")
                raise SystemExit(0)
            if MODE == "codex_version_refresh_leak":
                refreshed = json.loads(CODEX_AUTH)
                refreshed["tokens"]["access_token"] = REFRESHED_CODEX_TOKEN
                auth_path = Path(os.environ["CODEX_HOME"]) / "auth.json"
                auth_path.write_text(
                    json.dumps(refreshed, separators=(",", ":")) + "\\n",
                    encoding="utf-8",
                )
                auth_path.chmod(0o600)
                print(REFRESHED_CODEX_TOKEN)
                raise SystemExit(3)
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
        for live_source in (
            Path(CALLER_HOME) / ".claude/.credentials.json",
            Path(CALLER_HOME) / ".codex/auth.json",
        ):
            try:
                live_source.read_bytes()
            except OSError:
                continue
            print(json.dumps({{"type": "live_credential_visible"}}))
            raise SystemExit(9)

        config_root = Path(
            os.environ["CLAUDE_CONFIG_DIR"]
            if RUNTIME == "claude"
            else os.environ["CODEX_HOME"]
        )
        environment_auth = bool(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        if environment_auth:
            if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
                print(json.dumps({{"type": "auth_exposure_error"}}))
                raise SystemExit(9)
        elif RUNTIME == "claude":
            if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") != CLAUDE_TOKEN:
                print(json.dumps({{"type": "auth_exposure_error"}}))
                raise SystemExit(9)
            if "ANTHROPIC_API_KEY" in os.environ or "OPENAI_API_KEY" in os.environ:
                print(json.dumps({{"type": "auth_exposure_error"}}))
                raise SystemExit(9)
        else:
            auth_path = config_root / "auth.json"
            try:
                auth_bytes = auth_path.read_bytes()
                auth_metadata = auth_path.lstat()
                auth_value = json.loads(auth_bytes)
                access_token = auth_value["tokens"]["access_token"]
            except (KeyError, OSError, TypeError, json.JSONDecodeError):
                print(json.dumps({{"type": "auth_exposure_error"}}))
                raise SystemExit(9)
            if auth_bytes != CODEX_AUTH and access_token != REFRESHED_CODEX_TOKEN:
                print(json.dumps({{"type": "auth_exposure_error"}}))
                raise SystemExit(9)
            if (
                auth_path == Path(CALLER_HOME) / ".codex/auth.json"
                or auth_path.is_symlink()
                or auth_metadata.st_mode & 0o077
            ):
                print(json.dumps({{"type": "auth_exposure_error"}}))
                raise SystemExit(9)
            if "ANTHROPIC_API_KEY" in os.environ or "OPENAI_API_KEY" in os.environ:
                print(json.dumps({{"type": "auth_exposure_error"}}))
                raise SystemExit(9)
        if MODE == "signal_wait_phase":
            marker = Path(os.environ["TMPDIR"]) / "signal-phase-ready"
            marker.write_text("ready", encoding="ascii")
            time.sleep(60)
            raise SystemExit(0)
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
        state_root = home.parent
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
            print(json.dumps({{"type": "argument_error"}}))
            raise SystemExit(8)
        config = config_root / ("settings.json" if RUNTIME == "claude" else "hooks.json")
        value = json.loads(config.read_text(encoding="utf-8"))

        if MODE in {{"codex_refresh", "codex_refresh_leak"}}:
            refreshed = json.loads(CODEX_AUTH)
            refreshed["tokens"]["access_token"] = REFRESHED_CODEX_TOKEN
            auth_path = config_root / "auth.json"
            auth_path.write_text(
                json.dumps(refreshed, separators=(",", ":")) + "\\n",
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            if MODE == "codex_refresh_leak":
                print(REFRESHED_CODEX_TOKEN)
                raise SystemExit(3)
        if MODE == "credential_leak":
            if RUNTIME == "claude":
                print(os.environ["CLAUDE_CODE_OAUTH_TOKEN"])
            else:
                print(json.loads((config_root / "auth.json").read_text())["tokens"]["access_token"])
            raise SystemExit(3)
        if MODE == "credential_prefix_leak":
            if RUNTIME == "claude":
                secret = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
            else:
                secret = json.loads((config_root / "auth.json").read_text())["tokens"]["access_token"]
            print(secret[:12])
            raise SystemExit(3)
        if MODE == "raw_auth_leak" and RUNTIME == "codex":
            sys.stdout.buffer.write((config_root / "auth.json").read_bytes())
            sys.stdout.flush()
            raise SystemExit(3)
        if MODE == "credential_short_prefix_leak":
            if RUNTIME == "claude":
                secret = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
            else:
                secret = json.loads(
                    (config_root / "auth.json").read_text()
                )["tokens"]["access_token"]
            print(secret[:7])
            raise SystemExit(3)
        if MODE == "output_limit_stdout_credential_tail":
            if RUNTIME == "claude":
                secret = os.environ["CLAUDE_CODE_OAUTH_TOKEN"].encode()
            else:
                secret = json.loads(
                    (config_root / "auth.json").read_text()
                )["tokens"]["access_token"].encode()
            sys.stdout.buffer.write((b"x" * (64 * 1024)) + secret)
            sys.stdout.flush()
            time.sleep(60)
            raise SystemExit(0)
        if MODE in {{
            "codex_refresh_output_limit_stderr",
            "codex_unclassifiable_output_limit_stderr",
        }}:
            auth_path = config_root / "auth.json"
            if MODE == "codex_refresh_output_limit_stderr":
                refreshed = json.loads(CODEX_AUTH)
                refreshed["tokens"]["access_token"] = REFRESHED_CODEX_TOKEN
                auth_path.write_text(
                    json.dumps(refreshed, separators=(",", ":")) + "\\n",
                    encoding="utf-8",
                )
                tail = REFRESHED_CODEX_TOKEN.encode()
            else:
                auth_path.write_bytes(
                    b'{{"tokens":{{"access_token":["unclassifiable"]}}}}\\n'
                )
                tail = b"unclassifiable-refresh-tail"
            auth_path.chmod(0o600)
            sys.stderr.buffer.write((b"x" * (64 * 1024)) + tail)
            sys.stderr.flush()
            time.sleep(60)
            raise SystemExit(0)
        if MODE == "codex_refresh_escaped_raw_leak":
            raw = (
                b'{{"tokens":{{"access_token":'
                b'"\\\\u2603-refreshed-escaped-token"}}}}\\n'
            )
            auth_path = config_root / "auth.json"
            auth_path.write_bytes(raw)
            auth_path.chmod(0o600)
            sys.stdout.buffer.write(raw)
            sys.stdout.flush()
            raise SystemExit(3)

        if MODE == "control_layout":
            control_paths = [path_head, config_root]
            for group in value.get("hooks", {{}}).get("PreToolUse", []):
                for hook_spec in group.get("hooks", []):
                    for argument in shlex.split(hook_spec["command"]):
                        if argument.startswith("$"):
                            print(json.dumps({{"type": "control_layout_error"}}))
                            raise SystemExit(9)
                        candidate = Path(argument)
                        if candidate.is_absolute():
                            control_paths.append(candidate)
            if any(path == state_root or state_root in path.parents for path in control_paths):
                print(json.dumps({{"type": "control_layout_error"}}))
                raise SystemExit(9)

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
        tool_name = "Bash" if RUNTIME == "claude" else "exec_command"
        tool_input = {{"command": command}}
        if MODE == "codex_non_shell_mutation":
            tool_name = "apply_patch"
            tool_input = {{"patch": "forged-control-mutation"}}
        payload = {{
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "cwd": os.getcwd(),
            "tool_input": tool_input,
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
        if MODE == "codex_non_shell_mutation":
            if not denied:
                candidates = [
                    Path(os.environ["HOME"]) / ".claude" / "settings.json",
                    Path(os.environ["CODEX_HOME"]) / "hooks.json",
                    Path(os.environ["HOME"]) / ".local" / "share" /
                    "claude-config" / "pre_pr_tribunal" / "hook_common.py",
                    Path(os.environ["PATH"].split(os.pathsep, 1)[0]) / "gh",
                    Path(os.environ["HOME"]).parent / "hosts",
                    Path(os.environ["HOME"]).parent / "probe-command-guard.py",
                ]
                for candidate in candidates:
                    try:
                        candidate.write_text("forged", encoding="utf-8")
                    except OSError:
                        pass
            print(json.dumps({{"type": "hook_result", "events": events}}))
            raise SystemExit(0)
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


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_subscription_is_default_and_environment_auth_is_explicit_opt_in(
    fake_runtimes, tmp_path, runtime
):
    fake = fake_runtimes()
    default_result, default_work = _run_probe(
        fake, tmp_path / "subscription", runtime=runtime
    )
    environment_result, environment_work = _run_probe(
        fake,
        tmp_path / "environment",
        runtime=runtime,
        auth_source="environment",
    )

    assert default_result.returncode == environment_result.returncode == 0
    assert json.loads(default_result.stdout)[runtime]["status"] == "PASS"
    assert json.loads(environment_result.stdout)[runtime]["status"] == "PASS"
    assert not default_work.exists()
    assert not environment_work.exists()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_environment_auth_is_isolated_to_the_selected_runtime_provider(
    fake_runtimes, tmp_path, runtime
):
    fake = fake_runtimes(**{runtime: "environment_isolation"})

    result, work_dir = _run_probe(
        fake,
        tmp_path,
        runtime=runtime,
        auth_source="environment",
    )

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report[runtime]["status"] == "PASS"
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("runtime", "relative_path"),
    [
        ("claude", Path(".claude/.credentials.json")),
        ("codex", Path(".codex/auth.json")),
    ],
)
def test_subscription_missing_credentials_fail_closed_without_environment_fallback(
    fake_runtimes, tmp_path, runtime, relative_path
):
    fake = fake_runtimes()
    (fake.caller_home / relative_path).unlink()

    result, work_dir = _run_probe(fake, tmp_path, runtime=runtime)

    runtime_report = json.loads(result.stdout)[runtime]
    assert result.returncode != 0
    assert runtime_report["status"] == "CREDENTIAL_MISSING"
    assert "test-anthropic-key" not in result.stdout
    assert "test-openai-key" not in result.stdout
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("runtime", "relative_path"),
    [
        ("claude", Path(".claude/.credentials.json")),
        ("codex", Path(".codex/auth.json")),
    ],
)
def test_subscription_rejects_symlink_and_non_owner_only_credentials(
    fake_runtimes, tmp_path, runtime, relative_path
):
    fake = fake_runtimes()
    credential = fake.caller_home / relative_path
    original = credential.read_bytes()
    credential.unlink()
    target = tmp_path / f"{runtime}-target"
    target.write_bytes(original)
    target.chmod(0o600)
    credential.symlink_to(target)

    symlink_result, symlink_work = _run_probe(
        fake, tmp_path / "symlink", runtime=runtime
    )
    assert json.loads(symlink_result.stdout)[runtime]["status"] == "CREDENTIAL_UNSAFE"
    assert not symlink_work.exists()

    credential.unlink()
    credential.write_bytes(original)
    credential.chmod(0o640)
    mode_result, mode_work = _run_probe(fake, tmp_path / "mode", runtime=runtime)
    assert json.loads(mode_result.stdout)[runtime]["status"] == "CREDENTIAL_UNSAFE"
    assert not mode_work.exists()


def test_secure_credential_reader_rejects_wrong_owner_simulation(
    fake_runtimes, monkeypatch
):
    module = _load_probe_module()
    fake = fake_runtimes()
    real_uid = os.geteuid()
    monkeypatch.setattr(module.os, "geteuid", lambda: real_uid + 1)

    with pytest.raises(module.ProbeFailure) as raised:
        module._read_secure_credential(
            fake.caller_home, ".claude", ".credentials.json"
        )

    assert raised.value.code == "CREDENTIAL_UNSAFE"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("duplicate", "CREDENTIAL_MALFORMED"),
        ("malformed", "CREDENTIAL_MALFORMED"),
        ("oversize", "CREDENTIAL_OVERSIZE"),
        ("expired", "CREDENTIAL_EXPIRED"),
        ("bool_expiry", "CREDENTIAL_MALFORMED"),
        ("empty_token", "CREDENTIAL_MALFORMED"),
        ("unreadable", "CREDENTIAL_UNREADABLE"),
    ],
)
def test_claude_subscription_credentials_fail_with_stable_status(
    fake_runtimes, tmp_path, case, expected
):
    fake = fake_runtimes()
    credential = fake.caller_home / ".claude/.credentials.json"
    if case == "duplicate":
        credential.write_text(
            '{"claudeAiOauth":{"accessToken":"first","accessToken":"second",'
            '"expiresAt":4102444800000}}',
            encoding="utf-8",
        )
    elif case == "malformed":
        credential.write_text("{", encoding="utf-8")
    elif case == "oversize":
        credential.write_bytes(b"{" + b"x" * (1024 * 1024) + b"}")
    elif case == "expired":
        credential.write_text(
            '{"claudeAiOauth":{"accessToken":"expired-token",'
            '"expiresAt":1}}',
            encoding="utf-8",
        )
    elif case == "bool_expiry":
        credential.write_text(
            '{"claudeAiOauth":{"accessToken":"bool-token",'
            '"expiresAt":true}}',
            encoding="utf-8",
        )
    elif case == "empty_token":
        credential.write_text(
            '{"claudeAiOauth":{"accessToken":"",'
            '"expiresAt":4102444800000}}',
            encoding="utf-8",
        )
    else:
        credential.chmod(0o000)
    if case != "unreadable":
        credential.chmod(0o600)

    result, work_dir = _run_probe(fake, tmp_path, runtime="claude")

    assert json.loads(result.stdout)["claude"]["status"] == expected
    assert result.returncode != 0
    assert str(fake.caller_home) not in result.stdout
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("duplicate", "CREDENTIAL_MALFORMED"),
        ("malformed", "CREDENTIAL_MALFORMED"),
        ("oversize", "CREDENTIAL_OVERSIZE"),
        ("unreadable", "CREDENTIAL_UNREADABLE"),
    ],
)
def test_codex_subscription_credentials_fail_with_stable_status(
    fake_runtimes, tmp_path, case, expected
):
    fake = fake_runtimes()
    credential = fake.caller_home / ".codex/auth.json"
    if case == "duplicate":
        credential.write_text(
            '{"tokens":{"access_token":"first","access_token":"second"}}',
            encoding="utf-8",
        )
    elif case == "malformed":
        credential.write_text("{", encoding="utf-8")
    elif case == "oversize":
        credential.write_bytes(b"{" + b"x" * (1024 * 1024) + b"}")
    else:
        credential.chmod(0o000)
    if case != "unreadable":
        credential.chmod(0o600)

    result, work_dir = _run_probe(fake, tmp_path, runtime="codex")

    assert json.loads(result.stdout)["codex"]["status"] == expected
    assert result.returncode != 0
    assert str(fake.caller_home) not in result.stdout
    assert not work_dir.exists()


@pytest.mark.parametrize(
    "token_value",
    [
        "short",
        ["nested-list-secret"],
        {"nested": "nested-object-secret"},
    ],
)
def test_codex_rejects_short_or_structured_token_like_values(
    fake_runtimes, token_value
):
    module = _load_probe_module()
    fake = fake_runtimes()
    credential = fake.caller_home / ".codex/auth.json"
    credential.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "valid-primary-access-token",
                    "refresh_token": token_value,
                }
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    credential.chmod(0o600)

    with pytest.raises(module.ProbeFailure) as raised:
        module._load_codex_subscription_auth(fake.caller_home)

    assert raised.value.code == "CREDENTIAL_MALFORMED"


def test_codex_benign_nested_metadata_does_not_enter_secret_inventory(
    fake_runtimes, tmp_path
):
    auth_document = (
        b'{"auth_mode":"chatgpt","tokens":'
        b'{"access_token":"valid-primary-access-token"},'
        b'"metadata":{"labels":["benign-public-value"]}}\n'
    )
    fake = fake_runtimes(codex_auth_document=auth_document)

    result, work_dir = _run_probe(fake, tmp_path, runtime="codex")

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["codex"]["status"] == "PASS"
    assert not work_dir.exists()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_runtime_control_references_have_no_writable_state_ancestor(
    fake_runtimes, tmp_path, runtime
):
    fake = fake_runtimes(**{runtime: "control_layout"})
    result, work_dir = _run_probe(fake, tmp_path, runtime=runtime)
    report = json.loads(result.stdout)

    assert report[runtime]["status"] == "PASS"
    assert result.returncode == 0
    assert not work_dir.exists()


def test_claude_expiry_must_cover_the_complete_two_phase_runtime_budget(
    fake_runtimes, monkeypatch
):
    module = _load_probe_module()
    fake = fake_runtimes()
    now = 2_000_000_000.0
    configured_child_deadlines = (
        (3 * module.RUNTIME_TIMEOUT_SECONDS)
        + (2 * module.INTERNAL_TIMEOUT_SECONDS)
    )
    required_scheduling_cushion = 30
    expires_at = int(
        (
            now
            + configured_child_deadlines
            + required_scheduling_cushion
            - 1
        )
        * 1000
    )
    credential = fake.caller_home / ".claude/.credentials.json"
    credential.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": fake.claude_token,
                    "expiresAt": expires_at,
                }
            }
        ),
        encoding="utf-8",
    )
    credential.chmod(0o600)
    monkeypatch.setattr(module.time, "time", lambda: now)

    with pytest.raises(module.ProbeFailure) as raised:
        module._load_claude_subscription_auth(fake.caller_home)

    assert raised.value.code == "CREDENTIAL_EXPIRED"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_probe_preserves_live_subscription_source_bytes_and_metadata(
    fake_runtimes, tmp_path, runtime
):
    fake = fake_runtimes()
    credential = fake.caller_home / (
        ".claude/.credentials.json" if runtime == "claude" else ".codex/auth.json"
    )
    before_bytes = credential.read_bytes()
    before = credential.stat()

    result, work_dir = _run_probe(fake, tmp_path, runtime=runtime)

    after = credential.stat()
    assert result.returncode == 0
    assert credential.read_bytes() == before_bytes
    assert (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_nlink,
        after.st_size,
        after.st_atime_ns,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_nlink,
        before.st_size,
        before.st_atime_ns,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    assert not work_dir.exists()


def test_codex_staged_auth_can_refresh_without_mutating_live_source(
    fake_runtimes, tmp_path
):
    fake = fake_runtimes(codex="codex_refresh")
    source = fake.caller_home / ".codex/auth.json"
    before_bytes = source.read_bytes()
    before = source.stat()

    result, work_dir = _run_probe(fake, tmp_path, runtime="codex")

    after = source.stat()
    assert result.returncode == 0
    assert json.loads(result.stdout)["codex"]["status"] == "PASS"
    assert source.read_bytes() == before_bytes
    assert (
        after.st_ino,
        after.st_mode,
        after.st_atime_ns,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) == (
        before.st_ino,
        before.st_mode,
        before.st_atime_ns,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    assert "rotated-codex-access-token" not in result.stdout
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("runtime", "mode"),
    [
        ("claude", "credential_leak"),
        ("claude", "credential_prefix_leak"),
        ("codex", "credential_leak"),
        ("codex", "credential_prefix_leak"),
        ("claude", "credential_short_prefix_leak"),
        ("codex", "credential_short_prefix_leak"),
        ("codex", "raw_auth_leak"),
        ("codex", "codex_refresh_leak"),
        ("codex", "codex_refresh_escaped_raw_leak"),
    ],
)
def test_subscription_secret_leaks_are_sensitive_and_capture_hashes_are_withheld(
    fake_runtimes, tmp_path, runtime, mode
):
    fake = fake_runtimes(**{runtime: mode})

    result, work_dir = _run_probe(fake, tmp_path, runtime=runtime)

    runtime_report = json.loads(result.stdout)[runtime]
    phase = runtime_report["missing_verdict"]
    assert runtime_report["status"] == "SENSITIVE_OUTPUT"
    assert phase["sensitivity"]["high_risk"] is True
    assert "CREDENTIAL" in phase["sensitivity"]["reasons"]
    assert phase["capture"]["stdout_sha256"] == "WITHHELD"
    assert phase["capture"]["stderr_sha256"] == "WITHHELD"
    assert fake.claude_token not in result.stdout
    for secret in (
        "fixture-codex-access-token",
        "fixture-codex-refresh-token",
        "fixture-codex-id-token",
        "rotated-codex-access-token",
    ):
        assert secret not in result.stdout
        assert hashlib.sha256(secret.encode()).hexdigest() not in result.stdout
    assert not work_dir.exists()


def test_exact_initial_codex_auth_document_with_escaped_token_is_sensitive(
    fake_runtimes, tmp_path
):
    escaped_auth = (
        b'{"auth_mode":"chatgpt","tokens":'
        b'{"access_token":"\\u2603-long-escaped-token"}}\n'
    )
    fake = fake_runtimes(
        codex="raw_auth_leak",
        codex_auth_document=escaped_auth,
    )

    result, work_dir = _run_probe(fake, tmp_path, runtime="codex")

    runtime_report = json.loads(result.stdout)["codex"]
    phase = runtime_report["missing_verdict"]
    assert runtime_report["status"] == "SENSITIVE_OUTPUT"
    assert phase["sensitivity"]["high_risk"] is True
    assert "CREDENTIAL" in phase["sensitivity"]["reasons"]
    assert phase["capture"]["stdout_sha256"] == "WITHHELD"
    assert phase["capture"]["stderr_sha256"] == "WITHHELD"
    assert "\\u2603-long-escaped-token" not in result.stdout
    assert not work_dir.exists()


def test_codex_version_refresh_leak_is_classified_from_post_execution_stage(
    fake_runtimes, tmp_path
):
    fake = fake_runtimes(codex="codex_version_refresh_leak")

    result, work_dir = _run_probe(fake, tmp_path, runtime="codex")

    runtime_report = json.loads(result.stdout)["codex"]
    assert runtime_report["status"] == "SENSITIVE_OUTPUT"
    assert runtime_report["sensitivity"] == {
        "detected": True,
        "high_risk": True,
        "reasons": ["CREDENTIAL"],
    }
    assert runtime_report["capture"]["stdout_sha256"] == "WITHHELD"
    assert "rotated-codex-access-token" not in result.stdout
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("runtime", "mode"),
    [
        ("claude", "version_initial_leak_zero"),
        ("codex", "version_initial_leak_zero"),
        ("codex", "codex_version_refresh_leak_zero"),
    ],
)
def test_zero_exit_version_credential_leaks_withhold_all_derived_fields(
    fake_runtimes, tmp_path, runtime, mode
):
    fake = fake_runtimes(**{runtime: mode})

    result, work_dir = _run_probe(fake, tmp_path, runtime=runtime)

    runtime_report = json.loads(result.stdout)[runtime]
    assert runtime_report["status"] == "SENSITIVE_OUTPUT"
    assert runtime_report["phase"] == "version"
    assert runtime_report["version"] == "WITHHELD"
    assert runtime_report.get("version_capture_sha256", "WITHHELD") == "WITHHELD"
    assert runtime_report["capture"] == {
        "exit_class": "ZERO",
        "stdout_sha256": "WITHHELD",
        "stderr_sha256": "WITHHELD",
    }
    assert not work_dir.exists()


def test_version_sensitivity_precedes_a_colliding_control_digest_breach(
    fake_runtimes, tmp_path, monkeypatch
):
    module = _load_probe_module()
    fake = fake_runtimes(claude="version_initial_leak_zero")
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    original_digest = module._protected_digest
    calls = 0

    def breach_after_isolation(paths):
        nonlocal calls
        calls += 1
        digest = original_digest(paths)
        return "0" * 64 if calls >= 4 else digest

    monkeypatch.setattr(module, "_protected_digest", breach_after_isolation)
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
            ]
        )

    runtime_report = json.loads(stdout.getvalue())["claude"]
    assert exit_code != 0
    assert runtime_report["status"] == "SENSITIVE_OUTPUT"
    assert runtime_report["version"] == "WITHHELD"
    assert runtime_report["capture"]["stdout_sha256"] == "WITHHELD"
    assert runtime_report["capture"]["stderr_sha256"] == "WITHHELD"
    assert not work_dir.exists()


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
    capture = report["claude"]["missing_verdict"]["capture"]
    assert capture["stdout_sha256"] == "WITHHELD"
    assert capture["stderr_sha256"] == "WITHHELD"
    assert len(result.stdout.encode("utf-8")) < 16 * 1024
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("runtime", "mode", "expected_status"),
    [
        ("claude", "output_limit_stdout_credential_tail", "OUTPUT_LIMIT"),
        ("codex", "codex_refresh_output_limit_stderr", "OUTPUT_LIMIT"),
        (
            "codex",
            "codex_unclassifiable_output_limit_stderr",
            "CREDENTIAL_STAGING_FAILED",
        ),
    ],
)
def test_output_limit_withholds_hashes_for_uninspected_credential_tails(
    fake_runtimes, tmp_path, runtime, mode, expected_status
):
    fake = fake_runtimes(**{runtime: mode})

    result, work_dir = _run_probe(fake, tmp_path, runtime=runtime)

    runtime_report = json.loads(result.stdout)[runtime]
    phase = runtime_report["missing_verdict"]
    assert result.returncode != 0
    assert runtime_report["status"] == expected_status
    assert phase["capture"]["stdout_sha256"] == "WITHHELD"
    assert phase["capture"]["stderr_sha256"] == "WITHHELD"
    assert "rotated-codex-access-token" not in result.stdout
    assert not work_dir.exists()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_probe_times_out_runtime_and_cleans_only_its_work_dir(
    fake_runtimes, tmp_path, monkeypatch, runtime
):
    module = _load_probe_module()
    monkeypatch.setattr(module, "RUNTIME_TIMEOUT_SECONDS", 0.1)
    fake = fake_runtimes(**{runtime: "timeout"})
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
                runtime,
                "--repo-source",
                str(REPO),
                "--work-dir",
                str(work_dir),
            ]
        )

    report = json.loads(stdout.getvalue())
    assert exit_code != 0
    assert report[runtime]["status"] == "TIMEOUT"
    assert outside.read_text(encoding="utf-8") == "preserve"
    assert not work_dir.exists()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize(
    ("mode", "marker_name"),
    [
        ("signal_wait_version", "signal-version-ready"),
        ("signal_wait_phase", "signal-phase-ready"),
    ],
)
def test_probe_signals_cleanup_staged_codex_auth_and_control_roots(
    fake_runtimes,
    tmp_path,
    signum,
    mode,
    marker_name,
):
    fake = fake_runtimes(codex=mode)
    temp_root = tmp_path / "controller-tmp"
    temp_root.mkdir(mode=0o700)
    environment = dict(fake.env, TMPDIR=str(temp_root))
    work_dir = tmp_path / "probe"
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--runtime",
            "codex",
            "--repo-source",
            str(REPO),
            "--work-dir",
            str(work_dir),
        ],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    marker = work_dir / "tmp" / marker_name
    deadline = time.monotonic() + 15
    while not marker.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            process.communicate(timeout=5)
            pytest.fail(f"probe did not reach {marker_name}")
        time.sleep(0.02)
    assert process.poll() is None
    assert (work_dir / "auth-stage/codex-auth.json").is_file()

    os.kill(process.pid, signum)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0
    assert stderr == ""
    assert json.loads(stdout)["status"] == "INTERRUPTED"
    assert not work_dir.exists()
    assert not list(temp_root.glob("pre-pr-tribunal-controls-*"))


def test_keyboard_interrupt_immediately_after_codex_staging_cleans_every_root(
    fake_runtimes, tmp_path, monkeypatch
):
    module = _load_probe_module()
    fake = fake_runtimes()
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    allocated = {
        "pre-pr-tribunal-probe-": tmp_path / "default-probe",
        "pre-pr-tribunal-controls-": tmp_path / "default-controls",
    }

    def allocate(*, prefix):
        target = allocated[prefix]
        target.mkdir(mode=0o700)
        return str(target)

    def interrupt_after_stage(*_args, **_kwargs):
        stage = allocated["pre-pr-tribunal-probe-"] / "auth-stage/codex-auth.json"
        assert stage.is_file()
        raise KeyboardInterrupt

    monkeypatch.setattr(module.tempfile, "mkdtemp", allocate)
    monkeypatch.setattr(module, "_probe_runtime", interrupt_after_stage)
    stdout = io.StringIO()
    interrupted = False
    try:
        with redirect_stdout(stdout):
            exit_code = module.main(
                ["--runtime", "codex", "--repo-source", str(REPO)]
            )
    except KeyboardInterrupt:
        interrupted = True
        exit_code = None

    assert interrupted is False
    assert exit_code != 0
    assert json.loads(stdout.getvalue())["status"] == "INTERRUPTED"
    assert all(not path.exists() for path in allocated.values())


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
    allocated = {
        "pre-pr-tribunal-probe-": tmp_path / "default-probe",
        "pre-pr-tribunal-controls-": tmp_path / "default-controls",
    }
    prefixes = []

    def allocate(*, prefix):
        prefixes.append(prefix)
        target = allocated[prefix]
        target.mkdir(mode=0o700)
        return str(target)

    monkeypatch.setattr(module.tempfile, "mkdtemp", allocate)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = module.main(
            ["--runtime", "claude", "--repo-source", str(REPO)]
        )

    assert exit_code == 0
    assert json.loads(stdout.getvalue())["status"] == "PASS"
    assert prefixes == ["pre-pr-tribunal-probe-", "pre-pr-tribunal-controls-"]
    assert all(not target.exists() for target in allocated.values())


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
    with module._EvidenceRecorder() as evidence:
        module._make_fake_gh(fake_bin, evidence.address, repo)
        result = subprocess.run(
            [str(fake_bin / "gh"), *arguments],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        counts = evidence.counts()

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert counts == (0, 1)


def test_fake_gh_rejects_wrong_cwd_and_second_exact_call(tmp_path):
    module = _load_probe_module()
    repo = tmp_path / "repo"
    wrong_repo = tmp_path / "wrong-repo"
    repo.mkdir()
    wrong_repo.mkdir()
    fake_bin = tmp_path / "fake-bin"
    with module._EvidenceRecorder() as evidence:
        module._make_fake_gh(fake_bin, evidence.address, repo)
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
        counts = evidence.counts()

    assert wrong_cwd.returncode != 0
    assert first.returncode == 0
    assert duplicate.returncode != 0
    assert counts == (1, 2)


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
        [sys.executable, str(guard), "claude"],
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
        [sys.executable, str(guard), "claude"],
        input=json.dumps(wrong_command),
        text=True,
        capture_output=True,
        check=False,
    )
    wrong_cwd = dict(base, cwd=str(tmp_path))
    rejected_cwd = subprocess.run(
        [sys.executable, str(guard), "claude"],
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


@pytest.mark.parametrize(
    "payload",
    [
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"patch": "forged"},
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "unknown_tool",
            "tool_input": {},
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {
                "command": "gh pr create --title canary --body canary",
            },
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec_command",
            "tool_input": {},
        },
    ],
)
def test_codex_matcherless_guard_denies_nonexact_or_commandless_tools(
    tmp_path, payload
):
    module = _load_probe_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    guard = tmp_path / "guard.py"
    module._make_probe_guard(guard, repo)
    request = dict(payload, cwd=str(repo))

    result = subprocess.run(
        [sys.executable, str(guard), "codex"],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
    )

    event = json.loads(result.stdout)
    assert result.returncode == 0
    assert result.stderr == ""
    assert event["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_evidence_counts_are_not_manufactured_by_writable_probe_files(tmp_path):
    module = _load_probe_module()
    repo = tmp_path / "repo"
    fake_bin = tmp_path / "fake-bin"
    repo.mkdir()
    with module._EvidenceRecorder() as evidence:
        module._make_fake_gh(fake_bin, evidence.address, repo)
        (tmp_path / "canary-count").write_text("1\n", encoding="ascii")
        (tmp_path / "invalid-canary-count").write_text("1\n", encoding="ascii")
        assert evidence.counts() == (0, 0)
        result = subprocess.run(
            [
                str(fake_bin / "gh"),
                "pr",
                "create",
                "--title",
                "canary",
                "--body",
                "canary",
            ],
            cwd=repo,
            check=False,
        )
        assert result.returncode == 0
        assert evidence.counts() == (1, 0)


def test_probe_install_removes_runtime_irrelevant_skill_symlinks(tmp_path):
    module = _load_probe_module()
    work_dir = tmp_path / "work"
    repo = work_dir / "repo"
    home = tmp_path / "controls" / "home"
    work_dir.mkdir()
    home.mkdir(parents=True)
    module._create_probe_repo(repo, work_dir)

    module._install(REPO, home, repo)

    for runtime in (".claude", ".codex"):
        target = home / runtime / "skills/pre-pr-tribunal"
        assert not target.exists()
        assert not target.is_symlink()


def test_control_digest_excludes_codex_staged_credential_bytes(tmp_path):
    module = _load_probe_module()
    control_root = tmp_path / "controls"
    placeholder = control_root / "home/.codex/auth.json"
    placeholder.parent.mkdir(parents=True, mode=0o700)
    placeholder.write_text("{}\n", encoding="utf-8")
    placeholder.chmod(0o600)
    stage = tmp_path / "work/auth-stage/codex-auth.json"
    module._stage_codex_auth(stage, b'{"tokens":{"access_token":"first-secret"}}\n')
    before = module._protected_digest((control_root,))

    module._stage_codex_auth(
        stage, b'{"tokens":{"access_token":"different-secret"}}\n'
    )
    after = module._protected_digest((control_root,))

    assert before == after
    assert placeholder.read_bytes() == b"{}\n"


def test_codex_staging_failure_is_stable_and_cleans_all_disposable_state(
    fake_runtimes, tmp_path, monkeypatch
):
    module = _load_probe_module()
    fake = fake_runtimes()
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)

    def fail_stage(_path, _data):
        raise module.ProbeFailure("CREDENTIAL_STAGING_FAILED")

    monkeypatch.setattr(module, "_stage_codex_auth", fail_stage)
    allocated = {
        "pre-pr-tribunal-probe-": tmp_path / "default-probe",
        "pre-pr-tribunal-controls-": tmp_path / "default-controls",
    }

    def allocate(*, prefix):
        target = allocated[prefix]
        target.mkdir(mode=0o700)
        return str(target)

    monkeypatch.setattr(module.tempfile, "mkdtemp", allocate)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = module.main(
            ["--runtime", "codex", "--repo-source", str(REPO)]
        )

    report = json.loads(stdout.getvalue())
    assert exit_code != 0
    assert report["codex"]["status"] == "CREDENTIAL_STAGING_FAILED"
    assert all(not path.exists() for path in allocated.values())


def _probe_boundary_layout(module, tmp_path, *, nested_controls):
    work_dir = tmp_path / "boundary"
    repo = work_dir / "repo"
    home = work_dir / "home"
    control_root = (
        work_dir / "controls" if nested_controls else tmp_path / "controls"
    )
    control_home = control_root / "home"
    fake_bin = control_root / "fake-bin"
    work_dir.mkdir()
    home.mkdir()
    control_home.mkdir(parents=True)
    module._create_probe_repo(repo, home)
    hosts_file = control_root / "hosts"
    guard = control_root / "guard.py"
    module._make_hosts_file(hosts_file)
    module._make_probe_guard(guard, repo)
    module._install(REPO, control_home, repo)
    module._install_probe_guard(control_home, guard)
    (control_home / ".codex/auth.json").write_text("{}\n", encoding="utf-8")
    (control_home / ".codex/auth.json").chmod(0o600)
    return work_dir, repo, home, control_root, fake_bin, hosts_file, guard


def test_isolation_verifier_protects_external_control_root_and_sinkholes_github(
    tmp_path,
):
    module = _load_probe_module()
    (
        work_dir,
        repo,
        home,
        control_root,
        fake_bin,
        hosts_file,
        guard,
    ) = _probe_boundary_layout(module, tmp_path, nested_controls=False)
    with module._EvidenceRecorder() as evidence:
        module._make_fake_gh(fake_bin, evidence.address, repo)
        before = module._protected_digest((control_root,))
        module._verify_isolation(
            work_dir=work_dir,
            repo=repo,
            home=home,
            control_root=control_root,
            fake_gh=fake_bin / "gh",
            hosts_file=hosts_file,
            guard=guard,
            evidence=evidence,
        )
        after = module._protected_digest((control_root,))

    assert before == after


def test_isolation_verifier_rejects_control_root_below_runtime_work_dir(tmp_path):
    module = _load_probe_module()
    (
        work_dir,
        repo,
        home,
        control_root,
        fake_bin,
        hosts_file,
        guard,
    ) = _probe_boundary_layout(module, tmp_path, nested_controls=True)
    with module._EvidenceRecorder() as evidence:
        module._make_fake_gh(fake_bin, evidence.address, repo)
        with pytest.raises(module.ProbeFailure) as raised:
            module._verify_isolation(
                work_dir=work_dir,
                repo=repo,
                home=home,
                control_root=control_root,
                fake_gh=fake_bin / "gh",
                hosts_file=hosts_file,
                guard=guard,
                evidence=evidence,
            )

    assert raised.value.code == "ISOLATION_UNAVAILABLE"


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


def test_codex_non_shell_mutation_is_denied_and_controls_remain_immutable(
    fake_runtimes, tmp_path
):
    fake = fake_runtimes(codex="codex_non_shell_mutation")
    result, work_dir = _run_probe(fake, tmp_path, runtime="codex")

    runtime = json.loads(result.stdout)["codex"]
    assert result.returncode != 0
    assert runtime["status"] == "CANARY_MISMATCH"
    assert runtime["phase"] == "pass_verdict"
    _assert_phase_counts(
        runtime["pass_verdict"],
        denied=True,
        canary_count=0,
        invalid_call_count=0,
    )
    for phase in ("missing_verdict", "pass_verdict"):
        assert runtime[phase]["controls_intact"] is True
        assert len(runtime[phase]["control_sha256"]) == 64
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
    assert phase["capture"]["stdout_sha256"] == "WITHHELD"
    assert phase["capture"]["stderr_sha256"] == "WITHHELD"
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
    auth_source = "environment" if mode == "auth_api_key_leak" else None
    result, work_dir = _run_probe(
        fake, tmp_path, runtime="claude", auth_source=auth_source
    )

    runtime = json.loads(result.stdout)["claude"]
    sensitivity = runtime["missing_verdict"]["sensitivity"]
    assert runtime["status"] == "SENSITIVE_OUTPUT"
    assert sensitivity["detected"] is True
    assert sensitivity["high_risk"] is True
    assert not work_dir.exists()
