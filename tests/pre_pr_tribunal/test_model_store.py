import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pre_pr_tribunal.git_state import capture_snapshot
from pre_pr_tribunal.model import (
    MAX_EVIDENCE_TEXT_BYTES,
    MAX_REPORT_BYTES,
    Reviewer,
    SchemaError,
    parse_decisions,
    parse_reviewer_report,
)
from pre_pr_tribunal.verdict_store import begin_round, finalize_round, read_verdict


def NOW():
    return "2026-09-01T00:00:00Z"


@pytest.fixture
def snapshot(git_repo):
    return capture_snapshot(git_repo, "master", now=NOW)


def execution(identifier="A-R1-E001", *, command="python3 -m pytest -q"):
    stdout = "1 passed"
    return {
        "id": identifier,
        "command": command,
        "exit_code": 0,
        "stdout_excerpt": stdout,
        "stderr_excerpt": "",
        "capture_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "truncated": False,
    }


def finding(identifier="A-R1-001", *, reviewer="A", severity="HIGH", execution_ids=()):
    return {
        "id": identifier,
        "reviewer": reviewer,
        "severity": severity,
        "title": "Incorrect boundary",
        "rationale": "The boundary permits an invalid state.",
        "path": "tracked.txt",
        "line": 1,
        "execution_ids": list(execution_ids),
        "acceptance_condition": "The invalid state is rejected.",
    }


