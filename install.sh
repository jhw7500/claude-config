#!/bin/bash
# 다른 호스트에서 실행: 개인 Claude Code 자산 설치 (스킬 + 셸 함수 + 스크립트 + 지침)
set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1) plugin-toggle 스킬 심볼릭 링크 (repo pull 시 자동 갱신)
mkdir -p ~/.claude/skills
ln -sfn "$REPO_DIR/skills/plugin-toggle" ~/.claude/skills/plugin-toggle
echo "[install] 스킬 링크: ~/.claude/skills/plugin-toggle"

# 2) plug 함수 source (중복 방지)
if grep -q "claude-config/shell/plug.sh" ~/.bashrc 2>/dev/null; then
  echo "[install] plug 함수 이미 등록됨"
else
  echo "source $REPO_DIR/shell/plug.sh" >> ~/.bashrc
  echo "[install] plug 함수 추가 -> ~/.bashrc"
fi

# 3) 개인 스크립트 심볼릭 링크
mkdir -p ~/.claude/scripts
for f in stop-text-required.py timestamp-hook.py bg-hud-complete.py context-bar.sh apex-toggle.sh gstack-toggle.sh; do
  [ -f "$REPO_DIR/scripts/$f" ] && ln -sfn "$REPO_DIR/scripts/$f" ~/.claude/scripts/"$f"
done
echo "[install] 스크립트 링크: ~/.claude/scripts/ (6개)"

# 4) 글로벌 지침 — OMC 무관 파일만 자동 복사(없을 때만), CLAUDE.md는 수동 머지
cp -n "$REPO_DIR/claude-md/CLAUDE-notion.md" ~/.claude/ 2>/dev/null && echo "[install] CLAUDE-notion.md 복사" || echo "[install] CLAUDE-notion.md 이미 존재(건너뜀)"
cp -n "$REPO_DIR/claude-md/RTK.md"          ~/.claude/ 2>/dev/null && echo "[install] RTK.md 복사" || echo "[install] RTK.md 이미 존재(건너뜀)"

echo ""
echo "완료. 적용: source ~/.bashrc  +  새 Claude 세션"
echo ""
echo "[수동] CLAUDE.md 는 상단 OMC 블록(자동관리)이 있어 자동 복사하지 않음."
echo "       사용자 지침 부분만 머지하세요:  diff $REPO_DIR/claude-md/CLAUDE.md ~/.claude/CLAUDE.md"
echo "[주의] settings.json / .claude.json 은 호스트별 상태라 동기화 안 함."
echo "       플러그인 off 는 'plug off <key>' 또는 plugin-toggle 스킬로 이 호스트에서 적용."
