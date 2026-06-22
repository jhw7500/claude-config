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
    assert bridge.parse_command("select 3") == ("select", "3")
    assert bridge.parse_command("select abcd1234") == ("select", "abcd1234")
    assert bridge.parse_command("clear") == ("clear", "")
    assert bridge.parse_command("status") == ("status", "")
    assert bridge.parse_command("fork add a test") == ("fork", "add a test")
    assert bridge.parse_command("just do the thing") == ("run", "just do the thing")
