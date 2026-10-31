---
name: superpowers-extras
description: Use together with any superpowers process skill — brainstorming, test-driven-development, verification-before-completion, systematic-debugging, requesting-code-review, receiving-code-review, subagent-driven-development, dispatching-parallel-agents, writing-plans, using-git-worktrees. Carries later corrections to those skills' rules that the plugin-installed copies do not have. Read this whenever one of those ten is loaded, before acting on its rules.
---

# superpowers — later corrections

The skills below ship inside the `superpowers` plugin, so their files
live in a versioned plugin cache and any edit there is erased by the next
plugin update. This skill therefore carries **only the delta** — the rules
added or amended after review of real sessions. The plugin skills remain the
base; read them first, then apply what follows.

Naming note: a user skill with the SAME name does **not** shadow a plugin
skill — both stay in the list side by side (verified 2026-09-01). That is why
this is a separate `-extras` skill rather than a copy of each original.

Provenance: observations #1–#5 in the task-observer log, actioned 2026-09-01;
observations #7–#15, #17, #20, #21, #28–#30, #35–#37, #39, staged 2026-09-07
and revised 2026-09-10. Staging is not evidence of live installation.
Base skills are from the `superpowers` plugin (v6.3.0). The
subagent-driven-development section comes from claude-config issue #205
(2026-10-13) and quotes plugin v7.0.0. The deltas added for claude-config
issue #215 (2026-10-19) were written against plugin v7.0.0.
Separately, additions from the multi-source observation reviews of 2026-10-19
(never installed, re-applied here), 2026-10-26 (re-applied here) and 2026-10-31
are keyed by source workspace and observation number in each run's report
(`skill-observations/skill-updates/<run>/REVIEW.md` in the task-observer
workspace). Where one of their headings names a base section, it
is a plugin v7.0.x section name; where v7.0.0 and v7.0.1 differ, both are
given. The older brainstorming amendments below still cite v6.3.0 text that
v7.0.x no longer contains and need re-mapping.

These deltas are maintained **locally only** — they are not contributed
upstream, by decision. This skill is the canonical home for them: when a
plugin update replaces the base skills, the deltas here survive and stay
applicable as long as the sections they reference still exist. If a base
skill is restructured enough that a delta no longer maps onto it, fix the
delta here rather than editing the plugin cache.

---

## superpowers:brainstorming

### ⚠ Amends an existing rule — "The Process" checklist

The plugin copy says:

> - Only one question per message - if a topic needs more exploration, break it into multiple questions

**Read it as this instead:**

- **Dependent questions go one per message.** If a later question's framing or
  option set would change depending on an earlier answer, it MUST wait for that
  answer. This is what the rule protects: never ask a question whose premise is
  not yet established, and never stack such a chain into a single message.
- **Independent axes may share one message** when the harness offers a
  structured multi-question prompt. The test is mechanical: can you write out
  each axis's full option set without knowing any other axis's answer? If yes
  they are independent, and serialising them costs your human partner round
  trips for no comprehension benefit. If you cannot tell, split — the rule is a
  dependency constraint, not a licence to batch.
- If a topic needs more exploration, break it into multiple questions.

The same amendment applies wherever the plugin copy says "one at a time" in
its numbered steps: it means *dependent* ones one at a time.

### Adds to "Exploring approaches"

