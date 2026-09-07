# PR 전 적대적 리뷰 트리뷰널 설계

- 날짜: 2026-09-01
- 대상: GitHub Issue #32
- 상태: 대화 설계 승인 완료, 문서 검토 대기
- 기준 커밋: `a4e8cded0f86e9a4e01b8f99c36349fd7b8381a8`
- 지원 런타임: Claude Code, Codex CLI

## 1. 200단어 설계 요약

이 기능은 PR을 자동 생성하는 도구가 아니라, 검증되지 않은 변경이 PR 단계로
넘어가지 못하게 하는 로컬 품질 게이트다. 하나의 `pre-pr-tribunal` Skill을 Claude
Code와 Codex에 함께 설치하고, 두 런타임은 동일한 snapshot, reviewer prompt,
verdict schema와 판정 core를 사용한다. 런타임별 차이는 subagent 호출과
`PreToolUse` payload 변환에만 둔다.

실행 시 clean worktree에서 base SHA, HEAD SHA, merge-base, 변경 파일 목록과 diff
digest를 고정한다. Reviewer A는 correctness/security, B는 실행 증거, C는 단순성과
범위를 서로 독립적으로 검토한다. 세 결과는 strict schema로 정규화한다.
CRITICAL/HIGH가 남으면 첫 snapshot의 변경 파일 안에서만 제한적으로 자동 수정하고
검증·커밋한 뒤 새 snapshot으로 전원 재심한다. 새 경로, 의존성, 권한, 환경변수,
secret 변경 요구는 자동 반영하지 않는다. 반박은 명령, 종료 코드, 비밀을 제거한
bounded 출력이 붙고 다음 독립 라운드에서 finding이 재발행되지 않을 때만 닫힌다.
전체 루프는 세 라운드에서 종료한다.

통과 verdict는 gitignored `.review/verdict.json`에 원자적으로 기록하고 repository,
base/head SHA와 diff digest에 결속한다. Claude와 Codex의 얇은 hook은 실제
`gh pr create`만 식별한다. verdict가 없거나 stale, malformed, incomplete이거나
열린 CRITICAL/HIGH가 있으면 `deny`한다. 관련 없는 shell 명령은 건드리지 않는다.
두 런타임 모두 fake `gh` canary를 둔 실제 hook probe로 차단과 통과를 검증한다.
설치는 기존 secure transaction writer의 검증·rollback primitive를 공용 모듈로
추출해 unrelated 설정을 보존한다. Claude는 `Bash` matcher를 사용하고 Codex는
matcher 없는 `PreToolUse` handler에서 payload를 자체 필터링한다. 명령 해석은 기존
command-hygiene hook을 변경하지 않는 전용 bounded scanner로 격리한다.

## 2. 배경과 문제

현재 `/jhw:ship`은 PR이 생성된 뒤 Claude/Gemini workflow와 설치된 reviewer의
응답을 모아 merge gate를 판단한다. 이 흐름은 merge 안전에는 효과적이지만 다음
비용은 이미 발생한 뒤다.

1. 명백한 correctness/security 결함이 원격 PR에 먼저 노출된다.
2. 사실 검증이 필요한 주장까지 클라우드 reviewer 라운드를 소비한다.
3. 불필요하게 큰 diff가 PR 이후에야 단순화된다.
4. 여러 reviewer의 지적을 고친 뒤 새 finding이 생기며 라운드가 길어진다.

Issue #32는 이 검증을 PR 생성 이전으로 옮긴다. `/jhw:ship`을 대체하지 않고 그
앞에 놓여, 로컬 self-review와 원격 independent review가 서로 다른 단계에서
동작하게 한다.

## 3. 목표

1. PR 생성 전 세 개의 독립 reviewer 관점을 항상 실행한다.
2. 동작 주장은 Reviewer B의 실제 명령과 출력으로 증명하거나 반증한다.
3. 열린 CRITICAL/HIGH가 있으면 PR 생성을 차단한다.
4. 제한된 자동 수정과 최대 세 라운드로 수렴하거나 명시적으로 멈춘다.
5. verdict를 정확한 repository/base/HEAD/diff와 결속해 stale 승인을 거부한다.
6. Claude와 Codex가 같은 의미와 파일 형식을 사용하게 한다.
7. 실제 runtime hook에서 `gh pr create`가 실행되지 않는 것을 canary로 증명한다.

