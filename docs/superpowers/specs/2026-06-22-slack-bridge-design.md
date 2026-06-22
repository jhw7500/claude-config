# Slack ↔ Claude Code 세션 브릿지 — 설계 (헤드리스 방식)

- **날짜**: 2026-06-22
- **상태**: 설계 승인 대기 (사용자 리뷰)
- **저장소**: `claude-config` (개인 Claude Code 설정 동기화)

## 1. 배경 / 의사결정 경로

목표: Claude Code 세션을 데스크톱에서 떠나 있을 때 Slack에서 **보고(턴 결과 확인)**, **이어서 지시하고**, **답을 받는다**.

### 1.1 1차 후보(폐기) — repowire 내장 Slack 브릿지
이 호스트엔 repowire(`0.10.1`, systemd 데몬)가 있고 `slack/bot.py`로 **Slack 브릿지가 이미 구현**돼 있어, 처음엔 "토큰 3개만 설정" 방향을 잡았다. 그러나 **실측 검증에서 신뢰성 문제**가 드러나 폐기:

- **클라우드 relay 사망**: `wss://repowire.io` WS 핸드셰이크가 `HTTP 404`로 거부, 30초마다 재시도 222회+ (사이트 자체는 200 → 엔드포인트/버전 불일치 또는 relay 폐지). → **타 머신 연결 불가.**
- **유휴 세션 실시간 제어 불가**: `online`으로 표시된 peer(`wlan-package-2`)에 `ask_peer` **타임아웃**. 원인 — repowire는 Claude Code의 `Stop`/`UserPromptSubmit`/`Notification(idle_prompt)` hook 이벤트에 얹어 메시지를 주입하므로, **유휴 에이전트 루프를 강제로 깨우는 server-push가 없다.** 폰 사용 시나리오(세션 대개 유휴)와 정면 충돌.

### 1.2 채택 방식 — 헤드리스 실행(on-demand)
Slack 메시지가 오면 그 자리에서 `claude -p --resume <id>`로 **세션을 한 턴 실행**하고 결과를 Slack에 회신. 항상 온디맨드로 실행되므로 "유휴라 응답 못 함"·relay 문제를 **원천 우회**하고, 기존 세션 히스토리를 이어간다.

> 참고(대안): Claude Code 네이티브 `--remote-control` + 모바일/웹 앱 경로도 존재. Slack 통합이 명시 요구라 본 설계는 Slack-헤드리스로 진행하되, 네이티브 경로는 추후 별도 조사 가능.

## 2. 확정된 기술 사실 (실측)

- `claude -p --output-format json` → 단일 JSON 반환:
  `{"type":"result","subtype":"success","is_error":false,"result":"<답 텍스트>","session_id":"<uuid>","permission_denials":[],"num_turns":1,"total_cost_usd":0.29,...}`
- 세션 잇기: `claude -p --resume <id>` → **기본 같은 id에 append**(`--fork-session`이면 새 id로 분기).
- 세션 저장 위치: `~/.claude/projects/<cwd인코딩>/<session-id>.jsonl` (**파일명 = session id**, 디렉터리명 = cwd의 `/`→`-` 인코딩).
- 권한: `--permission-mode plan|acceptEdits|bypassPermissions`, `--allowedTools/--disallowedTools`, 그리고 settings의 `permissions.deny` 규칙(`Bash(rm:*)` 등).
- 버전: `claude` 2.1.185. **비용 주의**: 빈 컨텍스트가 아니라 resume 시 전체 transcript를 매 턴 로드 → 한 메시지가 풀컨텍스트 턴(예: 32k 토큰 컨텍스트의 사소한 턴이 $0.29). §9 참조.

## 3. 결정 사항 (사용자 확정)

