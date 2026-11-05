# Backlog drainer v3 validation — 2026-11-04

## PR #266 executable-mode evidence correction — 2026-11-05

This follow-up addresses Codex comment `4477353814` on parent `c5511271d0f0cc288b5b0c7f82ae3f30230d1a9e`. With repository `core.fileMode=false`, Git status could hide executable-bit changes outside the source allowlist even though an explicitly collected file used its actual mode. Git evidence probes now use a command-scoped `core.fileMode=true` override, so clean-start, allowlist checks, and post-commit binding observe the same mode semantics without rewriting repository configuration.

Six real-Git regressions cover both executable-bit directions, coherent allowlisted manifests, empty and unrelated allowlists, post-commit mode changes, and retention of the fixture's `core.fileMode=false`. Four rejection cases reproduced the missing checks before the fix; the six-case GREEN also includes the two coherent-manifest controls.

The five older JSON packets below retain their original bytes and describe only their own recorded inputs. This component correction does not establish native C3 recovery, live Claim transitions, or public-entry foreign-UID fixtures. Those scope limits remain unchanged.

<!-- backlog-fix-pr266-filemode-suite:begin -->
```json
{
  "schema": 1,
  "scope": "PR #266 core.fileMode evidence correction: real-Git executable-mode regressions and final whole-project verification; not native-C3 recovery or reusable Tribunal evidence",
  "parent_head": "c5511271d0f0cc288b5b0c7f82ae3f30230d1a9e",
  "tested_working_change_digest": "54400d5812275c1d36e040569f42fedd82219c62b1bb7fec2aaca0302d785cf6",
  "source_manifest": {
    "change_digest": "54400d5812275c1d36e040569f42fedd82219c62b1bb7fec2aaca0302d785cf6",
    "count": 3,
    "parent_head": "c5511271d0f0cc288b5b0c7f82ae3f30230d1a9e",
    "path_set_digest": "b5d919799768ebeb060da2d5bb67ce6c14bd41d9d70b071959409cf2bc103608",
    "paths": [
      "docs/validation/2026-11-04-backlog-drainer.md",
      "scripts/backlog_checkpoint/git_evidence.py",
      "tests/test_backlog_git_evidence.py"
    ],
    "sources": [
      {
        "change": "modify",
        "mode": "100644",
        "path": "docs/validation/2026-11-04-backlog-drainer.md",
        "sha256": "a2223749e34dca66d5ab0af9dce16935e3c1aa6a2acac1bc484cf0351d20bd9d"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "scripts/backlog_checkpoint/git_evidence.py",
        "sha256": "50b91745a2443c9585870adcdefeb743a2b672ceeec8a5ca5bf339fe388a0a10"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "tests/test_backlog_git_evidence.py",
        "sha256": "ee2f524d5f1c6c6f672e291354b40950221cbe3463a309d9b00258507456a82c"
      }
    ]
  },
  "source_hashes": {
    "install.sh": "e498f4565c4fbc89d7b7b6a116b497fed94a73ac4a7fe675ba6a9907b7364ecf",
    "scripts/backlog-checkpoint.py": "95c09efb70b907f1fad1f07a9c74d56211127c91be70c5e27ad76266dd617526",
    "scripts/backlog_checkpoint/__init__.py": "1c62a4c05db819c7d779f2c22acd2af63b56fb4f648ef4ae236af838a307da2a",
    "scripts/backlog_checkpoint/admission.py": "c9aec0c5474d83dbdd67a0481e26becee3ecddc048948fc15264d6001607ae6e",
    "scripts/backlog_checkpoint/cli.py": "b6ce9591980a9705765a23e41a69119de5f8705834e39387c76107be288942e3",
    "scripts/backlog_checkpoint/codec.py": "aef3989e5a3eed08cf2f32a994191a7fc42f62b0eb531b02787cfb68d9c28179",
    "scripts/backlog_checkpoint/git_evidence.py": "50b91745a2443c9585870adcdefeb743a2b672ceeec8a5ca5bf339fe388a0a10",
    "scripts/backlog_checkpoint/queue.py": "cd889886dde12c433278c3451b9cd9bc931c7bdaa1dc2acd56bf825479841ffd",
    "scripts/backlog_checkpoint/storage.py": "56509e8f63f7c067550cc5a47149e2ca76c21a536f8e01ea7677192b35fcd1e4",
    "scripts/install-backlog-drain.py": "8df080971d07066c585d81c40fe1d40ac644ae96582f3991bd6cd015f62ecc19",
    "skills/backlog-drain/SKILL.md": "dce1c5a6907dd507e342c81229544cc37dab116e9174903d10f060add2a57c85",
    "skills/backlog-drain/references/protocol.md": "7a618109cdaf5e6b15d09735bf4260c019db9489906a683a3eee3c8dfaabfd80",
    "tests/test_backlog_admission.py": "75f9befd46a36eb6a83a4a0a6e069287890209e4eef7285c7fa9266261e2b013",
    "tests/test_backlog_checkpoint.py": "75278441871f2e466639bd2bf209b4823c2ada42ea6c93aeebf50eacee6516db",
    "tests/test_backlog_cli.py": "dc527b6d476d30e445d7d80b68c58224ce7fcdaaf4b1d1470f04ff075e6d09be",
    "tests/test_backlog_git_evidence.py": "ee2f524d5f1c6c6f672e291354b40950221cbe3463a309d9b00258507456a82c",
    "tests/test_backlog_install.py": "ce30f2c11ac5dbd0cba6653906f3c3bb108360ad5824035a0b169cd47080458e",
    "tests/test_backlog_queue.py": "4fedd10261709a7321f4113d51933bea850be5bc853a3139afc921b85bf31f8a"
  },
  "suite": {
    "command": "rtk proxy /usr/bin/env -u LD_LIBRARY_PATH PYTHONDONTWRITEBYTECODE=1 \"$HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python\" -m pytest -q -n 8 -p no:cacheprovider",
    "exit_code": 0,
    "timed_out": false,
    "elapsed_seconds": 214.06,
    "summary": "5439 passed in 213.90s (0:03:33)",
    "capture_sha256": "a29fcab61b20dd4f3409c26c27022b2761fce91a9aec84a86086d600f29caaa9",
    "complete": true,
    "output_limited": false,
    "stdout_bytes": 6156,
    "stderr_bytes": 0,
    "stdout": "bringing up nodes...\nbringing up nodes...\n\n........................................................................ [  1%]\n........................................................................ [  2%]\n........................................................................ [  3%]\n........................................................................ [  5%]\n........................................................................ [  6%]\n........................................................................ [  7%]\n........................................................................ [  9%]\n........................................................................ [ 10%]\n........................................................................ [ 11%]\n........................................................................ [ 13%]\n........................................................................ [ 14%]\n........................................................................ [ 15%]\n........................................................................ [ 17%]\n........................................................................ [ 18%]\n........................................................................ [ 19%]\n........................................................................ [ 21%]\n........................................................................ [ 22%]\n........................................................................ [ 23%]\n........................................................................ [ 25%]\n........................................................................ [ 26%]\n........................................................................ [ 27%]\n........................................................................ [ 29%]\n........................................................................ [ 30%]\n........................................................................ [ 31%]\n........................................................................ [ 33%]\n........................................................................ [ 34%]\n........................................................................ [ 35%]\n........................................................................ [ 37%]\n........................................................................ [ 38%]\n........................................................................ [ 39%]\n........................................................................ [ 41%]\n........................................................................ [ 42%]\n........................................................................ [ 43%]\n........................................................................ [ 45%]\n........................................................................ [ 46%]\n........................................................................ [ 47%]\n........................................................................ [ 48%]\n........................................................................ [ 50%]\n........................................................................ [ 51%]\n........................................................................ [ 52%]\n........................................................................ [ 54%]\n........................................................................ [ 55%]\n........................................................................ [ 56%]\n........................................................................ [ 58%]\n........................................................................ [ 59%]\n........................................................................ [ 60%]\n........................................................................ [ 62%]\n........................................................................ [ 63%]\n........................................................................ [ 64%]\n........................................................................ [ 66%]\n........................................................................ [ 67%]\n........................................................................ [ 68%]\n........................................................................ [ 70%]\n........................................................................ [ 71%]\n........................................................................ [ 72%]\n........................................................................ [ 74%]\n........................................................................ [ 75%]\n........................................................................ [ 76%]\n........................................................................ [ 78%]\n........................................................................ [ 79%]\n........................................................................ [ 80%]\n........................................................................ [ 82%]\n........................................................................ [ 83%]\n........................................................................ [ 84%]\n........................................................................ [ 86%]\n........................................................................ [ 87%]\n........................................................................ [ 88%]\n........................................................................ [ 90%]\n........................................................................ [ 91%]\n........................................................................ [ 92%]\n........................................................................ [ 93%]\n........................................................................ [ 95%]\n........................................................................ [ 96%]\n........................................................................ [ 97%]\n........................................................................ [ 99%]\n.......................................                                  [100%]\n5439 passed in 213.90s (0:03:33)\n",
    "stderr": ""
  },
  "original_capture_record_sha256": "a992569e674b3c81873210a4edc92d972c9e4af694201d3cf2ede34dccdd7a83",
  "tested_validation_document": {
    "sha256": "a2223749e34dca66d5ab0af9dce16935e3c1aa6a2acac1bc484cf0351d20bd9d",
    "bytes": 115143
  },
  "independent_fix_review": {
    "status": "APPROVED",
    "scope": [
      "scripts/backlog_checkpoint/git_evidence.py",
      "tests/test_backlog_git_evidence.py"
    ],
    "findings": [],
    "report_sha256": "e0470c336899c5632f75970f5dd3a2456deabdb8322588e9ee524a63c8109ada"
  },
  "new_regression_cases": 6,
  "red_rejection_cases": 4,
  "repository_config_mutated": false,
  "native_C3_pass": false,
  "public_foreign_uid_fixture_pass": false,
  "live_claim_transition_pass": false,
  "post_suite_change_scope": [
    "docs/validation/2026-11-04-backlog-drainer.md"
  ]
}
```
<!-- backlog-fix-pr266-filemode-suite:end -->

## Historical PR #266 FIFO correction for `c551127` — 2026-11-05

This follow-up addresses Codex comment `4477013647` on parent `84e609b737e83d90c92ab040804d3927e82b79f1`. A current-user mode-0600 FIFO could block a stored-artifact or Git checkpoint open before descriptor validation. A regular source replaced with a FIFO between metadata inspection and open had the same problem. These reads now open nonblocking before the existing type, owner, mode, and identity checks; regular-file validation and size limits remain in place.

Five real-FIFO regressions cover snapshot, evidence manifest, nested raw log, Git checkpoint, and the source metadata/open replacement. Each runs in an owned child with bounded cleanup. The separate source-race RED follows an earlier instrumentation mismatch; that mismatch is not counted as a product failure.

The four earlier JSON packets below are preserved as historical records for their own parent and working-manifest inputs. Their source hashes are not assertions about later working trees. This follow-up does not establish native C3 recovery, live Claim transitions, or public-entry foreign-UID fixtures; those limits remain unchanged.

