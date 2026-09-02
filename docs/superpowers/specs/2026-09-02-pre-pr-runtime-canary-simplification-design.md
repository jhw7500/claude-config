# PR 전 실제 runtime canary 단순화 설계

- 날짜: 2026-09-02
- 대상: GitHub Issue #32, Task 8 실제 Claude/Codex canary
- 상태: Task 5 live runtime 호환성 수정 검증 중
- 기준 커밋: `841a5e26b3315ed57bf65dcbcb948a6165022afa`
- 대체 범위: `2026-09-01-pre-pr-adversarial-tribunal-design.md`의 16.5절

## 1. 요약

실제 runtime canary는 Claude Code와 Codex가 설치된 `PreToolUse` hook을 통해 같은
`gh pr create --title canary --body canary` 명령을 차단하고 허용하는지만 검증한다.
Runtime 출력의 version, hash, 진단 문자열 또는 민감정보 여부를 증거로 사용하지
않는다. Stdout과 stderr는 `/dev/null`로 폐기하고, canary 전용 hook wrapper와 fake
`gh`가 남기는 짧은 marker만 판정한다.

단일 probe 프로세스가 기존 bubblewrap, GitHub hostname sinkhole, 모든 고정 `gh`
경로의 fake bind-over, exact-command guard와 immutable control root를 유지한다. 별도
supervisor, signal state machine, evidence socket/thread, runtime-output JSON lexer와
credential leak classifier는 제거한다.

Claude OAuth는 runtime child environment에만 전달한다. Codex auth JSON은 디스크에
복사하지 않고 sealed memfd에서 bubblewrap의 writable tmpfs `CODEX_HOME/auth.json`으로
초기화한다. Codex의 atomic refresh는 tmpfs 안에서 허용되며 namespace 종료와 함께
사라진다. Probe가 비정상 종료돼 임시 repo/config가 남더라도 credential 사본은
남지 않는다.

## 2. 변경 이유

기존 Task 8 probe는 안전한 진단 출력을 만들기 위해 runtime raw output에서 version,
hash와 credential 흔적을 파생했다. 그 결과 다음 책임이 한 파일에 결합됐다.

- catchable signal의 모든 bytecode 경계에서 resource ownership 보장
- child process-group 생성, 종료와 reap
- initial/refreshed credential inventory
- 임의 JSON 문자열과 Unicode escape 해석
- runtime 출력의 민감도, auth 실패와 control breach 우선순위
- evidence socket과 immutable control 검증

최종 독립 리뷰에서 `Popen()` publication, handler installation, adjacent JSON token,
일반 log brace 오탐과 quadratic matching이 반복 발견됐다. Issue #32의 실제 완료
조건은 raw output 분석이 아니라 fake `gh`의 missing/pass 행동이다. 불필요한 진단
기능을 제거해 문제 표면을 줄이는 것이 추가 보안 state machine보다 안전하고
운영하기 쉽다.

## 3. 목표

1. Claude와 Codex 각각 missing verdict에서 hook deny와 fake `gh` 0회를 확인한다.
2. Valid pass verdict에서 hook allow와 fake `gh` 정확히 1회를 확인한다.
3. 실제 GitHub hostname과 real `gh` 실행 경로를 기존 bubblewrap 경계로 차단한다.
4. Subscription auth를 live 설정 상속 없이 사용할 수 있게 한다.
5. Codex credential 사본을 host filesystem에 만들지 않는다.
6. Runtime raw output이나 그 파생값을 report, log 또는 validation artifact에 남기지
   않는다.
7. 실패를 소수의 stable status로 표현하고 nonzero로 종료한다.

## 4. 비목표와 위협 모델

- 같은 UID의 악의적인 local process나 repository owner의 marker 위조를 방어하지
  않는다.
- Runtime output을 디버깅하거나 provider auth 오류 문구를 분류하지 않는다.
- SIGKILL, kernel failure 또는 power loss 뒤 non-secret 임시 repo/config가 항상
  제거된다고 주장하지 않는다.
- Provider network를 Claude와 Codex별로 분리하지 않는다. 기존처럼 GitHub 대상만
  sinkhole하고 provider connectivity는 공유한다.
- `/jhw:ship`이나 PR 이후 review gate를 대체하지 않는다.

Issue #32의 위협 모델은 정책을 따르는 coding agent의 실수, stale verdict와 잘못된
hook wiring이다. Same-UID adversary에 대한 tamper-proof audit system이 아니다.

## 5. 구조

```text
probe-pre-pr-tribunal.py
  ├─ secure subscription preflight
  ├─ non-secret work/control/evidence directories
  ├─ canary-only hook marker wrapper
  ├─ fake gh marker writer
  ├─ existing bubblewrap + guard + hostname sinkhole
  ├─ missing-verdict runtime child
  └─ pass-verdict runtime child
```

