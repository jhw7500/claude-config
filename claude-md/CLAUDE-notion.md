# Notion 작업 전용 지침

> 적용 범위: 모든 `/jhw:*` 스킬 (record / review / save / note / import / recall / start / close / delete / status / project / cclog / history / context / search 등) + 직접 호출하는 `mcp__notion__*` / `mcp__jhw-notion__*` 도구.
> 전역 원칙(action 결과 보고, 정보 수집 → 조기 종료 방지, 외부 비동기 알림 등)은 `~/.claude/CLAUDE.md` 참조. 본 파일은 Notion 도메인 특수성만 담는다.

---

## 저장 흐름 규칙

`/jhw:record`, `/jhw:review`, `/jhw:save`, `/jhw:note`, `/jhw:start`, `/jhw:close`, `/jhw:delete` 등을 실행할 때, **사용자 승인 이후에는 중간에 멈추지 않고 한 흐름으로 저장을 완료**한다.

### Why
- 사용자는 리뷰 단계에서 이미 저장 대상을 승인하며("OK", "전체 저장", "N번만 저장"), 이후 중간 확인은 불필요한 대화 왕복을 유발하여 매우 반복적으로 불편을 주었음.
- 동일한 불편이 여러 세션에서 반복 발생하였고, 프로젝트 메모리에만 기록했을 때는 다른 프로젝트나 메모리 소실 시 재발하였음.

### How to apply
- 승인 직후 아래 단계를 **끊지 말고 연속 실행**한다:
  1. 필요 시 `mcp__notion__notion-search`로 대상 DB/페이지 ID 조회
  2. 필요 시 `mcp__notion__notion-fetch`로 스키마 조회
  3. 필요 시 Relation 대상 페이지 URL 조회
  4. `mcp__notion__notion-create-pages` / `mcp__notion__notion-update-page` / `mcp__jhw-notion__jhw_record` / `jhw_note` 등 실제 저장
  5. 결과 URL을 **사용자에게 텍스트로 보고**
- 중간 단계에서 미리보기를 재표시하거나 "진행할까요?"를 재차 묻지 않는다. 승인은 리뷰 시점 한 번이면 충분하다.
- 독립적인 조회(검색, fetch, 병행 정보 탐색)는 **반드시 병렬**로 호출한다.
- `/jhw:record`, `/jhw:save` 등은 미리보기 + 승인을 한 번 받고, 이후는 동일하게 한 흐름으로 처리한다.

---

## 중간 실패 회복 패턴

**트리거**: 정보 수집 중 Bash/Tool cancel·error 발생.

**올바른 동작**:
- cancel/error 발생 → 같은 응답 안에서 retry
- 정보 수집 완료 → **추가 입력 대기 없이 같은 응답 안에서 create-pages/update-page 호출까지**
- 결과 URL 보고 후 응답 종료

---

## DB batch 호출 규칙 (parent별 분리)

**핵심**: `notion-create-pages`는 한 번의 호출에 **단일 parent DB 항목들**만 생성 가능. 여러 DB에 동시에 저장해야 하면 **DB 개수만큼** 호출 분할.

**사전 계획표 의무**: 응답 시작 시 다음 형식으로 명시한다:
```
[항목 수 / DB 종류 / create-pages 호출 횟수]
예) 4 items / [KB×3, DecisionLog×1] / 2 calls
```

**자가 점검 (응답 종료 직전)**:
1. **승인된 모든 항목**에 대해 `create-pages` / `update-page` 호출이 포함됐는가?
2. parent별 호출 횟수가 사전 계획과 일치하는가?
3. 도구 호출 결과(JSON) 위에 사용자용 **표 형식 보고**가 있는가? (`# / DB / 제목 / URL`)
4. 위 항목 중 하나라도 빠지면 응답 종료 금지 — 같은 응답 안에서 마저 처리.

---

## 스키마 작성 주의

