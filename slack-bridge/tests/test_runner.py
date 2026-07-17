import importlib
import json

import pytest

import config
import runner


def test_build_command_basics():
    cmd = runner.build_command("SID", "do a thing")
    assert cmd[:3] == ["claude", "-p", "do a thing"]
    assert "--resume" in cmd and cmd[cmd.index("--resume") + 1] == "SID"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--permission-mode") + 1] == config.PERMISSION_MODE
    # every deny rule present
    for rule in config.DENY_TOOLS:
        assert rule in cmd
    assert "--fork-session" not in cmd


def test_build_command_fork():
    assert "--fork-session" in runner.build_command("SID", "x", fork=True)


def test_permission_mode_env_parsing(monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_PERMISSION_MODE", "  acceptEdits  ")
    importlib.reload(config)
    assert config.PERMISSION_MODE == "acceptEdits"
    monkeypatch.setenv("CLAUDE_BRIDGE_PERMISSION_MODE", "")
    importlib.reload(config)
    assert config.PERMISSION_MODE == "bypassPermissions"  # empty -> default
    monkeypatch.setenv("CLAUDE_BRIDGE_PERMISSION_MODE", "bypasspermissions")  # case typo
    with pytest.raises(SystemExit):
        importlib.reload(config)
    monkeypatch.delenv("CLAUDE_BRIDGE_PERMISSION_MODE")
    importlib.reload(config)  # restore module state for other tests


def test_build_command_honors_permission_mode(monkeypatch):
    monkeypatch.setattr(config, "PERMISSION_MODE", "acceptEdits")
    cmd = runner.build_command("SID", "x")
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


def test_parse_result_success():
    stdout = json.dumps({
        "type": "result", "is_error": False, "result": "PONG",
        "session_id": "abc", "total_cost_usd": 0.25,
        "permission_denials": [],
    })
    r = runner.parse_result(stdout)
    assert r.ok and r.text == "PONG" and r.session_id == "abc"
    assert r.cost_usd == 0.25 and r.denials == []


def test_parse_result_error_and_denials():
    stdout = json.dumps({
        "is_error": True, "result": "blocked",
        "session_id": "abc", "total_cost_usd": 0,
        "permission_denials": [{"tool_name": "Bash"}],
    })
    r = runner.parse_result(stdout)
    assert not r.ok and r.denials == ["Bash"]


def test_is_active_threshold():
    assert runner.is_active(1000.0, now=1000.0 + config.ACTIVE_THRESHOLD_SECONDS - 1)
    assert not runner.is_active(1000.0, now=1000.0 + config.ACTIVE_THRESHOLD_SECONDS + 1)


def test_run_turn_extracts_json_from_noisy_stdout(monkeypatch):
    import subprocess
    payload = '{"result":"ok","session_id":"s1","is_error":false,"total_cost_usd":0,"permission_denials":[]}'

    class _P:
        returncode = 0
        stdout = "WARNING: something\n" + payload
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    r = runner.run_turn("s1", "/tmp", "hi")
    assert r.ok and r.text == "ok" and r.session_id == "s1"
