#!/usr/bin/env bash
# PreToolUse(Edit|Write|NotebookEdit): 세션의 첫 "프로젝트 파일" 수정 직전에
# Task 등록 권유 리마인더를 세션당 1회 주입한다.
# 트리거는 결정적(하네스 책임), 권유할지 말지의 판단은 Claude가
# global-guidance "Task 등록 권유" 규칙으로 한다.
set -u

IN=$(cat)
SID=$(printf '%s' "$IN" | jq -r '.session_id // empty' 2>/dev/null)
FP=$(printf '%s' "$IN" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' 2>/dev/null)
[ -z "$SID" ] && exit 0

# 스크래치·설정·메모리·세션상태 수정은 "작업 시작"으로 치지 않는다.
# (state 파일을 남기지 않으므로 이후 첫 실제 프로젝트 수정에서 정상 발화)
case "$FP" in
  ""|/tmp/*|"$HOME"/.claude/*|*/.omc/*|*/memory/*.md|*HANDOFF*.md) exit 0 ;;
esac

STATE_DIR="${TMPDIR:-/tmp}/claude-task-nudge-$(id -u)"
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
STATE="$STATE_DIR/$SID"
[ -e "$STATE" ] && exit 0
: > "$STATE"

cat <<'MSG'
[TASK-NUDGE] 이 세션의 첫 프로젝트 파일 수정입니다. 이 세션에서 아직 Project Control Task를 시작하지 않았다면, global-guidance "Task 등록 권유" 규칙에 따라 작업 착수 전에 사용자에게 Task/Issue 등록 여부를 1회 물어보세요. (이미 task start를 했거나, 서브에이전트이거나, 포트폴리오 등록 저장소 밖이거나, 조회·문서·설정만 만지는 세션이면 이 리마인더는 무시.)
MSG
exit 0
