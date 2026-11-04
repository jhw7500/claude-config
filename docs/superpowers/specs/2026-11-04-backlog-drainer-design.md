# #31 백로그 드레이너 설계 v3 — 2026-11-04

상태: 사용자가 v3 설계와 #31 Formal Task 시작·재개 및 구현을 승인했다(2026-11-04). 기존 Task 재개 성공 후 구현 중이다. Tribunal·PR·머지·Task finish는 승인 범위에 포함하지 않는다.
독립 검토: issue31_v3_design_review의 최종 판정 APPROVED. 열린 material finding 없음. 사용자의 구현 승인을 대신하지 않는다.
대상: https://github.com/jhw7500/claude-config/issues/31
기준: c5fb7ed961101b46281a8171afb883012cefe2b2
출발점: [2026-10-20 v2 노트](https://github.com/jhw7500/claude-config/issues/31#issuecomment-6281727724).

## 접근 노트

목표는 컨텍스트 한계로 자식 에이전트가 중단돼도 감독이 검증된 단계에서 작업을 이어 가게 하는 것이다. 기존 v2의 동시 이슈 한 개와 검증된 커밋에서 정지하는 범위를 유지한다. 이번 설계 재검토는 문서 작업이며 Task를 시작하지 않는다. 구현 승인과 Formal Task 시작 승인은 이후 구분한다.

제안은 Claude와 Codex가 공유하는 스킬 한 벌과 Python 표준 라이브러리 체크포인트 도구다. 감독은 현재 승인된 Task 하나를 유지하고 자식에게 같은 이슈의 제한된 단계를 순서대로 맡긴다. 자식이 코드를 수정하고 증거를 반환하면 감독이 검증하고 스냅샷을 저장한다. 부모와 자식은 동시에 같은 파일을 쓰지 않는다. 자식은 감독의 체크포인트나 Task 기록을 변경하지 않는다.

스냅샷은 짧은 Markdown 안의 구조화된 실행 증거다. 이슈별 Task 상태를 복제하는 별도 원장은 만들지 않는다. Task 소유권과 lifecycle의 정본은 계속 Registry다. 파일 이름이나 저장된 Claim 값으로 권한을 얻지 않으며, 재개 전에는 기존 launcher 계약과 실제 Git 상태를 다시 확인한다. 자유 형식 next 문장을 명령으로 실행하지 않는다.

단계 시작과 완료를 구별하여 기록한다. 중간에 자식이 죽으면 결과를 성공으로 추정하지 않는다. 기존 자식과 도구 실행이 종료됐고, 소유권과 작업 내용이 일치하는 경우에만 제한된 재실행을 허용한다. 감독 자체가 죽었거나 소유권이 달라졌다면 자동 takeover 없이 멈춘다. 테스트와 독립 리뷰가 확인된 커밋에서 멈추며 다음 이슈를 선점하지 않는다. 검증은 실제 자식 중단과 체크포인트만을 사용한 재생성을 포함한다. 단순 파일 파서 테스트를 에이전트 복구 성공으로 보고하지 않는다. 검토 결과와 근거를 남긴다.

## 유지할 범위와 대안

원안의 밤새 백로그 전체를 처리하는 목표를 이번 v3가 완료한다고 주장하지 않는다. v2에서 합의한 제한을 구현 가능한 첫 단계로 구체화한다. 큐 조회는 읽기 전용이며 이슈 선택·Task 시작·다음 이슈 전환은 현재 승인된 lifecycle 밖에서 자동 실행하지 않는다. [R1][R2][R3]

| Option | Native workflow | Durable checkpoint | Task lifecycle automation |
| --- | --- | --- | --- |
| A: shared skill + helper | Claude / Codex | stage evidence | none |
| B: standalone daemon | new model/API loop | new controller | expanded scope |
| C: existing handoff only | manual | reminder + document | none |

권고안은 A다. 공통 스킬에서 실제 native capability를 확인하고 도구 호출 지침만 런타임별로 나누는 선례가 있다. B는 별도 모델 호출·프로세스 수명·승인 체계를 늘린다. C는 현재 저장 알림을 재사용하지만 단계 완료 판정과 실제 자식 복구의 빈틈을 해결하지 못한다. A는 구현자의 제안이며 이번 사용자의 문서 검토 승인과 동일시하지 않는다. [R4][R5]

## 구성과 설치

- 제안 소스: skills/backlog-drain/SKILL.md, scripts/backlog-checkpoint.py, 필요한 공통 체크포인트 템플릿과 테스트. 모델 SDK나 새 패키지를 추가하지 않는다.
- SKILL은 감독의 순서·native 자식 dispatch·검증·정지 조건을 담당한다. Claude와 Codex 모두 실제 제공된 도구로 실행하며, 지원 도구가 없으면 unsupported로 정지한다. native role은 설치된 역할을 명시한다. 팀·Goal·Ralplan 세션을 자동으로 만들지 않는다.
- helper는 스키마 검사, 현재 Git 증거 수집, 스냅샷의 읽기·원자 저장만 담당한다. 에이전트 생성, 모델 호출, Task mutation, 임의 명령 실행을 하지 않는다.
- 양쪽 런타임에서 스킬이 발견되도록 명시 설치와 임시 HOME 검증을 추가한다. 현재 install.sh는 일반 스킬을 Claude에만 자동 연결하며 commands의 일반 자동 배포도 제공하지 않는다. 기존 pre-pr-tribunal의 양쪽 명시 설치 패턴을 참고하되 그 runtime·gate·state를 재사용하거나 변경하지 않는다. [R5]
- 기존 hook의 발화 여부와 관계없이 감독이 단계 경계마다 helper를 직접 호출한다. Claude 전용 훅과 Codex에 같은 보장을 가정하지 않는다. 전역 HOME 설치는 구현 검증에 포함하지 않는다. [R4]

## 실행 입력과 소유권

1. 사용자가 선택한 정확한 Issue URL과 현재 checkout을 입력으로 받는다. 큐 목록이나 의존성 힌트는 실행 권한이 아니다. 처음에는 한 이슈만 실행한다. 이슈별 승인 목록이나 모든 백로그를 시작할 권한을 만들어 내지 않는다. [R2]
2. 구현 시작 전에는 기존 /jhw:task 계약을 따른다. 명시적으로 승인된 start/resume이 성공한 Task·Claim·owner·branch·worktree 좌표만 사용한다. drainer가 Task 시작·재개·finish·switch·takeover를 대신하지 않는다. 다른 owner, active Task 충돌, 좌표 부재에서는 멈춘다. [R2][R3][R6]
3. 같은 이슈의 단계 위임은 같은 Task 안의 제한 작업이다. 자식에게 독립 Task의 Claim을 상속했다고 주장하지 않는다. 실제 native 권한·sandbox·승인 경계를 유지하고, 자식의 파일 소유 범위와 보고 형식을 명시한다. [R3]
4. Git root·물리 경로·branch·기준 HEAD·작업 트리 baseline을 보존한다. 첫 실행은 현재 Claim에 연결된 깨끗한 Task worktree에서만 시작한다. live launcher의 worktree mapping과 checkout의 exact root·realpath·branch를 대조하고 git worktree list --porcelain -z에서 같은 물리 경로·branch를 가리키는 canonical record가 하나인지 확인한다. alias·다른 writer·child cwd 이탈은 PAUSED다. 기존 #31 worktree를 발견했다는 사실로 재사용하거나 이동하지 않는다. [R3][R7]
5. 같은 물리 checkout에 대한 run admission 파일을 private 상태 root 아래 realpath의 SHA-256을 key로 O_EXCL 생성한다. 이미 active admission이 있으면 새 run을 시작하지 않는다. 같은 감독의 재개만 현재 native 실행과 기존 run의 연결을 확인해 이어 간다. 감독은 자신의 terminal checkpoint와 writer 종료를 확인한 뒤 자신의 admission을 해제할 수 있다. successor의 승인은 아래 명시적 recovery 조정 절차로만 반영하며 프로세스 부재만으로 남의 admission을 지우거나 탈취하지 않는다. 이 파일은 중복 실행 방지용이며 Task 권한이나 별도 Task 상태 원장이 아니다. 자식은 매 작업 전 exact cwd·branch를 확인한다.

### 감독 동일성의 관측 계약

snapshot의 supervisor_identity는 runtime(claude|codex), origin_adapter(같은 값), host, session_fingerprint를 기록한다. fingerprint는 현재 세션이 제공하는 origin_adapter·session_id·host 문자열 배열을 아래 canonical JSON 방식으로 직렬화한 bytes의 SHA-256이다. raw session ID는 저장하지 않는다. 현재 session_id는 native 호출 환경이 문서화한 값만 사용하고 파일명·cwd·과거 transcript·snapshot에서 추정하지 않는다. 확보할 수 없으면 unsupported로 정지한다.

재개 시 현재 세션의 fingerprint를 다시 계산해 비교하고, 기존 Task 절차의 status가 반환한 task_id·claim_id·host·branch·worktree를 별도로 대조한다. status가 origin_adapter·session_id까지 증명한다고 주장하지 않는다. fingerprint 일치는 같은 감독인지 확인하기 위한 비권위적 정합성 검사이며 Task 승인·Claim 획득·실제 native 권한을 대신하지 않는다. 어느 비교든 실패하면 PAUSED이고, 새 감독 세션은 기존 명시적 resume/recovery 절차를 거친다. [R2][R3]

## 단계와 정지 상태

아래는 실행 단계이며 Registry의 Task lifecycle 상태가 아니다.

```text
SELECTED -> PREFLIGHT -> APPROACH_REVIEW -> IMPLEMENT
                                             |
                                             v
READY_FOR_TRIBUNAL <- COMMIT <- VERIFY <- INDEPENDENT_REVIEW
       |
       +-> STOP (claim unchanged; next issue is not started)

any uncertain evidence -> PAUSED
child ended + live scope verified -> reconcile -> same stage or PAUSED
supervisor/owner changed -> PAUSED (existing Task recovery contract)
```

- APPROACH_REVIEW는 현재 이슈의 짧은 접근 노트를 독립 검토한다. 독립 검토의 REVISE는 반영 후 다시 검토하고, unresolved blocker가 있으면 구현으로 넘어가지 않는다. 이번 #31 설계 재검토는 그 이전 문서 작업이다. [R1][R7]
- IMPLEMENT 전에는 단계를 started로 저장하고 감독이 선택한 파일만 자식에게 맡긴다. 자식은 동시에 하나만 쓰기 작업을 한다. 감독은 자식 실행 중 같은 소스를 편집하지 않는다.
- INDEPENDENT_REVIEW와 VERIFY는 실제 결과·검토 대상 diff·입력 parent HEAD·테스트 실행 범위와 검증된 source manifest digest를 함께 남긴다. 이후 내용·mode·경로 집합이나 parent가 달라지면 관련 증거를 무효화하고 필요한 검증을 다시 수행한다. 동일 내용을 commit한 것만으로 기존 검증을 무효화하지 않는다. 단순 성공 문자열은 증거가 아니다. [R7]
- COMMIT은 저장소의 Change Evidence 계약과 실제 검증 결과를 사용한다. COMMIT started 레코드에는 parent_head, 검증된 change_digest, source manifest의 immutable 참조를 먼저 저장한다. 결과 commit은 부모가 정확히 parent_head 하나이고, 부모 대비 변경 경로·mode·내용이 manifest와 완전히 일치하며 작업 트리에 추가 source 변경이 없을 때만 채택한다. 이 post-commit binding으로 review/test 증거를 결과 commit OID에 연결한다. commit hook이 내용을 바꾸었거나 parent·digest가 다르면 PAUSED다. [R7]
- commit 후 checkpoint 전 중단에서는 저장한 parent의 직계 자식 중 현재 HEAD와 일치하는 commit 하나만 위 조건으로 채택한다. 후보가 없거나 여럿이거나 중간 commit이 있으면 PAUSED이고 재커밋하지 않는다. 기존 증거가 post-commit binding을 통과한 뒤에만 READY_FOR_TRIBUNAL로 간다.
- READY_FOR_TRIBUNAL에서 코드 변경과 자식 실행을 끝낸다. Task Claim은 유지하며 completed/finish/handoff를 자동 호출하지 않는다. 사용자에게 커밋·검증·현재 Claim·다음 구체 액션을 보고한다. 장기 중단·이슈 전환에는 기존 Task handoff 절차를 사용자 요청에 따라 적용한다. Tribunal·PR·머지는 각각 현재 허용 범위의 별도 요청으로만 실행한다. [R1][R2]

터미널 해제의 현재 child slot은 READY phase와 attempt가 같아야 한다. 이 단계에 실제 자식이 없으면 이전 phase의 모든 자식·도구·writer 종료를 관측한 뒤 child_id=null, 현재 phase/attempt, quiescent로 기록한다. 과거 실제 child ID의 phase를 바꿔 꾸미지 않고 그 영수증은 immutable evidence에 보존한다. PAUSED 상태의 admission은 정상 해제하지 않는다. Task Claim 해제와는 별개다.

## 스냅샷 형식과 저장

스냅샷은 repo root의 HANDOFF.backlog-<run-id>.md 한 파일이다. run-id는 충돌 방지용 난수이며 identity나 권한의 근거가 아니다. 하나의 엄격한 JSON 레코드를 한 줄로 담아 전체 15줄 이내를 유지한다. 감독만 이 파일을 갱신하며 .ai/handoff.md와 Registry를 직접 수정하지 않는다. 일반 /resume의 자유 형식 문서를 바꾸지 않고 backlog-drain의 명시 경로 재개만 지원한다. [R4][R8]

다음이 schema 1의 필수 필드다. JSON bool을 정수로 받지 않으며 추가 키·중복 키·잘못된 타입은 거부한다. UTF-8 길이는 bytes 기준이다.

- schema는 정수 1, run_id는 32자리 lowercase hex, generation은 0 이상 정수, phase는 위 단계 또는 PAUSED, phase_status는 started|verified, attempt는 0|1이다. next_action은 해당 단계명 또는 STOP이고 임의 명령이 아니다. blockers는 ID 문자열 최대 16개, 각 128 bytes다.
- supervisor_identity는 위 관측 계약을 따른다. host는 255 bytes 이하, session_fingerprint는 64자리 lowercase SHA-256 hex다.
- binding은 issue_url(256 bytes 이하), task_id·claim_id(각 128 bytes 이하), repo_root·worktree_root(각 absolute 2048 bytes 이하)다. 이는 당시 관측 기록이다.
- git은 branch(256 bytes 이하), base_head·observed_head(40자리 hex), source_count(0~10000), path_set_digest·change_digest(64자리 hex), commit_parent(40자리 hex 또는 null), result_commit(같은 형식 또는 null)이다. 전체 변경 목록은 snapshot에 넣지 않는다.
- child는 실제 native child_id(256 bytes 이하 또는 null), phase, attempt, observed_status(running|quiescent|interrupted|completed|unknown)다. SDK·TUI의 서로 다른 ID를 같은 값으로 추정하지 않는다.
- evidence는 manifest 상대경로(ASCII 128 bytes 이하), sha256(64자리 hex), size(0~1MiB 정수)다. 상세 테스트·리뷰·변경 목록은 이 immutable 증거에 둔다.

JSON canonical bytes는 UTF-8, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False의 JSON 직렬화이며 마지막 newline은 제외한다. digest는 이 bytes의 SHA-256이다. 경로는 유효한 UTF-8만 지원하고 Unicode 정규화를 하지 않으며 control 문자·NUL·상대 참조의 absolute path와 '..'를 거부한다. 추적 상태의 source path 집합은 repo 상대 UTF-8 bytes 오름차순으로 정렬하여 각 bytes 뒤에 NUL 하나를 붙인 전체를 SHA-256한다. rename은 delete+add로 풀어 Git 설정에 따른 rename 추정 차이를 없앤다.

source manifest는 경로별 add|modify|delete, 결과 mode, 결과 bytes의 SHA-256(delete는 null)을 포함한 canonical JSON 배열이다. change_digest는 이 배열 bytes의 SHA-256이며 원본 코드나 허용 범위 밖 파일을 증거 원장에 복제하지 않는다. commit의 부모 대비 변경도 같은 형태로 재생성해 비교한다. 타인의 변경은 digest에 포함해 승인한 것으로 만들지 않는다.

상세 evidence는 현재 사용자 소유의 private 상태 디렉터리 아래 backlog-drain/<run-id>/에 저장한다. absolute state root는 실행 시작 때 고정하고 checkpoint 밖의 원장으로 Task status를 복제하지 않는다. manifest는 1MiB, 개별 raw log는 16MiB 상한이며 상대경로·bytes 길이·SHA-256으로 참조한다. 검증 기록은 cwd·실제 argv·입력 식별자·허용한 환경 값·count·exit code·raw log 참조를 담는다. 인증 값이나 전체 환경을 복사하지 않는다. 파일은 같은 UID의 non-symlink regular file만 descriptor-relative로 열며 범위 밖 참조는 거부한다. snapshot pointer 교체 전에 immutable evidence 저장과 fsync를 끝낸다. [R10]

JSON의 phase는 실행 복구의 힌트이며 Task status가 아니다. 임의 shell·인증 토큰·전체 transcript를 넣지 않는다. 완성된 snapshot은 UTF-8 12KiB 이하·15줄 이하를 검증한다. 상한 초과는 자르지 않고 PAUSED다. 1000개 변경 경로·200개 테스트·100개 리뷰 항목을 manifest에 둔 정상 fixture가 이 snapshot 예산 안에 들어오는지 시험한다. [R2][R4]

저장 실패는 publish 전 일반 실패와 publish 뒤 내구성 미확정 상태를 구분한다. 전자는 기존 snapshot을 보존하고, 후자는 명시적 indeterminate 오류로 PAUSED 처리한다. 이미 관측됐을 수 있는 generation을 자동 rollback하거나 곧바로 재시도하지 않는다. 필요한 no-follow/dirfd 기능이 없으면 파일 변경 전에 unsupported로 정지한다. JSON nesting은 최대 64이며 Unicode Cc 제어문자는 경로에서 거부한다.

저장에는 안전한 디렉터리 안의 임시 regular file, 현재 UID, 0600, flush/fsync, atomic rename, 디렉터리 fsync를 사용한다. 같은 실행의 쓰기 잠금 안에서 현재 generation과 caller의 expected_generation이 같을 때만 generation+1을 저장한다. 새 파일은 expected_generation=null인 create만 허용한다. symlink·다른 UID·경로 이탈은 거부하며 실패한 저장 뒤에는 다음 단계로 진행하지 않는다. 이는 실행 증거의 무결성 규칙이며 Task 소유권을 부여하는 lock이 아니다. untracked snapshot은 exact 경로 하나만 실행 산출물로 분리해 source digest·commit에서 제외하며, 기존 tracked 파일을 덮어쓰거나 ignore 설정을 자동 변경하지 않는다.

## 중단과 재개

1. 정확한 스냅샷 경로를 지정해 로드한다. 최신 mtime이나 HANDOFF glob으로 다른 실행의 파일을 고르지 않는다. 현재 PreCompact hook의 전체 glob은 drainer의 완료·복구 판정에 사용하지 않는다. 그 hook의 일반 동작 수정은 이번 범위에 넣지 않는다. [R8][R9]
2. Task Registry와 현재 launcher 계약을 우선 확인한다. 감독이 동일한 Claim을 계속 보유하는 경우에만 자식 재실행 후보가 된다. 감독 세션이 바뀌었으면 snapshot만으로 실행을 재개하지 않는다. 명시적 Task resume 또는 별도 승인된 recovery 결과가 필요하다. 프로세스가 없어 보인다는 사실을 takeover 승인으로 바꾸지 않는다. [R2][R3]
3. native 자식의 terminal 상태와 이전 도구 실행의 종료를 확인한다. 완료 여부를 알 수 없는 도구·백그라운드 writer가 있으면 새 writer를 만들지 않고 PAUSED로 보고한다. 자식에게 무추적 background 프로세스를 허용하지 않는다. [R7]
4. Git 상태와 기록을 대조한다. verified 단계의 입력·diff·HEAD가 일치하면 다음 단계로 간다. started만 기록된 단계는 완료가 아니다. 무변경이면 같은 단계의 새 attempt를 만들 수 있고, 변경이 있으면 감독이 diff·커밋·실행 증거를 조정한 뒤 확인된 작업만 이어 간다. 불일치가 설명되지 않으면 PAUSED다. 기존 변경을 삭제하거나 중단된 단계를 통째로 재실행하지 않는다.
5. 자식 재실행은 한 phase당 최대 1회다. 같은 단계의 두 번째 중단·저장 실패·범위 변경은 PAUSED로 끝낸다. commit·외부 게이트·Task lifecycle에 exactly-once 보장을 주장하지 않는다. 이미 일어난 외부 효과를 재요청으로 보상하지 않는다.

### 승인된 supervisor recovery 뒤의 admission 조정

사용자가 별도 승인한 기존 Task resume/recovery가 실제로 성공한 경우에만 successor 조정을 허용한다. 이 문서의 runner가 그 lifecycle을 호출하거나 승인하지 않는다. 기존 admission의 exact bytes·SHA-256·run-id·physical-root key와 이전 snapshot의 digest/generation을 먼저 고정한다. 같은 Task/Issue의 live successor Claim·owner·branch·worktree, 이전 run의 child/tool/writer quiescence, 현재 Git 증거가 모두 확인돼야 한다. Task 성공 결과 없이 fingerprint만 바꿔 재개하지 않는다.

admission의 생성·해제·조정은 같은 root-key lock 안에서 수행한다. successor 조정은 초기 생성의 O_EXCL과 달리 exact active record의 CAS + atomic replace다. 옛 active를 옮기거나 삭제하지 않은 채 exact bytes의 O_EXCL private archive를 먼저 저장하고 파일·디렉터리를 fsync한다. successor의 새 snapshot과 임시 admission을 준비·fsync한 뒤, old active가 고정 bytes 그대로인지 다시 비교하고 active 경로만 atomic replace·디렉터리 fsync한다. archive가 이미 존재하면 exact bytes 일치만 허용하며 자동 덮어쓰지 않는다.

durable invariant는 조정의 모든 crash boundary에 old active 또는 successor active가 반드시 존재한다는 것이다. archive 전·후와 successor publish 전·후에 죽어도 admission 없이 새 실행을 허용하는 상태를 만들지 않는다. 증거가 불완전하거나 임시 successor가 남았으면 이를 정상 빈 상태로 읽지 않고 exact reconciliation 또는 PAUSED로 처리한다. 새 snapshot은 successor binding/identity와 이전 snapshot digest를 기록하며 이전 단계는 보조 증거로만 재검증한다. 더 이상 옛 Claim/fingerprint 동일성을 요구하지 않는다. 자동 Task rollback·다른 파일 삭제·mtime 기반 archive 선택은 하지 않는다. [R2][R3]

## 읽기 전용 큐 순서

선택된 저장소의 열린 Issue와 실제 blocked-by 관계를 각각 모든 pagination이 끝날 때까지 읽고, user-supplied exact Issue URL pair와 합친다. 관계 조회원은 GET /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by이며, per_page=100으로 Link 헤더의 rel=next를 마지막 성공 응답까지 따라간다. edge B->I는 I가 B에 blocked-by임을 뜻한다. 제목 유사도나 본문 속 임의 링크로 edge를 만들지 않는다. 모든 페이지·참조 Issue의 open/closed 상태를 확인하기 전에는 complete queue를 발표하지 않는다. HTTP 오류·누락 페이지·상한 초과·미조회 Issue는 INCOMPLETE와 이유만 출력하고 순위를 내지 않는다. 조회 상한은 열린 Issue 1000개·관계 10000개다. [R1][R2][R12]

closed blocker는 충족된 것으로 분리 표시하고 edge에서 제거한다. 열린 외부 저장소 blocker가 있으면 해당 Issue와 그 downstream을 blocked 집합으로 분리한다. 나머지 open-issue graph는 Kahn topological sort를 사용하고 zero-indegree 동률은 Issue 번호 오름차순으로 고정한다. cycle SCC와 그 downstream은 ordered queue 밖에 남겨 cycle/external/downstream 이유를 출력한다. 목록 조회는 Task·worktree·브랜치·Issue를 변경하지 않는다.

고정 fixture는 열린 Issue {1,2,3,4,5,6,7,8}, edge {1->3,2->3,3->4,5->6,6->5,6->7}, closed blocker 99->8이다. 기대 queue는 [1,2,3,4,8], blocked는 {5:cycle,6:cycle,7:downstream}이다. 99 조회가 실패하거나 관계 pagination이 불완전하면 queue 없이 INCOMPLETE다.

## 검증과 완료 기준

다음은 구현 후 충족할 기준이다. 이번 설계 문서 작성으로 통과했다고 기록하지 않는다.

| ID | Scenario | Required result |
| --- | --- | --- |
| C1 | valid snapshot / changed generation / large evidence fixture | exact parse / stale write rejected / snapshot <= 12KiB |
| C2 | partial write / symlink / wrong owner / oversized file | old record intact / reject |
| C3 | native interrupt at pre/post-edit barrier in each runtime | fresh child ID + no history inheritance; reconcile or unsupported/PAUSED |
| C4 | supervisor or Claim changes | PAUSED; no Task mutation |
| C5 | commit succeeds before checkpoint | unique matching parent + manifest adopted; mismatch PAUSED |
| C6 | another session has newer HANDOFF | explicit run file only |
| C7 | tests or diff change after verification | previous evidence invalidated |
| C8 | terminal commit reached | no Tribunal, PR, merge, finish, or next issue |
| C9 | issue dependencies / cycle / incomplete reads | deterministic order + reasons / blocked |
| C10 | temporary HOME installation | Claude + Codex skill discovery |
| C11 | worktree alias / duplicate branch / child cwd escape | PAUSED before write |
| C12 | valid Task worktree + serial children / duplicate admission | exact cwd preserved / second run rejected |
| C13 | backlog snapshot + general PreCompact glob | freshness interaction recorded; no recovery decision from glob |
| C14 | approved successor recovery + crash before/after archive and publish | old or successor active always present; missing proof PAUSED |

검증의 두 층위를 구별한다. helper의 단위·프로세스 테스트는 소유한 fixture 자식 프로세스만 실제로 중단하여 쓰기 전·rename 전·commit 후를 재현한다. 이 결과를 native 에이전트 전체의 복구 증거라고 보고하지 않는다.

구현 후 동적 확인은 fixture Task worktree와 승인된 native 자식을 사용한다. 자식은 먼저 pre-edit barrier를 보고하고 쓰기 없는 대기 상태에 들어간다. 다른 시나리오는 fixture 한 파일의 변경을 마친 뒤 완료 checkpoint를 저장하기 전에 post-edit barrier를 보고한다. 감독은 각 barrier에서 정확한 자식 run을 중단하고 중단 상태·pending tool 종료·추적한 writer의 quiescence를 각각 관측한다. 응답·PID·로그 정체 하나만으로 종료를 추정하지 않는다. 실제 중단 실험의 통과에는 중단 요청 전 running 관측과 같은 run의 interrupted/stopped 증거가 필요하다. 요청 전에 이미 completed였던 자식은 실제 중단 실험을 통과한 것으로 세지 않는다.

- Codex: collaboration.interrupt_agent는 현재 turn만 중단하므로 agent 객체가 제거됐다고 기록하지 않는다. list_agents와 소유한 tool/session 결과로 quiescence를 확인하고 예전 ID는 다시 followup_task/send_message로 깨우지 않는다. replacement는 설치된 역할을 명시한 spawn_agent, fork_turns='none'으로 새 child ID를 얻는다. 이번 세션에 노출된 native tool 계약을 근거로 하며 지원하지 않는 환경에는 같은 보장을 약속하지 않는다.
- Claude: 실제 설치가 Agent background 실행, 정확한 대상의 TaskStop, 같은 run에 결합된 terminal(stopped/failed/completed) 관측을 제공할 때만 실험한다. SDK 문서의 event가 native TUI에도 노출된다고 가정하거나 agentId·taskId를 같다고 추정하지 않는다. replacement는 새 non-fork Agent 호출과 새 ID를 확인한다. 기존 child에 SendMessage/resume하지 않는다. [R11]

새 자식에는 checkpoint·현재 Git 상태·live Task 확인과 다음 한 단계만 주고 이전 transcript·자동 history fork를 넘기지 않는다. 변경이 한 번만 남고 검증·독립 리뷰·post-commit binding을 거쳐 같은 종료 상태에 도달하는지 확인한다. 두 런타임 각각 이 절차를 실행하며, 해당 중단·terminal·새 context capability가 없으면 그 런타임의 복구는 unsupported다. 도구나 writer의 종료를 입증하지 못한 경우는 PAUSED로 보고한다. unsupported/PAUSED를 native 복구 통과로 세지 않는다.

현재 일반 PreCompact glob에는 새 HANDOFF.backlog-*.md도 들어가므로 일반 compaction freshness에 영향을 주는 것은 기존 glob 동작의 결과로 명시한다. C13은 이 공존을 확인한다. drainer의 완료·복구에는 exact snapshot만 사용하며 일반 hook 의미를 바꾸는 수정은 하지 않는다. [R9]

검증 순서는 기존 관련 테스트의 기준선 확인, 새 회귀의 RED, 구현 후 GREEN, 표적 정적 검사, 필요한 통합·전체 테스트, 독립 리뷰다. 기존 hooks와 /resume의 동작은 바꾸지 않으므로 그 변경을 확인하는 테스트를 새로 만들지 않는다. [R4][R7][R8]

## 구현 전 남겨 둘 결정과 후속 단계

- 권고안 A와 위 정지 경계가 사용자의 구현 의도에 맞는지 설계 승인으로 확정한다. 신규 Formal Task 또는 기존 #31 Task 재개는 별도 명시적 승인을 받은 뒤 기존 launcher로만 수행한다. 기존 #31 worktree나 Task 표시는 탐색 힌트이며 Claim의 근거가 아니다. [R2][R3][R7]
- 설계 승인 뒤에 writing-plans로 파일·RED/GREEN·설치 검증·native 중단 증거 수집을 구현 단계로 나눈다. 구현 계획을 이 문서의 검증 완료 기록으로 혼동하지 않는다.
- #31을 닫을지는 좁혀진 v3 기준과 원안에서 남은 무인 큐 처리 범위를 함께 검토해 사용자에게 보고한다. 이 문서만으로 원래 Issue를 완료 처리하지 않는다. [R1]

## 근거

- [R1] [Issue #31](https://github.com/jhw7500/claude-config/issues/31), [사용자 v2 메모](https://github.com/jhw7500/claude-config/issues/31#issuecomment-6281727724).
- [R2] [설치된 Task 절차: 승인·충돌](/home/jhw/.codex/skills/jhw-task/references/task.md:20), [switch의 부분 완료](/home/jhw/.codex/skills/jhw-task/references/task.md:414), [recovery 별도 승인](/home/jhw/.codex/skills/jhw-task/references/task.md:436). 설치된 외부 workflow 계약이며 이 저장소의 Task 서버 구현이 아니다.
- [R3] [Task 단위와 owner tuple](/home/jhw/.codex/skills/jhw-task/references/task.md:59), [원칙과 Registry 우선](/home/jhw/ai/opencode/projects/claude-config/claude-md/global-guidance.md:24).
- [R4] [checkpoint hook main](/home/jhw/ai/opencode/projects/claude-config/hooks/handoff-checkpoint-hook.py:52), [reminder 출력](/home/jhw/ai/opencode/projects/claude-config/hooks/handoff-checkpoint-hook.py:126), [15줄 스냅샷 지침](/home/jhw/ai/opencode/projects/claude-config/claude-md/guidance/handoff-checkpoint.md:9).
- [R5] [Claude 스킬 설치](/home/jhw/ai/opencode/projects/claude-config/install.sh:90), [명시 command 목록](/home/jhw/ai/opencode/projects/claude-config/install.sh:145), [양쪽 런타임 설치 선례](/home/jhw/ai/opencode/projects/claude-config/scripts/install-pre-pr-tribunal.py:313), [native capability 분기 선례](/home/jhw/ai/opencode/projects/claude-config/skills/pre-pr-tribunal/SKILL.md:25).
- [R6] [Task preflight 분류](/home/jhw/ai/opencode/projects/claude-config/scripts/jhw-control-host.py:1256), [start 결과 검증](/home/jhw/ai/opencode/projects/claude-config/scripts/jhw-control-host.py:1548), [busy 오류 보존 테스트](/home/jhw/ai/opencode/projects/claude-config/tests/test_jhw_control_host.py:3369).
- [R7] [독립 검증·작은 변경](/home/jhw/ai/opencode/projects/claude-config/claude-md/global-guidance.md:17), [Task 권유와 첫 변경](/home/jhw/ai/opencode/projects/claude-config/claude-md/global-guidance.md:29), [다른 writer 확인](/home/jhw/ai/opencode/projects/claude-config/claude-md/global-guidance.md:36), [Change Evidence](/home/jhw/ai/opencode/projects/claude-config/claude-md/global-guidance.md:40).
- [R8] [명시 스냅샷 선택](/home/jhw/ai/opencode/projects/claude-config/commands/resume.md:12), [현재 Git 대조](/home/jhw/ai/opencode/projects/claude-config/commands/resume.md:31).
- [R9] [현재 PreCompact의 전체 HANDOFF glob](/home/jhw/ai/opencode/projects/claude-config/hooks/precompact-handoff.sh:34).
- [R10] [검증 증거의 필수 기록](/home/jhw/ai/opencode/projects/claude-config/claude-md/global-guidance.md:174).
- [R11] [Claude 공식 Agent/TaskStop 타입과 task event](https://code.claude.com/docs/en/agent-sdk/typescript), [non-fork subagent와 resume](https://code.claude.com/docs/en/sub-agents), [현재 tools reference](https://code.claude.com/docs/en/tools-reference). 2026-11-04 조회한 문서이며 실제 설치 capability 확인을 대체하지 않는다. SDK 도입은 제안하지 않는다.
- [R12] [GitHub 공식 blocked-by endpoint](https://docs.github.com/en/rest/issues/issue-dependencies#list-dependencies-an-issue-is-blocked-by), [Link 기반 pagination 완료 규칙](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api). 2026-11-04 조회.