## 4. 비목표

- GitHub 웹 UI, `gh api`, REST/GraphQL client 또는 직접 HTTP 호출을 막지 않는다.
- 악의적인 로컬 사용자가 hook을 끄거나 verdict를 조작하는 것을 방어하지 않는다.
- `/jhw:ship`의 PR 이후 reviewer나 merge gate를 대체하지 않는다.
- reviewer가 제안한 임의 명령을 자동 실행하지 않는다.
- 새 dependency, 권한, environment variable, secret, CI 설정을 자동 수정하지 않는다.
- 네 번째 이상의 자동 review 라운드를 만들지 않는다.
- 다른 저장소의 PR 정책이나 중앙 서버 상태를 관리하지 않는다.

이 기능의 위협 모델은 정책을 따르는 coding agent의 실수와 stale local state다.
로컬 repository를 이미 제어하는 공격자에 대한 security boundary로 표현하지 않는다.

## 5. 확정된 제품 결정

| 항목 | 결정 |
| --- | --- |
| 런타임 | Claude Code와 Codex 동시 지원 |
| verdict 보관 | gitignored `.review/verdict.json` |
| 자동 수정 | 첫 snapshot의 변경 파일 범위 안에서 허용 |
| 라운드 상한 | 최대 3회 |
| 블로킹 심각도 | CRITICAL, HIGH |
| PR 차단 표면 | 실제 `gh pr create` shell 실행 |
| 오류 정책 | 관련 없는 명령은 fail-open, 식별된 PR 생성의 gate 오류는 fail-closed |
| 원격 review | 기존 `/jhw:ship` 유지 |

## 6. 검토한 접근

### 6.1 Skill 오케스트레이터 + 결정적 core + 얇은 hook

Skill이 agent orchestration과 수정 루프를 맡고 Python core가 snapshot, schema,
state, 최종 gate를 판정한다. Hook은 shell 명령 식별과 core 호출만 담당한다.
모델 판단과 결정적 검증의 경계가 분명하고 두 런타임이 core를 공유할 수 있어
이 접근을 채택한다.

### 6.2 Python 프로그램이 Claude/Codex CLI를 직접 실행

완전 자동화할 수 있지만 nested session, 인증, token budget, process recovery와
runtime upgrade까지 프로그램이 소유해야 한다. Issue #32의 품질 게이트보다 훨씬
큰 agent platform이 되므로 채택하지 않는다.

### 6.3 Prompt-only Skill + marker 파일

구현은 작지만 marker가 현재 diff를 검토했다는 사실, 세 reviewer가 모두
terminal인지, 반박에 실행 증거가 있는지를 결정적으로 확인할 수 없다. stale 또는
부분 실행을 통과시킬 수 있어 채택하지 않는다.

## 7. 전체 구조

```text
Claude Skill ──┐
               ├─> snapshot core ─> A/B/C independent review
Codex Skill ───┘                         │
                                        v
                              normalize + decision ledger
                                        │
                         blockers? ─ yes ┴─> bounded fix/rebuttal
                              │ no                 │
                              v                    └─> next round (max 3)
                      atomic local verdict
                              │
                 ┌────────────┴────────────┐
                 v                         v
       Claude PreToolUse gate     Codex PreToolUse gate
                 └────── actual gh pr create only ──────┘
```

### 7.1 구성요소

| 구성요소 | 책임 |
| --- | --- |
| `pre-pr-tribunal` Skill | runtime 선택, reviewer fan-out, 결과 수집, 수정/반박 루프 |
| Reviewer prompt 3개 | A/B/C의 입력·허용 행동·출력 schema 고정 |
| 공유 Python core/CLI | snapshot, strict JSON, finding/disposition, atomic verdict, gate check |
| Claude hook adapter | Claude `PreToolUse:Bash` 입력과 structured deny 출력 |
| Codex hook adapter | Codex shell hook 입력과 structured deny 출력 |
| installer wiring | Skill, executable, 두 runtime hook의 멱등·보존적 설치 |
| test/probe harness | unit, installer, direct adapter, 실제 runtime canary 검증 |

구현 계획에서 최종 파일 경로를 고정하되, source of truth는 하나의 core와 하나의
reviewer prompt set이어야 한다. 런타임마다 schema나 판정 코드를 복제하지 않는다.

