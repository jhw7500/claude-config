# Reviewer B snapshot-bound evidence reuse

Status: approved by the user on 2026-09-12; implementation in progress.
Issue: https://github.com/jhw7500/claude-config/issues/113
Source baseline: `8e2dadc58ef231ecf561d4c945ca8797ca92447e`.

## Outcome and boundary

컨트롤러가 이미 실행한 검증을 Reviewer B가 다시 실행하지 않고 사용할 수 있게 한다.
B는 먼저 snapshot, contract, 실행 환경과 원본 capture를 검증하고, 유효한 증거가
없는 항목만 독립적으로 실행한다. 재사용 결과는 B가 직접 실행한 결과와 출처를 구분한다.

A/C 역할, 세 reviewer의 완료 요구, HIGH/CRITICAL 판정 및 최대 세 round 규칙은
유지한다. Claim/command/time budget은 #114의 범위이며 이번 변경에 포함하지 않는다.
서버, 외부 업로드, credential 저장소, 새로운 패키지 의존성은 추가하지 않는다.

## Existing surfaces

- `model.py`는 execution/claim의 strict schema, 크기 제한, secret/home-path 검사를
  제공한다. 현재 `capture_sha256`은 형식만 검사하며 실제 capture bytes와 대조하지 않는다.
- `review_store.py`는 descriptor를 통한 안전한 파일 접근, 현재 사용자 소유권,
  `0600`, lock과 atomic publication을 제공한다.
- `review_context.py`는 reviewer별 context와 digest를 만든다. B 입력에 증거가
  결속되도록 확장하되 A/C에 B 증거나 다른 reviewer 보고서를 전달하지 않는다.
- `verdict_store.py`는 snapshot/contract 확인 후 정확한 report bytes를 seal하며,
  finalize에서 receipt를 다시 검증한다. 재사용 evidence도 이 검증 경계에 연결한다.
- `telemetry.py`는 관측 누락을 null로 남기며 gate의 권한 근거가 되지 않는다.
- 설치 명세는 `scripts/install-pre-pr-tribunal.py`의 명시적 모듈 목록이다.
  새 모듈과 installed integration tests를 함께 갱신한다.

## Approaches considered

| Option | Capture provenance | Integration cost | Decision |
| --- | --- | --- | --- |
| Runtime capture and frozen bundle | Runtime-observed execution | Moderate | Recommended |
| Import existing excerpts and metadata | Controller-attested execution | Lower | Rejected |

추천안은 승인된 기존 검증을 전용 capture 경로로 한 번 실행하고 runtime이 관측한
종료 코드와 capture를 저장한다. 컨트롤러가 결과를 수기로 선언하는 import-only
방식은 실제 실행 여부와 snapshot 시점을 충분히 연결하지 못한다.
캡처 없이 이미 끝난 과거 실행은 이번 형식의 검증된 증거로 소급 승격하지 않는다.

## Data flow

```text
Approved validation -> Runtime capture -> Frozen bundle -> B verification -> B claims
                                                               |
                                        missing/invalid/live ---+-> Fresh execution
```

새 evidence 모델/저장 모듈과 capture 실행 모듈을 기존 CLI에 연결한다. Command는
`evidence-capture`, `evidence-freeze`, `evidence-verify`의 세 가지 역할로 나눈다.

1. Capture: 승인된 argv를 실행하며 실행 전후 Git snapshot과 명시된 환경 profile을
   확인한다. 명령 문자열을 shell로 재해석하거나 bundle에 들어 있는 명령을 실행하지 않는다.
2. Freeze/project: 승인된 검증 records를 immutable bundle로 묶고 B 전용 읽기 입력으로
   준비한다. 진행 중인 round의 evidence 선택은 바꾸지 않는다.
3. Verify: B가 사용하려는 record의 결속과 capture를 읽기 전용으로 확인한다.
   통과한 entry만 report의 재사용 참조 대상이 된다.

