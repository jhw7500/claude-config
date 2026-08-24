#!/usr/bin/env python3
"""Delegate Nudge Hook (UserPromptSubmit)

전역지침 "탐색 위임"(global-guidance.md)은 텍스트 규칙만으로는 작동하지 않았다 —
실측 위임률 0.5%(호출 기준), 상주 규칙 임계(3회)의 8배 지점에서도 위임하지 않는다.
이 훅은 직전 턴의 **실측 숫자**(탐색성 호출 수·tool_result 바이트)를 계산해,
임계 초과 시에만 시점 넛지를 주입한다. 검정 가설: "상주 규칙이 못 바꾼 행동을
실측 숫자를 담은 시점 넛지가 바꾼다" (설계 리뷰 v2.1, 2026-08-24).

- 입력(stdin): UserPromptSubmit hook JSON (session_id, transcript_path, prompt)
- 출력(stdout): 임계 초과 시 reminder 텍스트. 그 외 빈 출력.
- 상태: ~/.claude/state/delegate-nudge/<session_id> — transcript 오프셋·발화 수·
  현재 임계. flock 직렬화 (handoff-checkpoint-hook.py 와 동일 정책).
  콜드스타트(상태 없음/오프셋>파일크기)는 오프셋을 현재 파일 크기로 시딩하고
  그 턴은 발화하지 않는다 — 배선 직후 진행 중이던 세션의 오발화가 전환율 분모를
  오염시키는 것을 막는다 (설계 리뷰 v2 MAJOR 1).
- 델타는 메인 스레드만 집계: 서브에이전트 기록은 <세션>/subagents/ 별도 파일이고,
  구버전 인라인 기록은 isSidechain 으로 제외 (v1 기각 사유 C1 — 서브에이전트
  오염 구조적 차단).
- 델타에 Agent/Task 호출이 있으면 발화하지 않는다 — 이미 위임한 턴에 위임하라는
  메시지는 노이즈다 (설계 리뷰 v2 MINOR 3).
- 발화/억제 이벤트를 ~/.claude/state/delegate-nudge/log.jsonl 에 append 한다.
  발화는 transcript 에도 남지만(hook_success attachment) **억제는 transcript 가
  기록할 수 없으므로** 로그가 필요하다 (설계 리뷰 v2 MINOR 4).
- 판정(사전 등록, 설계 노트 v2.1): 귀무 전환율 7.8% 대비 성공 = n>=50 발화에서
  다음 턴 전환율 >=20% (단측 이항 p<0.01), 롤백 = n>=50에서 95% CI 상한 <15%.
- 임계 조정: env DELEGATE_NUDGE_CALLS(기본 10) / DELEGATE_NUDGE_KB(기본 100).
  킬 스위치: CLAUDE_SKIP_DELEGATE_NUDGE=1 — 주입만 끄고 오프셋은 계속 전진한다
  (escape prefix 동일). 멈춘 동안의 델타가 재개 시 낡은 넛지로 발화하는 것을 막는다.
"""
import fcntl
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

ESCAPE_PREFIXES = ("#noreminder", "#nr", "#raw", "#silent", "#조용히")

MAX_FIRES_PER_SESSION = 3
DELTA_CAP_BYTES = 2 * 1024 * 1024  # 30일 실측 최대 델타 1.99MB — 초과분은 파싱 스킵

# 탐색성 도구 판정 — scripts/delegation-ratio.py 의 is_direct() 와 동기화 유지
DIRECT_EXPLORE = {"Bash", "Grep", "Glob", "Read"}
DIRECT_INFO = {"WebSearch", "WebFetch", "ToolSearch"}
# mcp__<server>__<op> 의 마지막 연산명 세그먼트만 본다 — 서버 네임스페이스에
# 동사가 들어간 action 도구(mcp__mcp_search__build_corpus 등)의 오분류 방지
# (Codex 리뷰 P2).
MCP_INFO_VERB_RE = re.compile(r"(search|fetch|retrieve|recall|query)", re.IGNORECASE)
DELEGATE_TOOLS = {"Agent", "Task"}


def is_mcp_info(name: str) -> bool:
    if not name.startswith("mcp__"):
        return False
    return bool(MCP_INFO_VERB_RE.search(name.rsplit("__", 1)[-1]))

