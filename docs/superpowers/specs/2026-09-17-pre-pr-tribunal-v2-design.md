# pre-pr-tribunal v2: snapshot-bound risk and review policy

Status: implementation approved. Issue: https://github.com/jhw7500/claude-config/issues/143

This design supersedes the unmerged 2026-09-15 draft and incorporates the
overlapping contracts from #132 and #135 plus the review-quality findings from
#121 and #136.

## Outcome

The tribunal spends pre-PR review cost in proportion to merge risk without
creating an unrecorded bypass:

```text
committed diff ----> built-in risk floor -----+
                                                max ---> effective intensity
explicit request --> requested intensity ------+
repository config -> floor raises/reviewers/models

effective 0       -> off       -> snapshot-bound skipped verdict
effective 1..66   -> single    -> one active-reviewer round
effective 67..100 -> iterative -> current decision chain, at most 3 rounds
```

The pre layer reports only findings that are expensive to reverse after merge:
security defects, data loss, broken contracts, unsafe state transitions, and
irreversible design choices. Style, naming, duplication, dead code, import
placement, and ordinary simplification remain PR-review/CI concerns.

## Authority and binding

The controller creates the policy record. Reviewer output never chooses or
weakens review intensity. A schema-v5 verdict binds:

- repository/base/head/merge-base/diff digest;
- committed config digest;
- built-in risk floor and matched reasons;
- requested intensity, requester, reason, and whether parsing failed closed;
- effective intensity and mode;
- each reviewer's enabled state and runtime-specific model;
- report/diff/evidence contract versions and lifecycle identity.

The PR gate recaptures the snapshot and recomputes policy from committed bytes.
Either mismatch makes the verdict stale. `off` is represented by terminal
`gate.status = "skipped"`; it is not PASS and it still requires a current
schema-v5 verdict.

## Risk floor

Built-in classification cannot be lowered by repository configuration.

| Floor | Mode | Examples |
| ---: | --- | --- |
| 0 | off | content edits to `.md`, `.mdx`, `.rst`, or `.txt` files under `docs/` or `doc/` only |
| 50 | single | ordinary recognized source or test changes |
| 100 | iterative | tribunal/config/hooks/workflows/deploy/auth/data/dependency changes; installed instruction prose under `claude-md/`, `skills/`, or `commands/`; rename/copy/type/unknown status; binary, symlink, submodule, mixed or unknown paths |

Every changed path is classified, including both sides of a rename. The final
floor is the maximum. Ambiguous Git metadata or classification errors resolve
to 100.

