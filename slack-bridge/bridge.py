"""Slack <-> Claude Code headless bridge (Socket Mode), thread-per-session.

Each Claude session is bound to a Slack thread: pick a session from the
`sessions` list (▶ button, `select N`, or the App Home tab) and the bot opens
a thread for it. Replies inside that thread run that session; the bot answers
in-thread. The thread<->session map is persisted so threads survive a service
restart.

Channel hygiene: one sessions-list message per channel (old one is deleted on
refresh so the list is always the latest message), one thread per session
(re-selecting an already-bound session answers with an ephemeral permalink
instead of a new root), and the "작업 중…" ack is deleted once the result is
posted. The App Home tab shows the same list outside the channel entirely.
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
# serializes thread check-and-create so a double-click can't bind twice
_bind_lock = threading.Lock()
# serializes the list delete->post->store cycle
_ui_lock = threading.Lock()
# last shown list per channel: index -> session_id (for `select N`)
_last_list: dict[str, dict[int, str]] = {}

# channel -> ts of the single sessions-list message (delete+repost keeps one).
# Persisted so a restart can still delete the previous list message.
_UI_STATE_FILE = os.path.expanduser(
    os.environ.get("CLAUDE_BRIDGE_UI_STATE_FILE", "~/.config/claude-slack-bridge-ui.json")
)


def _load_list_msg() -> dict[str, str]:
    try:
        with open(_UI_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        lm = d.get("list_msg") if isinstance(d, dict) else None
        return {str(k): str(v) for k, v in lm.items()} if isinstance(lm, dict) else {}
    except (OSError, ValueError):
        return {}


_list_msg: dict[str, str] = _load_list_msg()


def _save_list_msg() -> None:
    try:
        d = os.path.dirname(_UI_STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = _UI_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"list_msg": _list_msg}, f)
        os.replace(tmp, _UI_STATE_FILE)
    except OSError:
        log.exception("failed to persist ui state")

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


def _post(channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None):
    kwargs = {"channel": channel, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    if blocks:
        kwargs["blocks"] = blocks
    return app.client.chat_postMessage(**kwargs)


def _notice(channel: str, user: str | None, text: str) -> None:
    """Prefer an ephemeral note (leaves no trace in channel history)."""
    if user:
        try:
            app.client.chat_postEphemeral(channel=channel, user=user, text=text)
            return
        except Exception:  # noqa: BLE001 — fall back to a normal post
            pass
    _post(channel, text)


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


def _show_last(info, channel: str, thread_ts: str | None = None) -> None:
    """Re-fetch and post a session's current last input/output."""
    head = f"🔄 `{info.repo or info.folder}` (`{info.session_id[:8]}`) · ⏱ {_ago(info.mtime)} 전"
    _post(channel, "\n".join([head, *_last_exchange(info)]), thread_ts)


def _thread_for(session_id: str) -> str | None:
    """Most recent thread already bound to this session (reverse lookup)."""
    with _state_lock:
        bound = [ts for ts, sid in _thread_session.items() if sid == session_id]
    if not bound:
        return None
    return max(bound, key=float)


def _session_text(i: int, s) -> str:
    repo = f"`{s.repo}`" if s.repo else "—"
    branch = f"  ⎇ {s.branch}" if s.branch else ""
    badge = _BADGE.get(s.live, "")
    header = f"*{i}.* {repo}{branch}  {badge}  _(`{s.session_id[:8]}`)_"
    meta = f"📁 `{s.cwd}`"
    title = f"📝 {s.title}" if s.title and s.title != "(no title)" else ""
    summary = f"💬 {s.turns} · ⏱ {_ago(s.mtime)} 전"
    return "\n".join(p for p in (header, meta, title, summary) if p)


def _render_sessions(items, mode: str = "") -> tuple[str, list]:
    """Build (fallback_text, blocks) for a sessions list message."""
    blocks, lines = [], []
    for i, s in enumerate(items, 1):
        text = _session_text(i, s)
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
    return header + "\n" + "\n".join(lines), blocks


def _list_items(mode: str = ""):
    if mode == "live":
        # full-scan candidate matching: an old-mtime open session is not cut
        return sessions.list_live_sessions(CFG.projects_dir)
    return sessions.list_sessions(CFG.projects_dir)


