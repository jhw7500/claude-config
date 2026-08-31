# Codex MCP 전이 진실성과 deadline 경계 재설계

- 날짜: 2026-08-31
- 대상: GitHub issue #67, Task 6 후속 재설계
- 상태: 대화 설계 승인 완료, 문서 검토 대기
- 기준 커밋: `9804da8f33e333409e0d5d9360a388ec7c98936d`
- 상위 명세: `docs/superpowers/specs/2026-08-29-codex-mcp-ownership-design.md`

## 1. 배경

Task 6의 다섯 번째 독립 재리뷰에서 기능 회귀는 모두 통과했지만 두 가지
load-bearing 안전 결함이 남았다.

1. raw record가 여전히 expected digest인 상태에서도 journal의 `phase`만
   `committed`이면 recovery가 발생하지 않은 event와 receipt를 만들 수 있다.
2. absolute deadline이 만료된 뒤 복합 filesystem helper와 오류 recovery가
   새로운 syscall 경계를 시작할 수 있고, 첫 force delivery 뒤 두 번째 pidfd
   준비가 만료되면 완전한 partial report 없이 예외가 탈출할 수 있다.

두 문제의 공통 원인은 핵심 invariant가 분산된 조건문에 의존한다는 점이다.
Journal의 진실은 phase, raw digest, receipt를 함께 보아야 하지만 현재 recovery는
각 증거를 독립적인 commit 증거로 취급한다. Deadline도 단일 능력으로 전달되지
않고 helper 전후의 수동 check로 구현되어 복합 작업과 예외 경로가 빠진다.

이 문서는 기존 공개 CLI와 `StateStore.transition()` 계약, persisted schema,
pidfd 안전 모델을 유지하면서 두 invariant를 각각 하나의 내부 구성요소로
집중시키는 재설계를 정의한다.

## 2. 목표

1. Journal recovery 결정을 phase 문자열이 아닌 완전한 증거 관계로 계산한다.
2. 모순된 journal, raw digest, receipt 조합은 event 생성이나 증거 삭제 없이
   `StateCorruption`으로 fail closed한다.
3. 시간 제한 경로의 모든 의미 있는 filesystem/procfs/pidfd 경계를 하나의
   absolute deadline capability가 통제한다.
4. Deadline 만료 뒤 새로운 관측·변경·recovery syscall을 시작하지 않는다.
5. 첫 signal 이후 만료나 준비 실패가 발생해도 exact action set 전체를 포함한
   nonzero partial/unavailable 결과를 반환한다.
6. 현재의 read-only audit, dry-run, Hook silent exit, redaction, root binding,
   signal receipt no-replay 계약을 보존한다.

## 3. 비목표

- 전체 ownership ledger를 새로운 WAL 형식으로 교체하지 않는다.
- 기존 session/process/signal-intent/event-receipt schema를 변경하지 않는다.
- macOS 또는 Windows 지원을 추가하지 않는다.
- 새로운 retry, background worker, daemon protocol을 추가하지 않는다.
- live HOME/Codex 설정, user systemd unit, 실제 사용자 프로세스로 검증하지 않는다.
- Task 7 이후의 installer/configuration 범위를 선행 구현하지 않는다.

## 4. 검토한 접근

### 4.1 누락된 조건문만 추가

Recovery에 `current == expected` 검사를 추가하고 발견된 helper마다 deadline
check를 더하는 방식이다. Diff는 작지만 동일한 분산 구조를 유지한다. 이미 여러
수정 라운드에서 새로운 누락 경계가 반복되었으므로 채택하지 않는다.

### 4.2 진실 판정과 boundary I/O 추출

순수 recovery truth table과 deadline-aware syscall gateway를 별도 내부 모듈로
추출한다. `StateStore`는 orchestration과 private-path 검증을 유지하지만 두 핵심
판정을 직접 재구현하지 않는다. Persisted schema와 호출자 계약을 유지하면서
원인을 제거할 수 있으므로 이 접근을 채택한다.

### 4.3 새 append-only WAL로 전면 교체

State mutation과 event를 하나의 WAL에서 재생하는 방식이다. 장기적으로 단순할
수 있지만 migration, retention, crash recovery, 설치 호환성을 다시 증명해야 한다.
현재 두 결함에 비해 범위와 위험이 지나치게 크므로 채택하지 않는다.

