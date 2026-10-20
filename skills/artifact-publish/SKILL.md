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
   not the Markdown itself, so the read cannot be compared with the document byte for byte. Compare the text
   instead. The read result names the file that holds the full page.

   Record `verified --version "<version id from the read>"` only if all of these hold. Otherwise record
   `failed --reason "<the check that failed>"`.
   - The read names the version id that the entry recorded in step 7. If the entry has no recorded `version`
     (it was settled in [Unrecorded attempt](#unrecorded-attempt)), skip this check; the text check decides.
   - It says the artifact is owned by you or that you are a writer.
   - The page `<title>` is the entry's `document` name.
   - The text of the page's `<article>` element equals the document's text, comparing with whitespace collapsed.
     - Leave out the article's first label, `Markdown · <document>`.
     - The page drops the Markdown markup, such as heading marks, list markers, backticks, table pipes, table
       divider rows and code fences. Leave that markup out of the document's side too.
     - The page escapes characters such as `"` as `&quot;`. Compare them unescaped.
     - Every word must match, and neither side may have extra text.
   - `show --entry "$ENTRY"` still exits 0, so the exported document has not changed since step 1.

   These checks tie the page to this document's text. They do not prove the page holds the document byte for byte,
   which the read cannot show.
9. **Report.** Give the user the entry, the URL, the final state and, on failure, the reason. Do not republish
   automatically. Publish again only when the user asks. That request starts again from step 1, and from `failed`
   with a recorded URL it updates that artifact.

## Unrecorded attempt

First settle the one artifact the earlier attempt could have published to.

- If `url` is recorded, it is `url`. Otherwise, if `mode` is `update`, it is `target_url`. The entry itself names
  this artifact.
- Otherwise, list the user's artifacts (`action: "list"`). The list shows each artifact under its page title, which
  for a published Markdown file is the file name. So look for ones listed under the entry's `document` name and
  updated after the `publishing` time in the entry history.
  - A match is only a guess, so in every case show the user what you found. Tell them that the entry will be bound
    to the artifact they settle on, and that any later publish of this entry goes to it.
  - Then ask them to choose one of these:
    - They can confirm one artifact as the one the earlier attempt made. Settle on it.
    - They can pick another URL. Settle on it.
    - They can have you record `failed`. That allows a new publish, which may duplicate an artifact the earlier
      attempt made. Then stop.
    - If they choose none of these, stop without recording anything.

With the URL settled, record `published --url "<that url>"` and continue from step 8. The read shows whether that
artifact already holds this document. Nothing is published again here. If step 8 records `failed`, publishing again
needs the user's request, as step 9 says.

## Never

- Do not change, reformat or wrap the document. Publish exactly the exported bytes.
- Do not publish to a URL other than the one the entry records; the script refuses to record it.
- Do not share, pin or delete artifacts as part of this skill.