- **Cost an integration point by what it can reach, not by where it sits.**
  Before quoting the size of any approach that hooks into existing code ("just
  patch line N", "a one-line change here"), state the execution context at that
  line: privileges held, working namespace (chroot/jail/container), sandbox or
  seccomp state, and whether the resources the hook actually needs are reachable
  from there. Locating where to insert code is not the same as establishing what
  that code will be able to do. A size quoted before that check can be off by an
  order of magnitude — and every trade-off you weighed against it silently
  inherits the error.

### Adds to Red Flags

| Thought | Reality |
|---------|---------|
| "I found the exact line — it's a one-line patch" | A location is not a cost. Check the privileges, namespace and sandbox state at that line before quoting a size. |
| "These questions are related, so I'll ask them one at a time" | Related ≠ dependent. Serialise only when one answer changes another's option set. |

### Adds to "How it gets made" (plugin v7.0.x)

- **"Shared X" names a goal, not a mechanism.** Before you propose any way to
  share, sync or replicate state between machines, projects or people, settle
  two framing questions first, one per message as plain questions: cadence and
  unit. (The ranges — continuous, scheduled or only on request; everything, per
  project or per item — are for your own framing, not a menu to show.) The
  mechanism — a mount, a sync daemon, a script run by hand — usually follows
  from those two answers.
  Designing the mechanism first anchors the conversation on its failure modes,
  and the correction then arrives after the design is already written.

### Adds to "Offer recon" (plugin v7.0.x)

- **A risk about an input that already exists is a lookup, not a mitigation.**
  When a design says "we could not obtain the real file, config or sample, so a
  strict parser might reject it" and proposes a fallback, first check whether
  the real artifact can be read now through access you already have, and offer
  that lookup to your human partner. A mitigation designed around a fact you
  could have looked up only makes the failure safe; it does not make the change
  work. Stay inside existing access — this does not
  authorise new credentials or wider reads.
- **For an environment split or replication, recon has a data-flow axis.**
  Beside assets, services and secrets, list for each scheduled job and daemon
  what it reads (paths, hosts, databases) and whether any of those inputs would
  now be produced on the other machine. Also look for existing multi-host hooks
  (remote host lists, ssh or rsync to named hosts): they reveal earlier attempts
  and dead configuration. Consumers of data that will now be produced elsewhere
  are what silently breaks after a split, and size or ownership listings do not
  show them.

### Adds to "Lay out the approaches" (plugin v7.0.1; "Think wide privately" in v7.0.0)

- **Replacing a tool means matching what it actually does, not what its config
  says.** When an approach replaces or re-implements an existing tool, cost it
  against the tool's effective behaviour at the version actually in use —
  inherited presets, default ignore lists, implicit grammar — not against the
  local config file, which shows only the overrides. If the effective set is
  not already readable in the project, offer the lookup (the tool's published
  source or package) as recon; your human partner decides. Without it, say the
  equivalence is unverified. An approach costed from the visible config
  silently narrows behaviour and is called "small" for the wrong reason.
- **Cost removing an exemption by removing it once.** An exemption or a loose
  admission rule accumulates dependents that production code does not show:
  test fixtures and harness scripts written while it existed assume it, so a
  search of production paths undercounts the scope. Search the whole
  repository for the condition, not only the package directory. The honest
  estimate is to apply the one-line change to a throwaway copy, run the full
  suite once, and count the failures and distinct call sites — that runs code,
  so offer it as a spike; your human partner decides. Without it, quote the
  scope as unmeasured.

---

## superpowers:test-driven-development

### Adds to Verify RED

**Read both arms.** A red/green pair is a two-sided instrument, not a
one-sided gate. When you run a control arm (feature present) beside a mutation
arm (feature removed or reverted), an unexpected result in EITHER arm is a
finding:

- **Mutation arm green for a claimed change detector** → this indicts your
  harness, not the subject. The test cannot detect the change it claims to test.
- **Control arm red while the mutation arm behaves as expected** → this is
  evidence about your harness or your usage of the API before it is evidence
  about the subject. Diagnose the harness before you conclude "this can't be
  done" and abandon the approach — an unexpected control failure has wrongly
  killed workable designs.

Reading only the arm you expected to pass discards half the instrument's
diagnostic value.

**Name the test's claim before interpreting its arms.** A change detector
must distinguish old from new behavior. A preservation guard checks an
invariant that should hold in both; keep useful guards and report their count
separately, not as evidence that the new behavior was detected. A suite with
only preservation guards has not yet demonstrated the change.

**Probing a third-party extension point?** Exercise every declared variant of
it — the argument-taking form and the no-argument form, each registration shape
the API documents. Per-variant dispatch contracts are where undocumented arity
and signature traps live: one variant works while another drops the connection
or throws from inside the library. A probe that tries a single variant reports
the library's capability wrongly, in either direction.

**A test that passes on its first run has two possible meanings** — the
implementation is already correct, or the harness never established the
condition the test needs. You cannot tell them apart without measuring the
precondition. So before you conclude "already implemented", assert that the
setup actually holds: the file is in the state the test assumes, the process is
in the namespace, the resource is absent. Probabilistic preconditions (a PID
being free, a port being unbound, a cache being cold) must be *forced*, not
hoped for — a build host with hundreds of processes will accidentally satisfy a
condition that the real target, with a handful, never does. Where the condition
cannot be forced in this environment, exit with a distinct **skip** status; do
not let it report as a pass.

**Choose the mutation at the contract boundary the assertion names.** When a
characterization test cannot be made red by reverting (there is no production
change to revert), mutate the function whose return value the test actually
observes — not one internal guard on the way there. Redundant defenses make a
shallow mutation a false negative in both directions: the assertion stays green,
and you conclude either "the test is vacuous" (wrong) or "verified" (worse). If
a contract-level mutation stays green, look for a second independent guard
before declaring the test worthless.

**A control built from symlinks is not isolated.** When the new test must run
against the old code in a separate control directory (so that the shared
worktree is never reverted), a control whose subtrees are symlinks back to the
real tree defeats itself as soon as the harness locates its subject through a
canonicalised path (`resolve()`, `realpath`, `readlink -f` on its own file):
the control computes the real root, exercises the new code, and its pass reads
as "the old code already satisfied the test". Make the control a physical copy
of every path the test uses to find its subject. Before trusting a control run,
print the root the harness actually resolved and assert it lies inside the
control directory.

**Replacing an interpreter of input? Compare values, not verdicts.** When a
parser, serializer or format converter is replaced, run the old and the new
implementation over the same fixtures and compare their outputs. Both can
return success while producing different values — the old one may quietly
return a neighbouring section's value — so a pass/fail oracle hides exactly the
defect that matters. Keep the old implementation reachable by the test (a build
of the previous revision, or a pinned test-side copy) until the comparison is
done; whether it also stays in the production build is a separate decision.

### Adds to GREEN — minimal code

**Editing a conditional chain is a structural change, not a text change.**
After splicing a new branch into an `if / else if / else` chain, diff the *set
of conditions* before and after: every original predicate must still be present,
in the same order. A search-and-replace that rewrites a chain element can
replace a guard instead of preceding it — an auth or validation check
disappears, the suite stays green, and the new tests pin only the new behaviour.
New tests prove the new behaviour; only a pre-existing negative test proves you
did not remove an old guard. Before writing code into a guarded path, locate the
test that pins the guard — and if there is none, write it first, so its
accidental removal turns red.

### Adds to test construction

**Do not build a substring assertion out of values that can contain each
other.** When a test redirects a hard-coded production path to a temporary one
and then asserts `original_path not in result`, giving the temporary path the
same directory shape as the real one (`{tmp}/etc/ssh/...`) makes the result
*contain the original as a suffix* — the assertion fails while the substitution
was perfectly correct. Choose deliberately dissimilar names for redirected
targets, and leave a comment saying why: "make it look like the real path" is an
easy and plausible-sounding regression. More generally, before asserting
anything by containment, establish that the compared values are not prefixes or
suffixes of one another.

**Split an invariant's tests by the argument's three states.** For a rule such
as "this optional add-on never disturbs the main operation", test the argument
(1) absent, (2) present and valid, and (3) present but invalid — empty string,
wrong type, out of range. Invariants break most often in the third state, and
the absent case never reaches it. Check separately whether a layer that runs
*before* the main code (an argument parser's type conversion, schema
validation, a decorator) rejects the invalid value first: that layer turns a
fail-open add-on into one that stops the main operation. If a contract says to
always pass the argument while another passage says the value can be missing,
that combination is a third-state test case.

**A fake carries only your belief about the real tool.** Code that depends on
the output shape of an external tool (a container engine's rendered config, a
CLI's JSON, a parser's tree) can pass every fake-based test and still fail on
the first real input, because the fake reproduces what the implementer believes
the tool prints. Capture the real output once through a call that changes
nothing (render, parse, config dump, help text, dry run), pin it as a fixture,
and record the capture command and tool version beside it. Check a reviewer's
proposed flag or fix against the same capture before adopting it.

---

## superpowers:verification-before-completion

### Adds to The Gate Function — bind the run to its inputs

For a baseline or completion test, keep a run record: **input identity, absolute
cwd, command, expected scope/count, actual passed/failed/skipped counts, target
exit status, raw-log location, and claim limits**. Use the runner's structured
report or actual assertions, not a count of arbitrary PASS strings; a tool
without test counts needs its own success evidence.

Before launch, identify what the command actually reads: a literal revision
plus relevant dirty source, fixtures, and configuration digests. A commit SHA
alone does not describe uncommitted inputs. Record only relevant, non-secret
identity evidence, not environment or credential dumps.

While the run is active, do not edit its inputs. Finish it before editing, or
take a quiescent snapshot into a separate checkout/copy and keep writers away
from that snapshot before continuing edits elsewhere. For a pre-change
baseline, pin the pre-change inputs, not the edited candidate. A worktree or
commit name does not itself prevent writes. An observed input change
invalidates baseline/regression attribution; equal start/end digests do not
exclude an intervening change and restore.

Gate directory entry before the runner. Capture the target process's exit
status before formatting output; retain the raw log and read the actual
summary as well. A pipeline-wide status is not necessarily the target status.
The existing Instrument Check below governs skipped or unexecuted checks.

**The outer channel reports the wrapper, not the target.** A background job
wrapped as `{ cmd; echo "EXIT=$?"; } > log` exits 0 whatever `cmd` returned, so
the harness's completion notice says "exit code 0" while the log holds
`10 failed` and `EXIT=1`. Any layer inserted between a check and its reporting
channel can replace the check's status with its own (the Instrument Check's
wrapper bullet below says the same of wait and watch commands). End such a
wrapper with `rc=$?; echo "EXIT=$rc"; exit "$rc"` so both channels agree, and
before treating a background run as passed, open the log and read its recorded
`EXIT=` line and summary — never the outer notice alone. This check cannot
protect you when the log was written by a different invocation than the one
whose status you read, or when the wrapper's own redirection failed: print the
log path and the target's status on one line outside the redirection, as the
capture example below does, and confirm the log's timestamp belongs to this run.

**A cached build makes a repeated run measure less.** Diagnostics such as
compiler warnings are emitted only for units that are actually compiled, so the
second run of an incremental build reports zero warnings for a tree that has
several. To compare diagnostics before and after a change, take the baseline
from a clean build of the pre-change revision in a separate detached checkout,
not from a re-run. Results a runner caches are silenced the same way — some
test runners replay or skip unchanged tests and report them as cached or
up to date — so for test counts too, confirm from the log that the tests
actually executed in this run. A separate baseline checkout also keeps the rule
above against editing a running input.

### Bash capture example

Set `verification_cwd` to the intended absolute isolated checkout and
`verification_log` to a fresh private log file's absolute path. Substitute the
project's real test command. This captures execution, not a semantic PASS
verdict; inspect the full log and expected/actual checks afterward.
Keep the log outside runner-managed cleanup paths. Log validation or readback
failure returns 125 with `capture_error`; `verifier_exit` still reports the
target status. Otherwise the wrapper returns the target status. These checks
detect missing/invalid logs, not same-user tampering or in-place truncation.

```bash
(
    cd -- "$verification_cwd" || exit
    export -n verification_log
    if bash tests/check.sh >"$verification_log" 2>&1; then
        verification_rc=0
    else
        verification_rc=$?
    fi
    printf 'log=%s verifier_exit=%s\n' "$verification_log" "$verification_rc"
    if [[ ! -f "$verification_log" || -L "$verification_log" ||
          ! -O "$verification_log" || ! -r "$verification_log" ]] ||
       ! tail -n 30 -- "$verification_log"; then
        printf 'capture_error=raw_log_unavailable verifier_exit=%s\n' "$verification_rc" >&2
        exit 125
    fi
    exit "$verification_rc"
)
```

`cd target && setup; test` does not gate the test: the semicolon allows it to
run from the old directory after a failed `cd`. Keep the exit guard even when
the happy-path directory exists. This procedure grants no permission to
modify a shared checkout, reset production, install, or publish.

### New section — The Instrument Check

A "good" result — pass, clean, blocked, absent, none found — is evidence only
once you know the instrument **could have produced the opposite result**. If it
could not, what you have is not evidence; it is a verification gap in the shape
of one.

Ask before reporting it:

- **Negative assertions** ("blocked", "rejected", "no leak") need a positive
  control — see the pattern below.