def _cmd_sessions(channel: str, mode: str = "") -> None:
    items = _list_items(mode)
    if items:
        _last_list[channel] = {i: s.session_id for i, s in enumerate(items, 1)}
        text, blocks = _render_sessions(items, mode)
    else:
        # the empty-state message replaces the list (and is tracked as it),
        # so a stale interactive list never lingers next to "세션이 없습니다"
        _last_list.pop(channel, None)
        text = ("열려 있는 세션이 없습니다. `sessions all`로 전체를 보세요." if mode == "live"
                else "세션이 없습니다.")
        blocks = None
    # keep exactly one list message, always at the bottom: delete old, repost
    with _ui_lock:
        old = _list_msg.get(channel)
        if old:
            try:
                app.client.chat_delete(channel=channel, ts=old)
            except Exception:  # noqa: BLE001 — already deleted / too old
                pass
        resp = _post(channel, text, blocks=blocks)
        try:
            _list_msg[channel] = str(resp["ts"])
            _save_list_msg()
        except (KeyError, TypeError):
            pass


_permalink_cache: dict[str, str] = {}  # message ts -> permalink (never changes)


def _permalink(channel: str, ts: str) -> str | None:
    cached = _permalink_cache.get(ts)
    if cached:
        return cached
    try:
        link = app.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except Exception:  # noqa: BLE001
        return None
    if len(_permalink_cache) > 256:  # bound the cache; entries re-fetch cheaply
        _permalink_cache.clear()
    _permalink_cache[ts] = link
    return link


def _thread_link(session_id: str) -> str | None:
    """Permalink of the thread bound to this session, if any."""
    ts = _thread_for(session_id)
    if not ts:
        return None
    return _permalink(CFG.channel_id, ts)


def _ensure_thread(session_id: str, channel: str) -> tuple[str | None, bool, str | None]:
    """Bind a session to a thread, reusing an existing binding.

    Returns (thread_ts, created, error_text). The whole check-and-create is
    serialized so concurrent selects (double-click, Home+channel) can't bind
    the same session to two threads.
    """
    info = sessions.find_session(CFG.projects_dir, session_id)
    if info is None:
        return None, False, f":x: 세션 `{session_id[:8]}` 을(를) 찾지 못했습니다."
    with _bind_lock:
        existing = _thread_for(info.session_id)
        if existing:
            return existing, False, None
        badge = _BADGE.get(info.live, "")
        try:
            resp = _post(
                channel,
                f"🧵 *{info.repo or info.folder}* (`{info.session_id[:8]}`) {badge} — "
                "이 스레드에 *답글*로 이어가세요 · `fork <메시지>` `force <메시지>` `last` `status` `clear`",
            )
        except Exception:  # noqa: BLE001 — Slack API/network error
            log.exception("failed to post thread root")
            return None, False, ":x: 스레드 생성 실패(메시지 전송 오류)."
        try:
            root_ts = resp["ts"]
        except (KeyError, TypeError):
            root_ts = None
        if not root_ts:
            return None, False, ":x: 스레드 생성 실패(메시지 ts를 받지 못함)."
        with _state_lock:
            _thread_session[str(root_ts)] = info.session_id
            _save_threads()
        return str(root_ts), True, None


def _open_thread(session_id: str, channel: str, user: str | None = None) -> None:
    """Channel-origin select: existing binding answers as an ephemeral note."""
    ts, created, err = _ensure_thread(session_id, channel)
    if err:
        _notice(channel, user, err)
        return
    if not created:
        link = _permalink(channel, ts) if ts else None
        tail = f" → {link}" if link else " — 기존 스레드에 답글로 이어가세요."
        _notice(channel, user, f":thread: `{session_id[:8]}` 이미 열린 스레드가 있습니다{tail}")


def _open_thread_from_home(session_id: str, user: str) -> None:
    """Home-origin select: feedback goes back into the Home view with a link."""
    ts, created, err = _ensure_thread(session_id, CFG.channel_id)
    if err:
        _publish_home(user, notice=err)
        return
    link = _permalink(CFG.channel_id, ts) if ts else None
    label = "새 스레드가 열렸습니다" if created else "이미 열린 스레드가 있습니다"
    tail = f" → <{link}|채널에서 열기>" if link else " (브리지 채널에서 이어가세요)"
    _publish_home(user, notice=f":thread: `{session_id[:8]}` {label}{tail}")


