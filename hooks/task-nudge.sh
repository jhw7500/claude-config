#!/usr/bin/env bash
adapter="$HOME/.local/share/claude-config/hooks/task-nudge-claude.py"
[ -f "$adapter" ] || exit 0
/usr/bin/python3 "$adapter" || exit 0
