# #47 Codex 서술 계측 설계안 — 2026-11-01

상태: 사용자 “승인 구현”(2026-11-01)으로 승인됐으며 구현·검증을 완료했고 로컬 변경을 보존한다.
대상: https://github.com/jhw7500/claude-config/issues/47
기준 커밋: 557b5e3f469b59050dca62fddc30e027b42ca193
기존 Claude CLI 기준선: tests/test_delegation_ratio.py — 19 passed (0.40s).

## 목적과 승인된 범위

사용자는 기존 실험과 분리한 Codex 파서·서술 지표 추가를 요청했다. 현재 Claude 계측은 ~/.claude/projects의 호출·tool_result만 읽으므로 Codex의 작업과 부모/자식 도구 출력이 빠진다. Codex 로컬 기록을 읽어 이 누락을 관찰할 수 있게 한다.

1차 delegate-nudge 실험의 기준, 입력, 분모, default 출력, hook 배선은 그대로 둔다. Codex 수치를 1차 실험의 성공·롤백 판정에 합치지 않는다. 현재 누적 자료의 보류 판정도 이 변경으로 바꾸지 않는다.

CLI 이름·세부 집계 방식은 구현자의 제안으로 작성했고, 이후 사용자의 명시적 설계·구현 승인을 받았다.

## 접근과 CLI

권장: **독립 Codex 집계기 + 기존 CLI의 명시적 선택 진입점**.

- scripts/codex-delegation-ratio.py를 독립 파서·CLI로 추가한다. Python 3.10 표준 라이브러리만 사용한다.
- delegation-ratio.py에 --source claude|codex를 추가하고 기본은 claude로 둔다. codex일 때만 새 집계기로 넘긴다. 기존 Claude collect/is_direct 및 JSON 필드는 수정하지 않는다.
- --root, --days, --since, --overall, --json의 의미를 유지한다. Codex의 기본 root는 ~/.codex/sessions다. 설치된 symlink에서 호출해도 실제 소스 옆의 집계기를 찾는다.
- Codex JSON에는 provider=codex, descriptive_only=true, 별도 overall/days/coverage를 기록한다. 텍스트에도 Codex 전용·서술 지표·저장된 출력 바이트라는 정의를 표시한다.
- install.sh의 명시적 스크립트 목록에 새 CLI만 추가하고 임시 HOME 테스트로 확인한다. 실제 사용자 HOME의 installer는 실행하지 않는다.

기존 함수에 Codex 이름·경로를 섞는 방식은 동기화 계약과 분모를 흔들어 제외한다. 완전히 분리된 CLI만 두면 #47의 기존 도구 진입점에서 Codex 지표를 고를 수 없으므로, 작은 라우팅 선택지만 기존 CLI에 둔다. Claude/Codex 합산 모드는 제공하지 않는다.

예상 사용:
- python3 scripts/delegation-ratio.py --json
- python3 scripts/delegation-ratio.py --source codex --days 14 --json
- python3 scripts/codex-delegation-ratio.py --root <fixture> --overall --json

## 기록의 정체성과 범위

공식 0.166.0 및 실제 로컬 기록에서 확인한 session_meta / response_item을 기준으로 한다. 첫 session_meta만 파일의 정본 메타데이터로 쓴다. 자식 파일 뒤에 복사된 부모 메타데이터로 역할을 바꾸지 않는다.

- id는 thread ID, session_id는 root ID다. 파일명이나 마지막 메타데이터로 이를 덮어쓰지 않는다.
- source.subagent.thread_spawn 및 parent_thread_id로 자식의 출처를 확인한다. 단순히 subagent라는 이유만으로 부모 ID를 만들지 않는다.
- subagent_history_start_ordinal이 있으면 그 전의 부모 복사본은 자식 실행으로 세지 않는다.
- 상속 구간 제외는 자식뿐 아니라 일반 main fork에도 적용한다. forked_from_id가 있으면 자신의 구간을 증명할 수 있어야 한다. 명시적인 own-history ordinal 또는 history_base가 나타내는 물리적인 자체 suffix를 사용하고, 어떤 경계도 확인하지 못하면 파일 전체를 coverage의 보류로 기록한다. 부모의 논리 ordinal을 자식 파일의 물리 ordinal로 추측 변환하지 않는다.
- history_base는 공유 prefix 참조다. 이 단계에서는 외부 prefix를 따라 읽지 않고, 물리적으로 기록된 자신의 구간만 센다. 상속된 문맥은 실행이 아니며, 세션 수명 전체를 복원했다고 말하지 않는다. 경계를 확인할 수 없는 root/main fork도 같은 보류 규칙을 따른다.
- session_meta의 정체성·역할을 읽지 못한 파일은 main으로 추측하지 않고 보류한다.

기록 원본은 읽기만 한다. 허용한 root의 regular JSONL 파일만 읽으며 symlink·깨진 JSON·비객체 레코드·알 수 없는 형식을 처리한 수를 coverage에 기록한다. 파일/호출의 시각은 timezone이 있는 ISO timestamp로 판정하고 호스트 현지 일자에 나눈다.

## 집계 정의

집계 단위는 **기록된 모델 도구 호출과 저장된 출력**이다. 코드 셀의 텍스트를 실행하거나 검색해서 내부 호출 수를 추정하지 않는다.

