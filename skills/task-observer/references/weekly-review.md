# Comprehensive Review (scheduled or fallback)

Cross-checks all unresolved observations against all skills, propagates
cross-cutting principles, and applies improvements that don't need user
input. Two modes:

- **Scheduled autonomous review (preferred):** a recurring task (e.g.
  Mon/Wed/Fri mornings) via the platform's scheduler. Runs without the user
  present and applies non-escalated observations autonomously.
- **In-session 7-day fallback:** pending at session start when BOTH are
  true: no scheduled review is registered (or none succeeded in 7+ days),
  AND neither `skill-observations/last-review-date.txt` nor a verified
  source-specific scheduled REVIEW.md receipt establishes a review within
  7 days (a missing marker is recreated with `never` — see Session Start
  steps 1 and 3). A source absent from or skipped in a report is not reviewed.
  In an interactive session a pending
  fallback surfaces as a one-line offer and runs only if the user opts in
  (SKILL.md, Session Start step 3) — it never gates the user's task.

**Reachability — where does scheduled work actually run?** Scheduled mode
requires the scheduling agent's execution environment to read and write
the workspace folder. Persistence and execution context are independent
axes: knowing where the state lives is not enough — check whether the
scheduler runs somewhere that can reach it. Three regimes:

1. **Shared filesystem** (e.g. Cowork's mounted folder): scheduled mode
   works as described.
2. **Local-only filesystem with a cloud scheduler** (e.g. remote routines
   that run on hosted infrastructure): scheduled mode is physically broken
   — the remote agent cannot read `skill-observations/` or stage updates
   to `skill-updates/`. Do not register a routine. Recommend a recurring
   calendar reminder plus a manual "run the skill review" trigger in a
   local session, or syncing the observation log to storage the scheduler
   can reach (e.g. a git repository it can clone).
3. **Local-only filesystem with a local scheduler** (cron, Task Scheduler,
   a terminal-resident loop): works, but the user must keep the local
   agent runnable.

## Approval policy

**Interactive (user present):** always present observations grouped by
skill (number, title, one-sentence summary), flag judgment calls as "needs
your input", and wait for blanket or selective approval before applying.

**Scheduled autonomous (user absent):** apply non-escalated observations by
default — safety comes from the staging-plus-review pattern (nothing is
live until the user installs it). **Escalate without applying** when: (1)
the observation proposes a NEW skill (naming/scope/type/licence need the
user); (2) it removes or substantially restructures existing content; (3)
it self-flags uncertainty ("not sure if…", "worth discussing…"); (4) two
observations conflict. A scheduled run should still apply every
non-escalated item — a review that applies nothing is just a report
generator.

## Steps

**Step 0 — recommend scheduled setup (fallback mode only).** Ordering
guard: run Step 1's no-observations short-circuit FIRST — if there are no
unresolved observations and no outstanding principles, skip Step 0 entirely and
just update the timestamp. A brand-new install must never get a setup
prompt before it has done any work. Otherwise: check
`skill-observations/scheduled-review-decline.txt`: if under 30 days old and
the fallback isn't firing repeatedly, skip. Check for a registered
scheduled task (scheduler presence or
`skill-observations/scheduler-registered.txt`); if found, skip. Before
offering, check reachability (see the regimes above): if the platform's
scheduler runs where it cannot reach the workspace folder (regime 2), do
NOT offer registration — recommend the calendar-reminder-plus-manual-
trigger pattern instead, and skip the rest of this step. Otherwise
offer to set one up. Yes → register via the platform scheduler (Cowork:
`create-shortcut` / `set_scheduled_task`; terminal: cron), name it
`weekly-skill-review`, use the draft prompt at
`skill-observations/scheduled-task-draft.md` if present, then verify the
registration actually succeeded (the scheduler lists the task, or the
platform confirmed creation) BEFORE writing today's date to
`scheduler-registered.txt`. If registration fails or can't be verified, do
NOT write the marker — the marker would permanently suppress the fallback
while no review ever runs. Tell the user registration failed and leave the
fallback active. No → write today's date to
`scheduled-review-decline.txt` (suppresses for 30 days; repeated fallback
firings within the window re-surface the offer). No scheduler available in
this environment → skip silently.

**Step 1 — load.** Archive entries resolved in *previous* sessions (see
Archival on Write in SKILL.md). Read the observation log.

Build the work queue from the structural identifiers, not from a status
filter. The unresolved set includes OPEN, STAGED, missing, blank, and
unrecognised statuses; only ACTIONED and DECLINED are resolved. Concretely:

1. Enumerate all `### Observation N:` headers first — this is the
   authoritative list of entries in the log.
2. For each header, classify the entry's status by looking for a
   `**Status:**` line within its body. Treat a missing, blank, or any
   non-ACTIONED / non-DECLINED status as OPEN.
3. A `STAGED` entry is unresolved. Check its referenced bundle against the
   current live skill and source observation. If it is still valid, report it
   as pending installation without generating another copy. If either changed,
   reassess the delta from the current live file. Never infer installation from
   a previous review's completion message or an `ACTIONED` label alone.
4. Never derive the work queue from a `grep '**Status:** OPEN'` alone.
   Derive it from the header list minus the resolved (ACTIONED /
   DECLINED) entries. A grep on an optional field silently drops every
   entry missing that field — the review then confidently reports a
   clean log while a backlog of untriaged observations is skipped.

**Legacy staging reconciliation:** if an old ACTIONED entry has a staging-only
report/bundle and the intended delta is absent from the current live skill,
include it as installation-pending in this review. Record the contradictory
source status explicitly; do not silently treat the label as installation proof.
In multi-source mode, correct the review's classification, not the raw log.

**Reconciliation guard:** before proceeding, assert that
`count(### Observation headers) == count(status-classified entries)`.
If the counts differ, the delta is statusless entries — surface and
triage them (as OPEN) rather than proceeding as if the log were clean.

Also read all active cross-cutting principles. If there are no unresolved
observations and no outstanding principles: report "no open observations
or outstanding principles", update the timestamp, and stop.

**Step 2 — inventory skills.** List all skills (system prompt
`<available_skills>` or the skills directory). Only user-owned custom
skills can be updated. Known read-only system skills: docx, pdf, xlsx,
pptx, skill-creator, schedule (grow this list when an update fails for
permissions). Observations targeting a system skill are NOT skipped — route
them to a complementary user-owned `{system-skill}-extras` skill containing
only the delta, creating it if needed and noting the pairing in
configuration.

**Step 3 — cross-check observations.** Evaluate every unresolved observation
against every skill — not just the skill named in its header; Principles
often generalise. Build skill → [relevant observations]. Interactive:
present all of it and await approval. Autonomous: apply the approval policy
above and continue.

**Step 4 — cross-check principles.** Flag every skill that doesn't yet
comply with each active cross-cutting principle.

**Step 5 — apply.** For each skill with approved/non-escalated items,
produce an updated SKILL.md: integrate insights into the sections where
they belong (never append an observations list at the bottom); preserve
structure, voice, and attribution; place new rules where they logically
live. Follow the editing rules in `references/skill-authoring.md` (live
file as base, staging, diff-before-overwrite).

**Step 6 — mark STAGED.** After validating the delivered bundle, record:
`STAGED (YYYY-MM-DD) — <bundle path>; installation pending`.
The scheduled review creates drafts, so it reports zero newly installed
observations. `ACTIONED (YYYY-MM-DD)` is reserved for a separate authorized
installation followed by read-back verification of the live skill. STAGED
entries remain unresolved and are not archived. When source logs are read-only,
record this status in the review report alongside the source path and literal
observation ID; keep the source entry OPEN and state that it is unchanged.

**Step 7 — timestamp.** Write today's date to
`skill-observations/last-review-date.txt` for a writable single-source review.
For a read-only multi-source review, use the report rules below instead.

**Step 8 — deliver and summarise.** Stage updated skills (see Delivery
below), then present:

```
## Weekly Skill Review Complete — [date]

Staged skills ([N] observations prepared, installation pending):

**[skill-name]** — [1-sentence change summary]; observations #[N], #[N]

### Observations Staged
[numbers and titles]

### Installed and verified
0 in this scheduled staging run

### Skipped (needs manual review)
[items with reasons]
```

In interactive mode, wait for the user to acknowledge before other work.
Scheduled mode delivers the report and exits; it does not wait for an absent user.

## Read-only multi-source review

When the launcher supplies several source workspaces, deduplicate by canonical
path and key every observation by `(source log path, literal observation ID)`.
The same number in two projects is not the same observation. Read every active
source and its principles. Never move, archive, rewrite, or timestamp these raw
logs: another session may be appending, and sensitive-directory writes may be
blocked. This overrides source mutations in Steps 1, 6, and 7.

Use the launcher's unique output directory for full skill bundles and REVIEW.md.
Before preparing updates, consult previous reports for still-pending bundles
and compare them with the current live skill and observation. Record each source's
header count, classified count, review time, read failures/skips, and pending
bundle references. Record STAGED statuses in the report only; explicitly state
that the original OPEN statuses and per-source timestamps remain unchanged.
An unreadable source is an incomplete review, never an empty or clean queue.

## Constraints

- Don't modify observation entries beyond their status field.
- Don't create new skills in a review — note candidates for the user to
  action via the skill-creator.
- Unsure how to integrate an observation → skip it and say so in the
  summary.
- Treat internal observations with the same rigour as open-source.

## Delivering updated skills

Save each updated skill to
`[workspace folder]/skill-updates/[date]/[skill-name]/` — the FULL skill
directory (SKILL.md plus references/, scripts/, assets/ where present),
never SKILL.md alone — and present it for review and installation. In
Cowork: via `present_files` and its upload button. In environments without
a presentation tool (e.g. Claude Code CLI): report the staged path and a
change summary in chat and let the user review and install from there.
Never write to the live skill directly, even where the skills directory is
writable — staging-only is a deliberate safety property of the review loop
(nothing goes live without the user's sign-off), not a filesystem
constraint. For any skill with
supporting files, zip the staged directory into a `.skill` bundle and
present the bundle; a bare SKILL.md install silently truncates a
multi-file skill. Pre-delivery gate (two items, run as the last step
before presenting): (1) grep the staged SKILL.md body for `references/`,
`scripts/`, `assets/` paths and fail the delivery if any referenced file
is missing from the staged set; (2) for multi-file skills, fail the
delivery if the artefact being presented is bare file links rather than
the `.skill` bundle. Sweep build artefacts (`__pycache__/`, `*.pyc`,
`.DS_Store`, `.~lock.*`) before zipping and read the archive listing back
after. When seeding staged
copies from the read-only mount, `chmod -R u+w` the staged path first —
the mount's read-only mode travels with the copy, for directories as
well as files. Do not edit skill files in place — nothing goes live
until the user installs it. **Keep-two rule:** retain every pending bundle,
regardless of age. Among bundles confirmed installed or explicitly declined,
keep the two most recent per skill; older resolved bundles are cleanup
candidates. Never delete an unresolved bundle to meet a retention count.
