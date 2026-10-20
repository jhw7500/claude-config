# Notion-specific guidance — Notion 작업 전용 지침

> Scope: all `/jhw:*` skills (record / review / save / note / import / recall / start / close / delete / status / project / cclog / history / context / search, etc.) + directly called `mcp__notion__*` / `mcp__jhw-notion__*` tools.
> Global principles (reporting action results, preventing early exit after information gathering, external async notification, etc.) are in `~/.claude/CLAUDE.md`. This file holds only Notion-domain specifics. It is written in English to save context tokens; user-facing output stays in Korean.

---

## On-demand Notion lookup (reference) — 온디맨드 노션 조회 (참고용)

**Trigger**:
1. (Guaranteed) when the prompt contains the marker `노션참고`/`노션 참고`/`@notion` — a hook enforces it with the `[NOTION-RECALL]` reminder.
2. (Auxiliary, model judgment) when the user clearly shows **lookup/reference intent** — "노션에서 찾아/참고/조회", "예전에 이거 어떻게 했더라(프로젝트 지식)", "관련 결정 있었나".

**Three-way intent split (prevents misfires, mandatory)**:
- **Retrieve** → this rule fires.
- **Save** "노션에 저장/기록해줘" → the save-flow rules (below). This rule does not fire.
- **Code change** "노션 MCP 코드 고쳐줘" → code work in this repository. This rule does not fire.

**Action**:
1. Extract **topic keywords** (work target, technology, symptom, etc.) from the prompt.
2. Call `mcp__jhw-notion__jhw_retrieve` (`topic`=the topic; set `project` when a project is identified). If the tool is unavailable, use `mcp__notion__notion-search` → fetch the relevant candidates' bodies with `mcp__notion__notion-fetch`.
3. Do the work based on the returned content.
4. **Report in one line what you relied on** (including title/URL).
5. If the lookup is still insufficient, only then ask the user (state what was found and what is still unclear).
- Make independent lookups **in parallel**. Not applicable: things immediately checkable in code/files, general knowledge, things already settled in this session.

---

## Save-flow rules — 저장 흐름 규칙

When running `/jhw:record`, `/jhw:review`, `/jhw:save`, `/jhw:note`, `/jhw:start`, `/jhw:close`, `/jhw:delete`, etc., **after user approval, complete the save in one flow without stopping midway**.

### How to apply
- Right after approval, run the following steps **continuously, without breaking**:
  1. If needed, look up the target DB/page ID with `mcp__notion__notion-search`
  2. If needed, fetch the schema with `mcp__notion__notion-fetch`
  3. If needed, look up the Relation target page URLs
  4. Actually save with `mcp__notion__notion-create-pages` / `mcp__notion__notion-update-page` / `mcp__jhw-notion__jhw_record` / `jhw_note`, etc.
  5. **Report the result URL to the user as text**
- Do not re-show the preview or ask "shall I proceed?" again at intermediate steps. One approval at review time is enough.
- **Always** make independent lookups (search, fetch, parallel information gathering) **in parallel**.
- `/jhw:record`, `/jhw:save`, etc. get preview + approval once, and are afterwards handled the same way in one flow.
- If a Bash/tool cancel or error occurs during information gathering: in the same response, retry → finish gathering → call create-pages/update-page without waiting for further input → report the result URL, then end.

---

## DB batch call rule (split per parent) — DB batch 호출 규칙 (parent별 분리)

**Core**: one `notion-create-pages` call can create items under **a single parent DB** only. When saving to several DBs, split the calls **once per DB**.

**Mandatory up-front plan**: state it at the start of the response in this format:
```
[항목 수 / DB 종류 / create-pages 호출 횟수]
예) 4 items / [KB×3, DecisionLog×1] / 2 calls
```

**Self-check (right before ending the response)**:
1. Is there a `create-pages` / `update-page` call for **every approved item**?
2. Does the number of calls per parent match the up-front plan?
3. Is there a **table-style report** for the user above the tool-call results (JSON)? (`# / DB / 제목 / URL`)
4. If any of the above is missing, do not end the response — finish it in the same response.

---

## Schema-writing cautions — 스키마 작성 주의

- URL field key: `userDefined:url` (not plain `url`)
- Relation field: JSON array string — in the form `"[\"https://www.notion.so/...\"]"`
- Multi-select: JSON array string — in the form `"[\"옵션명\"]"`
- Select: the option name string as is
- Date: use the expanded keys — `date:{name}:start`, `date:{name}:end` (a plain `date` key errors)
  - `date:{name}:is_datetime` accepts **only the number `0`/`1` or omission** (not checkbox `__YES__/__NO__`). For date-only values, **omission is recommended**.
- Checkbox: `__YES__` / `__NO__`
- Number: a JavaScript number (not a string)
- **System reserved property names** — rejected by the Notion API in the `properties` JSON:
  - `id`, `url` → can be bypassed with the `userDefined:id`, `userDefined:url` prefix
  - `content` → **cannot be bypassed even with the prefix** (trying `userDefined:content` gives a "not found" error). In create-pages, move it into the page-level `content` field (= body markdown) and leave the `content` column in properties empty. The same holds for update-page — the `content` property is rejected both raw and prefixed. (Confirmed: 2026-04-27 AI Preferences DB)
  - Add newly discovered reserved names to this list

---

## Minimize turn overhead — 턴 오버헤드 최소화

- After approval, you may save by calling `mcp__notion__notion-create-pages` or `mcp__jhw-notion__jhw_record` directly, **without going through a skill** such as `/jhw:note`. The schema is cached in `memory/reference_notion_schema.md`.
- Invoke a skill **only when preview + approval is mandatory**, as with `/jhw:record` and `/jhw:save`. If the user has already approved, do not waste a turn loading a skill.

---

## Accumulated Notion violation cases — Notion 작업 위반 사례 누적

> The case narratives moved to `~/.claude/archive/violations.md` (2026-07-07). Prevention rules in short: ① once gathering is done, save in the same response ② split create-pages per parent + the up-front plan ③ a table/URL text report right after every save/jhw_* call.
