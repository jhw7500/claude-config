#!/bin/bash
# 다른 호스트에서 실행: 개인 Claude Code 자산 설치 (스킬 + 셸 함수 + 스크립트 + 지침)
set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1) 스킬 심볼릭 링크 (skills/ 아래 모든 스킬 자동 링크, repo pull 시 자동 갱신)
mkdir -p ~/.claude/skills
for d in "$REPO_DIR"/skills/*/; do
  name="$(basename "$d")"
  ln -sfn "${d%/}" ~/.claude/skills/"$name"
  echo "[install] 스킬 링크: ~/.claude/skills/$name"
done

# 2) plug 함수 source (중복 방지)
if grep -q "claude-config/shell/plug.sh" ~/.bashrc 2>/dev/null; then
  echo "[install] plug 함수 이미 등록됨"
else
  echo "source $REPO_DIR/shell/plug.sh" >> ~/.bashrc
  echo "[install] plug 함수 추가 -> ~/.bashrc"
fi

# 3) 개인 스크립트 심볼릭 링크
mkdir -p ~/.claude/scripts
for f in stop-text-required.py timestamp-hook.py bg-hud-complete.py context-bar.sh apex-toggle.sh; do
  [ -f "$REPO_DIR/scripts/$f" ] && ln -sfn "$REPO_DIR/scripts/$f" ~/.claude/scripts/"$f"
done
echo "[install] 스크립트 링크: ~/.claude/scripts/ (5개)"

# 4) 글로벌 지침 — OMC 무관 파일만 자동 복사(없을 때만), CLAUDE.md는 수동 머지
cp -n "$REPO_DIR/claude-md/CLAUDE-notion.md" ~/.claude/ 2>/dev/null && echo "[install] CLAUDE-notion.md 복사" || echo "[install] CLAUDE-notion.md 이미 존재(건너뜀)"
cp -n "$REPO_DIR/claude-md/RTK.md"          ~/.claude/ 2>/dev/null && echo "[install] RTK.md 복사" || echo "[install] RTK.md 이미 존재(건너뜀)"

# 5) 훅 배선 (settings.json) — timestamp-hook + stop-text-required. 멱등·가산·백업.
#    statusLine(context-bar) 교체와 CLAUDE.md 머지는 별도 검토 대상이라 여기서 다루지 않음.
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
  cp "$SETTINGS" "$SETTINGS.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$SETTINGS" <<'PY'
import json, sys
f = sys.argv[1]
with open(f) as fh:
    d = json.load(fh)
hooks = d.setdefault("hooks", {})

def ensure(event, command):
    groups = hooks.setdefault(event, [])
    for g in groups:
        for h in g.get("hooks", []):
            if h.get("command") == command:
                return False  # 이미 있음 → 멱등 skip
    groups.append({"hooks": [{"type": "command", "command": command}]})
    return True

ts = "python3 $HOME/.claude/scripts/timestamp-hook.py"
st = "python3 $HOME/.claude/scripts/stop-text-required.py"
added = []
if ensure("UserPromptSubmit", ts + " prompt"): added.append("UserPromptSubmit<-timestamp")
if ensure("Stop", ts + " stop"): added.append("Stop<-timestamp")
if ensure("Stop", st): added.append("Stop<-stop-text-required")
with open(f, "w") as fh:
    json.dump(d, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("[install] 훅 배선:", ", ".join(added) if added else "이미 적용됨(변경 없음)")
PY
else
  echo "[install] settings.json 없음 — 훅 배선 건너뜀"
fi

echo ""
echo "완료. 적용: source ~/.bashrc  +  새 Claude 세션"
echo "[수동] CLAUDE.md 는 상단 OMC 블록(자동관리)이 있어 자동 복사하지 않음."
echo "       사용자 지침 부분만 머지:  diff \$REPO_DIR/claude-md/CLAUDE.md ~/.claude/CLAUDE.md"
echo "[주의] settings.json / .claude.json 은 호스트별 상태라 동기화 안 함."