새 daemon, supervisor process, socket protocol 또는 background evidence thread를
추가하지 않는다. Probe entrypoint 하나와 test fixture만 유지한다.

## 6. 인증 처리

### 6.1 공통 source validation

기존 secure read 계약을 유지한다.

- Caller HOME 아래 정확한 credential source만 연다.
- Directory와 file을 descriptor-relative, no-follow, close-on-exec 방식으로 연다.
- Current UID owner, regular file, owner-only mode, bounded stable read와 duplicate-free
  JSON을 요구한다.
- Live source path는 sandbox에서 empty immutable directory로 가린다.
- 실행 전후 source bytes와 metadata가 같아야 한다.
- Missing, malformed, insecure 또는 expired source는 `CREDENTIAL_UNAVAILABLE`이다.

### 6.2 Claude

`.claudeAiOauth.accessToken`과 `expiresAt`을 검증한다. Version probe가 제거되므로
validity margin은 두 120초 runtime phase, 두 30초 begin/finalize deadline과 30초
scheduling cushion을 합친 330초다. OAuth 값은 Claude child의
`CLAUDE_CODE_OAUTH_TOKEN`에만 들어간다. Probe report, argv, marker와 filesystem에는
쓰지 않는다.

### 6.3 Codex

검증된 `.codex/auth.json` bytes는 다음 순서로 전달한다.

1. `os.memfd_create()`로 close-on-exec memfd를 만든다.
2. Bounded bytes를 기록하고 처음으로 rewind한다.
3. `MFD_ALLOW_SEALING`과 `F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW |
   F_SEAL_WRITE`를 요구하고 적용해 source memfd 변경을 막는다.
4. Bubblewrap에 해당 FD만 `pass_fds`로 전달한다.
5. Bubblewrap은 writable tmpfs `CODEX_HOME`을 만들고 `--perms 0600 --file FD
   CODEX_HOME/auth.json`으로 초기화한다.
6. 검증된 `hooks.json`도 별도 sealed memfd로 열어 `--ro-bind-data`로 같은 tmpfs의
   `CODEX_HOME/hooks.json`에 read-only 배치한다. Hook command는 immutable control
   root의 absolute package path를 계속 사용한다.
7. Codex는 tmpfs 안에서 auth file을 atomic replace하거나 refresh할 수 있지만 hook
   config는 수정할 수 없다.
8. Parent FD와 namespace가 종료되면 모든 credential copy가 사라진다.

Synthetic probe에서 read-only control root 위의 tmpfs `CODEX_HOME`, writable auth
initialization/atomic replacement와 read-only hooks overlay가 함께 성공함을 확인했다.
`memfd_create`, exact required seals, bubblewrap `--file` 또는 `--ro-bind-data`를
안전하게 사용할 수 없으면 `ISOLATION_UNAVAILABLE`로 중단한다. Host disk staging
fallback은 없다.

### 6.4 Explicit environment mode

`--auth-source environment`는 호환성을 위한 명시적 opt-in으로 유지한다. Claude에는
`ANTHROPIC_API_KEY`만, Codex에는 `OPENAI_API_KEY`만 전달한다. Subscription mode는
API key로 fallback하지 않는다.

## 7. 격리 경계

기존 승인된 경계를 유지한다.

- Bubblewrap은 required이며 사용할 수 없으면 fail closed한다.
- Control root 전체를 read-only mount한다.
- Runtime HOME, tmpfs Codex auth와 phase evidence만 최소 writable mount한다. Codex
  hook config는 tmpfs 안에서도 read-only overlay다.
- Caller `.claude`와 `.codex`는 empty immutable directory로 가린다.
- Host runtime launcher는 sandbox 진입 전에 canonical executable로 해석하고 immutable
  control root의 빈 target 위에 read-only bind한 뒤 live config directory를 가린다.
  Codex executable 옆에 별도 `codex-code-mode-host`가 있으면 그 canonical executable도
  전용 sibling target에 함께 bind한다. Companion이 없는 배포는 기존 단일 target을
  유지하며, 어느 경우에도 runtime package나 `.codex` config 전체를 노출하지 않는다.
- PATH 첫 항목과 발견된 모든 고정 system `gh` 경로를 fake executable로 덮는다.
- GitHub hostname은 isolated hosts file로 loopback/sinkhole 처리한다.
- Hook guard는 Claude의 exact `Bash` tool을 요구한다. Codex는 matcherless adapter와
  동일하게 non-empty tool name과 string command를 요구한 뒤 exact command와 exact cwd만
  허용해 code-mode 또는 이후 command-tool 이름 변경을 수용한다.
- Fake `gh`는 exact argv와 cwd를 검증한다.
- Runtime process에는 `--die-with-parent` bubblewrap boundary를 적용한다.

