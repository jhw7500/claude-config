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


def test_tool_without_text_field_is_ignored(tmp_path):
    """텍스트 필드가 없는 도구는 검사 대상이 아니다 (Bash 의 command 등)."""
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo '\x01'"},
    }
    assert run_hook(payload).stdout.strip() == ""


def test_unknown_tool_with_text_field_is_scanned(tmp_path):
    """도구명이 아니라 텍스트 필드로 판정한다.

    회귀: PR #27 리뷰 [MEDIUM] — 도구명 하드코딩 시 새 편집 도구가 누락된다.
    matcher 를 넓히기만 하면 훅 수정 없이 커버되어야 한다.
    """
    target = tmp_path / "future.ts"
    target.write_text("x\n", encoding="utf-8")
    payload = {
        "tool_name": "SomeFutureEditTool",
        "tool_input": {"file_path": str(target), "content": "y = /[\x01]/"},
    }
    assert "CTRL-CHAR-GUARD" in context_of(run_hook(payload))


def test_duplicate_text_falls_back_to_relative(tmp_path):
    """같은 텍스트가 파일에 여러 번 있으면 절대 좌표를 단정하지 않는다.

    회귀: PR #27 리뷰 [MEDIUM] + Codex P2 — find() 가 첫 일치를 반환해 이번
    편집과 무관한 줄을 가리키면, 사용자가 기존의 의도된 문자를 지우고 새로 들어온
    제어문자는 그대로 두게 된다.
    """
    target = tmp_path / "dup.ts"
    dup = "bad = /[\x01]/\n"
    target.write_text(dup + "filler\n" + dup, encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target), "old_string": "x", "new_string": dup},
    }
    context = context_of(run_hook(payload))
    assert "상대 위치" in context
    assert "dup.ts:1" not in context
    assert "dup.ts:3" not in context


def test_path_with_braces_does_not_crash(tmp_path):
    """경로에 중괄호가 있어도 포매팅이 깨지지 않는다.

    회귀: PR #27 리뷰 [MEDIUM] 은 str.format 이 치환된 값 안의 {} 를 재귀
    해석해 KeyError 를 낸다고 봤으나, 실제로는 해석하지 않는다. 이후 % 포매팅
    등으로 바꾸다 실제로 깨지는 것을 막기 위해 고정한다.
    """
    directory = tmp_path / "{monorepo}"
    directory.mkdir()
    target = directory / "a.ts"
    content = "x = /[\x01]/"
    target.write_text(content, encoding="utf-8")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": content},
    }
    result = run_hook(payload)
    assert result.returncode == 0, result.stderr
    assert result.stderr.strip() == ""
    context = context_of(result)
    assert "CTRL-CHAR-GUARD" in context
    assert "{monorepo}" in context


def test_kill_switch(tmp_path):
    result = run_hook(
        write_payload(tmp_path, "x = /[\x01]/\n"),
        env_extra={"CLAUDE_SKIP_CTRLCHAR_GUARD": "1"},
    )
    assert result.stdout.strip() == ""


def test_missing_file_falls_back_to_relative_position(tmp_path):
    """파일을 읽지 못해도 작성 텍스트 기준 상대 위치는 보고한다."""
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(tmp_path / "gone.ts"),
            "content": "x = /[\x01]/",
        },
    }
    context = context_of(run_hook(payload))
    assert "CTRL-CHAR-GUARD" in context
    assert "상대 위치" in context


def test_preexisting_control_char_is_not_reported(tmp_path):
    """기존 파일에 있던 제어문자는 위치 보고에 섞이면 안 된다.

    회귀: PR #26 Claude 리뷰 [MEDIUM] — 트리거는 새 텍스트만 보는데 위치 보고가
    파일 전체를 스캔해, 이번 편집과 무관한 줄을 함께 보고하던 결함.
    """
    target = tmp_path / "test.ts"
    lines = ["line%d" % i for i in range(1, 101)]
    lines[4] = "old = /[\x01]/"      # 5번째 줄 — 편집 전부터 있던 것
    lines[99] = "new = /[\x07]/"     # 100번째 줄 — 이번 편집이 넣은 것
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "line100",
            "new_string": "new = /[\x07]/",
        },
    }
    context = context_of(run_hook(payload))
    assert "test.ts:100" in context
    assert repr("\x07") in context
    assert repr("\x01") not in context
    assert "test.ts:5 " not in context


def test_write_reports_absolute_file_position(tmp_path):
    target = tmp_path / "abs.ts"
    content = "a\nb\nc = /[\x01]/\n"
    target.write_text(content, encoding="utf-8")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": content},
    }
    context = context_of(run_hook(payload))
    assert "abs.ts:3" in context
    assert "(열 7)" in context


@pytest.mark.parametrize("payload", [None, {"tool_name": "Write"}, {"tool_name": 3}])
def test_malformed_input_exits_quietly(payload):
    result = run_hook(payload)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