# server_tool_use 는 서버측 도구(WebSearch 등) 블록 — stop-text-required.py:141 과
# 동일하게 tool_use 와 등가 취급한다 (Codex P2, PR #44 라운드 2)
_TOOL_MARKERS = ('"tool_use"', '"server_tool_use"', '"tool_result"')
_TOOL_USE_TYPES = ("tool_use", "server_tool_use")

REMINDER = """<system-reminder>
[DELEGATE-NUDGE] 지난 턴에서 탐색성 호출 {calls}회 · tool_result {kb}KB가 메인 컨텍스트에 들어왔다.

이번 턴에 유사한 탐색(조사·로그 분석·코드 검색)이 이어진다면 서브에이전트에 위임하고
결론 + 근거 위치(file:line, 핵심 출력 1~2줄)만 회수하라 — 세부 규칙:
~/.claude/global-guidance.md > "탐색 위임 (컨텍스트 보존, 전역)".
단, 중간 판단이 계속 필요한 순차 의존 강한 인터랙티브 디버깅이면 이 리마인더는 무시하라.
</system-reminder>"""


def env_int(key: str, default: int) -> int:
    try:
        v = int(os.environ.get(key, ""))
        return v if v > 0 else default
    except ValueError:
        return default


def count_delta(path: str, offset: int, limit: int):
    """transcript[offset:] 의 메인 스레드 탐색성 호출·바이트·위임 호출을 집계.

    바이너리 모드로 정확히 limit 바이트만 읽는다 — 텍스트 모드 read(n)은 문자 수
    기준이라 멀티바이트 델타에서 창 경계를 넘어 읽고(동시 append 시 다음 턴과
    이중 집계), 바이트 오프셋 seek 도 텍스트 모드에선 미정의 동작이다
    (Gemini 리뷰 HIGH, PR #44). BytesIO 순회로 줄 리스트 중복 점유도 피한다.
    """
    calls = agent_calls = result_bytes = 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(limit)
    for raw in io.BytesIO(data):
        line = raw.decode("utf-8", errors="replace")
        if not any(m in line for m in _TOOL_MARKERS):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # 델타 경계의 잘린 줄 포함
        if not isinstance(rec, dict):  # ["tool_use"] 류 비객체 JSON — 스킵 (Codex P2 R3)
            continue
        if rec.get("isSidechain") is True:
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):  # 문자열 등 비객체 message — 스킵 (Codex P2)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        rtype = rec.get("type")
        for item in content:
            if not isinstance(item, dict):
                continue
            if rtype == "assistant" and item.get("type") in _TOOL_USE_TYPES:
                name = item.get("name") or ""
                if name in DELEGATE_TOOLS:
                    agent_calls += 1
                elif (name in DIRECT_EXPLORE or name in DIRECT_INFO
                      or is_mcp_info(name)):
                    calls += 1
            elif rtype == "user" and item.get("type") == "tool_result":
                try:
                    # UTF-8 인코딩 후 길이 — 코드포인트 수로 재면 한글 등
                    # 비ASCII 출력이 1/3로 과소집계된다 (Codex 리뷰 P2)
                    result_bytes += len(json.dumps(
                        item.get("content", ""), ensure_ascii=False
                    ).encode("utf-8"))
                except (TypeError, ValueError):
                    continue
    return calls, agent_calls, result_bytes


