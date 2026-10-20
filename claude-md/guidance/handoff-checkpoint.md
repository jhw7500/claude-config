# HANDOFF 체크포인트 (전역)

> 매 세션 로드되는 `claude-md/global-guidance.md`에서 분리한 상황별 지침이다. 전역 지침에는 언제 이
> 파일을 읽을지만 남겼고, 규칙 본문의 정본은 이 파일이다.

> 전제: `handoff-checkpoint-hook.py` 배선됨 (claude-config `install.sh`). 미설치 호스트에서는
> 리마인더가 오지 않을 뿐, phase 경계 수동 갱신 권장은 동일하게 적용된다.

- UserPromptSubmit 훅이 세션 작업량 임계마다 `[HANDOFF-CHECKPOINT]` 리마인더를 주입한다.
  수신 시: 지금이 **phase 경계**(구현 완료 / 리뷰 라운드 종료 / 외부 게이트 판정 확정 직후 / 머지 /
  저장 완료)면 사용자에게 묻지 말고 repo root `HANDOFF.<세션>.md`를 즉시 갱신한다. 경계 중간이면
  다음 경계에서 갱신한다. 외부 게이트(Tribunal, CI, 리뷰 verdict)의 판정 직후, 특히 FAIL 직후는 곧
  미커밋 수정이 길어지는 구간이라 체크포인트가 가장 필요하다.
- 체크포인트는 **15줄 이내로 덮어쓴다**: ① 완료·검증된 것 ② 다음 구체 액션 1개 ③ 제약
  (코드 프리즈, 보드 예약 등) ④ 열린 PR/이슈 번호. Task 세션이면 tsk-/clm-id를 남긴다. 게이트
  판정 직후라면 게이트 식별자(run·round·HEAD)와 blocker ID 목록을 함께 남겨, 로그 없이 판정 상태를
  재구성할 수 있게 한다.
- 이 절은 8번 "Handoff 층위 분리"의 세션 스냅샷 권장을 리마인더로 강제하는 장치다 — 목표는
  크래시 후 `/revive` 재구성이 아니라 **체크포인트 resume**이 기본 복구 경로가 되는 것.
  `/revive`는 최후 수단으로 유지한다.
- 임계 조정: env `HANDOFF_CHECKPOINT_FIRST_MB` / `HANDOFF_CHECKPOINT_STEP_MB` (기본 각 3MB).
