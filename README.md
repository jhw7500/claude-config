# claude-config

개인 Claude Code 설정 동기화 (스킬 + 셸 함수 + 스크립트 + 글로벌 지침). 여러 호스트에서 공유.

## 포함

| 경로 | 내용 |
|---|---|
| `skills/plugin-toggle/` | 플러그인 on/off 스킬. `enabledPlugins` 토글 + **`/reload-plugins`로 재시작 없이 즉시 적용** |
| `skills/gstack-toggle/` | gstack 사용자 스킬(~47개) on/off 토글(디렉토리 이동 방식). 미설치 호스트선 no-op이라 안전 |
| `slack-bridge/` | Slack 비공개 채널 ↔ Claude Code 세션 헤드리스 브릿지. `setup-slack-bridge.sh`로 systemd `--user` 서비스 설치. 상세는 `slack-bridge/README.md` |
| `shell/plug.sh` | `plug on\|off <key>` 셸 함수 (bkit/docs/pw/pyright/compound) |
| `scripts/` | 개인 hook/toggle과 **jhw-control-host.py** secure launcher: stop-text-required.py, timestamp-hook.py, bg-hud-complete.py, context-bar.sh, apex-toggle.sh, setup-mcp.sh |
| `hooks/` | 커스텀 훅 6개: carl·notion-continuous·general-continuation·post-info·post-action·bg-task. install.sh가 env-aware **자동 배선** |
| `manifest/` | `mcp.json` — MCP 서버 정의(공개 6 + 사내 4). `setup-mcp.sh`가 사용 |
| `claude-md/` | 글로벌 지침: **global-guidance.md**(공통·항상) + CLAUDE-notion.md(notion 환경) + RTK.md(rtk 환경) |

## 설치 (다른 호스트)

```bash
git clone https://github.com/jhw7500/claude-config.git
cd claude-config && ./install.sh
source ~/.bashrc
```

- `skills/`·`hooks/`·일반 `scripts/`는 **심볼릭 링크**라 `git pull`로 갱신됩니다. host-control
  launcher는 mode `0500` 보안 사본이므로 launcher 갱신에는 `./install.sh` 재실행이 필요합니다.
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

## Project Control Task launcher

`jhw-control-host`는 clean shell에서 Project Control 호출에 필요한 non-secret 좌표와 세 credential을
parent shell에 남기지 않고 child `jhw-control`에만 주입하는 **secure-store-only** launcher입니다.
contract v3의 명령 surface는 정확히 `unlock`, `preflight`, `portfolio status`, `task start`,
`task finish` 다섯 개입니다. `task start`와 `task finish` 앞에는 사용자 출력에 드러나지 않는
hidden preflight를 강제하며, 설정·저장소가 안전하지 않거나 preflight가 실패하면 Task mutation을
실행하지 않습니다.

지원 범위는 Linux Secret Service(DBus session), `/usr/bin/python3`의 system `keyring`·`SecretStorage`,
그리고 `auth status --show-token --json hosts`와 secure credential store를 지원하는 GitHub CLI입니다.
launcher는 현재 UID의 private `/run/user/<uid>`와 실제 D-Bus UNIX socket을 직접 검증·파생하므로
Codex/Claude parent shell에 `XDG_RUNTIME_DIR`나 `DBUS_SESSION_BUS_ADDRESS`를 주입하지 않습니다.
store가 잠겨 있으면 사용자 터미널에서 다음 한 명령으로 먼저 풉니다.

```bash
jhw-control-host unlock
```

이 명령은 기존 `org.freedesktop.secrets` owner를 고정하고 GNOME keyring 40의 exact private
`UnlockWithMasterPassword` 계약을 feature-detect한 뒤 canonical login collection만 해제합니다.
daemon을 생성·교체하거나 collection을 생성하지 않으며, 암호는 echo 없이 읽어 child 메모리와 로컬
D-Bus에만 전달하고 argv·환경·파일·출력에는 넣지 않습니다. 모든 종료 경로에서 terminal 상태를
복구하고 읽지 않은 입력도 폐기합니다. 비지원 Secret Service나 계약 변경은
fail-closed합니다. 이미 풀렸으면 암호를 묻지 않습니다. 잠금 해제 후 backend를 고정해 다음 값을
명시적으로 provision합니다.

