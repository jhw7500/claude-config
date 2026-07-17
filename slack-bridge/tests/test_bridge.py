import os

# bridge.py builds the slack App at import time from env -> set dummy env first.
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("SLACK_CHANNEL_ID", "C_TEST")
os.environ.setdefault("SLACK_ALLOWED_USER_ID", "U_TEST")

import bridge


def test_is_authorized():
    ok = {"channel": "C_TEST", "user": "U_TEST"}
    assert bridge.is_authorized(ok, "C_TEST", "U_TEST")
    assert not bridge.is_authorized({"channel": "C_OTHER", "user": "U_TEST"}, "C_TEST", "U_TEST")
    assert not bridge.is_authorized({"channel": "C_TEST", "user": "U_X"}, "C_TEST", "U_TEST")
    assert not bridge.is_authorized({"channel": "C_TEST", "user": "U_TEST", "bot_id": "B1"}, "C_TEST", "U_TEST")
    assert not bridge.is_authorized({"channel": "C_TEST", "user": "U_TEST", "subtype": "message_changed"}, "C_TEST", "U_TEST")


def test_parse_command():
    assert bridge.parse_command("sessions") == ("sessions", "")
    assert bridge.parse_command("list") == ("sessions", "")
    assert bridge.parse_command("sessions live") == ("sessions", "live")
    assert bridge.parse_command("sessions all") == ("sessions", "all")
    assert bridge.parse_command("list live") == ("sessions", "live")
    # unknown list-args stay prompts (no command hijacking)
    assert bridge.parse_command("list the files in src") == ("run", "list the files in src")
    assert bridge.parse_command("sessions in this repo") == ("run", "sessions in this repo")
    assert bridge.parse_command("select 3") == ("select", "3")
    assert bridge.parse_command("select abcd1234") == ("select", "abcd1234")
    assert bridge.parse_command("clear") == ("clear", "")
    assert bridge.parse_command("status") == ("status", "")
    assert bridge.parse_command("fork add a test") == ("fork", "add a test")
    assert bridge.parse_command("force just do it") == ("force", "just do it")
    assert bridge.parse_command("last") == ("last", "")
    assert bridge.parse_command("refresh") == ("last", "")
    # refresh-prefixed real instructions must still run as prompts (not hijacked)
    assert bridge.parse_command("refresh the cache please") == ("run", "refresh the cache please")
    assert bridge.parse_command("last thing to do") == ("run", "last thing to do")
    assert bridge.parse_command("just do the thing") == ("run", "just do the thing")


def test_thread_map_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_STATE_FILE", str(tmp_path / "threads.json"))
    bridge._thread_session.clear()
    bridge._thread_session["1700000000.000100"] = "sess-abc"
    bridge._save_threads()
    # reload from disk into a fresh dict
    loaded = bridge._load_threads()
    assert loaded == {"1700000000.000100": "sess-abc"}
