#!/usr/bin/env bash
# PreCompact hook: blocks auto/manual compaction if no recent handoff file
# exists. Looks for `HANDOFF.md` (legacy default) AND any session-scoped
# `HANDOFF.<session>.md` at the project root, and uses the freshest mtime.
# If everything is stale or missing, prompts Claude to run /handoff (or
# /handoff <session>) so state is preserved before context is summarized.

set -u

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
