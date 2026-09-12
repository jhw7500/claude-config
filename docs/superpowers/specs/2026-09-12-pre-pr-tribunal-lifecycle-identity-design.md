# Pre-PR Tribunal lifecycle identity 설계

- 날짜: 2026-09-12
- 대상 Issue: [#115](https://github.com/jhw7500/claude-config/issues/115)
- 상태: 설계 승인 완료, 구현 계획 작성 완료
- 설계 기준 커밋: `a25b7089a0d36d82443b2ada2c705b8a28aa1728`
- 선행 설계: `2026-09-10-pre-pr-tribunal-slot-recovery-design.md`

## 1. 요약

선택적 reviewer 복구 telemetry는 같은 snapshot과 round의 과거 실행 기록을 읽어
현재 요청이 최초 실행인지 재실행인지 계산한다. 현재 구현은 가장 최근에 남아 있는
`new_round` run을 lifecycle 경계로 사용한다. 그러나 bounded ledger가 그 run을
축출하거나 새 `begin`의 telemetry 생성만 실패하면 현재 verdict와 과거 경계를
연결할 내구성 있는 식별자가 없다. 이때 과거 요청을 현재 lifecycle의 요청으로 잘못
재사용하면서 `accounting_complete=true`를 반환할 수 있다.

이 설계는 verdict schema v3에 임의의 128-bit `lifecycle_id`를 추가하고 telemetry
schema v3의 bound run이 같은 값을 갖게 한다. 복구 집계는 snapshot이나 list 순서가
아니라 이 ID로 현재 lifecycle만 선택한다. 현재 ID의 telemetry 기록이 없으면 요청
기반 수치를 추측하지 않고 `accounting_complete=false`로 둔다.

함께 다음 두 schema v2 부채를 정리한다.

1. 새 round는 `Invocation("new_round", (), ())`를 저장하지 않고 bound run과
   invocation 부재에서 `new_round`를 파생한다.
2. schema v3 terminal run은 `ended_monotonic_ns`를 반드시 가지며 running run은
   반드시 `null`이어야 한다.

Verdict와 telemetry의 PASS/FAIL 관계는 바뀌지 않는다. Telemetry는 계속 관측용
side channel이며 기록 실패가 tribunal gate 결과를 변경하지 않는다.

## 2. 문제와 재현 조건

### 2.1 Lifecycle marker 축출

`_prior_request_history`는 같은 snapshot과 round의 run을 모은 뒤 마지막
`new_round`부터 잘라 현재 lifecycle을 추정한다. `_append_run`은 16개 상한에서
terminal complete run 중 wall-clock `started_at`이 가장 이른 항목을 축출한다.

따라서 다음 조건에서 현재 경계가 사라질 수 있다.

- 이전 lifecycle의 incomplete run은 축출할 수 없다.
- 현재 `new_round` run은 terminal complete라 축출 후보가 된다.
- wall clock rollback 또는 동일 시각 때문에 현재 run이 가장 오래된 후보가 된다.
- marker가 없어진 뒤 같은 snapshot의 과거 요청이 다시 집계된다.

Ledger append 순서만 사용해도 marker 자체가 없어지면 어느 resume이 어느
lifecycle에 속했는지 증명할 수 없다.

### 2.2 새 begin의 telemetry 생성 실패

`begin`은 telemetry를 관측 side channel로 취급한다. `create_run`이
`TELEMETRY_TOO_LARGE` 등으로 실패해도 primary `begin_round`는 정상 진행한다. 이는
의도된 분리다. 하지만 같은 snapshot의 과거 `new_round`가 ledger에 남아 있으면
나중의 `telemetry-resume`이 그것을 현재 경계로 오인할 수 있다.

현재 verdict에는 telemetry run ID나 별도 lifecycle generation이 없다. Snapshot,
round, runtime과 초 단위 `created_at`만으로는 빠르게 반복된 begin이나 clock rollback을
구별할 수 없으므로 휴리스틱 비교는 충분하지 않다.

### 2.3 중복 상태와 불완전한 terminal 시간

Schema v2는 새 round에도 resume 전용 상태와 같은 `Invocation` 객체를 저장한다.
또한 CLI는 monotonic 종료 시각을 전달하지만 core API와 parser는 terminal schema-v2
run의 `ended_monotonic_ns=null`을 허용한다. 그 결과 같은 상태를 두 방식으로 표현하고,
새 writer가 만든 terminal run에도 invocation elapsed가 불필요하게 unknown일 수 있다.

## 3. 목표

- 현재 verdict lifecycle과 telemetry run을 내구성 있는 ID로 결속한다.
- ledger 축출과 wall-clock 순서에 의존하지 않고 prior request history를 선택한다.
- 현재 lifecycle의 telemetry 시작 기록이 없으면 요청 수치를 fail-closed로 만든다.
- resume마다 이전 시도 집합을 전달해 marker 축출 뒤에도 재실행 분류를 유지한다.
- 새 round의 중복 invocation metadata를 제거한다.
- 새 schema terminal run의 monotonic 종료 시각을 필수화한다.
- verdict v1/v2와 telemetry v1/v2를 rewrite 없이 계속 읽는다.
- telemetry 오류가 verdict, report, receipt 또는 gate를 변경하지 않게 한다.

## 4. 비목표

- Telemetry를 review 권한이나 merge 승인 증거로 사용하지 않는다.
- Ledger 상한 16개와 span 상한 128개를 늘리지 않는다.
- 과거 v1/v2 run에 lifecycle identity나 monotonic 시간을 추정해 채우지 않는다.
- 기존 v1/v2 파일을 읽는 과정에서 자동 migration하거나 rewrite하지 않는다.
- Report schema, reviewer 수, CRITICAL/HIGH 차단 기준을 바꾸지 않는다.
- Wall-clock timestamp를 lifecycle identity로 승격하지 않는다.
- 별도 lifecycle pointer 파일이나 두 번째 verdict 정본을 만들지 않는다.

## 5. 설계 선택

### 5.1 선택: verdict가 소유하는 임의 lifecycle ID

새 verdict lifecycle을 만들 때 `secrets.token_hex(16)`으로 32자리 lowercase hex ID를
생성한다. 이 값은 보안 토큰이나 권한 증명이 아니라 한 verdict lifecycle의 불변
generation 식별자다. 같은 round를 같은 snapshot에서 다시 시작해도 새 ID를 만든다.

Verdict가 ID를 소유해야 하는 이유는 telemetry가 실패해도 primary lifecycle은
계속 존재하기 때문이다. Resume은 현재 verdict에서 ID를 읽고, ledger에 같은 ID가
없다는 사실을 관측 누락으로 안전하게 판정할 수 있다.

### 5.2 기각: marker pinning만 적용

현재 `new_round`를 축출 대상에서 제외하면 marker 축출은 막을 수 있다. 그러나 새
begin의 telemetry 생성이 실패했을 때 남아 있는 marker가 현재 verdict의 것인지
과거 verdict의 것인지 증명하지 못한다. 두 MEDIUM 중 하나만 해결하므로 기각한다.

### 5.3 기각: timestamp 또는 ledger 순서 휴리스틱

Verdict `created_at`은 초 단위이고 telemetry는 primary begin 직전에 시작한다. 빠른
반복 begin, 동일 초, wall-clock rollback과 부분 기록에서는 시간 구간이나 최신 list
위치가 identity가 될 수 없다. 모호한 경우만 fail-closed로 처리해도 정상 fast path를
자주 unknown으로 만들므로 채택하지 않는다.

### 5.4 기각: 별도 lifecycle pointer 파일

별도 파일은 verdict와 두 번째 정본을 만든다. Verdict write와 pointer write 사이의
부분 실패를 다시 정의해야 하며, telemetry 실패가 primary transaction을 막지 않아야
한다는 기존 원칙과 충돌한다. Lifecycle ID를 verdict 자체에 두면 이 문제가 없다.

## 6. Schema v3

### 6.1 Verdict schema v3

Verdict v3는 최상위에 다음 필드를 추가한다.

```json
{
  "schema": 3,
  "lifecycle_id": "0123456789abcdef0123456789abcdef"
}
```

불변식은 다음과 같다.

- v3 verdict의 모든 상태는 정확히 하나의 유효한 `lifecycle_id`를 가진다.
- reviewer 제출, attempt 기록, seal, finalize 등 같은 lifecycle의 모든 전이는 ID를
  그대로 보존한다.
- 새로운 round 또는 같은 snapshot의 새로운 round-1 begin은 새 ID를 생성한다.
- caller가 CLI 인자로 ID를 주입할 수 없다.
- 테스트만 deterministic token factory를 dependency로 전달할 수 있다.

Parser는 verdict v1, v2, v3를 읽는다. v1/v2에는 읽기만으로 ID를 만들어 붙이지
않는다. 기존 pending v2 verdict는 명시적 `migrate-v2-pending`을 거친 뒤 복구한다.
Migration은 봉인된 report와 receipt를 재검증해 보존하고 새 lifecycle ID를 발급하되,
과거 telemetry identity는 추정하지 않으므로 첫 v3 resume의 accounting을 unknown으로
시작한다.

### 6.2 Telemetry schema v3

Telemetry v3 run은 최상위에 다음 필드를 추가한다.

```json
{
  "binding": {"contract": {"telemetry_schema": 3}},
  "lifecycle_id": "0123456789abcdef0123456789abcdef",
  "invocation": null,
  "ended_monotonic_ns": null
}
```

상태별 계약은 다음과 같다.

| Run state | `lifecycle_id` | `invocation` | `ended_monotonic_ns` |
| --- | --- | --- | --- |
| pending, 아직 verdict와 미결속 | `null` | `null` | `null` |
| bound 새 round, running | 필수 | `null` | `null` |
| bound resume, running | 필수 | resume metadata | `null` |
| terminal schema v3 | 상태에 따라 위와 동일 | 상태에 따라 위와 동일 | 필수 |

Schema v3 bound run에는 예외 없이 lifecycle ID가 필요하다. Legacy pending verdict는
해당 migration을 먼저 거쳐 v3 ID를 얻는다. Migration 직후에는 같은 ID의 prior run이
없으므로 첫 resume이 `telemetry_incomplete=true`와
`PRIOR_REQUEST_HISTORY_INCOMPLETE`를 기록하고 요청 기반 수치를 `null`로 만든다.

Telemetry ledger parser는 schema 1, 2, 3을 읽고 각 run의 contract version에 따라
필드를 검증한다. Schema v1/v2 terminal run의 기존 optional monotonic end 의미는
그대로 유지한다. 새 writer는 schema 3만 쓴다.

### 6.3 Invocation 표현

Schema v3 새 round는 invocation 객체를 저장하지 않는다. `_recovery`는 v3 bound
run에서 `invocation is None`이면 `kind=new_round`, reuse와 prior-attempt 집합은 empty로
파생한다. Resume만 verified sealed role과 previously attempted role을 invocation에
저장한다.

Schema v2의 `Invocation("new_round", (), ())`는 호환 reader에서 계속 해석한다.
기존 bytes를 v3 모양으로 rewrite하지 않는다.

### 6.4 Pending verdict migration

설치 contract의 `verdict_schema`가 3이 되면 pending v2 verdict는 그대로는
`CONTRACT_DRIFT`다. 자동 변환 대신 operator가 status를 확인한 뒤 전용
`migrate-v2-pending` 명령을 한 번 실행한다.

명령은 하나의 review lock 안에서 다음 순서를 지킨다.

1. 입력 verdict가 schema 2, `in_progress`이며 snapshot이 현재와 같은지 확인한다.
2. Persisted contract의 report-text와 diff-recipe version이 현재 값과 같은지 확인한다.
3. 각 sealed slot의 canonical file 안전성, exact raw digest, parsed report,
   receipt context digest와 attempt를 다시 검증한다.
4. Pending slot의 attempt count와 last error를 그대로 보존한다.
5. 새 lifecycle ID를 생성하고 contract의 verdict schema만 3으로 올린 v3 verdict를
   원자적으로 쓴다.
6. Migration 결과에 `telemetry_history=unknown`을 반환한다.

Reviewer context digest는 report-text/diff-recipe 계약과 reviewer별 projected
context로 계산되며 verdict schema 번호를 포함하지 않는다. 따라서 위 검증이 성공한
v2 sealed receipt는 내용이나 digest를 다시 쓰지 않고 보존할 수 있다.

Schema 1 pending은 기존 `migrate-legacy-pending` 경로를 유지하되 목적 schema를 3으로
올린다. 인증 가능한 receipt가 없으므로 기존과 같이 canonical bytes를 attempt
evidence로 보존하고 세 slot을 pending으로 만든다. 두 migration 모두 명시적 명령이며
status 조회만으로 verdict를 변경하지 않는다.

## 7. 데이터 흐름

```text
new begin
   |
   +-> create pending telemetry run (관측 실패 허용)
   |
   +-> begin_round -> verdict v3 + random lifecycle_id
   |
   +-> bind telemetry run to snapshot + lifecycle_id

explicit resume
   |
   +-> read and verify current pending verdict
   +-> read current lifecycle_id
   +-> select only telemetry runs with the same lifecycle_id
   |      |
   |      +-> none: prior history incomplete
   |      +-> present: union observed and carried attempts
   |
   +-> append resume run with the same lifecycle_id
          + verified reused roles
          + cumulative previously attempted roles
          + incomplete marker when history is not provable
```

`_prior_request_history`는 더 이상 마지막 `new_round` index를 찾지 않는다. Snapshot과
contract 비교는 방어적 binding 검증으로 유지하지만 lifecycle 선택 조건은 exact
`lifecycle_id` equality다.

## 8. Eviction과 누적 상태

Ledger 상한과 일반 축출 정책은 유지한다. 현재 marker를 특별히 pin하지 않는다.
대신 각 resume run이 현재 lifecycle ID와 그 시점까지의 `previously_attempted` 집합을
보존한다.

첫 resume 전에 marker가 존재하면 그 run의 완전성과 요청을 읽은 뒤 새 resume을
append한다. Append가 marker를 축출하더라도 새 resume이 같은 lifecycle identity와
누적 상태를 가진다. 이후 resume은 남아 있는 같은-ID run만 읽는다.

현재 lifecycle에 incomplete observation이 한 번이라도 있으면 새 resume도
`telemetry_incomplete=true`를 기록한다. 따라서 원인이 된 run이 나중에 축출돼도
`accounting_complete=false`가 최신 run을 통해 계속 전달된다.

## 9. 실패 처리

| 조건 | 동작 |
| --- | --- |
| 새 begin의 telemetry create/bind 실패 | verdict는 계속 진행, projection은 unavailable |
| 현재 lifecycle ID의 prior run 없음 | resume observation은 시작하되 accounting incomplete |
| current verdict가 v1 | `migrate-legacy-pending` 전까지 복구 거부 |
| current verdict가 v2 | `migrate-v2-pending` 전까지 `V2_MIGRATION_REQUIRED`로 복구 거부 |
| v2에서 v3로 migration 직후 | reviewer evidence 보존, prior telemetry accounting unknown |
| ledger가 corrupt/unsafe | 기존처럼 telemetry-resume 거부, primary evidence 불변 |
| ledger가 full이고 resume append 불가 | observation warning, tribunal 복구 권한을 만들지 않음 |
| terminal v3 run에 monotonic end 없음 | `TELEMETRY_INVALID`, bytes rewrite 없음 |
| running v3 run에 monotonic end 존재 | `TELEMETRY_INVALID`, bytes rewrite 없음 |
| lifecycle ID mismatch | 해당 run을 current history에서 제외 |

Telemetry 오류는 report 제출, slot sealing, verdict finalization 결과를 바꾸지 않는다.
반대로 telemetry run이나 lifecycle ID만으로 pending recovery 권한을 얻을 수 없다.
현재 verdict, snapshot, contract와 receipt 검증이 계속 선행한다.

## 10. 구현 경계

예상 변경 범위는 다음과 같다.

- `hooks/pre_pr_tribunal/model.py`: verdict v3 lifecycle field와 strict parser model
- `hooks/pre_pr_tribunal/verdict_store.py`: ID 생성, v1/v2/v3 parsing, 전이 보존,
  explicit v2 pending migration
- `hooks/pre_pr_tribunal/telemetry.py`: telemetry v3, ID 기반 history, v3 시간 불변식
- `hooks/pre_pr_tribunal/cli.py`: begin bind, resume projection과 migration 명령 연결
- `skills/pre-pr-tribunal/SKILL.md`: v3/legacy recovery와 summary 의미 갱신
- `scripts/probe-pre-pr-tribunal.py`: installed v3 lifecycle probe
- 관련 model, recovery, telemetry, probe, skill contract 테스트

Report parser, canonical report storage, receipt digest, finalizer blocker 계산과 Git hook은
변경하지 않는다.

## 11. 테스트 전략

모든 동작 변경은 TDD red-green-refactor 순서로 구현한다.

### 11.1 Lifecycle identity 회귀

- 같은 snapshot과 round의 과거 lifecycle에 B 요청이 있어도 새 ID의 B 최초 요청은
  rerun이 아니다.
- 현재 `new_round` marker를 ledger append가 축출해도 다음 B 요청은 누적 prior
  attempt 때문에 rerun이다.
- marker의 wall-clock이 과거 run보다 이르더라도 ID 기반 결과는 같다.
- 새 begin의 telemetry 생성이 실패한 뒤 resume하면
  `accounting_complete=false`, request-derived counts는 `null`이다.
- 다른 lifecycle ID의 incomplete run은 현재 lifecycle completeness를 오염시키지
  않는다.

### 11.2 Schema 호환성과 strictness

- 새 verdict의 ID는 32자리 lowercase hex이고 모든 전이가 보존한다.
- malformed, missing, duplicate 또는 caller-supplied lifecycle ID를 거부한다.
- verdict v1/v2와 telemetry v1/v2 fixtures는 byte rewrite 없이 읽힌다.
- v2 pending migration은 sealed bytes/receipt와 pending attempt 상태를 보존하고 새 ID를
  발급한다.
- v2 migration이 snapshot, contract 또는 sealed receipt drift를 만나면 원본 verdict와
  report bytes를 변경하지 않는다.
- v3 새 round wire record에는 invocation object가 없고 summary는 `new_round`다.
- v3 resume wire record만 invocation metadata를 갖는다.
- terminal v3 run의 missing monotonic end와 running v3 run의 non-null end를 거부한다.
- terminal v2 run의 기존 null monotonic end는 호환 reader가 계속 허용한다.

### 11.3 통합 검증

- Recovery telemetry test file
- Telemetry/model/skill contract의 영향 범위 suite
- Installed lifecycle probe
- 전체 `pytest -q tests`
- `git diff --check`

Test expectation은 literal lifecycle ID와 observable summary/file bytes로 검증한다.
Production helper로 기대값을 다시 계산하거나 source text 존재만 검사하지 않는다.

## 12. 배포와 호환성

설치 runtime이 먼저 v3 reader/writer를 제공한다. Reader는 v1/v2/v3를 지원하므로
기존 terminal evidence와 pending verdict를 읽을 수 있다. 새 begin만 verdict와
telemetry v3를 쓴다.

기존 v2 pending verdict는 status 확인 후 명시적 `migrate-v2-pending`으로 v3가 된다.
Migration은 reviewer slot과 receipt를 보존하고 새 lifecycle identity를 만들지만,
과거 telemetry와의 연결은 소급 생성하지 않는다. 따라서 migration 직후 첫 resume의
invocation accounting은 unknown이다. 새로운 v3 begin은 처음부터 정확한 ID 기반
집계를 사용한다.

Candidate branch가 아직 v3를 포함하지 않아도 installed runtime이 contract 정본이라는
기존 원칙은 유지한다. 이 변경은 외부 service, network migration 또는 별도 data
backfill을 요구하지 않는다.

## 13. 수용 조건

다음을 모두 만족하면 구현 완료다.

1. 같은 snapshot의 서로 다른 verdict lifecycle이 request history를 공유하지 않는다.
2. Marker 축출과 wall-clock rollback이 재실행 분류를 바꾸지 않는다.
3. 새 begin telemetry 누락 후 resume은 확실한 수치를 만들지 않는다.
4. 새 round invocation 중복 상태가 제거된다.
5. 새 terminal run은 monotonic end를 항상 가진다.
6. Verdict/telemetry v1/v2 입력은 계속 읽히며 자동 rewrite되지 않는다.
7. 명시적 v2 pending migration이 검증된 sealed evidence와 pending attempt 상태를
   보존하며, drift나 부분 실패 시 원본을 변경하지 않는다.
8. Telemetry 실패는 primary tribunal verdict나 report evidence를 변경하지 않는다.
9. 영향 범위 테스트, 전체 테스트와 diff validation이 통과한다.
