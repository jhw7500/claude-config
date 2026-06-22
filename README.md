# claude-config

개인 Claude Code 설정 동기화 (스킬 + 셸 함수 + 스크립트 + 글로벌 지침). 여러 호스트에서 공유.

## 포함

| 경로 | 내용 |
|---|---|
| `skills/plugin-toggle/` | 플러그인 on/off 스킬. `enabledPlugins` 토글 + **`/reload-plugins`로 재시작 없이 즉시 적용** |
| `skills/gstack-toggle/` | gstack 사용자 스킬(~47개) on/off 토글(디렉토리 이동 방식). 미설치 호스트선 no-op이라 안전 |
| `slack-bridge/` | Slack 비공개 채널 ↔ Claude Code 세션 헤드리스 브릿지. `setup-slack-bridge.sh`로 systemd `--user` 서비스 설치. 상세는 `slack-bridge/README.md` |
| `shell/plug.sh` | `plug on\|off <key>` 셸 함수 (bkit/docs/pw/pyright/compound) |
| `scripts/` | 개인 hook/toggle: stop-text-required.py, timestamp-hook.py, bg-hud-complete.py, context-bar.sh, **apex-toggle.sh**, **setup-mcp.sh** |
| `hooks/` | 커스텀 훅 6개: carl·notion-continuous·general-continuation·post-info·post-action·bg-task. install.sh가 env-aware **자동 배선** |
| `manifest/` | `mcp.json` — MCP 서버 정의(공개 6 + 사내 4). `setup-mcp.sh`가 사용 |
| `claude-md/` | 글로벌 지침: **global-guidance.md**(공통·항상) + CLAUDE-notion.md(notion 환경) + RTK.md(rtk 환경) |

## 설치 (다른 호스트)

```bash
git clone https://github.com/jhw7500/claude-config.git
cd claude-config && ./install.sh
source ~/.bashrc
```

- `skills/`·`scripts/`는 **심볼릭 링크** → `git pull`만 하면 자동 갱신
- **CLAUDE.md 전역지침 (env-aware)**: `~/.claude/CLAUDE.md`의 `claude-config:START/END` 블록만 관리 — 항상 `@global-guidance.md`, **notion MCP 있으면** `@CLAUDE-notion.md`, **`rtk` 있으면** `@RTK.md`를 자동 추가. **OMC 블록은 미변경**(inline/file-split 무관), 실행 전 `.bak` 백업
- **훅 자동 배선**: `settings.json`에 멱등 추가 — `.bak` 백업, `statusLine`·기존 훅 보존
  - **항상**: `timestamp`(프롬프트/완료) · `stop-text-required`(조기종료 방지) · `general-continuation` · `bg-task-progress`(Pre/Post/SubagentStop, `Agent|Bash`) · `post-info-tool-continuation`
  - **notion 환경**: `notion-continuous-exec` · `post-action-tool-report`
  - `carl-hook`은 파일만 동기화하고 자동 배선하지 않음(APEX/CARL 사용 시 수동 배선)
- `context-bar`(statusLine 교체)는 **미포함** (현재 OMC HUD와 상호배타라 별도 결정 필요)

## 토글 메커니즘 — 2종류 (대체 불가, 병행)

| 대상 | 메커니즘 | 도구 |
|---|---|---|
| **마켓플레이스 플러그인** (bkit, document-skills 등) | `enabledPlugins` 부울 토글 + `/reload-plugins` | `plug` / plugin-toggle 스킬 |
| **스킬 묶음** (gstack ~47) | 스킬 디렉토리를 `~/.claude/skills-disabled/`로 **이동** (gstack 본체·CLI 보존) | gstack-toggle 스킬 |
| **커맨드 프레임워크** (APEX 7) | `commands/` 그룹을 `~/.claude/_disabled/commands/`로 **이동** | `apex-toggle.sh` |

→ 둘 다 플러그인이 아니라 디렉토리 이동 방식이라 plugin-toggle로 대체되지 않습니다. 각자 전용 토글을 씁니다.

## 사용

```bash
plug off bkit                            # 플러그인 끄기 → 세션에서 /reload-plugins
# gstack 스킬셋은 Claude 세션에서 "gstack 켜줘/꺼줘" (gstack-toggle 스킬)
~/.claude/scripts/apex-toggle.sh off     # APEX 커맨드 비활성화 → 새 세션
```

## MCP 등록 (opt-in)

MCP 서버는 호스트별 상태(설치·키·경로)라 `install.sh` 본체에 넣지 않고 **별도 스크립트**로 둡니다.
필요한 호스트에서만:

```bash
cp secrets.example.env secrets.local.env   # 키·경로 채우기 (.gitignore로 제외됨)
bash scripts/setup-mcp.sh                   # 사내 포함 (또는 --no-internal / --dry-run)
```

`manifest/mcp.json`의 서버를 `claude mcp add <name> -e KEY=val -- <cmd>` 로 멱등 등록합니다(이미 있으면 skip).
공개 6개(brave/morph/pdf/sequentialthinking/filesystem/repowire) + 사내 4개(cts-email/cts-ta/jhw-notion/ssh-mcp).
notion(OAuth)·repowire-channel은 수동 확인이 필요합니다.

## 동기화하지 않는 것 (중요)

- `~/.claude/settings.json`, `~/.claude.json` 은 호스트마다 설치 플러그인·MCP·프로젝트 경로가 달라
  통째 동기화하면 다른 호스트가 깨집니다. 플러그인 정책은 각 호스트에서 `plug`/스킬로 적용하세요.

## 배경

무거운 플러그인(bkit always-on ~13k tok 등)을 전역 off baseline으로 두고, 필요할 때만 켜서
plan 사용 한도 소비와 응답 stall을 줄이기 위한 설정입니다.

## Slack 브릿지

폰/Slack에서 기존 Claude Code 세션을 이어서 작업. (repowire와 무관 — 독립 서비스)

1. `manifest/slack-app.yaml` 로 Slack 앱 생성 → 토큰 3개 + 내 user ID 확보
2. `secrets.local.env` 에 `SLACK_BOT_TOKEN/SLACK_APP_TOKEN/SLACK_CHANNEL_ID/SLACK_ALLOWED_USER_ID`
3. `bash scripts/setup-slack-bridge.sh`  (systemd `--user` 서비스 등록·기동)

상세·명령어·한계는 `slack-bridge/README.md` 참조.