"Documentation-only" means the floor-0 row above and nothing wider. Changing only
Markdown files is not enough. Sensitive file names and path prefixes are checked
before the documentation rule, so `docs/requirements-guide.md` is 100, and a
`.md` file outside `docs/` or `doc/` is 100 as well: a repository-root
`README.md` is an unknown path. Prose under `claude-md/`, `skills/`, and
`commands/` is deliberately classified as configuration, not documentation.
`install.sh` installs those files into `~/.claude/`, where they become the
instructions every agent session runs under, so a wording change there is a
behavior change (#160, option 2). A rename or copy is 100 even between two
documentation paths.

A change whose built-in floor looks too high for its content is not lowered by
reclassification or by repository configuration. A human-requested lowering
path bound to one snapshot is proposed separately in #162; until it exists,
these changes run the iterative mode.

Repository `[policy]` patterns may raise the floor by mapping to `off`,
`single`, `iterative`, or integer 0..100. They cannot reduce the built-in
value. `.pre-pr-tribunal.toml` itself is always floor 100, preventing a policy
weakening change from reviewing itself at the weakened level.

## Human-direct intensity grants

Neither repository configuration nor the controller's `--intensity` request can
lower the built-in floor. A person can, for one snapshot, through
`intensity-grant --base B --runtime R --intensity N --reason TEXT` (#162). This
reverses #135's non-goal "lower the floor by user request"; #132's "no
unrecorded bypass" and the raise-only `[policy]` rule still hold.

The command requires a terminal on stdin, shows the snapshot, floor, mode and
reasons it resolved, and records nothing until the person types `lower`. N must
be below the floor and must change the
mode, so a grant from floor 100 yields `single` (1..66) or `off` (0) and never
reaches round 2. The grant is `.review/intensity-grant.json` (mode 0600). It
binds the repository, base, head, merge base, diff SHA-256, committed config
digest, resolved floor and reasons, and installed contract versions. Round-1
`begin` applies it only while all of these still match and no `--intensity` was
passed; passing one alongside a matching grant is `INTENSITY_GRANT_CONFLICT`. A
mismatched or malformed grant is ignored, so the snapshot is reviewed at its
floor.

The verdict records the lowering without a new field: the policy keeps the
pre-grant `risk_floor`, and its request has source `human_grant`, requester
`human-direct`, and the typed reason. Only that source may sit below the floor;
every parser, the PR gate's recomputation, and the INCONCLUSIVE restart rule
check it. At N=0 the gate is the existing `skipped`, told apart by that source.

A grant can also be relayed (#166). Before round 1 the controlling agent shows
the `policy-preview` result in its own prompt and asks whether to keep the
floor. Only when the user chooses a lower value does it run `intensity-grant
--relayed`, right after showing the preview; it needs no terminal. Neither path
asks the user to retype a SHA: the grant binds the snapshot it was recorded for,
so a head, base, diff or config that changes before `begin` voids it. The grant
records `channel: relayed`, and the verdict records
requester `human-relayed` instead of `human-direct`. The parser accepts only
these two requesters for `human_grant`. Grants written before #166 carry no
channel and are read as terminal grants. After recording a relayed grant the
agent runs `begin` with no intensity arguments. When the user keeps the floor it
runs `intensity-grant --revoke`, which removes any earlier grant for the
repository so that a keep-floor answer is never overridden by an older grant.

Both paths are friction against policy-following agent mistakes, the threat
model this design already states, not a security boundary: an agent running as
the same user can obtain a pseudo-terminal (for example with `script`), and a
relayed grant is the agent's record of the user's answer. The skill contract
forbids agents from choosing the value, asking again after the user keeps the
floor, or running the terminal path.

## Declared-unexecutable paths

`[unexecutable]` maps a path pattern to a short reason. It declares that the
primary entry path for those files cannot be executed in any available
environment — a cross-compiled embedded target, for example — so no reviewer can
honestly produce execution evidence for it.

At report seal, every changed path matching a declared pattern must appear in
Reviewer B's `coverage.primary_entry_paths`, and the claim it maps to must be
`unverified`. Omission raises `UNEXECUTABLE_PATH_UNCOVERED`; any other claim result
raises `UNEXECUTABLE_PATH_NOT_UNVERIFIED`. Both are retryable within the round.

`refuted` is rejected alongside `supported` because both require execution IDs,
which the declaration asserts cannot honestly exist. Only `unverified` forbids
them, and only `unverified` resolves the gate to `INCONCLUSIVE` rather than
`PASS`; a `refuted` claim would otherwise seal and finalize to `PASS`, which is
the outcome the declaration exists to prevent.

A rename matches when either its pre- or post-rename path matches a declared
pattern, and covering either side satisfies the rule.

The anchor is the diff, not the report: a path the reviewer simply omits fails
instead of passing silently. The declaration is read from the committed config at
the bound HEAD, so an uncommitted edit cannot change it, and its digest is already
part of `PolicyBinding.config_sha256`.

A declaration is enforced only through Reviewer B's sealed coverage, so a
configuration that declares a pattern while setting `[reviewer.B] enabled = false`
would void every declaration silently. `resolve_policy` therefore forces Reviewer B
enabled whenever the committed config declares any pattern and the review mode is
not `off`. It records `unexecutable-requires-reviewer-b` as the first policy reason
under that same condition — before the per-path reasons, because the reason cap
truncates the tail and this entry is the durable record of a security override. An
`off` run dispatches no reviewer at all, so forcing there would record an override
that never happened; the gap described below is why that mode is excluded rather
than made to enforce. The decision is keyed
to the committed config, never to the changed paths: a round transition rejects a
changed reviewer set, and a declared path may legitimately disappear between rounds
because auto-fix scope is allowed to shrink.

Forcing adds a reviewer; it never substitutes for one. The requirement that at least
one reviewer be enabled for a non-`off` mode is therefore checked against the
committed configuration values, before any override applies, so a declaration cannot
stand in for a configuration that enables nobody. A request that failed closed is the
sole exemption, because it overrides the whole request rather than a configuration
decision and already enables every role.

One gap remains open by design. At risk floor 0 the review mode is `off`, there are
no active reviewers, and the gate passes without any report, so a documentation-only
change does not apply the declaration even though the declaration is repository-wide.
That follows from floor 0 having no review at all rather than from the declaration,
but it means a declaration is not a guarantee about every change in the repository.

Deploy order matters: merge the rule before writing any declaration. A declaration
that predates the rule would leave verdicts sealed without it. Narrowing the forcing
condition likewise changes the recomputed policy, and the gate compares the whole
binding rather than its reviewer set, so an `off`-mode verdict sealed by an older
runtime becomes `VERDICT_STALE` until its round is begun again.

## Requested intensity

The only request channel is the tribunal CLI `begin` command. Environment
variables are ignored. The controller passes at most one `--intensity` plus
`--intensity-requester` and `--intensity-reason`.

- An omitted request is value 0 from requester `system`, reason
  `policy-default`.
- An explicit valid request is 0..100 and requires non-empty bounded requester
  and reason fields.
- Repeated, malformed, out-of-range, or incompletely attributed input records a
  fail-closed request and selects intensity 100 with A/B/C enabled.
- A valid explicit zero is retained in the skipped/review verdict with its
  requester and reason.

## Repository config

`.pre-pr-tribunal.toml` uses a deliberately bounded Python-3.10-compatible TOML
subset: quoted strings, booleans, integers, tables, and string-key inline
tables. Unsupported TOML features, duplicate keys, unknown sections/keys, or
unknown values are errors.

```toml
[policy]
"docs/**" = "off"
"hooks/**" = "iterative"
"**" = "single"

[unexecutable]
"drivers/**" = "NO_CROSS_SDK"

[reviewer.A]
enabled = true
model = { claude = "inherit", codex = "inherit" }

[reviewer.B]
enabled = true
model = { claude = "inherit", codex = "inherit" }

[reviewer.C]
enabled = false
model = { claude = "inherit", codex = "inherit" }
```

Absent config defaults to A/B enabled, C disabled, no repository floor raises,
and distinct A/B models (`opus`/`sonnet` on Claude,
`gpt-5.6-sol`/`gpt-6-astra` on Codex) so role labels are not the only source of
review diversity. At least one reviewer must be enabled for a non-off mode.
Model names come from a bounded runtime allowlist; unknown names stop before
dispatch rather than silently falling back.

All three role keys remain present in the verdict. Inactive roles have an
explicit `disabled` slot with no report, receipt, attempt, or error. Context,
submit, failure, and validation operations reject a disabled role. Finalize and
the gate require every active slot sealed and every inactive slot disabled.

## Mode transitions

### off

`begin --round 1` writes a terminal skipped verdict. No reviewer context or
report exists. The gate allows PR creation only while the recomputed exact
snapshot policy remains off.

### single

Only round 1 is valid. PASS permits PR creation. FAIL cannot be rerun on the
same snapshot. After a committed fix changes the snapshot, a new round-1
lifecycle is allowed. This prevents repeated sampling of identical code while
making the mode usable after fixes. Commit metadata alone is not a changed
reviewed snapshot: the base or merge-base and diff digest must change.

### iterative

The current round 1 -> decisions -> round 2 -> decisions -> round 3 state
machine remains. Reviewer decisions and replacement findings are role-local.
Round 3 failure is terminal for that snapshot. A changed snapshot may start a
new round-1 lifecycle. An empty commit does not reset this terminal boundary.

If snapshot/config policy changes make the active reviewer set or effective
policy incompatible with a later-round transition, the transition fails and
the controller must begin a new round-1 lifecycle.

### inconclusive empirical verification

When active Reviewer B returns any `unverified` claim and there are no
HIGH/CRITICAL findings, finalize produces `inconclusive`, not PASS. A better
evidence bundle or newly available safe validation can restart round 1 even on
the same code snapshot, but never at a lower effective intensity. This is
intentionally distinct from both a code defect and a successful review.

Reviewer B must enumerate primary documented entry paths and represent every
one as supported, refuted, or unverified. Authenticated reusable evidence keeps
its existing provenance rules; unsafe or unavailable execution never becomes
support by inference. Report contract 4 requires at least one empirical claim
and an explicit complete primary-entry-path-to-claim mapping before Reviewer B
can seal, so an empty report cannot authorize PASS.

### terminal non-pass user override

A normal PASS or policy-off SKIPPED verdict remains the automatic authorization
path. After finalization and telemetry closure, a human may explicitly authorize
one canonical PR-create attempt for either a round-3 FAIL with open blockers or
a finalized INCONCLUSIVE verdict. No other gate result is eligible: earlier
round failures, pending or incomplete reviews, dirty or stale snapshots, unsafe
or invalid state, repository mismatch, and ambiguous commands remain closed.

The controller shows the bound head, diff digest, round, terminal status, and
blocker count before asking. A direct operator types an exact terminal
confirmation; a policy-following agent records the explicit answer as a
`human-relayed`-style channel. The latter is operational friction and an audit
record, not cryptographic proof that a human was present.

The private `pr-override-grant.json` is a current-user-owned, regular,
non-symlink mode-`0600` file bound to the exact verdict bytes, repository,
base/head/merge-base, diff, round, gate, installed contract, runtime, and
channel. The hook first validates the unchanged snapshot and exact canonical
command, then consumes the matching grant atomically under the review lock.
Consumption precedes command execution, so failure does not make the grant
reusable. Mismatch is a denial; unsafe grant storage fails closed. This creates
one deliberate exception at the terminal policy boundary without weakening any
integrity or command-recognition boundary.

## Reviewer independence and scope

Reviewer contexts contain the immutable snapshot/diff contract, the role's own
prior findings and decisions, policy-selected model, and Reviewer B evidence.
They do not contain a controller-authored change summary or peer conclusions.

Reviewer A covers correctness, security, contracts, and state transitions.
Reviewer B covers executable behavior and documented entry paths. Reviewer C
is optional and covers irreversible scope/design excess, not ordinary cleanup.
Prompts require enumeration of shared-state writers and both persistent-state
and transition scenarios when relevant, addressing the blind spots observed in
#121.

## PR appendix

Finalize/status returns a bounded deterministic Markdown fragment containing
active reviewers' MEDIUM/LOW findings. It is advisory and never changes the
blocker calculation. Controllers append it to the PR body when non-empty.

## Compatibility

Schemas 1-4 remain readable so installed-state recovery and stale-verdict
diagnostics stay bounded. Only schema 5 matches the current contract and can
authorize a new PR. Existing schema-v4 evidence bundles and exact-byte reviewer
receipts retain their validation semantics.

## Validation

Tests cover policy precedence, path kinds, intensity attribution/fail-closed
handling, default and custom reviewer activation, model rejection, off verdict
binding, single retry, iterative continuity, active-only finalize, unverified
inconclusive outcome, PR appendix output, gate policy drift, installer
packaging, both hook adapters, and the existing #113 evidence canaries.

## Non-goals

- Cross-backend dispatch inside one tribunal run. Per-runtime model selection
  is supported; backend mixing needs a separate authenticated response path.
- Replacing PR-layer linters/reviewers with tribunal findings.
- Weakening the exact-byte report, private-file, evidence, three-round, or
  direct-PR command-binding contracts.
