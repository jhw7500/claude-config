---
name: artifact-publish
description: |
  Publish a document staged with artifact-outbox.py (usually exported from Codex) to the signed-in claude.ai account
  as a private Artifact, read it back, and record every step in the outbox entry. Use when given an artifact-outbox
  entry directory or asked to "publish this outbox entry", "artifact-outbox 항목 발행해줘",
  "Codex가 내보낸 문서를 Artifact로 저장해줘".
---

# artifact-publish

Only an interactive Claude Code session signed in with `/login` can publish Artifacts, so Codex stages a confirmed
document with `artifact-outbox.py export` and this skill publishes that exact file. Each step is recorded in the entry
before the next one runs, so a retry finds the artifact an earlier attempt made instead of creating a second one.

Every command below is `/usr/bin/python3 $HOME/.claude/scripts/artifact-outbox.py ...`, and `ENTRY` is the entry
directory the user gave. If the script is missing, report that `./install.sh` has not linked it and stop.

## Steps

1. **Check the entry.** Run `show --entry "$ENTRY"`. Exit 3 means the document changed after export: stop and ask the
   user to export it again. From the output, note `title`, `document`, `mode`, `target_url`, `state` and `url`.
   - `verified`: already done. Report the URL and stop.
   - `published`: the publish was recorded but not checked. Run step 2, then continue from step 8.
   - `publishing`: an earlier attempt may have published without recording it. Run step 2, then go to
     [Unrecorded attempt](#unrecorded-attempt).
2. **Check the account.** Run `claude auth status --json`. Continue only if `loggedIn` is `true` and `authMethod` is
   `claude.ai`. Otherwise ask the user to sign in with `/login` and stop without recording anything, so the entry
   keeps its state for the next attempt. Use `email` as the account.
3. **Choose the target.** If `url` is recorded, update that artifact. Otherwise, if `mode` is `update`, update
   `target_url`. Otherwise create a new artifact. For an update, read the target first with the Artifact tool
   (`action: "read"`). Continue only if the result says the artifact is owned by you or that you are a writer.
   Otherwise record `failed --reason "no writer access to the target"` and stop.
4. **Read the document.** Read the whole `$ENTRY/<document>` file before publishing it. Do not edit it.
5. **Record the attempt before publishing.** Run `record --entry "$ENTRY" --state publishing --account "<email>"`.
   If the script refuses, follow its message and do not publish.
6. **Publish the file as it is.** This skill asks for the Markdown file itself to be published as Markdown. Call the
   Artifact tool with `file_path` set to `$ENTRY/<document>`, the entry `title`, and the target `url` for an update.
   Meet any other requirement the tool states, such as loading a skill or giving an icon, without changing the file.
7. **Record the URL immediately.** Before anything else, run
   `record --entry "$ENTRY" --state published --url "<url>" --version "<version id>"` with the URL and the version id
   from the publish result. If the tool refused the publish before creating anything, for example by
   rejecting the file or because the user declined it, record `failed --reason "<error>"` and stop. If the outcome is
   unknown, such as a timeout, a dropped connection or an error that does not say nothing was created, do not record
   `failed`: leave the entry in `publishing` and go to [Unrecorded attempt](#unrecorded-attempt).
8. **Read it back.** Read the artifact with `action: "read"`. The service keeps the page it renders from the Markdown,
   not the Markdown itself, so the read cannot be compared with the document byte for byte. Record `verified` only if
   all of these hold, and otherwise record `failed --reason "<the check that failed>"`:
   - the read names the version id that the entry recorded in step 7;
   - it says the artifact is owned by you or that you are a writer;
   - the page `<title>` is the entry's `document` name;
   - `show --entry "$ENTRY"` still exits 0, so the file that was sent is the exported document.
9. **Report.** Give the user the entry, the URL, the final state and, on failure, the reason. Do not republish
   automatically.

## Unrecorded attempt

First settle the one artifact the earlier attempt could have published to.

- If `url` is recorded, it is `url`. Otherwise, if `mode` is `update`, it is `target_url`.
- Otherwise, list the user's artifacts (`action: "list"`). Look for ones with the entry title updated after the
  `publishing` time in the entry history.
  - If exactly one matches, it is that artifact.
  - If none or several match, show the user what you found and ask them to choose. They can pick a URL. They can
    instead have you record `failed`, which allows a new publish that may duplicate an artifact the earlier attempt
    made.

With the URL settled:

1. Record `published --url "<that url>"`.
2. Record `failed --reason "an earlier publish attempt was not recorded"`.
3. Continue from step 3.

That publishes the document again to the same artifact. No version id was recorded for the earlier attempt, so
step 8 could not check it. The new publish adds a version, not another artifact.

## Never

- Do not change, reformat or wrap the document. Publish exactly the exported bytes.
- Do not publish to a URL other than the one the entry records; the script refuses to record it.
- Do not share, pin or delete artifacts as part of this skill.
