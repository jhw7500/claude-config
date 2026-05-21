---
name: gstack-toggle
description: |
  Enable, disable, or check gstack's ~47 user skills to control Claude Code context.
  Use when asked to "turn gstack on/off", "enable/disable gstack skills", "gstack 켜줘/꺼줘",
  "gstack 스킬 토글", or to check gstack skill status. Keeps the gstack repo dir and its
  bin/CLI/browser-daemon intact — only the individual skill copies are moved.
---

# gstack-toggle

gstack installs ~47 individual skill directories directly into `~/.claude/skills/`
(autoplan, browse, ship, qa, review, design-*, plan-*, etc.). They fill the
skill-listing context budget every session. This skill moves them in/out of a
holding directory while **never** touching:

- `~/.claude/skills/gstack` — the repo body (holds `bin/`, the browser daemon, git)
- `~/.claude/skills/omc-reference` — OMC, not gstack

## How to run

1. Parse the user's intent into ONE action: `off`, `on`, or `status`.
2. Run the matching Bash block below **verbatim**.
3. Report the count and remind: changes take effect in a **new Claude Code session**
   (the current session's skill list is already loaded in memory).

### off — disable gstack skills
```bash
SKILLS="$HOME/.claude/skills"; REPO="$SKILLS/gstack"; HOLD="$HOME/.claude/skills-disabled"
mkdir -p "$HOLD"; n=0
for path in "$SKILLS"/*/; do
  name="$(basename "$path")"
  [ "$name" = "gstack" ] && continue
  [ "$name" = "omc-reference" ] && continue
  [ "$name" = "gstack-toggle" ] && continue
  if [ -d "$REPO/$name" ] && [ -f "$SKILLS/$name/SKILL.md" ]; then
    mv "$SKILLS/$name" "$HOLD/$name"; n=$((n+1))
  fi
done
echo "gstack OFF: moved $n skill(s) -> $HOLD (repo & CLI kept)."
```

### on — re-enable gstack skills
```bash
SKILLS="$HOME/.claude/skills"; HOLD="$HOME/.claude/skills-disabled"; n=0
shopt -s nullglob
for path in "$HOLD"/*/; do
  name="$(basename "$path")"
  mv "$HOLD/$name" "$SKILLS/$name"; n=$((n+1))
done
echo "gstack ON: restored $n skill(s)."
```

### status — show counts
```bash
SKILLS="$HOME/.claude/skills"; HOLD="$HOME/.claude/skills-disabled"
echo "active skill dirs: $(find "$SKILLS" -maxdepth 1 -mindepth 1 -type d | wc -l) | held (disabled): $(find "$HOLD" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)"
```
