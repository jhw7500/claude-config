# pre-PR Tribunal 보고서 검증 및 지연 계측 설계

- 날짜: 2026-09-09
- 대상: GitHub Issues #109, #111, #112
- 상태: 대화 설계 승인 완료, 문서 검토 대기
- 기준 커밋: `df1285dbb7655717af3d60c7fc098715a29203dd`
- 지원 런타임: Claude Code, Codex CLI

## 1. 설계 요약

현재 tribunal 보고서는 실행 출력에 흔한 LF와 TAB까지 일반 제어문자로 거부한다.
세 reviewer가 정상 종료해도 마지막 `finalize`에서야 이 오류가 드러나며, 파일이
ambient `umask`의 영향을 받아 `0644`로 만들어지면 그보다 먼저 `FILE_UNSAFE`로
실패한다. 동시에 어느 단계가 오래 걸렸는지를 재현할 bounded 기록이 없다.

이 변경은 보고서 JSON shape과 PASS/FAIL 의미를 유지하면서 세 경계를 분리한다.
첫째, decoded `stdout_excerpt`와 `stderr_excerpt`에만 LF(U+000A)와 TAB(U+0009)을
허용한다. 명령, finding 본문, 경로와 나머지 필드는 기존처럼 모든 `Cc`/`Cs`를
거부한다. 둘째, controller는 reviewer 응답 bytes를 재직렬화하지 않고 canonical
inbox에 저장하며, 저장 primitive가 `fchmod(0600)`을 적용한다. 각 reviewer가
종료되는 즉시 finalizer와 같은 parser로 검증하고, `finalize` 직전에는 세 파일의
type, owner, symlink 부재, 정확한 mode와 raw digest를 각각 다시 확인한다. 최종화는
그 뒤에도 세 보고서를 다시 전부 parse한다.

셋째, `.review/telemetry.json`에 snapshot과 contract version에 결속된 bounded span
ledger를 별도로 둔다. snapshot/preflight, view 생성/정리, reviewer별 dispatch 대기와
전체 실행, 보고서 검증, finalize, recovery/retry를 success, failure, timeout,
incomplete, clock anomaly로 기록한다. 원시 출력, secret, 자유 형식 오류문과 absolute
home path는 저장하지 않는다. 이 ledger는 verdict나 PR hook의 입력이 아니며 손상,
초과 또는 기록 실패가 기존 gate 결과를 바꾸지 않는다.

## 2. 문제와 관측 근거

### 2.1 보고서 text 계약 충돌

`model._text`는 NFC가 아니거나 Unicode category가 `Cc` 또는 `Cs`인 모든 문자열을
거부한다. JSON escape `\n`과 `\t`는 decode 후 각각 LF와 TAB이 되므로 현재
`executions[].stdout_excerpt`와 `stderr_excerpt`도 `TEXT_INVALID`가 된다. Issue
#109의 실제 round에서는 A 6건, B 14건, C 9건이 이 조건에 걸렸고 세 보고서가 모두
폐기됐다.

출력 excerpt는 bounded 실행 증거를 담는 필드다. 여러 줄 출력을 성실히 보존할수록
실패 확률이 높아지는 현재 계약은 Reviewer B의 역할과 모순된다. 반대로 command나
path까지 제어문자를 허용하면 shell 표시, 로그 경계와 path 검증을 약화시킨다.

### 2.2 늦은 오류 발견과 파일 안전성

현재 controller는 A/B/C가 모두 terminal이 된 뒤에야 보고서를 쓰고 `finalize`를 한
번 호출한다. 첫 reviewer의 malformed JSON, snapshot mismatch 또는 unsafe file도
가장 늦은 reviewer가 끝난 뒤 발견된다. 또 strict reader는 group/other permission이
있는 파일을 거부하지만 Skill에는 exact mode 생성 계약이 없다.

조기 검증이 finalizer와 다른 parser를 쓰면 두 판정이 drift할 수 있다. 보고서를
검증 과정에서 trim, newline folding 또는 JSON 재직렬화하면 reviewer가 실제로 낸
응답과 finalizer가 읽는 bytes가 달라진다. 따라서 parser 공유와 raw-byte identity가
함께 필요하다.