## 5. 구속력 있는 invariant

### 5.1 Journal truth

- `phase`는 단독 commit 증거가 아니다.
- Raw record의 exact canonical digest가 primary state truth다.
- Receipt는 해당 event가 이미 materialize되었다는 dedup 증거이며, raw state가
  expected인 전이를 commit으로 바꾸는 증거가 아니다.
- Transition ID는 다음 canonical payload의 SHA-256으로 다시 계산할 수 있어야
  한다.

```text
{
  record_kind,
  record_key,
  expected_digest,
  updated_digest,
  event_without_event_id
}
```

- filename, journal `event_id`, event 안의 `event_id`, 재계산한 transition ID,
  receipt의 transition/event ID가 모두 같아야 한다.
- 모순을 발견한 recovery는 event/receipt를 생성하거나 journal/receipt를
  삭제·격리하지 않는다. 원본 증거를 남기고 `StateCorruption`을 반환한다.

### 5.2 Absolute deadline

- 한 작업은 하나의 absolute monotonic deadline만 사용한다.
- Deadline은 helper가 새로 계산하거나 연장하지 않는다.
- `open`, `read`, `write`, `fstat/stat`, `mkdir`, `chmod`, `fsync`, `rename/replace`,
  `unlink`, directory iteration, procfs observation, pidfd open은 각각 하나의
  의미 있는 boundary다.
- 각 boundary는 시작 직전과 반환 직후 같은 budget을 검사한다.
- 반환 직후 만료를 발견하면 다음 boundary를 시작하지 않는다.
- 이미 열린 descriptor의 `close`만 비관측 resource finalization으로 허용한다.
  Close 오류는 상태 recovery나 추가 filesystem 작업을 유발하지 않는다.
- 만료된 경로는 temporary file이나 prepared journal을 즉시 정리하지 않는다.
  다음 invocation이 새 budget 아래 bounded reconciliation을 수행한다.
- pidfd send는 final lexical-root authority와 분리할 수 없으므로 특수한 하나의
  prepared effect composite다. Deadline은 final root validation 직전에 확인하며,
  그 validation과 send 사이에는 clock read를 포함한 어떤 boundary도 두지 않는다.
  이 composite가 시작된 뒤 deadline이 넘어가는 경우는 이미 시작된 한 effect의
  잔여로 취급하고, send 반환 뒤에는 in-memory accounting과 descriptor close 외의
  새 boundary를 시작하지 않는다.

### 5.3 Irreversible effect accounting

- Signal 전 durable dispatch/no-replay evidence가 존재해야 한다.
- Signal effect가 반환한 뒤에는 실제 delivery를 확정할 수 없더라도
  `delivered-or-indeterminate`로 취급하며 자동 반복을 허용하지 않는다.
- 첫 delivery 이후 모든 오류와 만료는 남은 exact identities를 `skipped`로
  합성하고 nonzero partial/unavailable report를 반환한다.
- 두 번째 이후 pidfd 준비도 per-action accounting 경계 안에 있어야 한다.

## 6. 구성요소 설계

### 6.1 `transition_truth.py`

이 모듈은 filesystem이나 `StateStore`를 알지 못하는 순수 판정 모듈이다.

```python
class RecoveryDecision(Enum):
    DISCARD_PREPARED = "discard_prepared"
    FINALIZE_UPDATED = "finalize_updated"
    ALREADY_RECEIPTED = "already_receipted"


@dataclass(frozen=True)
class RecoveryEvidence:
    phase: str
    current_digest: str
    expected_digest: str
    updated_digest: str
    has_matching_receipt: bool


def decide_recovery(evidence: RecoveryEvidence) -> RecoveryDecision:
    ...


def derive_transition_id(
    record_kind: str,
    record_key: str,
    expected_digest: str,
    updated_digest: str,
    event_without_id: Mapping[str, object],
) -> str:
    ...
```

`decide_recovery()`는 정상 결정만 반환한다. 다음 truth table에서 corruption인
조합은 typed `RecoveryContradiction`을 발생시키며 `StateStore`가 이를
`StateCorruption`으로 변환한다.

