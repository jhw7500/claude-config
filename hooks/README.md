# `~/.claude/hooks/` — Claude Code 훅 스크립트

이 디렉토리는 **전체 Claude Code 세션에 적용**되는 사용자 훅을 담는다.
훅은 `~/.claude/settings.json` 의 `hooks` 섹션에 등록되어 있다.

## 파일 개요

| 파일 | 트리거 | 역할 |
|------|--------|------|
| `carl-hook.py` | UserPromptSubmit | CARL 컨텍스트 브래킷, 도메인 규칙, 결정 기록을 주입 (외부 도구 유지) |
| `notion-continuous-exec-hook.py` | UserPromptSubmit | Notion 저장/불러오기 스킬 승인 응답 감지 시 "연속 실행" reminder 주입 |
| `general-continuation-hook.py` | UserPromptSubmit | "한번에/끝까지/연속으로" 등 사용자의 연속 실행 의도 감지 시 reminder 주입 |
| `bg-task-progress-hook.py` | PreToolUse + PostToolUse | Agent / Bash `run_in_background=true` 시작·완료 알림 강제 + statusLine 카운터 관리 |

## 동작 원칙

1. **조용히 실패**: 모든 훅은 예외 발생 시 `return 0`으로 조용히 종료. Claude Code 본 동작을 절대 차단하지 않는다.
2. **선택적 주입**: 트리거 조건이 맞지 않으면 stdout에 아무것도 쓰지 않는다 (훅이 없는 것과 동일).
3. **Escape hatch**: 사용자 메시지 시작에 `#noreminder`, `#nr`, `#raw`, `#silent`, `#조용히` 중 하나가 있으면 UserPromptSubmit 훅들이 주입을 스킵한다.
4. **전역 규칙 참조**: 훅 reminder는 행동 트리거만 담고, 세부 규칙은 `~/.claude/CLAUDE.md`를 참조하도록 설계.

## 디버그 로깅

훅이 실제로 언제 어떤 payload를 받는지 관찰하려면 두 방법 중 하나로 활성화:

```bash
# 방법 1: 환경변수
export CLAUDE_HOOK_DEBUG=1

# 방법 2: flag 파일 (현재 쉘 세션 제약 없음)
touch ~/.claude/logs/hook-debug.on
```

로그: `~/.claude/logs/hook-debug.log` (JSON Lines 형식)

예시 항목:
```json
{"ts": "2026-04-17T15:32:30", "tag": "notion", "payload_keys": ["session_id", "user_prompt"], "prompt_len": 2}
{"ts": "2026-04-17T15:32:30", "tag": "bg-task", "payload_keys": ["session_id", "tool_input", "tool_name"], "tool_name": "Bash"}
```

끌 때:
```bash
rm ~/.claude/logs/hook-debug.on   # 또는 unset CLAUDE_HOOK_DEBUG
```

로깅은 기본 OFF. 활성화 시 overhead는 무시 가능 (append 한 줄).

## 백그라운드 작업 카운터

`bg-task-progress-hook.py`는 statusLine이 `🔄 BG:N` 표시를 할 수 있도록 세션별 파일을 관리한다.

| 경로 | 내용 |
|------|------|
| `~/.claude/state/bg-tasks/` | 루트 디렉토리 (권한 `0700`) |
| `~/.claude/state/bg-tasks/<session_id>/` | 세션별 서브디렉토리 |
| `~/.claude/state/bg-tasks/<session_id>/<tool_use_id>.task` | tool_use_id가 있을 때 Pre/Post 정확 매칭용 |
| `~/.claude/state/bg-tasks/<session_id>/<uuid>.task` | tool_use_id 없을 때 fallback |

- Pre (시작) 시: 파일 생성 → 카운터 증가
- Post (완료) 시: 같은 tool_use_id 파일 삭제 → 카운터 감소
- 자동 정리: 24시간 초과 파일은 statusLine 갱신 시 삭제
- 4시간 초과 파일은 카운트에서 제외 (orphan 방지)

## Escape Prefix 정리

사용자가 "이 메시지 한 번만" 훅 주입 없이 처리하고 싶을 때:

```
#noreminder 지금 중간 확인 받아줘
#nr OK
#raw 전체 저장
#silent 1,2
#조용히 예
```

영향 범위: UserPromptSubmit 훅 2개(notion, general). `carl-hook`은 별도 도구라 영향 안 받음.
`bg-task-progress-hook`은 사용자 prompt와 무관하게 도구 호출 파라미터를 검사하므로 escape 대상 아님.

## 추가/수정 시 체크리스트

- [ ] 훅 스크립트 최상위는 `try/except Exception`으로 감싸고 `return 0` 보장
- [ ] payload 키 이름은 snake_case와 camelCase 둘 다 fallback 처리
- [ ] Escape prefix 검사 먼저 수행 (UserPromptSubmit 훅의 경우)
- [ ] stdout 출력은 `<system-reminder>...</system-reminder>` 블록 형식
- [ ] 새 훅 추가 시 이 README의 표에 엔트리 추가
- [ ] `settings.json` 등록 시 필요하면 `matcher` 필드로 대상 도구 제한

## 실패 시 영향도 정리

| 훅 | 실패 원인 | Claude Code에 미치는 영향 |
|----|----------|---------------------------|
| carl-hook.py | CARL 파싱 오류 | CARL 규칙 주입 안 됨, 외 동작 정상 |
| notion-continuous | JSON 파싱 실패 | reminder 주입 안 됨 (조용히 스킵) |
| general-continuation | 정규식 오류 | 동일 — 조용히 스킵 |
| bg-task-progress | payload 키 불일치 | 카운터 증감/알림 누락, 도구 실행 자체는 정상 |

**모든 훅은 exit 0을 반환**하도록 설계되어 Claude Code의 UserPromptSubmit / PreToolUse / PostToolUse 흐름을 중단시키지 않는다.
