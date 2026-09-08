# Pre-PR Gate Target Binding Design

## Goal

Close the five Round 3 HIGH findings without adding a PR execution wrapper or a
release workflow. A passing Tribunal verdict may authorize only one canonical,
direct `/usr/bin/gh pr create` command whose repository, base, head, working tree, and
reviewed snapshot remain the values represented by that verdict.

## Scope

This amendment covers:

- repository/base/head target binding for the PR command;
- GitHub CLI default-repository selection and the `gh pr new` alias;
- rejection of shell work that can run before the PR command;
- fail-closed handling for Bash `coproc` and dynamic leading assignments;
- ANSI-C and GNU `env --split-string` candidate recognition;
- fail-closed handling when a hook payload exceeds 1 MiB;
- exact alignment between snapshot producer text/path bounds and the verdict
  parser contract;
- contract and canary documentation for the supported command form.

It does not add a wrapper, dependency, permission, environment variable,
network endpoint, release step, or fix for an unrelated intermittent probe
timeout.

## Canonical command envelope

The supported form is one simple command with the literal `/usr/bin/gh`
executable, no execution wrapper, and an explicit literal base:

```sh
/usr/bin/gh pr create --base master --fill
```

The scanner continues to return `NO_MATCH` for data and unrelated commands.
Once an executable `gh pr create` candidate is present, it returns
`AMBIGUOUS_CANDIDATE` for any envelope that cannot preserve the reviewed
snapshot until `gh` starts.

| Candidate property | Result |
|---|---|
| One simple `/usr/bin/gh` command with no wrapper, redirection, or command substitution | Eligible for verdict checks |
| Bare `gh`, another executable path, or an execution wrapper | `AMBIGUOUS_CANDIDATE` |
| Another non-empty command before or after it | `AMBIGUOUS_CANDIDATE` |
| Subshell or command-substitution execution context | `AMBIGUOUS_CANDIDATE` |
| Redirection on the candidate command | `AMBIGUOUS_CANDIDATE` |
| `env -C`/`--chdir` or `time -o`/`--output` before `gh` | `AMBIGUOUS_CANDIDATE` |
| Dynamic shell `-c` script or an operand option before `-c` | `AMBIGUOUS_CANDIDATE` |
| GNU `env -S`/`--split-string`, including clusters, accepted long abbreviations, and trailing argv, containing a candidate | `AMBIGUOUS_CANDIDATE` |
| Target-changing `gh` global option before `pr`, between `pr` and `create`, or after `create` | `AMBIGUOUS_CANDIDATE` |
| Static quote/backslash removal produces a noncanonical executable | `AMBIGUOUS_CANDIDATE` |
| GNU split-string uses `\c` termination | `AMBIGUOUS_CANDIDATE` |
| Shell `-c`, brace/glob expansion, or dynamic content in a bound candidate | `AMBIGUOUS_CANDIDATE` |
| Official `gh pr new` alias | `AMBIGUOUS_CANDIDATE` |
| Candidate launched by Bash `coproc`, including named compound forms | `AMBIGUOUS_CANDIDATE` |
| Dynamic or shell-expanding leading assignment before a bound candidate | `AMBIGUOUS_CANDIDATE` |

A trailing newline or separator with no other executable segment does not by
itself create a stale snapshot and may remain eligible.

## Target binding

The first scan answers only whether a PR-create candidate exists and whether
its shell context is safe. After the current verdict is read, the gate rescans
with `expected_base=verdict.base_ref`. The bound scan requires exactly one
literal `--base VALUE`, `--base=VALUE`, `-B VALUE`, or `-BVALUE`, and the value
must equal the verdict base exactly.

The bound scan rejects:

- a missing, duplicated, dynamic, or mismatched base;
- `--head`, `--head=`, `-H`, or an attached `-HVALUE`;
- `--repo`, `--repo=`, `-R`, or an attached `-RVALUE`;
- `--hostname`, `--config`, and their attached value forms;
- leading `GH_REPO`, `GH_HOST`, `GH_CONFIG_DIR`, `GIT_DIR`, `GIT_WORK_TREE`,
  or `GIT_COMMON_DIR` assignments, including assignments consumed by `env`;
- inherited `GH_REPO`, `GIT_DIR`, `GIT_WORK_TREE`, or `GIT_COMMON_DIR`, and an
  inherited `GH_HOST` other than the repository's supported `github.com` host.
- shell- or `env`-assigned Git execution variables that can alter repository or
  ref resolution, including `GIT_CONFIG_*`, and their inherited equivalents.