| current digest | phase | matching receipt | 결정 |
| --- | --- | --- | --- |
| expected | prepared | 없음 | `DISCARD_PREPARED` |
| updated | prepared | 없음 | `FINALIZE_UPDATED` |
| updated | committed | 없음 | `FINALIZE_UPDATED` |
| updated | prepared/committed | 있음 | `ALREADY_RECEIPTED` |
| expected | committed | 없음/있음 | corruption |
| expected | prepared | 있음 | corruption |
| 제3 digest | 모든 값 | 없음/있음 | corruption |

`expected_digest == updated_digest`, 알 수 없는 phase, ID 재계산 불일치도 schema
검증 단계에서 corruption이다.

### 6.2 `deadline_io.py`

이 모듈은 absolute deadline과 실제 boundary 호출을 결합한다.

```python
@dataclass(frozen=True)
class DeadlineBudget:
    deadline: float | None
    monotonic: Callable[[], float]

    def check(self) -> None:
        ...


class DeadlineIO:
    def __init__(self, budget: DeadlineBudget):
        ...

    def open_fd(
        self,
        name: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        ...

    def read(self, fd: int, size: int) -> bytes:
        ...

    def write(self, fd: int, data: bytes) -> int:
        ...

    def fstat(self, fd: int) -> os.stat_result:
        ...

    def stat(
        self,
        name: str,
        *,
        dir_fd: int,
        follow_symlinks: bool = False,
    ) -> os.stat_result:
        ...

    def mkdir(self, name: str, mode: int, *, dir_fd: int) -> None:
        ...

    def fchmod(self, fd: int, mode: int) -> None:
        ...

    def fsync(self, fd: int) -> None:
        ...

    def replace(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        ...

    def unlink(self, name: str, *, dir_fd: int) -> None:
        ...

    def directory_names(self, directory_fd: int, limit: int) -> tuple[str, ...]:
        ...

    def close_fd(self, fd: int) -> None:
        ...
```

각 resource-free method는 operation 전후에 `budget.check()`를 실행하며 실제
boundary 하나만 감싼다. `open_fd()`는 open 반환 뒤 만료된 경우 자신이 얻은 fd를
`close_fd()`로 닫고 원래 deadline 예외를 다시 발생시킨다. Directory iterator도
같은 방식으로 iterator ownership과 close를 내부에서 처리한다. 따라서 post-check
예외가 resource handle을 호출자에게 전달하지 못해 leak시키는 경로가 없다.
복합 helper는 반드시 여러 typed method의 명시적 순서로 구성한다. 시간 제한
mutation/recovery 경로에서 raw `os.*` boundary를 직접 호출하지 않는다.

Directory 생성은 다음 순서를 갖는다.

1. budgeted open 시도
2. 없으면 budgeted `mkdir(mode=0o700)`
3. budgeted open과 fstat 검증
4. 새로 생성된 경우에만 budgeted parent fsync

`chmod`는 생성 직후 필수 단계에서 제거한다. `0o700`은 group/other bit가 없으므로
umask가 더 공개적인 mode를 만들 수 없고, 지나치게 제한적인 결과는 다음 open의
private-directory 검증이 fail closed한다.

Atomic JSON write는 다음 순서를 갖는다.

1. canonical bytes를 boundary 없이 생성
2. unique private temporary file open
3. fstat/fchmod/write/fsync를 각각 budgeted boundary로 수행
4. budgeted no-follow target validation
5. budgeted replace
6. budgeted directory fsync

어느 boundary 반환 직후 만료되더라도 다음 단계나 stat/unlink cleanup을 시작하지
않는다. Replace 전 남은 `.tmp-<digest>` 파일은 authoritative record가 아니며,
다음 fresh-budget maintenance가 private owner/mode/link/name을 검증한 뒤 bounded
수로 정리한다. Replace 후 만료되면 journal truth table이 실제 target digest로
완료 여부를 판정한다.

### 6.3 `StateStore` integration

`StateStore.transition()` 공개 signature는 유지한다. 내부 순서는 다음과 같다.

1. 하나의 `DeadlineBudget`과 `DeadlineIO` 생성
2. 기존 unresolved journal과 complete receipt set을 같은 budget으로 검증
3. exact authority와 expected raw digest 검증
4. derived transition ID가 포함된 prepared journal 기록
5. effect가 있으면 deadline과 process/lease/intent/force evidence를 먼저 검증하고,
   final lexical root validation 직후 clock read 없이 즉시 effect 실행
