---
name: superpowers-extras
description: Use together with any superpowers process skill — brainstorming, test-driven-development, verification-before-completion, systematic-debugging, requesting-code-review, receiving-code-review. Carries later corrections to those skills' rules that the plugin-installed copies do not have. Read this whenever one of those six is loaded, before acting on its rules.
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
Base skills are from the `superpowers` plugin (v6.3.0).

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

---

## superpowers:test-driven-development

### Adds to Verify RED

**Read both arms.** A red/green pair is a two-sided instrument, not a
one-sided gate. When you run a control arm (feature present) beside a mutation
arm (feature removed or reverted), an unexpected result in EITHER arm is a
finding:

- **Mutation arm green** → this indicts your harness, not the subject. The test
  cannot detect the thing it claims to test.
- **Control arm red while the mutation arm behaves as expected** → this is
  evidence about your harness or your usage of the API before it is evidence
  about the subject. Diagnose the harness before you conclude "this can't be
  done" and abandon the approach — an unexpected control failure has wrongly
  killed workable designs.

Reading only the arm you expected to pass discards half the instrument's
diagnostic value.

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

---

## superpowers:verification-before-completion

### New section — The Instrument Check

A "good" result — pass, clean, blocked, absent, none found — is evidence only
once you know the instrument **could have produced the opposite result**. If it
could not, what you have is not evidence; it is a verification gap in the shape
of one.

Ask before reporting it:

- **Negative assertions** ("blocked", "rejected", "no leak") need a positive
  control — see the pattern below.
- **Revert/mutation pairs** are two-sided. Read both arms: a mutation arm that
  stays green indicts the harness, not the subject.
- **Absence** is scoped to the channels you actually searched. Enumerate where
  the system persists output (logs, build artifacts, check-run output, API,
  attachments) before promoting "I did not find it" to "it does not exist" — a
  wrong absence claim also spawns work to fix a defect that isn't there.
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

Knowing this rule is not the same as running it: this trap has recurred in
projects that had already written it down.

**Applies to any scripted mutation of a structured document, too.** After a
script edits Markdown, HTML, JSON or config, verifying that the new text is
present does not verify the edit. Assert the whole-file invariants the mutation
must *not* change — element/section/heading counts, tag balance, absence of
escape artefacts such as a literal `\1` from a mishandled backreference — and
render or re-parse the artifact once after the session's first scripted edit.
"New content is present" and "nothing that should be unchanged changed" are
different claims; only the second one catches a structural break, which
otherwise ships repeatedly because every content-level check keeps passing.

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

This is the persistence half of the same rule that keeps unverified inferences
out of commit messages, docs and issues: once a claim is in a durable artifact
its provenance is laundered, and later readers will not re-derive it.

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

### Adds to Common Failures

- Treating "blocked" / "not found" / "no output" as proof without a positive control
- Citing a CI job as evidence without checking whether its steps executed
- Reading a mutation result before asserting the mutation was actually injected — and injected at the intended site
- Writing a verification claim into a commit message, comment or document in the same step that produces the evidence
- Filling a timestamp or a `since` baseline from an estimate of elapsed time
- Treating "the test passed on the first run" as "already implemented" without measuring the precondition

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

### Adds to Red Flags

- "CI is flaky, just re-run it" (said without having opened the log)
- "It returned success, so the work happened"
- "Two things broke at once, so there are two bugs"
- "That service is running, so it isn't the problem"
- "I ran the command and the value is what I wanted, so the command works"

### Adds to Rationalizations

| Excuse | Reality |
|---|---|
| "CI is flaky, re-run it" | Open the log first. A deterministic cause makes the re-run a guaranteed repeat. |
| "The command exited 0, so it worked" | Exit code reports the command, not the effect. Check the destination. |
| "The log is empty because of rotation" | A plausible local story for one symptom is what stops you finding the shared cause. |
| "The unit is active, so it ran" | Conditions skip units silently, and skips are not failures. Read the drop-in and condition lines. |

---

## superpowers:requesting-code-review

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
