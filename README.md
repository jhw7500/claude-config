# claude-config

개인 Claude Code 설정 동기화 (스킬 + 셸 함수). 여러 호스트에서 공유.

## 포함

- `skills/plugin-toggle/` — 플러그인 on/off 스킬. `enabledPlugins`를 토글하고
  **`/reload-plugins`로 재시작 없이 즉시 적용**. 자연어("bkit 전역 꺼줘")로 동작.
- `skills/gstack-toggle/` — gstack 사용자 스킬(~47개) on/off 토글(디렉토리 이동 방식).
  gstack 미설치 호스트에선 no-op이라 안전.
- `shell/plug.sh` — `plug on|off <key>` 셸 함수 (bkit/docs/pw/pyright/compound).

## 설치 (다른 호스트)

```bash
git clone https://github.com/jhw7500/claude-config.git
cd claude-config && ./install.sh
source ~/.bashrc
```

스킬은 `~/.claude/skills/plugin-toggle`로 **심볼릭 링크**되어, `git pull` 시 자동 갱신됩니다.

## 사용

```bash
plug off bkit       # bkit 끄기 → 세션에서 /reload-plugins
plug on compound    # compound 켜기 → /reload-plugins
```
또는 Claude 세션에서: "bkit 전역 꺼줘" (plugin-toggle 스킬이 처리).

## 동기화하지 않는 것 (중요)

- `~/.claude/settings.json`, `~/.claude.json` 은 호스트마다 **설치된 플러그인·MCP·프로젝트 경로가 달라**
  통째 동기화하면 다른 호스트가 깨집니다.
- 플러그인 on/off 정책은 각 호스트에서 `plug` 또는 plugin-toggle 스킬로 적용하세요
  (스킬이 그 호스트의 실제 키를 읽어 처리).

## 배경

무거운 플러그인(bkit always-on ~13k tok 등)을 전역 off baseline으로 두고, 필요할 때만 켜서
plan 사용 한도 소비와 응답 stall을 줄이기 위한 설정입니다.
