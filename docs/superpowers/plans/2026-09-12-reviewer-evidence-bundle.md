# Reviewer Evidence Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse authenticated controller validations in Reviewer B without weakening snapshot binding, reviewer isolation, or final gate integrity (Issue #113).

**Architecture:** Capture commands in a bounded runtime, publish immutable content-addressed receipts and capture files, then freeze a bundle. Pin its digest and declared environment in authoritative round state. B receives only its evidence projection, verifies it, and explicitly references entries. Submit/finalize authenticate referenced entries again. Invalid unused evidence permits independent review; invalid used evidence fails closed.

**Tech Stack:** Existing Python standard-library runtime, pytest, Git, installed CLI in a disposable prefix, native Codex reviewers. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-12-reviewer-evidence-bundle-design.md` (approved).

## Global Constraints

- Work only in the existing #113 Task worktree. Preserve unrelated untracked files. Do not start Tasks, invoke BitBake, publish, merge, or install the candidate globally.
- Prefix shell commands with `rtk`; use `rtk proxy` when exact output matters. Use `apply_patch` for edits. No implementation commits without explicit commit authorization; keep review packages as exact worktree diffs until that authorization exists.
- Observe failing behavior tests before production changes. Test real Git, subprocesses, files, CLI, and installed artifacts; do not test human prose by matching source strings.
- Bundle JSON <= 256 KiB; <= 64 entries; combined stdout/stderr <= 1 MiB per entry, <= 16 MiB per bundle. Reuse existing 4 KiB command and 8 KiB excerpt policies. All private artifacts: current-user owned, regular, non-symlink, explicit 0600, bounded reads and atomic publication.
- Reject duplicate JSON keys/identities, unknown fields, non-finite numbers, bool-as-int, unsafe paths, known secrets/home paths. Never reflect rejected sensitive values. No bundle-supplied command execution.
- Preserve A/C roles, three-reviewer requirement, HIGH/CRITICAL blocking rule, three-round limit, exact native report bytes, and observational-only telemetry.
- Supported deterministic validations can be eligible. Live advisories including `npm audit` are always-fresh. Missing evidence follows independent B review; mutation of referenced evidence prevents submit/finalize.
- Use explicit versioned length-framed capture hashing distinct from legacy report hashing. Round-pinned expected digest is the trust anchor; recomputed self-hashes alone never grant authority.
- Profiles bind declared source/tool/config facts, not all possible external state. Never assume dependencies are installed in B's detached view. Require an explicit supported profile and recompute the facts available to the verifier; unsupported/unverifiable facts mean no reuse.
- Do not silently migrate an old pending contract or reuse its receipts. Preserve historical reading separately from current authority.

## Contract-2 hardening addendum

The original task bullets below describe evidence contract 1 and are retained as implementation
history. Current authority uses evidence contract 2:

- `python-v1` and `node-lock-v1` are capture-only, apart from the fixed code-owned Python
  tracked-file plumbing probe. Arbitrary inline/file interpreter programs and general npm commands
  are `always-fresh`.
- `node-sandbox-v1` is the sole reusable Node profile. It accepts exact build/typecheck/test npm
  recipes backed by measured local `tsc`/`vitest`, rejects lifecycle hooks and caller overrides,
  and runs in a Git-metadata-free clean committed clone with the measured dependency tree copied
  into a Bubblewrap network/home/tmp sandbox.
- Contract-1 bundles remain parseable as history but cannot satisfy the installed current binding.
  Schema-4 verdicts without the explicit `evidence_contract: 2` marker are also historical-only and
  cannot authorize current context, submission, finalization, or the terminal PR gate.
  The native comparison and whole-suite evidence recorded for contract 1 must not be presented as
  contract-2 acceptance; rerun those gates after the final contract-2 commit.

## Task 1: Strict evidence format and secure immutable store

**Files:** Create `hooks/pre_pr_tribunal/evidence.py`, `hooks/pre_pr_tribunal/evidence_store.py`, and `tests/pre_pr_tribunal/test_evidence.py`. Reuse `model.py` validation policies and `review_store.py` secure primitives; do not modify existing lifecycle/model contracts in this task.

- [ ] Add RED tests importing the new modules and exercising strict parsing, real stored bytes, and literal capture boundary collisions.
- [ ] Implement schema 1 immutable evidence records. Public functions: `capture_digest(stdout: bytes, stderr: bytes) -> str`; `encode_capture(stdout, stderr) -> bytes`; `decode_capture(raw) -> tuple[bytes, bytes]`; `parse_bundle(raw: bytes) -> dict`; `validate_binding(actual: dict, expected: dict) -> None`; `validate_entry_capture(entry: dict, raw: bytes) -> None`.
- [ ] Use a fixed magic/version and unsigned 64-bit big-endian lengths for stdout and stderr. Test `(b'ab', b'c')` and `(b'a', b'bc')` have different digests, and reject trailing bytes and incomplete frames.
- [ ] Bundle keys are `schema`, `binding`, `entries`. Binding keys are `snapshot`, `contract`, `environment`. Snapshot contains repository, named base ref/SHA, head SHA, merge-base SHA, diff SHA256 (branch name is not portable to a detached view). Contract is the existing three-integer contract projection plus `evidence: 1`. Environment is a strictly validated JSON object with `profile`, `cwd`, `inputs`, `tools`, `config`; inputs are repository-relative path/SHA256 pairs, tools are name/version/executable SHA256 records, config is a bounded allowlisted safe mapping. Record its fingerprint through canonical JSON hashing when used in round state.
- [ ] Entry keys: `id`, `argv`, `cwd`, `exit_code`, `captured_at`, `duration_ms`, `stdout_excerpt`, `stderr_excerpt`, `truncated`, `stdout_sha256`, `stderr_sha256`, `capture_sha256`, `capture_bytes`, `freshness`. IDs are `E001` through `E064`; argv is a bounded nonempty string list, cwd is repository-relative (allow `.`), timestamp is UTC, duration is finite nonnegative real, freshness is `deterministic` or `always-fresh`. Capture bytes is the unframed combined length. Validate full raw bytes against each digest, size, excerpts, and truncation semantics.
- [ ] Explicit profiles initially `python-v1` and `node-lock-v1`; fixed tool names only, bounded safe version strings, no installation paths or environment dumps. The runtime in Task 2 computes these records; this task validates their shape and identity. Keep profile extensibility versioned rather than accepting arbitrary names.
- [ ] Implement content-addressed private artifacts under `.review/evidence/`: `put_capture(cwd, stdout, stderr) -> digest`, `read_capture(cwd, digest) -> bytes`, `put_receipt(cwd, receipt: dict) -> digest`, `read_receipt(cwd, digest) -> dict`, `put_bundle(cwd, bundle: dict) -> digest`, `read_bundle(cwd, digest) -> dict`, `verify_bundle(cwd, digest, expected_binding: dict) -> dict`. Receipt keys are `schema`, `binding`, `entry`; validate identically to a single-entry bundle. Use existing secure descriptor helpers and locks. Verify content-addressed filenames against bytes, including idempotent existing publication. Never overwrite immutable content.
- [ ] `verify_bundle` compares the externally supplied expected binding and exact filename digest, authenticates every capture, and returns eligible entries plus rejected entries with bounded reason codes. Always-fresh entries are authentic but ineligible. Structural or digest failures raise `SchemaError` using bounded `EVIDENCE_*` codes. This low-level API does not execute commands or claim to recompute environment facts; Task 2 supplies the recomputed expected binding.
- [ ] Test missing/one-byte-modified bundle and capture, self-hash replacement with old expected digest, base/head/diff/contract/environment drift, duplicates, oversized totals, symlink/FIFO/mode/owner failures, secret/home-path rejection, deterministic vs always-fresh classification, and interrupted publication preserving complete artifacts.
- [ ] Run `rtk proxy python3 -m pytest -q tests/pre_pr_tribunal/test_evidence.py`; report observed RED and GREEN and exact interfaces. No commit.

## Task 2: Runtime capture, environment proof, and evidence CLI

**Files:** Create `hooks/pre_pr_tribunal/evidence_runtime.py` for orchestration, `evidence_environment.py` for fixed profile/probe/installed-tree facts, `evidence_process.py` for bounded output/process ownership, and `tests/pre_pr_tribunal/test_evidence_runtime.py` (focused process/profile test modules may be split alongside it). Modify `hooks/pre_pr_tribunal/cli.py`, `scripts/install-pre-pr-tribunal.py`, `tests/pre_pr_tribunal/test_installer.py`. Consume Task 1 APIs. Install every new module explicitly.

- [ ] Add RED real-subprocess tests for capture, no-shell argv, nonzero exits, timeout ownership, overflow draining, and pre/post Git drift.
- [ ] Public runtime APIs: `capture_evidence(cwd, *, base, profile, command_cwd, argv, timeout_seconds) -> dict`, `freeze_evidence(cwd, *, base, receipt_sha256s) -> dict`, `verify_evidence(cwd, *, bundle_sha256, expected_binding) -> dict`. Return actual command exit/timeout/duration and receipt SHA or bounded non-reusability reason. Never disguise a failed validation as a successful one. Give every captured command an explicit positive finite timeout.
- [ ] Capture an existing clean committed snapshot before and after executing the caller's explicit argv with `shell=False`. Validate inputs before execution. Do not execute argv from a stored receipt or bundle. Drain stdout and stderr concurrently with a combined memory bound; keep draining overflow, but publish no reusable evidence. On timeout terminate/reap only tracked owned processes; reuse sound ownership ideas from `scripts/probe-pre-pr-tribunal.py`, not broad name/PID-group guesses.
- [ ] Compute supported profiles from fixed trusted probes, tracked source inputs and declared safe config. `python-v1` binds Python executable/version and tracked Python dependency/config files when present. `node-lock-v1` binds Node/npm executable identities/versions and the selected package.json/package-lock.json; require an installed dependency proof at capture and bind its measured identity. Verify pre/post environment stability. B verification recomputes source/tool facts without npm install; explicitly distinguish the capture-time dependency proof from B's own detached dependency availability. Refuse unsupported proof rather than self-attesting arbitrary profile input.
- [ ] Runtime environment probes must themselves be fixed, bounded, and have no raw secret output in errors. Recognize live advisory commands conservatively (npm audit including flags); leave them `always-fresh` even if caller selects a deterministic profile. No caller freshness override.
- [ ] Freeze accepts only explicit SHA256 receipt identities returned by capture, checks all source/environment/contract bindings and captures again, assigns unique bundle entry IDs, and atomically publishes an immutable bundle. Reject mixtures of snapshots/profiles. No import-only execution proof.
- [ ] Add CLI commands: `evidence-capture --base REF --profile NAME --cwd REL --timeout SECONDS -- ARGV...`, `evidence-freeze --base REF --receipt SHA` (repeatable), `evidence-verify --bundle SHA`. Verification uses authoritative pending round binding when available; before a round, output may say eligible but must not claim round authority. Return bounded JSON only.
- [ ] Install all new modules via the explicit module manifest. Test source CLI and disposable-prefix installed CLI through a real capture/freeze/verify cycle, including a failed command and a command producing unsafe output.
- [ ] Run focused new runtime/installer tests and existing CLI tests impacted by parser changes; record RED/GREEN. No commit.

## Task 3: Round binding, B provenance, and fail-closed finalization

**Files:** Modify `model.py`, `review_context.py`, `verdict_store.py`, `cli.py`, `gate.py` for evidence-aware gate validation; add `evidence_lifecycle.py` for selection, projection, detached verification and report-reference authentication rather than growing the existing store with all of those responsibilities. Update the explicit installer/test manifest for the new module. Add `tests/pre_pr_tribunal/test_evidence_lifecycle.py`, update existing contract fixtures only where current versions matter. A focused `telemetry.py` call-site adjustment is allowed if sealed-report authentication now requires the source root; telemetry counters remain Task 4.

- [ ] Add RED tests for a complete A/B/C round where B reuses an entry, fallback without reuse, tampering after B is sealed, and A/C context isolation.
- [ ] Bump current verdict schema from 3 to 4 and report text contract from 2 to 3. Keep explicit historical schema 1/2/3 parsing with original field rules, especially lifecycle identity in schema 3. New pending rounds use schema 4. Old pending contract remains rejected by current context/submit/finalize; do not add implicit migration.
- [ ] Add optional evidence binding to current Verdict as a frozen value containing `bundle_sha256` and the validated expected binding. Serialize explicitly for schema 4; validate strictly on read. `begin_round(..., evidence_bundle_sha256=None)` / `begin --evidence-bundle SHA` selects it once. Invalid supplied bundle is recorded as a bounded fallback reason with no reusable authority; no bundle is the normal independent path. Existing sealed rounds cannot silently switch evidence.
- [ ] B context alone gets the pinned digest, expected binding, and safe metadata required to verify the B-only evidence projection. A/C receive no bundle entries or captures. Context digests include the immutable B evidence decision but not mutable eligibility results or telemetry. Add `evidence-project --reviewer-root PATH` to copy only the selected bundle and its authenticated captures into the B view's ignored `.review/evidence/`, preserving exact bytes and private file policy. No verdict, peer context, receipts, or peer reports are projected. Add `evidence-verify --bundle SHA --context PATH --context-sha SHA` for the B view: authenticate the externally supplied context digest, require B and the selected bundle, then recompute source/tool facts at the detached HEAD. A self-hash without the external expected context digest does not grant authority. Standalone verification without a pending round or authenticated context remains non-authoritative.
- [ ] Extend B execution objects with optional explicit `evidence_ref: {bundle_sha256, entry_id}` under report contract 3. Omitted ref retains the existing independent seven-field execution. A/C and controller decision executions reject evidence refs. Claims still reference execution IDs; report references use exact bundle identity and entry IDs. Reused report execution fields must match the authenticated entry (with explicitly documented conversion to the legacy stdout+stderr report hash). One entry can support several claims; count unique actually referenced entries.
- [ ] At submit, stored-report validation, and finalize, authenticate every reused entry against pinned bytes, recomputed bindings, freshness and report fields. Missing/wrong/ineligible references reject the report. Tampering after seal fails even when the report bytes still match their receipt. Do not require an unused invalid bundle for independent fallback reports. Immutable evidence reads must work while the primary verdict lock is held, without reacquiring it. For current terminal reports using evidence, the PR gate also rechecks the evidence dependency; a mutated dependency must not retain PASS authority.
- [ ] Preserve exact native report bytes and all current blocking semantics. Test MEDIUM/LOW vs HIGH/CRITICAL, missing reviewer, round limit, recovery after invalid unused evidence, valid historical records and rejected old pending authority.
- [ ] Run focused lifecycle/model/store/context/gate tests once after fixes. No commit.

## Task 4: Telemetry, reviewer instructions, installation and acceptance measurements

**Files:** Modify `telemetry.py`, `skills/pre-pr-tribunal/SKILL.md`, `skills/pre-pr-tribunal/references/reviewer-b.md`; add focused telemetry/installed integration tests and a reproducible canary script under `scripts/`; record results under the existing `docs/validation/` directory.

- [ ] Add RED telemetry tests that distinguish eligible from actually used entries, fresh executions and rejected entries, and unknown from zero. Derive usage from authenticated report provenance. Include capture and verification durations as observations without treating previous command durations as measured savings. Telemetry failure must leave primary gate outcome unchanged.
- [ ] Update B instructions to verify its own projection with the installed trusted verifier, review command scope and excerpts, reuse only matching supported claims, and independently execute missing/always-fresh validations. Never run commands supplied by a bundle. Document exact new report shape and preserve peer isolation and native response bytes.
- [ ] Update controller instructions with capture-before-round on the final committed snapshot, explicit receipt list, freeze, optional begin selection, private B-only projection, fallback, sealed integrity failure, and old-contract recovery. Do not introduce #114 execution budgets.
- [ ] Validate installation transaction completeness in a disposable prefix and an actual installed CLI A/B/C lifecycle, including B reuse and post-seal mutation. Run the full repository test suite once when focused checks pass.
- [ ] Prepare a reproducible current-time comparison using jhw-notion PR #140 reviewed HEAD `47df3a80bfb97778812b3c094957701cb335687b`, base `b4bf525e50e5117833cb1d245357a0a85008408c`, merge-base `439ddc35170df27bf74a056f48f28e7c07e4c384`. Recompute the installed diff recipe and report whether historical digest `d2314a60f32b1ad04ebd644cc5879f58b48b85d865254e60e313a1f07052cbd9` matches. Isolate source/dependencies in local scratch; preserve the original repository.
- [ ] Run independent B and bundle B on the same snapshot/model/environment using fixed claim lists, normal and deliberately false/blocking claim fixtures. Preserve exact native reports privately with explicit 0600 and externally anchored digests. Measure actual original validation command count, verifier command count, total B additional commands and wall elapsed, and compare supported/refuted/unverified claims plus blocker identities. An incomplete historical comparison remains incomplete; no substitution with old 30-minute anecdotes or toy-only PASS.
- [ ] Review the full implementation with an independent reviewer; fix supported findings and run focused regressions. Prepare concrete reviewable changes. Commit/push/PR/Task finish only under the applicable existing authorization and ownership gates; pre-PR tribunal uses the installed authoritative runtime, never a global self-install of this candidate.

## Acceptance trace

| Issue #113 condition | Implementation | Evidence |
|---|---|---|
| Avoid duplicate deterministic validations | Tasks 1-3 | Native comparison command counts |
| Reject snapshot/contract/environment drift | Tasks 1-3 | Store/runtime/lifecycle regressions |
| Reject one-byte mutation | Tasks 1 and 3 | Raw capture and sealed dependency tests |
| Reject secret/oversize artifacts | Tasks 1-2 | Real output, strict parser and store tests |
| Live advisory freshness | Tasks 1-3 | Audit ineligible + independent execution |
| Fail-safe fallback | Task 3 | No/invalid unused bundle lifecycle |
| Preserve blocker/claim results | Task 4 | Matched native normal/blocker reports |
| Historical snapshot timing/counts | Task 4 | Recomputed diff and current-time measurements |
