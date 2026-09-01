# Strict Tribunal Report and Decision Schema

Reviewer reports are strict JSON objects with exactly `schema`, `reviewer`, `round`, `snapshot`, `status`, `findings`, `executions`, `claims`, and `prior_decisions`. Report size is at most 128 KiB; evidence text is at most 8 KiB; commands and repository-relative paths are at most 4 KiB. Each reviewer has at most 128 findings, 128 executions, 128 claims, and 128 prior decision responses. Decision files contain at most 384 decisions.

IDs are round-local. Decisions are written after a round and enter only through the following round's `begin --decisions`. A prior decision may be acknowledged only by the originating reviewer. Evidence excerpts are sanitized and bounded; raw secret-bearing output, tokens, private keys, credentials, and absolute home paths invalidate evidence.

A finding path may be any normalized repository-relative path, including outside the diff. Finding scope does not authorize source changes: only controller automatic fixes are limited to round-1 `initial_paths`.

## Complete Reviewer A finding report

<!-- valid-reviewer-a -->
```json
{
  "schema": 1,
  "reviewer": "A",
  "round": 1,
  "snapshot": {
    "head_sha": "1111111111111111111111111111111111111111",
    "diff_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "status": "complete",
  "findings": [
    {
      "id": "A-R1-001",
      "reviewer": "A",
      "severity": "HIGH",
      "title": "Unsafe replacement order",
      "rationale": "The target can change after validation and before replacement.",
      "path": "src/example.py",
      "line": 12,
      "execution_ids": [],
      "acceptance_condition": "Revalidate the target immediately before replacement."
    }
  ],
  "executions": [],
  "claims": [],
  "prior_decisions": []
}
```

## Complete Reviewer B execution and claim report

<!-- valid-reviewer-b -->
```json
{
  "schema": 1,
  "reviewer": "B",
  "round": 1,
  "snapshot": {
    "head_sha": "1111111111111111111111111111111111111111",
    "diff_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "status": "complete",
  "findings": [],
  "executions": [
    {
      "id": "B-R1-E001",
      "command": "python3 -m pytest -q tests/example.py",
      "exit_code": 0,
      "stdout_excerpt": "1 passed",
      "stderr_excerpt": "",
      "capture_sha256": "c170a0864a6f10c7f15ff52e35b6a315716aea7e6c95ad3856f9349f05cd28be",
      "truncated": false
    }
  ],
  "claims": [
    {
      "id": "B-R1-C001",
      "statement": "The focused regression passes.",
      "result": "supported",
      "execution_ids": ["B-R1-E001"],
      "reason": ""
    }
  ],
  "prior_decisions": []
}
```

## Complete Reviewer C empty report

<!-- valid-reviewer-c -->
```json
{
  "schema": 1,
  "reviewer": "C",
  "round": 1,
  "snapshot": {
    "head_sha": "1111111111111111111111111111111111111111",
    "diff_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "status": "complete",
  "findings": [],
  "executions": [],
  "claims": [],
  "prior_decisions": []
}
```

## Complete fixed decision file

<!-- valid-fixed-decision -->
```json
[
  {
    "id": "D-R1-A-001",
    "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
    "disposition": "fixed",
    "rationale": "The target is now revalidated immediately before replacement.",
    "executions": [
      {
        "id": "D-R1-E001",
        "command": "python3 -m pytest -q tests/example.py",
        "exit_code": 0,
        "stdout_excerpt": "fixed",
        "stderr_excerpt": "",
        "capture_sha256": "992a93455c71fedd36ac9bbc439952c041cf61445958472af479269b8d873513",
        "truncated": false
      }
    ]
  }
]
```

## Complete rebutted decision file

<!-- valid-rebutted-decision -->
```json
[
  {
    "id": "D-R1-A-002",
    "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
    "disposition": "rebutted",
    "rationale": "Independent execution demonstrates that the target identity is checked before replacement.",
    "executions": [
      {
        "id": "D-R1-E002",
        "command": "python3 -m pytest -q tests/example.py",
        "exit_code": 0,
        "stdout_excerpt": "rebutted",
        "stderr_excerpt": "",
        "capture_sha256": "234425b18615354e404c1cf962b11d0accea1e5b6a17146af8a907b26c202154",
        "truncated": false
      }
    ]
  }
]
```
