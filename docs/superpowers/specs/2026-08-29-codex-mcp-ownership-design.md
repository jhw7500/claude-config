# Codex MCP 소유권 추적과 고아 프로세스 안전 정리 설계

- 날짜: 2026-08-29
- 대상: GitHub issue #67
- 상태: 사용자 설계 승인 완료
- 1차 지원 범위: Linux의 Codex stdio MCP

## 1. 배경

2026-08-25 시점 진단에서는 다수의 Codex 관련 프로세스와 세션별 MCP
인스턴스가 관찰되었다. 일부는 고아 프로세스일 가능성이 있었지만, 현재
구성에는 Codex 세션과 MCP 프로세스를 재현 가능하게 연결하는 소유권 증거가
없다. 이름이나 command line만으로 종료하면 활성 세션, 느리게 종료 중인 서버,
의도적으로 공유된 서버, PID가 재사용된 다른 프로세스를 잘못 종료할 수 있다.

Codex의 공식 Hook 입력은 `session_id`와 `cwd`를 제공하고 `SessionStart` 및
`SessionEnd` 생명주기 이벤트를 지원한다. Codex stdio MCP 설정은 `command`,
`args`, `env`, `cwd` 등의 실행 정보를 가진다. 이 설계는 이 두 공식 표면을
supervisor wrapper로 연결한다.

참고:

- [Codex Hooks](https://learn.chatgpt.com/codex/hooks)
- [Codex MCP](https://developers.openai.com/codex/extend/mcp)

## 2. 목표

1. 관리 대상 Codex stdio MCP마다 정확한 세션 소유자 또는 명시적인 shared
   owner를 기록한다.
2. `active`, `shared`, `exiting`, `orphan`, `unknown`, `stubborn`, `gone` 상태와
   그 판정 증거를 재현 가능하게 출력한다.
3. PID 재사용, 느린 종료, 활성 세션, shared 서버에 대한 오종료를 막는다.
4. 진단과 cleanup 명령은 dry-run을 기본값으로 한다.
5. 명확한 소유자 종료, grace 경과, 동일 identity 재확인, pidfd 확보를 모두
   만족한 프로세스만 종료한다.
6. 정상 세션 종료와 강제 종료 모두에서 관리 대상 프로세스가 안전하게
   수렴하도록 한다.
7. 설치 전후 및 cleanup 전후의 프로세스 수, RSS, 처리 결과를 제공한다.
8. 수동 진단, cleanup, 강제 cleanup, rollback 절차를 운영 문서에 남긴다.

## 3. 비목표

- Claude Code MCP 설정이나 프로세스를 변경하지 않는다.
- HTTP MCP 서버를 감싸거나 종료하지 않는다.
- 프로세스 이름 기반 bulk kill을 제공하지 않는다.
- token 절약 효과를 직접 측정하거나 보장하지 않는다.
- 1차 구현에서 macOS나 Windows 프로세스 identity를 지원하지 않는다.
- 소유권 증거가 불충분한 프로세스를 추측으로 정리하지 않는다.

## 4. 결정 사항

- Codex stdio MCP만 1차 대상으로 한다.
- Linux `/proc`와 pidfd를 안전 종료의 필수 기반으로 사용한다.
- 설치는 `install.sh`에 묶지 않고 별도 opt-in 명령으로 제공한다.
- user-scope 설정을 기본 대상으로 하고 project-scope 설정은 사용자가 경로를
  명시한 경우에만 변경한다.
- 자동 cleanup은 `SIGTERM`까지만 허용한다. `SIGKILL`은 별도 수동 force
  절차에서만 허용한다.
- systemd user timer가 있으면 60초 주기로 scavenger를 실행하고, 기본 owner
  종료 grace는 120초로 한다.
- 이 기능의 로컬 ownership ledger는 Project Control Registry와 완전히
  별개이다. Project Control Claim, Registry writer, lifecycle lock을 사용하지
  않는다.

## 5. 전체 구조

```text
SessionStart Hook ───────→ session lease
                                │
Codex ─→ supervisor wrapper ─→ stdio MCP process tree
                │               │
                └───────────────┴→ local ownership ledger

SessionEnd Hook ─→ owner 종료 표시 ─┐
systemd timer ───────────────────────┼→ classify → revalidate → diagnose/cleanup
수동 CLI ────────────────────────────┘
```

### 5.1 opt-in 설정 도구

`scripts/setup-codex-mcp-ownership.sh`가 설정 변경을 계획, 적용, 검증,
rollback한다.

```text
scripts/setup-codex-mcp-ownership.sh
  --check
  --apply
  --rollback
  [--server NAME]
  [--project PATH]
  [--shared SCOPE:NAME]
```

### 5.2 lifecycle Hook

하나의 설치된 Hook 진입점이 `SessionStart`와 `SessionEnd` payload를 처리한다.

- `SessionStart`: session lease를 새로 만들거나 갱신하고 이전 orphan에 대한
  bounded opportunistic scan을 요청한다.
- `SessionEnd`: 해당 lease를 `ended`로 표시하고 비동기 scavenger 실행만
  요청한다.
- Hook은 cleanup grace를 기다리지 않는다. 공식 `SessionEnd` Hook의 실행 시간
  제한 안에서 원자적 상태 기록만 끝낸다.
- `compact`나 `resume`으로 같은 `session_id`의 `SessionStart`가 다시 발생하면
  중복 owner를 만들지 않고 lease를 갱신한다.

### 5.3 supervisor wrapper

각 관리 대상 stdio MCP의 `command`와 `args`를 안정된 사용자 설치 경로의
wrapper 호출로 교체한다. 원래 `env`, `cwd`, timeout, enabled, tool filter 등은
변경하지 않는다.

wrapper는 다음을 수행한다.

1. 서버명과 원래 executable/args를 받는다.
2. 자신의 Codex 조상 identity, cwd, spawn 시각을 기록한다.
3. MCP child를 전용 process group에서 실행하되 stdin/stdout/stderr 파일
   descriptor를 그대로 상속한다.
4. 정상 signal과 exit code를 중계한다.
5. child 및 발견 가능한 descendant의 identity를 ledger에 기록한다.
6. Hook과 시작 순서가 엇갈리면 bounded wait 후 reconciliation 대상으로 남긴다.
7. child 종료 후 결과를 기록하고 child exit code로 종료한다.

wrapper는 MCP protocol payload를 읽거나 기록하지 않는다.

### 5.4 scavenger와 CLI

설치된 사용자 명령은 다음 표면만 제공한다.

```text
codex-mcp-ownership audit [--json]
codex-mcp-ownership cleanup [--apply] [--force]
codex-mcp-ownership explain PID
```

- `audit`: 항상 읽기 전용이며 event log에도 기록하지 않는다.
- 인자 없는 `cleanup`: 후보와 증거만 출력하는 dry-run이다.
- `cleanup --apply`: 검증된 orphan에 자동 정책인 `SIGTERM`만 적용한다.
- `cleanup --apply --force`: stubborn 프로세스에 대한 수동 강제 경로다.
  TTY 확인 또는 직전 dry-run에서 발급한 evidence token을 요구한다. token은
  exact process identity와 판정 snapshot에 묶이고 짧은 시간 뒤 만료된다.
- `explain PID`: 해당 PID의 상태, owner, identity 비교 결과, 자동 처리 가능
  여부를 설명한다.

## 6. 로컬 상태와 개인정보 경계

기본 상태 루트는 다음과 같다.

```text
${XDG_STATE_HOME:-$HOME/.local/state}/claude-config/codex-mcp-ownership/
  sessions/
  processes/
  transactions/
  events.jsonl
  state.lock
```

- 디렉터리는 0700, 파일은 0600으로 강제한다.
- symlink, 소유자가 다른 파일, hard link, group/other 권한이 열린 파일은
  신뢰하지 않고 중단한다.
- session 파일명에는 untrusted `session_id`를 직접 쓰지 않고 SHA-256 digest를
  사용한다. 원문은 길이와 control character를 검증한 뒤 JSON 값으로만
  저장한다.
- workspace는 진단 정보로만 저장하며 파일 경로 구성에 사용하지 않는다.
- 환경변수 값, MCP protocol 데이터, transcript 내용, 전체 command line은
  저장하지 않는다.
- executable은 device/inode와 안전한 basename만 기록한다. args는 저장하지
  않는다.
- event log는 apply, Hook, timer가 만든 lifecycle mutation만 기록한다. `audit`와
  dry-run은 stdout으로만 결과를 반환하고 event log를 포함한 상태 파일에 쓰지
  않는다. log는 크기와 보존 기간을 제한하고 private mode를 유지한다.
- transaction backup은 rollback에 필요한 원본 설정 바이트를 포함하므로
  private mode로 보관하고, 적용 중인 최신 transaction과 제한된 이전본만
  유지한다.

## 7. 프로세스 identity

프로세스 identity는 다음 tuple이다.

```text
(boot_id, pid, /proc/<pid>/stat start_ticks, executable st_dev, executable st_ino)
```

- `boot_id`는 재부팅 전후 PID와 start tick 충돌을 분리한다.
- `/proc/<pid>/stat`의 start tick은 동일 부팅 내 PID 재사용을 분리한다.
- executable device/inode는 같은 PID와 start tick을 전제로 한 추가
  일관성 증거다.
- PPID도 PID 숫자만 저장하지 않고 같은 identity 형식으로 저장한다.
- 종료 직전에는 pidfd를 열고 위 identity를 다시 읽는다. pidfd를 얻은 뒤
  동일 identity가 아니면 signal을 보내지 않는다.
- pidfd가 없거나 권한 때문에 identity를 모두 읽지 못하면 해당 실행은
  진단 전용으로 강등한다.

## 8. 세션과 MCP 연결 알고리즘

### 8.1 session lease

`SessionStart` Hook은 다음 최소 정보를 기록한다.

- validated `session_id`
- `cwd`
- Hook event source
- wall-clock 및 monotonic/boot-relative 관측 시각
- Hook process에서 추적한 Codex 조상 identity chain
- lease 상태: `active` 또는 `ended`

`SessionEnd`가 있으면 명시적으로 `ended`가 된다. `SessionEnd`가 없더라도
기록된 Codex host identity가 사라지면 owner 소멸의 첫 관측 근거가 된다.
반대로 host가 살아 있고 `SessionEnd`가 없다면 stale 시간만으로 종료를
추측하지 않는다. Codex가 연결 해제 직후가 아니라 세션 종료 시 Hook을
호출할 수 있다는 공식 동작 때문에 안전 쪽으로 지연한다.

### 8.2 wrapper association

wrapper와 active lease를 다음 순서로 비교한다.

1. 정확히 일치하는 Codex 조상 identity가 있는가.
2. canonicalized cwd가 같은가.
3. `SessionStart`와 wrapper spawn이 허용된 시간 창 안에 있는가.
4. 위 조건을 만족하는 active lease가 정확히 하나인가.

정확히 하나일 때만 해당 `session_id`를 owner로 기록한다. 0개 또는 2개
이상이면 `unknown`으로 남긴다. Hook과 wrapper의 시작 순서가 바뀐 경우
reconciler가 같은 규칙으로 다시 판정한다. 시간 창이나 cwd만으로 임의의
최신 세션을 선택하지 않는다.

`--shared SCOPE:NAME`으로 명시된 서버는 session association을 강제하지 않고
scope-qualified shared owner로 기록한다. shared 프로세스는 자동 종료하지
않는다.

## 9. 상태 분류

| 상태 | 필수 증거 | 자동 동작 |
|---|---|---|
| `active` | owner lease가 active이고 host 및 MCP identity가 일치 | 없음 |
| `shared` | 설정에 scope-qualified shared owner가 명시됨 | 진단만 |
| `exiting` | 명시적 SessionEnd 또는 owner host 소멸을 처음 관측, grace 이내 | 대기 |
| `orphan` | 정확한 단일 owner, owner 종료, grace 경과, 동일 identity 재확인 | `--apply`에서 TERM 가능 |
| `unknown` | 미관리, 소유자 없음/복수, 증거 누락/불일치 | 진단만 |
| `stubborn` | TERM 후 shutdown grace를 지나도 동일 identity로 생존 | 수동 force만 |
| `gone` | 기록된 identity가 더 이상 존재하지 않음 | ledger 정리만 |

상태 전이는 ledger의 이전 상태만 믿지 않고 매번 live `/proc` 증거와 함께
재계산한다.

## 10. 종료 안전 규칙

자동 종료가 허용되려면 다음 조건을 모두 만족해야 한다.

1. 프로세스가 이 wrapper가 만든 관리 대상이어야 한다.
2. shared가 아닌 정확한 단일 owner가 기록되어야 한다.
3. `SessionEnd` 또는 exact owner host identity 소멸이 관측되어야 한다.
4. 첫 owner 종료 관측 시각이 ledger에 원자적으로 저장되어야 한다.
5. 기본 120초 grace가 경과해야 한다.
6. 독립적인 두 번째 scan에서 같은 process identity여야 한다.
7. signal 직전에 pidfd를 확보하고 identity를 다시 검증해야 한다.

자동 경로는 다음 순서로 동작한다.

1. wrapper가 살아 있으면 wrapper pidfd에 `SIGTERM`을 보내 정상 forwarding을
   우선한다.
2. 짧은 shutdown grace 뒤 남은 descendant를 다시 열거한다.
3. wrapper가 없거나 descendant가 남으면 각 프로세스의 ancestry, process
   group, identity를 개별 확인하고 각 pidfd에 `SIGTERM`만 보낸다.
4. 계속 살아 있는 exact identity는 `stubborn`으로 기록한다.

자동 `SIGKILL`, process name kill, argv substring kill, 검증 없는 process group
kill은 금지한다. 수동 force도 증거를 다시 출력하고 pidfd identity를
재검증한다. 새 프로세스가 같은 PID를 얻거나 group에 들어온 경우에는
대상에서 제외한다.

## 11. abnormal-exit scavenger

Linux user systemd가 사용 가능하면 opt-in apply가 다음 unit을 설치한다.

- 60초 간격의 user timer
- 짧게 실행되고 종료하는 oneshot cleanup service
- timer의 명령은 `cleanup --apply`이며 `--force`를 절대 포함하지 않음

unit은 적용 transaction의 마지막 단계에서만 활성화한다. systemd user
manager가 없으면 설치를 성공으로 위장하지 않고 `degraded`로 표시한다. 이
경우 다음 `SessionStart` Hook이 grace가 이미 지난 이전 orphan에 대해 bounded
`--apply` scan을 수행한다. Hook 안에서 sleep하거나 grace를 기다리지 않는다.

동시에 Hook, timer, 수동 CLI가 실행될 수 있으므로 짧은 상태 변경 구간만
kernel `flock`으로 직렬화한다. lock 파일이 남아 있어도 활성 writer를 뜻하지
않으며, process가 끝나면 커널 lock은 자동 해제된다. timeout 시 lock 파일을
삭제하거나 다른 실행을 takeover하지 않고 일시 실패로 종료한다.

## 12. 설정 적용

### 12.1 대상 범위

- 기본: `~/.codex/config.toml`의 stdio MCP
- 선택: `--project PATH`가 가리키는 `.codex/config.toml`
- 제외: HTTP MCP
- 별도 project 설정에서 발견됐지만 명시적으로 선택되지 않은 프로세스는
  `unknown(reason=unmanaged)`이며 자동 종료하지 않는다.

기존 저장소의 `manifest/mcp.json`도 user scope MCP를 기본으로 하며
`scripts/setup-mcp.sh`는 host별 MCP 설치를 opt-in으로 분리한다. 새 설치기도
같은 운영 철학을 따른다.

### 12.2 check

`--check`는 어떤 persistent file도 변경하지 않는다.

- Codex 설치 및 Hook 기능 상태 확인
- config private-file 검사
- stdio/HTTP/이미 관리됨/미관리 project 항목 분류
- 변경될 서버명과 필드명만 출력
- pidfd와 systemd user manager 지원 여부 출력
- command, args, env 값은 출력하지 않음

### 12.3 apply

apply 순서는 다음과 같다.

1. 모든 설정과 설치 경로를 private-file 규칙으로 preflight한다.
2. 원본 config 바이트, inode, hash를 읽고 transaction snapshot을 만든다.
3. wrapper, Hook, CLI를 안정된 사용자 lib/bin 경로에 원자적으로 설치한다.
4. `~/.codex/hooks.json`에 이 기능의 exact `SessionStart`/`SessionEnd` command
   Hook만 가산적으로 병합한다.
5. lossless TOML round-trip parser를 사용하는 전용 관리 환경에서 stdio
   항목의 `command`와 `args`만 변경한다.
6. lock 안에서 inode/hash CAS를 다시 확인하고 temp file, file `fsync`, atomic
   rename, directory `fsync` 순서로 반영한다.
7. Codex CLI JSON 조회를 capture하고 redacted semantic validation을 수행한다.
8. systemd unit을 쓰고 daemon-reload한 뒤 timer를 마지막에 활성화한다.

lossless parser는 apply 시에만 필요한 pinned dependency다. wrapper, Hook,
scavenger는 Python 표준 라이브러리만 사용한다. check는 stdlib TOML parser와
Codex JSON 조회만으로 계획을 만들며 dependency 설치나 cache 생성을 하지
않는다.

Hook, config, runtime, systemd 단계 중 하나라도 실패하면 현재 hash가 이
transaction의 예상값일 때만 이전 raw snapshot으로 되돌린다. concurrent user
변경이 감지되면 덮어쓰지 않고 충돌로 중단하며 복구 정보를 출력한다.

### 12.4 idempotency와 rollback

- 이미 exact wrapper 형태인 항목은 변경하지 않는다.
- 같은 Hook과 unit을 중복 추가하지 않는다.
- rollback은 현재 config/Hook이 이 transaction의 적용 결과와 정확히
  일치할 때만 해당 변경을 제거한다.
- 적용 후 사용자가 수정한 항목은 rollback이 덮어쓰지 않는다.
- rollback은 먼저 새 wrapper 생성을 막도록 config를 복원한다.
- 관리 중인 wrapper/MCP가 살아 있으면 Hook, timer, runtime 제거를 보류하고
  pending 상태를 출력한다. 프로세스가 정상 종료된 뒤 rollback을 다시
  실행하면 제거를 완료한다.
- 기존 실행 중인 Codex 세션과 MCP는 apply나 rollback 시 직접 종료하지
  않는다. 설정 효과는 다음 세션부터 적용된다.

## 13. 관측과 출력

human 및 JSON 출력은 다음 정보를 제공한다.

- 서버명과 config scope
- owner session 또는 shared owner
- workspace
- 상태와 reason code
- process identity의 비민감 요약
- owner 종료 근거와 grace deadline
- signal 가능 여부와 거부 사유
- 정리 전후 상태별 process count
- 정리 전후 `/proc/<pid>/status` 기반 총 RSS
- attempted, terminated, survived, skipped 결과

명령 출력과 event log에는 credential-bearing env, command, args, transcript
내용을 포함하지 않는다. JSON schema는 version 필드를 가져 이후 확장을
가능하게 한다.

## 14. 오류 처리

모든 불확실성은 fail-closed로 처리한다.

- owner 증거 누락 또는 복수 후보: `unknown`
- process identity 필드 누락: `unknown`
- pidfd 미지원/권한 실패: 진단 전용
- corrupt ledger: 원본을 private quarantine하고 signal 금지
- config symlink, 비공개 권한 위반, 소유자 불일치: 적용 중단
- config CAS 충돌: 사용자 변경 보존 후 적용 중단
- lock timeout: stale lock 삭제 없이 일시 실패
- process가 검사 사이에 사라짐: `gone`, signal 없음
- systemd 없음: degraded + SessionStart opportunistic mode
- Hook timeout/실패: Codex를 막지 않고 진단 이벤트 기록; 소유권 불확실
  프로세스는 unknown

## 15. 테스트 전략

기본 테스트는 실제 HOME, 실제 Codex config, 실제 systemd, 실제 사용자
프로세스를 건드리지 않는다.

### 15.1 단위 테스트

- `/proc/<pid>/stat`의 괄호가 포함된 comm과 start tick parsing
- boot ID, PID, start tick, executable inode 비교
- session ID hashing과 control character 거부
- atomic JSON write, corrupt state quarantine, private mode 검증
- Hook payload의 startup/resume/compact/end idempotency
- unique/zero/multiple lease association
- shared owner routing
- 상태 전이와 grace deadline
- pidfd backend 호출 조건
- event redaction과 RSS aggregation

### 15.2 가짜 `/proc`와 signal backend

임시 `/proc` fixture와 호출을 기록하는 signal backend로 다음을 검증한다.

| 시나리오 | 필수 결과 |
|---|---|
| 정상 세션 종료 | wrapper/MCP 종료 후 `gone`으로 수렴 |
| owner 강제 종료 | 첫 scan `exiting`, grace 후 두 번째 scan에서만 `orphan` |
| PID 재사용 | start tick 또는 executable identity 변경 시 signal 0회 |
| 느린 정상 종료 | grace 안에서 signal 0회 |
| active session | 반복 scan에도 signal 0회 |
| explicit shared | 모든 자동 경로에서 signal 0회 |
| 동일 cwd의 복수 세션 | `unknown`, signal 0회 |
| Hook/wrapper 순서 역전 | reconciliation 후 unique owner 연결 |
| dry-run | ledger, event log, 설정, signal mutation 0건 |
| TERM 무시 | `stubborn`, 자동 KILL 0회 |
| pidfd 미지원 | 진단 전용, signal 0회 |
| `/proc` 권한 부족 | `unknown`, signal 0회 |
| process 중간 소멸 | `gone`, signal 0회 |

### 15.3 설정 테스트

임시 HOME의 config와 Hook 파일로 다음을 검증한다.

- comment, ordering, quoted server names, multiline args 보존
- env, cwd, timeout, enabled, tool filter 보존
- HTTP server 제외
- user scope 기본 및 explicit project scope
- 서버 단위 apply와 shared 지정
- fresh apply, identical rerun, rollback
- apply 뒤 user edit가 있으면 CAS conflict
- symlink, FIFO, hard link, 공개 권한 거부
- backup과 temp file의 0600, directory의 0700
- 출력에 semantic secret이 포함되지 않음
- 중간 단계별 failure injection에서 원본 복원
- systemd 미지원 시 degraded 결과

### 15.4 Linux 통합 테스트

harmless fake stdio MCP fixture를 실제 child process로 실행해 다음을 검증한다.

- stdin/stdout/stderr 투명성
- signal forwarding과 child exit code 전달
- descendant process 추적
- 정상 owner 종료 시 최종 managed process count 0
- 강제 owner 종료 후 grace와 recheck를 거쳐 TERM-responsive fixture가 0으로
  수렴
- stubborn fixture는 살아 있고 자동 KILL이 발생하지 않음
- cleanup 전후 process count와 RSS가 실제 `/proc` 값과 일치

실제 Codex 설치를 사용하는 smoke test는 별도 opt-in으로 두고 CI 기본 경로에
포함하지 않는다.

### 15.5 완료 판정 gate

issue를 완료로 판정하기 전 controlled live smoke에서 실행된 모든 관리 대상
stdio MCP가 정확한 session owner 또는 explicit shared owner로 분류되어야 한다.
하나라도 `unknown`이면 안전 정책에 따라 signal은 보내지 않지만, 소유권 매핑
완료 조건은 충족하지 않은 것으로 보고 원인을 해결할 때까지 issue를 닫지
않는다. PID reuse, active, slow-exit, shared fixture에서 signal 호출이 0회라는
증거도 함께 보존한다.

## 16. 운영 문서

운영 문서는 다음 절차를 포함한다.

1. 설치 전 `--check` 해석
2. apply와 새 Codex 세션에서의 활성화 확인
3. `audit` 및 `explain PID`로 수동 진단
4. dry-run cleanup 결과 해석
5. `cleanup --apply`와 자동 timer 확인
6. stubborn 프로세스의 evidence token 기반 수동 force
7. systemd degraded mode 진단
8. config conflict와 corrupt ledger 복구
9. 부분 및 최종 rollback
10. before/after process count와 RSS 보고

## 17. 완료 조건 추적

| issue #67 요구 | 설계 대응 |
|---|---|
| 세션 또는 shared owner 매핑 | Hook lease + wrapper association + explicit shared |
| unknown auto-kill 금지 | 상태 모델과 fail-closed 종료 gate |
| PID reuse 방어 | boot ID + start tick + executable identity + pidfd |
| dry-run 무변경 | CLI 기본값 및 mutation-free 테스트 |
| grace + 동일 identity 재확인 | 120초 grace + 독립 scan + signal 직전 recheck |
| 정상/강제 종료 수렴 | wrapper forwarding + timer scavenger + 통합 테스트 |
| 운영 진단/cleanup/rollback | opt-in setup, CLI, 운영 문서 |
| count/RSS/cleanup 결과 | audit와 structured event schema |

## 18. 검토한 대안

### Hook process diff만 사용

설정 변경은 작지만 Hook 이전에 시작된 MCP, reparented child, shared host,
동일 cwd의 동시 세션을 정확히 연결하지 못한다. 완료 조건의 소유권 증명에
부족해 제외한다.

### 외부 `/proc` scanner만 사용

실행 파일명, cwd, ancestry는 후보 증거일 뿐 session ID를 제공하지 않는다.
진단 도구는 될 수 있지만 안전한 자동 cleanup의 권한 근거가 될 수 없어
제외한다.

### Codex와 Claude MCP를 동시에 지원

두 제품의 config와 Hook 계약이 다르고 현재 저장소의 Claude MCP sync와 Codex
config가 분리되어 있다. 공통 schema를 먼저 만들면 #67의 위험한 cleanup
경계보다 scope가 커지므로 Claude 지원은 후속 작업으로 남긴다.
