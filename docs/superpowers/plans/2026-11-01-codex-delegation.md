# Codex Descriptive Delegation Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** #47의 Codex rollout JSONL을 독립적으로 집계하고 기존 Claude CLI·실험을 보존한다.
**Architecture:** 새 standalone Codex 집계기, 기존 CLI의 opt-in --source 라우팅, 기존 installer allowlist·README에 통합한다. first metadata와 own-history 경계를 기준으로 부모 복사본을 제외하고 response_item만 집계한다.
**Tech Stack:** Python 3.10 표준 라이브러리, pytest, 기존 Bash installer.
**Spec:** docs/superpowers/specs/2026-11-01-codex-delegation-design.md (사용자 2026-11-01 “승인 구현”).

## Global Constraints

- Python 3.10 표준 라이브러리만 사용한다.
- 기존 Claude collect/is_direct 및 기본 text/JSON 출력은 유지한다.
- Codex 위임 비중은 delegate / ALL observed main tool calls다. Claude 판정 기준과 합치지 않는다.
- first session_meta canonical; 모든 fork는 own-history 경계를 검증하고, 불명확하면 보류한다.
- history_base 외부 prefix는 따라 읽지 않는다. 원본은 읽기 전용이다.
- 대표 비율은 spawn/followup 요청 비중이다. actual successful work나 token 절감량이라고 부르지 않는다.
- 사용자 HOME installer·Task mutation·commit·PR·merge는 실행하지 않는다.
- 실행: 이 세션의 native 구현. 커밋 단계는 사용자의 별도 요청 전까지 생략한다.

## Review Focus

1. 자식 파일 뒤의 부모 session_meta가 역할을 main으로 바꾸지 않아야 한다 — Task1.
2. fork prefix가 다른 thread ID를 갖더라도 중복 실행으로 세지 않아야 한다 — Task1.
3. 배치 wrapper 99회·위임 1회가 1%여야 한다 — Task2.
4. 출력 배열·동일 call의 여러 output item·중복 output item을 구분해야 한다 — Task2.
5. 설치 symlink 진입점이 실제 소스 옆의 새 CLI를 찾고, 기본 Claude 출력은 기존과 같아야 한다 — Task3.

### Task 1: Canonical Codex records and ownership

**Files:** Create scripts/codex-delegation-ratio.py; tests/test_codex_delegation_ratio.py.
**Interfaces:**
- collect(root: str, since_dt: datetime | None) -> dict: provider, descriptive_only, overall, days, coverage.
- main(argv: list[str] | None = None) -> int: --root/--days/--since/--overall/--json.
- Integration executes the standalone script with parsed forwarded CLI options; no import-time dependencies.

- [x] Write synthetic tests for main/sub, first metadata, ordinal prefix, all fork hold, history_base physical suffix, unknown metadata, non-object/malformed records and symlinks.
- [x] Run tests and observe missing-script/unsupported behavior failures.
- [x] Implement streaming regular-file readers, first-meta inventory, own-record selection and typed record validation.
- [x] Verify thread identity and missing boundaries through the same public CLI fixtures.

### Task 2: Descriptive counts and byte accounting

**Files:** Same Codex implementation and test file.
**Interfaces:** Codex overall/day values include main_tool_calls, sub_tool_calls, direct, delegate, delegate_spawn, delegate_followup, opaque_calls, other_calls, main_bytes, sub_bytes, delegation_pct, offload_pct, sessions/subagent_sessions. coverage exposes scanned/held/malformed/duplicate/conflicting/orphan/unknown/inherited counts and completeness.

- [x] Add failing tests for namespaced delegation, communication exclusions, direct/read/search/MCP classification and opaque denominator.
- [x] Add failing tests for function/custom calls and outputs, UTF-8 string/array outputs, missing ID, orphan output, conflicting duplicates, repeated output item, valid multiple output fragments.
- [x] Add failing tests for call-time date filtering, --since vs --overall precedence, malformed/naive timestamps, empty/missing root and invalid --days.
- [x] Implement per-thread call and output identity; report uncertainty and hold conflicting data rather than guessing.
- [x] Implement per-day/overall reporting. Output bytes use JSON UTF-8 serialized output values, ensure_ascii=False.
- [x] Verify all Codex tests and original 19 Claude tests.

### Task 3: Existing CLI, deployment and user documentation

**Files:** Modify scripts/delegation-ratio.py, install.sh, README.md, tests/test_delegation_ratio.py, tests/test_installer_private_config.py (or one focused integration test module).
**Interfaces:** delegation-ratio.py --source codex forwards only parsed --root/--days/--since/--overall/--json options to resolved sibling codex-delegation-ratio.py. Default remains Claude; explicit --source claude is identical.

- [x] Add failing tests comparing old default and explicit Claude output, Codex route/default root/explicit root, symlink resolution and nonzero propagation.
- [x] Add failing temporary-HOME installer test for new script link and idempotency; real user HOME is never installer target.
- [x] Add source choice and minimal routing; leave Claude collector/classifier unchanged.
- [x] Add the new executable to installer allowlist. Preserve existing executable bits; new script mode100755.
- [x] Update README with both usage commands, descriptive metric definitions/coverage, recorded-output limitation, no merged experiment, and explicit companion-tag decision (no Bash reclassification/tag).
- [x] Run CLI/installer focused tests.

### Task 4: Whole-change validation and review

- [x] Run tests/test_codex_delegation_ratio.py + tests/test_delegation_ratio.py + installer-related tests.
- [x] Validate Python syntax without changing runtime state; git diff --check and changed-file modes; ShellCheck installer if available.
- [x] Perform bounded read-only smoke on real Codex files only after synthetic tests pass; capture only aggregate counters/coverage, never prompts or raw output content.
- [x] Request one independent whole-branch review focused on ownership, duplicates, compatibility and misleading completeness. Fix concrete findings and rerun affected checks.
- [x] Run appropriate broader repository tests once after local checks pass; do not repeat unchanged successful suites.
- [x] Mark plan tasks done only on actual evidence; report changed files, checks and remaining limitations. Leave reviewable local changes without commit/PR/install.
