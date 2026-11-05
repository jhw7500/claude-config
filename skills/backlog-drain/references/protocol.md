# Backlog Drain Protocol

## Helper Envelope

Invoke the installed helper through Python. Every successful command writes one canonical JSON line to stdout:

```json
{"ok":true,"result":{}}
```

Ordinary validation failures write `{"error":"...","ok":false,"status":"ERROR"}` to stderr and exit 1. Argument errors exit 2. A durability-indeterminate operation writes status `PAUSED` and exits 3. Input files and `-` stdin are capped at 1 MiB; output is capped at 2 MiB. Unknown JSON keys are rejected at operation boundaries. The helper never runs `next_action`, shell text, Task lifecycle actions, Git mutation, or native dispatch.

## Exact Commands

```text
backlog-checkpoint.py snapshot read --path ABS_CHECKPOINT
backlog-checkpoint.py snapshot write --path ABS_CHECKPOINT --input FILE|- --expected-generation new|N

backlog-checkpoint.py evidence write --state-root ABS_STATE --run-id RUN_ID --input FILE|-
backlog-checkpoint.py evidence read --state-root ABS_STATE --run-id RUN_ID --input REFERENCE_FILE|-

backlog-checkpoint.py admission acquire --state-root ABS_STATE --worktree-root ABS_ROOT --input RECORD_FILE|-
backlog-checkpoint.py admission release --state-root ABS_STATE --worktree-root ABS_ROOT --input RECORD_FILE|-
backlog-checkpoint.py admission reconcile --state-root ABS_STATE --worktree-root ABS_ROOT --input PAYLOAD_FILE|-

backlog-checkpoint.py git inspect --root ABS_ROOT --branch BRANCH --record-root ABS_ROOT
backlog-checkpoint.py git collect --root ABS_ROOT --input PATHS_FILE|- [--checkpoint-path ABS_CHECKPOINT]
backlog-checkpoint.py git bind --root ABS_ROOT --parent-head OID --commit-oid OID --input MANIFEST_FILE|- [--checkpoint-path ABS_CHECKPOINT]

backlog-checkpoint.py queue plan --repo OWNER/REPO [--explicit-edges EDGE_FILE|-]
backlog-checkpoint.py queue plan --input GRAPH_FILE|-
```

`evidence write` input is an evidence object; its returned `{ref,sha256,size}` is used unchanged by the snapshot and by `evidence read`. The exact remaining payloads follow.

## Canonical Payload Schemas

JSON objects reject unknown or missing keys. `hex32`, `hex40`, and `hex64` mean lowercase hexadecimal strings of that exact length. Integers never accept booleans. Absolute paths are exact physical paths. Serialize digest inputs as UTF-8 JSON with sorted keys, separators `(',', ':')`, `ensure_ascii=false`, and no trailing newline.

### Snapshot write

```json
{
  "schema":1,"run_id":"hex32","generation":0,
  "phase":"IMPLEMENT","phase_status":"started","attempt":0,
  "next_action":"INDEPENDENT_REVIEW","blockers":[],
  "supervisor_identity":{"runtime":"codex","origin_adapter":"codex","host":"HOST","session_fingerprint":"hex64"},
  "binding":{"issue_url":"URL","task_id":"TASK","claim_id":"CLAIM","repo_root":"/ABS/REPO","worktree_root":"/ABS/WORKTREE"},
  "git":{"branch":"BRANCH","base_head":"hex40","observed_head":"hex40","source_count":0,"path_set_digest":"hex64","change_digest":"hex64","commit_parent":null,"result_commit":null},
  "child":{"child_id":null,"phase":"IMPLEMENT","attempt":0,"observed_status":"quiescent"},
  "evidence":{"ref":"evidence/hex64.json","sha256":"hex64","size":0}
}
```

Replace every placeholder with a live observation or helper result. `phase` and `child.phase` are one of `SELECTED|PREFLIGHT|APPROACH_REVIEW|IMPLEMENT|INDEPENDENT_REVIEW|VERIFY|COMMIT|READY_FOR_TRIBUNAL|PAUSED`; `phase_status` is `started|verified`; `attempt` is `0|1`; `next_action` is a phase or `STOP`; child status is `running|quiescent|interrupted|completed|unknown`; runtime and origin adapter must both be the same `claude` or `codex`. `commit_parent` and `result_commit` are null or hex40. Use Git collect's `count`, `path_set_digest`, and `change_digest` unchanged. The evidence ref must be exactly `evidence/<sha256>.json` and its size must match the canonical evidence bytes. `session_fingerprint = SHA256(canonical JSON [origin_adapter, documented_session_id, host])`; raw session ID is never stored.