- **Revert/mutation pairs** are two-sided. Read both arms: a claimed change
  detector that stays green on mutation leaves that change unproved. Report
  preservation guards separately; their pass in both arms is expected.
- **Absence** is scoped to the channels you actually searched. Enumerate where
  the system persists output (logs, build artifacts, check-run output, API,
  attachments) before promoting "I did not find it" to "it does not exist" — a
  wrong absence claim also spawns work to fix a defect that isn't there.
- **A path-scoped predicate answers only for the exact path you gave it.**
  `git check-ignore`, `git check-attr`, permission checks and path-matching
  linters report on their argument, not on what lies beneath it. A negative
  answer for an ancestor directory is not a negative answer for its contents:
  a rule in a nested `.gitignore` can ignore every file below while the
  ancestor itself is reported as not ignored. Query the exact path you are
  about to rely on; when a directory's contents are the subject, enumerate the
  concrete paths (the tool's `--stdin` form takes a list where it has one).
- **Multi-consumer artifacts** (specs, schemas, config, interface definitions)
  verify against the STRICTEST consumer. A lenient parser hides defects while
  passing them: passes accumulate, confidence grows, and the gap grows with it.
  Pick the tool that rejects best, not the one that passes easiest — and if only
  a lenient one exists, say so when you report.
- **A check that was skipped reports the same colour as a check that passed.**
  Before citing CI — or any gate — as evidence that something builds or passes,
  read what the job actually *executed*. A build job that skips on an
  architecture mismatch and still exits 0 is reporting "I did not try", not "it
  builds"; the real failure then rides through merge after merge with nothing
  anywhere showing red. "The pipeline is green" supports a completion claim only
  once you have seen the step run.
- **Silence is the failure mode of a broken instrument, not a neutral result.**
  A filter, poller or query that is misconfigured produces exactly the output of
  a correct one that found nothing. Before you read "no matches yet" as
  information about the world, confirm the instrument can currently produce a
  match — see "Watchers, pollers and filters" below.
- **A wrapper's exit status is not its subject's status.** A wait or watch
  command that exits 0 may mean "I finished watching" or "the gating subset
  passed", not "every item passed" — its own printed table can list failures in
  the same output. After any wait/watch, re-read the per-item states and assert
  that every item passed, or was skipped for a reason you have confirmed (see
  the skipped-check bullet above — a skip is not a pass). When an
  item is not green, name its cause only from the most authoritative channel:
  structured run/job state over the rendered summary (`cancelled` is not
  `failed`), stored artifacts over log text.
- **A derived copy cannot show what the original changed.** A diff between
  translations, summaries or converted copies of a document does not establish
  what changed in the canonical source — a change can sit in the original's body
  and appear only as a revision-history line in the translation. Declare the
  canonical version first, diff canonical against canonical, use derived diffs
  as hints, and verify each revision-history item in the canonical body.
  Without the canonical previous version, say so instead of stating "only N
  changes".
- **Text extracted from a document is one channel, not the document.** Text in
  figures, drawings and scanned regions is often absent from the text layer, and
  extraction order is not spatial layout. A claim about what a figure shows — a
  value inside a drawing, which arrow a label sits next to — needs a rendered
  view of that page: render the single page to an image and look. A search miss
  in the text layer means "not in this channel".
- **Identifiers in a generated artifact need an existence oracle.** Rendering,
  link and internal-consistency checks all pass for a version, tag, issue or PR
  number, or path that does not exist. Check such identifiers against a source
  that says whether they exist (a changelog section, the tag list, the issue
  tracker, the filesystem). When the data is layered — a preserved base plus
  newer augmentation layers — also diff whether a newer layer reverses an
  explicit decision recorded in the base; the newest layer is not automatically
  the most accurate.
- **A query you just wrote is part of the instrument.** This extends the
  "Silence" bullet above from monitoring loops to one-off queries. Before you
  read an empty or negative result from a new filter, extraction or status
  query, run it once against an item you already know matches — a run whose
  outcome you have seen, a comment you know is present — and keep that item in
  the query as a standing control; matching the wrong field (the message, when
  the marker lives in the title) otherwise returns a clean-looking "nothing".
  Where the query decides "finished", also run it against an item you know is
  still in progress, which must not pass: a bot's in-progress status comment
  counted as a finished review is a false "done" that a positive control cannot
  catch. A summary that collapses rows by name — one row per check across
  reruns — answers a different question from "what did this run do"; read the
  specific run.
- **A comparison rule needs near-miss negatives.** Before a rule that decides
  "this is the same document or output" goes into a skill, a test or a commit,
  cross-check it two by two: two known-matching pairs must pass, and two
  crossed pairs that differ by a small edit must fail. A containment check
  passes a version that only adds text — an old sentence can be a substring of
  its revision — so prefer equality of normalized content when sameness is the
  goal. A checker that fails its own matching pairs is measuring its
  normalization, not the target. (The containment rule under
  test-driven-development is the same trap inside a test.)

### Adds to Key Patterns — negative results

**Negative results ("blocked", "rejected", "not reachable"):**
```
✅ Same probe lands against the unhardened variant → THEN assert it is blocked here
❌ "The probe came back rejected, so the defense works"
```
Without a positive control, "blocked" and "never arrived" are
indistinguishable — a malformed payload gets credited to a defense that was
never exercised. Pair every "blocked" claim with either the same payload
succeeding somewhere, or proof it reaches the code path at all.

### Adds to Key Patterns — mutation loops

A mutation step is two operations, and only the first can fail quietly. Split
them and gate the second:

1. **Inject** — apply the mutation.
2. **Assert the injection** — the file/state actually changed, *and* changed in
   the intended place.
3. **Measure** — only now run the check and read its result.

Both halves of step 2 are load-bearing, because there are two distinct ways to
end up measuring nothing:

- **No-op injection.** The edit never landed — a shell substitution died on a
  delimiter collision, a pattern did not match, a write went to the wrong path.
  The subsequent "unchanged" reading is a fact about your tooling, not about the
  subject. Use a substitution tool with no delimiter ambiguity (a real string
  `replace` in a scripting language) rather than a one-line shell substitution,
  and treat a failed injection assertion as an **error**, never as a pass.
- **Wrong-target injection.** The edit landed, the "something changed"
  assertion passed, and it changed a different line — a near-identical sibling
  earlier in the file, matched by a first-occurrence replace. Assert in both
  directions: the intended site now has its new form, *and* the structurally
  similar neighbours are untouched (compare their count and content before and
  after). Where several similar lines exist, anchor the pattern on a value
  unique to the target instead of relying on first-occurrence.

Two more ways to measure nothing sit between steps 2 and 3: the mutation is in
place, but the measurement does not run it.

- **Unreached injection.** The edit landed at the intended site and the
  assertion passed, but the test never executes that site — its fixture takes
  another branch. A surviving mutant then looks exactly like "the test misses
  the defect". Before reporting a survivor as a test gap, show that the site
  runs under that test: in the same isolated copy the mutation runs in (never a
  shared or deployed tree), put a temporary failure (a raise or abort) at the
  site, confirm the test blows up, then restore the site.
- **Reverted injection.** The mutation was in place once, but a later step
  restored the file — typically to prepare the next mutation in the same
  scratch copy — and a re-run then measures the unmutated code. A green result
  looks exactly like a mutation the tests cannot catch. Re-apply each mutation
  in the same command that measures it, and print the mutated line (or a digest
  of the mutated file) in the same output as the test result.

Knowing this rule is not the same as running it: this trap has recurred in
projects that had already written it down.

**Restore so that the build sees the restore.** A restore that brings back the
original's old modification time (copying with preserved timestamps, some
archive extracts) lets a timestamp-based incremental build treat the object
built from the mutation as newer than the restored source, so the mutation
stays in the binary that later tests run. Restore in a way that updates the
timestamp, and in an incremental build confirm the outputs were rebuilt after
the restore (or build clean). A matching source digest proves the input only,
not what the measurement executes.

**Applies to any scripted mutation of a structured document, too.** After a
script edits Markdown, HTML, JSON or config, verifying that the new text is
present does not verify the edit. Assert the whole-file invariants the mutation
must *not* change — element/section/heading counts, tag balance, absence of
escape artefacts such as a literal `\1` from a mishandled backreference — and
render or re-parse the artifact once after the session's first scripted edit.
"New content is present" and "nothing that should be unchanged changed" are
different claims; only the second one catches a structural break, which
otherwise ships repeatedly because every content-level check keeps passing.