def append_log(state_dir: str, event: dict) -> None:
    """발화/억제 이벤트 append-only 기록. 실패해도 본 동작을 막지 않는다."""
    try:
        fd = os.open(os.path.join(state_dir, "log.jsonl"),
                     os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as e:
        sys.stderr.write("[delegate-nudge-hook] log append failed: %s\n" % e)


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):  # list/str 등 비정상 top-level 도 조용히 무시
        return 0

    # 킬 스위치/escape prefix 는 주입만 끄고 오프셋 전진은 계속한다 — 여기서
    # return 하면 스킵된 턴의 델타가 다음 프롬프트에 합산돼 낡은 넛지가 뒤늦게
    # 발화하거나 stale Agent 호출이 억제를 오염시킨다 (Codex 리뷰 P2).
    skip = os.environ.get("CLAUDE_SKIP_DELEGATE_NUDGE") == "1"
    prompt = payload.get("prompt") or ""
    if not skip and isinstance(prompt, str) and any(
        prompt.strip().lower().startswith(p) for p in ESCAPE_PREFIXES
    ):
        skip = True

    # session_id 는 상태 파일명에 그대로 쓰인다 — handoff-checkpoint-hook.py 와
    # 동일 정책으로 raw id 를 검증해 비정상 id 는 거부한다 (PR #25 리뷰).
    session_id = str(payload.get("session_id") or "")
    transcript = payload.get("transcript_path") or ""
    if not re.fullmatch(r"(?=.*[A-Za-z0-9])[A-Za-z0-9._-]{1,128}", session_id):
        return 0
    if not transcript or not os.path.isfile(transcript):
        return 0

    try:
        size = os.path.getsize(transcript)
    except OSError:
        return 0

    calls_threshold = env_int("DELEGATE_NUDGE_CALLS", 10)
    kb_threshold = env_int("DELEGATE_NUDGE_KB", 100)

    state_dir = os.path.expanduser("~/.claude/state/delegate-nudge")
    state_file = os.path.join(state_dir, session_id)

    try:
        os.makedirs(state_dir, exist_ok=True)
        fd = os.open(state_file, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        sys.stderr.write("[delegate-nudge-hook] state open failed: %s\n" % e)
        return 0

    fire = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # 다른 인스턴스가 처리 중 — 중복 방지

        data = os.read(fd, 512).decode("utf-8", errors="replace").strip()
        try:
            state = json.loads(data) if data else None
        except json.JSONDecodeError:
            state = None

        cold_start = (
            not isinstance(state, dict)
            or not isinstance(state.get("offset"), int)
            or state["offset"] < 0
            or state["offset"] > size  # 파일 축소/교체 — 재시딩
            # fires 가 비정수면 아래 << 연산이 TypeError 로 exit 1 — 훅 규약
            # (모든 경로 exit 0) 위반이므로 재시딩한다 (PR #44 Claude 리뷰 [MEDIUM])
            or not isinstance(state.get("fires", 0), int)
            or isinstance(state.get("fires", 0), bool)
            or state.get("fires", 0) < 0
        )
        if cold_start:
            # 오프셋을 현재 크기로 시딩하고 이 턴은 판정하지 않는다 (오발화 방지)
            state = {"offset": size, "fires": 0}
        else:
            state.setdefault("fires", 0)
            # 임계는 매 실행 env 에서 계산 (에스컬레이션 = 발화마다 2배).
            # 상태에 임계를 저장하면 env 변경이 기존 세션에 반영되지 않는다.
            eff_calls = calls_threshold << state["fires"]
            eff_bytes = (kb_threshold * 1024) << state["fires"]
            offset, delta_len = state["offset"], size - state["offset"]
            calls = agent_calls = rbytes = 0
            parsed = (not skip) and delta_len <= DELTA_CAP_BYTES
            if parsed and delta_len > 0:
                try:
                    calls, agent_calls, rbytes = count_delta(transcript, offset, delta_len)
                except OSError:
                    parsed = False
            state["offset"] = size

            if parsed and (calls >= eff_calls or rbytes >= eff_bytes):
                event = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "session": session_id, "calls": calls,
                    "agent_calls": agent_calls, "bytes": rbytes,
                }
                if agent_calls > 0:
                    event["action"] = "suppressed_agent"  # 이미 위임한 턴
                elif state["fires"] >= MAX_FIRES_PER_SESSION:
                    event["action"] = "suppressed_cap"
                else:
                    event["action"] = "fired"
                    state["fires"] += 1  # 다음 임계는 자동으로 2배 (위 시프트)
                    fire = True
                append_log(state_dir, event)

        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(state).encode("utf-8"))
    except OSError as e:
        sys.stderr.write("[delegate-nudge-hook] state write failed: %s\n" % e)
        return 0
    finally:
        os.close(fd)

    if fire:
        sys.stdout.write(REMINDER.format(calls=calls, kb=rbytes // 1024))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