def report(
    snapshot,
    reviewer,
    *,
    round_number=1,
    findings=(),
    executions=(),
    claims=(),
    prior_decisions=(),
):
    return {
        "schema": 1,
        "reviewer": reviewer,
        "round": round_number,
        "snapshot": {
            "head_sha": snapshot.head_sha,
            "diff_sha256": snapshot.diff_sha256,
        },
        "status": "complete",
        "findings": list(findings),
        "executions": list(executions),
        "claims": list(claims),
        "prior_decisions": list(prior_decisions),
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def report_paths(repo, snapshot, *, round_number=1, overrides=None):
    overrides = overrides or {}
    result = {}
    for reviewer in "ABC":
        value = report(snapshot, reviewer, round_number=round_number)
        value.update(overrides.get(reviewer, {}))
        result[reviewer] = write_json(
            repo / f".review/inbox/round-{round_number}/{reviewer}.json", value
        )
    return result


def commit_fix(repo):
    target = repo / "tracked.txt"
    target.write_text(target.read_text(encoding="utf-8") + "fixed\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "commit", "-qm", "fix finding"], check=True
    )


def decision(disposition="fixed", *, identifier="D-R1-A-001"):
    return {
        "id": identifier,
        "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
        "disposition": disposition,
        "rationale": "The failure is now covered by an independent test.",
        "executions": [execution("D-R1-E001")],
    }


def finalized_round_two_pass(repo):
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    finalize_round(
        repo,
        reviewer_paths=report_paths(
            repo, first.snapshot, overrides={"A": {"findings": [finding()]}}
        ),
        now=NOW,
    )
    commit_fix(repo)
    decisions_path = write_json(
        repo / ".review/inbox/round-1/decisions.json", [decision()]
    )
    second = begin_round(
        repo,
        base="master",
        runtime="codex",
        round_number=2,
        decisions_path=decisions_path,
        now=NOW,
    )
    accepted = {
        "decision_id": "D-R1-A-001",
        "outcome": "accepted",
        "replacement_finding_id": None,
    }
    finalize_round(
        repo,
        reviewer_paths=report_paths(
            repo,
            second.snapshot,
            round_number=2,
            overrides={"A": {"prior_decisions": [accepted]}},
        ),
        now=NOW,
    )
    verdict_path = repo / ".review/verdict.json"
    return verdict_path, json.loads(verdict_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"schema":1,"schema":1}', "JSON_DUPLICATE_KEY"),
        (json.dumps({"unexpected": 1}).encode(), "REPORT_SCHEMA_INVALID"),
        (b"\xff", "JSON_INVALID"),
    ],
)
def test_report_rejects_non_strict_json(snapshot, raw, code):
    with pytest.raises(SchemaError, match=code):
        parse_reviewer_report(
            raw, expected_reviewer=Reviewer.A, expected_round=1, snapshot=snapshot
        )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (("reviewer", "B"), "REPORT_REVIEWER_MISMATCH"),
        (("round", True), "REPORT_ROUND_MISMATCH"),
        (("status", "pending"), "REPORT_NOT_TERMINAL"),
        (
            ("snapshot", {"head_sha": "0" * 40, "diff_sha256": "a" * 64}),
            "REPORT_SNAPSHOT_MISMATCH",
        ),
    ],
)
def test_report_requires_exact_identity_and_terminal_status(snapshot, mutation, code):
    value = report(snapshot, "A")
    value[mutation[0]] = mutation[1]
    with pytest.raises(SchemaError, match=code):
        parse_reviewer_report(
            json.dumps(value).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"severity": "BLOCKER"}, "FINDING_SCHEMA_INVALID"),
        ({"path": "/tmp/file"}, "PATH_INVALID"),
        ({"path": "../tracked.txt"}, "PATH_INVALID"),
        ({"line": 0}, "FINDING_SCHEMA_INVALID"),
        ({"title": "bad\x00title"}, "TEXT_INVALID"),
        ({"id": "A-R4-001"}, "FINDING_ID_INVALID"),
        ({"reviewer": "B"}, "FINDING_REVIEWER_MISMATCH"),
    ],
)
def test_finding_validation_fails_closed(snapshot, change, code):
    item = finding()
    item.update(change)
    value = report(snapshot, "A", findings=[item])
    with pytest.raises(SchemaError, match=code):
        parse_reviewer_report(
            json.dumps(value).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


def test_duplicate_finding_and_unknown_execution_references_are_rejected(snapshot):
    duplicate = report(snapshot, "A", findings=[finding(), finding()])
    with pytest.raises(SchemaError, match="FINDING_ID_DUPLICATE"):
        parse_reviewer_report(
            json.dumps(duplicate).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )
    dangling = report(snapshot, "A", findings=[finding(execution_ids=["A-R1-E001"])])
    with pytest.raises(SchemaError, match="EXECUTION_REFERENCE_INVALID"):
        parse_reviewer_report(
            json.dumps(dangling).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"exit_code": True}, "EXECUTION_SCHEMA_INVALID"),
        ({"capture_sha256": "bad"}, "EXECUTION_SCHEMA_INVALID"),
        ({"truncated": 0}, "EXECUTION_SCHEMA_INVALID"),
        ({"command": ""}, "TEXT_INVALID"),
        ({"stdout_excerpt": "ghp_abcdefgh"}, "EVIDENCE_SECRET_DETECTED"),
        ({"stderr_excerpt": "-----BEGIN PRIVATE KEY-----"}, "EVIDENCE_SECRET_DETECTED"),
    ],
)
def test_execution_requires_exact_typed_sanitized_evidence(snapshot, change, code):
    item = execution()
    item.update(change)
    value = report(snapshot, "A", executions=[item])
    with pytest.raises(SchemaError, match=code):
        parse_reviewer_report(
            json.dumps(value).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", "tool --cwd=/home/alice/private"),
        ("command", "tool --cwd:(/Users/alice/private)"),
        ("command", 'tool "${X:-/home/alice/private}"'),
        ("command", "tool -C/home/alice/private"),
        ("stdout_excerpt", "cwd=/home/alice/private"),
        ("stderr_excerpt", "failed:(/Users/alice/private)"),
    ],
)
def test_assignment_and_punctuation_embedded_home_paths_are_rejected(
    snapshot, field, value
):
    item = execution()
    item[field] = value
    with pytest.raises(SchemaError, match="EVIDENCE_SECRET_DETECTED"):
        parse_reviewer_report(
            json.dumps(report(snapshot, "A", executions=[item])).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/home/alice/private",
        "https://example.com/Users/alice/private",
    ],
)
def test_url_paths_that_resemble_home_directories_are_not_false_positives(
    snapshot, value
):
    item = execution(command=f"curl {value}")
    item["stdout_excerpt"] = value
    parsed = parse_reviewer_report(
        json.dumps(report(snapshot, "A", executions=[item])).encode(),
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=snapshot,
    )
    assert parsed.executions[0].command == f"curl {value}"