**An injection guard that fails silently costs a round trip per miss.** A bare
`assert s.count(old) == 1` stops a bad write — good — but prints only
`AssertionError`, so you cannot tell 0 from 3, or which bytes differed, without
another run. Make every injection assertion print what it measured and a short
excerpt (`c = s.count(old); assert c == 1, (c, old[:60])`), and when replacing
a long block, anchor on its boundaries (the function header and the first line
after the body) instead of reproducing the body verbatim from memory — that is
where the miscounts come from. Size the failure output of the tests you write
around the mutation the same way: a failing `assertRegex` or `assertIn` embeds
its whole subject in the message, so against a large text one miss floods the
tool result and the harness truncates it. Assert
`re.search(pattern, text, re.M)` with a
short message instead, and remember that `^` never matches past the first line
without `re.M`. The failure output is part of the instrument: size it to the
channel that has to carry it.

**A scripted edit across several files is only as atomic as its write order.**
Resolve and assert every anchor in every file before writing any of them, and
prefer exact strings read from the file over regexes for anchors that may wrap
across lines or nest. Have the script print one `written: <path>` line per file
it writes. When a run aborts midway, the first diagnostic is the list of files
already written — not the anchor that failed — because the tree may now be
partially edited.

### Adds to Key Patterns — cause, effect and timing

**Post-state is not evidence of causation.** "I ran the command and then read
the expected value" does not establish that the command produced it — another
path may have set the same field first, and the command may be a no-op that
returns success. To claim an effect, show the state that *would have been
different*: put the system into a state that differs from the expected one (or
find it there), run the target operation alone, and compare before and after.
Running against a system already in the expected state cannot distinguish
success from no-op, and a diagnosis built on that reading will survive review
and merge before anything contradicts it.

**Then wait for the consumer, not for the command.** After reverting or
changing a setting, the evidence is not the file's contents — it is the
consumer having acted on it again. A daemon in a settle-sleep or backoff window
will still report the old behaviour seconds after a successful write. Poll
until the observable condition changes, or wait past the consumer's known tick
and backoff ceiling; a single read taken sooner reads as a failed revert.

**"Applied" is a statement about one instant.** A value written to a shared
control surface — sysfs knobs, cgroup limits, sysctl, firewall rules, scheduler
settings — stays applied only while every other writer agrees. Before claiming
it holds, read it back now; enumerate the other writers of that surface
(daemons, hooks, timers, configuration that names the knob) and stop them or
state why they will not write; then re-check after a delay, or run a guard that
compares the live value with the target and raises on drift. A logger that
records the value without comparing it is observation, not a check. Until those
steps are done, report "applied at T, persistence unverified".

**Read a write back through a path that cannot be stale.** A read-back is
evidence about the write only if it cannot be served from a cache filled before
the write. Read through the same principal and client, or fetch the created
resource by the identifier the write returned, and look at the response's cache
headers before treating a stale listing as a missing write — an
unauthenticated or differently cached list can lag by its cache lifetime.

**Identifiers the system issues are query results, not values.** Any handle the
subject can re-issue — network ids, PIDs, session handles, row ids — must be
looked up again after every operation that could regenerate it. A scenario
script that pins an id captured at startup will, after an intervening
reconfigure, act on the wrong object; the resulting legitimate failure then
invalidates the whole run while looking like a real reproduction. Resolve by
stable name at the point of use. Relatedly, pin environment-dependent branches
(signal strength, timing races, scheduler order) to a deterministic
configuration for the duration of the test rather than letting the environment
choose.

**A failure that appears beside a change is attributed by a matrix, not by one
re-run.** "My change broke it" and "it was already broken" are both confident
answers a single re-run can produce. Vary two axes independently — source
version (original / changed) and scope (the single test / its file / the whole
suite). First, because it is the cheapest check, look for any call path from
the changed code to the failing test at all. Read the failing cell's output
before running anything again; unless it already shows a deterministic cause,
repeat that cell once. That repeat is a measurement — it is how you establish
whether the failure is deterministic — not the retry that systematic-debugging's
"Before re-running a failed job or command" rule forbids. Record which cells
you ran; a cell that was never run is evidence for neither answer.

### Adds to Key Patterns — the instrument is your own tooling

**Your control path must not run over the system under test.** When verifying a
remote or embedded target, the channel you observe through has to be independent
of what you are exercising. Driving the test over the same interface it
disrupts (roaming, link switching, service restarts) turns the subject's correct
behaviour into a tool failure, and you cannot tell the two apart. Move the
control channel to a separate physical path, and keep exactly one multiplexed
session rather than several parallel ones. (Practical trap: a multiplexed
session's control socket is a Unix socket bound by a ~108-byte path limit — use
a short directory plus a hashed name, not a long scratch path.)

**Watchers, pollers and filters need a positive control before you trust their
silence.** Arm any monitoring loop with a query that must match something that
already exists — briefly move the baseline back so one known event is returned,
confirm it, then restore the real baseline. Two further rules for the loop
itself: do not discard the query's stderr (a syntax error, an auth failure or an
API change all become indistinguishable from "no matches" once stderr goes to
`/dev/null`), and escalate a run of non-zero exits into an explicit
`QUERY BROKEN` signal rather than letting it look like patience. A loop that
prints "armed" is not a loop that works — "armed" is produced by whichever
statements *did* succeed.

**A detection filter is part of the instrument.** An anchored pattern
(`grep -E '^FAIL'`) over a merged `2>&1` stream is unreliable: stdout and
stderr are buffered differently, so a failure marker can land mid-line after a
partial line from the other stream and never match at line start. Preserve the
process exit status and prefer separate streams or structured result records.
An unanchored match is only a diagnostic hint: quoted markers and test input can
also match, so verify the marker's provenance before counting failures. If two
runs of unchanged source produce different failure counts, suspect the filter
before you call the subject flaky.

**Compare two runs by their labels, not their counts.** When a baseline and a
completion run are compared through such a filter, subtracting PASS or FAIL
counts and matching the expected delta inherits every interleaving artefact.
Sort and diff the two runs' sets of test labels instead: added labels point at
new assertions, removed labels at removed ones, and every other difference must
be explained — usually as stream interleaving — before the comparison counts.
A set diff collapses duplicate labels and cannot see an assertion that kept its
label, so it is not a count of changed assertions; when you write a figure from
it, name the method (see "Copy evidence from the call that produced it"
below). Where you can, send stderr to its own file for the run.

