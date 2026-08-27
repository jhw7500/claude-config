# jhw-control-host v4 Task lifecycle 설계

- 날짜: 2026-08-27
- 상태: 사용자 승인
- 대상: `jhw7500/claude-config#28`, `jhw7500/jhw-notion#74`

## 배경

`jhw-control-host` v3는 secure-store-only credential broker와 실행 격리를 제공하지만 Task 명령은
`start`와 `finish`만 지원한다. `jhw-task` 정본은 나머지 lifecycle 명령을 raw `jhw-control`로
호출한다. 그 결과 secure-store-only 환경에서는 legacy contract migration, completion evidence,
status, recovery, ownership 확인이 중간에 끊긴다.

v3가 downstream의 success/error schema를 다시 엄격하게 구현한 것도 반복 장애를 만들었다.
`task_role` 추가와 `TASK_CONTRACT_*` 오류 추가 때 downstream은 정상 동작했지만 host가 출력을
`CONTROL_OUTPUT_INVALID`로 바꿨다. v4는 credential 경계를 유지하되 schema 정본을
`jhw-control` 한 곳으로 되돌린다.

## 목표

1. canonical Task lifecycle의 모든 현재 명령을 secure-store-only host로 실행한다.
2. parent shell에는 credential을 주입하지 않고 child `jhw-control`에만 전달한다.
3. mutation은 hidden preflight 뒤에만 실행하고, 읽기 전용 진단은 preflight 장애 때도 사용할 수 있게 한다.
4. host가 downstream result/error schema를 중복 구현하지 않게 해 additive 변경과 새 stable error code를 허용한다.
5. 기존 v3 `task start`·`task finish` 호출자의 핵심 출력 계약은 유지한다.

## 비목표

- 임의의 `jhw-control` 명령을 generic passthrough하지 않는다.
- credential의 파일 저장 fallback, shell source, parent environment export를 추가하지 않는다.
- host가 Task switch, contract migration, recovery 정책을 자동 결정하지 않는다. 조합과 승인 지점은 `jhw-task`가 담당한다.
- host가 GitHub Issue를 생성하거나 닫지 않는다.
- downstream이 의도적으로 출력한 모든 내부 필드를 host가 공개해야 한다고 보지 않는다.

## v4 public surface

`--contract`는 version `4`, credential policy `secure-store-only`, 다음 exact command family를 반환한다.

- `unlock`
- `preflight`
- `portfolio status`
- `task start`
- `task child-start`
- `task contract`
- `task completion-ready`
- `task promote`
- `task status`
- `task handoff`
- `task finish`
- `task recover`
- `task assert-owner`

위 목록 밖의 Task subcommand는 credential 조회나 child 실행 전에 거부한다. `switch`는 별도 명령이
아니며 기존처럼 `finish`와 `start`의 사용자 승인된 조합으로 남는다.

## 책임 분리

### host가 계속 책임지는 것

- trusted executable·ancestor·고정 PATH 검증
- Secret Service와 GitHub keyring에서 credential을 읽어 격리된 child environment에만 주입
- exact command family allowlist
- timeout, process-group 종료, 최대 12 KiB capture
- 성공 stdout 또는 실패 stderr 중 한 stream만 허용
- 단일 JSON, 중복 key 거부, 요청 command와 응답 command 일치
- credential, protected config/store/state path, 입력 checkout path의 raw·encoded 유출 검사
- canonical JSON 재직렬화
- v3 start/finish의 기존 핵심 projection과 요청 좌표 binding

### `jhw-control`이 유일하게 책임지는 것

- command별 flag와 lifecycle 규칙
- result object의 상세 schema
- stable error code와 code별 의미
- reason vocabulary와 command별 도달 가능성
- Task/Claim/Work Contract/Registry mutation의 정합성

host는 Task result object의 exact key 집합을 복제하지 않는다. 새 안전 필드가 추가되어도 command,
stream, byte bound, sensitive scan, 기존 필수 좌표가 유효하면 호환된다. error는 대문자 stable code를
그대로 보존하며 code별 allowlist와 exit-code 조합표를 host에 다시 복제하지 않는다.

## 공통 출력 처리

Task command는 다음 공통 envelope만 검사한다.

- 성공: exit `0`, stderr empty, stdout에 `{ "command": <exact>, "result": <object> }`
- 실패: exit `1|2|4|75|78`, stdout empty, stderr에 `{ "error": { "code": <stable-code>, ... } }`
- 공통 optional warning은 현재의 bounded warning vocabulary만 유지한다.

성공 result는 downstream JSON을 canonical 재직렬화해 전달한다. 다만 v3 호환이 필요한 `task start`와
`task finish`는 현재 public 핵심 필드를 계속 project한다. 이 projection은 필요한 필드의 type·좌표
관계만 확인하고 추가 downstream field를 거부하지 않는다. `task start`는 `task_id`, `claim_id`,
`branch`, `worktree_ref`, optional `latest_handoff`; `task finish`는 현재 required/base 및 조건부 필드를
유지한다. `task child-start` 성공은 start와 같은 네 좌표로 간소화한다.

