import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
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
    validate_report_bytes,
)
from pre_pr_tribunal.verdict_store import (
    begin_round,
    finalize_round,
    read_verdict,
    store_reviewer_report,
    validate_stored_reviewer_report,
)


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


def test_validate_report_bytes_returns_parser_result_and_raw_digest(snapshot):
    raw = json.dumps(report(snapshot, "A"), separators=(",", ":")).encode() + b"\n"

    parsed, digest = validate_report_bytes(
        raw,
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=snapshot,
    )

    assert parsed == parse_reviewer_report(
        raw, expected_reviewer=Reviewer.A, expected_round=1, snapshot=snapshot
    )
    assert digest == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("malformed", "JSON_INVALID"),
        ("text", "TEXT_INVALID"),
        ("reviewer", "REPORT_REVIEWER_MISMATCH"),
        ("round", "REPORT_ROUND_MISMATCH"),
        ("snapshot", "REPORT_SNAPSHOT_MISMATCH"),
    ],
)
def test_validate_report_bytes_raises_the_same_parser_code(snapshot, mutation, code):
    reviewer = Reviewer.A
    round_number = 1
    if mutation == "malformed":
        raw = b'{"schema":1'
    else:
        value = report(snapshot, "A")
        if mutation == "text":
            invalid = finding()
            invalid["title"] = "bad\x00title"
            value["findings"] = [invalid]
        elif mutation == "reviewer":
            value["reviewer"] = "B"
        elif mutation == "round":
            value["round"] = 2
        else:
            value["snapshot"]["head_sha"] = "0" * 40
        raw = json.dumps(value).encode()
    with pytest.raises(SchemaError, match=f"^{code}$"):
        parse_reviewer_report(
            raw,
            expected_reviewer=reviewer,
            expected_round=round_number,
            snapshot=snapshot,
        )
    with pytest.raises(SchemaError, match=f"^{code}$"):
        validate_report_bytes(
            raw,
            expected_reviewer=reviewer,
            expected_round=round_number,
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


@pytest.mark.parametrize("field", ("stdout_excerpt", "stderr_excerpt"))
@pytest.mark.parametrize("value", ("first\nsecond", "name\tvalue", "first\n\tsecond"))
def test_execution_excerpts_accept_only_lf_and_tab(snapshot, field, value):
    item = execution()
    item[field] = value
    parsed = parse_reviewer_report(
        json.dumps(report(snapshot, "A", executions=[item])).encode(),
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=snapshot,
    )
    assert getattr(parsed.executions[0], field) == value


@pytest.mark.parametrize("control", ("\r", "\x00", "\x1b", "\x7f"))
def test_execution_excerpts_reject_every_other_cc(snapshot, control):
    item = execution()
    item["stdout_excerpt"] = "left" + control + "right"
    with pytest.raises(SchemaError, match="^TEXT_INVALID$"):
        parse_reviewer_report(
            json.dumps(report(snapshot, "A", executions=[item])).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


@pytest.mark.parametrize("control", ("\n", "\t"))
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", "python3{control}-V"),
        ("title", "title{control}text"),
        ("rationale", "rationale{control}text"),
        ("acceptance_condition", "acceptance{control}condition"),
        ("statement", "statement{control}text"),
        ("reason", "reason{control}text"),
        ("path", "directory{control}/tracked.txt"),
    ],
)
def test_non_excerpt_text_keeps_rejecting_lf_and_tab(snapshot, control, field, value):
    value = value.format(control=control)
    item = execution(command=value) if field == "command" else execution()
    report_value = report(snapshot, "A", executions=[item])
    if field in {"title", "rationale", "acceptance_condition", "path"}:
        finding_value = finding()
        finding_value[field] = value
        report_value["findings"] = [finding_value]
    elif field in {"statement", "reason"}:
        report_value["reviewer"] = "B"
        item["id"] = "B-R1-E001"
        report_value["claims"] = [
            {
                "id": "B-R1-C001",
                "statement": "Claim statement",
                "result": "supported",
                "execution_ids": ["B-R1-E001"],
                "reason": "",
            }
        ]
        report_value["claims"][0][field] = value
    with pytest.raises(SchemaError, match="^(TEXT_INVALID|PATH_INVALID)$"):
        parse_reviewer_report(
            json.dumps(report_value).encode(),
            expected_reviewer=Reviewer.B if field in {"statement", "reason"} else Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


@pytest.mark.parametrize(
    "value",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "Authorization: Bearer examplecredential123456789",
        "xoxb-exampletoken123456789",
        "xoxc-exampletoken123456789",
        "xoxe-1-exampletoken123456789",
        "xapp-1-exampletoken123456789",
    ],
)
@pytest.mark.parametrize("field", ["command", "stdout_excerpt", "stderr_excerpt"])
def test_common_credential_formats_are_rejected(snapshot, field, value):
    item = execution()
    item[field] = value

    with pytest.raises(SchemaError, match="EVIDENCE_SECRET_DETECTED"):
        parse_reviewer_report(
            json.dumps(report(snapshot, "A", executions=[item])).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


@pytest.mark.parametrize("value", ["xoxc-short", "xoxe-label", "xapp-doc"])
def test_short_slack_like_labels_are_not_credentials(snapshot, value):
    item = execution()
    item["stdout_excerpt"] = value

    parsed = parse_reviewer_report(
        json.dumps(report(snapshot, "A", executions=[item])).encode(),
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=snapshot,
    )

    assert parsed.executions[0].stdout_excerpt == value


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


@pytest.mark.parametrize(
    "command",
    [
        "curl https://example.com;/home/alice/private",
        "curl https://example.com|/home/alice/private",
        "curl https://example.com&/home/alice/private",
        "curl https://example.com(/home/alice/private)",
        "curl https://example.com)/home/alice/private",
        "curl https://example.com>/home/alice/private",
        "curl https://example.com</home/alice/private",
        "curl https://example.com 2>/home/alice/private",
        "curl https://example.com`id`/home/alice/private",
        'curl "https://example.com$(id)/home/alice/private"',
        'curl "https://example.com/${HOST}/home/alice/private"',
        r"curl https://example.com/a\;/home/alice/private",
        "echo $(curl https://example.com/home/alice/private)",
        "echo `curl https://example.com/home/alice/private`",
    ],
)
def test_shell_control_or_interpolation_ends_http_url_path_exemption(
    snapshot, command
):
    item = execution(command=command)
    with pytest.raises(SchemaError, match="EVIDENCE_SECRET_DETECTED"):
        parse_reviewer_report(
            json.dumps(report(snapshot, "A", executions=[item])).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


@pytest.mark.parametrize("field", ["command", "stdout_excerpt"])
@pytest.mark.parametrize(
    "value",
    [
        'echo $(printf ")" https://example.com/home/alice/private)',
        'echo $(printf "literal ) here"; curl https://example.com/home/alice/private)',
        "echo $(printf `echo )`` https://example.com/home/alice/private)",
        "curl 'https://example.com/x;/home/alice/private",
        'curl "https://example.com/x;/home/alice/private',
    ],
)
def test_unclosed_or_nested_shell_state_never_exempts_http_home_paths(
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


@pytest.mark.parametrize("field", ["command", "stdout_excerpt"])
@pytest.mark.parametrize(
    "value",
    [
        "echo [https://example.com/home/alice/private]",
        "echo [label](https://example.com/home/alice/private)",
        "echo <https://example.com/home/alice/private>",
    ],
)
def test_delimited_http_home_path_is_allowed(snapshot, field, value):
    item = execution()
    item[field] = value
    parsed = parse_reviewer_report(
        json.dumps(report(snapshot, "A", executions=[item])).encode(),
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=snapshot,
    )
    assert getattr(parsed.executions[0], field) == value


@pytest.mark.parametrize("field", ["command", "stdout_excerpt"])
@pytest.mark.parametrize(
    "value",
    [
        "echo [https://example.com/home/alice]",
        "echo [https://example.com/Users/alice]",
        "echo [label](https://example.com/home/alice)",
    ],
)
def test_delimited_http_home_endpoint_is_allowed(snapshot, field, value):
    item = execution()
    item[field] = value
    parsed = parse_reviewer_report(
        json.dumps(report(snapshot, "A", executions=[item])).encode(),
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=snapshot,
    )
    assert getattr(parsed.executions[0], field) == value


@pytest.mark.parametrize("field", ["command", "stdout_excerpt"])
@pytest.mark.parametrize(
    "value",
    [
        "echo /home/]/private",
        "echo /home/)/private",
        "echo /Users/]/private",
        "echo /Users/)/private",
    ],
)
def test_delimiter_leading_direct_home_components_are_rejected(
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


@pytest.mark.parametrize("field", ["command", "stdout_excerpt"])
@pytest.mark.parametrize(
    "value",
    [
        "echo [https://example.com/home/alice",
        "echo [https://example.com/home/alice]/home/bob",
        "echo [https://example.com/home/alice];/home/bob",
        "echo [label](https://example.com/home/alice)/home/bob",
        "echo [label](https://example.com/home/alice);/home/bob",
        "echo /home/alice]",
        "echo /Users/alice)",
    ],
)
def test_delimited_home_endpoint_boundaries_are_rejected(snapshot, field, value):
    item = execution()
    item[field] = value
    with pytest.raises(SchemaError, match="EVIDENCE_SECRET_DETECTED"):
        parse_reviewer_report(
            json.dumps(report(snapshot, "A", executions=[item])).encode(),
            expected_reviewer=Reviewer.A,
            expected_round=1,
            snapshot=snapshot,
        )


@pytest.mark.parametrize("field", ["command", "stdout_excerpt"])
@pytest.mark.parametrize(
    "value",
    [
        "echo [https://example.com/home/alice/private",
        "echo [https://example.com/home/alice/private]/home/bob/private",
        "echo [https://example.com/home/alice/private];/home/bob/private",
    ],
)
def test_home_path_after_closing_url_bracket_is_rejected(snapshot, field, value):
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
    "command",
    [
        "curl 'https://example.com/a;/home/alice/private'",
        'curl "https://example.com/a|/home/alice/private"',
        "curl 'https://example.com/a&(/home/alice/private)'",
        r'curl "https://example.com/a\$/home/alice/private"',
    ],
)
def test_quoted_literal_shell_operators_remain_valid_http_url_path(snapshot, command):
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


@pytest.mark.parametrize("mask", (0o000, 0o022, 0o077))
def test_store_reviewer_report_preserves_bytes_and_forces_mode(git_repo, mask):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode() + b"\n"
    previous = os.umask(mask)
    try:
        receipt = store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
    finally:
        os.umask(previous)

    target = git_repo / ".review/inbox/round-1/A.json"
    assert target.read_bytes() == raw
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert receipt.reviewer is Reviewer.A
    assert receipt.round == 1
    assert receipt.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.path == ".review/inbox/round-1/A.json"


@pytest.mark.parametrize("relative", ("inbox", "inbox/round-1"))
@pytest.mark.parametrize("mode", (0o1700, 0o750))
def test_store_reviewer_report_rejects_nonexact_existing_report_directory(
    git_repo, relative, mode
):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    verdict_path = git_repo / ".review/verdict.json"
    verdict_before = verdict_path.read_bytes()
    directory = git_repo / ".review" / relative
    directory.chmod(mode)
    assert stat.S_IMODE(directory.stat().st_mode) == mode

    with pytest.raises(SchemaError, match="^FILE_UNSAFE$"):
        store_reviewer_report(
            git_repo,
            reviewer=Reviewer.A,
            raw=json.dumps(report(pending.snapshot, "A")).encode(),
        )

    assert verdict_path.read_bytes() == verdict_before
    assert not (git_repo / ".review/inbox/round-1/A.json").exists()


def test_store_reviewer_report_preserves_invalid_bytes_and_never_overwrites(git_repo):
    begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    verdict_path = git_repo / ".review/verdict.json"
    verdict_before = verdict_path.read_bytes()
    raw = b'{"schema":1'

    store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
    target = git_repo / ".review/inbox/round-1/A.json"
    assert target.read_bytes() == raw
    assert verdict_path.read_bytes() == verdict_before
    with pytest.raises(SchemaError, match="^REPORT_FILE_EXISTS$"):
        store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=b"new response")
    assert target.read_bytes() == raw
    assert verdict_path.read_bytes() == verdict_before


@pytest.mark.parametrize("kind", ("symlink", "fifo", "readonly"))
def test_store_reviewer_report_rejects_unsafe_existing_target(git_repo, kind):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    target = git_repo / ".review/inbox/round-1/A.json"
    target.parent.mkdir(mode=0o700, exist_ok=True)
    if kind == "symlink":
        target.symlink_to(git_repo / "tracked.txt")
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.write_bytes(b"previous")
        target.chmod(0o400)

    with pytest.raises(SchemaError, match="^FILE_UNSAFE$"):
        store_reviewer_report(
            git_repo,
            reviewer=Reviewer.A,
            raw=json.dumps(report(pending.snapshot, "A")).encode(),
        )


def test_explicit_full_panel_recovery_replace_preserves_each_new_response_exactly(
    git_repo,
):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    stale = {
        Reviewer(key): json.dumps(report(pending.snapshot, key)).encode()
        for key in "ABC"
    }
    fresh = {
        Reviewer(key): json.dumps(report(pending.snapshot, key), separators=(",", ":")).encode()
        + b"\n"
        for key in "ABC"
    }
    verdict_path = git_repo / ".review/verdict.json"
    for reviewer, raw in stale.items():
        store_reviewer_report(git_repo, reviewer=reviewer, raw=raw)

    for reviewer, raw in fresh.items():
        verdict_before = verdict_path.read_bytes()
        receipt = store_reviewer_report(
            git_repo,
            reviewer=reviewer,
            raw=raw,
            replace_pending_recovery=True,
        )
        target = git_repo / f".review/inbox/round-1/{reviewer.value}.json"
        assert target.read_bytes() == raw
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert receipt.raw_sha256 == hashlib.sha256(raw).hexdigest()
        assert verdict_path.read_bytes() == verdict_before


def test_validate_stored_reviewer_report_is_snapshot_bound_and_does_not_mutate(
    git_repo,
):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode()
    verdict_path = git_repo / ".review/verdict.json"
    verdict_before = verdict_path.read_bytes()
    store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)

    parsed, digest = validate_stored_reviewer_report(git_repo, reviewer=Reviewer.A)

    assert parsed.reviewer is Reviewer.A
    assert digest == hashlib.sha256(raw).hexdigest()
    assert verdict_path.read_bytes() == verdict_before


@pytest.mark.parametrize("control", ("\n", "\t"), ids=("lf", "tab"))
def test_pending_round_recovers_from_invalid_text_with_a_fresh_complete_panel(
    git_repo, control
):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    verdict_path = git_repo / ".review/verdict.json"
    pending_bytes = verdict_path.read_bytes()
    invalid = finding()
    invalid["rationale"] = f"first line{control}second line"
    stale_blocker_b = finding(identifier="B-R1-001", reviewer="B")
    stale_execution_b = execution("B-R1-E001")
    stale_blocker_b["execution_ids"] = [stale_execution_b["id"]]
    stale_blocker_c = finding(identifier="C-R1-001", reviewer="C")
    malformed_paths = report_paths(
        git_repo,
        pending.snapshot,
        overrides={
            "A": {"findings": [invalid]},
            "B": {
                "findings": [stale_blocker_b],
                "executions": [stale_execution_b],
            },
            "C": {"findings": [stale_blocker_c]},
        },
    )

    with pytest.raises(SchemaError, match="TEXT_INVALID"):
        finalize_round(git_repo, reviewer_paths=malformed_paths, now=NOW)

    still_pending = read_verdict(git_repo)
    assert still_pending.gate.status.value == "in_progress"
    assert still_pending.snapshot == pending.snapshot
    assert verdict_path.read_bytes() == pending_bytes

    fresh_paths = report_paths(git_repo, pending.snapshot)
    recovered = finalize_round(git_repo, reviewer_paths=fresh_paths, now=NOW)
    assert recovered.gate.status.value == "pass"
    assert recovered.snapshot == pending.snapshot
    assert all(slot.status == "complete" for slot in recovered.reviewers.values())


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


def test_round_one_replaces_owner_private_readonly_verdict(git_repo):
    """Checking a prior verdict's exact mode would reject safe owner-only state."""
    begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    verdict_path = git_repo / ".review/verdict.json"
    verdict_path.chmod(0o400)

    restarted = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )

    assert restarted.gate.status.value == "in_progress"
    assert stat.S_IMODE(verdict_path.stat().st_mode) == 0o600