@pytest.mark.parametrize(
    "value",
    [
        "tar -xC/home/alice/private archive.tar",
        "curl -so/home/alice/private https://example.com",
        "cat file:///home/alice/private",
        "curl 'https://example.com/search?next=/home/alice/private'",
        "curl 'https://example.com/page#file=/Users/alice/private'",
    ],
)
def test_home_paths_outside_http_url_path_components_are_rejected(snapshot, value):
    item = execution(command=value)
    with pytest.raises(SchemaError, match="EVIDENCE_SECRET_DETECTED"):
        parse_reviewer_report(
            json.dumps(report(snapshot, "A", executions=[item])).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


def test_valid_http_url_hostname_ending_in_option_shape_is_allowed(snapshot):
    value = "https://example-C/home/alice/private"
    item = execution(command=f"curl {value}")
    item["stdout_excerpt"] = value
    parsed = parse_reviewer_report(
        json.dumps(report(snapshot, "A", executions=[item])).encode(),
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=snapshot,
    )
    assert parsed.executions[0].stdout_excerpt == value


def test_valid_http_url_path_in_shell_assignment_is_allowed(snapshot):
    value = "https://example-C/home/alice/private"
    command = f"URL={value} curl \"$URL\""
    item = execution(command=command)
    parsed = parse_reviewer_report(
        json.dumps(report(snapshot, "A", executions=[item])).encode(),
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=snapshot,
    )
    assert parsed.executions[0].command == command


def test_report_and_evidence_byte_limits_are_enforced(snapshot):
    with pytest.raises(SchemaError, match="REPORT_TOO_LARGE"):
        parse_reviewer_report(
            b" " * (MAX_REPORT_BYTES + 1),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )
    item = execution()
    item["stdout_excerpt"] = "x" * (MAX_EVIDENCE_TEXT_BYTES + 1)
    value = report(snapshot, "A", executions=[item])
    with pytest.raises(SchemaError, match="TEXT_TOO_LARGE"):
        parse_reviewer_report(
            json.dumps(value).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


def test_rebuttal_without_execution_is_rejected():
    value = decision("rebutted")
    value["executions"] = []
    with pytest.raises(SchemaError, match="DECISION_EVIDENCE_REQUIRED"):
        parse_decisions(json.dumps([value]).encode(), prior_blockers=("A-R1-001",))


def test_decisions_reject_unknown_duplicate_and_incomplete_blocker_coverage():
    value = decision()
    value["extra"] = 1
    with pytest.raises(SchemaError, match="DECISION_SCHEMA_INVALID"):
        parse_decisions(json.dumps([value]).encode(), prior_blockers=("A-R1-001",))
    with pytest.raises(SchemaError, match="DECISION_ID_DUPLICATE"):
        parse_decisions(
            json.dumps([decision(), decision()]).encode(), prior_blockers=("A-R1-001",)
        )
    with pytest.raises(SchemaError, match="DECISION_COVERAGE_INVALID"):
        parse_decisions(b"[]", prior_blockers=("A-R1-001",))


def test_independently_numbered_decision_binds_through_finding_ref():
    independent = decision(identifier="D-R1-A-002")
    parsed = parse_decisions(
        json.dumps([independent]).encode(), prior_blockers=("A-R1-001",)
    )
    assert parsed[0].id == "D-R1-A-002"
    assert parsed[0].finding_id == "A-R1-001"


def test_reviewer_b_claims_and_behavioral_findings_require_execution(snapshot):
    claim = {
        "id": "B-R1-C001",
        "statement": "tests pass",
        "result": "supported",
        "execution_ids": [],
        "reason": "",
    }
    value = report(snapshot, "B", claims=[claim])
    with pytest.raises(SchemaError, match="CLAIM_EVIDENCE_REQUIRED"):
        parse_reviewer_report(
            json.dumps(value).encode(),
            expected_reviewer=Reviewer.B,
            expected_round=1,
            snapshot=snapshot,
        )
    value = report(snapshot, "B", findings=[finding("B-R1-001", reviewer="B")])
    with pytest.raises(SchemaError, match="BEHAVIOR_EVIDENCE_REQUIRED"):
        parse_reviewer_report(
            json.dumps(value).encode(),
            expected_reviewer=Reviewer.B,
            expected_round=1,
            snapshot=snapshot,
        )
    claim.update(result="unverified", reason="Unsafe to execute locally")
    parsed = parse_reviewer_report(
        json.dumps(report(snapshot, "B", claims=[claim])).encode(),
        expected_reviewer=Reviewer.B,
        expected_round=1,
        snapshot=snapshot,
    )
    assert parsed.claims[0].result == "unverified"


def test_non_scalar_schema_enums_fail_as_bounded_schema_errors(snapshot):
    claim = {
        "id": "B-R1-C001",
        "statement": "tests pass",
        "result": [],
        "execution_ids": [],
        "reason": "none",
    }
    value = report(snapshot, "B", claims=[claim])
    with pytest.raises(SchemaError, match="CLAIM_SCHEMA_INVALID"):
        parse_reviewer_report(
            json.dumps(value).encode(),
            expected_reviewer=Reviewer.B,
            expected_round=1,
            snapshot=snapshot,
        )
    malformed = decision()
    malformed["disposition"] = []
    with pytest.raises(SchemaError, match="DECISION_SCHEMA_INVALID"):
        parse_decisions(json.dumps([malformed]).encode(), prior_blockers=("A-R1-001",))


def test_begin_writes_in_progress_and_finalize_requires_all_reviewers(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert pending.gate.status.value == "in_progress"
    stored = read_verdict(git_repo)
    assert stored.round == 1 and all(
        item.status == "pending" for item in stored.reviewers.values()
    )
    with pytest.raises(SchemaError, match="REVIEWER_REPORT_MISSING"):
        finalize_round(
            git_repo,
            reviewer_paths={"A": git_repo / ".review/inbox/round-1/A.json"},
            now=NOW,
        )


def test_empty_reports_pass_and_round_one_restart_resets_pending(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="claude", round_number=1, now=NOW
    )
    pending_inode = (git_repo / ".review/verdict.json").stat().st_ino
    final = finalize_round(
        git_repo, reviewer_paths=report_paths(git_repo, pending.snapshot), now=NOW
    )
    assert final.gate.status.value == "pass"
    assert (git_repo / ".review/verdict.json").stat().st_ino != pending_inode
    assert not tuple((git_repo / ".review").glob(".verdict.tmp.*"))
    restarted = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert restarted.gate.status.value == "in_progress"
    assert all(
        slot.status == "pending" for slot in read_verdict(git_repo).reviewers.values()
    )


def test_round_restart_invalidates_stale_reports_before_fresh_reports_pass(git_repo):
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    stale_paths = report_paths(git_repo, first.snapshot)
    assert (
        finalize_round(git_repo, reviewer_paths=stale_paths, now=NOW).gate.status.value
        == "pass"
    )
    stale_decisions = write_json(git_repo / ".review/inbox/round-1/decisions.json", [])
    restarted = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert not stale_decisions.exists()
    with pytest.raises(SchemaError, match="REVIEWER_REPORT_MISSING"):
        finalize_round(git_repo, reviewer_paths=stale_paths, now=NOW)
    fresh_paths = report_paths(git_repo, restarted.snapshot)
    assert (
        finalize_round(git_repo, reviewer_paths=fresh_paths, now=NOW).gate.status.value
        == "pass"
    )


def test_finalize_rejects_report_path_alias_snapshot_drift_and_nonprivate_file(
    git_repo,
):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    paths = report_paths(git_repo, pending.snapshot)
    alias = git_repo / ".review/inbox/round-1/alias.json"
    alias.write_bytes(paths["A"].read_bytes())
    alias.chmod(0o600)
    with pytest.raises(SchemaError, match="REPORT_PATH_INVALID"):
        finalize_round(git_repo, reviewer_paths={**paths, "A": alias}, now=NOW)
    paths["A"].chmod(0o644)
    with pytest.raises(SchemaError, match="FILE_UNSAFE"):
        finalize_round(git_repo, reviewer_paths=paths, now=NOW)
    paths["A"].chmod(0o600)
    commit_fix(git_repo)
    with pytest.raises(SchemaError, match="SNAPSHOT_CHANGED"):
        finalize_round(git_repo, reviewer_paths=paths, now=NOW)


def test_store_rejects_symlinked_review_directory_and_unignored_state(git_repo):
    other = git_repo.parent / "other"
    other.mkdir()
    (git_repo / ".review").symlink_to(other, target_is_directory=True)
    with pytest.raises(SchemaError, match="REVIEW_DIRECTORY_UNSAFE"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    (git_repo / ".review").unlink()
    (git_repo / ".gitignore").write_text("other/\n", encoding="utf-8")
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "add", ".gitignore"], check=True
    )
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "commit", "-qm", "ignore change"],
        check=True,
    )
    with pytest.raises(SchemaError, match="VERDICT_NOT_IGNORED"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)


