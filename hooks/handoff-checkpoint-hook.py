#!/usr/bin/env python3
"""
Handoff Checkpoint Hook (UserPromptSubmit)

세션 작업량(transcript 크기)이 임계를 넘을 때마다 [HANDOFF-CHECKPOINT]
system-reminder를 1회 주입해, phase 경계에서 HANDOFF.<세션>.md 체크포인트를
갱신하도록 유도한다. 크래시 후 /revive 재구성이 아니라 체크포인트 resume이
기본 복구 경로가 되게 하는 장치 (전역지침 "HANDOFF 체크포인트" 절 참조).

- 입력(stdin): Claude Code UserPromptSubmit hook JSON
  (session_id, transcript_path, prompt 사용)
- 출력(stdout): 임계 도달 시 reminder 텍스트. 미도달/판단 불가 시 빈 출력.
- 상태: ~/.claude/state/handoff-checkpoint/<session_id> 에 마지막 알림 시점의
  transcript 크기를 기록. 상태 오류는 조용히 무시 (알림 1회 손실이 최악).
- 임계: 최초 HANDOFF_CHECKPOINT_FIRST_MB(기본 3), 이후 HANDOFF_CHECKPOINT_STEP_MB
  (기본 3) 성장마다. transcript 크기는 현재 컨텍스트가 아니라 누적 작업량의
  근사치다 — compact 후에도 계속 자라므로 "phase 경계 리마인더" 용도로만 쓴다.
"""
import json
import os
import sys

ESCAPE_PREFIXES = ("#noreminder", "#nr", "#raw", "#silent", "#조용히")

REMINDER = """<system-reminder>
[HANDOFF-CHECKPOINT] 세션 작업량이 체크포인트 임계에 도달했다.

지금이 phase 경계(구현 완료 / 리뷰 라운드 종료 / 머지 / 저장 완료)라면 사용자에게
묻지 말고 repo root의 `HANDOFF.<세션>.md`를 즉시 갱신하라. 경계 중간이라면 다음
경계 도달 시 갱신하라.

- 15줄 이내로 덮어쓴다: ① 완료·검증된 것 ② 다음 구체 액션 1개 ③ 제약
  (코드 프리즈, 보드 예약 등) ④ 열린 PR/이슈 번호.
- Task 세션이면 tsk-/clm-id를 본문에 남겨 두 기록을 연결한다.
- 세부 규칙: ~/.claude/global-guidance.md > "HANDOFF 체크포인트 (전역)".
</system-reminder>"""


def mb(env_key: str, default: float) -> float:
    try:
        v = float(os.environ.get(env_key, ""))
        return v if v > 0 else default
    except ValueError:
        return default


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0

    prompt = payload.get("prompt") or ""
    if isinstance(prompt, str) and any(
        prompt.strip().lower().startswith(p) for p in ESCAPE_PREFIXES
    ):
        return 0

    session_id = payload.get("session_id") or ""
    transcript = payload.get("transcript_path") or ""
    if not session_id or not transcript or not os.path.isfile(transcript):
        return 0

    try:
        size = os.path.getsize(transcript)
    except OSError:
        return 0

    first = mb("HANDOFF_CHECKPOINT_FIRST_MB", 3.0) * 1024 * 1024
    step = mb("HANDOFF_CHECKPOINT_STEP_MB", 3.0) * 1024 * 1024

    state_dir = os.path.expanduser("~/.claude/state/handoff-checkpoint")
    state_file = os.path.join(state_dir, session_id)
    last = -1.0
    try:
        with open(state_file) as fh:
            last = float(fh.read().strip())
    except (OSError, ValueError):
        pass

    # last < 0: 아직 알림 없음 → first 임계. 이후에는 마지막 알림 크기 + step.
    threshold = first if last < 0 else last + step
    if size < threshold:
        return 0

    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(state_file, "w") as fh:
            fh.write(str(size))
    except OSError:
        return 0  # 상태를 못 쓰면 알림도 내지 않는다 (매 턴 반복 알림 방지)

    sys.stdout.write(REMINDER)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