### 2.3 성능 기준선 부재

Issue #111의 선행 관측에서는 Reviewer B 한 명이 약 30분 걸렸지만, 그 시간이 runtime
dispatch, 탐색, 명령 실행, 보고서 생성 또는 final validation 중 어디에 쓰였는지
분리할 수 없다. 최적화 전후에 같은 snapshot과 contract를 비교할 수 없고, timeout과
recovery 비용도 사라진다.

## 3. 목표

1. 실행 excerpt의 정상적인 LF/TAB을 허용하되 text/path/command 방어 범위를 넓히지
   않는다.
2. 각 reviewer 종료 즉시 최종화와 동일한 strict schema 결과를 얻는다.
3. reviewer 응답의 exact bytes와 안전한 파일 속성을 최종화까지 보존한다.
4. report validation은 report, verdict와 reviewer slot을 수정하지 않는다.
5. 모든 최종 PASS는 여전히 세 terminal report의 최종 재검증 뒤에만 가능하다.
6. snapshot-bound 단계별 latency를 bounded local ledger로 자동 수집한다.
7. 실패, timeout, 미완료와 clock anomaly를 명시적으로 남긴다.
8. telemetry 유무나 오류가 기존 gate verdict와 PR hook 동작을 바꾸지 않는다.
9. Claude Code와 Codex가 같은 저장, 검증, 계측 계약을 사용한다.

## 4. 비목표

- reviewer 수, 역할 또는 CRITICAL/HIGH blocker 기준을 바꾸지 않는다.
- invalid report를 자동 수리, 추측, trim, newline folding 또는 재직렬화하지 않는다.
- 한 reviewer만 선택적으로 다시 실행하거나 partial reviewer slot 상태를 만들지 않는다.
- Reviewer B의 evidence bundle, 명령 budget 또는 실행 횟수 제한을 추가하지 않는다.
- prompt framing이나 reviewer blind spot을 이번 변경에서 실험하지 않는다.
- telemetry를 업로드하거나 외부 observability service와 dependency를 추가하지 않는다.
- telemetry를 승인 증거나 security boundary로 사용하지 않는다.
- 기존 post-PR reviewer와 merge gate를 대체하지 않는다.

## 5. 확정 결정

| 항목 | 결정 |
| --- | --- |
| LF/TAB 허용 위치 | decoded `stdout_excerpt`, `stderr_excerpt`만 |
| 계속 거부할 문자 | 두 excerpt의 LF/TAB을 제외한 모든 `Cc`/`Cs` |
| schema parser | `parse_reviewer_report` 하나를 조기 검증과 finalizer가 공유 |
| 보고서 보존 | reviewer terminal response의 exact bytes, 재직렬화 금지 |
| 파일 mode | ambient `umask`와 무관하게 정확히 `0600` |
| 파일 신뢰 조건 | current-user-owned regular non-symlink, canonical inbox path |
| 조기 검증 효과 | 보고서와 verdict/reviewer slot 불변 |
| 최종 검증 | A/B/C 모두 파일 안전성 확인 후 `finalize`가 다시 full parse |
| telemetry 위치 | gitignored `.review/telemetry.json` |
| telemetry 관계 | verdict와 별도 side channel, gate 입력에서 제외 |
| telemetry 시간 | wall clock 표시 + monotonic duration, anomaly 명시 |
| 외부 전송 | 없음 |

## 6. 전체 구조와 데이터 흐름

```text
reviewer A/B/C terminal response
              |
              v
      exact raw-byte capture
              |
              v
  secure canonical report store -----> raw SHA-256
  (owner, regular, no symlink, 0600)
              |
              v
  immediate strict validate-report ----> bounded result/error
              |                           + telemetry span
              v
  wait until all reviewers terminal
              |
              v
  verify A, B, C file safety + digest independently
              |
              v
  finalize: reopen and strict-parse all three reports
              |
              v
       unchanged verdict/gate

Telemetry side channel:
controller/core spans -> bounded atomic ledger -> sanitized summary
                              |
                              +-> never read by verdict or PR hook
```