def test_store_lock_is_nonblocking_and_atomic_file_is_private(git_repo):
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    verdict = git_repo / ".review/verdict.json"
    assert first.round == 1 and (verdict.stat().st_mode & 0o777) == 0o600
    lock = os.open(git_repo / ".review/lock", os.O_RDWR)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(SchemaError, match="STORE_LOCKED"):
            read_verdict(git_repo)
    finally:
        os.close(lock)


def test_oversized_combined_verdict_does_not_replace_pending_state(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    overrides = {}
    for reviewer in "ABC":
        evidence = [execution("B-R1-E001")] if reviewer == "B" else []
        findings = []
        for index in range(14):
            item = finding(
                f"{reviewer}-R1-{index:03d}",
                reviewer=reviewer,
                severity="LOW",
                execution_ids=("B-R1-E001",) if reviewer == "B" else (),
            )
            item["rationale"] = "x" * 7000
            findings.append(item)
        overrides[reviewer] = {"findings": findings, "executions": evidence}
    with pytest.raises(SchemaError, match="VERDICT_TOO_LARGE"):
        finalize_round(
            git_repo,
            reviewer_paths=report_paths(
                git_repo, pending.snapshot, overrides=overrides
            ),
            now=NOW,
        )
    assert read_verdict(git_repo).gate.status.value == "in_progress"
    assert not tuple((git_repo / ".review").glob(".verdict.tmp.*"))


def test_restart_rejects_existing_verdict_symlink_and_malformed_timestamp(git_repo):
    begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    verdict_path = git_repo / ".review/verdict.json"
    payload = json.loads(verdict_path.read_text(encoding="utf-8"))
    payload["created_at"] = "yesterday"
    write_json(verdict_path, payload)
    with pytest.raises(SchemaError, match="VERDICT_INVALID"):
        read_verdict(git_repo)
    verdict_path.unlink()
    outside = write_json(git_repo.parent / "outside.json", payload)
    verdict_path.symlink_to(outside)
    with pytest.raises(SchemaError, match="VERDICT_FILE_UNSAFE"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)


def test_persisted_complete_round_requires_originating_acceptance(git_repo):
    verdict_path, payload = finalized_round_two_pass(git_repo)
    payload["reviewers"]["A"]["prior_decisions"] = []
    write_json(verdict_path, payload)
    with pytest.raises(SchemaError, match="PRIOR_DECISION_RESPONSE_MISSING"):
        read_verdict(git_repo)


def test_persisted_fixed_decision_requires_head_change_from_prior_summary(git_repo):
    verdict_path, payload = finalized_round_two_pass(git_repo)
    payload["history"][0]["head_sha"] = payload["head_sha"]
    write_json(verdict_path, payload)
    with pytest.raises(SchemaError, match="FIXED_HEAD_UNCHANGED"):
        read_verdict(git_repo)


def test_independent_decision_numbers_survive_rounds_and_persisted_history(git_repo):
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    finalize_round(
        git_repo,
        reviewer_paths=report_paths(
            git_repo, first.snapshot, overrides={"A": {"findings": [finding()]}}
        ),
        now=NOW,
    )
    commit_fix(git_repo)
    first_decision = write_json(
        git_repo / ".review/inbox/round-1/decisions.json",
        [decision(identifier="D-R1-A-002")],
    )
    second = begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=2,
        decisions_path=first_decision,
        now=NOW,
    )
    first_response = {
        "decision_id": "D-R1-A-002",
        "outcome": "reissued",
        "replacement_finding_id": "A-R2-001",
    }
    finalize_round(
        git_repo,
        reviewer_paths=report_paths(
            git_repo,
            second.snapshot,
            round_number=2,
            overrides={
                "A": {
                    "findings": [finding("A-R2-001")],
                    "prior_decisions": [first_response],
                }
            },
        ),
        now=NOW,
    )
    assert read_verdict(git_repo).decisions[0].id == "D-R1-A-002"
    commit_fix(git_repo)
    second_decision = {
        "id": "D-R2-A-009",
        "finding_ref": {"round": 2, "id": "A-R2-001", "reviewer": "A"},
        "disposition": "fixed",
        "rationale": "Covered by a new regression test.",
        "executions": [execution("D-R2-E001")],
    }
    second_decisions_path = write_json(
        git_repo / ".review/inbox/round-2/decisions.json", [second_decision]
    )
    third = begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=3,
        decisions_path=second_decisions_path,
        now=NOW,
    )
    pending = read_verdict(git_repo)
    assert pending.decisions[0].id == "D-R2-A-009"
    assert pending.history[1].decision_outcomes[0]["decision_id"] == "D-R1-A-002"
    second_response = {
        "decision_id": "D-R2-A-009",
        "outcome": "accepted",
        "replacement_finding_id": None,
    }
    finalize_round(
        git_repo,
        reviewer_paths=report_paths(
            git_repo,
            third.snapshot,
            round_number=3,
            overrides={"A": {"prior_decisions": [second_response]}},
        ),
        now=NOW,
    )
    stored = read_verdict(git_repo)
    assert stored.gate.status.value == "pass"
    assert stored.decisions[0].finding_id == "A-R2-001"


