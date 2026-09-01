# Claude/Codex 전역 Task 넛지와 포트폴리오 분류 설계

- 날짜: 2026-08-31
- 대상: GitHub Issue #22
- 상태: 대화 설계 승인 완료, 문서 검토 대기
- 기준 커밋: `9cf4740e3383202c92b3d901bfbaa0699a55fded`
- 선행 계약: GitHub Issue #28의 secure-store-only `jhw-control-host`

## 1. 배경

현재 `task-nudge.sh`는 Claude Code의 `PreToolUse(Edit|Write|NotebookEdit)`에서
세션 첫 프로젝트 파일 수정을 감지하고 `[TASK-NUDGE]` 문구를 한 번 출력한다.
그러나 저장소가 Project Control 포트폴리오에 등록됐는지 확인하는 결정적 절차는
없고, Codex에는 같은 정책을 전달하는 런타임 진입점도 없다. 전역 지침은 이미
등록된 저장소라는 전제를 모델이 판단하게 하므로 등록·미등록·조회 실패를 서로
다르게 처리할 수 없다.

이 설계는 Claude와 Codex가 같은 분류 엔진과 정책을 사용하게 한다. 훅이 저장소
등록 여부를 읽기 전용으로 판정하고, 에이전트가 대화 증거를 이용해 즉시 작업,
향후 backlog, 반복·다중 세션 작업 또는 제외 작업을 구분한다. Issue 생성,
Project/Repository 등록, Task 시작은 계속 별도의 명시적 사용자 승인 뒤에만
수행한다.

Codex는 native `PreToolUse` 훅을 기본 경로로 사용한다. 훅이 다루지 않는 Bash나
특수 도구의 변경에는 전역 AGENTS 관리 블록을 fallback으로 설치한다. 두 경로는
동일한 공유 분류기를 사용하며 lifecycle mutation을 직접 실행하지 않는다.

## 2. 목표

1. Claude와 Codex의 첫 실질 변경 후보에서 같은 Task 넛지 의미를 세션당 최대
   한 번 제공한다.
2. exact GitHub repository slug와 `jhw-control-host portfolio status`의 검증된
   projection만으로 등록 여부를 `registered`, `unregistered`, `unknown` 중
   하나로 결정한다.
3. 조회 실패나 불완전한 부재 증거를 미등록으로 오인하지 않고 정책상 fail
   closed한다.
4. 등록 상태와 작업 성격에 따라 Formal Issue Task, Temporary Task, Task 없음,
   GitHub Issue만 생성, Project/Repository 등록 중 올바른 제안을 고르게 한다.
5. 제안과 모든 조회는 읽기 전용으로 유지하고, 서로 다른 mutation 승인 단계를
   합치지 않는다.
6. 기존 Claude 동작과 사용자 설정을 보존하면서 Codex 훅과 AGENTS fallback을
   멱등 설치한다.
7. 런타임 입력, 분류, 동시성, 설치 병합 및 네 가지 핵심 정책 조합을 임시
   환경의 자동 테스트로 고정한다.

## 3. 비목표

- 훅이 자연어 대화 전체를 해석해 작업 성격을 자동 확정하지 않는다.
- Bash command를 shell parser나 정규식으로 mutation 여부까지 추측하지 않는다.
- 훅이 도구 호출을 permission decision으로 거부하지 않는다. `unknown`의
  fail-closed는 에이전트가 후속 변경을 멈춰야 하는 정책 계약이다.
- Issue 생성, Project/Repository 등록, Task start/finish 또는 takeover를 자동
  실행하지 않는다.
- 포트폴리오 pagination을 직접 조합하거나 Project/Repository ID를 추측하지
  않는다.
- `jhw-control-host`의 credential, command 또는 output 계약을 확장하지 않는다.
- 기존의 모든 Claude/Codex 훅을 새 installer나 새 설정 형식으로 이관하지 않는다.
- 실제 사용자 HOME에 설치하거나 Codex hook trust를 자동 승인하는 작업은 테스트
  범위에 포함하지 않는다.

## 4. 검토한 접근

### 4.1 공유 Python 엔진과 얇은 런타임 어댑터