Provider connectivity는 유지한다. 직접 HTTP client나 같은 UID의 의도적 marker
위조를 막는 경계라고 표현하지 않는다.

## 8. Marker evidence

각 phase는 별도의 owner-private evidence directory와 두 개의 bounded append log를
사용한다.

- `hook.log`: `D\n`, `A\n`, `I\n`만 허용한다.
- `gh.log`: `V\n`, `I\n`만 허용한다.

Canary-only hook wrapper는 실제 설치된 adapter를 실행한다. Adapter exit가 0이고
runtime-native deny output이 strict contract와 일치하면 `D`, stdout이 비어 있으면
`A`, 그 밖의 결과는 `I`를 기록한다. Adapter output은 marker에 기록하지 않고 원래
runtime으로 그대로 전달한다. Wrapper capture는 기존 hook output 상한 안에서만
허용한다.

Fake `gh`는 exact argv와 cwd이면 `V`, 아니면 `I`를 기록한다. Log는 pre-created
owner-only regular file이며 최대 64 bytes다. Unknown line, oversize, duplicate hook
event 또는 invalid `gh` event는 `CANARY_MISMATCH`다.

Expected matrix는 다음과 같다.

| Phase | `hook.log` | `gh.log` |
| --- | --- | --- |
| missing verdict | 정확히 `D` 1개 | 비어 있음 |
| valid pass verdict | 정확히 `A` 1개 | 정확히 `V` 1개 |

Marker는 same-UID adversary에 대한 보안 증거가 아니라 canary fixture다. Exact-command
guard와 tools 제한을 따르는 runtime의 wiring 오류를 발견하는 데 사용한다.

## 9. Runtime 실행과 output

Runtime은 bubblewrap child process group으로 시작한다.

- stdin은 `/dev/null`이다.
- stdout과 stderr도 `/dev/null`이다.
- Claude는 disposable sandbox와 fake `gh`만 설명하는 고정 canary system prompt를 사용하고,
  user prompt는 41자 exact command 외의 prefix, suffix와 wrapper를 금지한다. 기본 agent
  planning의 비결정성은 canary wiring 판정에 포함하지 않는다.
- Runtime output byte, hash, version, auth marker 또는 sensitivity를 읽지 않는다.
- 120초 timeout을 넘으면 process group에 TERM, bounded grace 뒤 KILL을 보내고 reap한다.
- Nonzero는 내용을 해석하지 않고 `RUNTIME_FAILED`다.
- Runtime child가 끝나기 전 marker를 판정하지 않는다.

`Popen`과 process-group cleanup은 정상, timeout과 ordinary exception을 위한 짧은
`try/finally`만 사용한다. Custom signal handler, `pthread_sigmask`, decoded-output
scanner, selector와 capture digest는 사용하지 않는다.

SIGINT는 Python의 기본 `KeyboardInterrupt`와 `finally`에 맡긴다. SIGTERM/SIGKILL로
probe가 바로 종료되면 bubblewrap `--die-with-parent`가 runtime을 종료하고 memfd와
tmpfs credential은 사라진다. Non-secret 임시 directory가 남을 수 있다는 점은
문서화하며 이를 credential-cleanup 실패로 표현하지 않는다.

## 10. Report schema v2

성공 예시는 다음과 같다.

```json
{
  "schema": 2,
  "status": "PASS",
  "claude": {
    "status": "PASS",
    "missing": {"runtime_exit": "ZERO", "hook": "DENY", "gh_calls": 0},
    "pass": {"runtime_exit": "ZERO", "hook": "ALLOW", "gh_calls": 1}
  }
}
```

Allowed public fields는 schema, overall/runtime status와 phase별 `runtime_exit`, `hook`,
`gh_calls`뿐이다. 다음 field는 v2에서 제거한다.

- runtime version
- stdout/stderr hash
- sensitivity와 reason 목록
- raw output 또는 path
- credential/auth 분류 문자열

`--runtime all`은 Claude와 Codex가 모두 PASS일 때만 overall PASS다. Report는 단일
duplicate-free bounded JSON이고 path, prompt, environment와 credential을 포함하지
않는다.

## 11. Stable failure status

- `CREDENTIAL_UNAVAILABLE`
- `RUNTIME_UNAVAILABLE`
- `ISOLATION_UNAVAILABLE`
- `RUNTIME_FAILED`
- `TIMEOUT`
- `CANARY_MISMATCH`
- `SETUP_FAILED`
- `CLEANUP_FAILED`

Preflight credential 오류만 `CREDENTIAL_UNAVAILABLE`로 구분한다. Runtime output을
분석해 provider auth 실패를 재분류하지 않는다. 모든 failure는 nonzero다.

## 12. CLI 호환성