@pytest.mark.parametrize(
    "mutation",
    ("wrong_round", "wrong_reviewer", "non_scalar_reviewer", "duplicate", "outcome"),
)
def test_persisted_history_cross_references_fail_closed(git_repo, mutation):
    verdict_path, payload = finalized_round_two_pass(git_repo)
    blocker = payload["history"][0]["blocking_findings"][0]
    if mutation == "wrong_round":
        blocker["id"] = "A-R2-001"
    elif mutation == "wrong_reviewer":
        blocker["reviewer"] = "B"
    elif mutation == "non_scalar_reviewer":
        blocker["reviewer"] = []
    elif mutation == "duplicate":
        payload["history"][0]["blocking_findings"].append(dict(blocker))
    else:
        payload["history"][0]["decision_outcomes"] = [
            {
                "decision_id": "D-R1-A-001",
                "outcome": "accepted",
                "replacement_finding_id": None,
            }
        ]
    write_json(verdict_path, payload)
    with pytest.raises(SchemaError):
        read_verdict(git_repo)


def test_two_round_originating_reviewer_closure_and_round_limit(git_repo):
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    paths = report_paths(
        git_repo, first.snapshot, overrides={"A": {"findings": [finding()]}}
    )
    failed = finalize_round(git_repo, reviewer_paths=paths, now=NOW)
    assert failed.gate.status.value == "fail" and failed.gate.blocking_count == 1
    commit_fix(git_repo)
    decisions_path = write_json(
        git_repo / ".review/inbox/round-1/decisions.json", [decision()]
    )
    second = begin_round(
        git_repo,
        base="master",
        runtime="claude",
        round_number=2,
        decisions_path=decisions_path,
        now=NOW,
    )
    accepted = {
        "decision_id": "D-R1-A-001",
        "outcome": "accepted",
        "replacement_finding_id": None,
    }
    second_paths = report_paths(
        git_repo,
        second.snapshot,
        round_number=2,
        overrides={"A": {"prior_decisions": [accepted]}},
    )
    passed = finalize_round(git_repo, reviewer_paths=second_paths, now=NOW)
    assert passed.gate.status.value == "pass" and len(passed.history) == 1
    third = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    paths = report_paths(
        git_repo, third.snapshot, overrides={"A": {"findings": [finding()]}}
    )
    finalize_round(git_repo, reviewer_paths=paths, now=NOW)
    commit_fix(git_repo)
    d1 = write_json(git_repo / ".review/inbox/round-1/decisions.json", [decision()])
    r2 = begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=2,
        decisions_path=d1,
        now=NOW,
    )
    replacement = finding("A-R2-001")
    reissued = {
        "decision_id": "D-R1-A-001",
        "outcome": "reissued",
        "replacement_finding_id": "A-R2-001",
    }
    r2paths = report_paths(
        git_repo,
        r2.snapshot,
        round_number=2,
        overrides={"A": {"prior_decisions": [reissued], "findings": [replacement]}},
    )
    finalize_round(git_repo, reviewer_paths=r2paths, now=NOW)
    commit_fix(git_repo)
    d2value = {
        "id": "D-R2-A-001",
        "finding_ref": {"round": 2, "id": "A-R2-001", "reviewer": "A"},
        "disposition": "fixed",
        "rationale": "Covered by regression test.",
        "executions": [execution("D-R2-E001")],
    }
    d2 = write_json(git_repo / ".review/inbox/round-2/decisions.json", [d2value])
    r3 = begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=3,
        decisions_path=d2,
        now=NOW,
    )
    r3finding = finding("A-R3-001")
    response = {
        "decision_id": "D-R2-A-001",
        "outcome": "reissued",
        "replacement_finding_id": "A-R3-001",
    }
    r3paths = report_paths(
        git_repo,
        r3.snapshot,
        round_number=3,
        overrides={"A": {"prior_decisions": [response], "findings": [r3finding]}},
    )
    terminal = finalize_round(git_repo, reviewer_paths=r3paths, now=NOW)
    assert terminal.gate.status.value == "fail"
    with pytest.raises(SchemaError, match="ROUND_LIMIT_EXHAUSTED"):
        begin_round(git_repo, base="master", runtime="codex", round_number=4, now=NOW)