```bash
/usr/bin/python3 -I -m keyring --keyring-backend keyring.backends.SecretService.Keyring set jhw-control GH_PROJECT_TOKEN
/usr/bin/python3 -I -m keyring --keyring-backend keyring.backends.SecretService.Keyring set jhw-control NOTION_API_KEY
gh auth login --hostname github.com --git-protocol ssh --web
```

Project token과 Repo token은 달라야 합니다. GitHub CLI 저장 상태는 `gh auth status --hostname github.com
--active --json hosts`의 단일 active entry가 `tokenSource=keyring`이어야 하며, 평문 `hosts.yml` token은
거부됩니다. 계정이 없거나 평문 저장 상태이면 위 `gh auth login`을 다시 수행하고 launcher로
`tokenSource=keyring`을 재검증합니다(credential store가 없어서 `gh`가 평문 fallback하면 계속 거부).
`install.sh`는 launcher를 전용 `0700` 디렉터리의 mode `0500` 사본으로 atomic 설치하고
`~/.local/bin`에는 그 사본의 링크만 배치합니다. **설치 중 credential 조회·갱신을 하지 않고** 기존
파일 값을 자동 migration하지 않습니다. 잠김 시 조치는 `jhw-control-host unlock` 하나이며,
runtime 누락·credential 누락 시에도 launcher가 출력하는 path-free 단일 조치만 수행합니다.

보안 경계에서 현재 UID는 OS credential store에 접근할 수 있는 신뢰 주체입니다. launcher는 그 밖의
로컬 principal이 바꿀 수 있는 executable 및 ancestor를 거부합니다. group-write는 해당 group의
primary/supplementary member가 현재 UID 하나뿐임을 계정 DB에서 확인할 때만 허용하고, POSIX
access/default ACL과 world-write는 거부하며, ambient
`PATH`·Python/Node preload·credential 환경은 상속하지 않습니다. 고정 PATH의 모든 검색 디렉터리도
credential 조회 전에 같은 기준으로 검사하고, 검색 디렉터리 자체에는 sticky world-write도 허용하지
않습니다. installer 역시 `$HOME`부터 launcher entrypoint/target까지 기존 원본·해결 경로 체인을 먼저
검사합니다. 따라서 downstream `jhw-control` 설치물과 그 상위 디렉터리도 다른 principal의 write
권한이 없어야 합니다.

```bash
"$HOME/.local/bin/jhw-control-host" --contract
"$HOME/.local/bin/jhw-control-host" unlock
"$HOME/.local/bin/jhw-control-host" preflight
"$HOME/.local/bin/jhw-control-host" portfolio status
"$HOME/.local/bin/jhw-control-host" task start --resolve-from-checkout true <신규-Formal/Temporary-인자>
"$HOME/.local/bin/jhw-control-host" task start --task <tsk-id> <기존-Task-재개-인자>
"$HOME/.local/bin/jhw-control-host" task finish --task <tsk-id> --claim <clm-id> --status <completed|handoff|abandoned>
```

신규 Formal/Temporary Task는 visible absolute launcher preflight 성공 뒤 현재 checkout의 exact root와
`--resolve-from-checkout true`를 사용한다. Project/Repository ID를 portfolio 좌표에서 조합하거나
추측하지 않는다. resolver가 `PROJECT_REPOSITORY_NOT_FOUND`를 반환하면 Repository를 정확한 Project
Record에 등록한 뒤 재시도하고, `PROJECT_REPOSITORY_AMBIGUOUS`면 Repository의 Project 연관을 하나로
축소한 뒤 재시도한다. 어느 경우에도 Project를 임의 선택하거나 explicit mode로 자동 fallback하지
않는다. `--project`, `--repo-id`, `--task`는 start projection의 optional caller-coordinate binding이며,
생략해도 downstream Task/Claim coordinate validation은 약해지지 않는다.

