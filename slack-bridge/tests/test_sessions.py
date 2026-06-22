import json
import os
import time

import sessions


def _write_session(projects_dir, proj, sid, cwd, lines, mtime=None):
    d = os.path.join(projects_dir, proj)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{sid}.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_list_sessions_orders_and_extracts(tmp_path):
    pd = str(tmp_path)
    _write_session(pd, "-home-x-proj-a", "11111111-aaaa", "/home/x/proj/a", [
        {"type": "mode", "sessionId": "11111111-aaaa", "mode": "default"},
        {"type": "user", "cwd": "/home/x/proj/a",
         "message": {"role": "user", "content": "fix the parser bug"}},
    ], mtime=1000)
    _write_session(pd, "-home-x-proj-b", "22222222-bbbb", "/home/x/proj/b", [
        {"type": "user", "cwd": "/home/x/proj/b",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "add slack feature"}]}},
    ], mtime=2000)
    got = sessions.list_sessions(pd)
    assert [s.session_id for s in got] == ["22222222-bbbb", "11111111-aaaa"]
    assert got[0].title == "add slack feature"
    assert got[0].cwd == "/home/x/proj/b"
    assert got[0].folder == "b"
    assert got[1].title == "fix the parser bug"


def test_list_sessions_skips_injected_and_meta(tmp_path):
    pd = str(tmp_path)
    _write_session(pd, "-p", "33333333-cccc", "/home/x/p", [
        {"type": "last-prompt", "sessionId": "33333333-cccc"},
        {"type": "user", "cwd": "/home/x/p",
         "message": {"role": "user", "content": "<command-name>/foo</command-name>"}},
        {"type": "user", "cwd": "/home/x/p",
         "message": {"role": "user", "content": "the real first message"}},
    ])
    s = sessions.list_sessions(pd)[0]
    assert s.title == "the real first message"


def test_list_sessions_limit(tmp_path):
    pd = str(tmp_path)
    for i in range(5):
        _write_session(pd, f"-p{i}", f"id{i}", f"/c/{i}", [
            {"type": "user", "cwd": f"/c/{i}",
             "message": {"role": "user", "content": f"m{i}"}},
        ], mtime=1000 + i)
    assert len(sessions.list_sessions(pd, limit=2)) == 2


def test_find_session_by_prefix(tmp_path):
    pd = str(tmp_path)
    _write_session(pd, "-p", "abcdef12-3456-7890", "/c", [
        {"type": "user", "cwd": "/c",
         "message": {"role": "user", "content": "hi"}},
    ])
    assert sessions.find_session(pd, "abcdef12").session_id == "abcdef12-3456-7890"
    assert sessions.find_session(pd, "nope") is None
