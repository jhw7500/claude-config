# Controller happy path (worked example)

This is a non-normative example of one uneventful round on Claude: a new round 1, no evidence reuse, enough capacity for every active reviewer at once, and no runtime acceptance signal. SKILL.md is authoritative. Run each block so that a non-zero exit ends the sequence (for example with `set -e`, or by checking `$?` before the next block): a non-zero exit is a stop, not a warning. When any command below exits non-zero, a check fails, or a reviewer does not return one terminal report, stop following this page and apply the SKILL.md failure policy and telemetry lifecycle.

Only the controller reads this page. Reviewers receive their own role reference, the report schema, and their projected context, never this file.

## Setup

```bash
CLI="$HOME/.local/share/claude-config/pre_pr_tribunal/cli.py"
PY=/usr/bin/python3
span_id() { "$PY" -c 'import json,sys; print(json.load(sys.stdin)["span_id"])'; }
PRIVATE="$(mktemp -d)"; chmod 700 "$PRIVATE"   # controller-private files, never inside a view
```

Run `begin` as in SKILL.md step 3 and keep `RUN_ID` (`telemetry.run_id`), the snapshot `head_sha` as `BOUND_HEAD`, and `active_reviewers`. The loops below use `A B` for the default active set; use the exact `active_reviewers` array instead.

## Contexts and views

```bash
for REVIEWER in A B; do
  (umask 077; "$PY" "$CLI" context --reviewer "$REVIEWER" > "$PRIVATE/context-$REVIEWER.json") || exit
done
VIEW_ROOT="$(mktemp -d)"; chmod 700 "$VIEW_ROOT"
for REVIEWER in A B; do
  VIEW="$VIEW_ROOT/view-$REVIEWER"
  SPAN_ID="$("$PY" "$CLI" telemetry-start --run-id "$RUN_ID" --stage view_create --reviewer "$REVIEWER" --attempt 1 | span_id)"
  git worktree add --detach "$VIEW" "$BOUND_HEAD"; rc=$?
  if [ "$rc" -ne 0 ]; then exit "$rc"; fi   # step 6 failure handling; the drain's telemetry-recover closes this span
  "$PY" "$CLI" telemetry-finish --run-id "$RUN_ID" --span-id "$SPAN_ID" --outcome success
  test "$(git -C "$VIEW" rev-parse HEAD)" = "$BOUND_HEAD" || exit 1
  test -z "$(git -C "$VIEW" status --porcelain -uall)" || exit 1
  test ! -e "$VIEW/.review" || exit 1
done
```

Check that each context's `review_policy.model` equals the model `begin` returned for that reviewer.

## Dispatch

Immediately before each native dispatch, open both spans (the acceptance signal is unavailable), with `ATTEMPT=1` for the first request:

```bash
DISPATCH_SPAN_ID="$("$PY" "$CLI" telemetry-start --run-id "$RUN_ID" --stage reviewer_dispatch_wait --reviewer "$REVIEWER" --attempt "$ATTEMPT" | span_id)"
TOTAL_SPAN_ID="$("$PY" "$CLI" telemetry-start --run-id "$RUN_ID" --stage reviewer_total --reviewer "$REVIEWER" --attempt "$ATTEMPT" | span_id)"
```

Then start one native `Agent` call per reviewer, without a `name`, with the policy-selected model. A prompt that has worked contains only:

