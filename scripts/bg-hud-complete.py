#!/usr/bin/env python3
"""
BG 완료 반영 헬퍼 (수동 호출용)

사용법:
    python3 /home/jhw/.claude/scripts/bg-hud-complete.py <tool_use_id> [status]

- tool_use_id: `<task-notification>`의 <tool-use-id> 값
- status: completed (기본) 또는 failed
- 현재 작업 트리의 `.omc/state/sessions/*/hud-state.json`을 자동 탐색해
  해당 tool_use_id의 task를 완료 마킹한다.

종료 코드:
    0 - 반영 성공
    1 - 인자 부족
    2 - 대상 state 또는 task 없음
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def find_omc_root(start: Path) -> Path | None:
    cur = start.resolve()
    for _ in range(20):
        if (cur / ".omc" / "state" / "sessions").is_dir():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def find_state_for_task(omc_root: Path, tool_use_id: str) -> Path | None:
    sessions_dir = omc_root / ".omc" / "state" / "sessions"
    candidates = sorted(
        sessions_dir.glob("*/hud-state.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        tasks = data.get("backgroundTasks") if isinstance(data, dict) else None
        if not isinstance(tasks, list):
            continue
        for t in tasks:
            if isinstance(t, dict) and t.get("id") == tool_use_id:
                return path
    return None


def mark(path: Path, tool_use_id: str, status: str) -> bool:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: state read failed: {e}", file=sys.stderr)
        return False
    tasks = state.get("backgroundTasks", [])
    ts = now_iso()
    updated = False
    for t in tasks:
        if isinstance(t, dict) and t.get("id") == tool_use_id:
            prev = t.get("status")
            t["status"] = status
            t["completedAt"] = ts
            updated = True
            print(f"ok {tool_use_id[-20:]} {prev} -> {status} @ {ts}")
            break
    if not updated:
        return False
    state["timestamp"] = ts
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"ERROR: state write failed: {e}", file=sys.stderr)
        return False
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 1
    tool_use_id = sys.argv[1].strip()
    status = (sys.argv[2].strip().lower() if len(sys.argv) >= 3 else "completed")
    if status not in ("completed", "failed"):
        print(f"ERROR: invalid status '{status}' (completed|failed)", file=sys.stderr)
        return 1

    omc_root = find_omc_root(Path.cwd())
    if omc_root is None:
        print("ERROR: .omc/state/sessions not found from cwd", file=sys.stderr)
        return 2

    state_path = find_state_for_task(omc_root, tool_use_id)
    if state_path is None:
        print(f"ERROR: tool_use_id {tool_use_id} not found under {omc_root}", file=sys.stderr)
        return 2

    ok = mark(state_path, tool_use_id, status)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
