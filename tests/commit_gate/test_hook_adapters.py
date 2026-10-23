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