## 8. Snapshot 계약

### 8.1 시작 조건

Tribunal은 다음 조건을 모두 만족할 때만 시작한다.

1. 현재 directory가 정확히 하나의 Git worktree 안에 있다.
2. detached HEAD가 아니며 HEAD가 commit을 가리킨다.
3. tracked/untracked 변경이 없고 `.review/`만 ignore 상태다.
4. base branch가 명시되어 있고 local remote-tracking ref로 해석된다.
5. base와 HEAD의 merge-base를 계산할 수 있다.

Skill은 snapshot 직전 base를 fetch할 수 있지만 core와 hook은 network를 사용하지
않는다. Hook이 실행되는 순간에는 기록된 local base ref만 다시 검증한다.

### 8.2 고정 필드

Snapshot은 최소 다음 값을 가진다.

- canonical GitHub `owner/repository`
- base ref와 base commit SHA
- HEAD SHA
- merge-base SHA
- `git diff --binary --no-ext-diff <merge-base>..HEAD`의 SHA-256
- NUL-safe changed path 목록과 change kind
- 최초 round의 changed path 집합
- snapshot 생성 시각과 schema version

Digest는 표시용 diff가 아니라 exact byte stream에서 계산한다. Locale, pager,
color, text conversion이 결과를 바꾸지 않도록 Git 실행 환경을 고정한다. Symlink,
submodule과 binary 변경도 name/status와 binary diff에 포함한다.

### 8.3 Stale 판정

다음 중 하나면 기존 verdict는 stale이다.

- repository identity가 다름
- base ref가 다른 SHA를 가리킴
- HEAD SHA가 다름
- merge-base 또는 diff digest가 다름
- worktree가 dirty임
- verdict schema가 현재 reader보다 새롭거나 지원되지 않음

Stale verdict는 자동 갱신하거나 일부 finding을 재사용하지 않는다. 새 tribunal
round가 필요하다는 bounded reason만 반환한다.

## 9. Reviewer 계약

세 reviewer는 같은 snapshot을 받지만 현재 round의 서로 다른 결과를 보지 않는다.
Agent는 verdict나 peer output을 직접 쓰지 않고 final structured report를 parent에
돌려준다. Parent는 세 agent가 terminal에 도달한 뒤에만 core에 결과를 전달한다.

### 9.1 Reviewer A — correctness & security

검토 범위:

- 논리 결함과 잘못된 상태 전이
- 누락된 error path와 fail-open/fail-closed 역전
- command injection, path traversal, symlink/TOCTOU, control character
- copy-before-verify, rollback 불완전, `set -e` 오해
- 하드코딩된 host/path/credential 또는 secret 노출

Reviewer A는 source를 수정하지 않는다. 필요하면 읽기 전용 검색과 안전한 기존
테스트를 실행할 수 있지만, 실행 주장을 만들면 Reviewer B 형식의 evidence를 함께
제공해야 한다.

### 9.2 Reviewer B — empirical verifier

Reviewer B는 추론만으로 동작 사실을 단정하지 않는다. Diff, commit message,
설계 문서가 주장하는 각 중요한 동작에 대해 다음 중 하나를 반환한다.

- 검증 명령과 실제 결과가 주장을 지지함
- 검증 명령과 실제 결과가 주장을 반증함
- 안전하게 실행할 수 없어 검증 불가이며 그 이유가 명시됨

각 execution에는 command, exit code, sanitized bounded stdout/stderr excerpt,
전체 capture의 SHA-256, truncation 여부가 들어간다. Secret 가능성이 있는 출력은
verdict에 넣지 않고 해당 검증을 실패로 처리한다.

### 9.3 Reviewer C — simplicity cop

검토 범위:

- Issue 목표보다 큰 변경
- 이미 존재하는 pattern을 중복 구현한 code
- 필요하지 않은 abstraction/configuration/compatibility layer
- 하나의 책임을 넘는 module과 과도한 file count
- 더 작은 대안으로 같은 완료 조건을 만족할 수 있는지

Reviewer C는 단순히 “더 단순하게”라고 쓰지 않는다. 제거하거나 합칠 구체 대상과
그 변경이 보존해야 할 behavior를 함께 제시한다.

### 9.4 공통 finding

Finding은 최소 다음 필드를 가진다.

