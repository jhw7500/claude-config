# Claude Code Slack Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive existing Claude Code sessions from a private Slack channel — a message resumes the chosen session headlessly (`claude -p --resume`), runs one turn, and posts the result back.

**Architecture:** A long-running Python service (systemd `--user`) listens to one private Slack channel via Socket Mode. It scans `~/.claude/projects/*/*.jsonl` to list resumable sessions, lets the user pick one (sticky per channel), then for each message runs `claude -p --resume <id> --output-format json --permission-mode acceptEdits` in that session's cwd and posts the JSON `result` back. No dependency on repowire (its cloud relay is dead and idle peers don't respond — see spec §1).

**Tech Stack:** Python 3.12, `slack_bolt` (Socket Mode), the `claude` CLI (v2.1.185), systemd user service. Spec: `docs/superpowers/specs/2026-06-22-slack-bridge-design.md`.

## Global Constraints

- Language/runtime: Python ≥ 3.10 (host has 3.12). Dependency: `slack_bolt` (pulls `slack_sdk`) only — install into a dedicated venv `slack-bridge/.venv` (gitignored). No other new deps.
- Secrets: read only from `secrets.local.env` (gitignored). Required vars: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID`, `SLACK_ALLOWED_USER_ID`. Never commit real tokens; never print them unmasked.
- Permission boundary: every headless turn runs with `--permission-mode acceptEdits` plus the destructive-command deny list in `config.DENY_TOOLS`. Do not change these defaults.
- Channel must be **private**; manifest uses `groups:history` / `message.groups`.
- Only act on messages where `channel == SLACK_CHANNEL_ID` AND `user == SLACK_ALLOWED_USER_ID`. Ignore everything else (incl. bot messages / subtypes).
- Commits: follow repo convention (Korean `type: 요약` subjects, as in `git log`). End each commit message with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Absolute paths in all docs/messages to the user (repo rule).

## Before You Start

We are on `master` (the default branch). Create a feature branch first:

```bash
cd /home/jhw/ai/opencode/projects/claude-config
git checkout -b feat/slack-bridge
```

Also commit the already-written spec on this branch:

```bash
git add docs/superpowers/specs/2026-06-22-slack-bridge-design.md docs/superpowers/plans/2026-06-22-slack-bridge.md
git commit -m "docs: Slack 브릿지 설계·구현계획 추가

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## File Structure

```
slack-bridge/
  config.py        # env config + DENY_TOOLS + thresholds
  sessions.py      # scan ~/.claude/projects, parse id/cwd/mtime/title
  runner.py        # build+run `claude -p --resume`, parse JSON result
  bridge.py        # slack_bolt Socket Mode app: auth, commands, run path
  requirements.txt # slack_bolt
  claude-slack-bridge.service.template  # systemd unit (placeholders rendered by setup)
  README.md        # service-local docs
  tests/
    conftest.py        # puts slack-bridge/ on sys.path
    test_config.py
    test_sessions.py
    test_runner.py
    test_bridge.py
    fixtures/          # sample .jsonl built by tests at runtime (tmp_path)
manifest/slack-app.yaml          # Slack app manifest (create app "from manifest")
scripts/setup-slack-bridge.sh    # opt-in installer (venv, deps, systemd unit, verify)
secrets.example.env              # (modify) add SLACK_* placeholders
README.md                        # (modify) add "Slack 브릿지" section
.gitignore                       # (modify) ignore slack-bridge/.venv
```

`config.py`/`sessions.py`/`runner.py` are pure and unit-testable without Slack or network. `bridge.py` wires them to Slack; its pure helpers (auth, command parsing) are unit-tested, the live wiring is verified manually.

---

### Task 1: Scaffolding + config module

**Files:**
- Create: `slack-bridge/config.py`
- Create: `slack-bridge/requirements.txt`
- Create: `slack-bridge/tests/conftest.py`
- Create: `slack-bridge/tests/test_config.py`
- Modify: `secrets.example.env` (append SLACK_* block)
- Modify: `.gitignore` (add `slack-bridge/.venv`)

**Interfaces:**
- Produces: `config.load_config() -> Config` with fields `bot_token, app_token, channel_id, allowed_user_id, projects_dir: str`. Module constants `config.DENY_TOOLS: list[str]`, `config.ACTIVE_THRESHOLD_SECONDS: int`.

- [ ] **Step 1: Write the failing test**

Create `slack-bridge/tests/conftest.py`:

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