다음 CLI는 유지한다.

```text
probe-pre-pr-tribunal.py --runtime claude|codex|all
                         --repo-source ABSOLUTE_PATH
                         [--work-dir ABSENT_ABSOLUTE_PATH]
                         [--auth-source subscription|environment]
```

Report schema가 1에서 2로 바뀌는 것은 의도적인 breaking change다. 이 script의
consumer는 repository test와 운영 문서뿐이며, v1 report compatibility shim은 두지
않는다.

## 13. 구현 파일 경계

새 production subsystem을 만들지 않는다.

- `scripts/probe-pre-pr-tribunal.py`: 기존 entrypoint를 축소 재작성
- `tests/pre_pr_tribunal/test_probe_harness.py`: obsolete signal/parser 테스트를
  marker/memfd/runtime lifecycle 테스트로 대체
- `README.md`, `hooks/README.md`: v2 운영 계약
- `docs/validation/2026-09-01-pre-pr-tribunal-canary.md`: 기존 실패 이력과 새 검증 증거
- 기존 tribunal design: 본 문서를 authoritative canary 설계로 연결

현재 branch history를 reset하지 않는다. 새 구현 commit에서 기존 signal/parser
코드를 삭제하고 v2로 교체한다.

## 14. 검증 전략

모든 automated test는 synthetic credential과 fake runtime만 사용한다.

1. Memfd bytes가 bubblewrap tmpfs auth file을 초기화하고 atomic replace를 허용한다.
2. Host work/control/evidence tree 어디에도 auth bytes가 기록되지 않는다.
3. Claude와 Codex가 missing `D/0`, pass `A/1` matrix를 만족한다.
4. Duplicate/invalid/oversized marker가 `CANARY_MISMATCH`다.
5. Runtime이 placeholder stdout/stderr를 대량 출력해도 report와 filesystem에 나타나지
   않는다.
6. Nonzero와 timeout이 stable status이고 child process group이 남지 않는다.
7. Probe subprocess에 SIGINT/SIGTERM을 보내 runtime/bubblewrap가 사라지고 host disk에
   auth file이 없음을 확인한다. Non-secret temp residue는 test가 정리한다.
8. Absolute/system `gh`, PATH reset, `command -p`, GitHub hostname client와 hook deviation
   공격 fixture가 기존 isolation failure를 유지한다.
9. Subscription mode가 API key를 전달하지 않고 explicit environment mode가 provider별
   key 하나만 전달한다.
10. Report schema v2에 version, capture, hash, sensitivity, raw output와 path field가 없다.
11. 관련 tribunal, adapter, installer와 전체 repository regression이 통과한다.

구현 diff에는 custom termination state, `pthread_sigmask`, runtime-output JSON decoder,
sensitivity classifier, capture hash와 version extraction이 남아 있지 않아야 한다.

## 15. 실제 canary와 완료 조건

Synthetic test와 독립 scoped review가 먼저 통과해야 한다. 그 전에는 live credential을
읽거나 실제 Claude/Codex를 실행하지 않는다.

승인 뒤 controller가 Claude를 먼저 실행한다. Claude가 PASS일 때만 Codex를 실행한다.
두 runtime 모두 missing/pass matrix를 만족하면 sanitized v2 report와 실행 시각만
validation 문서에 기록한다. 실제 GitHub endpoint는 어떤 단계에서도 사용하지 않는다.

완료 조건은 다음과 같다.

- production probe가 단일-process marker 설계와 report v2를 구현한다.
- Host disk credential copy가 없다는 synthetic test가 통과한다.
- 기존 GitHub confinement regression이 모두 통과한다.
- 전체 repository test와 static checks가 통과한다.
- 독립 리뷰에 Critical/Important finding이 없다.
- 실제 Claude와 Codex canary가 순서대로 PASS한다.

## 16. 검토 후 제외한 접근

### 16.1 별도 supervisor/worker와 authenticated socket protocol

Resource ownership은 강화되지만 process, IPC, peer validation과 report protocol이 새로
생긴다. Issue #32의 compliant-agent canary보다 복잡해 제외한다.

### 16.2 단일 process의 완전한 signal state machine

모든 signal/bytecode 경계의 ownership을 증명하려다 현재 결함과 큰 test matrix가
생겼다. Credential을 host disk에 남기지 않는 쪽이 더 단순해 제외한다.

### 16.3 Runtime output secret classifier

진단 정보는 늘지만 임의 encoding, split token, false positive와 성능 문제를 계속
소유해야 한다. Raw output과 파생값을 공개하지 않으면 필요하지 않아 제거한다.

### 16.4 systemd 또는 외부 service supervisor

강한 lifecycle 보장은 가능하지만 새로운 설치/운영 dependency가 생겨 Issue #32
범위를 넘는다.
