import json
import os

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


def test_find_session_rejects_metachars(tmp_path):
    assert sessions.find_session(str(tmp_path), "../etc/passwd") is None
    assert sessions.find_session(str(tmp_path), "*") is None
    assert sessions.find_session(str(tmp_path), "") is None


def test_last_user_and_assistant(tmp_path):
    pd = str(tmp_path)
    _write_session(pd, "-p", "sid-last", "/c", [
        {"type": "user", "cwd": "/c",
         "message": {"role": "user", "content": "first msg"}},
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "first reply"}]}},
        {"type": "user",
         "message": {"role": "user",
                     "content": "<system-reminder>ignore me</system-reminder>"}},
        {"type": "user",
         "message": {"role": "user", "content": "latest question"}},
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [{"type": "tool_use", "name": "x"},
                                 {"type": "text", "text": "latest answer"}]}},
    ])
    s = sessions.list_sessions(pd)[0]
    assert s.title == "first msg"
    assert s.last_user == "latest question"
    assert s.last_assistant == "latest answer"


def _fake_proc(root, pid, args, cwd=None):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
    if cwd is not None:
        (d / "cwd").symlink_to(cwd)


def test_scan_live_classifies_processes(tmp_path):
    proc = tmp_path / "proc"
    work = tmp_path / "w1"
    work.mkdir()
    sid_bg = "07db4bcc-1f7b-4935-b3ee-aa43885537bc"
    sid_src = "260bbcca-bec2-4eee-bdfc-72275bb85765"
    sid_resume = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    sid_eq = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    sid_short = "cccccccc-dddd-eeee-ffff-000000000000"
    # interactive TUIs without id on cmdline -> cwd candidates (count=2)
    _fake_proc(proc, 100, ["claude", "--dangerously-skip-permissions"], cwd=str(work))
    _fake_proc(proc, 109, ["claude"], cwd=str(work))
    # bg-pty-host: --session-id is live, --resume source is NOT
    _fake_proc(proc, 101, [
        "claude", "bg-pty-host", "--bg-pty-host", "/tmp/x.sock", "131", "51", "--",
        "/v/2.1.204", "--session-id", sid_bg, "--fork-session",
        "--resume", f"/p/{sid_src}.jsonl",
    ])
    # resumed sessions in all supported arg forms -> exact ids
    _fake_proc(proc, 102, ["/home/u/.local/bin/claude", "--resume", sid_resume], cwd=str(work))
    _fake_proc(proc, 107, ["claude", f"--resume={sid_eq}"], cwd=str(work))
    _fake_proc(proc, 108, ["claude", "-r", sid_short], cwd=str(work))
    # transient/helper processes -> ignored (incl. flags before the subcommand)
    _fake_proc(proc, 103, ["claude", "-p", "hi", "--resume", sid_src], cwd=str(work))
    _fake_proc(proc, 104, ["claude", "daemon", "run"], cwd=str(work))
    _fake_proc(proc, 105, ["claude", "bg-spare", "--bg-spare", "/tmp/s.sock"], cwd=str(work))
    _fake_proc(proc, 106, ["python3", "bridge.py"], cwd=str(work))
    _fake_proc(proc, 110, ["claude", "--debug", "mcp", "serve"], cwd=str(work))
    # npm-style node wrapper: counts as a TUI; daemon under node is still skipped
    _fake_proc(proc, 111, ["node", "/usr/local/bin/claude", "--foo"], cwd=str(work))
    _fake_proc(proc, 112, ["node", "/usr/local/bin/claude", "daemon", "run"], cwd=str(work))
    _fake_proc(proc, 113, ["node", "/usr/lib/other/cli.js"], cwd=str(work))
    # --resume with an extensionless path still yields the id
    sid_path = "dddddddd-eeee-ffff-0000-111111111111"
    _fake_proc(proc, 114, ["claude", "--resume", f"/p2/{sid_path}"], cwd=str(work))
    open_ids, live_cwds = sessions.scan_live(str(proc))
    assert open_ids == {sid_bg, sid_resume, sid_eq, sid_short, sid_path}
    assert live_cwds == {str(work): 3}


