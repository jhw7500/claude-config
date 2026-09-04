"""PreToolUse hooks reach the model only through hookSpecificOutput.

Raw stdout on this event is discarded by the harness, so a hook that prints
plain text runs, matches, and still says nothing. These tests pin the envelope
for the hooks that deliver guidance.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).parents[1] / "hooks"


def run(script: str, payload: object, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        cwd=str(cwd),
        env=dict(os.environ),
    )


def context_of(result: subprocess.CompletedProcess[str]) -> str:
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    return specific["additionalContext"]


def test_agent_name_delivery_warns_through_the_envelope(tmp_path: Path) -> None:
    result = run(
        "agent-name-delivery-hook.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {"name": "reviewer", "prompt": "review this"},
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "[AGENT-NAME-DELIVERY]" in context_of(result)


def test_agent_name_delivery_stays_silent_without_a_name(tmp_path: Path) -> None:
    result = run(
        "agent-name-delivery-hook.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {"prompt": "review this"},
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_bg_task_start_notice_uses_the_envelope(tmp_path: Path) -> None:
    result = run(
        "bg-task-progress-hook.py",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "envelope-test",
            "cwd": str(tmp_path),
            "tool_use_id": "toolu_envelope_test",
            "tool_name": "Bash",
            "tool_input": {
                "command": "sleep 1",
                "description": "sleep probe",
                "run_in_background": True,
            },
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "[BG-TASK-START]" in context_of(result)


def test_bg_task_stays_silent_for_a_foreground_call(tmp_path: Path) -> None:
    result = run(
        "bg-task-progress-hook.py",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "envelope-test",
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
