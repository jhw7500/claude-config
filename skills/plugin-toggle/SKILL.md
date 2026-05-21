---
name: plugin-toggle
description: |
  Enable or disable Claude Code plugins (bkit, document-skills, etc.) without the interactive /plugin menu.
  Use when asked to "turn bkit on/off", "enable/disable a plugin", "플러그인 켜줘/꺼줘",
  "bkit 이 프로젝트에서만 켜줘", or to check plugin status. Edits enabledPlugins in settings.json.
---

# plugin-toggle

Toggles plugins by flipping `enabledPlugins` booleans in settings.json (no dir moves).

**Apply WITHOUT restart**: after editing, run `/reload-plugins` in the session — it reloads
plugins/skills/agents/MCP/hooks and picks up enable/disable changes live. A full restart is
NOT required (official: "When you install, enable, or disable plugins during a session,
run `/reload-plugins` to pick up all changes without restarting").

## Scope (which settings file to edit)

- **project (DEFAULT)** → `.claude/settings.local.json` in the current repo (personal, git-ignored).
  Best for "turn bkit on just for this project". Project/local settings override user settings.
- **global** → `~/.claude/settings.json`. Use only when the user says "global" / "전역" / "everywhere".

## How to run

1. Parse intent: plugin name(s), action (`on`/`off`/`status`), and scope (default project; global if asked).
2. Resolve each short name to its full `name@marketplace` key by reading the **live**
   `~/.claude/settings.json` `enabledPlugins` (the source of truth — keys differ per host)
   and matching the prefix before `@`. List this host's available keys with:
   `python3 -c "import json,os;print('\n'.join(json.load(open(os.path.expanduser('~/.claude/settings.json'))).get('enabledPlugins',{})))"`
   If a short name matches zero or multiple keys, ask instead of guessing.
3. Run the matching Python block **verbatim** (it preserves all other settings and creates files/keys as needed).
4. Report what changed, then tell the user to run **`/reload-plugins`** to apply immediately (no restart).

### status — show user (global) + project (local) state
```bash
python3 - <<'PY'
import json,os
def load(p):
    p=os.path.expanduser(p)
    return json.load(open(p)).get('enabledPlugins',{}) if os.path.exists(p) else {}
u=load('~/.claude/settings.json'); l=load('.claude/settings.local.json'); pr=load('.claude/settings.json')
keys=sorted(set(u)|set(l)|set(pr))
print(f"{'plugin':40} {'global':8} {'project':8} -> effective")
for k in keys:
    g=u.get(k); p=l.get(k, pr.get(k))
    eff = p if p is not None else (g if g is not None else True)
    print(f"{k:40} {str(g):8} {str(p):8} -> {'ON' if eff else 'OFF'}")
PY
```

### on / off — set a plugin (replace KEY and VALUE; VALUE is `true` for on, `false` for off)
Project scope (default) — edits `.claude/settings.local.json`:
```bash
python3 - <<'PY'
import json,os
KEY="bkit@bkit-marketplace"; VALUE=True   # <- set KEY and VALUE per request
f=".claude/settings.local.json"
os.makedirs(".claude",exist_ok=True)
d=json.load(open(f)) if os.path.exists(f) else {}
d.setdefault("enabledPlugins",{})[KEY]=VALUE
json.dump(d,open(f,"w"),indent=2); open(f,"a").write("\n")
print(f"[project] {KEY} -> {'ON' if VALUE else 'OFF'} in {f}")
PY
```
Global scope (only if asked) — edits `~/.claude/settings.json`:
```bash
python3 - <<'PY'
import json,os
KEY="bkit@bkit-marketplace"; VALUE=True   # <- set KEY and VALUE per request
f=os.path.expanduser("~/.claude/settings.json")
d=json.load(open(f))
d.setdefault("enabledPlugins",{})[KEY]=VALUE
json.dump(d,open(f,"w"),indent=2); open(f,"a").write("\n")
print(f"[global] {KEY} -> {'ON' if VALUE else 'OFF'} in {f}")
PY
```

For multiple plugins, loop the KEY list. Never reorder or drop other settings keys.

## After toggling
Always run `/reload-plugins` in the session to apply immediately (no restart needed).
