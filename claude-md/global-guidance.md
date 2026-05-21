# 전역 지침 (모든 Claude Code 세션 공통)

이 파일(`~/.claude/global-guidance.md`)은 `~/.claude/CLAUDE.md`에서 `@global-guidance.md`로 import되어 **모든 프로젝트 세션에 전역 적용**된다. 프로젝트별 지침은 각 프로젝트의 `CLAUDE.md` / `AGENTS.md`에 둔다.

---

> Notion 작업 전용 지침은 `@CLAUDE-notion.md` 참조 — `/jhw:*` 스킬과 `mcp__notion__*` / `mcp__jhw-notion__*` 도구 호출 시 적용. (notion이 없는 호스트에서는 import되지 않음)

---

## 공통 작업 규칙 (전역)

1. **절대 경로 사용** — 코드에서도, 사용자에게 파일 언급할 때도 절대 경로 사용.
2. **독립 작업 병렬 실행** — 의존성 없는 도구 호출은 반드시 병렬 배치.
3. **독립 검증 필수** — 구현 완료 후 자기 검증 외 독립 검증 수단 사용.
4. **변경 범위 최소화** — 요청한 것만 수정. 주변 코드 "개선"/리팩토링/주석 추가 금지.
5. **테스트 먼저 확인** — 수정 전 기존 테스트 실행하여 현재 상태 파악.
6. **롤백 가능한 변경 우선** — 되돌릴 수 없는 작업(force push, drop table 등)은 사전 확인.
7. **한국어 응답** — 모든 응답과 설명은 한국어로 작성.

---

## 진행상황 보고 (전역)

- 탐색 시작 전 1~2문장으로 무엇을 찾는지 먼저 말한다.
- 단계 변경 시 (지금 한 일 / 핵심 사실 / 다음 할 일)을 짧게 보고한다.
- 백그라운드 작업이 있으면 무엇이 돌고 있는지 명시한다.
- 코드 수정 후 (무엇을 / 어느 파일 / 무엇을 검증할지)를 바로 말한다.
- 막히면 (어디서 막혔는지 / 배제된 원인 / 다음 우회) 즉시 공유한다.
- 도구만 연속 호출하고 침묵하지 않는다.

---

## 정보 수집 → 조기 종료 방지 (전역)

**트리거** (다음 도구 호출 직후 또는 결과 수신 직후):
- 정보 수집·탐색 도구 일반: `WebFetch`, `WebSearch`, `query-docs`, MCP `*-fetch` / `*-search` 류
- **`AskUserQuestion`** — 사용자 답변 수신 직후 (답변에 의존하는 action까지 같은 응답 안에서 수행)
- 도메인별 세부는 도메인 문서 참조 (예: Notion → `@CLAUDE-notion.md`)

**필수 자가 점검 (응답 종료 직전)**:
1. 사용자 요청이 다단계인가? (저장/편집/생성/실행/적용 등 action을 포함)
2. 이 응답에 그 action 도구 호출(MCP create/update / `Edit` / `Write` / `Bash` 등)이 **승인된 모든 항목에 대해** 포함됐는가?
   - 항목들이 다른 parent/대상/그룹에 속하면 **그룹 수만큼** 분할 호출 필요
3. action 호출 후 **결과 요약 텍스트 보고**가 응답 마지막에 포함됐는가?
   - 도구 결과 JSON만으로는 부족 — 사용자가 한 눈에 볼 수 있는 표/요약 텍스트 의무
   - URL/ID/exit code 등 핵심 정보를 1줄 이상 텍스트로 전달
4. 위 항목 중 하나라도 빠지면 **응답 종료 금지** — 같은 응답 안에서 마저 처리.

**AskUserQuestion 특칙**:
- 답변이 단일 응답 단위로 반환되더라도, 그 답변에 의존한 action(파일 수정/삭제/이동/커밋 등)을 **같은 응답 안에서** 즉시 수행한다.
- 다단계 답변(중간에 추가 조회 필요)이라도 마지막 action까지 같은 응답에서 마친다.
- "답변 잘 받았습니다" 류 보고만 하고 종료하는 패턴 금지.