저장소 판정, 상태 파일, 정책 메시지를 한 Python 엔진에 두고 Claude/Codex
어댑터는 입력과 출력 형식만 변환한다. 정책 drift를 가장 작게 만들고 순수 함수와
임시 HOME으로 대부분을 검증할 수 있으므로 이 접근을 채택한다.

### 4.2 Claude shell과 Codex Python을 별도로 구현

기존 shell 훅을 거의 바꾸지 않는 장점이 있지만 portfolio schema, fail-closed,
상태 원자성 및 문구가 두 구현에서 독립적으로 변한다. 같은 의미라는 완료 기준을
장기적으로 보장하기 어려워 채택하지 않는다.

### 4.3 Codex plugin으로 배포

hook과 instruction을 하나의 plugin으로 묶을 수 있지만 Claude 설치와 별도 배포
수명주기가 생기고 Issue #22 범위를 plugin packaging까지 넓힌다. 현재는 전역
Codex hook과 AGENTS 병합만으로 요구를 충족하므로 채택하지 않는다.

## 5. 구속력 있는 경계

### 5.1 기계 판정과 의미 판정의 분리

공유 엔진이 결정하는 값은 다음뿐이다.

- 입력이 첫 실질 변경 후보인지
- canonical GitHub repository slug
- 포트폴리오 등록 상태
- 해당 runtime/session에 정상 안내를 이미 출력했는지

즉시 작업인지 backlog인지, Formal/Temporary Task가 필요한지와 단순 작업인지의
판정은 대화 맥락을 가진 에이전트가 수행한다. 훅 출력은 등록 상태와 구속력 있는
결정표를 제공하고, 에이전트가 그 표에서 하나의 추천안을 사용자에게 제시하게
한다. 엔진은 볼 수 없는 대화 신호를 추측하지 않는다.

### 5.2 읽기와 변경의 경계

자동 경로가 실행할 수 있는 Project Control 명령은 정확히 다음 하나다. 저장소
identity를 얻기 위한 read-only Git 호출은 6.2절의 `root`와 `remote.origin.url`
조회로 제한한다.

```text
$HOME/.local/bin/jhw-control-host portfolio status
```

엔진은 credential 파일을 읽거나 환경 변수를 조립하지 않고 raw
`jhw-control`을 실행하지 않는다. 조회 결과로 lifecycle 명령을 연쇄 실행하지
않는다. 다음 변경은 각각 별도의 사용자 승인이 필요하다.

1. GitHub Issue 생성
2. Project/Repository 등록
3. Formal 또는 Temporary Task 시작

앞 단계의 승인은 뒤 단계의 승인이 아니다. 향후 backlog에서는 Issue 생성만
제안하며 Task를 선점하지 않는다.

### 5.3 런타임 간 의미 동등성

Claude는 기존 호환성을 위해 평문 `[TASK-NUDGE]`를 stdout으로 받는다. Codex
native hook은 같은 본문을 `systemMessage` JSON에 담는다. 어댑터별 문법은 달라도
repository status, 근거, 선택 순서, 승인 경계와 오류 행동은 같아야 한다.

## 6. 구성요소

### 6.1 공통 이벤트

어댑터는 runtime payload를 다음 내부 값으로 정규화한다.

```text
runtime: claude | codex
session_id: opaque string
cwd: absolute candidate working directory
tool_name: normalized tool name
target_paths: zero or more candidate paths
```

Session ID는 경로 조각으로 사용하지 않고 SHA-256 digest로 상태 키를 만든다.
따라서 공식 런타임이 허용하는 opaque ID를 보존하면서 path traversal을 만들지
않는다. Session ID 또는 cwd가 없거나 잘못됐으면 완료 상태를 만들지 않고
`unknown` 안내를 반환한다.

Claude 어댑터는 `tool_input.file_path` 또는 `notebook_path`를 읽는다. Codex
어댑터는 `apply_patch`, `Edit`, `Write`만 받는다. `apply_patch`는 patch header의
대상 경로를 보수적으로 추출한다. 추출에 실패하거나 여러 경로 중 하나라도
프로젝트 경로일 수 있으면 cwd의 repository를 변경 후보로 취급한다.

다음 명백한 지원 파일은 완료 상태를 소비하지 않고 건너뛴다.

- `/tmp` 아래 scratch 파일
- 사용자 `~/.claude` 또는 `~/.codex` 설정
- `.omc` 세션 상태
- `memory/*.md`
- `HANDOFF*.md`