<!-- backlog-fix-pr266-fifo-suite:begin -->
```json
{
  "schema": 1,
  "scope": "PR #266 FIFO rejection fix: actual owned-FIFO/race component regressions and final whole-project verification; not native-C3 recovery or reusable Tribunal evidence",
  "parent_head": "84e609b737e83d90c92ab040804d3927e82b79f1",
  "tested_working_change_digest": "cd4138d130ce074e6842e2f9699840d5760d613d89d9ff4405d9abafd7b2fb62",
  "source_manifest": {
    "change_digest": "cd4138d130ce074e6842e2f9699840d5760d613d89d9ff4405d9abafd7b2fb62",
    "count": 5,
    "parent_head": "84e609b737e83d90c92ab040804d3927e82b79f1",
    "path_set_digest": "4b2f6c1715dfed1c2195dedb5cb33a5302f4770aeb49e8e31ba9637bd9ee10a9",
    "paths": [
      "docs/validation/2026-11-04-backlog-drainer.md",
      "scripts/backlog_checkpoint/git_evidence.py",
      "scripts/backlog_checkpoint/storage.py",
      "tests/test_backlog_checkpoint.py",
      "tests/test_backlog_git_evidence.py"
    ],
    "sources": [
      {
        "change": "modify",
        "mode": "100644",
        "path": "docs/validation/2026-11-04-backlog-drainer.md",
        "sha256": "cd89978e415b8141afc409bccf71288727b2310fd643e8eabd79bcc1afa08fb2"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "scripts/backlog_checkpoint/git_evidence.py",
        "sha256": "4f0fcc3703c8862e179c55836b62f0d85109dd035bc5b9b1b23f53b33c5eaa65"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "scripts/backlog_checkpoint/storage.py",
        "sha256": "56509e8f63f7c067550cc5a47149e2ca76c21a536f8e01ea7677192b35fcd1e4"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "tests/test_backlog_checkpoint.py",
        "sha256": "75278441871f2e466639bd2bf209b4823c2ada42ea6c93aeebf50eacee6516db"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "tests/test_backlog_git_evidence.py",
        "sha256": "e8983801896811d6160f14e0e678deff64989020facbaf5f696c336a280e3981"
      }
    ]
  },
  "source_hashes": {
    "install.sh": "e498f4565c4fbc89d7b7b6a116b497fed94a73ac4a7fe675ba6a9907b7364ecf",
    "scripts/backlog-checkpoint.py": "95c09efb70b907f1fad1f07a9c74d56211127c91be70c5e27ad76266dd617526",
    "scripts/backlog_checkpoint/__init__.py": "1c62a4c05db819c7d779f2c22acd2af63b56fb4f648ef4ae236af838a307da2a",
    "scripts/backlog_checkpoint/admission.py": "c9aec0c5474d83dbdd67a0481e26becee3ecddc048948fc15264d6001607ae6e",
    "scripts/backlog_checkpoint/cli.py": "b6ce9591980a9705765a23e41a69119de5f8705834e39387c76107be288942e3",
    "scripts/backlog_checkpoint/codec.py": "aef3989e5a3eed08cf2f32a994191a7fc42f62b0eb531b02787cfb68d9c28179",
    "scripts/backlog_checkpoint/git_evidence.py": "4f0fcc3703c8862e179c55836b62f0d85109dd035bc5b9b1b23f53b33c5eaa65",
    "scripts/backlog_checkpoint/queue.py": "cd889886dde12c433278c3451b9cd9bc931c7bdaa1dc2acd56bf825479841ffd",
    "scripts/backlog_checkpoint/storage.py": "56509e8f63f7c067550cc5a47149e2ca76c21a536f8e01ea7677192b35fcd1e4",
    "scripts/install-backlog-drain.py": "8df080971d07066c585d81c40fe1d40ac644ae96582f3991bd6cd015f62ecc19",
    "skills/backlog-drain/SKILL.md": "dce1c5a6907dd507e342c81229544cc37dab116e9174903d10f060add2a57c85",
    "skills/backlog-drain/references/protocol.md": "7a618109cdaf5e6b15d09735bf4260c019db9489906a683a3eee3c8dfaabfd80",
    "tests/test_backlog_admission.py": "75f9befd46a36eb6a83a4a0a6e069287890209e4eef7285c7fa9266261e2b013",
    "tests/test_backlog_checkpoint.py": "75278441871f2e466639bd2bf209b4823c2ada42ea6c93aeebf50eacee6516db",
    "tests/test_backlog_cli.py": "dc527b6d476d30e445d7d80b68c58224ce7fcdaaf4b1d1470f04ff075e6d09be",
    "tests/test_backlog_git_evidence.py": "e8983801896811d6160f14e0e678deff64989020facbaf5f696c336a280e3981",
    "tests/test_backlog_install.py": "ce30f2c11ac5dbd0cba6653906f3c3bb108360ad5824035a0b169cd47080458e",
    "tests/test_backlog_queue.py": "4fedd10261709a7321f4113d51933bea850be5bc853a3139afc921b85bf31f8a"
  },
  "suite": {
    "command": "rtk proxy /usr/bin/env -u LD_LIBRARY_PATH PYTHONDONTWRITEBYTECODE=1 \"$HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python\" -m pytest -q -n 8 -p no:cacheprovider",
    "exit_code": 0,
    "timed_out": false,
    "elapsed_seconds": 214.478,
    "summary": "5433 passed in 214.31s (0:03:34)",
    "capture_sha256": "f40838534cbc9e76a5d3e0c3df6273797608a2b649ab3747ce30286f61c9cc0d",
    "complete": true,
    "output_limited": false,
    "stdout_bytes": 6156,
    "stderr_bytes": 0,
    "stdout": "bringing up nodes...\nbringing up nodes...\n\n........................................................................ [  1%]\n........................................................................ [  2%]\n........................................................................ [  3%]\n........................................................................ [  5%]\n........................................................................ [  6%]\n........................................................................ [  7%]\n........................................................................ [  9%]\n........................................................................ [ 10%]\n........................................................................ [ 11%]\n........................................................................ [ 13%]\n........................................................................ [ 14%]\n........................................................................ [ 15%]\n........................................................................ [ 17%]\n........................................................................ [ 18%]\n........................................................................ [ 19%]\n........................................................................ [ 21%]\n........................................................................ [ 22%]\n........................................................................ [ 23%]\n........................................................................ [ 25%]\n........................................................................ [ 26%]\n........................................................................ [ 27%]\n........................................................................ [ 29%]\n........................................................................ [ 30%]\n........................................................................ [ 31%]\n........................................................................ [ 33%]\n........................................................................ [ 34%]\n........................................................................ [ 35%]\n........................................................................ [ 37%]\n........................................................................ [ 38%]\n........................................................................ [ 39%]\n........................................................................ [ 41%]\n........................................................................ [ 42%]\n........................................................................ [ 43%]\n........................................................................ [ 45%]\n........................................................................ [ 46%]\n........................................................................ [ 47%]\n........................................................................ [ 49%]\n........................................................................ [ 50%]\n........................................................................ [ 51%]\n........................................................................ [ 53%]\n........................................................................ [ 54%]\n........................................................................ [ 55%]\n........................................................................ [ 56%]\n........................................................................ [ 58%]\n........................................................................ [ 59%]\n........................................................................ [ 60%]\n........................................................................ [ 62%]\n........................................................................ [ 63%]\n........................................................................ [ 64%]\n........................................................................ [ 66%]\n........................................................................ [ 67%]\n........................................................................ [ 68%]\n........................................................................ [ 70%]\n........................................................................ [ 71%]\n........................................................................ [ 72%]\n........................................................................ [ 74%]\n........................................................................ [ 75%]\n........................................................................ [ 76%]\n........................................................................ [ 78%]\n........................................................................ [ 79%]\n........................................................................ [ 80%]\n........................................................................ [ 82%]\n........................................................................ [ 83%]\n........................................................................ [ 84%]\n........................................................................ [ 86%]\n........................................................................ [ 87%]\n........................................................................ [ 88%]\n........................................................................ [ 90%]\n........................................................................ [ 91%]\n........................................................................ [ 92%]\n........................................................................ [ 94%]\n........................................................................ [ 95%]\n........................................................................ [ 96%]\n........................................................................ [ 98%]\n........................................................................ [ 99%]\n.................................                                        [100%]\n5433 passed in 214.31s (0:03:34)\n",
    "stderr": ""
  },
  "original_capture_record_sha256": "ed6450bbe58bf7d17d57d3b7d7f83c7ba09145f3e308efd04629977852cf01c8",
  "tested_validation_document": {
    "sha256": "cd89978e415b8141afc409bccf71288727b2310fd643e8eabd79bcc1afa08fb2",
    "bytes": 102215
  },
  "independent_fix_review": {
    "status": "APPROVED",
    "scope": [
      "scripts/backlog_checkpoint/storage.py",
      "scripts/backlog_checkpoint/git_evidence.py",
      "tests/test_backlog_checkpoint.py",
      "tests/test_backlog_git_evidence.py"
    ],
    "findings": [],
    "report_sha256": "c1ecaad331938844e23c7e248102fc59403bc6456a4d9337b8598be8604280d5"
  },
  "new_regression_count": 5,
  "native_C3_pass": false,
  "public_foreign_uid_fixture_pass": false,
  "live_claim_transition_pass": false,
  "post_suite_change_scope": [
    "docs/validation/2026-11-04-backlog-drainer.md"
  ]
}
```
<!-- backlog-fix-pr266-fifo-suite:end -->

## Follow-up to the evidence review at `57b1cd0` — 2026-11-05

This section records fixes for the fresh review lifecycle `b89ad53faf25648e8559fd22676011f5`, parent HEAD `57b1cd0fad6220aa155e3a3ef138208989b5b7e5`. Finding IDs are round-local: the A-R1-001/A-R1-002 entries here are the state-root and file-input findings from this lifecycle. The earlier same-numbered findings below concern different code and remain historical. Formal re-review requires its own bound lifecycle; these implementation checks do not authorize PR creation or merge.

- **A-R1-001:** state-root resolution walks from `/` with a retained parent descriptor and no-follow directory opens for every component. Only the canonical single `/` anchor is accepted, including rejection of the POSIX implementation-defined `//` spelling. The final root keeps the existing current-UID/0700 rule while system-owned ancestors remain traversable. Release and reconciliation read evidence through that same retained root descriptor, rather than looking up the pathname again. The generic snapshot/worktree directory helper is unchanged.
- **A-R1-002:** file input is opened nonblocking and no-follow, must be a regular file, and reads at most 1 MiB plus one byte. All descriptors close on success and error. Existing bounded stdin, JSON parsing and structured-error behavior remain in place; valid input exactly at the cap is accepted.

The implementation added 17 filesystem/subprocess regressions. The meaningful initial REDs covered ancestor aliases, descriptor walking/root replacement, FIFO blocking, symlink following, non-regular inputs and sparse-file allocation under a child address-space limit. A separate RED covered nonexact raw string spellings. Focused GREEN and the six-file backlog suite passed: **218 passed in 4.80s**. Ruff F/E9, six-file in-memory compilation and `git diff --check` passed. Raw RED/GREEN logs are private current-user regular 0600 files under `.superpowers/sdd/2026-11-04-backlog-drainer/review-fix-e1/`.

The independent bounded fix review found and then rechecked one `//` anchor gap; its final result has no material findings. A fresh focused selection passed 17 tests. The six code/test files remain frozen. The parent then completed the single final whole-project run with exit 0: **5428 passed in 231.88s (0:03:51)**, driver elapsed 232.141 seconds. All 18 frozen execution/test/install/guidance hashes and the complete tested working manifest matched before and after execution. Only final result metadata in this validation document changed afterward. Earlier 5411-test logs and native-control artifacts are historical observations, not proof of these new bytes. Native C3 full-product/Claude recovery remains unverified. The Task Claim stays active; no Task lifecycle, push, PR or merge action is performed by this follow-up.

The exact final full-suite command was:

```text
rtk proxy /usr/bin/env -u LD_LIBRARY_PATH PYTHONDONTWRITEBYTECODE=1 "$HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python" -m pytest -q -n 8 -p no:cacheprovider
5428 passed in 231.88s (0:03:51)
```

It ran from the selected Task worktree on parent `57b1cd0fad6220aa155e3a3ef138208989b5b7e5` plus tested working-change digest `e7c7a9544b95d91feea97fe48129d87cd9fb2b347935bc2c3d68799862bb8980`. The complete stdout/stderr capture is 6156 bytes, SHA-256 `bc26b08fc2be6ea34db9b4ada545161bc019c55b3c8150bfbcb9ca642c541804`. Private input/result/log and the tested pre-result-metadata document remain under `review-fix-e1/`; the public current-source record below separates this working input from later documentation-only metadata. All earlier archived packets remain byte-identical.

## Historical first Tribunal round-1 follow-up — 2026-11-05

This historical follow-up describes the fixes committed as `5bf46737048dd81a5b14e5e122095023c2ed41fb`. Its round-local finding IDs refer to the earlier `3c09cde` review, not the later `57b1cd0` evidence-review lifecycle. Its measurements do not verify later source changes.

- **A-R1-001:** release now requires non-null parent/result coordinates, the exact source manifest and commit binding in authenticated evidence, matching snapshot counts/digests, and a fresh branch/worktree inspection plus `bind_commit` before removing the active admission. Invalid evidence, a changed source/HEAD/branch, and JSON boolean/integer substitutions preserve admission.
- **A-R1-002:** storage validates the exact absolute canonical root-level run slot and preserves run ID, supervisor identity, all binding fields, branch and frozen base HEAD across CAS. Changed identity or path cannot replace the old bytes. A normal COMMIT-to-READY update and separately created successor files remain supported.
- **A-R1-003:** collection and final binding reject assume-unchanged/skip-worktree index flags and compare the visible index before/after collection. No index flags or source bytes are changed by rejection.

The corrected boundaries were first exercised by failing regressions; the raw RED traces are preserved privately. Current support claims use the fresh, reproducible commands below. The final focused command is:

```text
rtk proxy env -u LD_LIBRARY_PATH "$HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python" -m pytest -q tests/test_backlog_admission.py tests/test_backlog_checkpoint.py tests/test_backlog_cli.py tests/test_backlog_git_evidence.py tests/test_backlog_install.py tests/test_backlog_queue.py
201 passed in 9.28s
```

Ruff F/E9 and in-memory compilation of all 15 backlog Python files pass; `git diff --check` passes. The existing global Ruff executable was used because the test virtual environment has no Ruff module. No dependency was installed.

A pure reference-skill application control using the old protocol omitted `source_manifest` and `commit_binding` and found no explicit immutable CAS fields. A fresh consumer of the updated reference produced all three required evidence fields and all 12 immutable identity fields. These are reference-application checks, not native interruption/recovery proof.

Final full-suite result: PASS, exit 0; `5411 passed in 230.77s (0:03:50)`, driver elapsed 230.94 seconds.

```text
rtk proxy env -u LD_LIBRARY_PATH "$HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python" -m pytest -q -n 8
```

The command ran from the implementation worktree on parent HEAD `3c09cde6d380900b8202e9a89968817c4e036e71` plus tested working-change digest `04ccbb465cb7e2d95145a7919656fef1a1d5d11106c3ae33e13a8aeaab7eee97`. All 18 frozen execution/test/install/guidance hashes matched after the command. The raw combined log is 6156 bytes, SHA-256 `7e53707baf6f84034ec034c716030276780e680a6710e896ba1622045df7cb44`. The private input, result and log are `.superpowers/sdd/2026-11-04-backlog-drainer/review-fix-r1/full-suite-{input.json,result.json,log}`. Only this result paragraph and historical-scope wording changed after the suite; executable, test and guidance bytes remained frozen.

Native C3 remains NOT PASS: no new native experiment was run, and the independently observable child-owned tool-terminal receipt / Claude bridge is still unavailable on the observed surface. The previous full-suite result is historical; the follow-up full-suite command and source hashes are recorded above and the exact archived bytes are embedded below. No actual-HOME deployment, push, PR, merge, Task finish or Claim lifecycle mutation is performed here.

## Historical pre-Tribunal validation (2026-11-04)

Validated base HEAD: `15c0ab85d5b6c216505a68f5e2513499e78951f1`
Validated working-change digest: `ea5616dc79c56e60bc88910347f7ab43ab57e9043fe89df8351b7d39d190a088`
Issue: `https://github.com/jhw7500/claude-config/issues/31`

## Outcome and claim boundary