**일반 위반 사례**:
- 2026-05-07: AskUserQuestion 3건 답변 수신 후 .gitignore Edit/파일 삭제/커밋 action을 이어가지 않고 응답 종료 (사용자가 "멈춘것같은데"라고 재촉)
- 2026-05-08: 단일 도구(Bash) 결과 수신 직후 **텍스트 응답 0줄로 turn 종료** → 사용자 인터럽트. **방지**: 도구 결과 수신 직후 응답 종료 직전 자가점검 — "텍스트 1줄 이상 + (다단계면) 후속 action 포함?" 미충족 시 즉시 이어가기. Stop hook으로 보강 (`~/.claude/scripts/stop-text-required.py`).
- 2026-05-14: `AskUserQuestion` 답변 수신 직후 **텍스트 0줄·후속 action 0개로 turn 종료** → 사용자 인터럽트. **방지**: stop-text-required.py에 "직전 user turn이 AskUserQuestion tool_result이고 현재 assistant turn이 비어있음" 검출. 답 수신 후엔 같은 응답에서 (a) 다음 질문 또는 (b) 설계/액션 중 하나를 반드시 출력.

> Notion 작업 도메인 위반 사례 누적은 `@CLAUDE-notion.md` 참조.

---

## 외부 비동기 작업 자동 알림 (필수 규칙)

**문제**: GitHub Actions, gh workflows, 사내 CI 등 **외부 비동기 작업**은 Claude Code의
`task-notification`이 자동으로 잡지 못함. 사용자가 직접 "결과 확인해줘"를 입력해야 알 수
있는 패턴이 반복되어 매우 불편하다.

**필수 동작**:
사용자에게 "끝나면 알려드릴게요/보고드릴게요/모니터링할게요" 라고 약속하는 순간, 다음 중
하나를 **반드시 실행**한다 — 약속 후 가만히 있는 것은 위반:

1. **`gh run watch <run-id> --exit-status` 를 BG로 실행**
   ```bash
   nohup gh run watch <RUN_ID> --exit-status > /tmp/watch.log 2>&1 &
   ```
   - 이건 내가 띄운 BG → 워크플로우 종료 시 task-notification 자동 발생
   - notification 받은 그 턴에서 즉시 결과 확인 + 사용자에게 보고
2. **`/schedule` 스킬로 일회성 예약** (특정 시각 또는 N분 후)
   - 별도 Claude 세션이 예약된 시각에 자동 실행 → 결과 확인 + 알림
3. **로컬 스크립트면 `run_in_background=true` Bash로 실행**
   - 이건 종료 시 task-notification 자동 발생 (이미 잘 동작하는 메커니즘)

**금지**: ScheduleWakeup만 사용하고 끝내기 — `/loop` 외부에서는 사용자 메시지에 흡수돼
무력화됨. 신뢰할 수 없는 알림.

**자가 점검 (응답 종료 직전)**:
- "결과 알려드릴게요/모니터링할게요" 류 약속을 했나?
- 이 응답에 위 1/2/3 중 하나의 자동화 호출이 포함됐나?
- 약속만 하고 자동화 호출 없으면 **응답 종료 금지**.

---

## BG 완료 알림 자동 반영 (수동 호출 규칙)

Claude Code가 Bash `run_in_background=true` 완료 시 전달하는
`<task-notification>` system-reminder는 훅으로 잡을 수 없다 (transcript에
persist되지 않음). 따라서 수신 즉시 아래 스크립트로 HUD state에 반영한다:

```bash
python3 ~/.claude/scripts/bg-hud-complete.py <tool_use_id> <status>
```

- `<tool_use_id>`: notification의 `<tool-use-id>` 값 (예: `toolu_01AF...`)
- `<status>`: `<status>` 값 — `completed` 또는 `failed`
- 스크립트가 현재 `.omc/state/sessions/*/hud-state.json`을 자동 탐색해 마킹.

### 언제 호출?
- `<task-notification>`을 받은 **바로 그 턴**에 호출 (별도 사용자 요청 없이).
- 다른 작업 중이라면 병렬 Bash 호출로 처리해 본래 작업 흐름을 끊지 않는다.

### 보고 방식
- 성공 시 1문장 또는 현재 진행 중 응답에 자연스럽게 통합 ("BG `<id 끝 4자>` 완료 반영").
- 실패 시 (`exit 2`) 조용히 스킵 — 이미 stale로 처리됐거나 state 갱신 전일 뿐.

### 적용 범위
- Bash BG만 해당 (Agent BG는 SubagentStop 훅이 즉시 처리).
- 한 notification에 여러 태그가 있으면 각각 개별 호출.