Create `slack-bridge/tests/test_config.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jhw/ai/opencode/projects/claude-config/slack-bridge && python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'config'`.

- [ ] **Step 3: Write minimal implementation**

Create `slack-bridge/config.py`:

```python
"""Configuration + safety constants for the Claude Code Slack bridge."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Destructive command patterns blocked even under acceptEdits.
# Claude Code permission-rule syntax: "Bash(<prefix>:*)".
# NOTE: exact match semantics are verified live in Task 3, Step 6.
DENY_TOOLS = [
    "Bash(rm:*)",
    "Bash(rmdir:*)",
    "Bash(git push --force:*)",
    "Bash(git push -f:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(mkfs:*)",
    "Bash(dd:*)",
    "Bash(shutdown:*)",
    "Bash(reboot:*)",
]

# A session whose transcript changed within this window is treated as
# "active" (possibly open in a TUI) -> require fork/confirm before resuming.
ACTIVE_THRESHOLD_SECONDS = 90


@dataclass(frozen=True)
class Config:
    bot_token: str
    app_token: str
    channel_id: str
    allowed_user_id: str
    projects_dir: str


def load_config() -> Config:
    def req(name: str) -> str:
        v = os.environ.get(name, "").strip()
        if not v:
            raise SystemExit(f"Missing required env var: {name}")
        return v

    return Config(
        bot_token=req("SLACK_BOT_TOKEN"),
        app_token=req("SLACK_APP_TOKEN"),
        channel_id=req("SLACK_CHANNEL_ID"),
        allowed_user_id=req("SLACK_ALLOWED_USER_ID"),
        projects_dir=os.path.expanduser(
            os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
        ),
    )
```

Create `slack-bridge/requirements.txt`:

```
slack_bolt>=1.18
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jhw/ai/opencode/projects/claude-config/slack-bridge && python -m pytest tests/test_config.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Add secrets placeholders + gitignore**

Append to `secrets.example.env` (after the existing keys):

```
# --- Slack 브릿지 (claude-slack-bridge) ---
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
SLACK_CHANNEL_ID=
SLACK_ALLOWED_USER_ID=
```

Append one line to `.gitignore`:

```
slack-bridge/.venv
```

- [ ] **Step 6: Commit**

```bash
cd /home/jhw/ai/opencode/projects/claude-config
git add slack-bridge/config.py slack-bridge/requirements.txt slack-bridge/tests/conftest.py slack-bridge/tests/test_config.py secrets.example.env .gitignore
git commit -m "feat(slack-bridge): config 모듈 + 시크릿 템플릿

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Session registry

**Files:**
- Create: `slack-bridge/sessions.py`
- Create: `slack-bridge/tests/test_sessions.py`

**Interfaces:**
- Consumes: nothing (reads filesystem only).
- Produces:
  - `sessions.SessionInfo` (frozen dataclass): `session_id: str, cwd: str, mtime: float, title: str`; property `folder: str` (basename of cwd).
  - `sessions.list_sessions(projects_dir: str, limit: int = 15) -> list[SessionInfo]` (newest first by mtime).
  - `sessions.find_session(projects_dir: str, session_id: str) -> SessionInfo | None` (exact id or unique-ish prefix match).

- [ ] **Step 1: Write the failing test**

Create `slack-bridge/tests/test_sessions.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jhw/ai/opencode/projects/claude-config/slack-bridge && python -m pytest tests/test_sessions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sessions'`.

- [ ] **Step 3: Write minimal implementation**

Create `slack-bridge/sessions.py`:

```python
"""Discover resumable Claude Code sessions under ~/.claude/projects."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from glob import glob


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    cwd: str
    mtime: float
    title: str

    @property
    def folder(self) -> str:
        return os.path.basename(self.cwd.rstrip("/")) or self.cwd


def _first_user_text(content) -> str | None:
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text")
        return None
    if isinstance(content, str):
        return content
    return None


def _extract(path: str) -> SessionInfo | None:
    session_id = os.path.splitext(os.path.basename(path))[0]
    cwd = ""
    title = ""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                if not title and d.get("type") == "user":
                    text = _first_user_text((d.get("message") or {}).get("content"))
                    if text and not text.startswith("<"):
                        title = " ".join(text.split())[:80]
                if cwd and title:
                    break
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if not cwd:
        return None
    return SessionInfo(session_id, cwd, mtime, title or "(no title)")


def list_sessions(projects_dir: str, limit: int = 15) -> list[SessionInfo]:
    infos = []
    for p in glob(os.path.join(projects_dir, "*", "*.jsonl")):
        info = _extract(p)
        if info is not None:
            infos.append(info)
    infos.sort(key=lambda s: s.mtime, reverse=True)
    return infos[:limit]


def find_session(projects_dir: str, session_id: str) -> SessionInfo | None:
    candidates = list_sessions(projects_dir, limit=100_000)
    for s in candidates:
        if s.session_id == session_id:
            return s
    for s in candidates:
        if s.session_id.startswith(session_id):
            return s
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jhw/ai/opencode/projects/claude-config/slack-bridge && python -m pytest tests/test_sessions.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/jhw/ai/opencode/projects/claude-config
git add slack-bridge/sessions.py slack-bridge/tests/test_sessions.py
git commit -m "feat(slack-bridge): 세션 레지스트리(스캔/파싱)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Headless runner

**Files:**
- Create: `slack-bridge/runner.py`
- Create: `slack-bridge/tests/test_runner.py`

**Interfaces:**
- Consumes: `config.DENY_TOOLS`, `config.ACTIVE_THRESHOLD_SECONDS`.
- Produces:
  - `runner.TurnResult` (frozen dataclass): `ok: bool, text: str, session_id: str, cost_usd: float, denials: list[str], raw: dict`.
  - `runner.build_command(session_id: str, prompt: str, *, fork: bool = False) -> list[str]`.
  - `runner.parse_result(stdout: str) -> TurnResult`.
  - `runner.is_active(mtime: float, *, now: float | None = None) -> bool`.
  - `runner.run_turn(session_id: str, cwd: str, prompt: str, *, fork: bool = False, timeout: int = 1800) -> TurnResult`.

- [ ] **Step 1: Write the failing test**

Create `slack-bridge/tests/test_runner.py`:

```python
import json

import config
import runner


def test_build_command_basics():
    cmd = runner.build_command("SID", "do a thing")
    assert cmd[:3] == ["claude", "-p", "do a thing"]
    assert "--resume" in cmd and cmd[cmd.index("--resume") + 1] == "SID"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    # every deny rule present
    for rule in config.DENY_TOOLS:
        assert rule in cmd
    assert "--fork-session" not in cmd