프로젝트 안의 모든 Markdown이나 configuration 파일을 경로만으로 제외하지
않는다. 단순 변경인지 대규모 문서·설정 작업인지는 에이전트가 대화 증거로
판정한다. Subagent 여부가 runtime payload에서 결정적으로 제공되면 건너뛰며,
그렇지 않으면 훅 메시지가 subagent에게 안내를 무시하도록 지시한다.

### 6.2 공유 분류 엔진

공유 엔진은 다음 순서만 orchestration한다.

```text
runtime event
  -> 입력 정규화
  -> 결정적 제외 규칙
  -> git root와 remote slug 확인
  -> secure launcher portfolio 조회
  -> 등록 상태 판정
  -> 세션 완료 상태 atomic create
  -> runtime adapter 출력
```

Git 호출은 읽기 전용이고 입력 cwd를 벗어나 임의 저장소를 탐색하지 않는다.
Identity source는 해당 root의 fetch용 `remote.origin.url` 하나뿐이다. `origin`이
없다고 다른 remote를 임의 선택하지 않는다. Remote는 `github.com`의 HTTPS 또는
SSH 표기에서 정확한 `owner/repository`를 추출하고 `.git` suffix와 GitHub의
case-insensitive 비교만 정규화한다. URL에 credential, 제어 문자, 추가 path
component가 있거나 지원하지 않는 host이면 slug를 추측하지 않고 `unknown`이다.

Launcher child에는 빈 stdin, 15초 timeout 및 bounded capture를 적용한다.
Timeout이면 child를 종료하고 회수한다. Launcher가 이미 제한하는 12 KiB를
engine에서도 상한으로 검증한다. Nonzero exit, timeout, invalid JSON, 중복 key,
예상하지 않은 top-level shape는 모두 `unknown`이다. Raw stdout/stderr는 훅
메시지나 로그로 전달하지 않는다.

### 6.3 포트폴리오 판정

Engine은 projected `repositories[].slug`, `truncated`, `total_items`와 optional
pagination indicator를 schema 검증한다. 결과는 다음 표로만 결정한다.

| 증거 | 결과 |
| --- | --- |
| 정확히 정규화된 slug가 결과에 존재 | `registered` |
| slug가 없고 결과가 완전함 | `unregistered` |
| slug가 없고 `truncated` 또는 pagination 잔여가 있음 | `unknown` |
| launcher/schema/slug 검증 실패 | `unknown` |

잘린 결과에서 slug가 발견되면 존재 증거는 완전하므로 `registered`다. 부재는
완전한 결과에서만 증명한다. Engine은 다음 page ID를 사용해 직접 pagination하지
않으며 누락된 Project/Repository ID를 조립하지 않는다.

### 6.4 세션당 한 번 상태

상태 키는 `runtime + SHA-256(session_id)`이며 repository별 키를 추가하지 않는다.
이는 한 Codex 또는 Claude 세션에서 여러 checkout을 방문하더라도 정상 넛지는
최대 한 번이라는 계약을 유지한다.

상태 디렉터리는 안전한 `XDG_RUNTIME_DIR`가 있으면 그 아래를, 아니면 UID를
포함한 system temporary directory 아래 private `0700` 디렉터리를 사용한다.
Unsafe `XDG_RUNTIME_DIR`는 무시하고 fallback을 검사한다. 기존 fallback 경로가
symlink이거나 현재 UID 소유가 아니거나 group/world writable이면 상태를 만들지
않고 `NUDGE_STATE_UNAVAILABLE`로 fail closed한다. 정상 판정 뒤 adapter 출력
직전에 `O_CREAT|O_EXCL`과 mode `0600`으로 완료 marker를 만든 호출만 메시지를
출력한다. 경쟁 호출은 marker 존재를 보고 조용히 종료하므로 중복 안내가 없다.

`unknown`과 결정적 제외는 marker를 만들지 않는다. 따라서 portfolio가 복구된
뒤 다음 변경 후보가 정상 안내를 받을 수 있다. `unknown`이 계속되면 매 후보에서
재조회하고 fail-closed 안내를 반복할 수 있다. Atomic marker 생성과 stdout 사이의
process crash는 외부 출력과 파일을 하나의 transaction으로 만들 수 없으므로
at-most-once를 우선한다. 이 드문 누락은 AGENTS fallback이 보완한다.