`task finish`는 `task_id`, `claim_id`, `status`, `released_at`, `worktree_removed`만 엄격히 projection한다.
`handoff`는 canonical `handoff_pointer`만 허용하고 worktree 제거 또는 cleanup error를 허용하지 않는다.
`completed`와 `abandoned`는 pointer를 허용하지 않으며, worktree가 남으면 정확히
`WORKTREE_CLEANUP_FAILED` cleanup_error여야 한다. 반환 오류도 allowlist와 reason을 엄격히 검사한다:
`HANDOFF_RETRY_CONFLICT`의 handoff/git-state metadata reason, `INVALID_WORKTREE_INSPECTION`의
`duplicate_dirty_files`, `WORKTREE_DIRTY`의 `handoff_copy_not_plain_file`만 reason을 가진다.

producer rollout 순서는 `producer merge → install.sh 재실행 → clean-shell --contract/preflight
→ jhw-notion resolver/skill merge → approved real Task smoke`다. installer는 producer-first 갱신을
지원하지만, 설치 중 credential 조회·갱신을 하지 않고 기존 파일 값을 자동 migration하지 않는다.

launcher 자체 오류는 path-free JSON과 안정적인 exit code로 반환합니다. 하위 `jhw-control` 출력은
12 KiB·단일 JSON·command별 strict schema·stdout/stderr 계약을 검증한 뒤 canonical JSON으로 다시
직렬화합니다. `task start` 성공은 `task_id`, `claim_id`, `branch`, `worktree_ref`와 검증된 optional
`latest_handoff`만 남기며, 명시한 `--task`/`--project`/`--repo-id`와 응답 좌표도 대조합니다. 다른
호스트가 먼저 선점한 정상 충돌의 host 값은 bounded 검증·민감정보 검사만 하고 반환하지 않습니다.
오류 code와 exit code 조합도 command별 계약과 다르면 폐기합니다. token 또는 보호된
config/credential-store/입력 checkout 경로가 raw·stream 결합·JSON decode·base64·대소문자가 섞인
hex/percent escape·표준 URL quote 형태로 출력에 섞이면 전체 출력을 폐기하고
`SENSITIVE_OUTPUT_REJECTED`로 실패합니다. Portfolio의 Unicode 텍스트 길이, GitHub ID byte 상한,
repository slug도 downstream schema와 같은 경계로 검증합니다. Claude와 Codex의
`$jhw-task` 정본 연동은 연결된 jhw-notion #74 Formal Task에서 같은 delivery sequence로 배포합니다.

## MCP 등록 (opt-in)

MCP 서버는 호스트별 상태(설치·키·경로)라 `install.sh` 본체에 넣지 않고 **별도 스크립트**로 둡니다.
필요한 호스트에서만:

```bash
bash scripts/setup-mcp.sh --no-internal     # preview/check (사내 포함은 옵션 생략)
bash scripts/setup-mcp.sh --no-internal --apply
# 이전 스크립트가 만든 legacy local 항목까지 user scope로 명시적으로 이동할 때만:
bash scripts/setup-mcp.sh --no-internal --apply --migrate-local
```

기본 실행과 `--check`는 비파괴 preview이며 `--dry-run`은 호환 별칭입니다. 종료 코드는
동기화 완료 `0`, missing/drift `2`, 안전하게 진행할 수 없는 상태 또는 운영 오류 `1`,
잘못된 모드 조합 `64`입니다. 변경은 `--apply`를 명시한 경우에만 수행합니다.

`manifest/mcp.json`의 서버는 개인 프로젝트 전반에서 쓰는 `user` scope로 관리합니다.
기존 type/command/args/env를 비교해 `IN_SYNC`, `MISSING`, `DRIFT`만 값 없이 보고합니다.
현재 작업 프로젝트의 project scope에 같은 이름이 있으면 항상 `SHADOWED`로 차단합니다.
legacy local 항목도 기본적으로
차단하며, 검토 후 `--apply --migrate-local`을 명시한 경우에만 모든 local project map에서
해당 관리 이름을 제거하고 user 항목으로 이동합니다. 관리하지 않는 항목과 다른 설정 키는 보존합니다.

