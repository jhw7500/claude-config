import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "hooks" / "control-char-guard-hook.py"


def run_hook(payload, env_extra=None) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env.pop("CLAUDE_SKIP_CTRLCHAR_GUARD", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload, ensure_ascii=False) if payload is not None else "",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def context_of(result) -> str:
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return ""
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def write_payload(tmp_path: Path, content: str, tool: str = "Write") -> dict:
    target = tmp_path / "sample.ts"
    target.write_text(content, encoding="utf-8")
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": str(target), "content": content},
    }


def test_detects_control_char_in_written_content(tmp_path):
    # 정규식 character class 안에 raw 0x01 — 실제 사고 형태
    content = "const RE = /[\x01-\x08]/;\n"
    result = run_hook(write_payload(tmp_path, content))
    context = context_of(result)
    assert "CTRL-CHAR-GUARD" in context
    assert repr("\x01") in context
    assert "sample.ts:1" in context


def test_clean_content_is_silent(tmp_path):
    result = run_hook(write_payload(tmp_path, "const RE = /[a-z]/;\n"))
    assert result.stdout.strip() == ""


def test_tab_newline_cr_are_allowed(tmp_path):
    result = run_hook(write_payload(tmp_path, "a\tb\r\nc\n"))
    assert result.stdout.strip() == ""


def test_edit_new_string_is_scanned(tmp_path):
    target = tmp_path / "board-service.ts"
    target.write_text("x = /[\x1b]/\n", encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "y",
            "new_string": "x = /[\x1b]/",
        },
    }
    assert "CTRL-CHAR-GUARD" in context_of(run_hook(payload))


def test_edit_old_string_is_not_scanned(tmp_path):
    """지우는 쪽에 제어문자가 있는 건 오히려 정상 — 발화하면 안 된다."""
    target = tmp_path / "clean.ts"
    target.write_text("x = 1\n", encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "x = /[\x01]/",
            "new_string": "x = 1",
        },
    }
    assert run_hook(payload).stdout.strip() == ""


def test_multiedit_scans_every_edit(tmp_path):
    target = tmp_path / "multi.py"
    target.write_text("ok\n", encoding="utf-8")
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(target),
            "edits": [
                {"old_string": "a", "new_string": "clean"},
                {"old_string": "b", "new_string": "bad\x07"},
            ],
        },
    }
    assert "CTRL-CHAR-GUARD" in context_of(run_hook(payload))


def test_notebook_new_source_is_scanned(tmp_path):
    target = tmp_path / "nb.ipynb"
    target.write_text("{}\n", encoding="utf-8")
    payload = {
        "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": str(target), "new_source": "print('\x0b')"},
    }
    assert "CTRL-CHAR-GUARD" in context_of(run_hook(payload))


def test_other_tools_are_ignored(tmp_path):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo '\x01'"},
    }
    assert run_hook(payload).stdout.strip() == ""


def test_kill_switch(tmp_path):
    result = run_hook(
        write_payload(tmp_path, "x = /[\x01]/\n"),
        env_extra={"CLAUDE_SKIP_CTRLCHAR_GUARD": "1"},
    )
    assert result.stdout.strip() == ""


def test_missing_file_still_warns(tmp_path):
    """위치 특정에 실패해도 경고 자체는 남아야 한다."""
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(tmp_path / "gone.ts"),
            "content": "x = /[\x01]/",
        },
    }
    context = context_of(run_hook(payload))
    assert "CTRL-CHAR-GUARD" in context
    assert "특정하지 못함" in context


@pytest.mark.parametrize("payload", [None, {"tool_name": "Write"}, {"tool_name": 3}])
def test_malformed_input_exits_quietly(payload):
    result = run_hook(payload)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