### 6.5 AGENTS fallback

Codex hook이 신뢰되지 않았거나 Bash·특수 도구처럼 matcher 밖의 변경을 하려는
경우, 활성 전역 AGENTS의 관리 블록이 다음 계약을 적용한다.

1. 현재 세션에서 `[TASK-NUDGE]`를 받았거나 Task 선택을 이미 끝냈으면 반복하지
   않는다.
2. 첫 실질 변경 전에 설치된 engine의 stateless manual-check entrypoint를 한 번
   호출한다.
3. Manual check는 같은 slug와 portfolio 판정 코드를 사용하지만 hook marker를
   만들지 않는다.
4. 반환된 등록 상태에 아래 정책표를 적용하고 필요한 제안에서 멈춘다.
5. `unknown`이면 분류가 복구될 때까지 후속 실질 변경을 하지 않는다.

Manual check는 stdout에 `repository_slug`, `registration_status`와 optional bounded
`reason`만 포함한 단일 JSON object를 반환하고 stderr에는 raw child output을
쓰지 않는다. Fallback block은 이 command와 7절의 다섯 단계 결정 순서를 짧지만
완전한 형태로 직접 포함한다. Native hook, manual check와 fallback block은 같은
policy fixture로 계약 테스트하여 이 불가피한 문구 중복의 drift를 탐지한다.

## 7. 정책 결정표

에이전트는 다음 순서로 하나의 결과를 선택한다. 앞의 조건이 뒤의 조건보다
우선한다.

1. **이미 결정됨 또는 제외 작업**: 이 세션에서 Task start를 했거나 사용자가
   Task 없이 진행을 선택했거나, 조회·Q&A·단순 문서/설정·subagent 작업이면
   Task 없이 진행한다.
2. **향후 backlog**: 등록 여부와 무관하게 GitHub Issue 생성만 제안한다.
   Issue를 만들기 전 승인을 받고 Task/Claim은 시작하지 않는다.
3. **등록 저장소의 즉시 작업**:
   - 기존 Issue가 있거나 다단계·검토·다중 세션 증거가 있으면 Formal Issue
     Task를 추천한다.
   - 기존 Issue가 없고 현재 세션에서 끝낼 수 있는 제한적 실질 작업이면
     Temporary Task를 추천한다.
   - Task의 조정 비용보다 작은 작업이면 Task 없음을 추천한다.
4. **미등록 저장소의 즉시 작업**:
   - 반복·다중 세션 증거가 있으면 Project/Repository 등록만 먼저 제안한다.
     등록 승인 자체로 Task를 시작하지 않는다.
   - 그런 증거가 없으면 Task 없이 진행한다.
5. **등록 상태 unknown**: 등록 또는 미등록으로 가정하지 않는다. 에이전트는
   현재 변경을 중단하고 bounded 오류 코드와 복구 필요만 사용자에게 알린다.

반복·다중 세션 작업은 다음 중 하나의 긍정 증거가 있을 때만 인정한다.

- 사용자가 장기, 반복 또는 여러 세션에 걸친 작업임을 명시했다.
- 기존 GitHub Issue, 승인된 계획 또는 Handoff가 있다.
- 여러 구현 단계와 검증이 필요한 아키텍처 작업이다.

단순히 repository 안에 있거나 파일 수가 여러 개라는 이유만으로 반복 작업이라
판정하지 않는다.

## 8. 출력과 오류 계약

정상 훅 메시지는 `[TASK-NUDGE]`, 안전한 repository slug, `registered` 또는
`unregistered`, 위 정책을 적용하라는 지시와 세 가지 mutation 승인 분리를
포함한다. 최종 추천은 메시지를 받은 에이전트가 대화 증거를 근거로 하나만
제시한다. Full path, session ID, credential, Task/Claim ID, Project/Repository
내부 ID와 raw launcher output은 출력하지 않는다.

Codex adapter는 유효한 단일 JSON object의 `systemMessage`만 stdout으로
출력한다. Claude adapter는 기존 평문 stdout 계약을 유지한다. Skip과 이미 완료된
세션은 stdout/stderr 모두 비우고 exit 0이다.

오류는 다음 bounded code 중 하나로만 안내한다.

