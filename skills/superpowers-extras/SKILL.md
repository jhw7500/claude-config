---
name: superpowers-extras
description: Use together with any superpowers process skill — brainstorming, test-driven-development, verification-before-completion, systematic-debugging. Carries later corrections to those skills' rules that the plugin-installed copies do not have. Read this whenever one of those four is loaded, before acting on its rules.
---

# superpowers — later corrections

The four skills below ship inside the `superpowers` plugin, so their files
live in a versioned plugin cache and any edit there is erased by the next
plugin update. This skill therefore carries **only the delta** — the rules
added or amended after review of real sessions. The plugin skills remain the
base; read them first, then apply what follows.

Naming note: a user skill with the SAME name does **not** shadow a plugin
skill — both stay in the list side by side (verified 2026-09-01). That is why
this is a separate `-extras` skill rather than a copy of each original.

Provenance: observations #1–#5 in the task-observer log, actioned 2026-09-01.
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

### Adds to the evidence table

| Claim | Required evidence | NOT evidence |
|---|---|---|
| Attack/edge case blocked | Same probe shown landing somewhere (positive control) | The probe returned a rejection |
| Spec/schema file valid | Strictest available consumer accepts it | One lenient runtime parsed it |
| Feature absent from a system | The channels you searched, enumerated | "I grepped the logs and found nothing" |

### Adds to Common Failures

- Treating "blocked" / "not found" / "no output" as proof without a positive control

### Adds to Rationalizations

| Excuse | Reality |
|---|---|
| "The attack was rejected, so we're safe" | Show the same probe landing first |
| "The parser accepted it" | Which parser? Lenient ones pass defects |

### Adds to Key Patterns

**Negative results ("blocked", "rejected", "not reachable"):**
```
✅ Same probe lands against the unhardened variant → THEN assert it is blocked here
❌ "The probe came back rejected, so the defense works"
```
Without a positive control, "blocked" and "never arrived" are
indistinguishable — a malformed payload gets credited to a defense that was
never exercised. Pair every "blocked" claim with either the same payload
succeeding somewhere, or proof it reaches the code path at all.

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

### Adds to Red Flags

- "CI is flaky, just re-run it" (said without having opened the log)
- "It returned success, so the work happened"

### Adds to Rationalizations

| Excuse | Reality |
|---|---|
| "CI is flaky, re-run it" | Open the log first. A deterministic cause makes the re-run a guaranteed repeat. |
| "The command exited 0, so it worked" | Exit code reports the command, not the effect. Check the destination. |