def _run_and_reply(channel: str, thread_ts: str, session_id: str, prompt: str, *,
                   fork: bool = False, force: bool = False) -> None:
    info = sessions.find_session(CFG.projects_dir, session_id)
    if info is None:
        _post(channel, f":x: 세션 `{session_id[:8]}` 이(가) 사라졌습니다. 본문에서 `sessions`.",
              thread_ts)
        return
    bridge_ran_recently = (
        time.time() - _last_bridge_run.get(info.session_id, 0.0)
    ) < config.ACTIVE_THRESHOLD_SECONDS
    if not fork and not force and info.live == "open":
        _post(channel,
              ":no_entry: 이 세션은 지금 호스트에서 열려 있습니다(프로세스 감지). "
              "그대로 이어 쓰면 대화가 분기되어 서로의 작업이 보이지 않게 됩니다. "
              "`fork <메시지>`=분기, 분기 위험을 감수하려면 `force <메시지>`.",
              thread_ts)
        return
    if not fork and not force and not bridge_ran_recently and runner.is_active(info.mtime):
        _post(channel,
              ":warning: 이 세션이 방금 활성 상태였습니다(다른 곳에서 열려 있을 수 있음). "
              "`fork <메시지>`로 분기하거나 잠시 후 다시 시도하세요. 무시하려면 `force <메시지>`.",
              thread_ts)
        return
    lock = _lock_for(info.session_id)
    if not lock.acquire(blocking=False):
        _post(channel, ":hourglass_flowing_sand: 이 세션은 이미 작업 중입니다. 잠시 후 다시 시도하세요.",
              thread_ts)
        return
    ack = _post(channel, ":hourglass_flowing_sand: 작업 중…", thread_ts)
    try:
        ack_ts = str(ack["ts"])
    except (KeyError, TypeError):
        ack_ts = None

    def work() -> None:
        posted = False
        try:
            res = runner.run_turn(info.session_id, info.cwd, prompt, fork=fork)
            _last_bridge_run[info.session_id] = time.time()
            if fork and res.session_id and res.session_id != info.session_id:
                _last_bridge_run[res.session_id] = time.time()
                with _state_lock:
                    _thread_session[str(thread_ts)] = res.session_id
                    _save_threads()
                _post(channel,
                      f":twisted_rightwards_arrows: 분기됨 → 이 스레드는 이제 새 세션 `{res.session_id[:8]}`",
                      thread_ts)
            head = "" if res.ok else ":x: (error)\n"
            tail = f"\n:no_entry: 차단된 도구: {', '.join(res.denials)}" if res.denials else ""
            body = f"{head}{res.text}\n\n_💰 ${res.cost_usd:.4f}_{tail}"
            if len(body) > 3500:
                body = body[:3500] + "\n…(잘림)"
            if info.live == "maybe":
                body += ("\n:warning: 같은 폴더에 열린 호스트 TUI가 있습니다. "
                         "이 세션이 호스트에 열려 있는 세션이라면 다음부터 `fork <메시지>`를 권장합니다.")
            _post(channel, body, thread_ts)
            posted = True
        except Exception as e:  # noqa: BLE001
            log.exception("turn failed")
            try:
                _post(channel, f":x: 실행 실패: {e}", thread_ts)
                posted = True
            except Exception:  # noqa: BLE001
                log.exception("failed to post error to Slack")
        finally:
            # the ack is redundant once any outcome message is up; keep it if
            # nothing could be posted so the thread isn't left silent
            if posted and ack_ts:
                try:
                    app.client.chat_delete(channel=channel, ts=ack_ts)
                except Exception:  # noqa: BLE001
                    pass
            lock.release()

    threading.Thread(target=work, daemon=True).start()