실패 error는 `[A-Z][A-Z0-9_]{1,63}` 형식의 `code`를 항상 보존한다. `reason`은 downstream vocabulary를
복제하지 않고 `[a-z][a-z0-9_]{0,63}` 형식의 bounded lowercase snake-case 식별자인지만 확인한다. `conflicting_claim`,
`retained_claim`, `retained_task`처럼 workflow 분기에 필요한 metadata는 bounded canonical coordinate
형식을 통과할 때만 보존하고 나머지 detail은 버린다. 새 stable code는 metadata 없이도 그대로
전달되므로 host 배포 없이 원인을 확인할 수 있다.

## preflight 정책

다음 mutation은 host가 같은 credential child environment에서 hidden preflight를 먼저 실행한다.

- `task start`
- `task child-start`
- `task contract`
- `task completion-ready`
- `task promote`
- `task finish`
- `task recover --action force-end|takeover|cleanup`

다음 읽기 전용 명령은 hidden preflight를 생략한다. launcher의 credential store, trust-chain, bounded
output 검사는 그대로 적용한다.

- `task status`
- `task handoff`
- `task assert-owner`
- `task recover --action status`

`task recover`에서 `--action status`가 정확히 한 번 확인되지 않으면 mutation으로 분류한다. malformed
요청이 preflight를 우회해 mutation으로 이어지지 않게 보수적으로 처리하고 최종 flag 오류는 downstream이
판정한다.

## consumer 전환

`jhw-notion/skills/claude/task.md`의 raw `jhw-control task ...` 호출을 모두
`"$HOME/.local/bin/jhw-control-host" task ...`로 바꾼다. 대상은 `child-start`, `status`, `contract`,
`handoff`, `promote`, `completion-ready`, `recover`, `assert-owner`다. 이미 host를 사용하는 `start`와
`finish`는 유지한다.

사용자 승인 규칙, resume/switch/recovery 순서, success 결과의 사용자-facing projection은 바꾸지 않는다.
Codex와 Claude에 설치되는 skill은 같은 `jhw-notion` 정본에서 동기화하며 raw CLI fallback을 문서화하지 않는다.

## 배포 순서

1. `claude-config`에서 v4 contract, 공통 envelope, command allowlist, preflight 분류를 TDD로 구현한다.
2. 전체 테스트와 독립 보안 리뷰 후 producer를 merge하고 `install.sh`로 stable host를 설치한다.
3. clean environment에서 `--contract` version 4와 `preflight`를 검증한다.
4. `jhw-notion`의 Task skill과 contract tests를 host-only로 전환한다.
5. consumer 전체 테스트·리뷰·merge·install을 마친다.
6. #74 legacy Task를 공식 좌표로 `status → finish --status handoff → contract → start →
   completion-ready → finish --status completed` 순서로 migration하고 Issue 후속 처리를 한다. `status`
   결과의 canonical Task/Claim/Repository 좌표만 다음 명령에 사용하며, 각 mutation은 기존 `jhw-task`
   승인 규칙을 따른다.

producer가 설치되기 전 consumer를 배포하지 않는다. 실패 시 raw CLI나 ambient credential로 fallback하지 않는다.

## 테스트와 수용 조건

### claude-config

- `--contract`가 exact v4 command inventory를 반환한다.
- 목록 밖 subcommand는 credential 조회 전 거부된다.
- mutation/read-only/recover-action preflight truth table을 고정한다.
- 모든 Task command에서 단일 JSON, stream, size, command binding, secret/path scan을 공통 검증한다.
- additive result field와 새 stable error code는 통과하고 malformed envelope·unsafe metadata는 거부한다.
- v3 start/finish projection과 coordinate binding은 회귀하지 않는다.
- installer copy/hash, Python compile, shellcheck, 전체 pytest가 통과한다.

### jhw-notion

- Task skill에 raw `jhw-control task` 호출이 남지 않는다.
- 10개 canonical Task command와 switch/recovery 조합이 absolute host path를 사용한다.
- 기존 authorization, no-retry, no-takeover, four-field start reporting 규칙이 유지된다.
- typecheck, Task skill contract test, 전체 test/build/install sync가 통과한다.

### live acceptance

- installed host와 source hash가 일치한다.
- clean environment contract/preflight가 통과한다.
- #74 legacy Task migration의 각 command가 stable bounded JSON을 반환한다.
- completed finish 후 worktree cleanup과 Issue 상태를 별도로 확인한다.

## 예상 실패와 대응

- downstream이 malformed JSON이나 mixed stream을 내면 `CONTROL_OUTPUT_INVALID`로 fail-closed한다.
- downstream에 안전한 새 result field/error code가 추가되면 host 재배포 없이 전달된다.
- output에 credential 또는 protected path가 들어가면 schema 유효 여부와 관계없이 전체를 폐기한다.
- preflight가 실패하면 mutation child를 호출하지 않는다. 읽기 전용 status/recovery 진단은 계속 사용할 수 있다.
- consumer가 v4보다 먼저 설치되면 unsupported command로 중단한다. raw CLI fallback은 하지 않는다.