Parser correctness와 filesystem trust는 서로 다른 층이다. `validate_report_bytes`는
bytes와 expected reviewer/round/snapshot만 받아 deterministic parse를 수행한다.
stored-report reader는 그 함수를 호출하기 전에 path와 descriptor의 안전성을
검증한다. 같은 parser를 공유하되 stdin/in-memory 검증을 파일 mode 검사와 혼동하지
않는다.

## 7. Text 및 JSON 계약

### 7.1 필드별 정책

| 필드군 | NFC | UTF-8 byte 상한 | LF/TAB | 다른 `Cc`/`Cs` | 추가 검사 |
| --- | --- | --- | --- | --- | --- |
| `stdout_excerpt`, `stderr_excerpt` | 필수 | 기존 evidence 상한 유지 | 허용 | 거부 | secret, absolute home path |
| `command` | 필수 | 기존 command 상한 유지 | 거부 | 거부 | command 계약 유지 |
| title/rationale/acceptance/claim/reason | 필수 | 기존 field 상한 유지 | 거부 | 거부 | 기존 schema 유지 |
| path/ref/id 및 나머지 text | 필수 | 기존 field 상한 유지 | 거부 | 거부 | 기존 전용 validator 유지 |

excerpt validator는 일반 `_text`를 느슨하게 만들지 않는다. 전용
`_execution_excerpt`가 UTF-8/NFC/byte bound를 먼저 확인하고 문자별로 LF와 TAB만
예외 처리한 뒤 기존 secret 및 absolute-home scan을 그대로 적용한다. CR(U+000D),
NUL(U+0000), ESC(U+001B), DEL(U+007F)을 포함한 나머지 `Cc`와 모든 `Cs` 문자는
계속 `TEXT_INVALID`다. 기존에 허용되던 다른 Unicode category의 의미는 바꾸지 않는다.

### 7.2 JSON transport와 예시

JSON 문법상 string 안의 physical unescaped newline은 여전히 `JSON_INVALID`다. 다음
예시는 JSON bytes에는 escape가 있고 decoded excerpt에는 LF/TAB이 있는 정상 입력이다.

```json
{"stdout_excerpt":"case A\n\t1 passed","stderr_excerpt":""}
```

literal `\\n` 두 문자를 강제하거나 여러 줄 출력을 ` | `로 접는 규칙은 제거한다.
Reviewer 문서는 multiline evidence를 JSON escape로 표현하되 원래 의미를 왜곡하지
말라고 안내한다. 모든 예시는 schema의 나머지 필수 key를 갖춘 완전한 report 예시와
field-only 예시를 명확히 구분한다.

### 7.3 Diff digest 재현 계약

`context`는 reviewer가 자신의 detached view에서 snapshot digest를 재현할 수 있도록
다음을 structured `diff_contract`로 제공한다.

- recipe version
- exact merge-base SHA와 HEAD SHA로 만든 `<merge-base>..<head>` revision range
- argument vector: `git diff --binary --no-ext-diff --no-textconv --full-index <range>`
- stdout의 exact bytes를 SHA-256으로 digest한다는 규칙
- locale/pager/config 영향을 제거하는 core의 sanitized Git environment contract

shell command 문자열 하나를 제공하지 않고 argument array와 environment key/value를
제공해 quoting ambiguity를 피한다. absolute executable/home path와 inherited `GIT_*`
값은 노출하지 않는다. core와 문서가 같은 constant에서 recipe version과 argv option을
사용하도록 contract test를 둔다.

## 8. Exact report 저장 계약

### 8.1 Canonical 위치

현재 round가 `N`이고 reviewer가 `X`일 때 대상은 오직
`.review/inbox/round-N/X.json`이다. caller가 arbitrary path, round, HEAD 또는 diff
digest를 선택하지 않는다. 이 값은 현재 verdict에서 파생한다.

### 8.2 Raw bytes 불변식

controller가 terminal response를 받은 뒤 다음을 지킨다.

1. response bytes를 trim, Unicode normalize, code-fence 제거 또는 JSON parse/serialize
   하지 않는다.
2. max report byte bound까지만 읽고 초과 입력은 `REPORT_TOO_LARGE`로 종료한다.
3. descriptor-anchored safe store primitive로 동일 bytes를 canonical path에 한 번만
   기록한다.