- response_item의 function_call/custom_tool_call을 호출 원천으로 쓴다.
- event_msg.item_completed, 토큰 사용량, API 요청 복사본은 별도 호출로 더하지 않는다.
- 메인 스레드의 spawn_agent와 followup_task를 위임 호출로 분리해 세고 합계를 제공한다. send_message/wait_agent/list_agents는 새 위임으로 세지 않는다.
- 명시적으로 지원하는 shell/read/search/MCP 정보 조회 도구만 direct로 분류한다. namespace를 정규화하되 다른 namespace의 동명 도구를 위임으로 오분류하지 않는다.
- functions.exec처럼 내부에 여러 도구를 담을 수 있는 wrapper는 opaque_calls로 따로 표시한다. main/sub의 전체 observed_tool_calls도 함께 제공한다. 대표 위임 비중은 delegate / main observed_tool_calls로 계산한다. opaque·통신·기타 호출도 분모에 포함하므로, 위임 1회와 opaque 99회는 1%다. direct+delegate만의 식별 비율은 출력하지 않는다. 이 비중을 기존 Claude delegation%와 같은 정의라고 부르지 않는다.
- 자식 도구 호출은 main의 direct/delegate 분모에 넣지 않는다. 자식의 호출 수·출력 바이트를 별도 기록한다.
- function_call_output/custom_tool_call_output의 문자열과 content 배열을 처리한다. output JSON 값의 UTF-8 직렬화 길이(ensure_ascii=False)를 저장된 출력 크기로 정의한다. 원래 실행 출력의 전체 크기나 실제 토큰 수라고 부르지 않는다.
- 출력에 call_id가 없거나 호출과 연결되지 않으면 추측 연결하지 않고 coverage에 보류한다. 위임 호출의 반환 출력은 main에서 받은 출력이므로 main에 속한다.
- 동일한 thread·call_id·종류의 호출 재기록은 한 번만 센다. 충돌하는 중복 호출은 진단한다. 출력은 response item ID로 중복을 제거하며, ID가 없는 호환 기록은 call_id·종류·payload fingerprint로 중복을 진단한다. 한 call의 서로 다른 유효 출력 조각은 각각 저장된 출력으로 센다.
- 누락/미지원 자료를 정상적인 빈 데이터와 구분한다. root가 없거나 읽을 수 없으면 CLI는 nonzero로 끝낸다. 빈 root는 명시적인 0 결과다.

coverage에는 적어도 발견/집계/보류 파일, main/sub thread 수, malformed/unknown metadata, inherited 구간 제외, 중복/충돌 호출, 고아 출력, opaque 호출, 미지원 레코드 수를 담는다. 보류 항목이 있는 결과를 완전한 전체 계측으로 표시하지 않는다.

## Claude 안에서 Codex에 보낸 Bash 호출

기존 Bash 분류와 실험 분모는 바꾸지 않는다. 새 Codex 지표에서 Claude Bash를 Agent로 재분류하지도 않는다.

이번 구현의 제안은 별도 Bash 위임 태그를 추가하지 않는 것이다. “코드 문자열에 codex가 있다”는 매칭으로 실제 위임·문서 인용·명령 실패를 구분할 수 없고, 현재 두 로그 형식 사이에 검증된 공통 실행 ID가 없다. Codex 세션 자체의 관측 지표를 제공하고, Claude Bash는 기존 분류로 남긴다. 이 선택과 이유를 README에 명시해 #47의 태깅 여부 결정 항목을 충족한다.

## 검증과 전달

새 테스트는 전부 synthetic tmp_path 기록을 사용하고 실제 세션 본문을 커밋하지 않는다.

필수 사례:
- 기본 Claude 호출과 --source claude의 기존 text/JSON 호환성
- Codex 메인/자식 분리, namespace가 다른 동명 도구, spawn/followup/통신 구분
- 첫 메타데이터 보존과 부모 복사 prefix 제외, 일반 main fork·자식 fork 양쪽의 확인 불가 경계 보류
- 위임 1회·opaque 99회가 1%인 대표 비율
- event 복사본·중복 파일/호출·중복 출력과 유효한 여러 출력 조각
- function/custom 호출, 문자열/배열/한글 출력, call_id 없는/고아 출력
- 일자 경계와 --since 필터, 자식의 독립 파일 시각
- malformed 입력·unknown source·빈/없는 root·잘못된 CLI 인자
- 임시 HOME의 배포 링크와 실행 비트

먼저 실패하는 테스트를 확인한 뒤 구현한다. 기존 Claude 19개 테스트와 새 테스트, 설치 관련 테스트를 통과시키고 Python 구문·git diff --check를 확인한다. 이후 범위에 맞는 독립 코드 검토와 필요한 저장소 검사로 마무리한다.

예상 파일은 새 Codex 집계기·테스트, 기존 CLI의 작은 라우팅 변경, README 사용 예시, installer allowlist와 관련 테스트다. 사용자에게 검토 가능한 diff와 실제 검증 결과를 전달한다. 사용자 HOME 설치, hook 변경, 새 실험 시작, commit/PR/merge는 이번 구현 완료의 자동 후속 작업으로 간주하지 않는다.

## 원천

- https://github.com/jhw7500/claude-config/issues/47
- 고정 공식 소스: openai/codex @ 6ff1e1ca3b30fc8b914720681fcf5fe62e95f856 (rust-v0.166.0)
- 첫 메타데이터: https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/rollout/src/recorder.rs#L1162-L1185
- 메타데이터·fork 경계: https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/protocol/src/protocol.rs#L3337-L3431
- 도구 항목·출력: https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/protocol/src/models.rs#L1124-L1227
- 저장 정책·잘림: https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/rollout/src/policy.rs#L230-L283
