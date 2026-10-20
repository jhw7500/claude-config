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
   - `publishing` with no `url`: an earlier attempt may have published without recording it. Go to
     [Unrecorded attempt](#unrecorded-attempt).
2. **Check the account.** Run `claude auth status --json`. Continue only if `loggedIn` is `true` and `authMethod` is
   `claude.ai`. Otherwise record `failed --reason "not signed in to claude.ai"` and stop. Use `email` as the account.
3. **Choose the target.** If `url` is recorded, update that artifact. Otherwise, if `mode` is `update`, update
   `target_url`. Otherwise create a new artifact. For an update, read the target first with the Artifact tool
   (`action: "read"`). Continue only if the result says the user is a writer. Otherwise record
   `failed --reason "no writer access to the target"` and stop.
4. **Read the document.** Read the whole `$ENTRY/<document>` file before publishing it. Do not edit it.
5. **Record the attempt before publishing.** Run `record --entry "$ENTRY" --state publishing --account "<email>"`.
   If the script refuses, follow its message and do not publish.
6. **Publish the file as it is.** This skill asks for the Markdown file itself to be published as Markdown. Call the
   Artifact tool with `file_path` set to `$ENTRY/<document>`, the entry `title`, and the target `url` for an update.
   Meet any other requirement the tool states, such as loading a skill or giving an icon, without changing the file.
7. **Record the URL immediately.** Before anything else, run `record --entry "$ENTRY" --state published --url "<url>"`
   with the URL from the publish result. If publishing failed, record `failed --reason "<error>"` and stop.
8. **Read it back.** Read the artifact with `action: "read"`. If the returned content equals the document byte for
   byte, record `verified`, adding `--version "<version>"` when the publish or read result names one. Otherwise
   record `failed --reason "read-back differs from the document"`.
9. **Report.** Give the user the entry, the URL, the final state and, on failure, the reason. Do not republish
   automatically.

## Unrecorded attempt

List the user's artifacts (`action: "list"`) and look for ones with the entry title updated after the `publishing`
time in the entry history. If exactly one matches, record `published --url "<its url>"` and continue from step 8.
Otherwise show the user what you found and ask them to choose: record a URL they pick, or record `failed`, which allows
a new publish that may duplicate an artifact the earlier attempt made.

## Never

- Do not change, reformat or wrap the document. Publish exactly the exported bytes.
- Do not publish to a URL other than the one the entry records; the script refuses to record it.
- Do not share, pin or delete artifacts as part of this skill.
