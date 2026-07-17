"""Slack <-> Claude Code headless bridge (Socket Mode), thread-per-session.

Each Claude session is bound to a Slack thread: pick a session from the
`sessions` list (▶ button or `select N`) and the bot opens a thread for it.
Replies inside that thread run that session; the bot answers in-thread. The
thread<->session map is persisted so threads survive a service restart.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config
import runner
import sessions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude-slack-bridge")

CFG = config.load_config()
# token_verification_enabled=False: skip slack_bolt's startup auth.test call so the
# App can be constructed at import time with a dummy token (unit tests) and so a
# headless start doesn't fail on token verification. The token is still used for all
# API calls; a bad token surfaces on first send rather than at startup.
app = App(token=CFG.bot_token, token_verification_enabled=False)

# Persistent thread_ts -> session_id map so session threads survive restarts.
_STATE_FILE = os.path.expanduser(
    os.environ.get(
        "CLAUDE_BRIDGE_STATE_FILE", "~/.config/claude-slack-bridge-threads.json"
    )
)


def _load_threads() -> dict[str, str]:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


_thread_session: dict[str, str] = _load_threads()
_state_lock = threading.Lock()
# last shown list per channel: index -> session_id (for `select N`)
_last_list: dict[str, dict[int, str]] = {}

_locks_guard = threading.Lock()
_session_locks: dict[str, threading.Lock] = {}
_last_bridge_run: dict[str, float] = {}  # session_id -> wall-clock time of last bridge turn


def _save_threads() -> None:
    try:
        d = os.path.dirname(_STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_thread_session, f)
        os.replace(tmp, _STATE_FILE)
    except OSError:
        log.exception("failed to persist thread map")


def _lock_for(session_id: str) -> threading.Lock:
    with _locks_guard:
        lk = _session_locks.get(session_id)
        if lk is None:
            lk = threading.Lock()
            _session_locks[session_id] = lk
        return lk


def is_authorized(event: dict, channel_id: str, user_id: str) -> bool:
    if event.get("bot_id") or event.get("subtype"):
        return False
    return event.get("channel") == channel_id and event.get("user") == user_id


def parse_command(text: str) -> tuple[str, str]:
    t = text.strip()
    low = t.lower()
    if low in ("sessions", "list"):
        return ("sessions", "")
    for kw in ("sessions", "list"):
        # only known filters count as args, so "list the files ..." stays a prompt
        if low.startswith(kw + " "):
            arg = low[len(kw):].strip()
            if arg in ("live", "all"):
                return ("sessions", arg)
            break
    if low in ("clear", "status"):
        return (low, "")
    # refresh-style keywords are commands ONLY as an exact single word, so a real
    # instruction like "refresh the cache" still runs as a prompt.
    if low in ("last", "refresh", "recent", "갱신", "최근"):
        return ("last", "")
    for kw in ("select", "fork", "force"):
        if low == kw or low.startswith(kw + " "):
            return (kw, t[len(kw):].strip())
    return ("run", t)


def _resolve_target(channel: str, arg: str) -> str | None:
    """arg may be a list index (from `sessions`) or a session id/prefix."""
    if arg.isdigit():
        return _last_list.get(channel, {}).get(int(arg))
    return arg or None


# SessionInfo.live -> list badge
_BADGE = {"open": "🖥️ 실행중", "maybe": "🟡 열림후보", "closed": "💤 종료"}


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"{s}초"
    m = s // 60
    if m < 60:
        return f"{m}분"
    h = m // 60
    if h < 24:
        return f"{h}시간"
    return f"{h // 24}일"


def _last_exchange(info) -> list[str]:
    """My last prompt + the reply to that prompt (or a 'pending' note)."""
    if not info.last_user:
        return []
    return [
        f"🗣️ *마지막 입력:* {info.last_user}",
        f"🤖 *마지막 출력:* {info.last_assistant or '_(아직 응답 없음)_'}",
    ]


def _show_last(info, say, thread_ts: str | None = None) -> None:
    """Re-fetch and post a session's current last input/output."""
    head = f"🔄 `{info.repo or info.folder}` (`{info.session_id[:8]}`) · ⏱ {_ago(info.mtime)} 전"
    text = "\n".join([head, *_last_exchange(info)])
    if thread_ts:
        say(text=text, thread_ts=thread_ts)
    else:
        say(text=text)