6. updated raw record 기록
7. journal phase 기록
8. truth table로 recovery/materialization 결정

`_recover_known_transition_locked()`는 더 이상 `deadline=None`을 만들지 않는다.
Fresh budget이 남아 있을 때만 같은 budget으로 실행한다. Effect 이후 deadline이
만료되면 recovery를 시작하지 않고 `PostEffectStateError`를 반환한다. 이미 durable한
dispatch record가 no-replay authority이며 다음 invocation이 journal을 복구한다.

Journal load는 structural validation 뒤 `derive_transition_id()`를 호출한다.
Recovery는 `decide_recovery()` 결과에 따라서만 다음 동작을 수행한다.

- `DISCARD_PREPARED`: event/receipt 없이 journal 제거
- `FINALIZE_UPDATED`: receipt 생성 후 JSONL cache materialization, journal 제거
- `ALREADY_RECEIPTED`: JSONL 재발행 없이 journal 제거
- contradiction: 아무것도 변경하지 않고 `StateCorruption`

### 6.4 Cleanup batch accounting

`execute_cleanup()`의 per-action `try`는 classification 재검증뿐 아니라
`_prepare_exact_signal()` 호출부터 pidfd close와 outcome 합성까지 포함한다.

- 첫 action 전 deadline 만료: signal 없이 기존 no-action 오류
- 첫 delivery 뒤 다음 pidfd open 전/후 만료: 이미 전달된 identity는
  delivered/indeterminate, 현재 및 이후 identity는
  `partial_force_deadline_exhausted`로 기록
- 결과: `partial_force=true`, `after_available=false`, `deadline_expired=true`,
  exact action 수와 같은 outcome 수, CLI nonzero
- 이미 열린 pidfd는 close만 하고 procfs 재관측, journal recovery, event append를
  시작하지 않음

TERM 경로도 예외를 탈출시키지 않지만 force와 달리 `partial_force`는 설정하지
않는다. 두 경로 모두 unavailable/nonzero truth를 유지한다.

## 7. 저장 형식과 migration

- Journal, receipt, session, process, signal-intent schema version은 유지한다.
- Transition ID derivation도 기존 schema 1 생성식과 동일하다.
- 기존 정상 prepared/committed journal은 truth table로 복구 가능하다.
- 기존 `committed + current=expected`, receipt contradiction, ID 불일치 상태는
  자동 migration하지 않고 corruption으로 중단한다. 자동 추측보다 운영자 진단을
  우선한다.
- 기존 safe `.tmp-*` 파일은 fresh-budget maintenance에서 제한된 개수만 정리한다.
  다른 이름, symlink, 비정상 owner/mode/link는 삭제하지 않고 corruption으로
  보고한다.

## 8. 오류 및 출력 계약

- Semantic contradiction: `StateCorruption`, signal/backend 호출 0회, nonzero CLI
- Pre-effect deadline: `OperationDeadlineExceeded`, signal 0회, 만료 검출 이후 새
  boundary 0회
- Post-effect deadline/persistence failure: `PostEffectStateError`, no-replay 유지,
  unavailable/nonzero report
- Partial force: exact action set 전체 outcome, `partial_force=true`, nonzero CLI
- Hook: 모든 예외를 삼키고 stdout/stderr 없이 exit 0; durable lifecycle milestone이
  입증된 경우에만 기존 notifier/fallback 결정을 수행
- 모든 진단은 기존 redaction/canary 금지 계약을 유지한다.

## 9. 파일 책임

- 생성: `codex-mcp-ownership/codex_mcp_ownership/transition_truth.py`
  - transition ID 재계산과 순수 recovery truth table
- 생성: `codex-mcp-ownership/codex_mcp_ownership/deadline_io.py`
  - absolute budget과 단일-boundary syscall gateway
- 수정: `codex-mcp-ownership/codex_mcp_ownership/state.py`
  - private path 검증, journal orchestration, truth/I/O 구성요소 연결
- 수정: `codex-mcp-ownership/codex_mcp_ownership/cleanup.py`
  - pidfd 준비를 batch accounting 경계 안으로 이동
- 필요시에만 수정: `codex-mcp-ownership/codex_mcp_ownership/hook.py`
  - 새 deadline exception 전달에 필요한 최소 wiring