4. 저장 전후 raw SHA-256을 비교하고 mismatch면 `REPORT_BYTES_MISMATCH`로 중단한다.
5. invalid JSON이나 schema 오류여도 이미 받은 original file은 수정하지 않는다.
6. 일반 실행에서는 기존 canonical file을 덮어쓰지 않는다. 예외는 기존
   pending-round 계약에 따라 사용자가 명시적으로 요청한 A/B/C 전체 재실행 recovery다.
   이 경우 세 새 terminal response가 모두 준비된 뒤에만 recovery 전용 atomic replace를
   사용한다. 한 slot만 재실행하거나 이전 peer 결과를 재사용하는 동작은 #115 범위다.

safe store는 `.review`와 `inbox/round-N`을 current user 소유의 mode `0700` directory로
열고 symlink를 따라가지 않는다. report는 ambient `umask`와 무관하게 descriptor에
`fchmod(0600)`을 수행하며, complete bytes와 metadata를 확인한 뒤에만 publish한다.
partial temporary file은 final report로 취급하지 않는다.

### 8.3 Storage CLI 경계

`store-report --reviewer <A|B|C>`는 bounded raw stdin을 한 번 읽어 위 safe store를
수행한다. expected round와 canonical target은 current verdict에서 파생하며 caller가
path를 전달하지 않는다. 성공 시 reviewer, round, `status=stored`, raw SHA-256만
bounded JSON으로 반환한다. JSON/schema validation은 저장 bytes를 바꾸지 않도록
별도 `validate-report`가 담당한다. 저장 중 발견한 oversized input, unsafe directory,
기존 target 또는 digest mismatch는 stable error로 끝난다.

기존 pending-round full-panel recovery에서만 `store-report`의 명시적 recovery replace
mode를 허용한다. controller는 사용자 개입, unchanged bound snapshot, all-pending
verdict, A/B/C 전체 fresh rerun과 세 terminal response 준비를 먼저 확인한다. replace는
각 새 response bytes를 고치지 않고 atomic하게 저장하며 exact `0600`을 다시 적용한다.
일반 실행, 일부 panel만의 rerun 또는 이전 peer report 재사용에는 이 mode를 쓰지 않는다.

### 8.4 Finalize 직전 검증

controller는 `finalize`를 호출하기 직전에 A, B, C를 각각 다시 열어 다음을 모두
확인한다.

- canonical path와 expected round/reviewer 일치
- regular file이며 symlink가 아님
- `st_uid == geteuid()`
- `stat.S_IMODE(st_mode) == 0o600`
- stored SHA-256이 처음 저장한 raw SHA-256과 일치
- current HEAD/diff에 대해 strict parser가 여전히 valid

한 파일이라도 실패하면 `finalize`를 호출하지 않는다. 세 검증이 성공해도 TOCTOU를
피하기 위해 `finalize_round`는 descriptor-anchored open, metadata 검사와
`parse_reviewer_report`를 다시 수행한다. 조기 validation 결과를 승인 cache로 쓰지
않는다.

## 9. 조기 validation API

### 9.1 Core API

하나의 pure function이 validation 의미의 source of truth다.

```text
validate_report_bytes(
  raw_bytes,
  expected_reviewer,
  expected_round,
  expected_snapshot
) -> validated report + raw_sha256
```

이 함수는 filesystem이나 verdict를 쓰지 않는다. 내부에서 기존
`parse_reviewer_report`를 호출하므로 malformed JSON, text, reviewer, round, snapshot,
execution/finding/claim 오류가 finalizer와 동일한 stable code로 끝난다.

### 9.2 CLI 표면

공개 CLI에는 reviewer-specific
`validate-report --reviewer <A|B|C> --source <stdin|stored>` 진입점을 추가한다.

| mode | 입력 | filesystem 검사 | report/verdict/slot mutation |
| --- | --- | --- | --- |
| in-memory/stdin | bounded raw stdin | 없음 | 없음 |
| stored | verdict에서 파생한 canonical file | owner/type/symlink/mode 포함 | 없음 |

