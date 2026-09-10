# Pre-PR Tribunal 유효 reviewer slot 보존·복구 설계

- 날짜: 2026-09-10
- 대상 Issue: [#124](https://github.com/jhw7500/claude-config/issues/124)
- 상태: 대화 설계 승인 완료, 문서 검토 대기
- 설계 기준 커밋: `df1285dbb7655717af3d60c7fc098715a29203dd`
- 선행 구현 계약: #109 (`fae27ef988f199eccc777022f3252f3872719c8a`)

## 1. 요약

Pre-PR tribunal의 한 reviewer가 형식 오류, timeout 또는 agent 장애를 일으켜도
이미 유효하게 끝난 다른 reviewer의 결과를 폐기하지 않는다. 유효한 결과는
reviewer별 slot에 즉시 봉인하고, 실패한 slot만 같은 역할로 다시 실행한다.

이 변경은 리뷰의 강도를 낮추지 않는다. 세 reviewer 모두 유효한 결과를 제출해야
최종 판정을 낼 수 있고, 어느 한 reviewer의 `CRITICAL` 또는 `HIGH` finding도 계속
차단 사유다. 줄이는 것은 보안 판정이 아니라 불필요한 운영 실패와 전체 panel
재실행이다.

핵심 원칙은 다음과 같다.

1. 유효한 reviewer 결과는 도착 즉시 원자적으로 봉인한다.
2. 형식 오류와 실행 장애는 그 slot만 재시도한다.
3. 봉인된 결과는 finding 내용과 관계없이 교체할 수 없다.
4. 최종화는 봉인 당시의 정확한 bytes와 현재 파일의 digest를 다시 대조한다.
5. telemetry와 종료 후 worktree cleanup 실패는 warning이며 verdict를 바꾸지 않는다.
6. snapshot, context, contract 또는 증거 무결성 실패는 계속 fail-closed다.

## 2. 문제

현재 tribunal은 round를 사실상 `all pending` 또는 `all complete`로만 표현한다.
A와 B가 유효한 응답을 냈어도 C의 JSON이 malformed이면 round 전체를 다시
시작해야 한다. 이 방식에는 다음 문제가 있다.

- 이미 비용을 지불해 얻은 유효 응답을 버린다.
- 재실행된 A 또는 B가 다시 timeout 나면서 실패 면적이 커진다.
- 리뷰 내용과 무관한 JSON 포맷이나 telemetry 문제가 품질 gate를 막는다.
- 같은 HEAD에서 복구해도 전체 panel 재실행 때문에 시간과 토큰이 반복 소비된다.
- operator가 어느 응답을 재사용해도 되는지 판단하게 만들어 선택 편향 위험이 생긴다.

#109는 raw report bytes 보존, 파일 권한 검증, 명시적 report validation 및
telemetry 기반을 추가하지만, 의도적으로 mixed slot 상태와 선택적 재실행은 범위
밖에 두었다. #124는 그 후속 계약을 정의한다.

## 3. 목표

- 유효한 A/B와 실패한 C를 동시에 표현하는 round 상태를 지원한다.
- 유효한 report를 reviewer별로 즉시 봉인하고 정확한 receipt를 verdict에 남긴다.
- malformed report는 같은 reviewer의 format retry만 수행한다.
- timeout, process failure 또는 reviewer 상실은 같은 역할의 replacement만 허용한다.
- retry/replacement 동안 유효한 peer slot을 그대로 보존한다.
- installed runtime이 report schema와 context contract의 정본이 되게 한다.
- legacy in-progress round를 증거가 충분할 때만 비선택적으로 이관한다.
- 실제 보안 finding과 증거 무결성 실패는 기존과 동일하게 차단한다.
- telemetry 및 terminal cleanup 문제를 판정 경로와 분리한다.

## 4. 비목표

- `2/3` 다수결로 통과시키지 않는다.
- `CRITICAL` 또는 `HIGH` finding을 운영 오류로 낮추지 않는다.
- reviewer 간 report 공유나 토론형 합의를 도입하지 않는다.
- controller가 내용이 마음에 들지 않는 report만 선택적으로 교체하게 하지 않는다.
- candidate branch의 아직 설치되지 않은 schema를 reviewer에게 강제로 적용하지 않는다.
- #124의 미검증 코드를 배포해 현재 #109 tribunal을 자기 자신으로 복구하지 않는다.
- 무한 재시도 또는 무제한 evidence 보존을 도입하지 않는다.

## 5. 설계 선택

### 5.1 선택: verdict 안의 reviewer별 봉인 상태

`verdict.json`을 round 상태의 유일한 정본으로 유지한다. 각 reviewer slot이
`pending` 또는 `sealed` 상태를 가지며, `sealed` slot에는 report와 receipt가
함께 기록된다.

장점은 다음과 같다.

- report 선택과 최종 verdict 사이에 별도 ledger가 생기지 않는다.
- gate가 한 파일의 snapshot-bound 상태만 검증하면 된다.
- operator가 파일 존재 여부만 보고 임의 재사용하는 경로를 차단한다.
- 최종화가 봉인 시점과 최종화 시점의 증거를 직접 인증할 수 있다.

### 5.2 기각: 별도 reuse ledger

별도 `reuse.json` 또는 controller-local cache는 verdict와 두 번째 정본을 만든다.
두 기록이 어긋날 때 어느 쪽을 신뢰할지 새 규칙이 필요하고, cleanup이나 session
전환 중 한쪽만 남을 수 있으므로 채택하지 않는다.

### 5.3 기각: controller가 기존 파일을 골라 재사용

파일이 schema-valid라는 이유만으로 controller가 A만 재사용하고 B는 재실행할 수
있으면 finding 내용을 본 뒤 reviewer를 교체하는 선택 편향이 가능하다. 신규
workflow에서는 유효한 제출을 즉시 봉인하고, legacy 이관은 모든 slot을 한 번에
검사하는 비선택적 batch로만 허용한다.

## 6. 상태 모델

### 6.1 Verdict schema v2

새 mixed-slot 상태는 `verdict_schema_version: 2`에서만 기록한다. v1의 terminal
`PASS`/`FAIL` verdict는 기존과 동일하게 읽을 수 있어야 하며 bytes를 자동으로
rewrite하지 않는다.

각 reviewer slot은 다음 두 상태만 가진다.

```json
{
  "reviewer": "A",
  "state": "pending"
}
```

```json
{
  "reviewer": "A",
  "state": "sealed",
  "report": {},
  "receipt": {
    "raw_sha256": "...",
    "context_sha256": "...",
    "report_contract_version": 1,
    "attempt": 1
  }
}
```

`sealed`는 단순히 파일이 존재한다는 뜻이 아니다. 설치된 runtime의 schema,
현재 round의 snapshot과 reviewer context, 파일 안전성 검사를 모두 통과한 정확한
bytes가 원자적으로 verdict에 수락됐다는 뜻이다.

허용 상태는 다음과 같다.

| Effective state | Slot 조합 | 의미 |
|---|---|---|
| `in_progress` | pending 1~3개, sealed 0~2개 | 실패한 slot만 계속 수집 |
| `ready` | sealed 3개 | 최종화 가능, 아직 PASS/FAIL 아님 |
| `pass` 또는 `fail` | sealed 3개 + terminal metadata | 불변 terminal verdict |

`ready`는 세 slot이 모두 sealed인 persisted `in_progress` 상태에서 계산한다.
중복 상태 전이를 피하기 위해 별도 gate state로 저장하지 않으며, persisted gate
state는 `in_progress|pass|fail`만 유지한다.

### 6.2 불변식

- 한 round에는 정확히 A/B/C 세 역할이 있다.
- `sealed` slot은 같은 round에서 `pending`으로 돌아갈 수 없다.
- `sealed` slot의 report, receipt 또는 reviewer role은 교체할 수 없다.
- report 내용에 blocker가 있어도 해당 slot은 유효하므로 봉인된다.
- 세 slot이 모두 봉인되기 전에는 PASS/FAIL을 계산하지 않는다.
- 세 slot이 봉인된 뒤에는 retry/replacement 명령을 거부한다.
- HEAD, merge-base, diff, context 또는 contract drift는 재수집이 아니라 새 round
  또는 명시적 recovery가 필요한 무결성 실패다.

## 7. Report 제출과 봉인

### 7.1 원자적 `submit-report`

신규 주 경로는 report 저장과 validation, slot 봉인을 하나의 runtime 명령으로
묶는 `submit-report`다. 명령 이름과 세부 인자는 구현 계획에서 현재 CLI 규칙에
맞게 확정하되, 의미 계약은 다음과 같다.

1. stdin의 정확한 raw bytes를 current-user-owned, non-symlink regular staging
   file에 mode `0600`으로 저장한다.
2. owner, type, mode를 즉시 검증한다.
3. 설치된 runtime의 report schema로 parse/validate한다.
4. round snapshot과 reviewer별 projected context digest를 검증한다.
5. 성공하면 canonical report file을 안전하게 publish하고 같은 lock 범위에서
   slot을 `sealed`로 전이한다.
6. verdict receipt에 exact `raw_sha256`, `context_sha256`, contract version 및
   attempt 번호를 기록한다.
7. 실패하면 slot은 `pending`으로 남고 stable error code를 반환한다.

이 경로에서는 controller가 validation 성공 후 별도의 `accept` 결정을 내리지
않는다. 유효한 bytes가 제출되면 finding의 내용과 관계없이 자동 봉인된다.

#109의 `store-report`와 `validate-report` primitives는 내부 구현과 read-only
진단 호환성에 재사용할 수 있다. 그러나 정상 skill 경로는 둘 사이에 선택 가능한
시간차가 없는 `submit-report` 계약을 사용한다.

### 7.2 정확한 bytes와 파일 안전성

봉인 대상 canonical report는 다음을 모두 만족해야 한다.

- current user 소유
- symlink가 아닌 regular file
- 정확히 mode `0600`
- 봉인 receipt의 `raw_sha256`와 일치
- parse된 report가 설치된 runtime schema와 일치
- reviewer role, round, snapshot 및 context binding 일치

ambient `umask`에 의존하지 않고 각 파일에 mode `0600`을 설정한다. 최종화 직전
동일 검사를 다시 수행하며 하나라도 실패하면 `finalize` 전에 중단한다.

### 7.3 실패 attempt 증거

malformed 또는 truncated bytes는 canonical sealed report로 publish하지 않는다.
대신 `.review/attempts/round-N/<reviewer>/` 아래 current-user-owned, non-symlink
regular file로 mode `0600`에 보존한다. attempt metadata에는 error code와 digest만
기록하고 report 본문을 telemetry로 내보내지 않는다.

보존량은 reviewer별 최근 3개 실패 attempt로 제한한다. limit을 넘은 파일은
새 제출을 받기 전에 가장 오래된 terminal attempt부터 지운다. 삭제가 안전하게
검증되지 않으면 새 evidence를 덮어쓰지 않고 stable storage error로 중단한다.

## 8. Reviewer별 복구 정책

| 실패 종류 | Slot 상태 | 자동 조치 | Peer slot |
|---|---|---|---|
| schema/JSON 형식 오류 | pending | 동일 reviewer 역할에 format-only 재요청 | sealed 유지 |
| timeout/process/agent failure | pending | 동일 역할의 fresh replacement | sealed 유지 |
| 유효 report, blocker 포함 | sealed | 교체 금지 | sealed 유지 |
| snapshot/context/contract drift | 변경 없음 | 전체 round 중단, 명시적 recovery 요구 | 재사용 금지 |
| owner/mode/symlink/digest 불일치 | 변경 없음 | finalize 전 중단 | 무결성 조사 전 재사용 금지 |
| telemetry emit 실패 | 기존 상태 유지 | warning 기록 | 영향 없음 |
| terminal reviewer worktree cleanup 실패 | 기존 상태 유지 | warning과 경로 기록 | verdict 영향 없음 |

format retry에는 기존 report를 추측해 고치는 parser repair를 사용하지 않는다.
reviewer에게 installed contract를 다시 제공하고 완전한 응답을 새로 받는다.

자동 retry는 slot당 최초 시도 포함 최대 3회로 제한한다. 그 안에서 format retry나
same-role replacement를 조합할 수 있다. 소진 시 round를 폐기하지 않고 이미
봉인된 peer를 보존한 채 `REVIEWER_UNAVAILABLE`로 operator에게 반환한다. 이후의
명시적 재개도 같은 pending slot에서 시작하며 전체 panel을 재실행하지 않는다.

## 9. Context와 contract binding

reviewer는 candidate branch가 아니라 설치된 trusted runtime이 제공하는 report
contract를 따른다. 각 slot receipt는 다음을 고정한다.

- installed report contract version
- verdict schema version
- canonical snapshot identity: HEAD, merge-base, base ref, diff digest
- reviewer별 projected context digest
- reviewer role과 round

projected context는 공통 snapshot과 그 reviewer 자신의 prior decision만으로 만든다.
다른 reviewer의 report나 finding은 포함하지 않는다. 따라서 peer가 먼저
봉인됐다는 이유로 아직 pending인 reviewer의 context digest가 변하지 않는다.

설치된 contract가 round 도중 바뀌면 신규 report를 이전 slot과 섞지 않는다.
`CONTRACT_DRIFT`로 중단하고 명시적으로 새 round를 시작한다.

## 10. 최종화

`finalize`는 다음 순서로만 동작한다.

1. snapshot과 installed contract binding을 다시 검증한다.
2. A/B/C가 모두 `sealed`인지 확인한다.
3. 각 canonical file의 type, owner, mode를 독립적으로 확인한다.
4. 각 file의 exact bytes digest를 persisted receipt와 대조한다.
5. 각 report를 다시 parse하고 verdict에 봉인된 report object와 의미적으로
   일치하는지 확인한다.
6. 세 report 전체의 blocker를 집계한다.
7. 하나라도 `CRITICAL` 또는 `HIGH`가 있으면 FAIL, 없으면 PASS로 원자 전이한다.

어떤 검증도 실패하면 terminal verdict를 쓰지 않는다. warning 성격의 telemetry
또는 이미 종료된 reviewer worktree cleanup 실패는 위 여섯 단계에 포함하지 않는다.

## 11. Telemetry와 cleanup

Telemetry는 관측 side channel이다. setup, start, complete 또는 failure event를
쓰지 못해도 report 검증과 verdict 전이는 계속 진행한다. runtime은 stderr 또는
bounded warning record로 실패를 알리되 gate error code로 승격하지 않는다.

Reviewer process가 종료되고 report 제출도 끝난 뒤의 worktree cleanup 실패 역시
warning이다. 정리 실패 경로와 이유를 남기고 verdict 판정을 계속한다.

다음 경우는 cleanup warning으로 낮추지 않는다.

- reviewer process 생존 여부가 불명확함
- worktree가 report 생성 중 변경됐을 가능성이 있음
- snapshot 또는 evidence ownership을 확인할 수 없음
- cleanup 대상이 정확히 식별되지 않음

이는 정리 실패가 아니라 증거 무결성 또는 destructive-target 안전성 문제이므로
기존 fail-closed 정책을 유지한다.

## 12. Legacy in-progress round 이관

v1 terminal verdict는 그대로 읽고 변경하지 않는다. v1 `in_progress` round는
설치된 v2 runtime에서 명시적 batch adoption을 요청할 때만 이관한다.

Batch adoption은 A/B/C canonical report를 모두 스캔하고 다음 규칙을 적용한다.

- 각 report를 독립적으로 type/owner/mode/schema/snapshot/context 검사한다.
- 유효한 report는 finding 내용과 관계없이 모두 봉인한다.
- invalid 또는 missing report는 모두 pending으로 둔다.
- caller가 reviewer subset을 지정할 수 없다.
- 기존 receipt와 동일 수준의 provenance를 재구성할 수 없으면 해당 slot은
  fail-closed로 pending 처리한다.
- 이관 결과와 각 slot의 stable reason code를 한 번에 반환한다.

이 규칙은 operator가 결과를 보고 유리한 report만 골라 재사용하는 것을 막는다.

## 13. #109와의 bootstrap 경계

#124 구현은 #109가 추가한 secure report storage, validation 및 telemetry 계약을
선행 조건으로 한다. 그러나 현재 #124 branch는 master에서 시작했고, #109
worktree에는 아직 진행 중인 tribunal state가 있다.

따라서 다음 경계를 지킨다.

1. 이 설계 승인 전에는 #109 commit을 #124에 통합하지 않는다.
2. 구현 계획에서 #109 HEAD를 안전하게 stack/merge하는 방식을 명시한다.
3. #109 worktree, `.review` state 및 installed runtime을 수정하지 않는다.
4. #124가 review, merge, install되기 전에는 #124 기능으로 #109 tribunal을
   복구했다고 주장하지 않는다.
5. 그 전에 #109를 완료해야 한다면 현재 trusted runtime의 legacy full-panel
   recovery가 한 번 더 필요할 수 있다.

이 경계는 unreviewed branch를 설치해 자기 자신의 review를 통과시키는 순환
self-hosting을 차단한다.

## 14. 예상 변경 영역

구현 계획은 최소한 다음 영역을 다룬다.

- `hooks/pre_pr_tribunal/model.py`
  - schema v2 reviewer slot과 invariant
- `hooks/pre_pr_tribunal/verdict_store.py`
  - mixed state, atomic seal, authenticated finalize, legacy batch adoption
- `hooks/pre_pr_tribunal/review_store.py`
  - secure staging/publish와 bounded attempt evidence
- `hooks/pre_pr_tribunal/cli.py`
  - submit, pending-slot recovery, batch adoption의 stable interface
- `hooks/pre_pr_tribunal/telemetry.py`
  - non-gating warning boundary 유지
- `.codex/skills/pre-pr-tribunal/SKILL.md`와 관련 reference
  - 실패한 slot만 재실행하는 controller workflow
- installer/probe/runtime copy
  - candidate가 아니라 installed contract를 reviewer에게 제공
- 단위, 통합, skill pressure 및 runtime canary tests

정확한 파일 목록과 command spelling은 승인 후 implementation plan에서 repository
현황을 다시 확인해 확정한다.

## 15. 검증 전략

### 15.1 Model과 persistence

- v2 mixed `pending/sealed` round-trip
- v1 terminal verdict read compatibility
- v1 in-progress의 비선택적 batch adoption
- sealed slot의 downgrade, overwrite, replacement 거부
- 세 slot 미완성 상태의 finalize 거부

### 15.2 Report 무결성

- raw bytes digest와 receipt 일치
- LF, TAB 등 JSON string edge case의 exact-byte 보존
- symlink, non-regular file, wrong owner, wrong mode 거부
- seal 이후 file mutation과 digest mismatch 검출
- snapshot, context, contract drift 검출
- malformed attempt의 mode `0600` 및 bounded retention

### 15.3 Recovery workflow

- A/B sealed, C malformed이면 C만 format retry
- A sealed, B timeout이면 B만 same-role replacement
- blocker를 포함한 valid report도 sealed되고 replacement 거부
- retry budget 소진 후 peer slot 유지 및 재개 가능
- reviewer subset을 지정한 legacy adoption 거부
- peer report 비노출 유지

### 15.4 Non-gating 운영 실패

- telemetry setup/emit 실패에도 verdict path 진행
- terminal worktree cleanup 실패가 warning으로만 남음
- active/uncertain reviewer와 unsafe cleanup target은 계속 차단

### 15.5 End-to-end

- 세 reviewer 모두 valid + blocker 없음 => PASS
- 한 reviewer valid blocker => FAIL
- 한 reviewer malformed 후 retry success => peer 재실행 없이 최종화
- installed contract와 candidate contract가 다를 때 installed contract 사용
- full repository suite와 설치 runtime canary 통과
- skill 문서 변경에 대한 baseline/pressure 시나리오 통과

## 16. 완료 조건

#124는 다음 조건을 모두 만족할 때 완료다.

- 유효 reviewer 결과가 slot별로 봉인되고 전체 재실행 없이 보존된다.
- 실패한 slot만 정책에 맞게 retry/replacement된다.
- 세 유효 report 없이 PASS/FAIL이 생성되지 않는다.
- real blocker와 증거 무결성 오류가 계속 fail-closed다.
- telemetry와 terminal cleanup 실패가 불필요한 gate blocker가 아니다.
- finalize가 persisted receipt와 exact file bytes를 인증한다.
- legacy 이관이 비선택적이고 provenance 부족 시 fail-closed다.
- #109 bootstrap 경계를 위반하지 않는다.
- 문서, 단위/통합/pressure/canary 검증이 모두 통과한다.