Capture는 임의 명령 실행에 대한 추가 권한을 부여하지 않는다. 명령 선택과 기존
사용자 승인 경계는 컨트롤러 책임이며, 검토 대상 파일의 command/URL을 실행하지 않는다.
Capture 대상은 최종 commit의 깨끗한 snapshot이다. Dirty/pre-commit 결과를 새 HEAD의
증거로 다시 이름 붙이지 않으며, 최종 검증을 capture 경로로 실행하도록 workflow를 정한다.

## Bundle and capture contract

Bundle은 strict versioned JSON이다. 중복 key, 알려지지 않은 field, 중복 entry/command
identity, 잘못된 enum/시간/해시/경로와 상한 초과를 거부한다.

결속 정보는 repository, base ref/SHA, head SHA, merge-base SHA, diff SHA-256,
reviewer/evidence contract version, 명시된 environment profile fingerprint를 포함한다.
Entry에는 명령 identity, repository-relative cwd, 종료 코드, capture 시각, 실제
monotonic duration, bounded excerpts, truncation 여부, stdout/stderr capture digest를 둔다.

Bundle digest는 내부의 자기 주장만 읽어 신뢰하지 않는다. Round 시작 시 선택한 digest를
권위 있는 round 상태와 B context digest에 결속한다. 이후 bundle와 capture bytes를
다시 읽어 그 digest와 대조한다. Bundle bytes를 바꾸고 내부 hash만 새로 계산해도 기존
round의 기대값과 달라 거부한다.

stdout/stderr는 길이를 구분한 versioned framing으로 해시하여 경계 이동으로 동일한
capture처럼 보이게 하지 않는다. 기존 report capture digest와 새 bundle capture
digest의 계산 규칙을 혼동하지 않도록 각 contract와 test에서 고정한다.

원본 capture는 controller-private 저장소에 bounded bytes로 보존하며 모델 입력에는
허용된 excerpts만 전달한다. B verifier는 B 전용으로 투영한 해당 capture만 읽는다.
누락되거나 잘린 원본은 완전한 capture 검증으로 표시하지 않는다.

초기 상한은 bundle JSON 256 KiB, 64 entries, entry당 combined capture 1 MiB,
bundle당 capture 합계 16 MiB로 한다. Excerpt/command 제한은 기존 model 제한을
재사용한다. 상한 초과 실행의 실제 종료 결과는 유지하되 재사용 가능한 evidence는
발행하지 않는다. Capture 명령은 승인된 검증의 실행 제한 시간을 명시적으로 받는다.
출력 상한 이후에는 저장을 중단하고 drain하되 출력 크기만으로 프로세스를 중단하지
않는다. 실행 제한 시간에 도달하면 capture가 생성하고 소유권을 추적한 프로세스만
종료·회수하고 timeout을 남긴다. 무관한 PID나 다른 실행의 프로세스를 정리하지 않는다.

Raw capture, argv, profile, excerpts에서 기존 secret/home-path 정책을 적용한다.
검출된 원문을 오류나 telemetry에 반사하지 않으며 reusable artifact를 발행하지 않는다.
실행 전 검증 가능한 입력은 실행 전에 거부한다. 임의 secret을 완벽하게 식별하는
보증은 하지 않으며 기존 알려진 패턴과 엄격한 허용 필드 경계를 명시한다.

파일은 gitignored `.review` 아래 현재 사용자 소유의 non-symlink regular file,
명시적 `0600`, bounded read 및 atomic publication을 사용한다. B 입력은 다른
reviewer 자료가 없는 별도 읽기 영역이며 reviewer의 Git view는 깨끗하게 유지한다.

## Environment and freshness

Environment는 raw 환경 변수 dump가 아닌 명시적 profile이다. 지원 profile은 실제
실행 도구의 identity/version, dependency lock과 선언된 입력의 digest, 필요한 안전한
설정의 fingerprint를 포함한다. Absolute installation path나 credential 값을 담지 않는다.
Round에는 검증 대상의 기대 profile을 결속한다. Verifier는 source/도구 fact를 다시
계산하고 runtime이 캡처한 환경 근거를 기대 profile과 대조한다. 필요한 fact를 확인할
수 없거나 값이 달라지면 재사용을 거부한다. B의 깨끗한 Git view에 의존성을 새로
설치했다는 가정으로 검증을 통과시키지 않는다.

