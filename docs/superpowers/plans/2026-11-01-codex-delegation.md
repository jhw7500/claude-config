# Codex Descriptive Delegation Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** #47의 Codex rollout JSONL을 독립적으로 집계하고 기존 Claude CLI·실험을 보존한다.
**Architecture:** 새 standalone Codex 집계기, 기존 CLI의 opt-in --source 라우팅, 기존 installer allowlist·README에 통합한다. first metadata와 own-history 경계를 기준으로 부모 복사본을 제외하고 response_item만 집계한다.
**Tech Stack:** Python 3.10 표준 라이브러리, pytest, 기존 Bash installer.
**Spec:** docs/superpowers/specs/2026-11-01-codex-delegation-design.md (사용자 2026-11-01 “승인 구현”).

초기 구현은 PR #263으로 게시했다. 이후 사용자는 비차단 지적 4건을 한 번에 보완하고 같은 PR에서 재리뷰하는 권장안에 “진행”으로 답했다. 아래 제약은 초기 로컬 구현 단계의 범위이며, 후속 수정·검증 결과는 문서 끝에 기록한다.

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

## PR #263 후속 보완과 판단

- A-R1-001: 복사 main fork에 공식 `forked_from_ordinal_exclusive`를 적용한다. 자식은 자신의 `subagent_history_start_ordinal`을 사용하고, `history_base` 파일은 로컬 suffix를 집계한다. 부모 cutoff·자식 투영 경계·물리 base에 동등성이나 최댓값 규칙을 강제하지 않는다. 잘못된 cutoff와 부모 없는 cutoff는 보류한다.
- A-R1-002: 명시적으로 잘못된 root ID와 상충하는 외부·중첩 parent를 보류한다. 다만 `session_id`의 존재와 외부 parent의 존재를 강제하라는 원 제안은 그대로 적용하지 않는다. 공식 구버전 역직렬화는 누락된 `session_id`를 `id`로 채우고, 외부 parent가 없거나 null이면 중첩 spawn parent를 허용한다. 이 호환성을 테스트로 보존한다. parent 충돌 보류는 이 계측기의 보수적 정책이며 상류 JSON 역직렬화 규칙이라고 주장하지 않는다.
- A-R1-003: `coverage.main_threads`/`sub_threads`에 유효한 고유 thread 수를 기록한다. 빈 thread와 기간 밖 thread도 포함하고, 보류된 정체성·역할 충돌 ID·복사본 중복은 제외한다. `overall`/`days`의 session 수는 기간 내 호출·출력에 참여한 thread 수로 유지한다.
- A-R1-004: 아래에 전체 테스트 및 실제 기록 조회의 명령·환경·관측 결과를 기록한다. 초기 심사의 원본 보고서와 캡처는 보존했다.

공식 근거는 openai/codex `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`의 [root ID 호환](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/protocol/src/protocol.rs#L3483-L3508), [parent fallback](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/thread_manager.rs#L2425-L2432), [자식 투영 경계](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/thread-store/src/local/thread_history_materialization.rs#L197-L235), [서로 다른 fork cutoff/base 사례](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/rollout/src/metadata_tests.rs#L30-L107)다.

## 실제 검증 결과 — 2026-11-01

수정 전 메타데이터·inventory 회귀에서 13개 실패와 호환성 2개 통과를 확인했다. fork 회귀는 8개 실패와 양성 사례 6개 통과를 확인한 뒤 수정했다. `cutoff=99`, 자식 경계 `3` 사례는 두 필드를 혼동하지 않는 방어 테스트이며 실제 기록에서 관측한 조합으로 주장하지 않는다.

변경 범위 검증:

```bash
python3 -m pytest tests/test_codex_delegation_ratio.py tests/test_codex_delegation_entrypoint.py tests/test_delegation_ratio.py tests/test_installer_private_config.py -q --tb=short
```

141 passed in 15.51s, exit 0. 모든 새 입력은 합성 임시 기록이며 installer는 임시 HOME에서만 실행했다. 독립적인 읽기 전용 코드 검토에서 새 정확성 지적은 없었다. 변경된 Python 두 파일은 Python 3.10 구문으로 파싱됐고 `git diff --check`가 통과했다.

전체 저장소 테스트는 기존 격리 환경을 사용했다. 그 환경은 초기 검증 때 아래 명령으로 만들었고, 테스트용 의존성은 저장소의 고정 해시를 검증해 설치했다.

```bash
uv venv --system-site-packages --python /usr/bin/python3 .superpowers/sdd/2026-11-01-codex-delegation/test-venv
uv pip install --python .superpowers/sdd/2026-11-01-codex-delegation/test-venv/bin/python --require-hashes --requirement requirements-test.lock
```

후속 수정 뒤 실제 실행 argv는 다음과 같다. 프로세스 환경에서 `LD_LIBRARY_PATH`를 제거하고 `PYTEST_XDIST_AUTO_NUM_WORKERS=4`를 지정했다.

```bash
.superpowers/sdd/2026-11-01-codex-delegation/test-venv/bin/python -m pytest -q --tb=short -n auto
```

5,132 passed in 408.87s, exit 0. UTC 10:06:51–10:13:40에 한 번 실행했고, 실행 전후 collector·회귀 테스트·README·설계 문서 해시가 같았다. 이 결과를 기록한 본 문서만 이후 보완했다. 초기 구현의 전체 테스트는 5,103개 통과였으며 이번 결과는 후속 수정에 대한 새 실행이다.

실제 Codex 기록을 읽기만 한 aggregate smoke:

```bash
python3 scripts/delegation-ratio.py --source codex --days 14 --json
```

exit 0, 11.61s. 관측 시점의 1,313개 파일을 읽었고 보류 파일은 0개였다. inventory는 main 494개·sub 818개, 선택 기간의 활동 session은 main 29개·sub 391개였다. 상속 레코드 14,299개를 제외했다. 미지원 레코드 106개·call_id 없는 출력 2개·약한 ID의 호환 출력 1,359개로 `coverage.complete=false`이며 완전한 세션 수명·원본 출력량·토큰 절감량의 증거가 아니다. 이 수치는 시점별 로컬 표본 관측이며 재현 가능한 합성 회귀 테스트와 구분한다. 원본 프롬프트나 도구 출력 내용은 기록하지 않았다.