def _cmd_sessions(channel: str, say, mode: str = "") -> None:
    if mode == "live":
        # full-scan candidate matching: an old-mtime open session is not cut
        items = sessions.list_live_sessions(CFG.projects_dir)
    else:
        items = sessions.list_sessions(CFG.projects_dir)
    if not items:
        say("열려 있는 세션이 없습니다. `sessions all`로 전체를 보세요." if mode == "live"
            else "세션이 없습니다.")
        return
    _last_list[channel] = {i: s.session_id for i, s in enumerate(items, 1)}
    blocks, lines = [], []
    for i, s in enumerate(items, 1):
        repo = f"`{s.repo}`" if s.repo else "—"
        branch = f"  ⎇ {s.branch}" if s.branch else ""
        badge = _BADGE.get(s.live, "")
        header = f"*{i}.* {repo}{branch}  {badge}  _(`{s.session_id[:8]}`)_"
        meta = f"📁 `{s.cwd}`"
        title = f"📝 {s.title}" if s.title and s.title != "(no title)" else ""
        summary = f"💬 {s.turns} · ⏱ {_ago(s.mtime)} 전"
        text = "\n".join(p for p in (header, meta, title, summary) if p)
        lines.append(text)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": f"▶ {i}"},
                "value": s.session_id,
                "action_id": "select_session",
            },
        })
    scope = " · 열림 후보만 (`sessions all`=전체)" if mode == "live" else " (`sessions live`=열림만)"
    header = (f"🕘 갱신 {time.strftime('%H:%M:%S', time.localtime())} · "
              f"{len(items)}개 (최신순){scope}")
    blocks.insert(0, {"type": "context", "elements": [{"type": "mrkdwn", "text": header}]})
    say(text=header + "\n" + "\n".join(lines), blocks=blocks)


def _open_thread(session_id: str, say) -> None:
    """Post a session root message and bind the resulting thread to the session."""
    info = sessions.find_session(CFG.projects_dir, session_id)
    if info is None:
        say(f":x: 세션 `{session_id[:8]}` 을(를) 찾지 못했습니다.")
        return
    badge = _BADGE.get(info.live, "")
    header = (
        f"🧵 *{info.repo or info.folder}* 세션 (`{info.session_id[:8]}`) {badge} — "
        "이 스레드에 *답글*로 이어가세요"
    )
    resp = say(text="\n".join([header, *_last_exchange(info)]))
    try:
        root_ts = resp["ts"]
    except (KeyError, TypeError):
        root_ts = resp.get("ts") if isinstance(resp, dict) else None
    if not root_ts:
        say(":x: 스레드 생성 실패(메시지 ts를 받지 못함).")
        return
    with _state_lock:
        _thread_session[str(root_ts)] = info.session_id
        _save_threads()
    say(
        text="여기에 *답글*로 지시를 보내세요. `fork <메시지>`=분기, `force <메시지>`=열림 감지 무시 실행, "
             "`last`=최신 입출력 갱신, `status`/`clear` 가능.",
        thread_ts=root_ts,
    )


def _run_and_reply(say, thread_ts: str, session_id: str, prompt: str, *,
                   fork: bool = False, force: bool = False) -> None:
    info = sessions.find_session(CFG.projects_dir, session_id)
    if info is None:
        say(text=f":x: 세션 `{session_id[:8]}` 이(가) 사라졌습니다. 본문에서 `sessions`.",
            thread_ts=thread_ts)
        return
    bridge_ran_recently = (
        time.time() - _last_bridge_run.get(info.session_id, 0.0)
    ) < config.ACTIVE_THRESHOLD_SECONDS
    if not fork and not force and info.live == "open":
        say(text=":no_entry: 이 세션은 지금 호스트에서 열려 있습니다(프로세스 감지). "
                 "그대로 이어 쓰면 대화가 분기되어 서로의 작업이 보이지 않게 됩니다. "
                 "`fork <메시지>`=분기, 분기 위험을 감수하려면 `force <메시지>`.",
            thread_ts=thread_ts)
        return
    if not fork and not force and not bridge_ran_recently and runner.is_active(info.mtime):
        say(text=":warning: 이 세션이 방금 활성 상태였습니다(다른 곳에서 열려 있을 수 있음). "
                 "`fork <메시지>`로 분기하거나 잠시 후 다시 시도하세요. 무시하려면 `force <메시지>`.",
            thread_ts=thread_ts)
        return
    lock = _lock_for(info.session_id)
    if not lock.acquire(blocking=False):
        say(text=":hourglass_flowing_sand: 이 세션은 이미 작업 중입니다. 잠시 후 다시 시도하세요.",
            thread_ts=thread_ts)
        return
    say(text=":hourglass_flowing_sand: 작업 중…", thread_ts=thread_ts)

    def work() -> None:
        try:
            res = runner.run_turn(info.session_id, info.cwd, prompt, fork=fork)
            _last_bridge_run[info.session_id] = time.time()
            if fork and res.session_id and res.session_id != info.session_id:
                _last_bridge_run[res.session_id] = time.time()
                with _state_lock:
                    _thread_session[str(thread_ts)] = res.session_id
                    _save_threads()
                say(text=f":twisted_rightwards_arrows: 분기됨 → 이 스레드는 이제 새 세션 `{res.session_id[:8]}`",
                    thread_ts=thread_ts)
            head = "" if res.ok else ":x: (error)\n"
            tail = f"\n:no_entry: 차단된 도구: {', '.join(res.denials)}" if res.denials else ""
            body = f"{head}{res.text}\n\n_💰 ${res.cost_usd:.4f}_{tail}"
            if len(body) > 3500:
                body = body[:3500] + "\n…(잘림)"
            if info.live == "maybe":
                body += ("\n:warning: 같은 폴더에 열린 호스트 TUI가 있습니다. "
                         "이 세션이 호스트에 열려 있는 세션이라면 다음부터 `fork <메시지>`를 권장합니다.")
            say(text=body, thread_ts=thread_ts)
        except Exception as e:  # noqa: BLE001
            log.exception("turn failed")
            try:
                say(text=f":x: 실행 실패: {e}", thread_ts=thread_ts)
            except Exception:
                log.exception("failed to post error to Slack")
        finally:
            lock.release()

    threading.Thread(target=work, daemon=True).start()


