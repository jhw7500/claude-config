# claude-config

개인 Claude Code 설정 동기화 (스킬 + 셸 함수 + 스크립트 + 글로벌 지침). 여러 호스트에서 공유.

## 포함

| 경로 | 내용 |
|---|---|
| `skills/plugin-toggle/` | 플러그인 on/off 스킬. `enabledPlugins` 토글 + **`/reload-plugins`로 재시작 없이 즉시 적용** |
| `skills/gstack-toggle/` | gstack 사용자 스킬(~47개) on/off 토글(디렉토리 이동 방식). 미설치 호스트선 no-op이라 안전 |
| `shell/plug.sh` | `plug on\|off <key>` 셸 함수 (bkit/docs/pw/pyright/compound) |
| `scripts/` | 개인 hook/toggle: stop-text-required.py, timestamp-hook.py, bg-hud-complete.py, context-bar.sh, **apex-toggle.sh / gstack-toggle.sh** |
| `claude-md/` | 글로벌 지침: CLAUDE.md, CLAUDE-notion.md, RTK.md |

## 설치 (다른 호스트)

```bash
git clone https://github.com/jhw7500/claude-config.git
cd claude-config && ./install.sh
source ~/.bashrc
```

- `skills/`·`scripts/`는 **심볼릭 링크** → `git pull`만 하면 자동 갱신
- `CLAUDE-notion.md`·`RTK.md`는 없을 때만 복사
- `CLAUDE.md`는 상단 OMC 자동관리 블록 충돌 방지를 위해 **수동 머지** (`diff` 안내 출력)

## 토글 메커니즘 — 2종류 (대체 불가, 병행)

| 대상 | 메커니즘 | 도구 |
|---|---|---|
| **마켓플레이스 플러그인** (bkit, document-skills 등) | `enabledPlugins` 부울 토글 + `/reload-plugins` | `plug` / plugin-toggle 스킬 |
| **스킬 묶음/프레임워크** (gstack 47, apex) | 스킬 디렉토리를 `~/.claude/_disabled/`로 **이동** | `gstack-toggle.sh` / `apex-toggle.sh` / gstack-toggle 스킬 |

→ gstack/apex는 플러그인이 아니라 스킬 디렉토리 이동 방식이라 plugin-toggle로 대체되지 않습니다. 각자 전용 토글을 씁니다.

## 사용

```bash
plug off bkit                            # 플러그인 끄기 → 세션에서 /reload-plugins
~/.claude/scripts/gstack-toggle.sh on    # gstack 스킬셋 활성화 → 새 세션
```

## 동기화하지 않는 것 (중요)

- `~/.claude/settings.json`, `~/.claude.json` 은 호스트마다 설치 플러그인·MCP·프로젝트 경로가 달라
  통째 동기화하면 다른 호스트가 깨집니다. 플러그인 정책은 각 호스트에서 `plug`/스킬로 적용하세요.

## 배경

무거운 플러그인(bkit always-on ~13k tok 등)을 전역 off baseline으로 두고, 필요할 때만 켜서
plan 사용 한도 소비와 응답 stall을 줄이기 위한 설정입니다.