The final working source is validated by targeted suites, a post-residual whole-project suite, real temporary Git repositories, owned process-crash fixtures, temporary-HOME discovery, and an isolated existing-hook coexistence experiment. The parent drained the final whole-project command with exit 0: `5370 passed in 225.12s`, driver elapsed 225.27 seconds, and the raw log is 6076 bytes. The result binds to working-change digest `ea5616dc79c56e60bc88910347f7ab43ab57e9043fe89df8351b7d39d190a088` on base HEAD `15c0ab85d5b6c216505a68f5e2513499e78951f1`; all 16 execution/test hashes and both guidance hashes matched the frozen input after execution. This is a working-source attestation, not a bare-HEAD-only claim.

The final-fix working tree addresses the whole-branch I1/I2/M1/M2 findings: every validated private hierarchy entry, whether newly created or already visible, is synced child-before-parent before dependent publication; new runs require a clean empty-allowlist Git baseline while exact existing-run resume keeps owned-edit reconciliation; malformed manifest `change`/`mode` values produce bounded errors; and a representative 1000-source/200-test/100-review evidence fixture round-trips within the declared budgets. The focused six-file backlog suite is fresh for the residual bytes; no new native experiment was run and native C3 remains unsupported/`PAUSED`.

Native recovery is **not a full PASS**. Codex reached an actual pre-edit running barrier and was interrupted, but the parent could not independently observe the child-owned pending tool's terminal result. The run therefore stopped `PAUSED`; no replacement or post-edit case ran. The current Codex surface exposed no Claude native stop/terminal/non-fork bridge, so Claude is unsupported on this surface. Neither finding is a global claim about every Claude TUI or SDK.

This validation does not claim an end-to-end production backlog run, automatic Task/Claim lifecycle mutation, external release action, merge, Task finish, next-issue transition, or completion of the original broad unattended-backlog goal.

## Evidence layers and commands

Evidence is classified as follows: **ACTUAL** is a direct current-root Git/hook/native receipt; **TEST** is automated component or owned-fixture evidence; **ADVICE** defines a threshold but is not proof; **UNTESTED** identifies a real gap.

### Final post-residual whole-project suite

```text
env -u LD_LIBRARY_PATH /home/jhw/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python -m pytest -q -n 8
5370 passed in 225.12s (0:03:45)
```

The parent independently drained the command with exit 0. Driver elapsed time was 225.27 seconds. The private log was 6076 bytes. Structured result: `.superpowers/sdd/2026-11-04-backlog-drainer/residual-i1-full-pytest-result.json`; frozen input and source hashes: `.superpowers/sdd/2026-11-04-backlog-drainer/residual-i1-full-suite-input.json`. The result records `execution_sources_unchanged:true` and the exact working-change digest above.

Historical only: production HEAD plus the earlier integration state returned `5355 passed in 197.65s`; the first post-fix working source returned `5368 passed in 200.64s`. Both predate the residual existing-directory I1 correction and are not the final proof.

### Targeted and static evidence

- Final-fix backlog target: `158 passed in 3.67s` across all six `tests/test_backlog_*.py` files.
- Final completion-gate rerun of the same six files: `158 passed in 5.43s`.
- Residual-I1 completion target across the same six files: `160 passed in 5.14s`; the two new real-filesystem regressions separately returned `2 passed in 0.05s` after first producing `2 failed in 0.08s` on the skipped existing-directory branch.
- Final residual completion-gate rerun of the same six files: `160 passed in 5.35s`.
- Scoped residual review `.superpowers/sdd/2026-11-04-backlog-drainer/residual-i1-review-r1.md` marks I1 addressed, I2/M1/M2 unchanged and addressed, all four findings clear, and no new residual finding; reviewed storage SHA-256 is `be5a4a67f64e5d580dabe5bd696b99fd155bb7e8cf19db4d74638e1d52ffb761`.
- Direct boundary check with a current-user mode-0700 state root immediately below root-owned `/tmp`: evidence write/read passed. The helper did not require the external parent to share the state-root UID; private child UID/mode/no-follow validation remained active.
- Final-fix focused RED named eight failures for missing hierarchy fsync behavior and malformed manifest error normalization; after the minimal fixes, the same selected cases returned `13 passed in 0.77s`.
- Final-fix Ruff on both changed production modules and four directly changed test modules: `All checks passed!`; `git diff --check` also passed.
- Backlog target: `145 passed in 4.27s`.
- Git evidence target: `36 passed in 3.58s` using temporary real Git repositories.
- Queue target: `21 passed in 0.31s` using a local fake command, not live network.
- Relevant temporary-HOME installer target: `153 passed in 17.24s`.
- Before the EOF trim, Ruff F/E9, 15-file in-memory compile, `bash -n`, and ShellCheck `-x` passed.
- Whole-branch diff inspection found only one extra final blank line in `tests/test_backlog_admission.py`. The frozen before file had SHA-256 `fe19823dbd30d42d9dc63a6681e1f18254c643886a9455221da3016c7073f6f5` and size 19425. The edited file is exactly its first 19424 bytes, ends in one LF, and has SHA-256 `91c772af161487550fbbd5d9953a4010613ecc2e68765b59af78c3056d11962d`.
- After that one-byte, semantics-free trim: `pytest -q tests/test_backlog_admission.py` returned `20 passed in 0.24s`. The whole suite was not repeated solely for an EOF-only change.

### Native and hook evidence separation

- **ACTUAL native:** `.superpowers/sdd/2026-11-04-backlog-drainer/native-cases/pre/context.json` and `snapshot-paused.md`.
- **ACTUAL hook fixture:** `task-6-precompact-coexistence.json`.
- **FAILED SETUP, not native proof:** `native-cases/pre-setup-failed/`; no native child or source fixture was created there.
- **ADVICE only:** `task-6-native-proof-advice.md`.
- **INVENTORY/PREPARATION only:** installed-version and runtime-preparation JSON files.

## C1–C14 results

### C1 — PASS (component)

Snapshot roundtrip, canonical JSON, generation CAS, bounded concurrent one-winner CAS, and ≤12 KiB budget are covered by `tests/test_backlog_checkpoint.py`. The final-fix fixture additionally performs actual evidence write/read and snapshot write/read with exactly 1000 source records, 200 test records, and 100 review records, asserting ≤1 MiB evidence, ≤12 KiB snapshot, and ≤15 snapshot lines.

### C2 — PASS (component)

Partial-write preservation, symlink/no-follow rejection, size/private-root/traversal/digest checks, and explicit post-publish indeterminate behavior passed. Fresh evidence and admission hierarchy tests verify child-directory sync before parent-directory sync, and injected parent-sync failures remain bounded before evidence or active-admission publication. Real-filesystem regressions additionally cover reopening a valid leftover after creator parent-sync failure and a concurrent opener while the creator is paused at that barrier; both openers establish their own child-plus-parent barrier. Existing private directories are validated rather than silently mode-repaired. Foreign UID is enforced by descriptor metadata but lacks a host-level foreign-UID fixture.

### C3 — NOT PASS

Codex actual pre-edit evidence was running → interrupt(previous running) → interrupted. Fixture PID/UID/cmdline, closed writer FD, and source absence were verified. Parent lookup of the child-owned pending tool failed at the namespace boundary, so no independent tool-terminal receipt exists. Exact pidfd cleanup and an empty process group prove cleanup only. The snapshot is `PAUSED` with `TOOL_SESSION_UNOBSERVABLE`; no source edit, replacement, post-edit case, follow-up, or old-child wake occurred.

Claude is unsupported on the current exposed surface because no Agent/TaskStop/same-run terminal/non-fork bridge was available. Installed CLI version alone is not proof, and no Claude model or SDK session ran. Cross-runtime C3 is therefore `NOT PASS`.

### C4 — PARTIAL

Supervisor/runtime consistency, stale/foreign admission, owner binding, and successor identity are tested. The actual pre-edit case stopped PAUSED without lifecycle mutation. No deliberate live supervisor/Claim switch was performed; recorded identity is observation, not Registry authority.

### C5 — PASS (component)

Unique direct commit adoption and content/path/head/multiple-child/merge/replacement-ref/final-recheck branches passed in `tests/test_backlog_git_evidence.py`. Normal and ignored public checkpoints passed collect→commit→bind.

### C6 — PASS (exact selection)

Exact checkpoint selection and foreign/extra/tracked/staged/staged-delete-recreate/symlink/mode rejection passed. The general compaction glob is not a recovery selector. No two-live-session native resume was performed.

### C7 — PASS

ABA, double collection, manifest mismatch, changed checkpoint, and final HEAD recheck tests passed. The final post-residual whole suite returned 5370 passes and remained bound to the unchanged 16 execution/test hashes and two guidance hashes. The historical 5355 and 5368 results are not reused as proof for the final working source.

### C8 — PASS for this run's stop boundary

No external gate, merge, Task finish, or next issue was executed. Admission release requires exact terminal/quiescent evidence and rejects stale child phase/attempt. This is not a successful native production run through every phase.

### C9 — PASS (component)

Deterministic Kahn order, closed blockers, long chains, cross-edges, cycles/downstream reasons, invalid input, pagination failure, unsafe links, caps, deduplication, and blocker lookup passed in `tests/test_backlog_queue.py`. No live GitHub query was made.

### C10 — PASS for temporary HOME

Dual-runtime discovery/helper execution from foreign cwd, conflict preservation, and launcher-only exclusion passed. Actual HOME was intentionally untouched.

### C11 — PASS (component)

Canonical checkout, alias/child/wrong-ref/wrong-branch, duplicate branch, admission spelling, and foreign-cwd helper resolution passed. A malicious native child cwd escape was not run.

### C12 — PARTIAL

Exclusive admission/private state, stale release, terminal/quiescence, concurrent CAS, and canonical worktree tests passed. The actual pre-edit case used the current Task root and left source unchanged, but C3 stopped before replacement/serial-child recovery. Task authority remains external.

### C13 — PASS for coexistence

The isolated existing-hook experiment recorded three exit-0 cases: stale general handoff blocked, fresh backlog-named handoff allowed compaction, and both stale blocked with the backlog file freshest. This confirms filename-freshness interaction only; the drainer still uses an exact path. Hook semantics and actual HOME heartbeat were not changed.

### C14 — PASS (component)

Required proof, exact archive/publish, prior/quiescence rejection, pre/post-publish fsync, and owned-child archive/publish crash boundaries preserve old or successor active. No Registry-approved successor Task recovery was invoked.

## Overall verdict

The bounded v3 helper/workflow implementation is validated at working-change digest `ea5616dc79c56e60bc88910347f7ab43ab57e9043fe89df8351b7d39d190a088`: all original I1/I2/M1/M2 findings are clear, the focused backlog target is current at 160 passes, relevant static checks pass, and the final whole-project suite has 5370 passes with stable frozen source hashes. The residual review found no remaining implementation blocker. Final canonical commit/evidence binding remains parent-owned. Native recovery remains intentionally unavailable on the observed tool surfaces: Codex is `UNSUPPORTED_TOOL_TERMINAL_VISIBILITY / PAUSED`, Claude is unsupported on this surface, and cross-runtime C3 is `NOT PASS`. These accepted conditional limitations are reported, not upgraded by fixture cleanup, advice, version output, or synthetic status.

## Decision ledger

Every durable `Ruling:` is retained with the cost if wrong.

1. Split implementation by responsibility, add no dependency, use explicit installed roles. **Cost:** runtime incompatibility or misrouted review.
2. Use targeted tests per slice and one fresh whole suite after integration. **Cost:** stale confidence or wasted runtime.
3. Parent commits after review; reviewer sees exact worker diff. **Cost:** unreviewed or wrong-byte commit.
4. Approved implementation authority persists within scope. **Cost:** needless approval churn or scope overrun if boundaries changed.
5. Native proof uses current actual Task root and isolated fixture; no fabricated Claim. **Cost:** invalid lifecycle evidence.
6. Treat the document-write hook misparse as a file-operation issue; do not change guards or take external action. **Cost:** guard bypass or unintended external action.
7. Retain fixed four-line Markdown v1 and test decode/roundtrip. **Cost:** compatibility migration or decoder ambiguity.
8. Post-rename directory-fsync failure is indeterminate; never auto-rollback; pre-publish failure preserves old bytes. **Cost:** destroyed generation or duplicate effects.
9. Reject Unicode Cc, cap nesting at 64, normalize recursion errors. **Cost:** path ambiguity/resource failure or over-tight rejection.
10. Release exact current snapshot with strict terminal/quiescent evidence; never release PAUSED; keep admission→snapshot lock order. **Cost:** foreign/stale release or race.
11. Fix the three concrete interface gaps together, including domain-error normalization, without unrelated cleanup. **Cost:** CLI error leakage or wider regression surface.
12. Terminal child slot matches phase/attempt and clears only after real prior quiescence; preserve old IDs in immutable evidence. **Cost:** fabricated READY state.
13. Allow one heavy protocol reference beside the short Skill and preflight installer targets. **Cost:** missing guidance or overlong discovery surface.
14. Exclude only one validated exact checkpoint; never glob/tracked/ignore mutation; reject unknown dirty paths. **Cost:** manifest contamination or hidden changes.
15. Retain four queue keys and deterministic `CLOSED_SATISFIED` reasons. **Cost:** consumer output migration only.
16. Support typed read-only repository collection and pure graph input; map runtime receipts precisely. **Cost:** CLI ambiguity, mutation surface, or false runtime promise.
17. Use validated parent-directory flock for snapshots, preserve admission→snapshot order, fail closed if unsupported. **Cost:** artifacts, CAS race, platform limitation, or coarser contention.
18. Untracked means absent from index and HEAD, including ignored files; reject tracked and staged-delete/recreated paths. **Cost:** rejected valid checkpoint or hidden tracked content.
19. Retain the private SDD workspace/raw native receipts required by the approved plan. Keep current-UID 0600 evidence private and copy no credential/raw session identifier into this document. **Cost:** local disk retention and later explicit cleanup; deleting now would erase the only evidence for native unsupported status. This retention is not hidden Task state.
20. Continue original I1 with the same fixer, minimal validated-existing-directory child-plus-parent fsync, and focused leftover/concurrent regressions; explicitly extend the generic one-scoped-review cap because a known load-bearing durability defect cannot be parked while claiming completion. Freeze I2/M1/M2 and guidance; add no general review or authority expansion. **Cost:** one additional focused review and a fresh full-suite pass.
21. Use the already-approved KeepBranch choice for finishing the development branch: stop at the verified commit with the Claim unchanged, without a redundant integration menu or new push/PR/merge/Task-finish action. Preserve the Task worktree and private evidence under prior authority. **Cost:** a later explicit integration decision remains necessary.

