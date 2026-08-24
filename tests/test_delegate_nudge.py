"""hooks/delegate-nudge-hook.py 테스트.

설계 리뷰 v2/v2.1 (2026-08-24) 승인 조건의 회귀 테스트:
- 콜드스타트 무발화 (v2 MAJOR 1 — 배선 직후 오발화가 전환율 분모를 오염)
- 서브에이전트/isSidechain 제외 (v1 기각 사유 C1)
- 델타 내 Agent 호출 시 발화 억제 + 억제 로그 (v2 MINOR 3/4)
- 세션당 3회 상한 + 임계 2배 에스컬레이션 (v1 기각 사유 C2)
- 델타 2MB 상한 (v2 MINOR 2)

훅 규약: 모든 경로 exit 0, 조건 미충족 시 무출력.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "hooks" / "delegate-nudge-hook.py"
SESSION = "sess1"


def tool_use(name, sidechain=False):
    return json.dumps({
        "type": "assistant", "isSidechain": sidechain,
        "message": {"content": [{"type": "tool_use", "id": "t", "name": name}]},
    })


def tool_result(content, sidechain=False):
    return json.dumps({
        "type": "user", "isSidechain": sidechain,
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t",
                                 "content": content}]},
    })


def run_hook(payload, home, env_extra=None):
    env = dict(os.environ)
    env.pop("CLAUDE_SKIP_DELEGATE_NUDGE", None)
    env["HOME"] = str(home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload, ensure_ascii=False) if payload is not None else "",
        text=True, capture_output=True, check=False, env=env,
    )


@pytest.fixture
def env(tmp_path):
    """(home, transcript_path, payload) — transcript 는 빈 파일로 시작."""
    transcript = tmp_path / f"{SESSION}.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = {"session_id": SESSION, "transcript_path": str(transcript),
               "prompt": "다음 작업 진행"}
    return tmp_path, transcript, payload


def append(transcript, lines):
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def state_of(home):
    return json.loads((home / ".claude/state/delegate-nudge" / SESSION).read_text())


def log_of(home):
    p = home / ".claude/state/delegate-nudge/log.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def cold_start(home, transcript, payload):
    r = run_hook(payload, home)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    return r


def test_cold_start_seeds_offset_without_firing(env):
    """회귀: v2 MAJOR 1 — 상태 없음 → 현재 크기로 시딩, 그 턴 무발화."""
    home, transcript, payload = env
    append(transcript, [tool_use("Bash")] * 50)  # 시딩 전 이미 무거운 세션
    r = run_hook(payload, home)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    st = state_of(home)
    assert st["offset"] == transcript.stat().st_size
    assert st["fires"] == 0
    assert log_of(home) == []


def test_fires_on_calls_threshold(env):
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    append(transcript, [tool_use("Bash")] * 10)
    r = run_hook(payload, home)
    assert r.returncode == 0, r.stderr
    assert "[DELEGATE-NUDGE]" in r.stdout
    assert "10회" in r.stdout
    log = log_of(home)
    assert len(log) == 1 and log[0]["action"] == "fired" and log[0]["calls"] == 10


def test_fires_on_bytes_threshold(env):
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    append(transcript, [tool_result("X" * 110 * 1024)])  # 100KB 임계 초과
    r = run_hook(payload, home)
    assert "[DELEGATE-NUDGE]" in r.stdout
    assert log_of(home)[0]["action"] == "fired"


def test_no_fire_below_thresholds(env):
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    append(transcript, [tool_use("Bash")] * 3 + [tool_result("small")])
    r = run_hook(payload, home)
    assert r.stdout == ""
    assert log_of(home) == []
    # 오프셋은 전진해야 다음 턴 델타가 이 턴을 다시 세지 않는다
    assert state_of(home)["offset"] == transcript.stat().st_size


def test_suppressed_when_delta_already_delegates(env):
    """회귀: v2 MINOR 3 — 이미 Agent 를 호출한 턴에는 발화하지 않되 로그는 남긴다."""
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    append(transcript, [tool_use("Bash")] * 10 + [tool_use("Agent")])
    r = run_hook(payload, home)
    assert r.stdout == ""
    log = log_of(home)
    assert len(log) == 1 and log[0]["action"] == "suppressed_agent"
    assert state_of(home)["fires"] == 0


def test_sidechain_records_excluded(env):
    """회귀: v1 기각 사유 C1 — 서브에이전트(isSidechain) 호출은 집계에서 제외."""
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    append(transcript, [tool_use("Bash", sidechain=True)] * 30)
    r = run_hook(payload, home)
    assert r.stdout == ""
    assert log_of(home) == []


def test_threshold_escalates_after_fire(env):
    """회귀: v1 기각 사유 C2 — 발화 후 임계 2배."""
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    append(transcript, [tool_use("Bash")] * 10)
    assert "[DELEGATE-NUDGE]" in run_hook(payload, home).stdout
    assert state_of(home)["fires"] == 1  # 임계는 base << fires 로 계산 = 20

    append(transcript, [tool_use("Bash")] * 10)  # 새 임계 20 미만
    assert run_hook(payload, home).stdout == ""

    append(transcript, [tool_use("Bash")] * 20)  # 새 임계 도달
    assert "[DELEGATE-NUDGE]" in run_hook(payload, home).stdout


def test_session_cap_three_fires(env):
    """회귀: v1 기각 사유 C2 — 세션당 최대 3회, 이후는 suppressed_cap 로그만."""
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    for expected in (10, 20, 40):  # 에스컬레이션된 임계를 매번 충족
        append(transcript, [tool_use("Bash")] * expected)
        assert "[DELEGATE-NUDGE]" in run_hook(payload, home).stdout
    append(transcript, [tool_use("Bash")] * 80)
    r = run_hook(payload, home)
    assert r.stdout == ""
    assert log_of(home)[-1]["action"] == "suppressed_cap"
    assert state_of(home)["fires"] == 3


def test_delta_over_cap_skips_parsing_but_advances_offset(env):
    """회귀: v2 MINOR 2 — 2MB 초과 델타는 파싱 스킵, 오프셋만 전진."""
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    big = tool_result("Y" * 1024)  # 줄당 약 1KB
    append(transcript, [big] * 2200)  # > 2MB
    r = run_hook(payload, home)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    assert state_of(home)["offset"] == transcript.stat().st_size


def test_escape_prefix_skips_but_advances_offset(env):
    """회귀: Codex 리뷰 P2 — 스킵한 턴의 델타가 다음 프롬프트에 합산되면 안 된다."""
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    append(transcript, [tool_use("Bash")] * 30)
    payload["prompt"] = "#nr 조용히 진행해"
    assert run_hook(payload, home).stdout == ""
    assert state_of(home)["offset"] == transcript.stat().st_size

    # 다음 일반 프롬프트: 새 델타가 없으므로 낡은 넛지가 뒤늦게 발화하지 않는다
    payload["prompt"] = "다음 작업"
    assert run_hook(payload, home).stdout == ""


def test_kill_switch_skips_but_advances_offset(env):
    """회귀: Codex 리뷰 P2 — 킬 스위치 중에도 오프셋은 전진해야 한다."""
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    append(transcript, [tool_use("Bash")] * 30)
    r = run_hook(payload, home, env_extra={"CLAUDE_SKIP_DELEGATE_NUDGE": "1"})
    assert r.returncode == 0
    assert r.stdout == ""
    assert state_of(home)["offset"] == transcript.stat().st_size


def test_mcp_namespace_verb_not_counted(env):
    """회귀: Codex 리뷰 P2 — 서버 네임스페이스의 동사로 action 도구를 세면 안 된다."""
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    append(transcript, [tool_use("mcp__mcp_search__build_corpus")] * 30)
    assert run_hook(payload, home).stdout == ""  # direct 0 — 발화 없음

    append(transcript, [tool_use("mcp__notion__notion-search")] * 10)
    assert "[DELEGATE-NUDGE]" in run_hook(payload, home).stdout  # 연산명 매칭은 유효


def test_env_threshold_override(env):
    home, transcript, payload = env
    cold_start(home, transcript, payload)
    append(transcript, [tool_use("Bash")] * 4)
    r = run_hook(payload, home, env_extra={"DELEGATE_NUDGE_CALLS": "4"})
    assert "[DELEGATE-NUDGE]" in r.stdout


@pytest.mark.parametrize(
    "payload",
    [None, {"prompt": "x"}, {"session_id": 3}, [1, 2, 3], "문자열", 42],
)
def test_malformed_input_exits_quietly(tmp_path, payload):
    """회귀: 재리뷰 지적 — 비 dict top-level JSON 도 exit 0 + 무출력이어야 한다."""
    r = run_hook(payload, tmp_path)
    assert r.returncode == 0
    assert r.stdout == ""


def test_path_traversal_session_id_rejected(env):
    home, transcript, payload = env
    payload["session_id"] = "../evil"
    r = run_hook(payload, home)
    assert r.returncode == 0
    assert r.stdout == ""
    assert not (home / ".claude/state/delegate-nudge").exists()


def test_missing_transcript_exits_quietly(env):
    home, transcript, payload = env
    payload["transcript_path"] = str(transcript) + ".nope"
    r = run_hook(payload, home)
    assert r.returncode == 0
    assert r.stdout == ""