expected round, HEAD와 diff SHA-256은 caller 인자가 아니라 current verdict에서 읽는다.
성공 stdout은 schema, reviewer, round, `status=valid`, raw SHA-256만 담은 bounded JSON이다.
실패 stderr는 기존 `PRE_PR_TRIBUNAL:<STABLE_CODE>` 형식을 사용하며 report 내용은
반사하지 않는다.

CLI wrapper가 #111 telemetry event를 기록하더라도 validation 대상 report,
`.review/verdict.json`과 reviewer slot은 바꾸지 않는다. telemetry 기록 실패는
validation의 성공/실패 값을 덮어쓰지 않는다.

### 9.3 Controller 순서

reviewer들은 계속 서로의 report를 보지 않는다. A가 먼저 끝나면 A만 저장·검증하고
B/C 결과를 기다리는 동안 bounded 오류를 확정할 수 있다. 이미 실행 중인 peer를
validation 목적으로 취소하거나 A의 결과를 전달하지 않는다. invalid report는
자동 수리하지 않고 finalization을 금지한다.

## 10. Telemetry 모델

### 10.1 저장 및 binding

`.review/telemetry.json`은 verdict와 독립된 schema 1 ledger다. 각 run은 random bounded
`run_id`와 다음 binding을 가진다.

```json
{
  "repository":"owner/repository",
  "base_ref":"master",
  "base_sha":"<40-hex>",
  "head_ref":"refs/heads/task/example",
  "head_sha":"<40-hex>",
  "merge_base_sha":"<40-hex>",
  "diff_sha256":"<64-hex>",
  "contract":{"report_text":2,"diff_recipe":1,"telemetry_schema":1}
}
```

snapshot capture가 성공하기 전에는 repository/base request와 candidate HEAD만 가진
binding object의 `status=pending` run을 허용한다. capture가 성공하면 exact binding을 한 번
채우고 이후 수정하지 않는다. safe repository root를 확정하기 전에 난 오류는 local
ledger를 둘 신뢰할 위치가 없으므로 bounded stderr에만 남긴다.

report/verdict JSON shape는 바뀌지 않아 schema 1을 유지한다. decoded excerpt 의미가
달라졌음을 `report_text=2`로 별도 식별한다. telemetry 비교는 exact snapshot과 이
contract tuple이 같은 run끼리 기본으로 수행하고, 다른 contract 비교는 차이를
명시한다.

### 10.2 Stage와 dimension

| stage | reviewer dimension | 측정 범위 |
| --- | --- | --- |
| `snapshot_preflight` | 없음 | safe root 확인부터 snapshot/verdict begin 결과까지 |
| `view_create` | A/B/C | detached reviewer view 생성 요청부터 사용 가능까지 |
| `reviewer_dispatch_wait` | A/B/C | dispatch 요청부터 runtime가 작업을 수락할 때까지 |
| `reviewer_total` | A/B/C | 수락부터 terminal response 수신까지 |
| `report_store` | A/B/C | exact-byte 저장과 metadata/digest 확인 |
| `report_validation` | A/B/C | terminal report의 immediate strict validation |
| `finalize` | 없음 | pre-final checks와 finalizer 결과를 별도 child span으로 포함 |
| `view_cleanup` | A/B/C | clean check부터 정상 제거 또는 보존 결정까지 |
| `recovery_retry` | optional A/B/C | 이전 incomplete span 인식부터 bounded recovery 결과까지 |

runtime가 dispatch acceptance 시점을 제공하지 않으면
`reviewer_dispatch_wait`는 `incomplete`와 `reason_code=RUNTIME_SIGNAL_UNAVAILABLE`로
남기고 dispatch-to-terminal 전체를 `reviewer_total`에 기록한다. 측정할 수 없는
구간을 0ms로 꾸미지 않는다.

### 10.3 Span shape와 상태

각 span은 bounded `span_id`, stage, optional reviewer, attempt number, wall-clock
`started_at`/`ended_at`, monotonic start/end, derived `duration_ms`, terminal outcome과
ASCII `reason_code`만 가진다. 자유 형식 message, command, stdout/stderr, finding,
claim, token content, absolute path와 environment value는 저장하지 않는다.