@pytest.mark.parametrize(
    ("owner_payload", "code"),
    [
        ([], "PRIOR_DECISION_RESPONSE_MISSING"),
        (
            [
                {
                    "decision_id": "D-R1-A-001",
                    "outcome": "reissued",
                    "replacement_finding_id": None,
                }
            ],
            "REPLACEMENT_FINDING_REQUIRED",
        ),
        (
            [
                {
                    "decision_id": "D-R1-A-001",
                    "outcome": "accepted",
                    "replacement_finding_id": "A-R2-001",
                }
            ],
            "PRIOR_DECISION_RESPONSE_INVALID",
        ),
    ],
)
def test_only_originating_reviewer_can_close_decision(git_repo, owner_payload, code):
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    finalize_round(
        git_repo,
        reviewer_paths=report_paths(
            git_repo, first.snapshot, overrides={"A": {"findings": [finding()]}}
        ),
        now=NOW,
    )
    commit_fix(git_repo)
    dpath = write_json(git_repo / ".review/inbox/round-1/decisions.json", [decision()])
    second = begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=2,
        decisions_path=dpath,
        now=NOW,
    )
    paths = report_paths(
        git_repo,
        second.snapshot,
        round_number=2,
        overrides={"A": {"prior_decisions": owner_payload}},
    )
    with pytest.raises(SchemaError, match=code):
        finalize_round(git_repo, reviewer_paths=paths, now=NOW)