- `HOOK_INPUT_INVALID`
- `REPOSITORY_IDENTITY_UNKNOWN`
- `PORTFOLIO_UNAVAILABLE`
- `PORTFOLIO_RESULT_INCOMPLETE`
- `NUDGE_STATE_UNAVAILABLE`

이 오류들은 원문 예외나 child stderr를 포함하지 않는다. `unknown`은 native
permission denial을 반환하지 않지만 AGENTS와 runtime guidance에서 정책상
차단이다. 사용자가 별도 mutation을 승인하기 전에는 Project Control 상태를
고치거나 다시 조회하는 읽기 전용 단계만 허용한다.

## 9. 설치와 migration

### 9.1 설치 layout

Task nudge의 실행 파일은 runtime 중립 private directory에 설치한다.

```text
~/.local/share/claude-config/hooks/
  task_nudge.py
  task-nudge-claude.py
  task-nudge-codex.py
```

디렉터리는 `0700`, 설치 파일은 owner-only mode로 원자 교체한다. Claude의 기존
`~/.claude/hooks/task-nudge.sh` 경로는 호환 shim으로 유지하고 neutral Claude
adapter를 호출한다. 다른 Claude hook의 현재 symlink 배포 방식은 바꾸지 않는다.
Codex config는 neutral Codex adapter의 stable command를 가리킨다.

독립적인 `scripts/install-task-nudge.py`가 task-nudge 관련 preflight, staging,
backup, merge와 rollback을 담당하고 `install.sh`는 고정 system Python으로 이를
호출한다. 이 helper는 설치 중 portfolio나 credential을 조회하지 않는다.

### 9.2 설정 병합

Claude `settings.json`에서는 기존 managed task-nudge command를 찾아 새 shim과
`Edit|Write|NotebookEdit` matcher로 갱신한다. 같은 command의 표기 차이는
정규화하고 중복 group을 만들지 않는다. 다른 hook group은 순서와 내용을
보존한다.

Codex `~/.codex/hooks.json`에는 `PreToolUse`의
`apply_patch|Edit|Write` matcher와 stable adapter command를 하나만 병합한다.
다른 사용자·project·plugin hook은 수정하지 않는다. Invalid JSON, 예상하지 않은
managed entry 충돌 또는 동일 command의 모순된 복수 entry는 임의로 정리하지
않고 설치를 중단한다.

### 9.3 AGENTS 병합

활성 전역 instruction 파일은 다음 순서로 선택한다.

1. 비어 있지 않은 `~/.codex/AGENTS.override.md`
2. `~/.codex/AGENTS.md`
3. 둘 다 없으면 새 `~/.codex/AGENTS.md`

Installer는 다음 exact marker 사이만 소유한다.

```text
<!-- claude-config:task-nudge:START -->
<!-- claude-config:task-nudge:END -->
```

Marker가 없으면 기존 문서 끝에 한 번 추가하고, 정확한 한 쌍이 있으면 내부만
교체한다. 한쪽 marker만 있거나 중복·역순·중첩이면 사용자 내용을 추측해 고치지
않고 task-nudge phase 전체를 무변경 실패시킨다.

### 9.4 보존, idempotence, rollback

Task-nudge 설치 phase는 모든 source와 target을 검증하고 변경안을 staging한 뒤
첫 target을 수정한다. 바뀌는 각 사용자 파일은 교체 전 타임스탬프 backup을
만든다. 각 write는 같은 private directory의 temporary file을 거쳐 원자
교체한다. 중간 실패 시 이 phase가 이미 바꾼 target만 backup으로 복구하고,
설치 전에 없던 target은 다시 없는 상태로 되돌린다.
이는 `install.sh`의 다른 독립 자산 설치까지 하나의 전역 transaction으로
되돌린다는 뜻은 아니다.

동일 source와 설정으로 재실행하면 파일, JSON entry, marker block 또는 backup을
추가로 만들지 않고 결과가 byte-identical해야 한다. 최초 JSON 변경은 안정된
형식으로 재직렬화할 수 있지만 unrelated key의 insertion order와 value는
보존한다. Markdown에서는 marker 밖의 기존 byte를 그대로 보존한다.

### 9.5 Codex trust

