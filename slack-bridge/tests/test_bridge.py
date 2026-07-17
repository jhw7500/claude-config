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


def test_thread_for_reverse_lookup():
    bridge._thread_session.clear()
    assert bridge._thread_for("sess-a") is None
    bridge._thread_session.update({
        "1700000001.000100": "sess-a",
        "1700000005.000100": "sess-a",   # newer binding wins
        "1700000003.000100": "sess-b",
    })
    assert bridge._thread_for("sess-a") == "1700000005.000100"
    assert bridge._thread_for("sess-b") == "1700000003.000100"
    assert bridge._thread_for("sess-x") is None
    bridge._thread_session.clear()


def _info(sid="11111111-aaaa", live="closed", turns=3):
    import sessions
    return sessions.SessionInfo(
        session_id=sid, cwd="/w/x", mtime=1000.0, title="t",
        branch="main", repo="repo", turns=turns, live=live,
    )


def test_render_sessions_badges_and_buttons():
    items = [_info(live="open"), _info(sid="22222222-bbbb", live="maybe")]
    text, blocks = bridge._render_sessions(items)
    assert "🖥️ 실행중" in text and "🟡 열림후보" in text
    assert "sessions live" in text  # scope hint in default mode
    # context header + one section per session, each with a select button
    assert len(blocks) == 3
    assert all(b["accessory"]["action_id"] == "select_session" for b in blocks[1:])
    text_live, _ = bridge._render_sessions(items, mode="live")
    assert "sessions all" in text_live


def test_home_view_structure():
    view = bridge._home_view([_info()])
    assert view["type"] == "home"
    ids = [b.get("type") for b in view["blocks"]]
    assert ids[0] == "header"
    refresh = [e for b in view["blocks"] if b.get("type") == "actions"
               for e in b["elements"]]
    assert refresh and refresh[0]["action_id"] == "home_refresh"
    sections = [b for b in view["blocks"] if b.get("type") == "section"]
    assert sections[0]["accessory"]["action_id"] == "select_session"
    empty = bridge._home_view([])
    assert "세션이 없습니다" in str(empty["blocks"][-1])
    # notice (thread-opened feedback) lands right under the header
    noticed = bridge._home_view([_info()], notice="🧵 열림 → 링크")
    assert noticed["blocks"][1]["type"] == "section"
    assert "🧵 열림" in noticed["blocks"][1]["text"]["text"]


def test_home_view_persistent_thread_link(monkeypatch):
    # a session with a bound thread keeps its link across refreshes
    monkeypatch.setattr(bridge, "_thread_link",
                        lambda sid: "https://x.slack.com/archives/C/p1" if sid == "11111111-aaaa" else None)
    view = bridge._home_view([_info(), _info(sid="22222222-bbbb")])
    sections = [b for b in view["blocks"] if b.get("type") == "section"]
    assert "열린 스레드로 가기" in sections[0]["text"]["text"]
    assert "열린 스레드로 가기" not in sections[1]["text"]["text"]


def test_thread_map_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_STATE_FILE", str(tmp_path / "threads.json"))
    bridge._thread_session.clear()
    bridge._thread_session["1700000000.000100"] = "sess-abc"
    bridge._save_threads()
    # reload from disk into a fresh dict
    loaded = bridge._load_threads()
    assert loaded == {"1700000000.000100": "sess-abc"}
