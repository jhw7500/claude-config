# jhw-control host launcher 구현 계획

> 구현은 `superpowers:test-driven-development` 순서로 진행한다. 각 동작은 실패 테스트를 먼저 확인하고 최소 구현으로 통과시킨다.

**Goal:** clean shell에서 사용자가 직접 `source`·`export`·credential 조회를 하지 않아도 secure-store-only launcher를 통해 안전하게 Project Control Task 등록 절차를 실행한다.

**Architecture:** 단일 Python supervisor가 고정 위치의 non-secret config를 same-FD로 검증·파싱하고, 격리된 Secret Service helper와 GitHub CLI의 원자적 JSON 결과에서 세 credential을 얻는다. 별도 `unlock` 경로는 검증된 user bus의 기존 Secret Service owner만 직접 해제하고 config나 credential을 먼저 읽지 않는다. 명시적으로 만든 child 환경에서 resolve한 Node와 `jhw-control`만 실행한다. `task start` 전에는 같은 환경으로 preflight를 강제하며, bounded 출력에서 secret·보호 경로를 발견하면 전체 출력을 폐기한다.

**Tech stack:** Python 3.10 표준 라이브러리, Bash installer, pytest. 외부 Python dependency는 추가하지 않는다. 설계: `docs/superpowers/specs/2026-08-26-jhw-control-host-design.md`.

## 전역 불변식

- legacy token 파일, `secrets.local.env`, ambient `GH_*`, `.env`, `.bashrc`로 fallback하지 않는다.
- config는 exact allowlist이며 한 FD에서 `O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`, regular/current UID/0600/nlink=1/byte bound를 검증한다.
- Project와 Notion token은 `PYTHON_KEYRING_BACKEND`로 선택을 강제하고 실제 Secret Service class identity를 확인한 isolated Python helper 결과에서만 받는다.
- Repo token은 고정 `gh`의 한 `auth status --hostname github.com --active --show-token --json hosts` 결과에서 token과 `tokenSource=keyring`을 함께 검증한다.
- 일반 provider stdin은 닫고, 환경은 allowlist로 새로 만들며, timeout과 출력 상한을 둔다. `unlock`만 검증된 TTY에서 echo-off 입력을 허용하며 child와 parent가 모든 종료 경로에서 terminal state를 복구하고 읽지 않은 입력 큐를 폐기한다.
- child에는 config 11개와 `GH_PROJECT_TOKEN`, `GH_REPO_TOKEN`, `NOTION_API_KEY`만 추가한다. PATH는 고정하고 Node/CLI executable·ancestor와 PATH의 모든 검색 디렉터리를 credential 조회 전에 확인한다. private-to-current-UID group write만 허용하고 POSIX access/default ACL과 world write는 거부하며 PATH entry 자체에는 sticky world-write도 허용하지 않는다.
- 허용 invocation은 `--contract`, `unlock`, `preflight`, `portfolio status ...`, `task start ...`뿐이다.
- `task start` hidden preflight 실패 시 그 안전한 stdout/stderr/exit를 그대로 반환하고 mutation을 실행하지 않는다. 성공 preflight 출력은 폐기한다.
- 하위 출력은 12 KiB, 단일 duplicate-free JSON, command별 closed success/error schema와 code/exit 조합, 성공 stdout-only/실패 stderr-only 계약을 검증해 canonical 재직렬화한다. Portfolio 문자열은 downstream의 UTF-16 문자 길이와 GitHub ID byte/slug 경계를 그대로 적용한다. `task start`는 명시된 `--task`/`--project`/`--repo-id`에 응답을 묶고 검증된 불변 좌표와 optional Handoff만 투영한다. 다른 호스트의 정상 선점 충돌은 bounded host 입력을 검증하되 projection에서 host를 제거한다. token 또는 config/credential-store/입력 checkout path가 raw·양 stream 조합·decoded JSON·padded/unpadded base64·대소문자를 정규화한 hex/percent escape·표준 URL quote에 섞이면 고정 `SENSITIVE_OUTPUT_REJECTED`만 반환한다.
- 자동 retry, credential migration/update, claim takeover를 하지 않는다.
- launcher 자체는 고정 `#!/usr/bin/python3 -I`로 시작한다. production에는 provider/home/path를 바꾸는 env·flag·config·hidden test override를 두지 않는다.