```text
running -> success
        -> failure
        -> timeout
        -> incomplete
        -> clock_anomaly
```

terminal span은 다시 열거나 덮어쓰지 않는다. recovery/retry는 새 attempt span이다.
process가 종료되어 `running`이 남으면 다음 controller가 이를 `incomplete`로 닫고 새
`recovery_retry` span을 연다. wall clock이 역행하거나 monotonic end가 start보다
작으면 duration을 음수/0으로 보정하지 않고 `duration_ms=null`, outcome
`clock_anomaly`로 남긴다. timeout은 실제 관측 duration과 stable timeout reason을
함께 가진다.

### 10.4 Bound와 안전한 저장

ledger는 최대 run 수, run별 span 수, string byte 길이와 전체 file byte 크기를
상수로 제한한다. 새 run을 위한 공간이 없으면 가장 오래된 terminal run부터
결정적으로 제거하되 active/incomplete run을 조용히 버리지 않는다. active data만으로
상한에 도달하면 새 span 대신 해당 run을 `telemetry_incomplete`로 표시할 수 있는
reserved terminal marker를 사용한다.

writer는 verdict store와 같은 descriptor-anchored, no-symlink, current-owner,
atomic-replace primitive를 사용하고 결과 파일에 `fchmod(0600)`을 적용한다. malformed,
unsafe 또는 oversized 기존 telemetry는 bounded telemetry error를 내고 덮어쓰지
않는다. verdict reader, finalizer와 PR hook은 이 파일을 읽지 않으므로 이 오류가 PASS
또는 FAIL을 만들지 않는다.

### 10.5 Sanitized summary

summary는 raw span을 그대로 내보내지 않고 snapshot/contract, stage별 count와
duration, reviewer별 total, outcome count와 anomaly code만 반환한다. immediate
validation의 개선을 비교할 때는 run start 기준의 bounded relative milestone만 추가해
첫 invalid report 탐지와 마지막 reviewer terminal 사이의 `wait_all_delay_ms`를
계산한다. raw wall timestamp, monotonic absolute value와 span ID는 내보내지 않는다.
다음은 형식 예시이며 실제 기준선 수치가 아니다.

```json
{
  "schema":1,
  "binding":{"diff_sha256":"<64-hex>","contract":{"report_text":2,"diff_recipe":1}},
  "reviewers":{"A":{"total_ms":1200},"B":{"total_ms":4200},"C":{"total_ms":900}},
  "stages":{"report_validation":{"count":3,"total_ms":18}},
  "outcomes":{"success":12,"failure":0,"timeout":0,"incomplete":0,"clock_anomaly":0},
  "early_detection":null
}
```

invalid report run에서는 `early_detection`이 reviewer, stable validation reason,
`detected_elapsed_ms`, nullable `all_reviewers_terminal_elapsed_ms`와 nullable
`wait_all_delay_ms`만 가진다. 필요한 milestone이 incomplete이거나 clock anomaly면
비교값을 0으로 꾸미지 않고 null로 남긴다.

고정 lockfile-only fixture에서 성능 최적화 전 automatic baseline summary와 최적화 후
summary를 같은 binding/contract 조건으로 보존해 Reviewer B 병목 구간을 식별한다.
repository source, report body와 명령 출력은 예시에 포함하지 않는다.

## 11. 원자성, 실패와 gate 독립성

report store와 telemetry store는 같은 `.review` trust root를 사용하지만 서로 다른
파일과 transaction이다. report 저장/검증 실패는 report contract 실패이므로
finalization을 막는다. telemetry 저장/요약 실패는 관측 실패이며 report나 verdict
판정을 바꾸지 않는다.

| 실패 | 결과 |
| --- | --- |
| report malformed/text/snapshot mismatch | 즉시 stable error, original 보존, finalize 금지 |
| report unsafe/wrong digest | finalize 호출 전 중단 |
| finalizer 재검증 실패 | 기존처럼 fail-closed |
| telemetry timeout/failure | span terminal outcome, gate 불변 |
| telemetry malformed/unsafe/oversized | bounded warning, 기존 파일 보존, gate 불변 |
| process crash 중 telemetry update | 이전 atomic file 또는 새 complete file 중 하나만 존재 |
| process crash 중 report store | partial file은 canonical report로 publish하지 않음 |