설치기는 hook trust를 자동 승인하지 않는다. 새 설치 또는 내용 변경 뒤 `/hooks`로
검토하고 신뢰해야 한다는 안내를 출력한다. Trust bypass flag, managed-policy 우회,
전역 allow-all은 사용하지 않는다. 훅이 아직 신뢰되지 않은 기간에는 AGENTS
fallback이 같은 manual checker를 사용한다.

## 10. 테스트 설계

### 10.1 공유 엔진

- Claude와 Codex payload normalization
- HTTPS/SSH remote의 exact slug와 malformed/credential-bearing remote 거부
- complete hit/miss, truncated hit/miss, pagination 잔여와 schema failure
- launcher missing, timeout, nonzero exit, oversized/invalid/duplicate-key JSON
- skip path가 marker를 소비하지 않는 동작
- opaque session ID hashing과 path traversal 방지
- 정상 판정의 세션당 at-most-once 및 runtime 분리
- 동시 process 중 한 호출만 marker를 얻는 동작
- `unknown`이 marker를 만들지 않고 다음 호출에서 복구되는 동작
- 모든 출력의 path/session/internal ID/credential canary 부재

Launcher는 temporary HOME의 exact
`$HOME/.local/bin/jhw-control-host` fixture로 대체한다. 실제 credential store와
Project Control을 호출하지 않는다.

### 10.2 정책과 adapter

정책 fixture는 최소 다음을 독립적으로 고정한다.

1. registered + immediate
2. registered + backlog
3. unregistered + recurring/multi-session immediate
4. unregistered + simple immediate
5. unregistered + backlog
6. unknown
7. lookup/Q&A, simple docs/config, subagent, already-decided session

각 fixture는 선택 가능한 action, 추천 규칙, 별도 승인 경계와 금지 action을
검사한다. 동일 fixture를 Claude plaintext, Codex `systemMessage`, AGENTS managed
block에 적용해 의미 drift를 탐지한다.

### 10.3 Installer

임시 HOME에서 다음을 검증한다.

- 빈 설정 신규 설치
- 기존 Claude/Codex hook과 AGENTS 본문 보존
- non-empty `AGENTS.override.md` 우선 선택
- 기존 managed entry migration과 중복 방지
- 동일 입력 재설치의 byte-identical 결과 및 추가 backup 없음
- 실제 변경 시에만 backup 생성
- invalid JSON, malformed/duplicate marker, managed hook 충돌의 무변경 실패
- staged write 중간 실패의 task-nudge target rollback
- `/hooks` 검토 안내 존재와 trust bypass 부재
- 설치 과정에서 launcher/credential provider를 실행하지 않는 동작

### 10.4 회귀 검증

기존 `task-nudge.sh`, global guidance, installer contract test를 새 구조에 맞춰
갱신하고 관련 Python test, shell syntax/static check 및 전체 repository test를
실행한다. 실제 사용자 HOME, live Codex trust와 실제 portfolio에는 test가 쓰지
않는다.

## 11. 완료 기준

1. Claude와 Codex의 첫 변경 후보에서 같은 등록 상태와 정책이 전달되고 정상
   세션에서는 최대 한 번만 안내된다.
2. Registered/unregistered와 immediate/backlog 조합이 자동 테스트로 고정된다.
3. 완전한 부재만 unregistered이며 모든 불확실성은 unknown으로 fail closed한다.
4. Backlog는 Issue만 제안하고 pre-claim하지 않는다.
5. Issue 생성, registration, Task start가 자동 실행되지 않고 각각 별도 승인임을
   모든 runtime guidance가 명시한다.
6. Native Codex hook이 없는 변경 경로에서도 AGENTS fallback이 같은 manual
   checker와 정책을 사용한다.
7. Installer가 기존 사용자 설정을 보존하고 멱등 동작하며 Codex trust를 우회하지
   않는다.
8. 관련 테스트와 전체 repository test가 모두 통과하고, 실제 HOME mutation 없이
   결과를 재현할 수 있다.

## 12. 참고 계약

- Codex hook lifecycle, `PreToolUse`, `systemMessage`, trust:
  <https://learn.chatgpt.com/docs/hooks>
- 전역 `AGENTS.override.md`와 `AGENTS.md` 탐색 우선순위:
  <https://learn.chatgpt.com/docs/agent-configuration/agents-md>