Snapshot reads and writes use exactly `binding.worktree_root/HANDOFF.backlog-<run_id>.md`, as an absolute canonical path with no alias or nested directory. Within an existing file, CAS preserves `run_id`, every `supervisor_identity` and `binding` field, `git.branch`, and the frozen `git.base_head`. Phase/status, attempt, next action, blockers, child observations, evidence, observed HEAD, source summaries, and commit-result fields may advance. A changed supervisor or Claim requires the separately authorized successor procedure and a new generation-0 run file; it cannot replace the old file's identity.

### Admission acquire/release

```json
{
  "schema":1,"run_id":"hex32","root_key":"hex64",
  "checkpoint_path":"/ABS/WORKTREE/HANDOFF.backlog-hex32.md",
  "owner":{"issue_url":"URL","task_id":"TASK","claim_id":"CLAIM","task_owner":"OWNER","branch":"BRANCH","worktree_root":"/ABS/WORKTREE","session_fingerprint":"hex64"}
}
```

`root_key = SHA256(UTF-8 exact physical worktree path)`. The run ID in the exact checkpoint filename must equal the record run ID. Owner fields must match the live Task observations and snapshot binding; they do not grant authority. Acquire and release receive the same exact record bytes.

Before release, write an evidence object with these three REQUIRED top-level fields and store its returned reference in the terminal snapshot. Copy the entire `git collect` result into `source_manifest` and the entire successful `git bind` result into `commit_binding`; do not rebuild either object's digests. The following one-source example shows their complete shapes:

```json
{
  "admission_release":{"schema":1,"run_id":"hex32","task_owner":"OWNER","session_fingerprint":"hex64","snapshot_generation":0,"tools_quiescent":true,"writers_quiescent":true},
  "source_manifest":{"parent_head":"P_hex40","paths":["src/example.py"],"count":1,"sources":[{"change":"modify","mode":"100644","path":"src/example.py","sha256":"source_hex64"}],"path_set_digest":"paths_hex64","change_digest":"changes_hex64"},
  "commit_binding":{"commit_parent":"P_hex40","result_commit":"C_hex40","source_count":1,"path_set_digest":"paths_hex64","change_digest":"changes_hex64"}
}
```

Release additionally requires snapshot phase `READY_FOR_TRIBUNAL`, status `verified`, next action `STOP`, and child phase/attempt equal to the current snapshot with status `quiescent|completed`. Set non-null `git.commit_parent = git.base_head = P` and `git.result_commit = git.observed_head = C`; the source count and both digests must equal the recorded binding. Release itself freshly inspects the exact branch/worktree and reruns `git bind` against `source_manifest`, C, and this exact checkpoint. Null, stale, mismatched, or dirty Git state preserves the active admission and fails. Quiescence booleans must be literal JSON `true`, not integers.

### Admission reconcile

Input has exactly the prior admission record, the new admission record, and this proof:

```json
{
  "expected":{"schema":1,"run_id":"hex32","root_key":"hex64","checkpoint_path":"/ABS/WORKTREE/HANDOFF.backlog-hex32.md","owner":{"issue_url":"URL","task_id":"TASK","claim_id":"OLD_CLAIM","task_owner":"OLD_OWNER","branch":"BRANCH","worktree_root":"/ABS/WORKTREE","session_fingerprint":"hex64"}},
  "successor":{"schema":1,"run_id":"newhex32","root_key":"hex64","checkpoint_path":"/ABS/WORKTREE/HANDOFF.backlog-newhex32.md","owner":{"issue_url":"URL","task_id":"TASK","claim_id":"NEW_CLAIM","task_owner":"NEW_OWNER","branch":"BRANCH","worktree_root":"/ABS/WORKTREE","session_fingerprint":"hex64"}},
  "proof":{"schema":1,"recovery_result":"succeeded","prior":{"admission_sha256":"hex64","snapshot_sha256":"hex64","snapshot_generation":0},"successor":{"snapshot_sha256":"hex64","snapshot_generation":0},"quiescence":{"children":"quiescent","tools":"quiescent","writers":"quiescent"},"git":{"branch":"BRANCH","worktree_root":"/ABS/WORKTREE","observed_head":"hex40","change_digest":"hex64"}}
}
```

`admission_sha256` hashes the canonical prior admission JSON bytes. Each `snapshot_sha256` hashes the full stored checkpoint file bytes, including its Markdown prefix/suffix. Snapshot generations and successor Git fields must match the decoded files. The successor evidence object must contain `{"admission_successor":{"prior_run_id":"hex32","prior_snapshot_sha256":"hex64","prior_snapshot_generation":0}}` before its reference is placed in the successor snapshot. Successor keeps the same root key, Issue, Task, and worktree, but uses a new run ID and Claim. `recovery_result` records an already-authorized external recovery; this template creates no approval.

