#!/usr/bin/env bash
# Fast-forward the runtime clones that ~/.claude symlinks into.
#
# install.sh points ~/.claude/{hooks,skills,commands} at whichever clone it was
# run from, so a second "runtime" checkout keeps live hooks off the branches a
# dev checkout wanders onto. Nothing kept that runtime copy current, and it fell
# 46 and 102 commits behind — which is why /jhw:pr was missing and a merged hook
# fix stayed dark.
#
# Fast-forward only: a runtime clone that is dirty or ahead is left alone and
# reported, never rewritten. Wiring is NOT touched — when the pull moves files
# that install.sh deploys, this says so and stops there.
set -uo pipefail

LOG=${RUNTIME_SYNC_LOG:-$HOME/.claude/logs/runtime-sync.log}
# A log nobody opens is not a notice. Anything still needing hands lands here,
# one file, rewritten each run so it is empty exactly when there is nothing to do.
MARKER=${RUNTIME_SYNC_MARKER:-$HOME/.claude/logs/runtime-sync-action-required}
PAIRS=${RUNTIME_SYNC_PAIRS:-"$HOME/ai/opencode/projects/claude-config-runtime
$HOME/ai/opencode/projects/jhw-notion-runtime"}

# Paths whose movement means the live install is stale until install.sh re-runs.
WIRING_PATHS='^(hooks/|scripts/|skills/|commands/|claude-md/|install\.sh$)'
BUILD_PATHS='^mcp-server/src/'

mkdir -p "$(dirname "$LOG")"

say() {
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$1" | tee -a "$LOG"
}

action_required=0

sync_one() {
  local repo=$1 name
  name=$(basename "$repo")

  if [ ! -d "$repo/.git" ]; then
    say "[skip] $name — not a git checkout"
    return
  fi
  if [ -n "$(git -C "$repo" status --porcelain --untracked-files=no)" ]; then
    say "[skip] $name — tracked files modified; resolve by hand"
    return
  fi

  local branch upstream before after
  branch=$(git -C "$repo" symbolic-ref --quiet --short HEAD) || {
    say "[skip] $name — detached HEAD"
    return
  }
  upstream="origin/$branch"

  if ! git -C "$repo" fetch --quiet origin "$branch" 2>/dev/null; then
    say "[warn] $name — fetch failed"
    return
  fi
  if [ "$(git -C "$repo" rev-list --count "$upstream..HEAD")" != "0" ]; then
    say "[skip] $name — ahead of $upstream; unmerged local commits"
    return
  fi

  before=$(git -C "$repo" rev-parse HEAD)
  after=$(git -C "$repo" rev-parse "$upstream")
  if [ "$before" = "$after" ]; then
    say "[ok] $name — already at ${after:0:8}"
    return
  fi
  if ! git -C "$repo" merge --ff-only --quiet "$upstream"; then
    say "[warn] $name — fast-forward refused"
    return
  fi

  local changed
  changed=$(git -C "$repo" diff --name-only "$before" "$after")
  say "[sync] $name — ${before:0:8} -> ${after:0:8} ($(printf '%s\n' "$changed" | grep -c .) files)"

  if printf '%s\n' "$changed" | grep -qE "$WIRING_PATHS"; then
    say "[ACTION] $name — run: bash $repo/install.sh   (hook wiring / deployed copies are stale)"
    printf 'bash %s/install.sh\n' "$repo" >>"$MARKER"
    action_required=1
  fi
  if printf '%s\n' "$changed" | grep -qE "$BUILD_PATHS"; then
    say "[ACTION] $name — run: (cd $repo/mcp-server && npm run build)   (MCP server dist is stale)"
    printf '(cd %s/mcp-server && npm run build)\n' "$repo" >>"$MARKER"
    action_required=1
  fi
}

: >"$MARKER"

while IFS= read -r repo; do
  [ -n "$repo" ] || continue
  sync_one "$repo"
done <<<"$PAIRS"

exit "$action_required"
