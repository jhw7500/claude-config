# claude-slack-bridge

Slack 비공개 채널에서 기존 Claude Code 세션을 헤드리스로 이어서 작업.

## 동작
메시지 → 선택된 세션을 `claude -p --resume <id> --output-format json
--permission-mode acceptEdits`(+위험 명령 deny)로 한 턴 실행 → 결과 회신.

## 설치
1. `manifest/slack-app.yaml` 로 Slack 앱 생성(From a manifest).
2. App-Level Token(`connections:write`)·Bot Token·비공개 채널 ID·내 Slack user ID 확보.
3. `secrets.local.env` 에 `SLACK_BOT_TOKEN/SLACK_APP_TOKEN/SLACK_CHANNEL_ID/SLACK_ALLOWED_USER_ID` 채우기.
4. `bash scripts/setup-slack-bridge.sh` 실행.

## 채널 명령
- `sessions` / `list` — 최근 세션 목록(버튼)
- `select <번호|id>` — 대상 세션 선택
- (일반 텍스트) — 대상 세션에서 한 턴 실행
- `fork <메시지>` — 대상이 활성일 때 분기 실행
- `clear` / `status`

## 한계
유휴가 아닌 "직접 실행" 방식이라 항상 실시간이지만, 턴마다 세션 transcript
전체를 로드하므로 큰 세션은 턴 비용이 큼(회신에 `💰 비용` 표기). 타 머신/스트리밍은 비목표.