Git collect input is `{"paths":["repo/relative/path"]}`. Git bind input is the exact collected manifest. When the run checkpoint exists, both commands receive the same exact absolute `--checkpoint-path`. Only a current-UID, mode-0600, root-level regular file named `HANDOFF.backlog-<decoded-run-id>.md`, bound to that exact worktree and still untracked, is excluded. Other HANDOFF files, tracked artifacts, foreign paths, symlinks, or dirty paths remain errors.

### New-run clean baseline

Git collection and post-commit binding reject any tracked index entry with `assume-unchanged` or `skip-worktree`, including otherwise clean entries. These flags can suppress source changes from status output. Preserve the index and source bytes; do not clear flags automatically.

After `git inspect` freezes the exact worktree and HEAD, but before creating a new checkpoint or acquiring admission, run `git collect --root ABS_ROOT --input FILE|-` with exactly `{"paths":[]}` and without `--checkpoint-path`. A successful result must have the frozen `parent_head`, `count:0`, `paths:[]`, and `sources:[]`; preserve that manifest as the clean baseline. Any staged, unstaged, or untracked source state makes empty-allowlist collection fail. Record `PAUSED`, leave every byte and index entry untouched, and do not reset, clean, discard, stash, or adopt those changes into the new run.

This gate applies only when no exact run exists yet. To resume one exact existing run, read its named checkpoint, revalidate live Task/Claim/session/worktree scope, and collect only that run's owned expected edits while passing its exact checkpoint path. Do not require an existing run with preserved expected edits to become globally clean, and do not use new-run bootstrap to bypass the fault-recovery or admission-reconciliation rules below.

For `git inspect`, `--record-root` is the exact physical path expected in `git worktree list --porcelain -z`. The result's `worktree_ref` is the symbolic Git branch ref such as `refs/heads/task/31`; neither value is the launcher's opaque `wt-*` worktree identifier. Compare the live launcher mapping separately. This helper validates Git consistency only and grants no Registry or Task authority.

`queue plan --repo` runs the read-only official GitHub GET/pagination collector; optional explicit-edge input is a JSON array of exact `[blocker_url, dependent_url]` pairs. `queue plan --input` takes exactly `issues` and `dependencies` and calls the deterministic pure planner. Publish an ordered queue only when result `status` is `COMPLETE`. `CLOSED_SATISFIED` is informational and does not block its Issue. An `INCOMPLETE` result is never reconstructed into a ready order.

## Native Capability Mapping

Use only controls actually exposed in the current runtime. Native identifiers are runtime-local observations; never infer `agentId`/`taskId`, translate an SDK/TUI identifier, or claim parity between runtimes.

| Runtime | Fresh fault recovery | Stop and terminal proof |
|---|---|---|
| Codex | Spawn an explicit installed role with `fork_turns=none`; pass only checkpoint, current Git coordinates, bounded next step, scope, and report contract. | Interrupt the exact active turn only. The object may remain available, so separately observe child running/idle state and every tool/writer terminal state before recovery. Do not use follow-up to wake the faulted child. |
| Claude | Use the actually exposed native `Agent` fresh/non-fork path with an explicit role and bounded recovery context. | Use exact-target `TaskStop`, then observe that same run's terminal child and tool/writer state. Never resume or `SendMessage` to the faulted child. |

If the required fresh/non-fork dispatch, exact stop target, session identity, or separate terminal observations are unavailable, record unsupported and `PAUSED`. Do not replace them with model SDK calls, TUI guesses, background polling, or a new orchestration layer.

## Resume and Freshness

Resume always names the exact checkpoint with `snapshot read --path`; never select by mtime or glob. A newer coexisting `HANDOFF.backlog-*.md` may be discovered by the general PreCompact hook, but that hook is not a backlog-drain selector and does not change this rule.

After reading, independently recheck live Task/Claim ownership, session fingerprint inputs, canonical worktree/branch, child/tool/writer quiescence, and Git state. JSON identity fields are observations, not approval. A changed supervisor requires the separately approved Task recovery flow before admission reconciliation.

For READY release with no current child, record `child_id=null`, READY phase/current attempt, and `quiescent` only after actual prior child/tool/writer quiescence was observed. Preserve earlier real IDs in immutable evidence; never retag an old child or create a shape-only verifier.

## Evidence Freshness

Review and verification evidence binds the source manifest's parent HEAD, path-set digest, change digest, cwd, argv, exit, and raw-log reference. A source content/mode/path-set or parent change invalidates dependent evidence. A commit with identical manifest content does not invalidate it; `git bind` supplies the unique post-commit binding. Any ambiguity or indeterminate publish is `PAUSED`, with no blind retry.