Profile은 명시된 입력에 대한 결속이다. 선언하지 않은 외부 상태까지 완전히 재현한다는
보증으로 표현하지 않는다. 지원할 수 없는 환경이나 외부 의존 조건은 독립 검증으로 보낸다.

Evidence contract 2에서 `python-v1`과 `node-lock-v1`은 capture-only다. 예외는 evidence
plumbing 자체를 검증하는 code-owned Python tracked-file probe뿐이다. 임의 Python/Node
program 또는 일반 npm invocation을 deterministic으로 분류하지 않는다.

Reusable Node build/typecheck/test는 `node-sandbox-v1`에서만 허용한다. 이 profile은
Bubblewrap이 있는 Linux controller에서 정확한 `npm run build`, `npm run typecheck`,
`npm test` recipe만 받는다. `package.json` script는 local measured dependency tree의
`tsc` 또는 `vitest run`으로 제한하고, pre/post lifecycle hook, caller flag, shell chain,
외부 config path와 local-tool escape를 거부한다. Runtime은 bound HEAD의 clean clone을
만들고 Git metadata를 제거한 뒤 측정한 `node_modules` tree를 복사·재검증한다. Command는
network namespace, private home, empty temporary directory 안에서 실행되므로 controller의
dirty/ignored file, credential-bearing Git config와 host temporary state를 볼 수 없다.
Bubblewrap 또는 정확한 recipe proof가 없으면 reusable evidence를 만들지 않는다.

선택된 Node executable과 npm package tree 전체도 digest에 결속한다. Capture 시 이 runtime을
private temporary tree로 복사하고 다시 측정한 뒤 `/opt/evidence-node`에 read-only mount한다.
따라서 현재 PATH에서 선택한 Node 버전을 사용하되 원래 home/NVM/mise 설치 경로 자체는
sandbox에 노출하지 않으며, 복사 전후 runtime identity가 다르면 재사용을 거부한다.

Controller repository가 `/usr`, `/bin`, `/lib`, `/lib64` 아래에 있으면 sandbox 구성을
거부하고 `/usr/local`은 빈 tmpfs로 가린다. 나머지 `/usr`와 `/bin`, `/lib*` mount는
read-only이지만 완전히 fingerprint된 OS image가 아니라 신뢰하는 host runtime surface다.
따라서 이 profile의 재현성 주장은 명시적으로 결속한 tool/script/input/dependency tree와
복사한 Node runtime으로
한정하며, 전체 운영체제 상태의 결정성을 주장하지 않는다.

`npm audit` 같은 live advisory 결과는 항상 `always-fresh`이며 캐시 재사용을 허용하지
않는다. 임의 TTL을 추정하지 않는다. Fresh 실행이 안전하게 불가능하면 기존 B 규칙에
따라 `unverified`로 남긴다.

## Reviewer report, recovery, and gate

B report는 어떤 bundle entry를 근거로 사용했는지 entry ID와 bundle digest를 명시한다.
이 provenance를 B의 새 execution과 구분하고, 같은 entry를 여러 관련 claim에서
참조할 수 있게 한다. 컨트롤러가 모델 응답을 수정하거나 빈 claim을 채우지 않는다.

Submit와 finalize에서 참조한 entry의 원본 결속을 검증한다. Evidence가 없는 기존
독립 검증 report도 허용한다. 잘못된 재사용 참조가 있는 report를 통과시키지 않으며,
sealed report가 의존하는 artifact가 변조됐을 때 자동으로 새 증거로 교체하지 않는다.

버전 변경은 명시적이다. 현재 evidence contract는 2다. Contract 1 evidence와 명시적
`evidence_contract: 2` marker가 없는 schema-4 verdict는 역사 기록으로 읽을 수 있지만
현재 round authority가 될 수 없다. 새 optional evidence binding과
report provenance에 필요한 schema/contract version을 갱신하고, 기존 기록의 읽기와 현재 판정 권한을 구분한다.
구 contract의 pending round를 묵시적으로 재해석하거나 receipt를 재사용하지 않는다.
불일치 시 원본을 보존하고 기존 명시적 recovery/abandon 경계로 돌아간다.