telemetry failure를 무시했다는 이유로 PASS를 만들어서는 안 되지만, telemetry 자체를
PASS 전제조건으로 삼아서 기존 tribunal을 새롭게 차단해서도 안 된다. 구현은 verdict
commit path와 telemetry update exception boundary를 분리하고, regression test로 두
방향을 모두 고정한다.

## 12. Stable 오류 코드

기존 parser code를 그대로 전달하고 새 storage/telemetry code만 추가한다.

| code | 의미 |
| --- | --- |
| `REPORT_FILE_EXISTS` | canonical reviewer slot을 덮어쓰려 함 |
| `FILE_UNSAFE` | 기존 code 유지; type, owner, symlink 또는 exact mode 위반 |
| `REPORT_BYTES_MISMATCH` | captured bytes와 stored bytes digest 불일치 |
| `TELEMETRY_INVALID` | telemetry schema/state transition 위반 |
| `TELEMETRY_FILE_UNSAFE` | telemetry type, owner, symlink 또는 exact mode 위반 |
| `TELEMETRY_TOO_LARGE` | configured byte/span/run bound 초과 |
| `TELEMETRY_CLOCK_ANOMALY` | monotonic 또는 wall clock 순서 위반 |

오류 문자열은 최대 64 ASCII characters의 enum/code이고 user-controlled content를
붙이지 않는다. validation 성공 payload와 telemetry summary도 기존 CLI JSON-only
규칙을 따른다.

## 13. 기존 상태와 호환성

- report와 verdict shape는 schema 1을 유지한다. LF/TAB 허용은 previously invalid
  input을 좁게 수용하는 parser 변화다.
- 기존 PASS verdict와 telemetry가 없는 repository는 PR hook에서 이전과 동일하게
  판정된다.
- upgrade 시 이미 in-progress인 round는 current parser로 검증할 수 있다. 첫
  telemetry event에서 `started_late=true` run을 만들고 관측하지 못한 앞 단계는
  `incomplete`로 표시한다.
- 기존 `0644` report를 자동 chmod하거나 rewrite하지 않는다. controller가 직접 받은
  current response를 secure store로 만든 경우만 신뢰한다.
- 기존 pending-round recovery는 explicit recovery replace mode로 A/B/C 전체 새 응답만
  교체한다. 이전 valid peer 재사용과 단일 slot 교체는 허용하지 않는다.
- `finalize`의 세 reviewer all-or-nothing 상태 모델은 유지한다. partial slot migration은
  #115가 별도 schema/state 설계로 다룬다.
- telemetry schema가 future reader보다 새로우면 telemetry만 unavailable이고 verdict
  및 gate는 계속 기존 schema로 판정한다.

## 14. Skill 및 문서 계약

Claude와 Codex용 설치 산출물은 같은 canonical `pre-pr-tribunal` Skill과 reference를
공유한다. Step 7의 controller 계약을 다음 순서로 바꾼다.

1. reviewer terminal response exact bytes를 secure canonical report store에 기록한다.
2. store가 각 파일에 exact `0600`을 설정했음을 확인한다.
3. 해당 reviewer를 즉시 strict validation하고 결과를 telemetry에 남긴다.
4. invalid report를 수리하거나 다시 serialize하지 않는다.
5. 세 reviewer가 terminal이 된 뒤 A/B/C의 type/owner/mode/digest를 각각 재검증한다.
6. 한 check라도 실패하면 `finalize`를 호출하지 않는다.
7. 모두 성공하면 finalizer가 세 보고서를 다시 parse하게 한다.

`report-schema.md`는 LF/TAB exception, JSON escape 예시, 계속 거부되는 control 문자와
raw-byte 금지 변환을 설명한다. reviewer A/B/C reference는 peer privacy와 report
formatting 규칙을 동일하게 인용한다. `context`의 `diff_contract`를 재현의 source of
truth로 사용하고 임의의 `git diff` 조합을 추측하지 않는다.

## 15. 검증 전략

### 15.1 Model unit tests