def test_verdict_persistence_failure_keeps_write_failure_code(git_repo, monkeypatch):
    """Mapping failed replacement persistence to file-unsafe breaks stable callers."""
    begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    verdict_path = git_repo / ".review/verdict.json"
    before = verdict_path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("injected replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(SchemaError, match="^VERDICT_WRITE_FAILED$"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)

    assert verdict_path.read_bytes() == before
    # A failed rename leaves its complete private staging link for explicit
    # recovery: pathname rollback cannot safely distinguish later substitution.
    residue = tuple((git_repo / ".review").glob(".tmp.*"))
    assert len(residue) == 1
    assert stat.S_IMODE(residue[0].stat().st_mode) == 0o600
    assert residue[0].stat().st_uid == os.geteuid()
    assert json.loads(residue[0].read_bytes())["gate"]["status"] == "in_progress"


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
    paths["A"].chmod(0o400)
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


def test_failed_legacy_verdict_without_head_ref_migrates_on_next_round(git_repo):
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
    verdict_path = git_repo / ".review/verdict.json"
    legacy = json.loads(verdict_path.read_text(encoding="utf-8"))
    del legacy["head_ref"]
    write_json(verdict_path, legacy)
    commit_fix(git_repo)
    decisions_path = write_json(
        git_repo / ".review/inbox/round-1/decisions.json", [decision()]
    )

    second = begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=2,
        decisions_path=decisions_path,
        now=NOW,
    )

    assert second.head_ref == "refs/heads/feature"
    assert json.loads(verdict_path.read_text(encoding="utf-8"))["head_ref"] == (
        "refs/heads/feature"
    )


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