- stable round-local ID
- reviewer `A|B|C`
- severity `CRITICAL|HIGH|MEDIUM|LOW`
- title과 bounded rationale
- path와 optional line anchor
- 재현 또는 검증 evidence
- 수정 후 확인할 acceptance condition

Path가 현재 diff 밖이거나 새 dependency/권한/secret을 요구하는 finding도 보고는
하지만 자동 수정 대상은 아니다.

## 10. Verdict와 decision ledger

### 10.1 예시 shape

```json
{
  "schema": 1,
  "repository": "owner/repository",
  "base": {"ref": "master", "sha": "<40-hex>"},
  "head_sha": "<40-hex>",
  "merge_base_sha": "<40-hex>",
  "diff_sha256": "<64-hex>",
  "initial_paths": ["hooks/example.py"],
  "round": 2,
  "producer_runtime": "codex",
  "reviewers": {
    "A": {"status": "complete", "findings": []},
    "B": {"status": "complete", "findings": [], "executions": []},
    "C": {"status": "complete", "findings": []}
  },
  "decisions": [],
  "gate": {"status": "pass", "blocking_count": 0},
  "created_at": "2026-09-01T00:00:00Z"
}
```

실제 schema는 unknown key, duplicate key, 잘못된 enum, control character와 크기
초과를 거부한다. Strict parser가 text acceptance의 정본이다. 모든 JSON-decoded string은
NFC여야 하고 Unicode General_Category `Cc` 또는 `Cs`를 포함할 수 없다. 따라서 JSON escape가
디코딩되는 LF, CR, TAB을 포함한 control character와 surrogate도 invalid다.
`producer_runtime`은 감사 정보일 뿐 gate 의미에는 영향을 주지 않는다. 따라서 Codex가 만든
valid verdict를 Claude hook이, Claude가 만든 verdict를
Codex hook이 동일하게 인정한다.

### 10.2 Decision 종류

Blocking finding은 다음 두 방식으로만 닫힌다.

1. `fixed`: source를 수정·검증·commit한 다음 새 round에서 finding이 재발행되지 않음
2. `rebutted`: controlling session이 독립적으로 선택·실행한 명령과 bounded output을
   decision ledger에 기록하고, 새 round가 그 evidence를 받은 뒤 finding을 다시
   발행하지 않음

Parent가 “동의하지 않음”만 기록하거나 reviewer가 제안한 command text를 그대로
실행한 결과는 rebuttal이 아니다. MEDIUM/LOW는 열린 상태로 보고할 수 있지만 기본
gate를 막지 않는다.

### 10.3 로컬 저장 안전성

- `.review/`는 repository `.gitignore`에 포함한다.
- Directory는 symlink가 아닌 private directory여야 한다.
- Verdict와 lock은 regular file이며 symlink를 따라가지 않는다.
- Writer는 lock 아래 temporary regular file을 쓰고 fsync 후 atomic replace한다.
- Verdict 전체, reviewer report, 개별 evidence에 각각 크기 상한을 둔다.
- Parser는 raw secret, absolute home path 또는 unbounded subprocess output을 error에
  반사하지 않는다.

로컬 사용자가 파일을 직접 바꿀 수 있으므로 cryptographic attestation으로
표현하지 않는다. 목표는 accidental/stale bypass 방지다.

## 11. 라운드와 제한적 자동 수정

### 11.1 Round algorithm

1. 기존 pass verdict를 invalid 상태로 전환하고 새 snapshot을 만든다.
2. A/B/C를 같은 snapshot으로 병렬 실행한다.
3. 세 report가 모두 terminal이고 schema-valid인지 확인한다.
4. 열린 CRITICAL/HIGH가 없으면 pass verdict를 기록한다.
5. Blocking finding이 있으면 fix 또는 evidence-backed rebuttal을 준비한다.
6. Source가 바뀌면 관련 테스트를 실행하고 하나의 review-fix commit을 만든다.
7. 새 snapshot으로 세 reviewer 전원을 다시 실행한다.
8. 세 번째 round 뒤에도 blocking finding이 있으면 fail verdict로 종료한다.

Agent failure, timeout 또는 malformed report가 있으면 그 round는 pass가 아니다.
실패를 숨기고 나머지 두 reviewer만으로 verdict를 만들지 않는다.

### 11.2 자동 수정 허용 범위

자동 수정은 다음을 모두 만족해야 한다.