apply는 `claude` 자식 프로세스나 remove/add 명령을 실행하지 않습니다. private regular file인
사용자 설정을 잠근 뒤 preview 때 읽은 전체 바이트와 다시 비교합니다. 설정 디렉터리도 현재 사용자
소유의 실제 directory이며 group/world-writable이 아니어야 합니다. preview의 directory inode를
apply에서 다시 확인하고, lock/read/temp/replace/fsync를 같은 dirfd에 고정합니다. 모든 관리 항목을
mode `0600` 임시 파일에 만든 후 한 번의 atomic replace로 커밋하며, 비교 중 외부 변경이 감지되면
아무것도 적용하지 않고 중단합니다. 이 스크립트의 동시 실행은 lock으로 직렬화되지만 lock을 따르지 않는
외부 설정 변경과 함께 실행해서는 안 됩니다. project `.mcp.json`도 preview에서 전체 바이트를 snapshot하고
커밋 직전에 다시 비교하므로 동시에 변경되면 적용 없이 중단합니다. replace 후 directory fsync만 실패하면
이미 적용됐을 수 있으므로 `APPLIED_UNCONFIRMED`를 보고하며, 다른 설정 작업을 멈춘 뒤 `--check`로 실제
상태를 확인합니다.

placeholder는 Claude Code가 MCP를 시작할 때 확장하므로, 새 Claude 세션을 시작하는 프로세스에
host-control/credential store가 필요한 변수를 주입해야 합니다. `setup-mcp.sh`는 저장소의
`secrets.local.env`를 읽거나 export하지 않으며, manifest의 env에는 기본값 없는 `${VAR}`만
저장합니다. command에는 placeholder를 허용하지 않고, args에는 manifest가 승인한 정확한 경로
표현 `${FILESYSTEM_MCP_ROOT}`, `${JHW_NOTION_DIST}/index.js`만 허용합니다. credential
플래그·label, URL userinfo, credential-bearing 연결 URL/URI/DSN, capability URL과 현재
credential 환경값의 복제를 preflight에서 거부합니다. percent-encode나 JSON string escape로
감싼 carrier도 정규화해 검사합니다. Oracle의 target 없는 `user/password`는 정확한
`sqlplus`/`sql`/`sqlcl` 실행 문맥과 `--connect`/`--logon` 값 문맥에서만 차단해 일반
`owner/repository` 경로와 구분합니다. `curl`, MySQL/MariaDB, Redis, Mongo shell,
`sshpass`, `sqlcmd`/`osql`/`bcp`, Docker/Podman login의 credential-bearing 단축 옵션도 해당
명령 문맥에서만 판정하며, `sh` 계열의 `-c`, `env`/`sudo`, `timeout`/`nohup`/`nice`/`stdbuf`가
감싼 하위 명령까지 같은 검사를 재귀 적용합니다.
이 검사는 임의의 모든 CLI가 가진 비밀번호 문법을 추측하는 범용 secret detector가 아닙니다.
일반 구조 carrier, 현재 환경의 credential 값, 위에 열거한 명령 문맥을 보호 범위로 삼습니다.
manifest에 새 command나 새로운 args 형식을 추가할 때는 literal 차단 사례와 정상 대조군을
보안 회귀 테스트에 함께 추가해야 합니다. 현재 관리 manifest의 실제 값은 env placeholder로만
전달되며 설정 payload, argv, 출력에 들어가지 않습니다.

`secrets.local.env`는 아래 Slack 브릿지 전용입니다. Slack setup만 이 파일이 현재 사용자 소유
regular file, mode `0600`, hard-link 1개인지 검증하고 data-only assignment로 파싱합니다.
기존 파일에 MCP 항목이 남아 있으면 제거하고 credential store/host-control 환경으로 옮겨야 하며,
Slack 이외의 변수 이름은 setup 단계에서 거부됩니다.

공개 6개(brave/morph/pdf/sequentialthinking/filesystem/repowire) + 사내 4개(cts-email/cts-ta/jhw-notion/ssh-mcp).
notion(OAuth)·repowire-channel은 수동 확인이 필요합니다.
credential 노출 대응은 [MCP credential 운영 절차](docs/security/mcp-credential-incident-response.md)를 따릅니다.

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