- 생성: `tests/codex_mcp_ownership/test_transition_truth.py`
  - 전체 truth table과 ID derivation의 순수 단위 테스트
- 생성: `tests/codex_mcp_ownership/test_deadline_io.py`
  - boundary 전후 만료와 no-later-boundary 테스트
- 수정: `tests/codex_mcp_ownership/test_state.py`
  - 실제 journal/receipt/raw-state 통합 반례
- 수정: `tests/codex_mcp_ownership/test_cleanup.py`
  - second-pidfd partial accounting 반례
- 필요시에만 수정: `tests/codex_mcp_ownership/test_hook_cli.py`
  - bounded fallback 회귀

## 10. 테스트 전략

모든 production 변경은 RED→GREEN 순서를 따른다.

### 10.1 Journal truth

- `committed + current=expected + no receipt`가 event/receipt를 만들지 않고
  journal을 보존한 채 `StateCorruption`을 발생시킨다.
- `prepared + current=expected`는 event 없이 preparation을 폐기한다.
- `prepared/committed + current=updated`는 한 logical event만 materialize한다.
- `receipt + current=expected`, 제3 digest, ID 재계산 불일치는 fail closed한다.
- JSONL rotation 여부는 decision과 event dedup에 영향을 주지 않는다.

### 10.2 Deadline boundaries

- Fake clock을 mkdir, open, fstat, write, fsync, replace, directory fsync 각각의
  반환 직후 만료시키고 그 뒤 boundary call 수가 0인지 확인한다.
- Open 반환 직후 만료가 fd를 정확히 한 번 close하고 handle을 유출하지 않는지
  확인한다.
- 만료 예외 경로가 stat/unlink/recovery를 시작하지 않는지 확인한다.
- Effect 뒤 만료가 `deadline=None` recovery를 호출하지 않는지 확인한다.
- 남겨진 safe temp가 다음 fresh budget에서만 bounded reconciliation되는지
  확인한다.

### 10.3 Cleanup accounting

- 두 identity force에서 첫 signal 후 두 번째 pidfd open 전/후 만료를 각각
  재현한다.
- 두 경우 모두 report가 반환되고 outcome 수가 exact action 수와 같으며
  partial/unavailable/nonzero인지 확인한다.
- Singleton, TERM, effect failure, post-effect indeterminate 기존 schedules를
  함께 실행한다.

### 10.4 회귀 및 안전 검사

- focused transition/deadline/state/cleanup/Hook/supervisor tests
- 전체 `tests/codex_mcp_ownership`
- pinned baseline dependency 경로를 사용한 전체 repository tests
- Python 3.10 compileall, changed-file Ruff/format, `git diff --check`
- production canary/traceback/direct Hook signal/sleep/systemctl-shell scan
- fixture process leak scan
- live HOME/Codex config, systemd apply, nonfixture signal은 실행하지 않음

## 11. 승인 기준

1. 두 기존 HIGH 반례와 새 truth-table/boundary matrix가 모두 GREEN이다.
2. Recovery에서 phase만으로 commit을 인정하는 production 경로가 없다.
3. Time-bounded state mutation/recovery가 `DeadlineIO` 밖에서 의미 있는 raw
   syscall을 시작하지 않는다.
4. Deadline 만료 뒤 허용되는 syscall은 이미 열린 descriptor close뿐이다.
5. 첫 irreversible effect 이후 모든 종료 경로가 complete outcome vector와
   nonzero unavailable truth를 반환한다.
6. 기존 package/full/static/security/leak gate가 통과한다.
7. 독립 reviewer가 `SPEC PASS / QUALITY PASS / ARCHITECTURAL CLEAR`를 모두
   판정한다.

## 12. 잔여 위험

- Python은 이미 시작된 syscall을 선점할 수 없다. Deadline은 새 boundary를
  막지만 실행 중인 한 syscall의 반환 시간은 제한하지 못한다.
- Kernel signal delivery는 userspace가 확정할 수 없다. Effect 반환 뒤 persistence
  실패는 의도적으로 delivered-or-indeterminate no-replay 상태로 남는다.
- Safe temp reconciliation은 다음 fresh-budget 실행까지 지연될 수 있다. Temp는
  authoritative state나 event로 해석되지 않는다.
- Linux procfs, pidfd, `renameat2` 전제는 상위 설계와 동일하게 유지된다.