GitHub CLI reads `remote.<name>.gh-resolved=base` from effective Git config.
Snapshot capture and revalidation inspect the same system, global, local, and
worktree scopes and accept no such marker or the single exact
`remote.origin.gh-resolved=base` marker. A
non-origin, duplicate, malformed, or unreadable marker is
`REPOSITORY_UNSUPPORTED`; it cannot redirect a passing verdict to another
remote.

An inherited `GH_HOST=github.com` and a private runtime `GH_CONFIG_DIR` do not
change the explicitly bound target and remain supported. The command itself may
not assign either variable because that would change execution relative to the
validated adapter environment.

`--head` is intentionally absent: every newly written snapshot and verdict
persists the canonical `refs/heads/...` symbolic HEAD as `head_ref` and compares
it with the current branch as well as the HEAD SHA. A terminal legacy FAIL
without `head_ref` may migrate only while beginning its next repair round; a
legacy verdict cannot authorize PR creation. Content options remain supported
when literal, while dynamic values and shell expansion are ambiguous because
their evaluation may execute work or form target-changing arguments.

## Scanner behavior

`scan_pr_create(command, expected_base=None)` keeps its public classification
shape. The optional expected base activates target-policy validation. Internal
context scanning preserves whether a candidate was nested, accompanied by
another command segment, redirected, or preceded by a mutating wrapper option.

ANSI-C words are treated conservatively. Multiple ANSI-C fragments that can
jointly form the executable, `pr`, and `create` words are ambiguous rather than
ignored. A dynamic ANSI-C shell `-c` script is ambiguous when it can conceal a
candidate. GNU split-string operands are scanned as executable text with the
existing recursion and token budget. Supported short-option clusters and
unambiguous long-option abbreviations are normalized first, and argv after the
split operand remains part of the constructed command. A candidate inside that
alternate argv construction is ambiguous. Shell options that consume an
operand are skipped before locating `-c`; supported `gh` global options are
recognized both before `pr` and between `pr` and `create`.
Static quote or backslash removal in executable position is interpreted as the
resulting argv name in both the normal parser and bounded streaming fallback.
The GNU split-string `\c` control escape is deliberately unsupported and fails
closed instead of attempting a partial emulation.
The official `gh pr new` alias is treated as a PR-create candidate but is not
eligible for the canonical pass envelope. Bash `coproc` candidates are unsafe
whether direct or compound; the bounded streaming fallback retains them as
ambiguous when its token budget is exhausted. Dynamic leading simple-command
or `env` assignments are also ambiguous for a bound candidate because shell
expansion can execute before `gh` starts.

Snapshot fields written into a verdict use the same decoded-text domain as the
strict reader: UTF-8, NFC, no Unicode `Cc`/`Cs`, and the reader's byte bounds.
Changed paths additionally reject absolute paths, backslashes, and empty,
`.` or `..` components. Round 1 records at most 1,024 unique initial paths, so
the producer cannot create a verdict that its own parser later rejects.

## Oversized hook payloads

The adapter already reads at most `MAX_STDIN_BYTES + 1`. If that read produces
more than `MAX_STDIN_BYTES`, the adapter emits the existing bounded native deny
payload with `COMMAND_AMBIGUOUS`. It does not attempt to parse, reflect, or log
the oversized input. Malformed inputs within the limit remain silent when no
command can be extracted reliably.

## Verification

Regression tests exercise real scanner, gate, and copied-adapter behavior:

- a passing verdict denies repository, base, and head overrides;
- a passing verdict denies prelude, redirection, and substitution mutations;
- the exact 1 MiB boundary is parsed normally and a larger payload is denied;
- combined ANSI-C fragments and ANSI-C shell scripts do not return `NO_MATCH`;
- GNU split-string spellings, clusters, accepted abbreviations, and trailing
  argv constructions do not return `NO_MATCH`;
- shell option operands and interposed `gh` global options cannot conceal a
  candidate;
- quote-removed executable names survive normal and token-limit scanning, and
  split-string `\c` termination cannot return `NO_MATCH`;
- `gh pr new`, direct/named `coproc`, and dynamic leading assignments are
  ambiguous in both scanner and adapter coverage;
- absent or origin GitHub CLI defaults preserve the canonical target while a
  non-origin local, worktree, or global default is rejected during capture and
  gate revalidation;
- base, symbolic HEAD, changed paths, and initial-path count stay inside the
  strict verdict parser's accepted domain;
- the canonical explicit-base command still reaches `PASS`;
- unrelated shell data and malformed in-limit payloads retain their existing
  no-output behavior.

After targeted and repository tests pass, the exhausted Round 3 `.review`
directory is moved intact to a private recoverable archive outside the
worktree. A new Tribunal begins at round 1 on the new clean commit. Push and PR
creation occur only if that new Tribunal returns PASS.