- stdout/stderr excerpt의 LF, TAB과 두 문자의 조합을 수용한다.
- 같은 문자를 command, rationale, claim reason과 path에 넣으면 거부한다.
- excerpt의 CR, NUL, ESC, DEL, surrogate와 다른 `Cc`/`Cs`를 거부한다.
- NFC, UTF-8 byte 상한, secret와 absolute home path 검사를 그대로 적용한다.
- physical unescaped newline JSON은 `JSON_INVALID`, escaped newline은 정상 parse된다.

### 15.2 Store 및 validation tests

- stdin과 stored validation이 같은 report object/error code를 낸다.
- A/B/C 각각 malformed JSON, reviewer/round/HEAD/diff mismatch를 즉시 탐지한다.
- validation 전후 verdict bytes와 reviewer slot이 동일하다.
- captured/stored/finalizer input의 SHA-256이 동일하다.
- `umask` 000, 022, 077에서도 report mode가 정확히 `0600`이다.
- symlink, FIFO, directory, wrong owner, `0644`, swapped file과 overwrite를 거부한다.
- A/B/C pre-final check 중 하나가 실패하면 finalizer가 호출되지 않는다.
- 조기 success 뒤 파일이 바뀌면 finalizer의 재검증이 실패한다.

### 15.3 Telemetry tests

- fake wall/monotonic clock으로 모든 stage duration을 결정적으로 검증한다.
- failure, timeout, process interruption, recovery attempt와 late-start migration을
  기록한다.
- wall/monotonic 역행은 null duration과 clock anomaly가 되며 음수로 저장되지 않는다.
- run/span/string/file 상한, pruning과 reserved incomplete marker를 검증한다.
- atomic replace failure 뒤 이전 complete ledger가 남는다.
- telemetry file이 항상 regular, current-owner, non-symlink, `0600`임을 검증한다.
- telemetry가 missing/malformed/unsafe/oversized/locked여도 동일 report input의
  verdict와 PR hook 결과가 byte-for-byte 동일하다.
- summary가 command, stdout/stderr, secret sentinel과 absolute home path를 포함하지
  않는다.

### 15.4 Contract 및 통합 tests

- Claude/Codex Skill 모두 immediate validation, no-repair, exact `0600` 및 pre-final
  verification 문구를 포함한다.
- `context.diff_contract` recipe로 계산한 digest가 snapshot digest와 같다.
- 세 reviewer를 서로 다른 순서로 완료해도 각 validation 시점과 최종 verdict가 같다.
- fixed lockfile-only fixture에서 reviewer별/stage별 summary를 생성하고 가장 긴 B
  구간을 식별할 수 있다.
- 전체 `pytest` suite와 기존 runtime canary를 실행해 PR hook 동작 불변을 확인한다.

## 16. Issue별 완료 조건 매핑

| Issue | 이 설계의 충족 지점 |
| --- | --- |
| #109 | excerpt-only LF/TAB, exact 0600 store, diff recipe 공개, parser/store/Skill tests |
| #111 | snapshot/contract-bound bounded spans, anomaly/failure 상태, sanitized baseline summary, gate 독립성 |
| #112 | shared strict parser, immediate reviewer validation, no mutation/repair, final revalidation, 양 runtime contract |

## 17. 후속 이슈와 명시적 경계

이 묶음은 후속 성능/재실행 변경의 관측 및 검증 기반만 만든다.

- #113은 Reviewer B evidence bundle 생성 방식을 바꾼다. 이번 report schema 수정에
  포함하지 않는다.
- #114는 Reviewer B command/time budget을 도입한다. #111 baseline 뒤 별도 수치로
  결정한다.
- #115는 snapshot-bound selective rerun과 partial reviewer slot을 요구한다. 현재
  all-pending/all-complete verdict state를 유지하므로 별도 migration이 필요하다.
- #121은 prompt framing A/B 실험이다. telemetry가 준비된 뒤 같은 snapshot/contract
  비교로 수행한다.

따라서 이 설계의 stop condition은 LF/TAB 계약, 조기 strict validation, exact secure
report lifecycle과 비침투적 telemetry가 검증되는 시점이다. reviewer behavior,
budget 또는 selective rerun을 구현하기 시작하면 범위를 벗어난다.