def test_fixed_decision_requires_changed_head_and_paths_stay_within_round_one(git_repo):
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    finalize_round(
        git_repo,
        reviewer_paths=report_paths(
            git_repo, first.snapshot, overrides={"A": {"findings": [finding()]}}
        ),
        now=NOW,
    )
    dpath = write_json(git_repo / ".review/inbox/round-1/decisions.json", [decision()])
    with pytest.raises(SchemaError, match="FIXED_HEAD_UNCHANGED"):
        begin_round(
            git_repo,
            base="master",
            runtime="codex",
            round_number=2,
            decisions_path=dpath,
            now=NOW,
        )
    (git_repo / "new.txt").write_text("new\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "-C", str(git_repo), "add", "new.txt"], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "commit", "-qm", "out of scope"],
        check=True,
    )
    with pytest.raises(SchemaError, match="AUTO_FIX_SCOPE_VIOLATION"):
        begin_round(
            git_repo,
            base="master",
            runtime="codex",
            round_number=2,
            decisions_path=dpath,
            now=NOW,
        )


def test_failed_round_cannot_be_reset_through_round_one(git_repo):
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    finalize_round(
        git_repo,
        reviewer_paths=report_paths(
            git_repo, first.snapshot, overrides={"A": {"findings": [finding()]}}
        ),
        now=NOW,
    )
    with pytest.raises(SchemaError, match="ROUND_TRANSITION_INVALID"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)


