#!/bin/bash
# weekly-skill-review — task-observer 관찰 로그 정기 리뷰 (scheduled autonomous 모드)
#
# 경로 주의: 워크스페이스 실체는 ~/.claude/ **밖**에 있다.
#   ~/.claude/projects/<id>/skill-observations 는 심링크일 뿐이고, 그 경로로 쓰려 하면
#   Claude 의 sensitive-file 정책이 `.claude/**` 를 차단한다(심링크 해석 *전* 경로 문자열로
#   판정하므로 심링크를 통해도 막힌다 — 2026-09-01 실측). 따라서 아래 WS 는 실체 경로다.
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

if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 2000000 ]; then
    mv -f "$LOG" "$LOG.1"
fi

cd "$REPO_DIR" 2>/dev/null || { echo "$(date -Is) cd 실패: $REPO_DIR" >> "$LOG"; exit 1; }

PROMPT="task-observer 스킬의 정기 리뷰를 실행해라.

절차: ${SKILLS}/task-observer/references/weekly-review.md 를 읽고 그대로 따른다.
관찰 워크스페이스: ${WS}
  - log.md, cross-cutting-principles.md, last-review-date.txt, archive/ 가 여기 있다.
  - ~/.claude/projects/... 경로는 심링크이고 쓰기가 차단되므로 **반드시 위 실체 경로**를 쓴다.
  - staged 산출물도 ${WS}/../skill-updates/<date>/ 아래에 둔다.

이 실행은 **scheduled autonomous 모드**다(사용자 부재).
- escalate 대상이 아닌 관찰만 적용한다.
- 다음은 적용하지 말고 보고만 한다: 새 스킬을 제안하는 관찰, 기존 내용을 제거하거나 크게
  재구조화하는 것, 스스로 불확실하다고 표시한 것, 서로 충돌하는 관찰.
- live 스킬 파일(플러그인 캐시 포함)은 절대 수정하지 않는다. staged 사본만 만든다.
- 작업 큐는 '### Observation N:' 헤더 열거에서 만든다(Status grep 만으로 만들지 않는다).
- 끝나면 last-review-date.txt 를 갱신하고 요약을 출력한다.
- OPEN 관찰이 없으면 아무것도 만들지 말고 타임스탬프만 갱신하고 끝낸다.
- 멀티파일 스킬은 .skill 번들로 묶는다. rm 은 허용되지 않으므로 빌드 산출물은 삭제하지 말고
  zip 의 제외 패턴으로 뺀다: zip -r x.skill <dir> -x '*__pycache__*' '*.pyc' '*.DS_Store'
- 스테이징 후 원본의 실행 비트를 잃지 않도록 chmod 로 복원한다(chmod 는 허용됨).
- 도구 제약(2026-09-01 실측): Bash 로 ls/find/diff/stat 는 allow 규칙을 넣어도 거부된다.
  파일 목록·내용·비교는 Read/Glob/Grep 도구로 해라. Bash 는 chmod/zip/unzip/cp/mkdir/tar
  에만 쓴다. 이 제약 때문에 작업을 포기하지 말고 전용 도구로 우회해라.
- skill-updates/ 아래 날짜 디렉터리는 최신 2개만 남긴다(keep-two). 삭제가 필요하면 rm 이
  막혀 있으므로 지우지 말고 요약에 '정리 필요' 로 보고만 해라."

{
    echo "===== weekly-skill-review $(date -Is) ====="
    claude -p "$PROMPT" \
        --add-dir "$WS" \
        --add-dir "$(dirname "$WS")" \
        --add-dir "$SKILLS" \
        --add-dir "$HOME/.claude/plugins/cache" \
        --permission-mode acceptEdits \
        --settings "$SCRIPT_DIR/weekly-skill-review-settings.json" \
        < /dev/null 2>&1
    echo "exit=$?"
    echo
} >> "$LOG" 2>&1
