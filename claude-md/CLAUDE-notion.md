# Notion 작업 전용 지침

> 적용 범위: 모든 `/jhw:*` 스킬 (record / review / save / note / import / recall / start / close / delete / status / project / cclog / history / context / search 등) + 직접 호출하는 `mcp__notion__*` / `mcp__jhw-notion__*` 도구.
> 전역 원칙(action 결과 보고, 정보 수집 → 조기 종료 방지, 외부 비동기 알림 등)은 `~/.claude/CLAUDE.md` 참조. 본 파일은 Notion 도메인 특수성만 담는다.

---

## 온디맨드 노션 조회 (참고용)

**트리거**:
1. (보장) 프롬프트에 마커 `노션참고`/`노션 참고`/`@notion` 존재 시 — 훅이 `[NOTION-RECALL]` 리마인더로 강제.
2. (보조·모델판단) 사용자가 명확히 **조회/참고 의도**를 보일 때 — "노션에서 찾아/참고/조회", "예전에 이거 어떻게 했더라(프로젝트 지식)", "관련 결정 있었나".

**의도 3-way 구분 (오발 방지, 필수)**:
- **조회(retrieve)** → 본 규칙 발동.
- **저장(save)** "노션에 저장/기록해줘" → 저장 흐름 규칙(아래). 본 규칙 발동 안 함.
- **코드수정** "노션 MCP 코드 고쳐줘" → 이 저장소 코드 작업. 본 규칙 발동 안 함.

**동작**:
1. 프롬프트에서 **주제 키워드**(작업 대상·기술·증상 등)를 뽑는다.
2. `mcp__jhw-notion__jhw_retrieve`를 호출한다(`topic`=주제, 프로젝트가 식별되면 `project` 지정). 도구가 없으면 `mcp__notion__notion-search` → 관련 후보를 `mcp__notion__notion-fetch`로 본문 조회.
3. 반환 내용을 근거로 작업을 수행한다.
4. **무엇을 근거로 삼았는지 1줄 보고**(제목/URL 포함).
5. 조회로도 불충분하면 그때 사용자에게 질문(무엇을 찾았고 무엇이 여전히 불명확한지 명시).
- 독립적인 조회는 **병렬** 호출. 적용 안 함: 코드/파일에서 즉시 확인되는 것, 일반 지식, 이번 세션에서 이미 확정된 것.

---

## 저장 흐름 규칙

`/jhw:record`, `/jhw:review`, `/jhw:save`, `/jhw:note`, `/jhw:start`, `/jhw:close`, `/jhw:delete` 등을 실행할 때, **사용자 승인 이후에는 중간에 멈추지 않고 한 흐름으로 저장을 완료**한다.

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
- 정보 수집 중 Bash/Tool cancel·error 발생 시: 같은 응답 안에서 retry → 수집 완료 → 추가 입력 대기 없이 create-pages/update-page 호출 → 결과 URL 보고 후 종료.

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

> 사례 서사는 `~/.claude/archive/violations.md`로 이관 (2026-07-07). 방지 규칙 요약: ① 수집 완료 후 같은 응답에서 저장까지 ② parent별 create-pages 분할 + 사전 계획표 ③ 모든 저장/jhw_* 호출 직후 표·URL 텍스트 보고 의무.
