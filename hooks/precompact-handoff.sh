#!/usr/bin/env bash
# PreCompact hook: blocks auto/manual compaction if no recent handoff file
# exists. Looks for `HANDOFF.md` (legacy default) AND any session-scoped
# `HANDOFF.<session>.md` at the project root, and uses the freshest mtime.
# If everything is stale or missing, prompts Claude to run /handoff (or
# /handoff <session>) so state is preserved before context is summarized.

set -u

# 발화 하트비트. 이 훅은 handoff 가 없거나 낡았을 때만 출력하므로 "출력 없음"이
# 정상 상태다. 따라서 transcript 마커만으로는 조건 미충족과 무동작이 구분되지
# 않는다 — scripts/hook-selfcheck.py 가 이 파일의 mtime 을 발화 증거로 읽는다.
# 실패해도 훅 본연의 동작을 막지 않는다.
HEARTBEAT_DIR="${CLAUDE_HOOK_HEARTBEAT_DIR:-$HOME/.claude/hook-heartbeat}"
# 파일명은 스크립트 자신에서 도출한다. 하드코딩하면 이름을 바꿨을 때 자가진단이
# 조용히 하트비트를 못 찾고, 그게 바로 이 장치가 막으려던 실패 양식이다
# (hook-selfcheck.py 는 배선된 경로의 basename 으로 찾는다).
if mkdir -p "$HEARTBEAT_DIR" 2>/dev/null; then
  date -Iseconds > "$HEARTBEAT_DIR/$(basename "${BASH_SOURCE[0]}")" 2>/dev/null || true
fi

STALE_SECONDS=${HANDOFF_STALE_SECONDS:-600}   # 10 minutes default
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

mtime_of() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}

newest_mtime=0
newest_file=""

# Match HANDOFF.md and HANDOFF.<anything>.md at the project root.
shopt -s nullglob
for f in "$PROJECT_DIR"/HANDOFF.md "$PROJECT_DIR"/HANDOFF.*.md; do
  [ -f "$f" ] || continue
  m=$(mtime_of "$f")
  [ -z "$m" ] && continue
  if [ "$m" -gt "$newest_mtime" ]; then
    newest_mtime=$m
    newest_file=$f
  fi
done
shopt -u nullglob

needs_handoff=0
reason=""

if [ -z "$newest_file" ]; then
  needs_handoff=1
  reason="No HANDOFF.md or HANDOFF.<session>.md found at $PROJECT_DIR."
else
  now=$(date +%s)
  age=$((now - newest_mtime))
  if [ "$age" -gt "$STALE_SECONDS" ]; then
    needs_handoff=1
    base=$(basename "$newest_file")
    reason="Freshest handoff ($base) is ${age}s old (threshold ${STALE_SECONDS}s)."
  fi
fi

# Hook stdin carries {"trigger":"manual"|"auto", ...}. Default behavior splits by it:
#   auto   → block (the reason goes to Claude, which writes /handoff itself — no user friction,
#            and protection matters most right before an automatic context squeeze)
#   manual → warn-only (the user asked for /compact; do not bounce their own command)
# Overrides: HANDOFF_GATE=block (always block) / HANDOFF_GATE=warn (never block).
TRIGGER=$(jq -r '.trigger // "manual"' 2>/dev/null || echo manual)

if [ "$needs_handoff" -eq 1 ]; then
  mode="${HANDOFF_GATE:-auto}"
  if [ "$mode" = "auto" ]; then
    if [ "$TRIGGER" = "auto" ]; then mode=block; else mode=warn; fi
  fi
  if [ "$mode" = "block" ]; then
    # JSON output → Claude Code blocks compaction and surfaces the reason.
    printf '{"decision":"block","reason":"%s Run the /handoff slash command (or /handoff <session> for a named session) to update the handoff file so the next AI tool (Codex, Cursor, or a fresh session) can continue this work. After /handoff completes, the compaction will proceed."}\n' "$reason"
  else
    # Warn-only — surface the reminder but let compaction proceed.
    printf '%s Consider running /handoff to preserve session state before compaction. (Set HANDOFF_GATE=block to restore the blocking gate.)\n' "$reason"
  fi
fi

exit 0