**A completion probe must answer on one channel with an explicit state.**
`grep -c` prints `0` *and* exits 1 when nothing matches, so a probe written as
`$(grep -c PATTERN log || echo 0)` yields two lines (`0`, `0`), and a `!= 0`
comparison reads a still-running job as finished. Make the probe print exactly
one token per outcome and compare against that token — MATCH, NO_MATCH, and
ERROR for an unreadable input (grep's exit statuses 0, 1 and 2) — so that "no
result yet" never folds into "could not tell".

**Timestamps are observations, not estimates.** Never write a time into a file,
a checkpoint or a `since`/baseline filter from a mental estimate of elapsed
time. Read it — `date -u`, or the `created_at` of the actual event you are
anchoring to. A baseline set a few minutes into the future does not error; it
silently matches nothing forever, and its silence is indistinguishable from "no
events yet". When starting a time-filtered watch, print the baseline and the
current time together and validate baseline <= now using parsed timestamps.
Also verify the event source's clock and time precision; this check alone does
not establish that a cross-host timestamp is comparable.

**Verify the thing that runs, in the shape it runs in.** For a generated
command sequence that manipulates system state (`ip`, `nft`, `sysctl`, a
shell pipeline assembled at runtime), testing its `awk`/`sed` fragments against
hand-written input verifies the text processing and nothing else — not the
tool's real output format, not ordering semantics, not exit-status propagation.
Execute the sequence in an authorized disposable environment with a before/after
dump. Namespace creation depends on host policy and available capabilities;
`unshare -rn` is not universally available to an unprivileged user. Verify the
required user/network namespace support first. If unavailable, report that
verification gap or use an already-authorized test environment; do not silently
elevate privileges. Run
both arms: once where it must succeed, and once where a step must fail, to prove
the failure actually surfaces. A trailing conditional at the end of a generated
sequence will otherwise mask every step's exit status.

**A command form written into prose is a function over shell grammar.** When
guidance prescribes a shell idiom around a placeholder (a `<command> 2>&1 |
tail` excerpt, a wrapper, a redirection pattern), enumerate the shapes the
placeholder can take — a simple command, an `&&` or `;` list (only its last
element feeds the pipe), a command that contains its own pipeline (whose status
`pipefail` changes), a subshell, a consumer that exits early — and run the form
on each through the same executor the reader will use before publishing it;
pin each shape in a test. A form measured on one example is verified for that
example only, and every review round will find the next shape it breaks.

**Before/after dumps must be captured the same way.** When you diff rendered
text from before and after a change to show that only the intended edit
changed, load both captures fresh through the same procedure (no navigation
state carried over between them), expand collapsed, tabbed and lazily rendered
content explicitly before capturing — the body of a collapsed `<details>` is
absent from `innerText` — and cross-check any "disappeared" section with an
independent count such as element counts. A large swing in dump size points at
the instrument first.

### Adds to Key Patterns — the claim's scope is the evidence's scope

**Report a guard's coverage as the mutation classes you ran.** One red mutation
is a fact about that mutation, not the guard's coverage. Write which classes
you actually tried and how many runs went red out of how many (red in 16 of 19
runs is probabilistic detection, not "catches"); keep structural detection (the
evidence cannot be produced) apart from
timing-dependent detection (a scheduling race) instead of joining them under
one verb such as "catches"; and name the classes you did not try as "not
covered" — silence reads as a coverage claim.

**Classify a check by the defects it catches, not by how "real" it is.** A
static source check that counts a repeated declaration catches the omission
that compiling and running cannot (three of four tables updated, every type
valid); a runtime test catches what text inspection cannot. When you summarise
verification — above all in a document leaving the team — state each item's
method (runtime, fixture, static source check) and what it cannot catch, and
carry the source test's own stated limits into the summary. A pass count
without the method erases the kind of coverage.

**A number quoted as a limit must say what kind of number it is.** Before
calling a value a limit, establish whether it is a measured physical limit, a
currently configured value, or a specification ceiling — values inside a
calculation are usually configuration, and a calculation does not verify its
own inputs. Keep the source's "estimated", "current setting" or "unconfirmed"
qualifiers in every summary: a summary more confident than its source is itself
a defect, and it is costliest once it reaches a public artifact.

**Count the population before changing what the code accepts.** One sample can
be consistent with a format without implying it. Before widening or tightening
a validation rule because of an observed identifier or value, collect the
population that is one cheap call away (a list call, the existing records) and
test the inferred alphabet or shape against it. A single-sample inference can
go into notes as a guess; it should not drive a code change.

### Adds to Key Patterns — changing a dependency pin

**"Bump the version" is a string change only if the interface did not move.**
Before changing a reference to a shared workflow, library or API client,
extract the interface difference between the old and new versions: inputs that
became required, inputs removed or renamed, and configuration keys added. Any
of these makes the change a migration — tell the requester, and list the
decisions it carries (for example whether a newly added option should be on)
instead of choosing them silently.

### Adds to a new section — Order of operations

**Never build the claim and the evidence in the same atomic step.** Run the
verification, read its output, *then* write the artifact that asserts it.
Bundling a mutation/verification arm together with the commit, push or comment
that describes its result means the claim is fixed before the result exists —
and when the arm silently produces nothing, the assertion is already pushed
before you learn it has no basis. `set -e` does not save you here: a
must-fail arm has to tolerate a non-zero exit (`|| true`), so the script sails
past the case where the expected failure did not happen.

Two rules make this safe:

- Assert the expected failure explicitly (`grep -q 'FAIL <marker>' || exit 1`)
  so the verification step itself halts the script.
- Capture logs defensively — `LC_ALL=C grep -a` — because a capture that turns
  out not to be valid text reads as an empty arm, which reads as success.

**A verification you have launched is not one you have read.** Before writing a
figure or claim into an issue, PR, commit message or document, ask whether a
check of that same claim is still running — a background agent, a CI job, a
re-measurement. If one is, hold the artifact until its result is in. If you
cannot hold it, leave the claim out or mark it as still being reproduced, and
come back to settle it when the result arrives. The launched check feels like
confirmation, but until it finishes the claim is exactly as unverified as one
nobody checked.

This is the persistence half of the same rule that keeps unverified inferences
out of commit messages, docs and issues: once a claim is in a durable artifact
its provenance is laundered, and later readers will not re-derive it.

**Copy evidence from the call that produced it.** Every number and every
command string written into a durable record — a commit's Validation field, a
PR body, a report — is copied from the tool call and the output line that
produced it, reopened at the moment of writing. Do not retype a command from
memory, substitute a different command that gives the same answer, or convert
a figure from another tally (assertion calls versus label-set diff lines). If
two methods count the same thing, read both from their outputs and explain the
difference in words. A reconstructed figure that sits next to a reproducible
method is the first thing a reviewer refutes.

**Make an irreversible step depend on the check that allows it.** Never put a
cleanup, deletion or release in the same unconditional script as the check
that decides whether it is allowed. Run the check, read its result, and only
then run the destructive step — or gate it explicitly on the check's success.
A script that prints a mismatch and then cleans up anyway has destroyed the
state a cheap retry needed.

### Adds to the evidence table

| Claim | Required evidence | NOT evidence |
|---|---|---|
| Attack/edge case blocked | Same probe shown landing somewhere (positive control) | The probe returned a rejection |
| Spec/schema file valid | Strictest available consumer accepts it | One lenient runtime parsed it |
| Feature absent from a system | The channels you searched, enumerated | "I grepped the logs and found nothing" |
| It builds / the tests pass | The log line showing the step ran | A green job that may have skipped |
| This command has an effect | Before/after around the command run alone, from a differing start state | The value was right afterwards |
| The setting was reverted | The consumer observably acting on the new value | The write succeeded |
| A mutation proved the test's power | The injection asserted at the intended site, neighbours unchanged | The mutated run came back red or green |
| Nothing has arrived yet | The same query returning a known existing event | An empty poll |
| A scripted document edit is correct | Whole-file invariant counts unchanged + a render/re-parse | The new text is present |
| A generated command sequence works | It ran in an authorized isolated test environment, both arms | Its text fragments parsed test input |
| The watched jobs all passed | Per-item states: each passed, or skipped for a confirmed reason | The watch command exited 0 |
| A guard catches a class of regression | The classes tried, red/runs for each, untried classes named | One mutation went red |
| A control run exercised the old code | The root the control harness resolved, inside the control directory | The control run passed |
| A figure shows X | A rendered view of that page | The extracted text order |
| A generated document's identifiers are right | Each checked against an existence oracle | Links resolve and copies agree |
| A setting stays applied | Other writers enumerated, plus a delayed re-check or a drift guard | A read-back right after the write |
| A comparison rule identifies the same document | Two matching pairs pass and two near-miss crossed pairs fail | Every line of one appears in the other |
| Two runs differ only by my change | A diff of their label sets, each difference attributed | The PASS count moved by the expected amount |

### Adds to Common Failures

- Treating "blocked" / "not found" / "no output" as proof without a positive control
- Citing a CI job as evidence without checking whether its steps executed
- Reading a mutation result before asserting the mutation was actually injected — and injected at the intended site
- Writing a verification claim into a commit message, comment or document in the same step that produces the evidence
- Publishing a figure while the check that verifies it is still running
- Filling a timestamp or a `since` baseline from an estimate of elapsed time
- Treating "the test passed on the first run" as "already implemented" without measuring the precondition
- Trusting a wait/watch command's exit status instead of the per-item states it printed
- Writing that a guard "catches" a class you never mutated, or merging probabilistic and structural detection
- Building a control directory from symlinks for a harness that resolves its own path
- Re-running a mutation in a shared copy without re-applying it in the same command
- Reporting a runtime setting as applied from one read-back taken right after the write

### Adds to Rationalizations

| Excuse | Reality |
|---|---|
| "The attack was rejected, so we're safe" | Show the same probe landing first |
| "The parser accepted it" | Which parser? Lenient ones pass defects |
| "CI is green, so it builds" | Green can mean the build step was skipped. Read what ran. |
| "I reverted it and the gate stayed green, so this change isn't covered" | Check the mutation landed at all, and landed on the right line |
| "I ran it and the value is correct" | From what starting state? A no-op looks identical |
| "The monitor has been quiet, so nothing has come in" | Prove the monitor can still match. Silence is also how it breaks |
| "The replacement worked — the new text is there" | Count what should NOT have changed |
| "It's roughly 19:58 now" | Read the clock. An estimated baseline fails silently, forever |
| "The watch exited 0, so CI passed" | Read the per-item table. The exit status may cover only the gating subset |
| "The translation diff shows three changes" | Diff the originals. A derived copy can omit a change made in the source |
| "That mutant survived, so the test is weak" | First prove the mutated line runs under that test |
| "I read it back and it's set" | That was one instant. Who else writes that knob, and what does it read later? |
| "Every line of the document is on the page" | Containment passes a version that only adds text. Cross-check with a near miss |

---

## superpowers:systematic-debugging

### Adds a precondition to Phase 1

**Read the failing thing's own output before you name a cause.** Open what the
failing component itself produced: CI job logs, stderr, exit status, the
response body, the destination that was written to. Then quote the line that
supports your diagnosis. If you cannot quote one, what you have is a hypothesis
— say the word "hypothesis" when you report it. Reaching for a plausible cause
before reading available output inverts the cost of investigation: the read
takes seconds, and the guess buys a wasted cycle plus a claim you have to
retract.

### Adds to evidence gathering

- A success exit code is not proof the work happened — check the destination for
  the record the work claims to have created.
- Check that the TOOL produced the output, not your shell: an empty or
  oddly-shaped result is often your own quoting or redirection having eaten the
  query.
- Before re-running a failed job or command, establish that the failure is
  non-deterministic. "Flaky" and "sporadic infra failure" are diagnoses and need
  the same evidence as any other — retrying a deterministic failure is
  structurally guaranteed to reproduce it and costs a full cycle.
- Do not confirm a cause by running the suspected operation and then reading the
  expected value. That is compatible with the operation being a no-op that
  returns success while some other path set the field. Establish a differing
  start state, run the operation alone, compare — see "Post-state is not
  evidence of causation" under verification-before-completion. A diagnosis built
  on an uncontrolled confirmation reads as solid, gets written into a fix, and
  is only contradicted much later.
- Before discarding evidence as a tooling error ("that reading can't be right,
  the parser must be off"), measure it again with a second, independent method.
  Until that re-measurement contradicts it, keep it as a live hypothesis, not
  noise: surprising evidence is more often a lead than a bug in the probe.
- When reproducing why a gate or controller blocked something, run its exact
  recipe — the argv and environment the controller itself builds, read from its
  source — not a similar-looking command. A near-copy can land on the right
  cause by luck and still mislead the re-check after a fix, because it exercises
  a different mechanism. A harness that calls the controller's functions
  directly should start by reading and meeting their preconditions, not by
  discovering them one rejection at a time.
- **Keep your own tooling out of the evidence.** Inspect artifact and evidence
  directories with absolute paths from a neutral working directory; never `cd`
  into them. Harness hooks keyed to the working directory can create their own
  state inside the tree you are observing — tripping the subject's permission
  checks and contaminating preserved failure evidence. Before attributing a new
  failure to the system under investigation, list the files in the observed tree
  that are newer than the start of your investigation.
- **A remembered failure must carry its cause condition.** Before re-citing a
  past failure ("this produces an invalid-format error"), check that the cause
  condition of that case holds now; the same symptom from a different cause
  predicts nothing here. When a cheap direct trial exists within the authority
  you already have (load it once, run it once), run it before asserting the
  failure; a trial on shared or target hardware still needs its usual approval.
- **When a recorded measurement and a code reading disagree, ask whether they
  saw the same layer.** Production, transport and display are different layers:
  a value set correctly inside the program can still be dropped by a log
  daemon's own filter. An absence observation ("it did not show up") can be
  created at any layer, so it is evidence about none of them until the layer is
  identified. Reject neither side before that.
- **A resource and the marker that announces it are different states.** When a
  consumer "cannot use X", check X itself and, separately, the marker the
  consumer actually reads (a flag file, a ready file) — follow the consumer's
  condition down to the check. Enumerate every writer and every deleter of the
  marker; when there are more deleters than writers, the asymmetry is the defect
  site. A recovery path that bypasses the managing service and repeats only the
  real action typically leaves the marker missing. Correlate the marker's last
  change with deploy and upgrade history.
- **Stopping a process does not reset the device behind it.** A driver can keep
  state across a userspace restart (a generation, epoch or configuration
  fingerprint) and refuse a different configuration within the same epoch. Read
  the state the driver exposes before and after a restart instead of assuming
  re-initialisation, and find out from the driver code what advances it.
  Read-only evidence comes first: this rule grants no authority to reset,
  unbind or power-cycle anything.
- **A stable error code is the address of the rule.** When a tool, hook or CI
  rejects you with a constant-looking code, search the decider's source for that
  code before your second attempt — the matching branch states the accepted
  form. Two consecutive attempts that return contradicting codes mean your model
  of the rule is wrong: stop guessing. A large source is no reason to skip the
  search; only the branch that uses the code needs reading.
- **Validate a positional parse against a value you already know.** Flat or
  positional tool output (`atop -P`, `sar`, `/proc` files, a CSV without a
  header) parsed with an off-by-one column offset still yields well-formed
  numbers, and nothing in the pipeline fails. Before using any number, check
  one parsed field against a known invariant — CPU tick fields summing to
  interval × HZ × CPU count, physical memory equal to the host's RAM, page size
  equal to the platform's. An implausible value is a column-offset signal, not
  a finding.
- **An average excludes only causes slower than its window.** Before writing
  "X was not involved" from telemetry, put the telemetry's sampling interval
  next to the claim and compare it with the timescale of the suspected
  mechanism. If the mechanism is faster than the sample, the claim is limited
  to "no sustained X": take one short high-rate sample, or read counters that
  integrate every episode (throttle and power-limit event counts, error
  counters), before excluding it.

### Adds to Phase 1 — evidence in multi-component systems

**Look for one ancestor before you branch into two bugs.** When several symptoms
are reported together, the presence of a plausible local explanation for one of
them (log rotation, a restart, a config drift) is exactly what stops the
investigation early. Ask first whether a single cause produces all of them.

- **"Running" is not innocence.** Classify each component as producer or
  consumer of the data in question. A consumer that is healthy but idle is
  evidence about its *upstream*: an empty output with a running writer means the
  writer received nothing, so look at who was supposed to generate the events.
- **Read the unit's conditions, not just its body.** For service managers, a
  unit skipped because a condition was unmet may not appear in failure-only
  log searches. Inspect status output, condition results, drop-ins, and the
  relevant journal instead of relying only on error-level messages. Read those explicitly before concluding a service
  "just isn't doing anything".
- **An experiment's gate outlives the experiment's flag.** A drop-in or
  override installed on a persistent path but keyed to state on a volatile one
  (a flag file under a `tmpfs`) becomes permanently fail-closed at the next
  reboot: the flag evaporates, the gate does not. Teardown for a temporary gate
  means removing the gate itself, not just the state it reads.
- **An error's wording does not say which layer produced it.** When a client
  wraps a remote service, reproduce once through the client and once with a
  direct minimal request to the service, and compare any suggestion or
  enumeration in the error ("did you mean …") with the client's own local
  catalog. Only a failure that also reproduces on the direct call is a
  server-side fact. Check account, quota or plan state on that same call: a
  client-side fix can be necessary and still not sufficient. Isolating a CLI's
  configuration for this can need an isolated home directory, not only an
  alternate config directory — legacy dot-directories still load.

**No window into internal state? Use the subject's own diagnostic paths.** When
the usual inspection points are unavailable (no kernel stack, memory or register
access), look in the subject's source for an existing path that reports the
state you need — a warning that prints a stored owner, a value carried in an
error return — and use it as a non-destructive probe. Run a positive control
first: confirm the probe reads a known live state before you trust what it says
about the faulty one.

### Adds to "Phase 3: Hypothesis and Testing"

**Reproduction is not attribution.** For a failure that needs two preconditions
at once, widening both and watching it fail does not say which one causes it,
and widening only one and seeing no failure does not refute it. Run a
reproducing arm (fails) beside a discriminating arm that differs from it in
exactly one factor (passes); only that pair attributes the failure to the
factor. Combinations you did not run stay unverified.

**Once a mechanism hypothesis exists, build its trigger.** Construct the
condition the hypothesis requires and test it deterministically instead of
waiting on a probabilistic reproduction (a kill loop that hits one time in
ten). A deterministic trigger is cheaper, and its negative result means
something. This is the debugging side of the TDD rule above that probabilistic
preconditions must be forced, not hoped for.

### Adds to Red Flags

- "CI is flaky, just re-run it" (said without having opened the log)
- "It returned success, so the work happened"
- "Two things broke at once, so there are two bugs"
- "That service is running, so it isn't the problem"
- "I ran the command and the value is what I wanted, so the command works"
- "That reading can't be right — it must be a parsing error"
- "It failed once I widened everything, so I know the cause"
- "The ready flag is missing, so the resource is broken"
- "The averages were normal, so load isn't involved"
- "The error says the model is unsupported, so the provider dropped it"

### Adds to Rationalizations

| Excuse | Reality |
|---|---|
| "CI is flaky, re-run it" | Open the log first. A deterministic cause makes the re-run a guaranteed repeat. |
| "The command exited 0, so it worked" | Exit code reports the command, not the effect. Check the destination. |
| "The log is empty because of rotation" | A plausible local story for one symptom is what stops you finding the shared cause. |
| "The unit is active, so it ran" | Conditions skip units silently, and skips are not failures. Read the drop-in and condition lines. |
| "Restarting the service reset the hardware" | Read the state the driver exposes, before and after. Process and device lifecycles differ. |
| "The code says the setting applies, so the measurement must be wrong" | Ask which layer each one observed before rejecting either. |

---

## superpowers:requesting-code-review

### Adds to How to Request — enumerate the prose's mechanism claims

Reviewers verify what they are asked to verify. When a diff adds documentation
or guidance prose, pull out each mechanism claim it makes — how a file is
deployed (link or copy), the order steps run in, what an external tool or a
gate does or rejects — and list each one in the reviewer prompt as an item to
confirm or refute against the code or by running it. An enumerated claim gets
checked against its primary source; an unenumerated one passes on how
plausible the sentence sounds, and a code-focused review then approves a
factual error.

- When a rule transcribes a contract's exception forms (sentinels, escapes,
  allowed values), take the whole set from the spec's table or the validator's
  constants, not from an example — and enumerate that claim too.
- A safety-net sentence ("re-validate afterwards") is itself a claim to
  enumerate: it hides any violation its validator cannot catch.
- When the same policy text is copied into two consumers, ask for an
  equivalence assertion with host-specific tokens neutralised, plus a mutation
  experiment showing the assertion fails when one copy drifts.

### Adds to reading the response

**Read the counters, not the summary sentence.** Automated reviewers frequently
publish a human-readable verdict ("No findings", "No validated blocking issues")
alongside machine counters from their own internal filtering — for example
`accepted=0; filtered=1; filtered_max=MEDIUM` a line above. A non-zero
`filtered` / `suppressed` / `skipped` / `dropped` counter means findings *were*
produced and something else discarded them; that judgement is the bot's quality
heuristic, not a review outcome, and the discarded finding can be entirely
valid. Recover the originals from the run log or artifacts and judge them
yourself.

A non-zero counter requires inspection, not an automatic blocking verdict.
Verified duplicates or justified exclusions can coexist with a CLEAN result.
Apply the repository's actual review gate after examining the discarded payload;
if the payload is unavailable, report the unresolved evidence gap rather than
inventing either approval or a finding.

**Enumerate the channels a reviewer can answer on before concluding it has not
answered.** A reviewer that fails — quota exhausted, environment rejected,
connector error — often reports that failure on a *different* channel from the
one carrying its reviews: a plain comment on the request rather than a formal
review or an inline note. If your polling only samples the formal channel, a
verdict available in seconds is indistinguishable from silence and you wait out
the entire timeout, then misreport the cause as a timeout rather than a refusal.
List the channels first (formal reviews, inline comments, plain comments,
reactions, status checks), sample the applicable channels, and classify a
failure body only after verifying the actor and its association with the current
request/head. An old failure or another author's comment is not a current verdict.

**Report settled blockers promptly; preserve required completion gates.** If a
terminal failure already prevents progress, report it now and identify the
outstanding responses. Do not infer approval from silence or advance a merge
while required reviewers or checks remain incomplete. Follow the repository's
explicit waiting, retry, and terminal-state policy.

**Turn a confirmed tooling gap into a scoped next action.** If the user asked
for implementation and the gap is within that scope, reproduce it, fix it, and
add a regression test. For a read-only investigation, preserve the evidence and
propose the fix; do not change tools, create issues, or expand the task without
the required authority.

---

## superpowers:receiving-code-review

### Adds to evaluating a suggestion

**Before rejecting a demand as impossible, ask which layer it is impossible
at.** "The test doesn't verify real state change — a regression where the
command silently does nothing would pass" is a legitimate observation even when
the test is a unit test with stubbed externals, where observing real state
change genuinely cannot be done. Impossibility is a property of the *layer*, not
of the requirement. Walk it up: unit → integration → real environment, and find
the layer where the property is measurable.

Rejecting an out-of-layer demand at its original layer leaves the gap the
reviewer identified fully intact, while producing a technically-correct reply
that closes the thread. Running the same block verbatim in a real environment
and measuring before/after usually costs little and frequently returns more than
was asked for — idempotency, ordering and permission behaviour fall out of the
same run.

When you do relocate the check, say so in your reply, and record why the
original layer cannot host it — otherwise the same finding is filed again on the
next review.

**Widening an exclusion weakens a protection.** When a suggestion widens what a
guard, filter or validator lets through — a broader boundary, an extra skip
pattern — enumerate the inputs the wider exclusion newly admits, not only the
false positives it fixes, and run those inputs against the base and the
candidate. Prefer the formulation that mirrors the source of truth's own rule
over a broader lexical one. A regression suite and a real-data replay measure
accepted-good inputs; neither contains adversarial inputs at the new boundary,
so "no diff on the suite" says nothing about what the change now lets through.

**When fixing a demonstrated bypass, test the fix's shape before its content.**
If the fix is a list of things to strip, escape or normalize, first try to name
three members of the class that the list does not cover — another escape
family, overstrike, a zero-width format character. If you can, the class is
open-ended: refuse on the presence of the whole class instead of normalizing
its known members. This is the global rule "생성·수정 전 주변 열거" item 4
(remove rather than defend) applied at the first fix rather than the third.
Inside subagent-driven-development it applies to blocking findings only (see
"Adds to the implementer brief" there); a bypass of a security guard is a
security finding.

---

## superpowers:subagent-driven-development

The budget rule itself lives in the global guidance
(`~/.claude/guidance/review-budget.md`, summarized in `~/.claude/global-guidance.md` section "리뷰·검증 예산"); this section maps it
onto this skill's loop. Where the two disagree, the budget rule wins. A scope or
budget your human partner states in the request beats both.

### Adds to setup — announce the budget

Before dispatching the first implementer, state the budget in one line: the
number of tasks, which tasks get a task review, and the fix-round cap. If you
expect to exceed it, ask before continuing — not after the overrun.

### Adds to setup and to "1. Dispatch the implementer" — plans the scripts do not expect

- The plugin's `task-brief` script finds only headings of the form `Task N`
  (any heading level) and exits with status 3 otherwise. For a plan whose
  sections use other headings (for example `## A1.`), extract each section by
  hand into the same brief path the script would use, and record that in the
  ledger.
- When the plan forbids commits, or the baseline you must review against
  includes uncommitted files that a worktree from HEAD would not contain, the
  review packages can still be kept: seed a separate scratch repository with
  hash-verified copies of the allowed files, run the tasks there, and let its
  commits define the review ranges, so the user's repository history stays
  untouched. This is a decision outside the plan text: announce it to your
  human partner in one line before acting (see "Adds to rulings" below — a
  ledger `Ruling:` line alone is not the announcement). Before the Finish step
  deletes any workspace, hand the result back: apply the scratch repository's
  final diff to the user's tree as uncommitted changes and verify the applied
  files by digest.

### ⚠ Amends an existing rule — "Review the task"

The plugin copy says:

> Never skip the task review, and never accept a report missing either verdict

**Read it as this instead:**

- **Run a task review only for high-risk tasks** — ones that touch
  permissions, a security boundary, data changes or concurrency. Every other
  task skips its task review; the final whole-branch review covers its diff.
- A task review you do run still requires both verdicts, spec compliance AND
  task quality.
- A task that skipped its task review completes with its own ledger line:
  `Task <N>: complete (commits <base7>..<head7>, review skipped: not high-risk)`
  — never `review clean`. Like the plugin's two completion lines, it marks the
  task DONE when the ledger is read back after compaction.

### ⚠ Amends an existing rule — "The fix loop"

The plugin copy says:

> The loop triggers when the review reports spec ❌, any Critical or Important
> finding, or a ⚠️ item you confirmed as a real gap.

and

> Five rounds maximum per task

**Read it as this instead:**

- **Only four kinds of finding enter the loop:** security, data integrity,
  violation of a sentence in the spec or plan, and a Critical correctness
  defect (a crash, a broken build or test, a regression of existing
  behaviour). Every other finding, Important included, goes to the ledger's
  follow-up list beside the deferred Minor findings, for the final review to
  triage; an item the final review classes as one of these four kinds enters
  its fix pass. A finding recorded on that list counts as parked with a ruling
  for the "5. Complete the task" gate, so the task may move on. Every item the
  final fix pass leaves unfixed is listed in your final message beside the
  rulings — the Finish step deletes the workspace, so an item kept only in the
  ledger would vanish unseen.
- **Never promote a Minor finding into a fix round.** The plugin already keeps
  Minor findings out of the loop; the failure seen in practice was the
  controller routing them back in.
- **One fix round per task; two only for a security or data-integrity
  defect.** The rounds 4–5 escalation therefore never happens. A defect that
  survives its cap goes to your human partner, not to another round.

### Adds to Model Selection

Default to sonnet for implementers and reviewers. Reserve opus for tasks that
need design judgment and for the final whole-branch review.

### Adds to rulings — decisions outside the plan text

The plugin has you rule on findings that conflict with the plan and ledger the
ruling. When a ruling adds verification, widens scope, or runs a command the
plan forbids, announce it to your human partner in one line before acting on
it. A ledger entry is not that announcement.

A ruling, deferral or fix spec that cites a safety net ("X still catches
this", "the CI audit reports it") must quote the location that enforces X
today — a workflow file and line, a hook, a test — not the plan or design that
will build it (global rule "판단 전 1차 출처 재확인"). When the diff states
such claims about external systems (CI, other repositories, deployed
services), list them in the scoped re-review prompt as claims to check against
those systems, as requesting-code-review's mechanism-claim list does. That
widens the re-review, so announce it in one line as above.

### Adds to the implementer brief — verification scope

The global rules "검증: 되돌리면 실패하는가" and "생성·수정 전 주변 열거" have
been seen to turn an implementer's task into an audit. Inside this loop, a
blocking defect means one of the four kinds that enter the fix loop above.

- Item 1 of "검증: 되돌리면 실패하는가" (compare against the old behaviour)
  applies only to blocking defects and to the task's core change: confirm the
  core change is detected (it fails on the old behaviour), and do not revert
  each safeguard in turn. A test that pins a guard the task touches (see
  "Adds to GREEN — minimal code" above) is part of the core change. Its other
  items still apply.
- Trigger ② of "생성·수정 전 주변 열거" (enumerate a demonstrated defect's
  class before fixing it) applies only to blocking defects; do not seal off a
  whole defect class for a non-blocking finding. Trigger ① is unaffected.

Say so in the brief.

### Adds — the plan header does not mandate the full loop

superpowers:writing-plans puts this header on every plan:

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

That line picks the execution skill. It does not make the plugin's full loop —
a review per task, five fix rounds — mandatory. The amendments above apply to a
plan that carries it exactly as they do to any other plan.

---

## superpowers:dispatching-parallel-agents

These additions apply to any delegated agent, including a single one dispatched
outside this skill.

### Adds to "Agent Prompt Structure"

- **Ask an investigation agent to split its confidence.** Require the report in
  two parts: CONFIRMED (each fact with file:line evidence) and UNCERTAIN (each
  with the means that would verify it). The uncertain list becomes the next
  experiment plan instead of being buried in prose.
- **For inventories of configuration, allow-list what may be printed.**
  Secrets appear in at least three shapes — environment values,
  `--flag=value`, and `--flag value` — and a redaction that enumerates shapes
  misses one. When an agent inventories configuration or processes that may
  embed secrets, tell it to print only structure (server names, command
  basenames, environment key names, flag names, argument counts), to treat whole
  argument arrays as sensitive, and to scan its planned output for key-like
  strings before returning. Prefer an existing redaction helper over a pattern
  the agent writes on the spot. Truncating output limits volume, not
  sensitivity; a secret found in a process argument list is itself a finding to
  fix.
- **Pass each premise with its confidence grade.** A premise written into a
  prompt becomes, for the agent, a confirmed fact. Mark as confirmed only what
  you saw in tool output in this session; mark the rest "estimate — verify or
  refute", and ask for refutation explicitly. Work out any number the premise
  rests on (a macro expansion, a type size, an ABI value) before you send it.
- **Keep a read-only agent's writes out of the repository.** Say that the
  repository is read-only for it and that anything it writes goes to absolute
  paths under a scratch directory — a `cd` may not stick, since some harnesses
  reset an agent's working directory between commands or refuse to leave the
  project root. Make "the repository's working-tree status is unchanged from
  when you started" an end condition it reports. After each delegated run,
  check the repository's status yourself: a mistyped redirection writes into
  whatever the working directory is, and agents rarely report the stray file.

### Adds to "Review and Integrate"

- **Delivery of a result is not termination of its producer.** A completion
  notice can carry a complete, well-formed result while the agent is still
  running, and its later final answer can differ materially. Before consuming an
  agent's output into anything expensive to reverse — sealing, publishing,
  committing — confirm the agent is terminal through a channel other than the
  one that delivered the output, such as a liveness listing. When the two
  disagree, keep waiting within your wait budget; if the budget runs out,
  report the agent as still running rather than consuming its interim output.
- **Spot-check evidence the agent claims to have computed.** Scan claimed
  digests for repeating or patterned hex, and recompute at least one digest over
  output you can reproduce yourself. A claimed empty-input digest proves only
  that the output was empty — it is a widely memorised constant, so confirm the
  output really was empty and never count it alone as authenticity evidence. A
  confidently formatted result can still be fabricated.
- **Read a report's tool failures first.** If the agent says its primary tool
  failed, treat anything in the report not marked as coming from a working tool
  as fallback-channel material, not as the primary channel's result, and ask for
  such a failure to be stated at the top of the report.
- **A status marker an agent relays is the record's state at its last update.**
  When a report says "not sent", "pending" or "not done" because a document
  says so, relay it as "per the record". Before planning an outbound or
  irreversible action from it — sending mail, publishing, filing — confirm the
  current plan with your human partner; the work may already be done through
  another channel.

---

## superpowers:writing-plans

### Adds to "Global Constraints"

- When the plan replaces or re-implements an existing tool, add one line
  naming the tool, its pinned version and where its effective rules were read
  (inherited presets and default ignore lists included). Put the exact rules
  and test vectors in the owning task as a test step, so the copied rules are
  checked against them (see brainstorming's "Replacing a tool means matching
  what it actually does" above).

### Adds to Self-Review — "3. Type consistency"

- **A reused variable is checked by its binding, not its name.** For a test
  step that reuses a variable or fixture from an existing test file, open where
  it is bound (the file it reads, the fixture's definition) before writing the
  assertion. A negative assertion (`doesNotMatch`, `not in`) against the wrong
  target passes forever, so until the binding is confirmed, do not count it as
  a change detector.

---

## superpowers:using-git-worktrees

### ⚠ Amends an existing rule — "1a. Native Worktree Tools"

The plugin copy says:

> Only proceed to Step 1b if you have no native worktree tool available.

**Read it as this instead:**

- **Inside a submodule or any nested repository, skip 1a and use 1b.** A native
  worktree tool is bound to the repository the session started in: given a
  worktree created inside a submodule it can fail, and it may not let you name
  an explicit base such as the submodule's own upstream branch. Create the
  worktree with git from inside the nested repository, from an explicit base.
  A worktree made this way is invisible to the harness, so you remove it with
  git yourself when the work is finished.
- **Whichever path you took, confirm where the session actually is.** A tool
  can report failure while the session's working directory has already moved.
  Before Step 2, ask git for the top-level directory of the current working
  directory and compare it with the worktree you meant to enter.
