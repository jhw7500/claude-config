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
| 0 | off | documentation-only Markdown/text under documentation paths |
| 50 | single | ordinary recognized source or test changes |
| 100 | iterative | tribunal/config/hooks/workflows/deploy/auth/data/dependency changes; rename/copy/type/unknown status; binary, symlink, submodule, mixed or unknown paths |

Every changed path is classified, including both sides of a rename. The final
floor is the maximum. Ambiguous Git metadata or classification errors resolve
to 100.

Repository `[policy]` patterns may raise the floor by mapping to `off`,
`single`, `iterative`, or integer 0..100. They cannot reduce the built-in
value. `.pre-pr-tribunal.toml` itself is always floor 100, preventing a policy
weakening change from reviewing itself at the weakened level.

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
enabled whenever the committed config declares any pattern. It records
`unexecutable-requires-reviewer-b` as the first policy reason whenever a pattern
is declared — before the per-path reasons, because the reason cap truncates the
tail and this entry is the durable record of a security override. The decision is keyed
to the committed config, never to the changed paths: a round transition rejects a
changed reviewer set, and a declared path may legitimately disappear between rounds
because auto-fix scope is allowed to shrink.

One gap remains open by design. At risk floor 0 the review mode is `off`, there are
no active reviewers, and the gate passes without any report, so a documentation-only
change does not apply the declaration even though the declaration is repository-wide.
That follows from floor 0 having no review at all rather than from the declaration,
but it means a declaration is not a guarantee about every change in the repository.

Deploy order matters: merge the rule before writing any declaration. A declaration
that predates the rule would leave verdicts sealed without it.

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