def test_cli_later_round_context_omits_own_decision_executions(git_repo):
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
    decisions_path = write_json(
        git_repo / ".review/inbox/round-1/decisions.json", [decision()]
    )
    begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=2,
        decisions_path=decisions_path,
        now=NOW,
    )

    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    context = subprocess.run(
        [sys.executable, str(cli), "context", "--reviewer", "A"],
        cwd=git_repo,
        text=True,
        capture_output=True,
    )

    assert context.returncode == 0 and context.stderr == ""
    payload = json.loads(context.stdout)
    assert payload["own_decisions"] == [
        {
            "id": "D-R1-A-001",
            "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
            "disposition": "fixed",
            "rationale": "The failure is now covered by an independent test.",
        }
    ]
    assert "executions" not in json.dumps(payload["own_decisions"])
    assert "python3 -m pytest -q" not in json.dumps(payload)
    assert "1 passed" not in json.dumps(payload)


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
    assert set(payload) == {"schema", "round", "snapshot", "initial_paths", "gate", "telemetry"}
    assert payload["telemetry"]["status"] == "active"
    context = subprocess.run(
        [sys.executable, str(cli), "context", "--reviewer", "A"],
        cwd=git_repo,
        text=True,
        capture_output=True,
    )
    context_payload = json.loads(context.stdout)
    assert context.returncode == 0
    assert context_payload["reviewer"] == "A"
    assert all(
        item["reviewer"] == "A" for item in context_payload["own_prior_findings"]
    )
    assert all(
        item["reviewer"] == "A" for item in context_payload["own_decisions"]
    )
    assert "stdout_excerpt" not in json.dumps(context_payload)
    assert context_payload["contract"] == {"report_text": 2, "diff_recipe": 1}
    assert context_payload["diff_contract"] == {
        "version": 1,
        "digest": "sha256",
        "arguments": [
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--full-index",
            context_payload["snapshot"]["merge_base_sha"]
            + ".."
            + context_payload["snapshot"]["head_sha"],
        ],
        "clear_inherited_prefixes": ["GIT_"],
        "environment": {
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false",
        },
    }
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