def test_build_command_fork():
    assert "--fork-session" in runner.build_command("SID", "x", fork=True)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jhw/ai/opencode/projects/claude-config/slack-bridge && python -m pytest tests/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'runner'`.

- [ ] **Step 3: Write minimal implementation**

Create `slack-bridge/runner.py`:

```python
"""Run a single headless Claude Code turn against a session."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass

from config import ACTIVE_THRESHOLD_SECONDS, DENY_TOOLS


@dataclass(frozen=True)
class TurnResult:
    ok: bool
    text: str
    session_id: str
    cost_usd: float
    denials: list[str]
    raw: dict


def build_command(session_id: str, prompt: str, *, fork: bool = False) -> list[str]:
    cmd = [
        "claude", "-p", prompt,
        "--resume", session_id,
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
    ]
    for rule in DENY_TOOLS:
        cmd += ["--disallowedTools", rule]
    if fork:
        cmd.append("--fork-session")
    return cmd


def is_active(mtime: float, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return (now - mtime) < ACTIVE_THRESHOLD_SECONDS


def parse_result(stdout: str) -> TurnResult:
    data = json.loads(stdout)
    denials = []
    for d in data.get("permission_denials", []) or []:
        if isinstance(d, dict):
            denials.append(d.get("tool_name") or d.get("tool") or json.dumps(d))
        else:
            denials.append(str(d))
    return TurnResult(
        ok=not data.get("is_error", False),
        text=data.get("result", "") or "",
        session_id=data.get("session_id", "") or "",
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        denials=denials,
        raw=data,
    )


def run_turn(session_id: str, cwd: str, prompt: str, *, fork: bool = False,
             timeout: int = 1800) -> TurnResult:
    cmd = build_command(session_id, prompt, fork=fork)
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    if not proc.stdout.strip():
        raise RuntimeError(
            f"claude produced no output (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:500]}"
        )
    return parse_result(proc.stdout)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jhw/ai/opencode/projects/claude-config/slack-bridge && python -m pytest tests/test_runner.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/jhw/ai/opencode/projects/claude-config
git add slack-bridge/runner.py slack-bridge/tests/test_runner.py
git commit -m "feat(slack-bridge): 헤드리스 러너(claude -p --resume)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 6: LIVE verification of the deny list (manual, costs a small amount)**

This confirms the `--disallowedTools "Bash(rm:*)"` rule syntax actually blocks. Create a throwaway session, then attempt a denied command:

```bash
cd /tmp
# 1) create a throwaway session, capture its id
SID=$(claude -p "reply with exactly: READY" --output-format json | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
echo "throwaway session: $SID"
# 2) attempt a denied rm via the bridge's exact flags
cd /home/jhw/ai/opencode/projects/claude-config/slack-bridge
python3 -c "
import json, subprocess, runner
cmd = runner.build_command('$SID', 'Use the Bash tool to run exactly: rm -rf /tmp/__bridge_denytest_nope__ ; then say DONE')
out = subprocess.run(cmd, cwd='/tmp', capture_output=True, text=True, timeout=300).stdout
r = runner.parse_result(out)
print('denials:', r.denials)
print('ok:', r.ok, 'text:', r.text[:120])
"
```

Expected: `denials:` is **non-empty** (contains a `Bash` entry), i.e. the `rm` was blocked.
- If denials is empty AND the model claims it ran `rm`: the deny syntax is wrong → adjust `config.DENY_TOOLS` rule format (try `"Bash(rm:*)"` → `"Bash(rm *)"` or consult `claude --help` / Claude Code permission docs), re-run until blocked, then re-run Task 1/Task 3 unit tests and amend the Task 1 commit.
- Document the confirmed-working syntax in a one-line comment above `DENY_TOOLS`.

---

### Task 4: Slack app (bridge.py)

**Files:**
- Create: `slack-bridge/bridge.py`
- Create: `slack-bridge/tests/test_bridge.py`

**Interfaces:**
- Consumes: `config.load_config`, `sessions.list_sessions/find_session`, `runner.run_turn/is_active`.
- Produces (pure helpers, unit-tested):
  - `bridge.is_authorized(event: dict, channel_id: str, user_id: str) -> bool`
  - `bridge.parse_command(text: str) -> tuple[str, str]` → `(cmd, arg)` where cmd ∈ {`sessions`, `select`, `clear`, `fork`, `status`, `run`}.
- Runtime entrypoint: `python bridge.py` starts Socket Mode and blocks.

- [ ] **Step 1: Write the failing test**

Create `slack-bridge/tests/test_bridge.py`:

```python
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
```

Requires `slack_bolt` importable: `cd slack-bridge && python -m pip install -r requirements.txt` into the active interpreter/venv before running (Task 6 sets up the real venv; for unit tests it must be importable).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jhw/ai/opencode/projects/claude-config/slack-bridge && python -m pytest tests/test_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge'` (or `slack_bolt` if deps not yet installed — install deps, then it fails on `bridge`).

- [ ] **Step 3: Write minimal implementation**

Create `slack-bridge/bridge.py`:

```python
"""Slack <-> Claude Code headless bridge (Socket Mode)."""
from __future__ import annotations

import logging
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config
import runner
import sessions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude-slack-bridge")

CFG = config.load_config()
# token_verification_enabled=False: skip slack_bolt's startup auth.test so the App can
# be built at import with a dummy token (unit tests) and start headless. Token is still
# used for API calls; a bad token surfaces on first send instead of at startup.
app = App(token=CFG.bot_token, token_verification_enabled=False)

# channel_id -> selected session_id (sticky)
_targets: dict[str, str] = {}
# last shown list per channel: index(int) -> session_id
_last_list: dict[str, dict[int, str]] = {}


def is_authorized(event: dict, channel_id: str, user_id: str) -> bool:
    if event.get("bot_id") or event.get("subtype"):
        return False
    return event.get("channel") == channel_id and event.get("user") == user_id


def parse_command(text: str) -> tuple[str, str]:
    t = text.strip()
    low = t.lower()
    if low in ("sessions", "list"):
        return ("sessions", "")
    if low in ("clear", "status"):
        return (low, "")
    for kw in ("select", "fork"):
        if low == kw or low.startswith(kw + " "):
            return (kw, t[len(kw):].strip())
    return ("run", t)


def _resolve_target(channel: str, arg: str) -> str | None:
    """arg may be a list index (from `sessions`) or a session id/prefix."""
    if arg.isdigit():
        return _last_list.get(channel, {}).get(int(arg))
    return arg or None


def _cmd_sessions(channel: str, say) -> None:
    items = sessions.list_sessions(CFG.projects_dir)
    if not items:
        say("세션이 없습니다.")
        return
    _last_list[channel] = {i: s.session_id for i, s in enumerate(items, 1)}
    blocks, lines = [], []
    for i, s in enumerate(items, 1):
        line = f"*{i}.* `{s.folder}` — {s.title}  _(id `{s.session_id[:8]}`)_"
        lines.append(line)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": line},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": f"▶ {i}"},
                "value": s.session_id,
                "action_id": "select_session",
            },
        })
    say(text="\n".join(lines), blocks=blocks)


def _select(channel: str, session_id: str, say) -> None:
    info = sessions.find_session(CFG.projects_dir, session_id)
    if info is None:
        say(f":x: 세션 `{session_id[:8]}` 을(를) 찾지 못했습니다.")
        return
    _targets[channel] = info.session_id
    say(f":white_check_mark: 대상: `{info.folder}` — {info.title} (`{info.session_id[:8]}`)")


def _run_and_reply(channel: str, prompt: str, say, *, fork: bool = False) -> None:
    sid = _targets.get(channel)
    if not sid:
        say("선택된 세션이 없습니다. `sessions` 로 목록을 보고 `select <번호>` 하세요.")
        return
    info = sessions.find_session(CFG.projects_dir, sid)
    if info is None:
        say(f":x: 세션 `{sid[:8]}` 이(가) 사라졌습니다. 다시 `sessions`.")
        return
    if not fork and runner.is_active(info.mtime):
        say(":warning: 이 세션이 방금 활성 상태였습니다(다른 곳에서 열려 있을 수 있음). "
            "`fork <메시지>` 로 분기하거나 잠시 후 다시 시도하세요.")
        return
    say(":hourglass_flowing_sand: 작업 중…")

    def work() -> None:
        try:
            res = runner.run_turn(info.session_id, info.cwd, prompt, fork=fork)
        except Exception as e:  # noqa: BLE001 - surface any failure to Slack
            log.exception("run_turn failed")
            say(f":x: 실행 실패: {e}")
            return
        if fork and res.session_id and res.session_id != info.session_id:
            _targets[channel] = res.session_id
            say(f":twisted_rightwards_arrows: 분기됨 → 새 세션 `{res.session_id[:8]}` (이후 메시지는 여기로)")
        head = "" if res.ok else ":x: (error)\n"
        tail = f"\n:no_entry: 차단된 도구: {', '.join(res.denials)}" if res.denials else ""
        say(f"{head}{res.text}\n\n_💰 ${res.cost_usd:.4f}_{tail}")

    threading.Thread(target=work, daemon=True).start()


@app.event("message")
def on_message(event, say):
    if not is_authorized(event, CFG.channel_id, CFG.allowed_user_id):
        return
    text = (event.get("text") or "").strip()
    if not text:
        return
    channel = event["channel"]
    cmd, arg = parse_command(text)
    if cmd == "sessions":
        _cmd_sessions(channel, say)
    elif cmd == "select":
        target = _resolve_target(channel, arg)
        if not target:
            say("사용법: `select <번호|id>`")
        else:
            _select(channel, target, say)
    elif cmd == "clear":
        _targets.pop(channel, None)
        say("대상 해제됨.")
    elif cmd == "status":
        sid = _targets.get(channel)
        say(f"대상: `{sid[:8]}`  권한: acceptEdits(+deny)" if sid else "대상 없음.")
    elif cmd == "fork":
        _run_and_reply(channel, arg, say, fork=True)
    else:
        _run_and_reply(channel, text, say)


@app.action("select_session")
def on_select_button(ack, body, say):
    ack()
    if body.get("user", {}).get("id") != CFG.allowed_user_id:
        return
    channel = body["channel"]["id"]
    session_id = body["actions"][0]["value"]
    _select(channel, session_id, say)


def main() -> None:
    log.info("Starting claude-slack-bridge (channel=%s)", CFG.channel_id)
    SocketModeHandler(app, CFG.app_token).start()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jhw/ai/opencode/projects/claude-config/slack-bridge && python -m pytest tests/test_bridge.py -v`
Expected: PASS (2 passed). Then run the full suite: `python -m pytest -v` → all pass.

- [ ] **Step 5: Commit**

```bash
cd /home/jhw/ai/opencode/projects/claude-config
git add slack-bridge/bridge.py slack-bridge/tests/test_bridge.py
git commit -m "feat(slack-bridge): Slack Socket Mode 앱(인가/명령/실행)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Slack app manifest + docs

**Files:**
- Create: `manifest/slack-app.yaml`
- Create: `slack-bridge/README.md`
- Modify: `README.md` (repo root — add "Slack 브릿지" row + section)

**Interfaces:** none (artifacts the user consumes manually).

- [ ] **Step 1: Write the manifest**

Create `manifest/slack-app.yaml`:

```yaml
# Slack app manifest for claude-slack-bridge (PRIVATE channel).
# Create: api.slack.com/apps -> Create New App -> From a manifest -> paste this.
# After creating: (1) Basic Information -> App-Level Tokens -> generate a token
# with scope `connections:write` (this is SLACK_APP_TOKEN, xapp-...).
# (2) Install to Workspace -> copy Bot User OAuth Token (SLACK_BOT_TOKEN, xoxb-...).
# (3) Create a PRIVATE channel, /invite the bot, copy its Channel ID (SLACK_CHANNEL_ID, C...).
# For a PUBLIC channel instead: swap groups:history->channels:history and
# message.groups->message.channels.
display_information:
  name: claude-bridge
features:
  bot_user:
    display_name: claude-bridge
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write
      - groups:history
settings:
  event_subscriptions:
    bot_events:
      - message.groups
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
  org_deploy_enabled: false
  token_rotation_enabled: false
```

- [ ] **Step 2: Validate the manifest is well-formed YAML with required keys**

Run:

```bash
python3 -c "
import yaml
m = yaml.safe_load(open('/home/jhw/ai/opencode/projects/claude-config/manifest/slack-app.yaml'))
assert m['settings']['socket_mode_enabled'] is True
assert m['settings']['interactivity']['is_enabled'] is True
assert 'chat:write' in m['oauth_config']['scopes']['bot']
assert 'groups:history' in m['oauth_config']['scopes']['bot']
assert 'message.groups' in m['settings']['event_subscriptions']['bot_events']
print('manifest OK')
"
```

Expected: `manifest OK`.

- [ ] **Step 3: Write service-local README**

Create `slack-bridge/README.md`:

```markdown
# claude-slack-bridge

Slack 비공개 채널에서 기존 Claude Code 세션을 헤드리스로 이어서 작업.

## 동작
메시지 → 선택된 세션을 `claude -p --resume <id> --output-format json
--permission-mode acceptEdits`(+위험 명령 deny)로 한 턴 실행 → 결과 회신.

## 설치
1. `manifest/slack-app.yaml` 로 Slack 앱 생성(From a manifest).
2. App-Level Token(`connections:write`)·Bot Token·비공개 채널 ID·내 Slack user ID 확보.
3. `secrets.local.env` 에 `SLACK_BOT_TOKEN/SLACK_APP_TOKEN/SLACK_CHANNEL_ID/SLACK_ALLOWED_USER_ID` 채우기.
4. `bash scripts/setup-slack-bridge.sh` 실행.

## 채널 명령
- `sessions` / `list` — 최근 세션 목록(버튼)
- `select <번호|id>` — 대상 세션 선택
- (일반 텍스트) — 대상 세션에서 한 턴 실행
- `fork <메시지>` — 대상이 활성일 때 분기 실행
- `clear` / `status`

## 한계
유휴가 아닌 "직접 실행" 방식이라 항상 실시간이지만, 턴마다 세션 transcript
전체를 로드하므로 큰 세션은 턴 비용이 큼(회신에 `💰 비용` 표기). 타 머신/스트리밍은 비목표.
```

- [ ] **Step 4: Add a section to the repo root README**

In `/home/jhw/ai/opencode/projects/claude-config/README.md`, add a row to the "포함" table:

```
| `slack-bridge/` | Slack 비공개 채널 ↔ Claude Code 세션 헤드리스 브릿지. `setup-slack-bridge.sh`로 systemd `--user` 서비스 설치. 상세는 `slack-bridge/README.md` |
```

And append a section near the bottom:

```markdown
## Slack 브릿지

폰/Slack에서 기존 Claude Code 세션을 이어서 작업. (repowire와 무관 — 독립 서비스)

1. `manifest/slack-app.yaml` 로 Slack 앱 생성 → 토큰 3개 + 내 user ID 확보
2. `secrets.local.env` 에 `SLACK_BOT_TOKEN/SLACK_APP_TOKEN/SLACK_CHANNEL_ID/SLACK_ALLOWED_USER_ID`
3. `bash scripts/setup-slack-bridge.sh`  (systemd `--user` 서비스 등록·기동)

상세·명령어·한계는 `slack-bridge/README.md` 참조.
```

- [ ] **Step 5: Commit**

```bash
cd /home/jhw/ai/opencode/projects/claude-config
git add manifest/slack-app.yaml slack-bridge/README.md README.md
git commit -m "docs(slack-bridge): 앱 매니페스트 + README

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Setup script + systemd unit + end-to-end verification

**Files:**
- Create: `slack-bridge/claude-slack-bridge.service.template`
- Create: `scripts/setup-slack-bridge.sh`

**Interfaces:** none (operational). Consumes all prior modules + `secrets.local.env`.

- [ ] **Step 1: Write the systemd unit template**

Create `slack-bridge/claude-slack-bridge.service.template` (placeholders `__PATH__` rendered by the setup script; `%h` = home):

```ini
[Unit]
Description=Claude Code Slack Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/ai/opencode/projects/claude-config/slack-bridge
EnvironmentFile=%h/ai/opencode/projects/claude-config/secrets.local.env
Environment=PATH=__PATH__
ExecStart=%h/ai/opencode/projects/claude-config/slack-bridge/.venv/bin/python bridge.py
Restart=always
RestartSec=5
StandardOutput=append:%h/.claude-slack-bridge.log
StandardError=append:%h/.claude-slack-bridge.log

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Write the setup script**

Create `scripts/setup-slack-bridge.sh`:

```bash
#!/bin/bash
# Slack 브릿지 설치 (opt-in). setup-mcp.sh 패턴.
# 사용: bash scripts/setup-slack-bridge.sh [--dry-run]
#   secrets.local.env 에서 SLACK_* 토큰을 읽어 systemd --user 서비스를 설치/기동.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_DIR="$REPO_DIR/slack-bridge"
DRY_RUN=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) echo "usage: setup-slack-bridge.sh [--dry-run]"; exit 0 ;;
    *) echo "unknown: $a"; exit 1 ;;
  esac
done

if [ ! -f "$REPO_DIR/secrets.local.env" ]; then
  echo "[setup] secrets.local.env 없음 — cp secrets.example.env secrets.local.env 후 SLACK_* 채우기"; exit 1
fi
set -a; . "$REPO_DIR/secrets.local.env"; set +a
for v in SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_CHANNEL_ID SLACK_ALLOWED_USER_ID; do
  if [ -z "${!v:-}" ]; then echo "[setup] $v 비어있음 (secrets.local.env)"; exit 1; fi
done
echo "[setup] tokens OK (bot ${SLACK_BOT_TOKEN:0:9}…, channel $SLACK_CHANNEL_ID)"

CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ]; then echo "[setup] 'claude' not on PATH"; exit 1; fi
SERVICE_PATH="$HOME/.config/systemd/user/claude-slack-bridge.service"
UNIT="$(sed "s|__PATH__|$PATH|g" "$BRIDGE_DIR/claude-slack-bridge.service.template")"

if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] venv: $BRIDGE_DIR/.venv ; deps: slack_bolt"
  echo "[dry-run] would write $SERVICE_PATH:"; echo "$UNIT"
  echo "[dry-run] systemctl --user enable --now claude-slack-bridge.service"
  exit 0
fi

echo "[setup] creating venv + installing deps"
# Prefer uv (no ensurepip needed). Fall back to stdlib venv (requires python3-venv).
if command -v uv >/dev/null 2>&1; then
  uv venv "$BRIDGE_DIR/.venv"
  uv pip install --python "$BRIDGE_DIR/.venv/bin/python" -q -r "$BRIDGE_DIR/requirements.txt"
else
  python3 -m venv "$BRIDGE_DIR/.venv"
  "$BRIDGE_DIR/.venv/bin/pip" install -q -r "$BRIDGE_DIR/requirements.txt"
fi

mkdir -p "$(dirname "$SERVICE_PATH")"
[ -f "$SERVICE_PATH" ] && cp "$SERVICE_PATH" "$SERVICE_PATH.bak"
printf '%s\n' "$UNIT" > "$SERVICE_PATH"
systemctl --user daemon-reload
systemctl --user enable --now claude-slack-bridge.service
sleep 2
systemctl --user --no-pager status claude-slack-bridge.service | head -12 || true
echo "[setup] 완료. 로그: tail -f ~/.claude-slack-bridge.log"
echo "[setup] Slack 채널에서 'sessions' 입력해 동작 확인."
```

Make it executable:

```bash
chmod +x /home/jhw/ai/opencode/projects/claude-config/scripts/setup-slack-bridge.sh
```

- [ ] **Step 3: Lint + dry-run**

Run:

```bash
cd /home/jhw/ai/opencode/projects/claude-config
shellcheck scripts/setup-slack-bridge.sh
bash scripts/setup-slack-bridge.sh --dry-run
```

Expected: shellcheck clean (fix any warnings); dry-run prints the rendered unit and the planned actions without writing anything.

- [ ] **Step 4: Real install (requires real Slack tokens in secrets.local.env)**

Pre-req: the user has created the Slack app from the manifest, joined the bot to the private channel, and filled `secrets.local.env`. Then:

```bash
cd /home/jhw/ai/opencode/projects/claude-config
bash scripts/setup-slack-bridge.sh
systemctl --user is-active claude-slack-bridge.service   # -> active
tail -n 20 ~/.claude-slack-bridge.log                    # -> "Starting claude-slack-bridge ..."
```

Expected: service `active`; log shows startup with no traceback.

- [ ] **Step 5: End-to-end live test in Slack**

In the private channel (as the authorized user):
1. Type `sessions` → a numbered list with `▶` buttons appears.
2. `select 1` (or click a button) → confirms target.
3. Send `reply with exactly: BRIDGE_OK` → bot posts `🤔 작업 중…` then `BRIDGE_OK` + `💰 $...`.
4. Send a message asking it to create a small file in that project, confirm acceptEdits worked (file created).
5. (Optional) ask it to `rm -rf` something → reply shows `차단된 도구: Bash`.
6. Conflict path: open that same session in a TUI, then send a Slack message within 90s → bot replies with the `:warning:` active notice; `fork <msg>` proceeds and reports a new session id.

Expected: all six behave as described. Capture any failure and fix before finishing.

- [ ] **Step 6: Commit**

```bash
cd /home/jhw/ai/opencode/projects/claude-config
git add slack-bridge/claude-slack-bridge.service.template scripts/setup-slack-bridge.sh
git commit -m "feat(slack-bridge): setup 스크립트 + systemd 유닛

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (completed)

**1. Spec coverage:**
- §4/§5 architecture & components → Tasks 2 (registry), 3 (runner), 4 (Slack app). ✓
- §6 commands (`sessions`/`select`/`clear`/`fork`/`status`) → Task 4 `parse_command` + handlers. ✓
- §7 permissions (acceptEdits + deny) → `config.DENY_TOOLS` + `runner.build_command` + live verify Task 3 Step 6. ✓
- §7 authorization (channel + user whitelist) → `bridge.is_authorized`, Task 4. ✓
- §8 deliverables (secrets, manifest, slack-bridge/, setup script, README) → Tasks 1, 5, 6. ✓
- §9 single-reply + cost display → `_run_and_reply` posts `.result` + `💰 cost`. ✓
- §10 slack_bolt + systemd → requirements.txt + Task 6. ✓
- §11 verification → unit tests per task + Task 3 Step 6 + Task 6 Steps 3–5. ✓
- Conflict avoidance (§5.4) → `runner.is_active` + `_run_and_reply` warning/fork. ✓

**2. Placeholder scan:** No "TBD/TODO/handle edge cases" left. The one genuine unknown (exact deny-rule syntax) is handled by an explicit live verification step (Task 3 Step 6) with a documented fix path, not a hand-wave.

**3. Type consistency:** `SessionInfo` fields used consistently (`session_id`, `cwd`, `mtime`, `title`, `folder`). `TurnResult` fields (`ok`, `text`, `session_id`, `cost_usd`, `denials`) match between `parse_result`, tests, and `_run_and_reply`. `parse_command` return tuple `(cmd, arg)` matches the dispatch in `on_message`. `build_command` flags match the live test in Task 3 Step 6.

## Known risk to watch during execution
- Deny-rule syntax (Task 3 Step 6) — if wrong, fix `DENY_TOOLS` format and re-verify before shipping.
- `slack_bolt` must be importable when running `tests/test_bridge.py` (install deps first).
- systemd `EnvironmentFile` reads `secrets.local.env` literally; the SLACK_* values are literal tokens (no `$VAR` expansion needed), so this is fine; unrelated `$HOME`-containing vars in that file are ignored by the bridge.
