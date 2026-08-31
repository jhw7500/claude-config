---
description: Update HANDOFF.<session>.md with current session state so another AI tool can pick up the work
argument-hint: "[session-name]"
---

You are saving a handoff document so another AI (Codex, Cursor, or a fresh Claude session) can continue this work without losing context.

## Session name

The user invoked this command with: `$ARGUMENTS`

- If `$ARGUMENTS` is non-empty, treat it as the **session name**. Slugify it (lowercase, replace spaces/`/` with `-`, strip anything that isn't `[a-z0-9._-]`). Target file: `HANDOFF.<slug>.md` at the repo root.
- If `$ARGUMENTS` is empty, target file is `HANDOFF.md` at the repo root (legacy default).

Use `git rev-parse --show-toplevel` to find the repo root. From here on, "the handoff file" refers to whichever path you resolved above.

## What to do

1. Read the existing handoff file if present. Preserve prior content — merge intelligently, don't wipe.
2. Review the recent conversation in your context. Identify:
   - The actual task being worked on (not the original request, the current state of it)
   - What's been done, what's in-flight, what's been ruled out
   - Decisions made and the reasoning
   - Open questions or unknowns
   - The very next concrete action a new AI should take
3. Run `git status` and `git diff --stat HEAD` to capture the working tree state.
4. Write the handoff file using the template below. Fill every section. Use `file:line` format for code references.
5. After saving, print exactly one line: `Handoff saved → <relative-path> · next: <next concrete step in <=15 words>`
   - Example with session: `Handoff saved → HANDOFF.kafka-dlq.md · next: wire DLQ retry header in consumer.ts:88`
   - Example without: `Handoff saved → HANDOFF.md · next: wire DLQ retry header in consumer.ts:88`

## Rules

- "Current State" reflects what's actually in the tree right now — run `git status` to verify, don't trust your memory.
- "Next Steps" must be concrete enough that a new AI can execute without asking clarifying questions. Bad: "fix the bug". Good: "in apps/agent/src/foo.ts:42 the timeout uses ms but the API expects seconds — convert it".
- Rewrite the "Context for the next tool" section from scratch every time. Don't leave stale narrative.
- Don't add new top-level sections. Keep the template stable so consumers know where to look.
- Don't ask before saving. This is a one-shot operation.
- If you genuinely have no new information to add (e.g. ran twice in a row), update the timestamp and skip the rest.
- Write the file content in English so any tool (Codex, Cursor, fresh Claude) can consume it without translation friction.
- One handoff = one session. Do not merge unrelated sessions into the same file. If the current work doesn't match the existing file's "Active Task", warn the user once and ask whether to overwrite or pick a different session name — do not silently clobber another session's state.

## Template (use exactly this structure when creating from scratch)

```markdown
# Handoff — <session-name-or-branch>
_Last updated: <ISO 8601 timestamp with timezone> · Tool: claude-code · Session: <session-slug or "default">_

## Active Task
<One sentence: what we're doing and why. The current task, not the original ask.>

## Current State
**Done:**
- <bullet with file:line refs>

**In flight:**
- <bullet>

**Tried and rejected:**
- <approach> — <why rejected>

## Next Steps
1. <concrete action with file refs>
2. <next>
3. <next>

## Key Decisions
- **Decision:** <what> · **Why:** <reasoning> · **Alternative considered:** <what was rejected and why>

## Open Questions
- [ ] <question that needs human input>

## Working Environment
- Branch: `<name>` · Base: `<main|master>`
- Commands to run: `<build/test/lint commands>`
- Known broken / skipped: <failing tests, lint issues, skipped checks>
- Changed files (`git diff --stat HEAD`):
  ```
  <paste output>
  ```

## Context for the next tool (3-5 sentences)
<Self-contained narrative: what file/system we're touching, what constraint shapes the work, what an outsider needs to know to make sense of "Next Steps". Rewrite this section from scratch every time. Mention which tool wrote this handoff and which tool is expected to pick it up if known (e.g. "Handing off to Codex to wire the retry header"); otherwise leave tool-agnostic.>
```

After writing, verify the file is well-formed Markdown, the timestamp is current, and the "Session:" field in the header matches the resolved slug (or `default` for the legacy `HANDOFF.md`).