| 항목 | 결정 |
|---|---|
| 세션 모델 | **기존 세션 resume + 충돌회피** (목록→선택→`--resume`) |
| 권한 모델 | **편집 허용(acceptEdits)** + 위험 명령 차단(deny 규칙) |
| 산출물 깊이 | 풀세트 (시크릿 템플릿 · 앱 매니페스트 · 브릿지 서비스 · setup 스크립트 · README) |
| 채널 | 비공개(private) |
| 기술 스택 | **Python + `slack_bolt`(Socket Mode)** |
| 회신 방식 | **v1 단일 회신**(`--output-format json`의 `.result`); 스트리밍은 후순위 |

## 4. 아키텍처

```
Slack(비공개 채널, Socket Mode)
        ⇅
[claude-slack-bridge 서비스]  (long-running, systemd --user)
   - Slack 리스너(인가된 채널/사용자만)
   - 세션 레지스트리/선택기
   - 헤드리스 러너 (async)
        ↓  claude -p --resume <id> --output-format json
           --permission-mode acceptEdits  [+deny 규칙]   (세션 cwd에서 실행)
        ↑  result / permission_denials / session_id
        ⇅
   결과를 Slack에 회신
```

브릿지는 repowire와 **무관**(relay·데몬 의존 없음). Slack↔브릿지↔로컬 `claude` 프로세스만.

## 5. 컴포넌트

1. **Slack 리스너** — Socket Mode로 비공개 채널 이벤트 수신. **인가 게이트**: 설정된 채널 ID + 설정된 Slack 사용자 ID에서 온 메시지만 처리(그 외 무시). 봇 메시지/서브타입 무시.

2. **세션 레지스트리/선택기** — `~/.claude/projects/*/*.jsonl` 스캔 → 각 세션의 `{id(파일명), cwd(디렉터리 디코딩), 최근활동(mtime), 제목(첫 user 메시지 스니펫)}`. 최근순 정렬, 버튼으로 제시. 채널별 sticky 대상(`_target`) 유지.

3. **헤드리스 러너** — sticky 세션에 대해 `claude -p --resume <id> --output-format json --permission-mode acceptEdits` 를 **해당 세션 cwd에서** async 실행. 시작 시 "🤔 작업 중…" ack, 완료 시 `.result`를 회신. `is_error`/`permission_denials` 있으면 함께 보고. 반환 `session_id` 추적(resume는 동일 id). 장시간 턴 대비 타임아웃·취소 처리.

4. **충돌회피** — resume 전 대상 `.jsonl`의 mtime이 임계(예: 90초) 내면 "현재 활성(다른 곳에서 사용 중일 수 있음)"으로 보고 ① 거부 후 안내 또는 ② `fork` 키워드로 `--fork-session` 분기(새 id 보고, 원본 보존). 비활성이면 in-place resume. (TUI 동시 열림 정확 감지는 best-effort — 구현 시 실증 검증)

## 6. Slack 명령(UX)

- `sessions` / `list` — 최근 세션 목록 + 💬 버튼(프로젝트 폴더·최근활동·첫 메시지 스니펫)
- 버튼 클릭 / `select <번호|id>` — 대상 세션 고정
- (일반 텍스트) — 고정된 세션에 한 턴 실행
- `fork` — 현재 대상이 활성일 때 분기 실행
- `clear` — 대상 해제
- `whoami`/`status` — 현재 대상·권한 모드 표시

## 7. 권한·안전 경계

- `--permission-mode acceptEdits`: 파일 편집은 자동 승인.
- **위험 명령 차단**: `permissions.deny`(전용 settings 파일을 `--settings`로 주입) 또는 `--disallowedTools`로 파괴적 Bash 패턴 차단 — 최소 `Bash(rm -rf:*)`, `Bash(git push --force:*)`, `Bash(git reset --hard:*)`, `Bash(:* drop table*)`, `Bash(mkfs:*)` 등. (정확한 규칙 문법은 구현 시 확정·검증)
- **인가**: 비공개 채널 + 발신자 Slack user ID 화이트리스트. 채널에 글 쓸 수 있는 사람 = Claude를 구동할 수 있는 사람이므로 채널 접근 통제가 1차 방어선.
- 토큰은 `secrets.local.env`(gitignore) 및 systemd 유닛 환경에만. 평문 로그 금지(마스킹).