## Appendix: source and test citations

| Area | Production source | Direct tests / receipts |
| --- | --- | --- |
| Snapshot schema and encoding | `scripts/backlog_checkpoint/codec.py` | `tests/test_backlog_checkpoint.py` canonical, roundtrip, type, depth, budget cases |
| Atomic snapshot/evidence storage | `scripts/backlog_checkpoint/storage.py` | `tests/test_backlog_checkpoint.py` CAS, partial-write, fsync, symlink, evidence cases |
| Admission and successor recovery | `scripts/backlog_checkpoint/admission.py` | `tests/test_backlog_admission.py` exclusivity, release, reconcile, real-process crash cases |
| Git source and commit binding | `scripts/backlog_checkpoint/git_evidence.py` | `tests/test_backlog_git_evidence.py` canonical root, manifests, exact checkpoint, ABA, bind branches |
| Queue collection/ranking | `scripts/backlog_checkpoint/queue.py` | `tests/test_backlog_queue.py` ordering, cycles, pagination, caps, explicit blocker cases |
| Bounded CLI | `scripts/backlog_checkpoint/cli.py`, `scripts/backlog-checkpoint.py` | `tests/test_backlog_cli.py` grouped argv, input, exact snapshot, bounded errors |
| Temporary-HOME install | `scripts/install-backlog-drain.py`, `install.sh` | `tests/test_backlog_install.py` and relevant installer suites |
| Shared workflow contract | `skills/backlog-drain/SKILL.md`, `skills/backlog-drain/references/protocol.md` | Task 5 consumer-evaluation JSON is advisory behavior evidence, not native recovery proof |
| Existing compaction hook coexistence | `hooks/precompact-handoff.sh` | `.superpowers/sdd/2026-11-04-backlog-drainer/task-6-precompact-coexistence.json` |
| Actual native attempt | no production source mutation | `.superpowers/sdd/2026-11-04-backlog-drainer/native-cases/pre/context.json`, `snapshot-paused.md` |
| Full suite | repository-wide | `.superpowers/sdd/2026-11-04-backlog-drainer/residual-i1-full-pytest-result.json`, `residual-i1-full-suite-input.json`, and private raw log |

Design criteria are defined in `docs/superpowers/specs/2026-11-04-backlog-drainer-design.md`. Implementation sequence is in `docs/superpowers/plans/2026-11-04-backlog-drainer.md`. Task reports under the private SDD workspace preserve RED/GREEN and review receipts; they are supporting evidence, not substitutes for the actual tests and source cited above.


## Evidence accessibility follow-up — 2026-11-05

The following additions expose previously private execution records and add bounded native observations for independent verification. They do not alter approved completion criteria or establish complete verification by themselves.

### Exact full-suite records for an isolated reader

The JSON packet below carries the original UTF-8 bytes of all five archived files as JSON strings. Decode a string to UTF-8 and verify its byte count and SHA-256 before interpreting it; the serialization around the string is not the archived file. Verify every frozen `source_hashes` entry against the exact historical Git blob at `5bf46737048dd81a5b14e5e122095023c2ed41fb`. These 18 executable/test/install/guidance hashes describe that commit, not later working files; the tested-versus-committed manifest difference is only this validation document. The two manifest digests remain distinct.

This permits independent record/source consistency checks from the committed checkout without opening the implementation worktree's private directory. It does not retroactively witness the historical execution or authenticate an entire historical environment. Current-source functional verification can be reproduced with a Python environment containing `requirements-test.lock`, pytest and pytest-xdist, from the checkout root: `env -u LD_LIBRARY_PATH PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider -n 8`. A pre-existing local test interpreter is also named in the archived command; no actual-HOME installation is required. A fresh run produces a new observation with its own duration and receipt.

A fresh non-fork native verifier independently selected and ran the entire pytest suite in a separate clean detached checkout at HEAD `5bf46737048dd81a5b14e5e122095023c2ed41fb`, tree `56698cd69026d25d357ed766eb255f636ef835f8`. It observed `5411 passed in 236.31s (0:03:56)`, exit 0, no timeout; HEAD/tree/clean state matched before and after. Its full stdout-plus-stderr SHA-256 is `21e707b5bfe9def69e9e61d534353c9941965dee0feeffc72f5d12de32417d79`. This is a newly witnessed execution of that committed snapshot, distinct from the archived 230.77-second run. The installed `capture-run` record below contains the complete streams; publication does not automatically make Python execution reusable under the Tribunal evidence-profile rules.

### Additional bounded native observations

Two fresh non-fork Codex executor children ran sequentially in the actual approved Task worktree. Each independently queried the read-only launcher status, checked exact cwd/branch/HEAD and clean empty-allowlist Git state, and exclusively wrote only its own mode-0600 fixture result. Parent verified the first child and all returned tool calls before starting the second; the second preserved the first artifact's bytes. Neither acquired a separate Claim or invoked lifecycle recovery. Their native completion and terminal foreground tool receipts are distinct from interruption recovery.

A separate native child began inside a nested directory of the same Task checkout. The live Task/Claim mapping matched, but exact cwd did not. It returned `PAUSED_NO_WRITE`, left the candidate canary absent, and performed no Git, admission, checkpoint or lifecycle mutation. The parent verified actual native completion and returned foreground calls.

The original interrupted pre-edit child's own retained history contains `turn_aborted` with reason `interrupted`, in addition to the parent's running/interruption observations. It contains only the earlier running exec-session receipt, with no subsequent tool-terminal result. A new non-writing hosted-sleep capability probe reported that `clock.sleep` was not exposed to that child; no sleep or interruption occurred in this probe. Neither observation is counted as C3 PASS. No old interrupted child was woken.