- the view path, and the instruction to run every command inside it and to write nothing inside it or beside it (scratch goes in the reviewer's own `mktemp -d`);
- a first check that `HEAD` is `$BOUND_HEAD` and `git status --porcelain -uall` is empty, answering `VIEW_INVALID` otherwise;
- the snapshot (repository, base SHA, head SHA, diff SHA-256, round) and the `git diff <base> <head>` to review;
- paths to its role reference, `references/report-schema.md`, and its own context file;
- the home-path rule from its role reference: write `$HOME`, never its expansion;
- that the final message is exactly one physical line of minified JSON.

## Seal each terminal report

Claude Code completion notifications HTML-escape the agent's text (for example `&&` arrives as `&amp;&amp;`), so the notification body is not the exact terminal response. Take the final assistant message bytes from the agent's own output transcript, write them to `$PRIVATE/response-$REVIEWER.raw` with mode `0600`, and confirm the file is a current-user-owned regular file.

```bash
"$PY" "$CLI" telemetry-finish --run-id "$RUN_ID" --span-id "$DISPATCH_SPAN_ID" --outcome incomplete --reason-code RUNTIME_SIGNAL_UNAVAILABLE
"$PY" "$CLI" telemetry-finish --run-id "$RUN_ID" --span-id "$TOTAL_SPAN_ID" --outcome success
RESPONSE="$PRIVATE/response-$REVIEWER.raw"
"$PY" "$CLI" submit-report --reviewer "$REVIEWER" --run-id "$RUN_ID" --attempt "$ATTEMPT" < "$RESPONSE" > "$PRIVATE/receipt-$REVIEWER.json"
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"   # a format code keeps the view for a same-handle retry
SEALED="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["raw_sha256"])' "$PRIVATE/receipt-$REVIEWER.json")"
test "$SEALED" = "$(sha256sum < "$RESPONSE" | cut -d' ' -f1)" || { echo REPORT_BYTES_MISMATCH; exit 1; }
```

Only after that receipt check, release the terminal reviewer as SKILL.md step 6 requires and clean up its exact view without `--force`:

```bash
VIEW="$VIEW_ROOT/view-$REVIEWER"
SPAN_ID="$("$PY" "$CLI" telemetry-start --run-id "$RUN_ID" --stage view_cleanup --reviewer "$REVIEWER" --attempt 1 | span_id)"
git worktree remove "$VIEW"; rc=$?
if [ "$rc" -eq 0 ]; then OUTCOME=success; else OUTCOME="failure --reason-code VIEW_CLEANUP_REFUSED"; fi
"$PY" "$CLI" telemetry-finish --run-id "$RUN_ID" --span-id "$SPAN_ID" --outcome $OUTCOME
rmdir "$VIEW_ROOT" 2>/dev/null || true   # never delete a non-empty VIEW_ROOT
```

## Finalize and close

After `status` shows every active reviewer sealed:

```bash
FINALIZE_SPAN_ID="$("$PY" "$CLI" telemetry-start --run-id "$RUN_ID" --stage finalize --attempt 1 | span_id)"
for REVIEWER in A B; do
  SPAN_ID="$("$PY" "$CLI" telemetry-start --run-id "$RUN_ID" --stage report_validation --reviewer "$REVIEWER" --attempt 1 | span_id)"
  STORED="$("$PY" "$CLI" validate-report --reviewer "$REVIEWER" --source stored | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["raw_sha256"])')"
  SEALED="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["raw_sha256"])' "$PRIVATE/receipt-$REVIEWER.json")"
  INBOX=".review/inbox/round-1/$REVIEWER.json"
  if [ -n "$STORED" ] && [ "$STORED" = "$SEALED" ] && [ ! -L "$INBOX" ] \
    && [ "$(stat -c '%U %a %F' "$INBOX")" = "$(id -un) 600 regular file" ] \
    && [ "$(sha256sum < "$INBOX" | cut -d' ' -f1)" = "$SEALED" ]; then
    "$PY" "$CLI" telemetry-finish --run-id "$RUN_ID" --span-id "$SPAN_ID" --outcome success
  else
    "$PY" "$CLI" telemetry-finish --run-id "$RUN_ID" --span-id "$SPAN_ID" --outcome failure --reason-code REPORT_BYTES_MISMATCH
    "$PY" "$CLI" telemetry-finish --run-id "$RUN_ID" --span-id "$FINALIZE_SPAN_ID" --outcome failure --reason-code REPORT_BYTES_MISMATCH
    exit 1   # integrity stop: do not finalize
  fi
done
"$PY" "$CLI" finalize > "$PRIVATE/finalize.json" 2> "$PRIVATE/finalize.err"; rc=$?
if [ "$rc" -eq 0 ]; then
  "$PY" "$CLI" telemetry-finish --run-id "$RUN_ID" --span-id "$FINALIZE_SPAN_ID" --outcome success
else
  PRIMARY_CODE="$(sed -n 's/^PRE_PR_TRIBUNAL:\([A-Z0-9_]*\).*/\1/p' "$PRIVATE/finalize.err" | head -n1)"
  "$PY" "$CLI" telemetry-finish --run-id "$RUN_ID" --span-id "$FINALIZE_SPAN_ID" --outcome failure --reason-code "$PRIMARY_CODE"
fi
```

On a PASS, `telemetry-close --run-id "$RUN_ID" --outcome success`, then report the bound HEAD, the diff SHA-256, and a non-empty `pr_appendix` as SKILL.md step 8 says. Any other finalizer result uses the matching close row in SKILL.md.
