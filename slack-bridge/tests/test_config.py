import pytest

import config


def test_load_config_reads_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")
    monkeypatch.setenv("SLACK_ALLOWED_USER_ID", "U123")
    cfg = config.load_config()
    assert cfg.bot_token == "xoxb-1"
    assert cfg.channel_id == "C123"
    assert cfg.allowed_user_id == "U123"
    assert cfg.projects_dir.endswith("/.claude/projects")


def test_load_config_missing_raises(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")
    monkeypatch.setenv("SLACK_ALLOWED_USER_ID", "U123")
    with pytest.raises(SystemExit):
        config.load_config()


def test_deny_tools_block_rm():
    assert any("rm" in rule for rule in config.DENY_TOOLS)
    assert config.ACTIVE_THRESHOLD_SECONDS > 0
