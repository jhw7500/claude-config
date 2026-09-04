#!/usr/bin/env python3
"""
Background Task Progress Hook (PreToolUse / PostToolUse / SubagentStop)

역할:
- PreToolUse:
  - run_in_background=true Agent/Bash 호출 시작 시 reminder 주입
  - 레거시 .task 파일 생성 (escape-hatch 유지)
  - OMC HUD state JSON (`<cwd>/.omc/state/sessions/<sid>/hud-state.json`)의
    backgroundTasks 배열에 running 항목 추가 → HUD 실시간 표시
- PostToolUse(Agent):
  - 완료 reminder 주입, 카운터 파일 삭제, HUD state에서 completed 마킹
- PostToolUse(Bash):
  - no-op. Bash run_in_background의 Post는 "시작" 신호라 완료가 아님.
  - 실제 완료는 SubagentStop로 잡히지 않으므로 30분 stale 정책에 맡김.
- SubagentStop:
  - tool_use_id 매칭으로 HUD state 항목 completed 마킹, .task 파일 삭제

모든 실패는 조용히 exit 0. Claude Code 흐름 차단 금지.
"""
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


DEBUG_LOG = os.path.expanduser("~/.claude/logs/hook-debug.log")


def debug_log(tag: str, info: "dict[str, object]") -> None:
    enabled = os.environ.get("CLAUDE_HOOK_DEBUG") == "1" or os.path.exists(
        os.path.expanduser("~/.claude/logs/hook-debug.on")
    )
    if not enabled:
        return
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG), exist_ok=True)
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tag": tag,
                **info,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


BG_DIR = Path.home() / ".claude" / "state" / "bg-tasks"


PRE_REMINDER = """[BG-TASK-START] 백그라운드 작업 시작 — {kind}: {what}

행동: (1) 지금 즉시 1문장으로 사용자에게 시작 알림
(2) 다른 작업 병행 시 분기마다 "BG 진행 중" 언급
(3) 결과가 필요하면 `Monitor`/`ScheduleWakeup`으로 주기적 확인 (맹목적 sleep 폴링 금지).

HUD 🔄 BG 카운트 증가.
세부 규칙: `~/.claude/CLAUDE.md` > "진행상황 보고"
"""


POST_REMINDER = """[BG-TASK-DONE] 백그라운드 작업 완료 — {kind}

행동: 이번 응답에서 1~2문장으로 완료 사실과 핵심 결과를 사용자에게 알림.
긴 결과는 요약만 + 상세 위치(파일/URL) 언급. 다음 단계가 명확하면 재확인 없이 진행.

HUD 🔄 BG 카운트 감소."""


def session_key(payload: "dict[str, object]") -> str:
    for k in ("session_id", "sessionId", "conversation_id", "conversationId"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    return f"pid-{os.getppid()}"


def get_cwd(payload: "dict[str, object]") -> str:
    for k in ("cwd", "working_directory", "workingDirectory"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    wk = payload.get("workspace")
    if isinstance(wk, dict):
        for k in ("current_dir", "project_dir"):
            v = wk.get(k)
            if isinstance(v, str) and v:
                return v
    return os.getcwd()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def is_background_call(payload: "dict[str, object]") -> tuple[bool, str, str]:
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return False, "", ""

    run_bg = tool_input.get("run_in_background")
    if not run_bg:
        return False, "", ""

    if tool_name == "Agent":
        kind = "Agent"
        desc = (
            tool_input.get("description")
            or tool_input.get("subagent_type")
            or "subagent"
        )
    elif tool_name == "Bash":
        kind = "Bash"
        desc = tool_input.get("description") or tool_input.get("command") or "shell command"
        if isinstance(desc, str) and len(desc) > 80:
            desc = desc[:77] + "..."
    else:
        return False, "", ""

    return True, kind, str(desc)


def counter_file(skey: str, tool_use_id: str | None) -> Path:
    d = BG_DIR / skey
    d.mkdir(parents=True, exist_ok=True)
    try:
        BG_DIR.chmod(0o700)
        d.chmod(0o700)
    except OSError:
        pass
    if tool_use_id:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_use_id)[:64]
        return d / f"{safe}.task"
    return d / f"{uuid.uuid4().hex}.task"


def find_matching_task(skey: str, tool_use_id: str | None) -> Path | None:
    d = BG_DIR / skey
    if not d.is_dir():
        return None
    if tool_use_id:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_use_id)[:64]
        candidate = d / f"{safe}.task"
        if candidate.is_file():
            return candidate
    for p in d.iterdir():
        if p.is_file() and p.suffix == ".task":
            return p
    return None


# ---------------------------------------------------------------------------
# OMC HUD state integration
# ---------------------------------------------------------------------------

def hud_state_path(cwd: str, session_id: str) -> Path:
    return Path(cwd) / ".omc" / "state" / "sessions" / session_id / "hud-state.json"


