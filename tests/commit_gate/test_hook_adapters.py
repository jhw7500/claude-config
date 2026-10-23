import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "hooks" / "commit_gate"


def run(name, payload, home):
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run([sys.executable, str(HOOKS / name)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def payload(tool, repo, command, camel=False):
    if camel:
        return {"hookEventName": "PreToolUse", "toolName": tool, "toolInput": {"command": command}, "cwd": str(repo)}
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}, "cwd": str(repo)}


@pytest.mark.parametrize("name,tool", [("claude_hook.py", "Bash"), ("codex_hook.py", "exec_command")])
@pytest.mark.parametrize("camel", [False, True])
def test_hook_adapters_emit_deny_shape(repo, home, name, tool, camel):
    r = run(name, payload(tool, repo, 'git commit -m "bad"', camel), home)
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "heading-contract" in out["permissionDecisionReason"]


@pytest.mark.parametrize("name,tool", [("claude_hook.py", "Bash"), ("codex_hook.py", "exec_command")])
def test_hook_adapters_allow_silently(repo, home, name, tool):
    r = run(name, payload(tool, repo, "git status"), home)
    assert (r.returncode, r.stdout) == (0, "")


def test_codex_non_shell_payload_allowed(repo, home):
    for p in (payload("apply_patch", repo, 'git commit -m "bad"'),
              {"hook_event_name": "PreToolUse", "tool_name": "exec_command",
               "tool_input": {"x": 1}, "cwd": str(repo)}):
        r = run("codex_hook.py", p, home)
        assert (r.returncode, r.stdout) == (0, "")


def test_garbage_stdin_allowed(home):
    r = subprocess.run([sys.executable, str(HOOKS / "claude_hook.py")], input="not json",
                       capture_output=True, text=True, env={**os.environ, "HOME": str(home)})
    assert (r.returncode, r.stdout) == (0, "")


def _with_workdir(p, workdir):
    p["tool_input"]["workdir"] = workdir
    return p


BAD = 'git commit -m "bad"'


def test_codex_absolute_workdir_overrides_cwd_allow(repo, repo_factory, tmp_path, home):
    other = repo_factory(tmp_path / "other", origin="https://github.com/someone/else.git")
    r = run("codex_hook.py", _with_workdir(payload("exec_command", repo, BAD), str(other)), home)
    assert (r.returncode, r.stdout) == (0, "")


def test_codex_absolute_workdir_overrides_cwd_deny(repo, tmp_path, home):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = run("codex_hook.py", _with_workdir(payload("exec_command", plain, BAD), str(repo)), home)
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("workdir", ["sub", "", 5])
def test_codex_non_absolute_workdir_warns_for_commit(repo, home, workdir):
    r = run("codex_hook.py", _with_workdir(payload("exec_command", repo, BAD), workdir), home)
    assert (r.returncode, r.stdout) == (0, "")
    assert "[commit-gate] Codex workdir가 절대경로가 아니어서 검사하지 않았다" in r.stderr
    r = run("codex_hook.py", _with_workdir(payload("exec_command", repo, "ls"), workdir), home)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_codex_missing_absolute_workdir_allowed(repo, tmp_path, home):
    r = run("codex_hook.py", _with_workdir(payload("exec_command", repo, BAD), str(tmp_path / "nope")), home)
    assert (r.returncode, r.stdout) == (0, "")


def test_claude_ignores_workdir(repo, repo_factory, tmp_path, home):
    other = repo_factory(tmp_path / "other", origin="https://github.com/someone/else.git")
    r = run("claude_hook.py", _with_workdir(payload("Bash", repo, BAD), str(other)), home)
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


RTK_VALID = """feat(x): 결과를 말하는 제목

 ### Contract version

v1

 ### Why

이유다.

 ### Changes

- 변경한다.

 ### Validation

- 실행한 명령과 결과.

 ### References

- https://github.com/jhw7500/automation/issues/237
"""


@pytest.mark.parametrize("prefix", ["rtk ", "rtk proxy "])
def test_rtk_prefixed_violation_denied_for_claude_and_codex(repo, tmp_path, home, prefix):
    cmd = prefix + BAD
    r = run("claude_hook.py", payload("Bash", repo, cmd), home)
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    plain = tmp_path / "plain"
    plain.mkdir()
    r = run("codex_hook.py", _with_workdir(payload("exec_command", plain, cmd), str(repo)), home)
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "heading-contract" in out["permissionDecisionReason"]


def test_rtk_prefixed_valid_message_allowed(repo, tmp_path, home):
    f = tmp_path / "msg.txt"
    f.write_text(RTK_VALID, encoding="utf-8")
    cmd = f"rtk git commit --cleanup=verbatim -F {f}"
    for name, tool in (("claude_hook.py", "Bash"), ("codex_hook.py", "exec_command")):
        r = run(name, payload(tool, repo, cmd), home)
        assert (r.returncode, r.stdout) == (0, ""), name