def test_list_sessions_live_annotation(tmp_path):
    pd = str(tmp_path / "projects")
    _write_session(pd, "-a", "11111111-aaaa", "/w/open", [
        {"type": "user", "cwd": "/w/open", "message": {"role": "user", "content": "x"}},
    ], mtime=4000)
    _write_session(pd, "-b", "22222222-bbbb", "/w/tui", [
        {"type": "user", "cwd": "/w/tui", "message": {"role": "user", "content": "y"}},
    ], mtime=3000)
    _write_session(pd, "-b2", "44444444-dddd", "/w/tui", [
        {"type": "user", "cwd": "/w/tui", "message": {"role": "user", "content": "y2"}},
    ], mtime=2000)
    _write_session(pd, "-c", "33333333-cccc", "/w/none", [
        {"type": "user", "cwd": "/w/none", "message": {"role": "user", "content": "z"}},
    ], mtime=1000)
    got = sessions.list_sessions(pd, live=({"11111111-aaaa"}, {"/w/tui": 1}))
    # one TUI in /w/tui -> only the newest session there is a 'maybe' candidate
    assert [s.live for s in got] == ["open", "maybe", "closed", "closed"]
    found = sessions.find_session(pd, "22222222", live=(set(), {"/w/tui": 1}))
    assert found.live == "maybe"


def test_list_live_sessions_full_scan(tmp_path):
    pd = str(tmp_path / "projects")
    # newest sessions are all closed noise; the open one is the OLDEST
    for i in range(20):
        _write_session(pd, f"-n{i}", f"00000000-{i:04d}", f"/noise/{i}", [
            {"type": "user", "cwd": f"/noise/{i}",
             "message": {"role": "user", "content": "n"}},
        ], mtime=5000 + i)
    _write_session(pd, "-old", "99999999-aaaa", "/w/old", [
        {"type": "user", "cwd": "/w/old", "message": {"role": "user", "content": "o"}},
    ], mtime=100)
    _write_session(pd, "-tui", "88888888-bbbb", "/w/tui", [
        {"type": "user", "cwd": "/w/tui", "message": {"role": "user", "content": "t"}},
    ], mtime=200)
    got = sessions.list_live_sessions(
        pd, live=({"99999999-aaaa"}, {"/w/tui": 1}))
    assert {(s.session_id, s.live) for s in got} == {
        ("99999999-aaaa", "open"), ("88888888-bbbb", "maybe"),
    }
    assert sessions.list_live_sessions(pd, live=(set(), {})) == []


def test_list_live_sessions_stops_early(tmp_path, monkeypatch):
    pd = str(tmp_path / "projects")
    # targets are the NEWEST files; older noise must never be peeked
    _write_session(pd, "-open", "99999999-aaaa", "/w/open", [
        {"type": "user", "cwd": "/w/open", "message": {"role": "user", "content": "o"}},
    ], mtime=9000)
    _write_session(pd, "-tui", "88888888-bbbb", "/w/tui", [
        {"type": "user", "cwd": "/w/tui", "message": {"role": "user", "content": "t"}},
    ], mtime=8000)
    for i in range(20):
        _write_session(pd, f"-n{i}", f"00000000-{i:04d}", f"/noise/{i}", [
            {"type": "user", "cwd": f"/noise/{i}",
             "message": {"role": "user", "content": "n"}},
        ], mtime=1000 + i)
    peeks = []
    real_peek = sessions._peek_cwd
    monkeypatch.setattr(sessions, "_peek_cwd",
                        lambda p: peeks.append(p) or real_peek(p))
    got = sessions.list_live_sessions(pd, live=({"99999999-aaaa"}, {"/w/tui": 1}))
    assert len(got) == 2
    assert len(peeks) == 1  # only the tui candidate; loop broke before the noise


def test_pending_input_when_no_reply(tmp_path):
    pd = str(tmp_path)
    _write_session(pd, "-p", "sid-pending", "/c", [
        {"type": "user", "cwd": "/c",
         "message": {"role": "user", "content": "q1"}},
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "a1"}]}},
        {"type": "user",
         "message": {"role": "user", "content": "q2 no reply yet"}},
    ])
    s = sessions.list_sessions(pd)[0]
    assert s.last_user == "q2 no reply yet"  # the actual latest prompt
    assert s.last_assistant == ""  # no reply to it yet