## Task 1: launcher 보안 계약을 실패 테스트로 고정

**Files**

- Create: `tests/test_jhw_control_host.py`

1. module loader와 fake bounded-runner를 만든다. fake는 호출 argv/env를 메모리에만 기록하고 provider별 bytes 결과를 반환한다.
2. launcher 첫 줄이 `#!/usr/bin/python3 -I`인지 고정하고, 실제 executable을 `env -i`와 poisoned PATH/PYTHONPATH/PYTHONHOME로 실행해 `--contract` 성공 및 canary 미실행을 확인한다.
3. `--contract`가 config/credential 접근 없이 path-free v2 JSON을 반환하는 테스트를 작성한다.
4. config의 정상 literal 값과 missing/0644/symlink/hardlink/owner mismatch/oversize/NUL/invalid UTF-8/duplicate/unknown/shell syntax 거부를 작성한다. same-FD path 교체 테스트도 포함한다.
5. keyring helper가 Project+Notion 값을 함께 요구하고 backend/missing/locked/timeout/oversize를 고정 오류로 바꾸며 provider stderr를 노출하지 않는 테스트를 작성한다.
6. GitHub JSON은 `hosts`가 정확히 github.com 하나, active entry 하나이며 `state=success`, owner 일치, `tokenSource=keyring`, bounded token일 때만 허용하는 테스트를 작성한다. ambient token과 `GH_CONFIG_DIR` poison이 provider env에 없는지도 확인한다.
7. Project/Repo 동일 token 거부, child-only 세 credential, fixed PATH, dangerous ambient env 제거를 작성한다.
8. allowlist 외 invocation은 provider 전에 exit 2, hidden preflight 실패는 task 미실행, 성공은 task 1회 실행, 안전한 output/exit exact preservation을 작성한다.
9. token/config path canary와 output bound/timeout이 고정 JSON으로 fail closed하는 테스트를 작성한다.
10. fresh `/usr/bin/python3 -I -c` subprocess가 launcher를 exact path로 load하고 function port에만 fake runner/home을 주입해 provider→hidden preflight→task 결과를 검증한다. production surface에는 이 port를 노출하지 않는다.
11. 실행: `rtk python3 -m pytest -q tests/test_jhw_control_host.py`. 새 module 부재로 실패하는 RED를 확인한다.

## Task 2: 최소 Python supervisor 구현

**Files**

- Create: `scripts/jhw-control-host.py` (mode 100755)

1. shebang을 고정 `/usr/bin/python3 -I`로 두고 stable error/result dataclass와 JSON emitter를 구현한다. 예외 메시지·subprocess stderr·실제 path는 emitter 입력으로 받지 않는다.
2. `read_control_config()`와 strict literal parser를 구현한다. required key는 현재 control 계약의 11개로 고정한다.
3. POSIX selector 기반 `run_bounded()`를 구현한다. stdin=`DEVNULL`, new session, timeout/overflow와 `Popen` 이후 모든 capture 예외에서 process group 종료·wait, stdout/stderr 별도 bounded capture를 보장한다.
4. 실제 passwd home에서 provider/child 기본 환경을 새로 만들고, safe locale 및 검증된 user-owned Unix `SSH_AUTH_SOCK`만 선택적으로 전달한다.
5. `python -I -c <helper>`에 고정 backend selector만 주입하고 exact Secret Service class와 Project·Notion token을 한 결과에서 검증한다.
6. 고정 PATH의 모든 search directory와 ancestor를 먼저 검증한 뒤 `gh`를 resolve하고 원자적 auth-status JSON을 검증해 Repo token을 얻는다. missing/insecure 복구는 secure-storage 기본 interactive `gh auth login`으로 안내한다.
7. 고정 `$HOME/.local/bin/node` 및 `$HOME/.local/bin/jhw-control` target을 resolve·검증하고 child argv/env를 만든다.
8. invocation allowlist, hidden preflight, strict output projection, duplicate JSON 거부, 양 stream/decoded JSON canary rejection을 구현한다.
9. 매 단계 후 Task 1의 해당 테스트를 실행하고 모두 GREEN으로 만든다.
10. `unlock`을 config/provider 전 조기 분기하고 canonical runtime/bus, fixed owner, exact private signature, login collection, post-unlock `Locked=false`를 검증한다. 비대화형·비지원·거부·timeout은 path-free 고정 오류로 끝내고 daemon replacement나 public prompt fallback은 두지 않는다.