- 수정 path가 round 1 `initial_paths` 안에 있음
- Issue #32의 목표와 직접 관련됨
- dependency 추가/업그레이드가 아님
- environment, secret, permission, remote endpoint 변경이 아님
- reviewer text의 URL, encoded payload 또는 shell command를 실행하지 않음
- 수정 후 targeted test와 repository-required test를 실행할 수 있음

조건 밖의 CRITICAL/HIGH는 자동 반영하지 않고 blocker로 남긴다. 사용자 승인을
추론하거나 scope를 넓혀 해결하지 않는다.

## 12. Runtime orchestration

### 12.1 공유 Skill

하나의 Skill source가 `~/.claude/skills`와 `~/.codex/skills`에 설치된다. Skill은
runtime별 tool name만 분기하고 다음 계약은 동일하게 유지한다.

- 동일 snapshot과 reviewer prompt
- 독립 병렬 실행
- 최대 3라운드
- 동일 report/verdict schema
- 동일 auto-fix 및 rebuttal boundary
- 동일 stop condition과 사용자 보고

### 12.2 Claude Code

Claude는 `Agent` subagent를 사용한다. Reviewer 결과 전달이 유실되지 않도록
foreground/unnamed 결과 수집을 기본으로 하고, mailbox mode를 쓸 때는 명시적
delivery contract를 둔다. 세 agent가 끝나기 전에는 peer report를 worktree에 쓰지
않는다.

### 12.3 Codex

Codex는 native collaboration subagent를 사용한다. 현재 설치본에서 multi-agent와
hooks가 활성 상태인지 preflight로 확인한다. 사용 가능한 native role routing이
있으면 적합한 read-only review role을 사용하고, 지원되지 않는 authority나 role을
prompt label로 위조하지 않는다.

두 runtime 모두 reviewer가 source를 직접 수정하지 않는다. Fix는 세 결과를 받은
controlling session 하나가 수행해 shared worktree write race를 막는다.

## 13. `gh pr create` gate

### 13.1 명령 식별

Hook matcher는 process spawn을 줄이는 최적화일 뿐 authority가 아니다. Adapter는
payload에서 shell command를 직접 검증한다. Scanner는 최소 다음을 처리한다.

- leading environment assignment
- `command`/`exec`/`time`/`env` wrapper와 GNU `env -S` alternate argv
- absolute 또는 relative `gh` executable path의 basename
- compound command, subshell, command substitution 안의 실제 subcommand
- line continuation과 ANSI-C quoted word
- quote, comment, heredoc data 안의 예시 문자열 제외

Regex 한 번으로 raw string의 `gh pr create` 포함 여부를 판정하지 않는다. Scanner가
실제 실행 가능 위치에서 후보를 찾은 뒤 `gh` argv가 `pr create` subcommand인지
확인한다. 명령이 관련 없다고 증명되면 아무 출력 없이 exit 0이다. 실행 후보가 있으면
다른 non-empty command segment, nested execution, redirection, cwd-changing wrapper 또는
output-writing wrapper를 허용하지 않는다.

Pass 후보는 literal `--base`를 정확히 한 번 포함하고 그 값이 verdict base와 같아야 한다.
`GH_REPO`/`GH_HOST`/Git worktree assignment, `--repo`/`-R`, `--head`/`-H`, hostname/config
override와 free-standing dynamic argv는 target binding을 증명할 수 없으므로 거부한다.

v1 scanner는 기존 `verification-command-hygiene-hook.py` parser를 refactor하거나
import하지 않는다. 요구하는 출력과 오류 경계가 다르므로 tribunal core 안에 작은
전용 scanner를 두되, quote/comment/heredoc 회귀 corpus는 기존 테스트와 같은 사례를
공유한다. 이 결정은 #19 동작 변경을 Issue #32 범위에 섞지 않기 위한 것이다.

### 13.2 Gate 판정

실제 PR 생성이 확인되면 다음 순서로 검사한다.

1. payload cwd의 exact repository root
2. worktree clean 여부
3. `.review/verdict.json` 안전한 open과 strict parse
4. repository/base/head/merge-base/diff binding
5. command의 explicit base와 repo/head/environment target binding
6. 세 reviewer terminal 상태
7. round 범위와 gate status
8. 열린 CRITICAL/HIGH count가 0인지