def test_round_three_failure_rejects_round_one_with_exhaustion(git_repo):
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    finalize_round(
        git_repo,
        reviewer_paths=report_paths(
            git_repo, first.snapshot, overrides={"A": {"findings": [finding()]}}
        ),
        now=NOW,
    )
    commit_fix(git_repo)
    r1_decisions = write_json(
        git_repo / ".review/inbox/round-1/decisions.json", [decision()]
    )
    second = begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=2,
        decisions_path=r1_decisions,
        now=NOW,
    )
    reissued = {
        "decision_id": "D-R1-A-001",
        "outcome": "reissued",
        "replacement_finding_id": "A-R2-001",
    }
    finalize_round(
        git_repo,
        reviewer_paths=report_paths(
            git_repo,
            second.snapshot,
            round_number=2,
            overrides={
                "A": {
                    "findings": [finding("A-R2-001")],
                    "prior_decisions": [reissued],
                }
            },
        ),
        now=NOW,
    )
    with pytest.raises(SchemaError, match="ROUND_TRANSITION_INVALID"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    commit_fix(git_repo)
    r2_decision = {
        "id": "D-R2-A-001",
        "finding_ref": {"round": 2, "id": "A-R2-001", "reviewer": "A"},
        "disposition": "fixed",
        "rationale": "Covered by a new regression test.",
        "executions": [execution("D-R2-E001")],
    }
    r2_decisions = write_json(
        git_repo / ".review/inbox/round-2/decisions.json", [r2_decision]
    )
    third = begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=3,
        decisions_path=r2_decisions,
        now=NOW,
    )
    r3_response = {
        "decision_id": "D-R2-A-001",
        "outcome": "reissued",
        "replacement_finding_id": "A-R3-001",
    }
    finalize_round(
        git_repo,
        reviewer_paths=report_paths(
            git_repo,
            third.snapshot,
            round_number=3,
            overrides={
                "A": {
                    "findings": [finding("A-R3-001")],
                    "prior_decisions": [r3_response],
                }
            },
        ),
        now=NOW,
    )
    with pytest.raises(SchemaError, match="ROUND_LIMIT_EXHAUSTED"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)


def test_cli_json_only_success_and_bounded_domain_error(git_repo):
    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    begun = subprocess.run(
        [
            sys.executable,
            str(cli),
            "begin",
            "--base",
            "master",
            "--runtime",
            "codex",
            "--round",
            "1",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
    )
    assert begun.returncode == 0 and begun.stderr == ""
    payload = json.loads(begun.stdout)
    assert set(payload) == {"schema", "round", "snapshot", "initial_paths", "gate"}
    context = subprocess.run(
        [sys.executable, str(cli), "context", "--reviewer", "A"],
        cwd=git_repo,
        text=True,
        capture_output=True,
    )
    context_payload = json.loads(context.stdout)
    assert (
        context.returncode == 0
        and "B" not in json.dumps(context_payload)
        and "stdout_excerpt" not in json.dumps(context_payload)
    )
    failed = subprocess.run(
        [
            sys.executable,
            str(cli),
            "finalize",
            "--reviewer-a",
            ".review/inbox/round-1/A.json",
            "--reviewer-b",
            ".review/inbox/round-1/B.json",
            "--reviewer-c",
            ".review/inbox/round-1/C.json",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
    )
    assert failed.returncode == 1 and failed.stdout == ""
    assert (
        failed.stderr == "PRE_PR_TRIBUNAL:REVIEWER_REPORT_MISSING\n"
        and len(failed.stderr) < 128
    )


def test_cli_finalize_and_status_emit_only_bounded_projections(git_repo):
    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    begun = subprocess.run(
        [
            sys.executable,
            str(cli),
            "begin",
            "--base",
            "master",
            "--runtime",
            "claude",
            "--round",
            "1",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    )
    assert begun.stderr == ""
    pending = read_verdict(git_repo)
    report_paths(git_repo, pending.snapshot)
    finalized = subprocess.run(
        [
            sys.executable,
            str(cli),
            "finalize",
            "--reviewer-a",
            ".review/inbox/round-1/A.json",
            "--reviewer-b",
            ".review/inbox/round-1/B.json",
            "--reviewer-c",
            ".review/inbox/round-1/C.json",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    )
    expected = {
        "round": 1,
        "gate_status": "pass",
        "blocking_count": 0,
        "verdict_path": ".review/verdict.json",
    }
    assert json.loads(finalized.stdout) == expected and finalized.stderr == ""
    status = subprocess.run(
        [sys.executable, str(cli), "status"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(status.stdout) == expected and status.stderr == ""
    assert "findings" not in status.stdout and "executions" not in status.stdout


def test_cli_usage_is_exit_two_without_traceback(git_repo):
    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    result = subprocess.run(
        [sys.executable, str(cli), "begin", "--round", "9"],
        cwd=git_repo,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2 and result.stdout == ""
    assert "Traceback" not in result.stderr and len(result.stderr.encode()) < 512


@pytest.mark.parametrize(
    "arguments", (["--help"], ["-h"], ["begin", "--help"], ["begin", "-h"])
)
def test_cli_help_is_stable_usage_without_stdout(git_repo, arguments):
    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    result = subprocess.run(
        [sys.executable, str(cli), *arguments],
        cwd=git_repo,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "PRE_PR_TRIBUNAL:USAGE\n"