def run_cli_bytes(git_repo, *arguments, input=b""):
    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    return subprocess.run(
        [sys.executable, str(cli), *arguments],
        cwd=git_repo,
        input=input,
        capture_output=True,
        check=False,
    )


def test_cli_stores_and_validates_exact_report_without_mutating_verdict(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode() + b"\n"
    verdict_path = git_repo / ".review/verdict.json"
    before = verdict_path.read_bytes()
    stored = run_cli_bytes(git_repo, "store-report", "--reviewer", "A", input=raw)
    stored_payload = json.loads(stored.stdout)
    assert stored.returncode == 0 and stored.stderr == b""
    assert stored_payload == {
        "reviewer": "A",
        "round": 1,
        "status": "stored",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }
    validated = run_cli_bytes(
        git_repo, "validate-report", "--reviewer", "A", "--source", "stored"
    )
    assert validated.returncode == 0 and validated.stderr == b""
    assert json.loads(validated.stdout) == {
        "reviewer": "A",
        "round": 1,
        "status": "valid",
        "raw_sha256": stored_payload["raw_sha256"],
    }
    assert verdict_path.read_bytes() == before
    assert (git_repo / ".review/inbox/round-1/A.json").read_bytes() == raw


def test_cli_validates_stdin_bytes_and_rejects_invalid_without_mutation(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode() + b"\n"
    verdict_path = git_repo / ".review/verdict.json"
    before = verdict_path.read_bytes()
    target = git_repo / ".review/inbox/round-1/A.json"
    sentinel = b'{"sentinel":true}\n'
    target.write_bytes(sentinel)
    target.chmod(0o600)
    report_before = target.read_bytes()
    report_mode_before = stat.S_IMODE(target.stat().st_mode)
    valid = run_cli_bytes(
        git_repo,
        "validate-report",
        "--reviewer",
        "A",
        "--source",
        "stdin",
        input=raw,
    )
    assert valid.returncode == 0 and valid.stderr == b""
    assert json.loads(valid.stdout) == {
        "reviewer": "A",
        "round": 1,
        "status": "valid",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }
    invalid = run_cli_bytes(
        git_repo,
        "validate-report",
        "--reviewer",
        "A",
        "--source",
        "stdin",
        input=b'{"schema":1',
    )
    assert invalid.returncode == 1
    assert invalid.stdout == b""
    assert invalid.stderr == b"PRE_PR_TRIBUNAL:JSON_INVALID\n"
    assert verdict_path.read_bytes() == before
    assert target.read_bytes() == report_before
    assert stat.S_IMODE(target.stat().st_mode) == report_mode_before == 0o600


def test_cli_stored_validation_completes_without_reading_open_stdin(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode()
    stored = run_cli_bytes(git_repo, "store-report", "--reviewer", "A", input=raw)
    assert stored.returncode == 0

    cli = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    process = subprocess.Popen(
        [
            sys.executable,
            str(cli),
            "validate-report",
            "--reviewer",
            "A",
            "--source",
            "stored",
        ],
        cwd=git_repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            pytest.fail("stored validation read from stdin before completing")
        stdout, stderr = process.communicate()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert returncode == 0 and stderr == b""
    assert json.loads(stdout) == {
        "reviewer": "A",
        "round": 1,
        "status": "valid",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize(
    "arguments",
    (
        ("store-report", "--reviewer", "D"),
        ("validate-report", "--reviewer", "A", "--source", "other"),
        ("validate-report", "--reviewer", "A"),
    ),
)
def test_cli_report_usage_is_bounded_exit_two(git_repo, arguments):
    result = run_cli_bytes(git_repo, *arguments)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"PRE_PR_TRIBUNAL:USAGE\n"


def test_cli_stored_validation_rejects_unsafe_mode_symlink_and_swapped_content(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode()
    stored = run_cli_bytes(git_repo, "store-report", "--reviewer", "A", input=raw)
    assert stored.returncode == 0
    target = git_repo / ".review/inbox/round-1/A.json"
    target.chmod(0o644)
    invalid_mode = run_cli_bytes(
        git_repo, "validate-report", "--reviewer", "A", "--source", "stored"
    )
    assert invalid_mode.returncode == 1
    assert invalid_mode.stderr == b"PRE_PR_TRIBUNAL:FILE_UNSAFE\n"

    target.chmod(0o600)
    target.unlink()
    target.symlink_to(git_repo / "tracked.txt")
    invalid_link = run_cli_bytes(
        git_repo, "validate-report", "--reviewer", "A", "--source", "stored"
    )
    assert invalid_link.returncode == 1
    assert invalid_link.stderr == b"PRE_PR_TRIBUNAL:FILE_UNSAFE\n"

    target.unlink()
    target.write_bytes(raw.replace(b'"reviewer":"A"', b'"reviewer":"B"'))
    target.chmod(0o600)
    swapped = run_cli_bytes(
        git_repo, "validate-report", "--reviewer", "A", "--source", "stored"
    )
    assert swapped.returncode == 1
    assert swapped.stderr == b"PRE_PR_TRIBUNAL:REPORT_REVIEWER_MISMATCH\n"


def test_cli_report_stdin_reader_surfaces_oversize_without_truncation(git_repo):
    begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    oversize = b"{" + b" " * MAX_REPORT_BYTES + b"}"
    result = run_cli_bytes(git_repo, "store-report", "--reviewer", "A", input=oversize)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"PRE_PR_TRIBUNAL:REPORT_TOO_LARGE\n"
    assert not (git_repo / ".review/inbox/round-1/A.json").exists()


def test_cli_recovery_flag_replaces_only_explicit_fresh_panel_and_normal_refuses(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    stale = {}
    fresh = {}
    for reviewer in "ABC":
        stale[reviewer] = json.dumps(report(pending.snapshot, reviewer)).encode()
        fresh[reviewer] = (
            json.dumps(report(pending.snapshot, reviewer), separators=(",", ":")).encode()
            + b"\n"
        )
        result = run_cli_bytes(
            git_repo, "store-report", "--reviewer", reviewer, input=stale[reviewer]
        )
        assert result.returncode == 0
    for reviewer in "ABC":
        result = run_cli_bytes(
            git_repo,
            "store-report",
            "--reviewer",
            reviewer,
            "--replace-pending-recovery",
            input=fresh[reviewer],
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {
            "reviewer": reviewer,
            "round": 1,
            "status": "stored",
            "raw_sha256": hashlib.sha256(fresh[reviewer]).hexdigest(),
        }
        normal = run_cli_bytes(
            git_repo, "store-report", "--reviewer", reviewer, input=fresh[reviewer]
        )
        assert normal.returncode == 1
        assert normal.stderr == b"PRE_PR_TRIBUNAL:REPORT_FILE_EXISTS\n"


@pytest.mark.parametrize(("drift", "code"), (
    ("dirty", "WORKTREE_DIRTY"),
    ("committed", "SNAPSHOT_CHANGED"),
))
def test_cli_recovery_rejects_snapshot_drift_before_replacing_any_evidence(git_repo, drift, code):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    paths = report_paths(git_repo, pending.snapshot)
    fresh = {
        reviewer: json.dumps(report(pending.snapshot, reviewer), separators=(",", ":")).encode() + b"\n"
        for reviewer in "ABC"
    }
    residue = git_repo / ".review/inbox/round-1/.tmp.1234.0123456789abcdef"
    residue.write_bytes(b"preserve prior private staging evidence")
    residue.chmod(0o600)
    protected = (*paths.values(), git_repo / ".review/verdict.json", residue)
    before = {path: path.read_bytes() for path in protected}
    metadata = {path: path.lstat() for path in protected}
    if drift == "dirty":
        (git_repo / "tracked.txt").write_text("uncommitted snapshot drift\n")
    else:
        commit_fix(git_repo)

    for reviewer in "ABC":
        result = run_cli_bytes(
            git_repo, "store-report", "--reviewer", reviewer,
            "--replace-pending-recovery", input=fresh[reviewer],
        )
        assert (result.returncode, result.stdout, result.stderr) == (
            1, b"", f"PRE_PR_TRIBUNAL:{code}\n".encode(),
        )
        for path in protected:
            assert path.read_bytes() == before[path]
            after = path.lstat()
            assert stat.S_IMODE(after.st_mode) == 0o600
            for field in ("st_ino", "st_uid", "st_mode", "st_mtime_ns", "st_ctime_ns"):
                assert getattr(after, field) == getattr(metadata[path], field)
        assert list((git_repo / ".review").rglob(".tmp.*")) == [residue]