Bundle이 없거나 B가 사용하기 전에 무효로 판정되면, bounded rejection reason을 남기고
기존 독립 실행을 사용한다. 사용한 bundle이 submission/finalization 때 변한 경우에는
integrity failure로 거부한다. 두 경로를 섞어 잘못된 PASS를 만들지 않는다.

## Telemetry

재사용·새 실행·거부 entry 수와 capture/verification 시간을 기존 계측에 연결한다.
단순히 verifier를 통과한 eligible entry 수와 실제 claim에서 사용한 entry 수는 구분한다.
실제 사용 수는 검증된 report provenance에 근거한다. 관측되지 않은 값은 null로 남긴다.

원래 검증 명령 수, bundle 검증 명령 수, B의 전체 추가 명령 수를 별도로 보고한다.
재사용한 명령의 과거 duration 합계를 이번 elapsed 절감 시간으로 간주하지 않는다.
실제 절감 시간은 동일 조건의 별도 대조 실행으로 측정한다. Telemetry 실패는 관측
누락이며, 유효한 report를 막거나 잘못된 report를 허용하는 근거가 되지 않는다.

## Validation and measurement

- Unit tests: strict schema, duplicate identities, snapshot/base/contract/environment drift,
  한 바이트 변조, capture mismatch, secret/home path, oversize, freshness와 출력 경계.
- Store tests: `0600`, owner, symlink/FIFO, atomic publication 중단, 재시작과 상태 불변.
- Contract tests: B 전용 projection, provenance/claim 연결, 무효 bundle fallback,
  sealed dependency 변조, A/C 격리 및 기존 hook/gate 결과 유지.
- Installed canary: disposable prefix에서 모듈·Skill 설치 완전성과 실제 CLI 흐름 검증.
  Candidate 설치를 전역 runtime 교체로 대체하지 않는다.
- Native canary: 같은 source snapshot/contract/model/environment에서 기존 독립 B와
  evidence 기반 B를 비교한다. 정상 fixture와 의도적 blocker fixture에서 required
  claims/blockers를 대조하고, 명령 수와 elapsed를 함께 기록한다.

실측 기준은 jhw-notion #125를 해결한 PR #140의 기록된 review HEAD
`47df3a80bfb97778812b3c094957701cb335687b`, base
`b4bf525e50e5117833cb1d245357a0a85008408c`, merge-base
`439ddc35170df27bf74a056f48f28e7c07e4c384`이다. GitHub compare로 변경 경로
`.gitignore`, `mcp-server/package-lock.json`, `mcp-server/package.json`을 확인했다.
Historical review에 기록된 diff SHA-256은
`d2314a60f32b1ad04ebd644cc5879f58b48b85d865254e60e313a1f07052cbd9`이며,
실행 전 실제 설치된 diff recipe로 재계산해 일치 여부를 확인한다.

이 비교는 해당 과거 source snapshot을 현재 시점에 다시 실행하는 시험이다. 과거의
advisory DB, host, model 실행 시간을 재현한다고 표현하지 않는다. Source/환경을
확보하지 못하면 historical canary는 미완료로 남기고 작은 합성 fixture로 대체 완료를
주장하지 않는다. 기존 15 claims/17 executions/약 30분 관측값은 출처가 있는 역사적
관측으로만 표시하고 새 대조 측정의 baseline 수치로 사용하지 않는다.

성능은 고정된 절감률을 성공 조건으로 만들지 않는다. 동일 검증의 실제 중복 실행이
줄고, blocker/claim 결과가 유지되며, 원본·결속·관측 한계를 감사할 수 있어야 한다.

## Delivery

설계 승인 후 명세를 확정하고 구현 계획을 작성한다. 변경은 이 Task branch에서만
수행한다. 구조·store/CLI·report/context·telemetry·installer/canary 순으로 구현하고
각 위험 경계에 대한 테스트를 통과시킨 뒤 전체 CI와 pre-PR review를 수행한다.
이 문서는 승인된 설계이며, 구현 완료나 측정 결과를 나타내지 않는다.