Official [multi-agent action semantics](https://developers.openai.com/api/docs/guides/responses-multi-agent#how-multi-agent-works) distinguish an interrupted turn from other tool actions; [Codex subagent settings](https://learn.chatgpt.com/docs/agent-configuration/subagents#global-settings) describe an interruption message. These descriptions do not establish parent access to child exec termination. The local unsupported/PAUSED conclusion rests on the actual observations above, not a universal claim about Codex or Claude products.

| Criterion | Additional evidence | Remaining boundary |
| --- | --- | --- |
| C3 | interrupted turn corroborated; sleep probe not run | tool termination/replacement and Claude native bridge unverified |
| C4 | unchanged live Task/Claim mapping observed | no live supervisor/Claim transition |
| C8 | bounded children stopped without external actions | no complete native recovered workflow |
| C11 | actual child cwd escape caused PAUSED_NO_WRITE | bounded case, no broader runtime confinement claim |
| C12 | two healthy native children serialized in valid Task root | native interruption/replacement remains unverified |
| C14 | prior component crash tests retained | no Registry-approved live successor recovery |

Deliberately changing a live Claim/supervisor or invoking successor recovery still requires the existing separately approved Task lifecycle procedure. No new Task/Claim was created merely for proof. Current gaps must remain PARTIAL or NOT PASS until corresponding live evidence exists.

<!-- backlog-suite-archive:begin -->
```json
{
  "schema": 1,
  "kind": "archived-test-records",
  "historical_result_commit": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
  "historical_parent_head": "3c09cde6d380900b8202e9a89968817c4e036e71",
  "historical_tested_working_change_digest": "04ccbb465cb7e2d95145a7919656fef1a1d5d11106c3ae33e13a8aeaab7eee97",
  "scope": "Exact archived UTF-8 file bytes, source consistency and reproduction inputs; not independent historical execution attestation, native recovery or live Claim-transition proof",
  "metadata_only_delta": [
    "docs/validation/2026-11-04-backlog-drainer.md"
  ],
  "source_hashes": {
    "scripts/backlog_checkpoint/__init__.py": "1c62a4c05db819c7d779f2c22acd2af63b56fb4f648ef4ae236af838a307da2a",
    "scripts/backlog_checkpoint/admission.py": "111d8b8e147de690ccab6cc9ae23a1faeabee2aefb0a41d57add9f24e36f2bca",
    "scripts/backlog_checkpoint/cli.py": "9f0d03dfd085113e90e96dcde996054231b19b0c385df6b5d9713183a3fbe7b6",
    "scripts/backlog_checkpoint/codec.py": "aef3989e5a3eed08cf2f32a994191a7fc42f62b0eb531b02787cfb68d9c28179",
    "scripts/backlog_checkpoint/git_evidence.py": "9553b5b20c700c39fdefa0ab129495faa46b3ae2899ebb1472b938f0b5730083",
    "scripts/backlog_checkpoint/queue.py": "cd889886dde12c433278c3451b9cd9bc931c7bdaa1dc2acd56bf825479841ffd",
    "scripts/backlog_checkpoint/storage.py": "fe1eb9a352c5a43542c939e03afb8b90a24acc4b2ef37c4711ef73e1e321a82a",
    "scripts/backlog-checkpoint.py": "95c09efb70b907f1fad1f07a9c74d56211127c91be70c5e27ad76266dd617526",
    "scripts/install-backlog-drain.py": "8df080971d07066c585d81c40fe1d40ac644ae96582f3991bd6cd015f62ecc19",
    "install.sh": "e498f4565c4fbc89d7b7b6a116b497fed94a73ac4a7fe675ba6a9907b7364ecf",
    "tests/test_backlog_admission.py": "0a3e9481e1c31b4ce6db827553937656f3b4b05e5a671dcf73f51947e4cc08e7",
    "tests/test_backlog_checkpoint.py": "3c5a82ed94825358f6645e75543155f29c41c5ea9208e38ebed4f36202c45e14",
    "tests/test_backlog_cli.py": "655c3e1d5b31dbc38753a85adc8fefdb14df3c1000dd69a97b929cd11d18456a",
    "tests/test_backlog_git_evidence.py": "346b9a91c7dae0ede35db4abecfea8d6086776ba1cdc56ffbc0f5b70dfbe0f46",
    "tests/test_backlog_install.py": "ce30f2c11ac5dbd0cba6653906f3c3bb108360ad5824035a0b169cd47080458e",
    "tests/test_backlog_queue.py": "4fedd10261709a7321f4113d51933bea850be5bc853a3139afc921b85bf31f8a",
    "skills/backlog-drain/SKILL.md": "dce1c5a6907dd507e342c81229544cc37dab116e9174903d10f060add2a57c85",
    "skills/backlog-drain/references/protocol.md": "7a618109cdaf5e6b15d09735bf4260c019db9489906a683a3eee3c8dfaabfd80"
  },
  "artifacts": [
    {
      "name": "full-suite-input.json",
      "bytes": 4701,
      "sha256": "5ecc42c515143f86eba96494e1d2904ec4d03203894b8c3f4cf0592f336988e7",
      "encoding": "utf-8",
      "data": "{\n  \"head\": \"3c09cde6d380900b8202e9a89968817c4e036e71\",\n  \"command\": \"rtk proxy env -u LD_LIBRARY_PATH \\\"$HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python\\\" -m pytest -q -n 8\",\n  \"cwd\": \"/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31\",\n  \"source_hashes\": {\n    \"scripts/backlog_checkpoint/__init__.py\": \"1c62a4c05db819c7d779f2c22acd2af63b56fb4f648ef4ae236af838a307da2a\",\n    \"scripts/backlog_checkpoint/admission.py\": \"111d8b8e147de690ccab6cc9ae23a1faeabee2aefb0a41d57add9f24e36f2bca\",\n    \"scripts/backlog_checkpoint/cli.py\": \"9f0d03dfd085113e90e96dcde996054231b19b0c385df6b5d9713183a3fbe7b6\",\n    \"scripts/backlog_checkpoint/codec.py\": \"aef3989e5a3eed08cf2f32a994191a7fc42f62b0eb531b02787cfb68d9c28179\",\n    \"scripts/backlog_checkpoint/git_evidence.py\": \"9553b5b20c700c39fdefa0ab129495faa46b3ae2899ebb1472b938f0b5730083\",\n    \"scripts/backlog_checkpoint/queue.py\": \"cd889886dde12c433278c3451b9cd9bc931c7bdaa1dc2acd56bf825479841ffd\",\n    \"scripts/backlog_checkpoint/storage.py\": \"fe1eb9a352c5a43542c939e03afb8b90a24acc4b2ef37c4711ef73e1e321a82a\",\n    \"scripts/backlog-checkpoint.py\": \"95c09efb70b907f1fad1f07a9c74d56211127c91be70c5e27ad76266dd617526\",\n    \"scripts/install-backlog-drain.py\": \"8df080971d07066c585d81c40fe1d40ac644ae96582f3991bd6cd015f62ecc19\",\n    \"install.sh\": \"e498f4565c4fbc89d7b7b6a116b497fed94a73ac4a7fe675ba6a9907b7364ecf\",\n    \"tests/test_backlog_admission.py\": \"0a3e9481e1c31b4ce6db827553937656f3b4b05e5a671dcf73f51947e4cc08e7\",\n    \"tests/test_backlog_checkpoint.py\": \"3c5a82ed94825358f6645e75543155f29c41c5ea9208e38ebed4f36202c45e14\",\n    \"tests/test_backlog_cli.py\": \"655c3e1d5b31dbc38753a85adc8fefdb14df3c1000dd69a97b929cd11d18456a\",\n    \"tests/test_backlog_git_evidence.py\": \"346b9a91c7dae0ede35db4abecfea8d6086776ba1cdc56ffbc0f5b70dfbe0f46\",\n    \"tests/test_backlog_install.py\": \"ce30f2c11ac5dbd0cba6653906f3c3bb108360ad5824035a0b169cd47080458e\",\n    \"tests/test_backlog_queue.py\": \"4fedd10261709a7321f4113d51933bea850be5bc853a3139afc921b85bf31f8a\",\n    \"skills/backlog-drain/SKILL.md\": \"dce1c5a6907dd507e342c81229544cc37dab116e9174903d10f060add2a57c85\",\n    \"skills/backlog-drain/references/protocol.md\": \"7a618109cdaf5e6b15d09735bf4260c019db9489906a683a3eee3c8dfaabfd80\"\n  },\n  \"working_manifest\": {\n    \"paths\": [\n      \"docs/validation/2026-11-04-backlog-drainer.md\",\n      \"scripts/backlog_checkpoint/admission.py\",\n      \"scripts/backlog_checkpoint/git_evidence.py\",\n      \"scripts/backlog_checkpoint/storage.py\",\n      \"skills/backlog-drain/references/protocol.md\",\n      \"tests/test_backlog_admission.py\",\n      \"tests/test_backlog_checkpoint.py\",\n      \"tests/test_backlog_git_evidence.py\"\n    ],\n    \"count\": 8,\n    \"path_set_digest\": \"8f8b21c0e79965aba7e6e708a8e8eaa68da407c0d30f8d127aa91ba96ac1dd51\",\n    \"change_digest\": \"04ccbb465cb7e2d95145a7919656fef1a1d5d11106c3ae33e13a8aeaab7eee97\",\n    \"sources\": [\n      {\n        \"change\": \"modify\",\n        \"mode\": \"100644\",\n        \"path\": \"docs/validation/2026-11-04-backlog-drainer.md\",\n        \"sha256\": \"3752f28445dbb7d26a71610370a1b12bc8828be4957eb225c25d354aed7bd15c\"\n      },\n      {\n        \"change\": \"modify\",\n        \"mode\": \"100644\",\n        \"path\": \"scripts/backlog_checkpoint/admission.py\",\n        \"sha256\": \"111d8b8e147de690ccab6cc9ae23a1faeabee2aefb0a41d57add9f24e36f2bca\"\n      },\n      {\n        \"change\": \"modify\",\n        \"mode\": \"100644\",\n        \"path\": \"scripts/backlog_checkpoint/git_evidence.py\",\n        \"sha256\": \"9553b5b20c700c39fdefa0ab129495faa46b3ae2899ebb1472b938f0b5730083\"\n      },\n      {\n        \"change\": \"modify\",\n        \"mode\": \"100644\",\n        \"path\": \"scripts/backlog_checkpoint/storage.py\",\n        \"sha256\": \"fe1eb9a352c5a43542c939e03afb8b90a24acc4b2ef37c4711ef73e1e321a82a\"\n      },\n      {\n        \"change\": \"modify\",\n        \"mode\": \"100644\",\n        \"path\": \"skills/backlog-drain/references/protocol.md\",\n        \"sha256\": \"7a618109cdaf5e6b15d09735bf4260c019db9489906a683a3eee3c8dfaabfd80\"\n      },\n      {\n        \"change\": \"modify\",\n        \"mode\": \"100644\",\n        \"path\": \"tests/test_backlog_admission.py\",\n        \"sha256\": \"0a3e9481e1c31b4ce6db827553937656f3b4b05e5a671dcf73f51947e4cc08e7\"\n      },\n      {\n        \"change\": \"modify\",\n        \"mode\": \"100644\",\n        \"path\": \"tests/test_backlog_checkpoint.py\",\n        \"sha256\": \"3c5a82ed94825358f6645e75543155f29c41c5ea9208e38ebed4f36202c45e14\"\n      },\n      {\n        \"change\": \"modify\",\n        \"mode\": \"100644\",\n        \"path\": \"tests/test_backlog_git_evidence.py\",\n        \"sha256\": \"346b9a91c7dae0ede35db4abecfea8d6086776ba1cdc56ffbc0f5b70dfbe0f46\"\n      }\n    ],\n    \"parent_head\": \"3c09cde6d380900b8202e9a89968817c4e036e71\"\n  }\n}"
    },
    {
      "name": "full-suite-result.json",
      "bytes": 818,
      "sha256": "8e973466761fee6721cfead7f0ba6b361d02d2a22f7063ba5b145b89e578a95f",
      "encoding": "utf-8",
      "data": "{\n  \"command\": \"rtk proxy env -u LD_LIBRARY_PATH \\\"$HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python\\\" -m pytest -q -n 8\",\n  \"exit_code\": 0,\n  \"elapsed_seconds\": 230.94,\n  \"source_hashes_unchanged\": true,\n  \"capture_sha256\": \"7e53707baf6f84034ec034c716030276780e680a6710e896ba1622045df7cb44\",\n  \"log_bytes\": 6156,\n  \"summary_tail\": \"........................................................................ [ 97%]\\n........................................................................ [ 98%]\\n........................................................................ [ 99%]\\n...........                                                              [100%]\\n5411 passed in 230.77s (0:03:50)\\n\",\n  \"working_change_digest\": \"04ccbb465cb7e2d95145a7919656fef1a1d5d11106c3ae33e13a8aeaab7eee97\"\n}"
    },
    {
      "name": "full-suite.log",
      "bytes": 6156,
      "sha256": "7e53707baf6f84034ec034c716030276780e680a6710e896ba1622045df7cb44",
      "encoding": "utf-8",
      "data": "bringing up nodes...\nbringing up nodes...\n\n........................................................................ [  1%]\n........................................................................ [  2%]\n........................................................................ [  3%]\n........................................................................ [  5%]\n........................................................................ [  6%]\n........................................................................ [  7%]\n........................................................................ [  9%]\n........................................................................ [ 10%]\n........................................................................ [ 11%]\n........................................................................ [ 13%]\n........................................................................ [ 14%]\n........................................................................ [ 15%]\n........................................................................ [ 17%]\n........................................................................ [ 18%]\n........................................................................ [ 19%]\n........................................................................ [ 21%]\n........................................................................ [ 22%]\n........................................................................ [ 23%]\n........................................................................ [ 25%]\n........................................................................ [ 26%]\n........................................................................ [ 27%]\n........................................................................ [ 29%]\n........................................................................ [ 30%]\n........................................................................ [ 31%]\n........................................................................ [ 33%]\n........................................................................ [ 34%]\n........................................................................ [ 35%]\n........................................................................ [ 37%]\n........................................................................ [ 38%]\n........................................................................ [ 39%]\n........................................................................ [ 41%]\n........................................................................ [ 42%]\n........................................................................ [ 43%]\n........................................................................ [ 45%]\n........................................................................ [ 46%]\n........................................................................ [ 47%]\n........................................................................ [ 49%]\n........................................................................ [ 50%]\n........................................................................ [ 51%]\n........................................................................ [ 53%]\n........................................................................ [ 54%]\n........................................................................ [ 55%]\n........................................................................ [ 57%]\n........................................................................ [ 58%]\n........................................................................ [ 59%]\n........................................................................ [ 61%]\n........................................................................ [ 62%]\n........................................................................ [ 63%]\n........................................................................ [ 65%]\n........................................................................ [ 66%]\n........................................................................ [ 67%]\n........................................................................ [ 69%]\n........................................................................ [ 70%]\n........................................................................ [ 71%]\n........................................................................ [ 73%]\n........................................................................ [ 74%]\n........................................................................ [ 75%]\n........................................................................ [ 77%]\n........................................................................ [ 78%]\n........................................................................ [ 79%]\n........................................................................ [ 81%]\n........................................................................ [ 82%]\n........................................................................ [ 83%]\n........................................................................ [ 85%]\n........................................................................ [ 86%]\n........................................................................ [ 87%]\n........................................................................ [ 89%]\n........................................................................ [ 90%]\n........................................................................ [ 91%]\n........................................................................ [ 93%]\n........................................................................ [ 94%]\n........................................................................ [ 95%]\n........................................................................ [ 97%]\n........................................................................ [ 98%]\n........................................................................ [ 99%]\n...........                                                              [100%]\n5411 passed in 230.77s (0:03:50)\n"
    },
    {
      "name": "commit-manifest.json",
      "bytes": 2271,
      "sha256": "2b97ba270fbf92a61f1b2677de06c4209db84c3f4641d9d32d3d6cea2851ecd3",
      "encoding": "utf-8",
      "data": "{\n  \"paths\": [\n    \"docs/validation/2026-11-04-backlog-drainer.md\",\n    \"scripts/backlog_checkpoint/admission.py\",\n    \"scripts/backlog_checkpoint/git_evidence.py\",\n    \"scripts/backlog_checkpoint/storage.py\",\n    \"skills/backlog-drain/references/protocol.md\",\n    \"tests/test_backlog_admission.py\",\n    \"tests/test_backlog_checkpoint.py\",\n    \"tests/test_backlog_git_evidence.py\"\n  ],\n  \"count\": 8,\n  \"path_set_digest\": \"8f8b21c0e79965aba7e6e708a8e8eaa68da407c0d30f8d127aa91ba96ac1dd51\",\n  \"change_digest\": \"864bcbfe98afeed1ed0aca66e593dc9a180bf1bea85c493e20dccff84a7e2f22\",\n  \"sources\": [\n    {\n      \"change\": \"modify\",\n      \"mode\": \"100644\",\n      \"path\": \"docs/validation/2026-11-04-backlog-drainer.md\",\n      \"sha256\": \"405ed8818b79f02aa55dc4bb09de22f0ec24c12fbc23213d879faab381283845\"\n    },\n    {\n      \"change\": \"modify\",\n      \"mode\": \"100644\",\n      \"path\": \"scripts/backlog_checkpoint/admission.py\",\n      \"sha256\": \"111d8b8e147de690ccab6cc9ae23a1faeabee2aefb0a41d57add9f24e36f2bca\"\n    },\n    {\n      \"change\": \"modify\",\n      \"mode\": \"100644\",\n      \"path\": \"scripts/backlog_checkpoint/git_evidence.py\",\n      \"sha256\": \"9553b5b20c700c39fdefa0ab129495faa46b3ae2899ebb1472b938f0b5730083\"\n    },\n    {\n      \"change\": \"modify\",\n      \"mode\": \"100644\",\n      \"path\": \"scripts/backlog_checkpoint/storage.py\",\n      \"sha256\": \"fe1eb9a352c5a43542c939e03afb8b90a24acc4b2ef37c4711ef73e1e321a82a\"\n    },\n    {\n      \"change\": \"modify\",\n      \"mode\": \"100644\",\n      \"path\": \"skills/backlog-drain/references/protocol.md\",\n      \"sha256\": \"7a618109cdaf5e6b15d09735bf4260c019db9489906a683a3eee3c8dfaabfd80\"\n    },\n    {\n      \"change\": \"modify\",\n      \"mode\": \"100644\",\n      \"path\": \"tests/test_backlog_admission.py\",\n      \"sha256\": \"0a3e9481e1c31b4ce6db827553937656f3b4b05e5a671dcf73f51947e4cc08e7\"\n    },\n    {\n      \"change\": \"modify\",\n      \"mode\": \"100644\",\n      \"path\": \"tests/test_backlog_checkpoint.py\",\n      \"sha256\": \"3c5a82ed94825358f6645e75543155f29c41c5ea9208e38ebed4f36202c45e14\"\n    },\n    {\n      \"change\": \"modify\",\n      \"mode\": \"100644\",\n      \"path\": \"tests/test_backlog_git_evidence.py\",\n      \"sha256\": \"346b9a91c7dae0ede35db4abecfea8d6086776ba1cdc56ffbc0f5b70dfbe0f46\"\n    }\n  ],\n  \"parent_head\": \"3c09cde6d380900b8202e9a89968817c4e036e71\"\n}"
    },
    {
      "name": "post-commit-binding.json",
      "bytes": 413,
      "sha256": "409a4091d13f15f5f2535dbffd72b4010019bf6f2744ef4adfde9cb010754867",
      "encoding": "utf-8",
      "data": "{\n  \"binding\": {\n    \"commit_parent\": \"3c09cde6d380900b8202e9a89968817c4e036e71\",\n    \"result_commit\": \"5bf46737048dd81a5b14e5e122095023c2ed41fb\",\n    \"source_count\": 8,\n    \"path_set_digest\": \"8f8b21c0e79965aba7e6e708a8e8eaa68da407c0d30f8d127aa91ba96ac1dd51\",\n    \"change_digest\": \"864bcbfe98afeed1ed0aca66e593dc9a180bf1bea85c493e20dccff84a7e2f22\"\n  },\n  \"source_hashes_unchanged\": true,\n  \"source_clean\": true\n}"
    }
  ]
}
```
<!-- backlog-suite-archive:end -->


### Bounded native observation data

This packet contains fixture result records and parent observations projected from the retained native traces. It preserves their stated scope and failure outcomes; it is not an independent historical attestation or a native recovery PASS. Raw full responses and transcripts remain private. No raw session ID, token or full environment is published.

<!-- backlog-native-observations:begin -->
```json
{
  "schema": 1,
  "kind": "bounded-native-observations",
  "observed_head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
  "authority": "Task/Claim fields and native identities are observations only; no new permission or ownership is conferred",
  "serial_1": {
    "artifact": {
      "case_id": "issue31-native-preflight-serial-1",
      "checks": [
        {
          "command": "rtk /home/jhw/.local/bin/jhw-control-host task status --task <task> --claim <claim>",
          "exit": 0,
          "result": "exact task/claim/worktree_ref/branch/head matched; dirty=false"
        },
        {
          "command": "rtk pwd -P",
          "exit": 0,
          "result": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31"
        },
        {
          "command": "rtk git rev-parse --show-toplevel",
          "exit": 0,
          "result": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31"
        },
        {
          "command": "rtk git branch --show-current",
          "exit": 0,
          "result": "task/8b1902dad18f-jhw7500-claude-config-31"
        },
        {
          "command": "rtk git rev-parse HEAD",
          "exit": 0,
          "result": "5bf46737048dd81a5b14e5e122095023c2ed41fb"
        },
        {
          "command": "rtk git status --porcelain=v1 --untracked-files=all",
          "exit": 0,
          "result": "empty"
        },
        {
          "command": "rtk proxy git worktree list --porcelain",
          "exit": 0,
          "result": "one exact matching physical-root record"
        },
        {
          "command": "rtk proxy /usr/bin/python3 scripts/backlog-checkpoint.py git collect --root <root> --input -",
          "exit": 0,
          "result": "count=0,parent_head=5bf46737048dd81a5b14e5e122095023c2ed41fb"
        },
        {
          "command": "rtk proxy stat -c <format> <artifact>",
          "exit": 1,
          "result": "artifact absent before exclusive creation"
        }
      ],
      "claim_granted_or_changed": false,
      "input_sha256": {
        ".superpowers/sdd/2026-11-04-backlog-drainer/gap-audit/scope-input.json": "3264a58c24591d003bc349a00c52f3347bb7c51a77a4725e1f53936d88e67ee0",
        "skills/backlog-drain/SKILL.md": "dce1c5a6907dd507e342c81229544cc37dab116e9174903d10f060add2a57c85",
        "skills/backlog-drain/references/protocol.md": "7a618109cdaf5e6b15d09735bf4260c019db9489906a683a3eee3c8dfaabfd80"
      },
      "lifecycle_action": "none",
      "lifecycle_mutation": false,
      "matched_scope": {
        "branch": "task/8b1902dad18f-jhw7500-claude-config-31",
        "claim_id": "clm-01a1a6a9-410d-7963-b922-d5f46ea6766a",
        "head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
        "root": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31",
        "task_id": "tsk-01a1570c-89ce-7cf6-8cd8-8b1902dad18f",
        "worktree_ref": "wt-8b1902dad18f-jhw7500-claude-config-31"
      },
      "observations": {
        "branch": "task/8b1902dad18f-jhw7500-claude-config-31",
        "change_digest": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        "empty_allowlist_count": 0,
        "git_root": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31",
        "head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
        "launcher_claim_match": true,
        "launcher_worktree_dirty": false,
        "path_set_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "physical_cwd": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31",
        "source_clean": true,
        "unique_worktree_record": {
          "branch_ref": "refs/heads/task/8b1902dad18f-jhw7500-claude-config-31",
          "head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
          "root": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31"
        }
      },
      "product_backlog_run_started_or_resumed": false,
      "schema": 1,
      "verdict": "PASS",
      "write_process": {
        "creation": "O_CREAT|O_EXCL|O_NOFOLLOW",
        "fd_close_completed": true,
        "fd_fsync_completed": true,
        "pid": 1811618,
        "requested_mode": "0600"
      }
    },
    "parent_observation": {
      "case": "serial-1",
      "native_task_complete": true,
      "tool_calls": 12,
      "tool_outputs": 12,
      "unreturned_calls": [],
      "native_tool_result_metadata": [
        {
          "exit_code": 1,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 1,
          "has_session": false
        }
      ],
      "artifact_sha256": "e1d33c1ba287e7bc3e9774de90ccd760d4204372c8e3aaabb4e13398330a8625",
      "lifecycle_mutation": false,
      "scope": "healthy bounded native step only, not interruption recovery"
    }
  },
  "serial_2": {
    "artifact": {
      "actual_commands": {
        "git_branch": {
          "exit": 0,
          "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "stdout_sha256": "9c20109440d798b68c0498e6f48fe9166b6d432125cb08b5abd417db6b8e4798"
        },
        "git_collect_empty_allowlist": {
          "exit": 0,
          "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "stdout_sha256": "6ebf20cd2011421c1f1f2d0c11645fa42abf1ce6adafc48261741da95f0a15da"
        },
        "git_head": {
          "exit": 0,
          "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "stdout_sha256": "0c8a9535b905663df8baca0de51ef19a0389c30998eaa2c1d594fa3ef0d70e2f"
        },
        "git_inspect": {
          "exit": 0,
          "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "stdout_sha256": "06dc2930ab8199daae5925c012b9a017358e8607094e9217e58e1e539bee1054"
        },
        "git_status": {
          "exit": 0,
          "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "stdout_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        },
        "git_worktree_list": {
          "exit": 0,
          "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "stdout_sha256": "f8ae645c18f0d0100d6f1c21a9efbc2ee186b41401a7f3e48b65a8dd7c04b3e4"
        },
        "task_status": {
          "exit": 0,
          "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "stdout_sha256": "bea28854ab8563564aea28b413b31b0d50ced5a82470da16b601699f6957cb5f"
        }
      },
      "case": "serial-2",
      "lifecycle_mutation": false,
      "matched_scope": {
        "branch": "task/8b1902dad18f-jhw7500-claude-config-31",
        "claim_id": "clm-01a1a6a9-410d-7963-b922-d5f46ea6766a",
        "head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
        "live_claim_match": true,
        "physical_root": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31",
        "source_clean": true,
        "task_id": "tsk-01a1570c-89ce-7cf6-8cd8-8b1902dad18f",
        "unique_live_worktree_record": true,
        "worktree_ref": "wt-8b1902dad18f-jhw7500-claude-config-31"
      },
      "observed": {
        "collect": {
          "change_digest": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
          "count": 0,
          "parent_head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
          "path_set_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "paths": [],
          "sources": []
        },
        "guide_sha256": "dce1c5a6907dd507e342c81229544cc37dab116e9174903d10f060add2a57c85",
        "parent_tool_calls": 12,
        "parent_tool_outputs": 12,
        "parent_unreturned_calls": [],
        "parent_verification_sha256": "c51238885fc19091fc509d9442da23c4415cc04c9723e6841f3720605ff1dcb9",
        "prior_artifact_sha256": "e1d33c1ba287e7bc3e9774de90ccd760d4204372c8e3aaabb4e13398330a8625",
        "protocol_sha256": "7a618109cdaf5e6b15d09735bf4260c019db9489906a683a3eee3c8dfaabfd80",
        "scope_input_sha256": "3264a58c24591d003bc349a00c52f3347bb7c51a77a4725e1f53936d88e67ee0"
      },
      "prior_artifact_unchanged": true,
      "purpose": "bounded native preflight/serial-writer verification fixture; no product backlog run or independent Claim",
      "quiescence": {
        "background_work": false,
        "children_started": false,
        "foreground_commands": "terminal",
        "retained_tool_sessions": false,
        "writers": "quiescent"
      },
      "schema": 1,
      "tool_terminal_observed": true,
      "verdict": "PASS",
      "write": {
        "exclusive_create": true,
        "fsync_before_close": true,
        "mode": "0600",
        "no_follow": true,
        "pid": 1841354
      }
    },
    "parent_observation": {
      "case": "serial-2",
      "native_task_complete": true,
      "tool_calls": 5,
      "tool_outputs": 5,
      "unreturned_calls": [],
      "native_tool_result_metadata": [
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        }
      ],
      "tool_output_forms": [
        "content-block array"
      ],
      "artifact_sha256": "c7f041e4b4104d82aab040002d9aa9f81b4ea5e7dbdce0541e094a1bc6570a53",
      "serial_1_artifact_unchanged": true,
      "lifecycle_mutation": false,
      "scope": "healthy serial child after serial-1 parent verification; not interruption recovery"
    }
  },
  "cwd_escape": {
    "reported_outcome": {
      "verdict": "PAUSED_NO_WRITE",
      "observed": {
        "cwd": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31/.superpowers/sdd/2026-11-04-backlog-drainer/gap-audit/cwd-escape",
        "physical_git_root": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31",
        "branch": "task/8b1902dad18f-jhw7500-claude-config-31",
        "head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
        "task_mapping_match": true,
        "claim_mapping_match": true,
        "worktree_clean": true
      },
      "mismatched_predicate": {
        "name": "exact_cwd_equals_selected_physical_worktree",
        "required": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31",
        "actual": "/home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31/.superpowers/sdd/2026-11-04-backlog-drainer/gap-audit/cwd-escape",
        "matched": false
      },
      "commands": [
        {
          "command": "rtk /bin/bash -lc 'pwd -P; git rev-parse --show-toplevel; git branch --show-current; git rev-parse HEAD; git status --short --branch'",
          "exit": 0
        },
        {
          "command": "rtk sed -n '1,260p' /home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31/skills/backlog-drain/SKILL.md",
          "exit": 0
        },
        {
          "command": "rtk sed -n '1,360p' /home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31/skills/backlog-drain/references/protocol.md",
          "exit": 0
        },
        {
          "command": "rtk rg -n 'live.*Task|task.*status|claim_id|worktree_root|jhw:task|task show|claim show' <authorized-root>/skills <authorized-root>/docs <authorized-root>/scripts",
          "exit": 0
        },
        {
          "command": "rtk sed -n '450,500p' <authorized-root>/docs/plans/2026-08-27-jhw-control-host-v4-lifecycle.md",
          "exit": 2
        },
        {
          "command": "rtk rg --files <authorized-root> | rtk rg '2026-08-27-jhw-control-host-v4-lifecycle.md$|jhw-task/SKILL.md$|task-nudge'",
          "exit": 0
        },
        {
          "command": "rtk sed -n '1,260p' /home/jhw/.codex/skills/jhw-task/SKILL.md",
          "exit": 0
        },
        {
          "command": "rtk sed -n '1,360p' /home/jhw/.codex/skills/jhw-task/references/task.md",
          "exit": 0
        },
        {
          "command": "rtk /home/jhw/.local/bin/jhw-control-host task status --task tsk-01a1570c-89ce-7cf6-8cd8-8b1902dad18f --claim clm-01a1a6a9-410d-7963-b922-d5f46ea6766a",
          "exit": 0
        },
        {
          "command": "rtk sha256sum <authorized-root>/skills/backlog-drain/SKILL.md <authorized-root>/skills/backlog-drain/references/protocol.md",
          "exit": 0
        },
        {
          "command": "rtk /bin/bash -lc '<check candidate absence; print physical cwd, Git root, branch, HEAD, and status>'",
          "exit": 0
        },
        {
          "command": "rtk /home/jhw/.local/bin/jhw-control-host task status --task tsk-01a1570c-89ce-7cf6-8cd8-8b1902dad18f --claim clm-01a1a6a9-410d-7963-b922-d5f46ea6766a",
          "exit": 0
        }
      ],
      "hashes": {
        "skills/backlog-drain/SKILL.md": "dce1c5a6907dd507e342c81229544cc37dab116e9174903d10f060add2a57c85",
        "skills/backlog-drain/references/protocol.md": "7a618109cdaf5e6b15d09735bf4260c019db9489906a683a3eee3c8dfaabfd80",
        "git_head": "5bf46737048dd81a5b14e5e122095023c2ed41fb"
      },
      "canary_created": false,
      "lifecycle_mutation": false,
      "admission_or_checkpoint_created": false,
      "git_mutation": false,
      "quiescence": {
        "foreground_tools_terminal": true,
        "background_work_started": false,
        "native_children_spawned": false,
        "writers_quiescent": true,
        "tools_quiescent": true
      }
    },
    "parent_observation": {
      "native_task_complete": true,
      "tool_calls": 12,
      "tool_outputs": 12,
      "tool_names": [
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec"
      ],
      "unreturned_calls": [],
      "tool_records": {
        "call_626ba1d593fa4d9998274389091f1294": {
          "name": "exec",
          "record_sha256": "e2e5ecd07ccdda9cd655f39f11c950b03aa6711a2dba9dea106d1b525dc1f6f9"
        },
        "call_2fb0538a312f41c99dbbff2d71567f9b": {
          "name": "exec",
          "record_sha256": "68f9f844ab9a82b48ecbc18e3c41690fe8cf85fa8d738016d68fcfe478abd0ee"
        },
        "call_616f8d649793422698a6cb15aa4aeae2": {
          "name": "exec",
          "record_sha256": "5e2714bd9c405d8109ecbc54ee60c0cae3b99f13d5c5c2802e232a0259736932"
        },
        "call_c6850f75b0e7407494c131ac81dbf933": {
          "name": "exec",
          "record_sha256": "931693c5e744e4e901bf3a95492d89ae944d9dedcb016cff7ec1e3caebcc7b24"
        },
        "call_bf0272450c8d4c9387a30fa3fb1e2655": {
          "name": "exec",
          "record_sha256": "1642d0ec45018853352edb2a0d49510190a9a994b2aabf34d7fe412f657d3cfc"
        },
        "call_d5f76d51423744498ae30d41fd3e8b8b": {
          "name": "exec",
          "record_sha256": "5893670d70f04b210bb4aed5b9afbca9b905123008369fe45bd147f77789d799"
        },
        "call_12e145a11b964c05a00c7f8abf40c53c": {
          "name": "exec",
          "record_sha256": "5d88b2bc7e5feef88ccaa2dcb70e2e2500e5e49486cb2abb3c5df68eb9f47ae3"
        },
        "call_1bf3014a042e4544af6dc419317aed9e": {
          "name": "exec",
          "record_sha256": "b9e6d8d284e1e0828f29d235b36316d1ccfb78bdf67c2bd7bf7340662ab912c6"
        },
        "call_7aae25d37cdb486fb4825b5d86bec04d": {
          "name": "exec",
          "record_sha256": "886434cacaddd5d1ea218c24ae0aa4dcf27cebc213e2844d977bc1e346d4127b"
        },
        "call_034503874d2f40de83bd1fb1121ab18b": {
          "name": "exec",
          "record_sha256": "75542c0de75942c1dac7c5619480ec38d50837cae3fe3fd56099ad08368edaf3"
        },
        "call_4520124efc37467caa14ad93c39ac835": {
          "name": "exec",
          "record_sha256": "f799854a5da7056095c83f37834a7865f58aec8d821ef9edd086bb57528a2aa7"
        },
        "call_80cc9cf196bb47f2ac006050bed18680": {
          "name": "exec",
          "record_sha256": "bb820889bc678df8388ebb2be15a9d382f70956cbf9c6335f69e9ff22cad67f3"
        }
      },
      "output_record_hashes": {
        "call_626ba1d593fa4d9998274389091f1294": "a0dff35a73afdff7cf6b89820d7c79d7fb93401b9f38c363d76a0a97b3861c5d",
        "call_2fb0538a312f41c99dbbff2d71567f9b": "2c4db0ae3712721cc125be7c9e2d422296b928be8702328a681026c85922c45f",
        "call_616f8d649793422698a6cb15aa4aeae2": "6736100f66e9eb9e75ccf7b0fe5e0916d89803fba8c3aec25ac1d745a2527360",
        "call_c6850f75b0e7407494c131ac81dbf933": "15ac6be15d87d91846e6f02e99e6d701e227b334e78a2b63a11f2ab89cd0b8b1",
        "call_bf0272450c8d4c9387a30fa3fb1e2655": "18da367b3903a2e03210b10ba12608f0989e9d0644de8623a9ac1642da7d3cff",
        "call_d5f76d51423744498ae30d41fd3e8b8b": "ea8c7bfe1ea5e05d8e167e0fd38b2e157e517d4c7dfb5d7c1e61a5dc072a3a84",
        "call_12e145a11b964c05a00c7f8abf40c53c": "cd40fa52fd9d122fd98ac3f156f96a59f64263ae2a931aeca2bf1fcc02b57e6e",
        "call_1bf3014a042e4544af6dc419317aed9e": "f67246e0d6f4073293236cee066ee06a2cdb6bddea3441d7d47a7af9f91c6a7e",
        "call_7aae25d37cdb486fb4825b5d86bec04d": "7e71db29f2e752c491222ad361dc699d58931d432cef3ce162cfa976fb97470e",
        "call_034503874d2f40de83bd1fb1121ab18b": "c623f9bcb8e8af982b67a330a0595f20f89aa65ec46ad457bc20b7571c984954",
        "call_4520124efc37467caa14ad93c39ac835": "fbf00d30265d35fb8569540091a4dc03ca8a4db9247b95c5d3ca6a996cf1c7bf",
        "call_80cc9cf196bb47f2ac006050bed18680": "24574de8a2bac9fd56dbd04045f776d5742dbedd82ac7271801dd6459103c222"
      },
      "native_tool_result_metadata": [
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 2,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        },
        {
          "exit_code": 0,
          "has_session": false
        }
      ],
      "verdict": "PAUSED_NO_WRITE",
      "canary_absent": true,
      "lifecycle_mutation": false,
      "scope": "actual native child cwd-escape guard on unchanged live Task/Claim; not supervisor takeover or interruption recovery"
    }
  },
  "interrupted_original": {
    "case": "pre-edit original child",
    "selected_records": [
      {
        "line": 18,
        "type": "custom_tool_call_output",
        "reason": null,
        "record_sha256": "2389051c06344fad3bd2ff183ea316b3d65ee77b6edc0be99e74b7e406b98ab6"
      },
      {
        "line": 59,
        "type": "turn_aborted",
        "reason": "interrupted",
        "record_sha256": "f213b4aba02e7fbbfe1d1ff533de3e0945c12c59b1c6349bf9360e399f942a2a"
      }
    ],
    "selected_raw_sha256": "d138f7460091b67d9e7b0b6b09df7a4d07d36befa7a0b408bf8d2c095df7d77e",
    "observed_turn_aborted": true,
    "tool_terminal_receipt_present": false,
    "native_recovery_pass": false,
    "scope": "existing evidence only; no old child was woken and no new recovery experiment ran"
  },
  "original_parent_lookup_failure": [
    {
      "call_id": "call_1b5b0f29407342cc8a322d55737eb0dc",
      "text": "Script error:\nwrite_stdin failed: Unknown process id 98446",
      "source_record_sha256": "b22a1712d460297456a0b23d341e42d58f1448427779cda9ded69ddec2a5b344"
    }
  ],
  "hosted_sleep_probe": {
    "reported_outcome": {
      "case": "clock-terminal-probe",
      "error": "clock.sleep tool unavailable",
      "writers": 0,
      "processes": 0,
      "lifecycle_mutation": false
    },
    "parent_observation": {
      "native_task_complete": true,
      "tool_calls": 1,
      "tool_outputs": 1,
      "tool_names": [
        "send_message"
      ],
      "unreturned_calls": [],
      "tool_records": {
        "call_5e6de68d1d8640608bccfcd85636fd4f": {
          "name": "send_message",
          "record_sha256": "d9f2e0e7037452f64c5ed169c1f260a9654442a03a2ddc8fd022f832da85898d"
        }
      },
      "output_record_hashes": {
        "call_5e6de68d1d8640608bccfcd85636fd4f": "9e6d90081a8b0e27fb756492c5ce420b866307981490a59dce6c7440886474d5"
      },
      "native_tool_result_metadata": [],
      "verdict": "NOT_RUN_UNAVAILABLE",
      "child_reported": "clock.sleep tool unavailable",
      "native_sleep_call_seen": false,
      "actual_interruption_seen": false,
      "native_recovery_pass": false,
      "lifecycle_mutation": false
    }
  },
  "scope_limits": [
    "No new native recovery PASS",
    "No Claude native bridge used",
    "No live supervisor/Claim transition or successor recovery",
    "No Task lifecycle mutation, new Task/Claim, PR, merge or next issue",
    "Projected observation records are not exact copies of full private native transcripts"
  ]
}
```
<!-- backlog-native-observations:end -->


### Fresh independent full-suite capture

<!-- backlog-fresh-suite:begin -->
```json
{
  "schema": 1,
  "kind": "fresh-independent-full-suite-observation",
  "head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
  "tree": "56698cd69026d25d357ed766eb255f636ef835f8",
  "native_observer": "/root/issue31_fresh_full_evidence",
  "before": {
    "head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
    "tree": "56698cd69026d25d357ed766eb255f636ef835f8",
    "clean": true,
    "coordinate_capture_sha256": "d8975165fc124c0376c68217e85bcf455ac6c5613160e3157eb6071792123bb6"
  },
  "after": {
    "head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
    "tree": "56698cd69026d25d357ed766eb255f636ef835f8",
    "clean": true,
    "coordinate_capture_sha256": "d8975165fc124c0376c68217e85bcf455ac6c5613160e3157eb6071792123bb6"
  },
  "summary": "5411 passed in 236.31s (0:03:56)",
  "scope_limits": [
    "This is one fresh independent observation of the current committed source only.",
    "It does not retroactively authenticate the earlier 5411-passed, 230.77-second execution.",
    "It does not close native recovery or live Claim-transition evidence gaps.",
    "No Tribunal review, historical-execution attestation, source mutation, index mutation, HEAD mutation, Task mutation, Claim mutation, runtime mutation, installation, model CLI, subagent, or background execution was performed."
  ],
  "trusted_capture_record": {
    "schema": 1,
    "helper": "/home/jhw/.local/share/claude-config/pre_pr_tribunal/cli.py",
    "command": "rtk proxy /usr/bin/env -u LD_LIBRARY_PATH PYTHONDONTWRITEBYTECODE=1 $HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python -m pytest -q -n 8 -p no:cacheprovider",
    "cwd": "/tmp/issue31-full-evidence-3h3t4vz_/checkout",
    "head": "5bf46737048dd81a5b14e5e122095023c2ed41fb",
    "exit_code": 0,
    "timed_out": false,
    "capture_sha256": "21e707b5bfe9def69e9e61d534353c9941965dee0feeffc72f5d12de32417d79",
    "stdout_bytes": 6156,
    "stderr_bytes": 0,
    "output_limited": false,
    "complete": true,
    "stdout": "bringing up nodes...\nbringing up nodes...\n\n........................................................................ [  1%]\n........................................................................ [  2%]\n........................................................................ [  3%]\n........................................................................ [  5%]\n........................................................................ [  6%]\n........................................................................ [  7%]\n........................................................................ [  9%]\n........................................................................ [ 10%]\n........................................................................ [ 11%]\n........................................................................ [ 13%]\n........................................................................ [ 14%]\n........................................................................ [ 15%]\n........................................................................ [ 17%]\n........................................................................ [ 18%]\n........................................................................ [ 19%]\n........................................................................ [ 21%]\n........................................................................ [ 22%]\n........................................................................ [ 23%]\n........................................................................ [ 25%]\n........................................................................ [ 26%]\n........................................................................ [ 27%]\n........................................................................ [ 29%]\n........................................................................ [ 30%]\n........................................................................ [ 31%]\n........................................................................ [ 33%]\n........................................................................ [ 34%]\n........................................................................ [ 35%]\n........................................................................ [ 37%]\n........................................................................ [ 38%]\n........................................................................ [ 39%]\n........................................................................ [ 41%]\n........................................................................ [ 42%]\n........................................................................ [ 43%]\n........................................................................ [ 45%]\n........................................................................ [ 46%]\n........................................................................ [ 47%]\n........................................................................ [ 49%]\n........................................................................ [ 50%]\n........................................................................ [ 51%]\n........................................................................ [ 53%]\n........................................................................ [ 54%]\n........................................................................ [ 55%]\n........................................................................ [ 57%]\n........................................................................ [ 58%]\n........................................................................ [ 59%]\n........................................................................ [ 61%]\n........................................................................ [ 62%]\n........................................................................ [ 63%]\n........................................................................ [ 65%]\n........................................................................ [ 66%]\n........................................................................ [ 67%]\n........................................................................ [ 69%]\n........................................................................ [ 70%]\n........................................................................ [ 71%]\n........................................................................ [ 73%]\n........................................................................ [ 74%]\n........................................................................ [ 75%]\n........................................................................ [ 77%]\n........................................................................ [ 78%]\n........................................................................ [ 79%]\n........................................................................ [ 81%]\n........................................................................ [ 82%]\n........................................................................ [ 83%]\n........................................................................ [ 85%]\n........................................................................ [ 86%]\n........................................................................ [ 87%]\n........................................................................ [ 89%]\n........................................................................ [ 90%]\n........................................................................ [ 91%]\n........................................................................ [ 93%]\n........................................................................ [ 94%]\n........................................................................ [ 95%]\n........................................................................ [ 97%]\n........................................................................ [ 98%]\n........................................................................ [ 99%]\n...........                                                              [100%]\n5411 passed in 236.31s (0:03:56)\n",
    "stderr": ""
  },
  "original_record_bytes": 6862,
  "original_record_sha256": "ad0a0f0b4f39d0563772ab41610a6aba6d3986fcc559e97bb676aee47b1e282b"
}
```
<!-- backlog-fresh-suite:end -->


### Read-only evidence packet verification

The following reader only parses committed JSON data, hashes bytes and checks the explicitly stated limits. It neither executes archived commands nor writes files. Its success establishes historical record/Git-blob consistency and a complete stored capture; it does not verify later working-source fixes, change the Tribunal verdict or prove native recovery.

```bash
rtk proxy python3 -c 'import hashlib,json,subprocess; from pathlib import Path; s=Path("docs/validation/2026-11-04-backlog-drainer.md").read_text(); read=lambda tag:json.loads(s.split("<!-- "+tag+":begin -->\n```json\n",1)[1].split("\n```\n<!-- "+tag+":end -->",1)[0]); a=read("backlog-suite-archive"); f=read("backlog-fresh-suite"); n=read("backlog-native-observations"); artifacts={x["name"]:x for x in a["artifacts"]}; assert len(artifacts)==5; assert all(len(x["data"].encode())==x["bytes"] and hashlib.sha256(x["data"].encode()).hexdigest()==x["sha256"] for x in artifacts.values()); assert len(a["source_hashes"])==18; assert all(hashlib.sha256(subprocess.check_output(["git","--no-replace-objects","cat-file","blob","5bf46737048dd81a5b14e5e122095023c2ed41fb:"+p])).hexdigest()==h for p,h in a["source_hashes"].items()); r=f["trusted_capture_record"]; assert r["complete"] and not r["output_limited"] and not r["timed_out"] and r["exit_code"]==0; assert hashlib.sha256((r["stdout"]+r["stderr"]).encode()).hexdigest()==r["capture_sha256"]; assert f["before"]==f["after"] and "5411 passed" in r["stdout"]; assert n["cwd_escape"]["parent_observation"]["canary_absent"]; assert not n["interrupted_original"]["native_recovery_pass"] and not n["hosted_sleep_probe"]["parent_observation"]["actual_interruption_seen"]; print("PASS: 5 exact archived records, 18 historical Git-blob hashes, stored capture streams and stated native limits")'
```


## 추가 native-control 관측 — 2026-11-05

<!-- backlog-native-quiescent-followup:begin -->

[정확한 관측 패킷](2026-11-05-backlog-native-controls.json)은 `1642f176404db13e99ae2ec1b49ca3b90f7c94f9`에서 실행한 다음 제한 검증의 공개 산출물과 명시적인 native 영수증 투영본의 UTF-8 bytes·길이·SHA-256을 담는다. 정확한 private 원본은 별도 보존하고 각 투영본에 원본 digest를 연결한다. 실행 코드·시험·가이드는 바꾸지 않았다. 각 native 자식은 설치된 executor 역할과 `fork_turns=none`으로 생성했고, 중단한 기존 자식은 다시 깨우지 않았다.

| 관측 | 실제 결과 | 확인하지 않은 범위 |
| --- | --- | --- |
| 쓰기 전 barrier | 모든 이전 도구 반환 후 실제 running turn을 interrupt(previous running); 같은 run의 interrupted/turn_aborted 확인; 새 자식 attempt 1이 fixture를 O_EXCL로 한 번 생성 | 중단 당시 pending 도구 없음; 전체 product phase/admission/commit 복구 아님 |
| 쓰기 후 barrier | fixture fsync/close와 모든 도구 exit 0 뒤 running turn interrupt; 새 자식이 같은 한 줄·해시를 확인하고 쓰기 0회로 채택 | 진행 중인 도구를 종료한 시험 아님; 전체 product checkpoint 기반 복구 아님 |
| Claim 불일치 | 실제 launcher의 synthetic nonmatching Claim 조회가 CLAIM_MISMATCH, exit 4; 정상 Claim의 전후 status bytes 동일 | 실제 stale Claim·Registry Claim 변경·supervisor 전환·successor recovery 아님 |
| 기존 pending-tool 사례 | 당시 PAUSED/STOP snapshot 원본과 private immutable evidence hash 일치; evidence는 식별자 제거 투영본; replacement 없음 | 과거 도구 종료 영수증은 계속 미확정; OS 정리는 native PASS가 아님 |

새 pre/post 실험은 현재 승인된 Task root 아래 ignored fixture에서 수행했다. pre fixture는 46 bytes/한 줄, SHA-256 `8518a6894a8c96b770e9e2cbceeb4fb075b3f8848286b6a63836b55e5cdc7102`; post fixture는 45 bytes/한 줄, SHA-256 `1963d24e5bdd73abc799d68477eab539400d5ee9f0001805c1dd0f3f3ddea66b`다. post 원본 자식의 9개 도구 호출/응답과 8개 exec exit 0, 새 자식의 4개 호출/응답과 4개 exec exit 0을 각각 확인했다. pre 원본의 첫 healthy precheck는 intent 키를 잘못 읽어 exit 1이었고 중단 전에 같은 자식에서 수정했다. fresh pre의 일반 launcher 설정 탐색 오류도 성공한 scope 확인 뒤 수정했으며 추가 fault recovery로 세지 않는다. 일부 초기 pre wrapper는 output만 보존해 모든 과거 shell exit code를 독립 복원한다고 주장하지 않는다. 최종 scope 확인은 각 exit 0을 보존했다.

각 단계의 실제 Task/Claim·branch·HEAD·source 청결 상태를 재확인했다. 각 fixture의 실험 intent를 보존했고, 부모는 이전 writer의 terminal을 확인한 뒤 다음 writer를 시작했다. intent는 실험 입력이며 canonical product checkpoint/admission 또는 Task 권한을 대신하지 않는다. 원본 상세 tool records는 private 상태에 남기고 패킷에는 필요한 action·status·exit·hash만 whitelist로 투영하고 원본 digest를 연결했다. 투영본의 digest는 private 원본 digest와 구별한다. 전체 private transcript나 raw native session identity를 게시하지 않는다.

이 관측은 C3의 Codex native-control 증거와 C4의 제한된 불일치 차단 증거를 보강한다. 진행 중인 도구 종료, Claude native recovery, 실제 supervisor/Claim 전환, 전체 product 단계 복구를 통과했다고 올리지 않는다. 이 turn의 Native2 bridge는 issued turn_token이 필요하지만 제공되지 않았으므로 호출하지 않았다. 이것을 모든 runtime의 영구 미지원으로 일반화하지 않는다.

<!-- backlog-native-quiescent-followup:end -->


## Historical fix verification input and complete capture for `84e609b`

This record describes the current fix input and complete test streams. The source hashes cover executable, test, installation and guidance files; they are checked against current bytes. The working manifest also includes the exact validation-document input used by the run. Its final result metadata was added afterward, so that document hash is historical and is not substituted for the final commit manifest.

<!-- backlog-fix-e1-suite:begin -->
```json
{
  "schema": 1,
  "scope": "final current-code/test verification after state-root and file-input fixes; working-source observation, not native-C3 recovery or reusable Tribunal evidence",
  "parent_head": "57b1cd0fad6220aa155e3a3ef138208989b5b7e5",
  "tested_working_change_digest": "e7c7a9544b95d91feea97fe48129d87cd9fb2b347935bc2c3d68799862bb8980",
  "source_manifest": {
    "paths": [
      "docs/validation/2026-11-04-backlog-drainer.md",
      "scripts/backlog_checkpoint/admission.py",
      "scripts/backlog_checkpoint/cli.py",
      "scripts/backlog_checkpoint/storage.py",
      "tests/test_backlog_admission.py",
      "tests/test_backlog_checkpoint.py",
      "tests/test_backlog_cli.py"
    ],
    "count": 7,
    "path_set_digest": "2936d7451b68e70e8bfe14efaf3ef86e6291b84ceceea9474449c3efaa5f1c53",
    "change_digest": "e7c7a9544b95d91feea97fe48129d87cd9fb2b347935bc2c3d68799862bb8980",
    "sources": [
      {
        "change": "modify",
        "mode": "100644",
        "path": "docs/validation/2026-11-04-backlog-drainer.md",
        "sha256": "3bee9c84333b3ff14204add4e3b931256de5bd75e83c2526fdbf112f5dc23114"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "scripts/backlog_checkpoint/admission.py",
        "sha256": "c9aec0c5474d83dbdd67a0481e26becee3ecddc048948fc15264d6001607ae6e"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "scripts/backlog_checkpoint/cli.py",
        "sha256": "b6ce9591980a9705765a23e41a69119de5f8705834e39387c76107be288942e3"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "scripts/backlog_checkpoint/storage.py",
        "sha256": "0d5454493051f38c4e7e45eeda79b1465f1784c3d1ccdfcf5e94634bce4d2da9"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "tests/test_backlog_admission.py",
        "sha256": "75f9befd46a36eb6a83a4a0a6e069287890209e4eef7285c7fa9266261e2b013"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "tests/test_backlog_checkpoint.py",
        "sha256": "5ffac123852ba9c732cdc5c1293007c900fb4570c8759b728a337a901ccb4406"
      },
      {
        "change": "modify",
        "mode": "100644",
        "path": "tests/test_backlog_cli.py",
        "sha256": "dc527b6d476d30e445d7d80b68c58224ce7fcdaaf4b1d1470f04ff075e6d09be"
      }
    ],
    "parent_head": "57b1cd0fad6220aa155e3a3ef138208989b5b7e5"
  },
  "source_hashes": {
    "install.sh": "e498f4565c4fbc89d7b7b6a116b497fed94a73ac4a7fe675ba6a9907b7364ecf",
    "scripts/backlog-checkpoint.py": "95c09efb70b907f1fad1f07a9c74d56211127c91be70c5e27ad76266dd617526",
    "scripts/backlog_checkpoint/__init__.py": "1c62a4c05db819c7d779f2c22acd2af63b56fb4f648ef4ae236af838a307da2a",
    "scripts/backlog_checkpoint/admission.py": "c9aec0c5474d83dbdd67a0481e26becee3ecddc048948fc15264d6001607ae6e",
    "scripts/backlog_checkpoint/cli.py": "b6ce9591980a9705765a23e41a69119de5f8705834e39387c76107be288942e3",
    "scripts/backlog_checkpoint/codec.py": "aef3989e5a3eed08cf2f32a994191a7fc42f62b0eb531b02787cfb68d9c28179",
    "scripts/backlog_checkpoint/git_evidence.py": "9553b5b20c700c39fdefa0ab129495faa46b3ae2899ebb1472b938f0b5730083",
    "scripts/backlog_checkpoint/queue.py": "cd889886dde12c433278c3451b9cd9bc931c7bdaa1dc2acd56bf825479841ffd",
    "scripts/backlog_checkpoint/storage.py": "0d5454493051f38c4e7e45eeda79b1465f1784c3d1ccdfcf5e94634bce4d2da9",
    "scripts/install-backlog-drain.py": "8df080971d07066c585d81c40fe1d40ac644ae96582f3991bd6cd015f62ecc19",
    "skills/backlog-drain/SKILL.md": "dce1c5a6907dd507e342c81229544cc37dab116e9174903d10f060add2a57c85",
    "skills/backlog-drain/references/protocol.md": "7a618109cdaf5e6b15d09735bf4260c019db9489906a683a3eee3c8dfaabfd80",
    "tests/test_backlog_admission.py": "75f9befd46a36eb6a83a4a0a6e069287890209e4eef7285c7fa9266261e2b013",
    "tests/test_backlog_checkpoint.py": "5ffac123852ba9c732cdc5c1293007c900fb4570c8759b728a337a901ccb4406",
    "tests/test_backlog_cli.py": "dc527b6d476d30e445d7d80b68c58224ce7fcdaaf4b1d1470f04ff075e6d09be",
    "tests/test_backlog_git_evidence.py": "346b9a91c7dae0ede35db4abecfea8d6086776ba1cdc56ffbc0f5b70dfbe0f46",
    "tests/test_backlog_install.py": "ce30f2c11ac5dbd0cba6653906f3c3bb108360ad5824035a0b169cd47080458e",
    "tests/test_backlog_queue.py": "4fedd10261709a7321f4113d51933bea850be5bc853a3139afc921b85bf31f8a"
  },
  "suite": {
    "command": "rtk proxy /usr/bin/env -u LD_LIBRARY_PATH PYTHONDONTWRITEBYTECODE=1 \"$HOME/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python\" -m pytest -q -n 8 -p no:cacheprovider",
    "exit_code": 0,
    "timed_out": false,
    "elapsed_seconds": 232.141,
    "summary": "5428 passed in 231.88s (0:03:51)",
    "capture_sha256": "bc26b08fc2be6ea34db9b4ada545161bc019c55b3c8150bfbcb9ca642c541804",
    "complete": true,
    "output_limited": false,
    "stdout_bytes": 6156,
    "stderr_bytes": 0,
    "stdout": "bringing up nodes...\nbringing up nodes...\n\n........................................................................ [  1%]\n........................................................................ [  2%]\n........................................................................ [  3%]\n........................................................................ [  5%]\n........................................................................ [  6%]\n........................................................................ [  7%]\n........................................................................ [  9%]\n........................................................................ [ 10%]\n........................................................................ [ 11%]\n........................................................................ [ 13%]\n........................................................................ [ 14%]\n........................................................................ [ 15%]\n........................................................................ [ 17%]\n........................................................................ [ 18%]\n........................................................................ [ 19%]\n........................................................................ [ 21%]\n........................................................................ [ 22%]\n........................................................................ [ 23%]\n........................................................................ [ 25%]\n........................................................................ [ 26%]\n........................................................................ [ 27%]\n........................................................................ [ 29%]\n........................................................................ [ 30%]\n........................................................................ [ 31%]\n........................................................................ [ 33%]\n........................................................................ [ 34%]\n........................................................................ [ 35%]\n........................................................................ [ 37%]\n........................................................................ [ 38%]\n........................................................................ [ 39%]\n........................................................................ [ 41%]\n........................................................................ [ 42%]\n........................................................................ [ 43%]\n........................................................................ [ 45%]\n........................................................................ [ 46%]\n........................................................................ [ 47%]\n........................................................................ [ 49%]\n........................................................................ [ 50%]\n........................................................................ [ 51%]\n........................................................................ [ 53%]\n........................................................................ [ 54%]\n........................................................................ [ 55%]\n........................................................................ [ 57%]\n........................................................................ [ 58%]\n........................................................................ [ 59%]\n........................................................................ [ 61%]\n........................................................................ [ 62%]\n........................................................................ [ 63%]\n........................................................................ [ 64%]\n........................................................................ [ 66%]\n........................................................................ [ 67%]\n........................................................................ [ 68%]\n........................................................................ [ 70%]\n........................................................................ [ 71%]\n........................................................................ [ 72%]\n........................................................................ [ 74%]\n........................................................................ [ 75%]\n........................................................................ [ 76%]\n........................................................................ [ 78%]\n........................................................................ [ 79%]\n........................................................................ [ 80%]\n........................................................................ [ 82%]\n........................................................................ [ 83%]\n........................................................................ [ 84%]\n........................................................................ [ 86%]\n........................................................................ [ 87%]\n........................................................................ [ 88%]\n........................................................................ [ 90%]\n........................................................................ [ 91%]\n........................................................................ [ 92%]\n........................................................................ [ 94%]\n........................................................................ [ 95%]\n........................................................................ [ 96%]\n........................................................................ [ 98%]\n........................................................................ [ 99%]\n............................                                             [100%]\n5428 passed in 231.88s (0:03:51)\n",
    "stderr": ""
  },
  "original_capture_record_sha256": "cd943121ce52241ae16aa7cbcf9a97ec39fb0ee44da9f0498cba6b8ba0a3aa1f",
  "tested_validation_document": {
    "bytes": 86267,
    "sha256": "3bee9c84333b3ff14204add4e3b931256de5bd75e83c2526fdbf112f5dc23114",
    "scope": "private exact tested input; later metadata-only document delta is excluded from current execution/test/guidance hash equality"
  },
  "independent_fix_review": {
    "scope": "bounded implementation fix slice only",
    "material_findings": 0,
    "focused_tests": 17,
    "source_hashes_unchanged": true,
    "response_sha256": "5f261aa6ca5d8ef33f57e8858b2342cf2ca25186ed5ac0a0eaef0660c90e3be7"
  },
  "native_C3_pass": false,
  "post_suite_change_scope": [
    "docs/validation/2026-11-04-backlog-drainer.md"
  ]
}
```
<!-- backlog-fix-e1-suite:end -->

The bounded reader below hashes current execution inputs and the complete stored streams; it does not execute any archived command or establish native recovery.

```bash
rtk proxy python3 -c 'import hashlib,json; from pathlib import Path; s=Path("docs/validation/2026-11-04-backlog-drainer.md").read_text(); p=json.loads(s.split("<!-- backlog-fix-e1-suite:begin -->\n```json\n",1)[1].split("\n```\n<!-- backlog-fix-e1-suite:end -->",1)[0]); assert len(p["source_hashes"])==18; assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h for n,h in p["source_hashes"].items()); r=p["suite"]; assert r["exit_code"]==0 and r["complete"] and not r["output_limited"] and not r["timed_out"]; assert len(r["stdout"].encode())==r["stdout_bytes"] and len(r["stderr"].encode())==r["stderr_bytes"]; assert hashlib.sha256((r["stdout"]+r["stderr"]).encode()).hexdigest()==r["capture_sha256"]; assert "5428 passed in 231.88s" in r["stdout"]; assert not p["native_C3_pass"]; print("PASS: 18 current source hashes, complete 5428-pass streams and unchanged native boundary")'
```