## Task 3: installer 배포를 TDD로 연결

**Files**

- Modify: `tests/test_installer_private_config.py`
- Modify: `install.sh`

1. 임시 HOME 설치 후 `$HOME/.local/bin/jhw-control-host`가 mode `0500` 전용 사본을 가리키는지, 재설치가 멱등인지, 기존 실파일은 `link_safely` 계약으로 백업되는지 실패 테스트를 추가한다. world-writable/symlinked ancestor와 symlink-to-directory target 거부도 고정한다.
2. `install.sh`가 `$HOME`부터 원본·해결 경로 체인을 사전 검증하고 `.local/bin`, `.local/lib`, 전용 디렉터리를 `0700`으로 만든 뒤 임시 파일을 `mv -T` atomic replace하고 bin link를 갱신하게 한다. 설치 중 launcher/provider/preflight는 실행하지 않는다.
3. 실행: `rtk python3 -m pytest -q tests/test_installer_private_config.py tests/test_jhw_control_host.py`.

## Task 4: 호출·provision 문서와 지침 갱신

**Files**

- Modify: `tests/test_jhw_control_host.py`
- Modify: `claude-md/global-guidance.md`
- Modify: `README.md`

1. Task 지침에서 raw `source`/`export`와 raw `jhw-control` 호출이 사라지고 절대 launcher 경로를 사용하는 실패 테스트를 추가한다.
2. global guidance의 Task 등록 계약을 `jhw-control-host preflight` → `portfolio status` → `task start`로 바꾸되 명시적 사용자 요청/승인과 좌표 비추측 규칙을 유지한다.
3. README에 `jhw-control-host unlock` 단일 잠금 해제, secure-store-only provider 세 개, 명시적 provision/update 명령, `--contract`, 정상 호출 예, fail-closed 오류, installer는 조회·migration을 하지 않는다는 점을 기록한다.
4. `$jhw-task` 정본은 jhw-notion 저장소라는 호환성 경계와, #28을 완료하기 전에 같은 delivery sequence에서 별도 Formal Task로 반영한다는 점을 기록한다.

## Task 5: jhw-notion Claude/Codex 진입점 연동

1. jhw-notion #74를 #28과 연결된 별도 Issue로 등록한다. claude-config Claim으로 jhw-notion 파일을 수정하지 않는다.
2. claude-config 변경을 검증·handoff한 뒤 jhw-notion Issue를 별도 Formal Task/worktree로 claim한다.
3. canonical Claude `$jhw-task` 문서와 이를 소비하는 Codex 진입점이 raw config sourcing/raw `jhw-control` 대신 절대 `jhw-control-host`를 사용하도록 TDD로 변경한다.
4. preflight → portfolio coordinate 확인 → task start 순서, 명시적 사용자 승인, preflight 실패 후 mutation 없음, 성공 식별자 반환을 두 runtime 계약 테스트로 고정한다.
5. 두 저장소 변경을 연결하고 통합 증거가 나오기 전에는 claude-config #28을 완료/close하지 않는다.

## Task 6: 검증과 독립 리뷰

1. `rtk python3 -m pytest -q tests/test_jhw_control_host.py tests/test_installer_private_config.py`.
2. `rtk python3 -m pytest -q tests`.
3. CI lock 환경에서 `rtk python3 -m pytest -q`를 실행한다. 기존 로컬 환경의 `slack_bolt` 수집 실패는 dependency 설치 후 재검증한다.
4. `rtk shellcheck -x -s bash -S error install.sh`, `rtk python3 -m py_compile scripts/jhw-control-host.py`, `rtk git diff --check`.
5. poisoned env/fake provider/canary 테스트 로그에 실제 credential 또는 보호 path가 없는지 검색한다.
6. 실제 설치 경로를 clean shell에서 실행한다. store가 미준비면 안정적인 no-mutation 실패만 확인하고, 모든 credential을 사용자가 명시적으로 provision한 뒤 preflight와 test Task 등록이 성공하는 positive smoke를 반드시 남긴다.
7. 독립 security/code review에서 모든 전역 불변식을 재검증한다. CRITICAL/HIGH가 남거나 secret/path canary가 한 번이라도 출력되면 전달·merge를 중단한다.