@app.event("message")
def on_message(event):
    if not is_authorized(event, CFG.channel_id, CFG.allowed_user_id):
        return
    text = (event.get("text") or "").strip()
    if not text:
        return
    channel = event["channel"]
    user = event.get("user")
    thread_ts = event.get("thread_ts")
    cmd, arg = parse_command(text)

    # --- inside a session-bound thread: run / manage that session ---
    # atomic .get(): `clear` in another handler thread may drop the key between
    # a membership check and the lookup
    sid = _thread_session.get(str(thread_ts)) if thread_ts else None
    if sid:
        if cmd == "status":
            _post(channel, f"이 스레드 세션: `{sid[:8]}`  권한: {config.PERMISSION_MODE}(+deny)",
                  thread_ts)
        elif cmd == "clear":
            with _state_lock:
                _thread_session.pop(str(thread_ts), None)
                _save_threads()
            _post(channel, "이 스레드의 세션 연결을 해제했습니다.", thread_ts)
        elif cmd == "sessions":
            if arg:
                # terse real prompts like "list all" must still reach the session
                _run_and_reply(channel, thread_ts, sid, text)
            else:
                _post(channel, "목록은 채널 *본문*에서 `sessions` 를 입력하세요.", thread_ts)
        elif cmd == "select":
            _post(channel, "이 스레드는 이미 세션에 연결돼 있습니다. 다른 세션은 본문에서 `sessions`로 여세요.",
                  thread_ts)
        elif cmd == "last":
            info = sessions.find_session(CFG.projects_dir, sid)
            if info is None:
                _post(channel, f":x: 세션 `{sid[:8]}` 을(를) 찾지 못했습니다.", thread_ts)
            else:
                _show_last(info, channel, thread_ts)
        elif cmd == "fork":
            if not arg:
                _post(channel, "사용법: `fork <메시지>`", thread_ts)
            else:
                _run_and_reply(channel, thread_ts, sid, arg, fork=True)
        elif cmd == "force":
            if not arg:
                _post(channel, "사용법: `force <메시지>` — 열림/활성 감지를 무시하고 이 세션에 그대로 실행",
                      thread_ts)
            else:
                _run_and_reply(channel, thread_ts, sid, arg, force=True)
        else:
            _run_and_reply(channel, thread_ts, sid, text)
        return

    # --- a thread we don't track ---
    if thread_ts:
        _post(channel, "이 스레드는 세션에 연결돼 있지 않습니다. 채널 *본문*에서 `sessions` → ▶ 로 세션 스레드를 여세요.",
              thread_ts)
        return

    # --- channel body (top level): commands only ---
    if cmd == "sessions":
        _cmd_sessions(channel, "" if arg == "all" else arg)
    elif cmd == "select":
        target = _resolve_target(channel, arg)
        if not target:
            _post(channel, "사용법: `select <번호|id>` (먼저 `sessions`)")
        else:
            _open_thread(target, channel, user)
    elif cmd == "status":
        _post(channel, f"열린 세션 스레드: {len(_thread_session)}개. `sessions` → ▶ 로 세션 스레드를 여세요.")
    elif cmd == "clear":
        _post(channel, "스레드 *안에서* `clear` 를 쓰면 그 스레드의 세션 연결이 해제됩니다.")
    elif cmd == "last":
        _post(channel, "`last`(최신 입출력 갱신)는 세션 *스레드 안에서* 사용하세요.")
    else:
        _post(channel, "여기는 채널 본문입니다. `sessions` 로 목록을 보고 ▶(또는 `select N`)로 세션 *스레드*를 연 뒤, "
              "그 스레드 안에서 지시를 보내세요.")


@app.action("select_session")
def on_select_button(ack, body):
    ack()
    user = body.get("user", {}).get("id")
    if user != CFG.allowed_user_id:
        return
    session_id = body["actions"][0]["value"]
    channel = (body.get("channel") or {}).get("id")
    if not channel:
        # Home-tab actions carry no channel -> feedback into the Home view
        _open_thread_from_home(session_id, user)
        return
    if channel != CFG.channel_id:
        return
    _open_thread(session_id, channel, user)


# --- App Home tab: the sessions list outside the channel -------------------

def _home_view(items, notice: str | None = None) -> dict:
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Claude Code 세션"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"🕘 갱신 {time.strftime('%H:%M:%S', time.localtime())} · {len(items)}개 (최신순) · "
            "▶를 누르면 채널에 세션 스레드가 열립니다"
        )}]},
        {"type": "actions", "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "🔄 새로고침"},
            "action_id": "home_refresh",
        }]},
        {"type": "divider"},
    ]
    if notice:
        blocks.insert(1, {"type": "section", "text": {"type": "mrkdwn", "text": notice}})
    for i, s in enumerate(items, 1):
        text = _session_text(i, s)
        link = _thread_link(s.session_id)
        if link:
            text += f"\n🧵 <{link}|열린 스레드로 가기>"
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
    if not items:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "세션이 없습니다."}})
    return {"type": "home", "blocks": blocks}


def _publish_home(user_id: str, notice: str | None = None) -> None:
    try:
        app.client.views_publish(user_id=user_id, view=_home_view(_list_items(), notice))
    except Exception:  # noqa: BLE001
        log.exception("failed to publish App Home view")


@app.event("app_home_opened")
def on_home_opened(event):
    if event.get("user") != CFG.allowed_user_id:
        return
    if event.get("tab", "home") != "home":
        return
    _publish_home(event["user"])


@app.action("home_refresh")
def on_home_refresh(ack, body):
    ack()
    user_id = body.get("user", {}).get("id")
    if user_id != CFG.allowed_user_id:
        return
    _publish_home(user_id)


def main() -> None:
    log.info(
        "Starting claude-slack-bridge (%d bound threads)",
        len(_thread_session),
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
