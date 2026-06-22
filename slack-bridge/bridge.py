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
