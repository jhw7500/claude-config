#!/bin/bash
# 다른 호스트에서 실행: plugin-toggle 스킬 + plug 함수 설치
set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1) 스킬 심볼릭 링크 (repo pull 시 자동 갱신) — skills/ 아래 모든 스킬 자동 링크
mkdir -p ~/.claude/skills
for d in "$REPO_DIR"/skills/*/; do
  name="$(basename "$d")"
  ln -sfn "${d%/}" ~/.claude/skills/"$name"
  echo "[install] 스킬 링크: ~/.claude/skills/$name -> ${d%/}"
done

# 2) plug 함수 source (중복 방지)
if grep -q "claude-config/shell/plug.sh" ~/.bashrc 2>/dev/null; then
  echo "[install] plug 함수 이미 등록됨 (~/.bashrc)"
else
  echo "source $REPO_DIR/shell/plug.sh" >> ~/.bashrc
  echo "[install] plug 함수 source 추가 -> ~/.bashrc"
fi

echo ""
echo "완료. 적용: source ~/.bashrc  +  새 Claude 세션"
echo "주의: settings.json / .claude.json 은 호스트별 상태라 동기화하지 않음."
echo "      플러그인 off 정책은 이 호스트에서 'plug off <key>' 또는 plugin-toggle 스킬로 적용."