## 8. 저장소 산출물 (풀세트)

- `secrets.example.env` 추가: `SLACK_BOT_TOKEN=`, `SLACK_APP_TOKEN=`, `SLACK_CHANNEL_ID=`, `SLACK_ALLOWED_USER_ID=`
- `manifest/slack-app.yaml` — Slack 앱 매니페스트(비공개 채널: `chat:write`/`groups:history`, bot event `message.groups`, Interactivity on, Socket Mode on). App-Level Token(`connections:write`)은 UI에서 1회 생성.
- `slack-bridge/` — `bridge.py`(서비스 본체), `requirements.txt`(slack_bolt+slack_sdk **또는** repowire식 httpx+websockets 경량 — §10 택1), `claude-slack-bridge.service`(systemd 유닛 템플릿), `README.md`. 전용 venv는 gitignore.
- `scripts/setup-slack-bridge.sh` — opt-in(`setup-mcp.sh` 패턴): `secrets.local.env` 로드 → 토큰 검증 → venv+deps 설치 → `~/.config/systemd/user/claude-slack-bridge.service` 생성(`.bak`) → `systemctl --user enable --now` → 검증 출력. `--dry-run` 지원.
- `README.md` "Slack 브릿지" 섹션: 앱 생성(매니페스트)→토큰→`setup-slack-bridge.sh`→사용법/한계.

## 9. 응답 방식 / 성능 / 비용

- **v1 (확정)**: 턴당 단일 회신(`--output-format json`의 `.result`) + "작업 중" ack. (단순·견고, 비용 동일)
- **v2(후순위)**: `--output-format stream-json`으로 부분 출력 스트리밍. (비용 중립 — 토큰 동일, 전달만 점진적)
- **비용**: resume는 매 턴 전체 transcript 로드 → 큰 세션일수록 턴 비용 큼. 대상 세션 크기/요금을 사용자가 인지하도록 결과에 `total_cost_usd` 표기.

## 10. 기술 스택 (확정)

- **확정**: Python + `slack_bolt`(Socket Mode) — 공식 SDK, 버튼/이벤트 처리 간단. 전용 venv(gitignore)에 `slack_bolt`+`slack_sdk` 설치.
- (참고) 경량 대안이었던 `httpx`+`websockets` 직접 구현(repowire `slack/bot.py` 방식)은 미채택.
- 서비스는 systemd `--user` 유닛으로 상시 가동·enabled(재부팅 유지).

## 11. 검증

1. 단위: 세션 스캔/파싱(목록·cwd 디코딩·제목 추출), deny 규칙이 위험 명령을 실제 차단하는지.
2. 라이브: Slack에서 테스트 세션 `select` → 메시지 → 그 세션 cwd에서 resume 실행 → `.result` 회신 확인. `fork` 경로. acceptEdits로 편집 1건 성공, 위험 명령 1건 차단 확인.
3. 동시성: 대상 세션 활성 시 충돌회피 동작.
4. 지속성: 재부팅/재로그인 후 서비스 자동 기동.

## 12. 비목표 / 리스크

- **비목표**: 타 머신(cross-host) 도달, 별도 인터랙티브 세션의 수동(passive) 실시간 관전, 스트리밍(v2), 네이티브 `--remote-control` 경로.
- **리스크**: ① 장시간 턴(타임아웃/취소 UX) ② 턴당 풀컨텍스트 비용 ③ TUI 동시 열림 정확 감지 한계(best-effort) ④ `claude` CLI 버전 변화로 플래그/출력 형태 드리프트 → 검증으로 회귀 확인 ⑤ deny 규칙 문법 정확성(구현 시 실증).