하나라도 실패하면 PR 명령을 실행하지 않고 bounded reason과 tribunal 재실행 방법을
agent에게 돌려준다. Passing verdict에서는 `allow`를 출력하지 않고 no-decision으로
기존 permission policy에 맡긴다.

### 13.3 Runtime 출력

Claude와 Codex adapter는 각 runtime이 지원하는 `PreToolUse` structured output의
`permissionDecision: "deny"`와 non-empty reason만 사용한다. Codex 설치본이
`ask`/`allow`를 지원하지 않아도 이 설계에는 영향이 없다. Runtime version이 deny
계약을 지원하지 않으면 installer/probe가 fail하고 기능을 설치 완료로 보고하지
않는다.

오류 경계는 다음과 같다.

- 실제 PR 생성 후보가 아닌 명령에서 parser/core 오류: fail-open, bounded diagnostic
- 실제 PR 생성 후보를 식별한 뒤 verdict/core 오류: fail-closed
- malformed hook payload로 command 자체를 읽을 수 없음: unrelated Bash까지 막지 않음
- 1 MiB 초과 hook payload: command를 신뢰할 수 없으므로 parse 전에 bounded deny

Hook은 network에 접근하거나 reviewer를 실행하지 않는다. Local Git과 bounded file
read만 수행해 일반 shell latency를 제한한다.

## 14. 설치와 호환성

Installer는 다음을 보존적으로 수행한다.

1. Skill source를 Claude와 Codex skill directory에 각각 안전하게 link한다.
2. 공유 executable/core와 runtime adapter를 installer-owned 위치에 배치한다.
3. Claude settings에 `PreToolUse:Bash` handler 하나를 멱등 추가한다.
4. Codex hooks에는 matcher 없는 `PreToolUse` handler 하나를 멱등 추가하고 adapter가
   shell command payload만 자체 필터링한다.
5. 같은 command의 낡은 matcher는 단일 managed group일 때만 갱신한다.
6. unrelated settings, hook group, skill과 AGENTS 내용을 그대로 보존한다.
7. malformed/duplicate-key/symlink/config drift에서는 어떤 target도 부분 변경하지 않는다.

Codex hook trust는 native Codex 절차를 따른다. Installer가 trust를 자동 생성하거나
`--dangerously-bypass-hook-trust`를 production 설정에 넣지 않는다. 실제 설치 후
사용자가 native prompt에서 새 hook source를 검토·신뢰해야 한다.

기존 설치기는 여러 파일을 transaction으로 다루므로 두 번째 ad-hoc `hooks.json`
writer를 만들지 않는다. v1은 `scripts/install-task-nudge.py`의 JSON strict parse,
target inspection, staged write, rollback primitive를 공용 installer module로 추출하고,
기존 task-nudge installer와 새 tribunal installer가 그 module을 함께 사용한다.

## 15. 오류 처리와 사용자 메시지

| 상태 | Gate | 사용자/agent 안내 |
| --- | --- | --- |
| verdict 없음 | deny | tribunal 실행 필요 |
| verdict malformed/unsafe | deny | local state 재생성 필요 |
| HEAD/base/diff stale | deny | 현재 snapshot으로 재심 필요 |
| worktree dirty | deny | commit/정리 후 재심 필요 |
| reviewer 일부 실패 | deny | 실패 reviewer 포함 round 재실행 |
| CRITICAL/HIGH 열림 | deny | finding ID와 bounded next action |
| 3라운드 소진 | deny | 자동화 종료, 사용자 판단 필요 |
| MEDIUM/LOW만 열림 | pass | non-blocking summary 유지 |
| valid pass verdict | no decision | runtime의 원래 permission 흐름 유지 |

Raw subprocess output, absolute home path, token, credential, 전체 diff를 hook reason에
넣지 않는다. Reason은 stable code와 다음 명령/Skill 이름만 제공한다.

## 16. 검증 전략

### 16.1 Pure/unit tests

- repository/base/head/diff snapshot 결정성
- duplicate JSON key와 unknown schema 거부
- severity와 blocking count
- missing/stale/malformed/incomplete verdict
- decision evidence 필수 필드와 크기 제한
- round 1..3과 round exhaustion
- initial path auto-fix boundary
- symlink/non-regular/permission/atomic replace/lock contention

### 16.2 Shell 명령 corpus

Positive fixture:

- `/usr/bin/gh pr create --base master`
- `FOO=1 command gh pr create --base master`
- `gh pr create -Bmaster --fill`

Ambiguous/deny fixture:

- explicit base가 없는 `gh pr create`
- `GH_REPO=other/repo gh pr create --base master`
- `tests && gh pr create --base master`
- executable command substitution 안의 `gh pr create`
- redirection을 포함한 PR candidate
- combined ANSI-C word와 `env -S`가 구성한 PR candidate

Negative fixture:

- `echo 'gh pr create'`
- comments와 heredoc 문서 예시
- `rg 'gh pr create' docs`
- `gh pr view`, `gh issue create`
- redirection target 또는 argument data에만 있는 문자열

v1은 기존 verification-command-hygiene parser를 수정하지 않는다. Tribunal 전용
scanner corpus와 함께 기존 #19 전체 test suite도 실행해 동작 격리를 증명한다.

### 16.3 Adapter tests

- Claude와 Codex의 실제 fixture payload
- no-match에서는 빈 stdout/exit 0
- match + fail verdict에서는 각 runtime이 이해하는 deny JSON
- match + pass verdict에서는 no-decision
- oversized input의 bounded deny와 deep/malformed in-limit input의 bounded behavior
- error message에 secret/path canary가 없는지

### 16.4 Installer tests

- 임시 HOME fresh install
- identical reinstall byte stability
- unrelated Claude/Codex hook 보존
- duplicate managed hook 정규화 또는 명시적 거부
- malformed JSON과 symlink drift에서 transaction rollback
- Skill이 두 runtime에 정확히 한 번 설치됨
- file/directory mode와 backup policy
- Codex hook trust를 위조하지 않음

### 16.5 실제 runtime canary probe

2026-09-02에 승인된
[`2026-09-02-pre-pr-runtime-canary-simplification-design.md`](./2026-09-02-pre-pr-runtime-canary-simplification-design.md)가
이 절의 authoritative 설계다. 실제 runtime canary는 기존 bubblewrap, fake `gh`,
GitHub sinkhole과 exact-command guard를 유지하되 runtime raw output, version, hash와
credential classifier를 증거에서 제거한다. Hook/fake-`gh` marker만 판정하고 Codex
auth는 sealed memfd에서 sandbox tmpfs로 초기화해 host disk staging을 만들지 않는다.

## 17. 성능과 운영 한계

- 모든 shell call에서 agent나 network를 실행하지 않는다.
- Runtime matcher와 자체 scanner로 대부분의 명령을 즉시 통과시킨다.
- Verdict 검증은 bounded JSON read와 소수 local Git command로 제한한다.
- Output과 state 크기 상한을 테스트로 고정한다.
- Hook timeout/exception은 실제 PR candidate 판정 전후 경계에 맞춰 처리한다.
- Runtime upgrade로 payload/deny 계약이 바뀌면 canary probe가 배포를 막는다.

## 18. 완료 조건 매핑

| Issue #32 완료 조건 | 설계상 검증 |
| --- | --- |
| 세 reviewer prompt와 역할 격리 | 공유 prompt 3개 + 동일 snapshot + peer 결과 비공유 테스트 |
| `.review/verdict.json` schema | strict schema/parser/fixture와 문서화된 shape |
| 증거 없는 반박 거부 | decision evidence validation + 다음 독립 round 재발행 검사 |
| `gh pr create` 실제 차단 | Claude/Codex fake `gh` canary probe |
| 착수 전 200단어 설계 리뷰 | 본 문서 1절과 사용자 승인 기록 |

추가로 다음 gate를 완료 기준에 포함한다.

- 두 runtime의 direct adapter와 installer integration test 통과
- repository 전체 pytest와 shellcheck 통과
- 실제 사용자 HOME, real PR, real GitHub API를 테스트 중 변경하지 않음
- PR 이후 `/jhw:ship` reviewer gate 통과

## 19. 참고 계약

- Claude Code hooks reference: https://code.claude.com/docs/en/hooks
- Claude Code hooks guide: https://code.claude.com/docs/en/hooks-guide
- OpenAI model guidance (multi-agent 개념):
  https://developers.openai.com/api/docs/guides/latest-model
- 이 저장소의 hook 운영 원칙: `hooks/README.md`
- PR 이후 review/merge gate: `/jhw:ship`