@app.event("message")
def on_message(event, say):
    if not is_authorized(event, CFG.channel_id, CFG.allowed_user_id):
        return
    text = (event.get("text") or "").strip()
    if not text:
        return
    channel = event["channel"]
    thread_ts = event.get("thread_ts")
    cmd, arg = parse_command(text)

    # --- inside a session-bound thread: run / manage that session ---
    if thread_ts and str(thread_ts) in _thread_session:
        sid = _thread_session[str(thread_ts)]
        if cmd == "status":
            say(text=f"이 스레드 세션: `{sid[:8]}`  권한: {config.PERMISSION_MODE}(+deny)",
                thread_ts=thread_ts)
        elif cmd == "clear":
            with _state_lock:
                _thread_session.pop(str(thread_ts), None)
                _save_threads()
            say(text="이 스레드의 세션 연결을 해제했습니다.", thread_ts=thread_ts)
        elif cmd == "sessions":
            if arg:
                # terse real prompts like "list all" must still reach the session
                _run_and_reply(say, thread_ts, sid, text)
            else:
                say(text="목록은 채널 *본문*에서 `sessions` 를 입력하세요.", thread_ts=thread_ts)
        elif cmd == "select":
            say(text="이 스레드는 이미 세션에 연결돼 있습니다. 다른 세션은 본문에서 `sessions`로 여세요.",
                thread_ts=thread_ts)
        elif cmd == "last":
            info = sessions.find_session(CFG.projects_dir, sid)
            if info is None:
                say(text=f":x: 세션 `{sid[:8]}` 을(를) 찾지 못했습니다.", thread_ts=thread_ts)
            else:
                _show_last(info, say, thread_ts)
        elif cmd == "fork":
            if not arg:
                say(text="사용법: `fork <메시지>`", thread_ts=thread_ts)
            else:
                _run_and_reply(say, thread_ts, sid, arg, fork=True)
        elif cmd == "force":
            if not arg:
                say(text="사용법: `force <메시지>` — 열림/활성 감지를 무시하고 이 세션에 그대로 실행",
                    thread_ts=thread_ts)
            else:
                _run_and_reply(say, thread_ts, sid, arg, force=True)
        else:
            _run_and_reply(say, thread_ts, sid, text)
        return

    # --- a thread we don't track ---
    if thread_ts:
        say(text="이 스레드는 세션에 연결돼 있지 않습니다. 채널 *본문*에서 `sessions` → ▶ 로 세션 스레드를 여세요.",
            thread_ts=thread_ts)
        return

    # --- channel body (top level): commands only ---
    if cmd == "sessions":
        _cmd_sessions(channel, say, "" if arg == "all" else arg)
    elif cmd == "select":
        target = _resolve_target(channel, arg)
        if not target:
            say("사용법: `select <번호|id>` (먼저 `sessions`)")
        else:
            _open_thread(target, say)
    elif cmd == "status":
        say(f"열린 세션 스레드: {len(_thread_session)}개. `sessions` → ▶ 로 세션 스레드를 여세요.")
    elif cmd == "clear":
        say("스레드 *안에서* `clear` 를 쓰면 그 스레드의 세션 연결이 해제됩니다.")
    elif cmd == "last":
        say("`last`(최신 입출력 갱신)는 세션 *스레드 안에서* 사용하세요.")
    else:
        say("여기는 채널 본문입니다. `sessions` 로 목록을 보고 ▶(또는 `select N`)로 세션 *스레드*를 연 뒤, "
            "그 스레드 안에서 지시를 보내세요.")


@app.action("select_session")
def on_select_button(ack, body, say):
    ack()
    if body.get("user", {}).get("id") != CFG.allowed_user_id:
        return
    if body.get("channel", {}).get("id") != CFG.channel_id:
        return
    session_id = body["actions"][0]["value"]
    _open_thread(session_id, say)


def main() -> None:
    log.info(
        "Starting claude-slack-bridge (channel=%s, %d bound threads)",
        CFG.channel_id, len(_thread_session),
    )
    if config.PERMISSION_MODE == "bypassPermissions":
        log.warning(
            "permission mode: bypassPermissions — turns run without permission "
            "prompts (DENY_TOOLS backstop only). Override with "
            "CLAUDE_BRIDGE_PERMISSION_MODE if unintended."
        )
    SocketModeHandler(app, CFG.app_token).start()


if __name__ == "__main__":
    main()
