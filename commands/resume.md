---
description: Read HANDOFF.<session>.md and continue the work from where the previous AI session left off
argument-hint: "[session-name]"
---

You are resuming work that another AI session (Claude, Codex, Cursor, or a previous Claude run) saved into a handoff file. Your job is to load that state, verify it matches reality, and execute the next concrete step.

## Session name

The user invoked this command with: `$ARGUMENTS`

- If `$ARGUMENTS` is non-empty, treat it as the **session name**. Slugify it (lowercase, replace spaces/`/` with `-`, strip anything that isn't `[a-z0-9._-]`). Target file: `HANDOFF.<slug>.md` at the repo root.
- If `$ARGUMENTS` is empty, target file is `HANDOFF.md` at the repo root (legacy default).

Use `git rev-parse --show-toplevel` to find the repo root. From here on, "the handoff file" refers to whichever path you resolved above.

## Steps

1. **Locate and read the handoff file.**
   - If the resolved file exists, read it.
   - If it does not exist:
     - List sibling handoff files at the repo root matching `HANDOFF*.md` (e.g. via `git ls-files HANDOFF*.md` or a `Glob`).
     - Stop and tell the user:
       ```
       No <resolved-path> found.
       Available sessions: <comma-separated list of HANDOFF.*.md slugs, or "(none)">
       Run /handoff <session> in the source tool, or pass a different session name.
       ```
     - Do not try to guess which file to load.

2. **Verify against reality** before trusting the handoff:
   - Run `git status` and `git rev-parse --abbrev-ref HEAD`.
   - Run `git log --oneline -5`.
   - Compare to the "Working Environment" section of the handoff:
     - **Branch mismatch:** if current branch differs from the one in the handoff, stop and ask the user whether to switch branches or proceed on the current one. Do not silently switch.
     - **Working tree drift:** if `git status` shows files that contradict "In flight" (e.g. handoff says X is in-flight but X is clean, or handoff lists no changes but the tree is dirty), surface the mismatch in one line.
     - **Timestamp:** if "Last updated" is more than 7 days old, flag it — the handoff may be stale.
     - **Session sanity:** if the header's "Session:" field disagrees with the slug you resolved from `$ARGUMENTS`, surface it — the file may have been renamed or copied.

3. **Print a resume summary** in this exact shape, then continue:
   ```
   Resuming from <resolved-path> (updated <relative time>, written by <tool from header>)
   Task: <one line from "Active Task">
   Next: <first item from "Next Steps">
   <optional: ⚠ Mismatch: <one-line description>>
   ```

4. **Execute the first item in "Next Steps"** unless step 2 surfaced a blocking mismatch. Treat that item as your task — use your usual planning, tool-calling, and verification. Don't re-summarize the whole handoff; just do the work.

5. **When that step is done**, ask the user whether to continue with the next item or stop. Don't auto-chain through every step — the user may have new priorities.

## Rules

- Trust the handoff as a *starting point*, not as ground truth. The code is ground truth — always reconcile with `git status` and direct file reads before acting.
- Don't modify the handoff file from this command. That's `/handoff`'s job. (Exception: if the task you executed completes a "Next Step" item, you may suggest running `/handoff <same-session>` at the end of the work — but only if the user agrees.)
- Preserve the session name when chaining: if the user resumed from `HANDOFF.kafka-dlq.md`, any follow-up `/handoff` should target the same session unless the user says otherwise.
- If "Next Steps" is empty or vague, ask the user to clarify rather than guessing.
- If "Open Questions" contains items that block the next step, surface them and ask the user before proceeding.
- Read the "Context for the next tool" section carefully — it's the previous AI's best attempt to brief you. Use it to interpret ambiguous file references.