def read_hud_state(path: Path) -> dict:
    if not path.is_file():
        return {
            "timestamp": now_iso(),
            "backgroundTasks": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        data.setdefault("backgroundTasks", [])
        if not isinstance(data["backgroundTasks"], list):
            data["backgroundTasks"] = []
        return data
    except (json.JSONDecodeError, OSError, ValueError):
        return {"timestamp": now_iso(), "backgroundTasks": []}


def write_hud_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def prune_old_completed(tasks: list, max_keep: int = 20) -> list:
    running = [t for t in tasks if t.get("status") == "running"]
    non_running = [t for t in tasks if t.get("status") != "running"]
    non_running = non_running[-max(0, max_keep - len(running)):]
    return running + non_running


def hud_add_task(cwd: str, session_id: str, task_id: str, description: str, kind: str) -> None:
    try:
        path = hud_state_path(cwd, session_id)
        state = read_hud_state(path)
        tasks = state["backgroundTasks"]
        for t in tasks:
            if t.get("id") == task_id:
                return
        tasks.append({
            "id": task_id,
            "description": description[:200],
            "agentType": kind,
            "startedAt": now_iso(),
            "status": "running",
        })
        state["backgroundTasks"] = prune_old_completed(tasks)
        state["timestamp"] = now_iso()
        state["sessionId"] = session_id
        write_hud_state(path, state)
    except OSError:
        pass


def hud_complete_task(cwd: str, session_id: str, task_id: str | None, failed: bool = False) -> bool:
    """task_id 매칭 실패 시 가장 오래된 running task를 완료 처리. 매칭된 경우 True."""
    try:
        path = hud_state_path(cwd, session_id)
        if not path.is_file():
            return False
        state = read_hud_state(path)
        tasks = state["backgroundTasks"]
        target = None
        if task_id:
            for t in tasks:
                if t.get("id") == task_id and t.get("status") == "running":
                    target = t
                    break
        if target is None:
            for t in tasks:
                if t.get("status") == "running":
                    target = t
                    break
        if target is None:
            return False
        target["status"] = "failed" if failed else "completed"
        target["completedAt"] = now_iso()
        state["backgroundTasks"] = prune_old_completed(tasks)
        state["timestamp"] = now_iso()
        write_hud_state(path, state)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------


def emit_context(text: str, hook_event: str) -> None:
    """Deliver ``text`` to the model.

    PreToolUse/PostToolUse/SubagentStop discard raw stdout; the only channel
    that reaches the model is the ``hookSpecificOutput.additionalContext``
    envelope. Writing plain text here makes the hook silently inert.
    """
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": hook_event,
                "additionalContext": text,
            }
        },
        sys.stdout,
        ensure_ascii=False,
    )
    sys.stdout.flush()

def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0

    if not isinstance(payload, dict):
        return 0

    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    hook_event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
    debug_log("bg-task", {
        "payload_keys": sorted(list(payload.keys())),
        "tool_name": str(tool_name),
        "hook_event": str(hook_event),
    })

    skey = session_key(payload)
    cwd = get_cwd(payload)
    tool_use_id_raw = (
        payload.get("tool_use_id")
        or payload.get("toolUseId")
        or payload.get("tool_call_id")
        or payload.get("toolCallId")
    )
    tool_use_id = tool_use_id_raw if isinstance(tool_use_id_raw, str) else None

    # --- SubagentStop: Agent subagent 완료의 권위적 신호 ---
    if hook_event == "SubagentStop":
        matched = hud_complete_task(cwd, skey, tool_use_id, failed=False)
        if matched:
            existing = find_matching_task(skey, tool_use_id)
            if existing is not None:
                try:
                    existing.unlink(missing_ok=True)
                except OSError:
                    pass
            emit_context(POST_REMINDER.format(kind="Agent"), "SubagentStop")
        return 0

    # Pre/Post는 Agent/Bash에만 적용
    if str(tool_name) not in ("Agent", "Bash"):
        return 0

    if hook_event:
        is_post = hook_event == "PostToolUse"
    else:
        is_post = any(k in payload for k in ("tool_output", "toolOutput", "tool_response", "tool_result", "toolResult"))

    if is_post:
        # Bash BG Post는 "시작 보고"일 뿐. HUD/카운터에 손대지 않음.
        if str(tool_name) == "Bash":
            return 0

        # Agent Post는 SubagentStop가 권위적이지만, SubagentStop 훅이 미등록인
        # 환경을 대비해 fallback으로 완료 처리한다. SubagentStop가 이미
        # 처리했다면 hud_complete_task는 False 반환.
        matched = hud_complete_task(cwd, skey, tool_use_id, failed=False)
        existing = find_matching_task(skey, tool_use_id)
        if existing is not None:
            try:
                existing.unlink(missing_ok=True)
            except OSError:
                pass
            if not matched:
                # 레거시 .task 존재했는데 HUD state에는 없던 경우에도 알림
                matched = True
        if matched:
            emit_context(POST_REMINDER.format(kind="Agent"), "PostToolUse")
        return 0

    # Pre
    is_bg, kind, desc = is_background_call(payload)
    if not is_bg:
        return 0

    task_id = tool_use_id or uuid.uuid4().hex
    try:
        counter_file(skey, tool_use_id).touch()
    except OSError:
        pass
    hud_add_task(cwd, skey, task_id, desc, kind)
    emit_context(PRE_REMINDER.format(kind=kind, what=desc), "PreToolUse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
