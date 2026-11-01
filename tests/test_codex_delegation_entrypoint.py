"""기존 CLI의 opt-in 라우팅·기본 HOME·설치 symlink 회귀."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/delegation-ratio.py"
TS = datetime(2026, 8, 20, 12).astimezone().isoformat()


def codex_tree(root):
    root.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "timestamp": TS, "payload": {
            "id": "main", "session_id": "main", "source": "cli", "cli_version": "0.166.0"}},
        {"type": "response_item", "timestamp": TS, "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "c", "input": "text(1)"}},
    ]
    (root / "rollout.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return root


def invoke(*args, script=SCRIPT, home=None):
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
    return subprocess.run([sys.executable, str(script), *args],
                          env=env, text=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("as_json", [False, True])
def test_explicit_claude_is_byte_identical_to_default(tmp_path, as_json):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": TS, "message": {"content": [
            {"type": "tool_use", "name": "Bash"},
            {"type": "tool_use", "name": "Agent"},
        ]},
    }) + "\n")
    args = ["--root", str(tmp_path), "--overall"] + (["--json"] if as_json else [])
    baseline = invoke(*args)
    explicit = invoke(*args, "--source", "claude")
    assert baseline.returncode == explicit.returncode == 0
    assert explicit.stdout == baseline.stdout
    if as_json:
        assert set(json.loads(explicit.stdout)) == {"overall", "days"}


def test_source_codex_forwards_explicit_root(tmp_path):
    root = codex_tree(tmp_path / "codex")
    result = invoke("--source", "codex", "--root", str(root), "--overall", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["provider"] == "codex"
    assert payload["overall"]["main_tool_calls"] == 1
    assert payload["overall"]["opaque_calls"] == 1


def test_codex_default_root_is_not_claude_root(tmp_path):
    codex_tree(tmp_path / ".codex/sessions")
    result = invoke("--source", "codex", "--overall", "--json", home=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["overall"]["main_tool_calls"] == 1


def test_symlink_entrypoint_resolves_helper_in_source_repository(tmp_path):
    root = codex_tree(tmp_path / "records")
    link = tmp_path / "bin/delegation-ratio.py"
    link.parent.mkdir()
    link.symlink_to(SCRIPT)
    result = invoke("--source", "codex", "--root", str(root), "--overall", "--json", script=link)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["overall"]["main_tool_calls"] == 1


def test_codex_failure_exit_and_stderr_propagate(tmp_path):
    result = invoke("--source", "codex", "--root", str(tmp_path / "missing"), "--overall")
    assert result.returncode == 1
    assert "not found" in result.stderr
    assert not result.stdout


def test_since_and_json_options_reach_codex_parser(tmp_path):
    root = codex_tree(tmp_path / "records")
    result = invoke("--source", "codex", "--root", str(root), "--overall",
                    "--since", "2026-08-21", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["overall"]["main_tool_calls"] == 0
