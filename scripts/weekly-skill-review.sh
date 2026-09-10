#!/bin/bash
# weekly-skill-review — task-observer 관찰 로그 정기 리뷰 (scheduled autonomous 모드)
#
# 관찰 원본은 읽기 전용으로 검토한다. .claude 아래 쓰기 제한을 우회하거나
# 다른 세션이 쓰는 로그를 이동하지 않는다. 심링크 별칭은 실체 경로로 중복 제거한다.
# 결과는 skill-updates/ 에 staged 로만 남는다 — 사용자가 설치하기 전까지 live 아님.
set -u
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

# 워크스페이스는 인자로 받는다. 이 저장소는 공개돼 있어 개인 경로를 파일에 두지 않는다
# (관찰 로그 자체도 gitignore 대상). cron 항목이 경로를 지정한다.
WS="${1:?usage: weekly-skill-review.sh <observation-workspace-dir>}"

# 나머지 경로는 이 스크립트 위치에서 계산한다(심링크로 설치되므로 readlink 필요).
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SKILLS="$REPO_DIR/skills"
LOG="$HOME/.claude/logs/weekly-skill-review.log"
mkdir -p "$(dirname "$LOG")"

[ -d "$WS" ] || { echo "$(date -Is) 워크스페이스 없음: $WS" >> "$LOG"; exit 1; }

# 기존 cron의 단일 워크스페이스 인자는 필수 anchor로 유지하되 전체 활성 소스를 찾는다.
INVENTORY="$(python3 "$SCRIPT_DIR/skill-review-workspaces.py" "$WS" \
    "$REPO_DIR/skill-observations" "$HOME/.claude/projects")" || exit 1
mapfile -t WORKSPACES <<< "$INVENTORY"
REVIEW_ROOT="$REPO_DIR/skill-observations/skill-updates"
OUTPUT="$REVIEW_ROOT/$(date -u +%Y-%m-%dT%H%M%SZ)-$$"
ADD_DIRS=(--add-dir "$REPO_DIR/skill-observations" --add-dir "$SKILLS")
for workspace in "${WORKSPACES[@]}"; do
    ADD_DIRS+=(--add-dir "$workspace")
done
# 명령 스킬의 소유 저장소도 읽을 수 있어야 jhw:* 관찰을 누락하지 않는다.
for skill_dir in "$HOME/.claude/plugins/cache" "$HOME/.claude/commands/jhw"; do
    if [ -d "$skill_dir" ]; then
        ADD_DIRS+=(--add-dir "$(readlink -f "$skill_dir")")
    fi
done

if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 2000000 ]; then
    mv -f "$LOG" "$LOG.1"
fi

cd "$REPO_DIR" 2>/dev/null || { echo "$(date -Is) cd 실패: $REPO_DIR" >> "$LOG"; exit 1; }

PROMPT="task-observer 스킬의 정기 리뷰를 실행해라.

절차: ${SKILLS}/task-observer/references/weekly-review.md 를 읽고 그대로 따른다.
읽기 전용 관찰 워크스페이스(각 log.md와 cross-cutting-principles.md):
${INVENTORY}
이번 산출물 경로: ${OUTPUT}
이전 pending 보고서 및 번들: ${REVIEW_ROOT}
다중 소스 모드의 원본 보존 규칙을 따른다. 로그·archive·last-review-date.txt는
수정하지 않는다. 각 출처 경로와 관찰 번호를 묶어서 식별한다(번호만으로 합치지 않는다).
검토 결과·누락 사유·검토 시각은 이번 경로의 REVIEW.md에 기록한다.

이 실행은 **scheduled autonomous 모드**다(사용자 부재).
- escalate 대상이 아닌 관찰만 적용한다.
- 다음은 적용하지 말고 보고만 한다: 새 스킬을 제안하는 관찰, 기존 내용을 제거하거나 크게
  재구조화하는 것, 스스로 불확실하다고 표시한 것, 서로 충돌하는 관찰.
- live 스킬 파일(플러그인 캐시 포함)은 절대 수정하지 않는다. staged 사본만 만든다.
- 작업 큐는 '### Observation N:' 헤더 열거에서 만든다(Status grep 만으로 만들지 않는다).
- 기존 pending 보고서와 현재 live 파일을 대조해 유효한 초안을 중복 생성하지 않는다.
- 초안은 STAGED(설치 대기)이며 ACTIONED가 아니다. 실제 설치 수는 0으로 보고한다.
- 모든 소스의 헤더 수와 분류 수를 대조한다. 읽기 실패는 빈 큐로 간주하지 않는다.
- 끝나면 소스별 검토/건너뜀 상태와 STAGED·설치·미해결 수를 REVIEW.md에 기록한다.
- 미해결 관찰이 없더라도 검토한 출처와 시각을 REVIEW.md에 기록한다.
- 멀티파일 스킬은 .skill 번들로 묶는다. rm 은 허용되지 않으므로 빌드 산출물은 삭제하지 말고
  zip 의 제외 패턴으로 뺀다: zip -r x.skill <dir> -x '*__pycache__*' '*.pyc' '*.DS_Store'
- 스테이징 후 원본의 실행 비트를 잃지 않도록 chmod 로 복원한다(chmod 는 허용됨).
- 도구 제약(2026-09-01 실측): Bash 로 ls/find/diff/stat 는 allow 규칙을 넣어도 거부된다.
  파일 목록·내용·비교는 Read/Glob/Grep 도구로 해라. Bash 는 chmod/zip/unzip/cp/mkdir/tar
  에만 쓴다. 이 제약 때문에 작업을 포기하지 말고 전용 도구로 우회해라.
- 설치 대기 번들은 나이와 개수에 관계없이 보존한다. 설치/폐기 확인된 오래된 번들만
  keep-two 정리 후보로 보고하며 이 실행에서는 삭제하지 않는다."

{
    echo "===== weekly-skill-review $(date -Is) ====="
    claude -p "$PROMPT" \
        "${ADD_DIRS[@]}" \
        --permission-mode acceptEdits \
        --settings "$SCRIPT_DIR/weekly-skill-review-settings.json" \
        < /dev/null 2>&1
    REVIEW_EXIT=$?
    echo "exit=$REVIEW_EXIT"
    echo
} >> "$LOG" 2>&1
exit "$REVIEW_EXIT"
