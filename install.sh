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

# 3.5) 커스텀 훅 심볼릭 링크 (hooks/ 전체 — repo pull 시 자동 갱신)
mkdir -p ~/.claude/hooks
for f in "$REPO_DIR"/hooks/*.py; do
  ln -sfn "$f" ~/.claude/hooks/"$(basename "$f")"
done
[ -f "$REPO_DIR/hooks/README.md" ] && ln -sfn "$REPO_DIR/hooks/README.md" ~/.claude/hooks/README.md
echo "[install] 훅 링크: ~/.claude/hooks/ ($(ls "$REPO_DIR"/hooks/*.py 2>/dev/null | wc -l)개)"

# 4) 전역 지침 머지 (env-aware) — 항상 global-guidance, 환경에 있는 것만 추가 import.
#    OMC 블록(inline/file-split 무관)은 절대 건드리지 않고 claude-config:START/END 블록만 관리.
CLAUDE_MD="$HOME/.claude/CLAUDE.md"
cp -f "$REPO_DIR/claude-md/global-guidance.md" ~/.claude/global-guidance.md
IMPORTS="@global-guidance.md"
if grep -qi "notion" "$HOME/.claude.json" 2>/dev/null; then
  cp -f "$REPO_DIR/claude-md/CLAUDE-notion.md" ~/.claude/CLAUDE-notion.md
  IMPORTS="$IMPORTS
@CLAUDE-notion.md"; NOTION="있음"
else
  rm -f ~/.claude/CLAUDE-notion.md 2>/dev/null; NOTION="없음(skip)"
fi
if command -v rtk >/dev/null 2>&1; then
  cp -f "$REPO_DIR/claude-md/RTK.md" ~/.claude/RTK.md
  IMPORTS="$IMPORTS
@RTK.md"; RTK="있음"
else
  rm -f ~/.claude/RTK.md 2>/dev/null; RTK="없음(skip)"
fi
python3 - "$CLAUDE_MD" "$IMPORTS" <<'PY'
import sys, os, re, datetime, shutil
f, imports = sys.argv[1], sys.argv[2]
START = "<!-- claude-config:START (managed by install.sh — do not edit between markers) -->"
END = "<!-- claude-config:END -->"
block = START + "\n" + imports + "\n" + END + "\n"
text = open(f).read() if os.path.exists(f) else ""
if os.path.exists(f):
    shutil.copy(f, f + ".bak." + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
if START in text and END in text:
    text = re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\n?", block, text, flags=re.S)
else:
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n" + block
open(f, "w").write(text)
print("[install] CLAUDE.md 전역지침 블록 갱신")
PY
echo "[install] 전역지침 머지 — notion: $NOTION, rtk: $RTK (OMC 블록 미변경)"

# 5) 훅 배선 (settings.json) — timestamp-hook + stop-text-required. 멱등·가산·백업.
#    statusLine(context-bar) 교체와 CLAUDE.md 머지는 별도 검토 대상이라 여기서 다루지 않음.
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
  cp "$SETTINGS" "$SETTINGS.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$SETTINGS" "$NOTION" <<'PY'
import json, sys
f = sys.argv[1]
notion = (len(sys.argv) > 2 and sys.argv[2] == "있음")
with open(f) as fh:
    d = json.load(fh)
hooks = d.setdefault("hooks", {})

def ensure(event, command, matcher=None):
    groups = hooks.setdefault(event, [])
    for g in groups:
        for h in g.get("hooks", []):
            if h.get("command") == command:
                return False  # 이미 있음 → 멱등 skip
    grp = {"hooks": [{"type": "command", "command": command}]}
    if matcher is not None:
        grp["matcher"] = matcher
    groups.append(grp)
    return True

S = "$HOME/.claude/scripts"
H = "$HOME/.claude/hooks"
ts = "python3 %s/timestamp-hook.py" % S
st = "python3 %s/stop-text-required.py" % S
gc = "python3 %s/general-continuation-hook.py" % H
bt = "python3 %s/bg-task-progress-hook.py" % H
pi = "python3 %s/post-info-tool-continuation-hook.py" % H
nc = "python3 %s/notion-continuous-exec-hook.py" % H
pa = "python3 %s/post-action-tool-report-hook.py" % H
PI_MATCH = ("ToolSearch|WebSearch|WebFetch|mcp__notion__notion-search|mcp__notion__notion-fetch|"
            "mcp__notion__notion-get-comments|mcp__jhw-notion__jhw_search|mcp__jhw-notion__jhw_context|"
            "mcp__jhw-notion__jhw_history|mcp__jhw-notion__jhw_status|"
            "mcp__plugin_context7_context7__query-docs|mcp__plugin_context7_context7__resolve-library-id")
PA_MATCH = ("mcp__jhw-notion__jhw_(record|note|delete|start|close|report_export)|"
            "mcp__notion__notion-(create-pages|update-page|create-database|update-data-source|"
            "create-comment|duplicate-page|move-pages)")
added = []
# 기존 (항상)
if ensure("UserPromptSubmit", ts + " prompt"): added.append("UPS<-timestamp")
if ensure("Stop", ts + " stop"): added.append("Stop<-timestamp")
if ensure("Stop", st): added.append("Stop<-stop-text")
# 흡수 — 항상 배선 (환경 무관하게 안전)
if ensure("UserPromptSubmit", gc): added.append("UPS<-general-continuation")
if ensure("PreToolUse", bt, "Agent|Bash"): added.append("Pre<-bg-task")
if ensure("PostToolUse", bt, "Agent|Bash"): added.append("Post<-bg-task")
if ensure("SubagentStop", bt, "*"): added.append("SubagentStop<-bg-task")
if ensure("PostToolUse", pi, PI_MATCH): added.append("Post<-post-info")
# 흡수 — notion 환경만
if notion:
    if ensure("UserPromptSubmit", nc): added.append("UPS<-notion-continuous")
    if ensure("PostToolUse", pa, PA_MATCH): added.append("Post<-post-action")
# 주: carl-hook 은 파일만 동기화하고 자동 배선하지 않음 (APEX/CARL 사용 시 수동 배선)
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
echo "[정보] CLAUDE.md 의 OMC 블록은 미변경, claude-config:START/END 블록만 env-aware로 관리."
echo "[주의] settings.json / .claude.json 은 호스트별 상태라 동기화 안 함."