- URL 필드 키: `userDefined:url` (단순 `url`이 아님)
- Relation 필드: JSON 배열 문자열 — `"[\"https://www.notion.so/...\"]"` 형태
- Multi-select: JSON 배열 문자열 — `"[\"옵션명\"]"` 형태
- Select: 옵션명 문자열 그대로
- Date: expanded key 사용 — `date:{name}:start`, `date:{name}:end` (단순 `date` 키는 에러 발생)
  - `date:{name}:is_datetime`는 **숫자 `0`/`1` 또는 생략**만 허용 (체크박스 `__YES__/__NO__` 아님). date-only면 **생략 권장**.
- Checkbox: `__YES__` / `__NO__`
- Number: JavaScript number (문자열이 아님)
- **System reserved property names** — Notion API가 `properties` JSON에서 거부:
  - `id`, `url` → `userDefined:id`, `userDefined:url` prefix로 우회 가능
  - `content` → **prefix로도 우회 불가** (`userDefined:content` 시도 시 "not found" 에러). create-pages 시 page-level `content` 필드(=본문 markdown)로 분리하고 properties의 `content` 컬럼은 비워둔다. update-page에서도 동일 — `content` property는 raw도 prefix도 모두 거부됨. (확인: 2026-04-27 AI Preferences DB)
  - 새 reserved name 발견 시 이 목록에 추가

---

## 턴 오버헤드 최소화

- 승인 이후 저장은 `/jhw:note` 등 **스킬을 경유하지 않고** `mcp__notion__notion-create-pages` 또는 `mcp__jhw-notion__jhw_record`를 직접 호출해도 된다. 스키마는 `memory/reference_notion_schema.md`에 캐시되어 있음.
- `/jhw:record`, `/jhw:save`처럼 **미리보기+승인이 필수인 경우에만** 스킬을 호출한다. 이미 사용자 승인이 끝났다면 스킬 로드로 턴을 낭비하지 않는다.

---

## Notion 작업 위반 사례 누적

- **2026-04-27**: 사용자 승인 → 정보 수집 시작 → Bash 실패 cancel → 같은 응답에서 retry 후 정보 수집 완료 → **여기서 응답 종료** ← 위반. 사용자가 "진행상황" 입력해야 비로소 create-pages 호출됨. **방지**: 정보 수집 완료 후 같은 응답에서 create-pages까지.
- **2026-04-28**: notion-fetch 직후 create-pages 누락하고 응답 종료 → 사용자가 `/jhw:status` 두 번 입력해 트리거. **방지**: fetch 결과 → 같은 응답 저장.
- **2026-05-07 (저녁) KB+DecisionLog 분할 누락**: `jhw:review` 1-4번 저장 승인 후 KB 3개만 한 번에 저장하고 Decision Log 1개 누락 → 사용자 "왜 완료가 안되었지?". 원인: KB와 Decision Log가 서로 다른 parent라 create-pages를 2번 호출해야 한다는 점을 사전 계획에서 누락. **방지**: 응답 시작 시 `[항목 수 / DB 종류 / 호출 횟수]` 사전 계획표.
- **2026-05-07 (저녁) Decision Log 결과 보고 누락**: 저장은 완료했지만 결과 요약 텍스트 보고 누락 → 사용자 "또 안하네". 원인: 도구 결과 JSON이 응답에 포함되면 보고 의무 충족됐다고 잘못 판단. **방지**: action 도구 호출 후 반드시 표 형식(`# / DB / 제목 / URL`) 텍스트로 응답 마무리.
- **2026-05-07 (저녁) jhw_record URL 보고 누락**: `mcp__jhw-notion__jhw_record` 호출 후 URL 받았으나 텍스트 보고 없이 응답 종료 → 사용자 "왜 jhw-notion만 호출하면 결과보고도안하고 멈추나" 개입. 원인: MCP tool result(URL JSON)는 컨텍스트에만 들어오고 사용자 화면엔 자동 표시 안 됨. **방지**: 모든 jhw_* 호출 직후 URL 1줄 보고 의무.
