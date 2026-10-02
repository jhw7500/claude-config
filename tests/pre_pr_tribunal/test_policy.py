import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from pre_pr_tribunal.cli import _parser, _status, main
from pre_pr_tribunal.gate import GateCode, evaluate_gate
from pre_pr_tribunal.git_state import (
    GitStateError,
    assert_auto_fix_scope,
    capture_snapshot,
)
from pre_pr_tribunal.model import GateStatus, ReviewMode, Reviewer, SchemaError
from pre_pr_tribunal.policy import (
    MAX_POLICY_REASONS,
    intensity_mode,
    parse_intensity_request,
    parse_policy_binding,
)
from pre_pr_tribunal import round_grant
from pre_pr_tribunal.verdict_store import (
    begin_round,
    finalize_round,
    submit_reviewer_report,
)


def NOW():
    return "2026-09-17T00:00:00Z"


BOUND_COMMAND = "PATH=/usr/bin:/bin /usr/bin/gh pr create --base master"


def _git(repo, *arguments):
    subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *arguments],
        check=True,
        env={
            **os.environ,
            "LC_ALL": "C",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


def _write(repo: Path, relative: str, content: str | bytes) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _repo(tmp_path, *, path="src/app.py", config=None, binary=False):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "feature")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", "https://github.com/jhw7500/claude-config.git")
    _write(repo, ".gitignore", ".review/\n")
    _write(repo, path, b"base\x00" if binary else "base\n")
    if config is not None:
        _write(repo, ".pre-pr-tribunal.toml", config)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    _write(repo, path, b"feature\x00" if binary else "feature\n")
    _git(repo, "commit", "-qam", "feature")
    return repo


def _report(
    verdict,
    reviewer,
    *,
    findings=(),
    claims=(),
    executions=(),
    coverage=None,
    populate_b=True,
):
    if reviewer == "B" and populate_b:
        if not claims:
            execution_id = f"B-R{verdict.round}-E999"
            executions = executions or (_execution(execution_id),)
            claims = ({
                "id": f"B-R{verdict.round}-C999",
                "statement": "The reviewed behavior is executable.",
                "result": "supported",
                "execution_ids": [execution_id],
                "reason": "",
            },)
        if coverage is None:
            coverage = {"complete": True, "primary_entry_paths": []}
    value = {
        "schema": 1,
        "reviewer": reviewer,
        "round": verdict.round,
        "snapshot": {
            "head_sha": verdict.head_sha,
            "diff_sha256": verdict.diff_sha256,
        },
        "status": "complete",
        "findings": list(findings),
        "executions": list(executions),
        "claims": list(claims),
        "prior_decisions": [],
    }
    if reviewer == "B" and coverage is not None:
        value["coverage"] = coverage
    return json.dumps(
        value,
        separators=(",", ":"),
    ).encode()


def _execution(identifier):
    stdout = "ok"
    return {
        "id": identifier,
        "command": "python3 -m pytest -q",
        "exit_code": 0,
        "stdout_excerpt": stdout,
        "stderr_excerpt": "",
        "capture_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "truncated": False,
    }


def _finding(identifier, reviewer, severity="HIGH"):
    return {
        "id": identifier,
        "reviewer": reviewer,
        "severity": severity,
        "title": "Contract regression",
        "rationale": "The committed behavior violates its contract.",
        "path": "src/app.py",
        "line": 1,
        "execution_ids": [],
        "acceptance_condition": "The contract is restored.",
    }


def _seal_empty(repo, verdict):
    for key in verdict.policy.active_reviewers:
        submit_reviewer_report(
            repo, reviewer=Reviewer(key), raw=_report(verdict, key), now=NOW
        )


def _authorize_next_round(
    repo, *, runtime="codex", round_number=1, decisions_path=None,
    intensity_values=None, intensity_requester=None, intensity_reason=None,
    evidence_bundle_sha256=None,
):
    preview = round_grant.preview_grant_binding(
        repo, base="master", runtime=runtime, round_number=round_number,
        decisions_path=decisions_path,
        intensity_values=intensity_values, intensity_requester=intensity_requester,
        intensity_reason=intensity_reason,
        evidence_bundle_sha256=evidence_bundle_sha256,
    )
    round_grant.record_grant(
        repo, base="master", runtime=runtime, round_number=round_number,
        decisions_path=decisions_path,
        expected_verdict_sha256=preview["verdict_sha256"],
        expected_binding_sha256=preview["binding_sha256"],
        reason="Fresh user request after the prior result", channel="relayed", now=NOW,
        intensity_values=intensity_values, intensity_requester=intensity_requester,
        intensity_reason=intensity_reason,
        evidence_bundle_sha256=evidence_bundle_sha256,
    )
    return preview


def test_docs_only_is_snapshot_bound_skipped_verdict(tmp_path):
    repo = _repo(tmp_path, path="docs/guide.md")
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is ReviewMode.OFF
    assert verdict.gate.status is GateStatus.SKIPPED
    assert verdict.policy.request.to_json() == {
        "value": 0,
        "source": "default",
        "requester": "system",
        "reason": "policy-default",
        "fail_closed_reason": None,
    }
    assert all(slot.status == "disabled" for slot in verdict.reviewers.values())
    assert evaluate_gate(repo, BOUND_COMMAND).code is GateCode.PASS


def test_only_documentation_paths_are_off(tmp_path):
    docs = _repo(tmp_path / "docs-case", path="docs/guide.md")
    docs_verdict = begin_round(
        docs, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert docs_verdict.policy.risk_floor == 0
    assert docs_verdict.policy.mode is ReviewMode.OFF
    assert evaluate_gate(docs, BOUND_COMMAND).code is GateCode.PASS

    for index, path in enumerate((
        "notes/guide.md",
        "README.md",
        "skills/pre-pr-tribunal/SKILL.md",
        "commands/review.md",
        "config/security.yaml",
    )):
        repo = _repo(tmp_path / f"operational-case-{index}", path=path)
        verdict = begin_round(
            repo, base="master", runtime="codex", round_number=1, now=NOW
        )
        assert verdict.policy.risk_floor == 100
        assert verdict.policy.mode is ReviewMode.ITERATIVE
        decision = evaluate_gate(repo, BOUND_COMMAND)
        assert decision.block is True
        assert decision.code is GateCode.REVIEW_INCOMPLETE


def test_copy_into_documentation_path_is_iterative(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "feature")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://github.com/jhw7500/claude-config.git",
    )
    _write(repo, ".gitignore", ".review/\n")
    _write(repo, "src/app.py", "shared content\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    _write(repo, "docs/copied.md", "shared content\n")
    _git(repo, "add", "docs/copied.md")
    _git(repo, "commit", "-qm", "copy into docs")

    snapshot = capture_snapshot(repo, "master", now=NOW)
    verdict = begin_round(
        repo, base="master", runtime="codex", round_number=1, now=NOW
    )

    assert [(item.status, item.path, item.old_path) for item in snapshot.paths] == [
        ("C100", "docs/copied.md", "src/app.py")
    ]
    assert snapshot.initial_paths == ("docs/copied.md",)
    assert_auto_fix_scope(snapshot.initial_paths, ("docs/copied.md",))
    with pytest.raises(GitStateError, match="^AUTO_FIX_SCOPE_EXPANDED$"):
        assert_auto_fix_scope(
            snapshot.initial_paths,
            ("docs/copied.md", "src/app.py"),
        )
    assert verdict.policy.risk_floor == 100
    assert verdict.policy.mode is ReviewMode.ITERATIVE


@pytest.mark.parametrize(
    "path",
    (
        ".github/dependabot.yml",
        ".github/dependabot.yaml",
    ),
)
def test_dependency_control_paths_are_iterative(tmp_path, path):
    repo = _repo(tmp_path, path=path)
    verdict = begin_round(
        repo, base="master", runtime="codex", round_number=1, now=NOW
    )

    assert verdict.policy.risk_floor == 100
    assert verdict.policy.mode is ReviewMode.ITERATIVE
    assert f"sensitive-file:{path}" in verdict.policy.reasons


def test_reviewer_b_requires_complete_claim_coverage_before_sealing(tmp_path):
    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)

    with pytest.raises(SchemaError, match="^CLAIM_COVERAGE_INVALID$"):
        submit_reviewer_report(
            repo,
            reviewer=Reviewer.B,
            raw=_report(
                verdict,
                "B",
                coverage={"complete": True, "primary_entry_paths": []},
                populate_b=False,
            ),
            now=NOW,
        )

    execution = _execution("B-R1-E001")
    claim = {
        "id": "B-R1-C001",
        "statement": "The documented entry path runs.",
        "result": "supported",
        "execution_ids": ["B-R1-E001"],
        "reason": "",
    }
    with pytest.raises(SchemaError, match="^CLAIM_COVERAGE_INVALID$"):
        submit_reviewer_report(
            repo,
            reviewer=Reviewer.B,
            raw=_report(
                verdict,
                "B",
                claims=(claim,),
                executions=(execution,),
                coverage={"complete": False, "primary_entry_paths": []},
            ),
            now=NOW,
        )

    with pytest.raises(SchemaError, match="^CLAIM_COVERAGE_INVALID$"):
        submit_reviewer_report(
            repo,
            reviewer=Reviewer.B,
            raw=_report(
                verdict,
                "B",
                claims=(claim,),
                executions=(execution,),
                coverage={
                    "complete": True,
                    "primary_entry_paths": [
                        {"path": "scripts/entry.py", "claim_id": "B-R1-C002"}
                    ],
                },
            ),
            now=NOW,
        )

    submit_reviewer_report(
        repo,
        reviewer=Reviewer.B,
        raw=_report(
            verdict,
            "B",
            claims=(claim,),
            executions=(execution,),
            coverage={
                "complete": True,
                "primary_entry_paths": [
                    {"path": "scripts/entry.py", "claim_id": "B-R1-C001"}
                ],
            },
        ),
        now=NOW,
    )


def test_root_build_configuration_cannot_be_classified_as_documentation(tmp_path):
    repo = _repo(tmp_path, path="CMakeLists.txt")
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    decision = evaluate_gate(repo, BOUND_COMMAND)
    assert verdict.policy.risk_floor == 100
    assert verdict.policy.mode is ReviewMode.ITERATIVE
    assert decision.block is True
    assert decision.code is GateCode.REVIEW_INCOMPLETE


def test_default_source_is_single_with_a_b_only(tmp_path):
    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is ReviewMode.SINGLE
    assert verdict.policy.active_reviewers == ("A", "B")
    assert verdict.policy.reviewers["A"].model != verdict.policy.reviewers["B"].model
    assert verdict.reviewers["C"].status == "disabled"
    with pytest.raises(SchemaError, match="^REVIEWER_DISABLED$"):
        submit_reviewer_report(
            repo, reviewer=Reviewer.C, raw=_report(verdict, "C"), now=NOW
        )


def test_sensitive_and_binary_changes_are_iterative(tmp_path):
    hooks = _repo(tmp_path / "hooks-case", path="hooks/check.py")
    hook_verdict = begin_round(
        hooks, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert hook_verdict.policy.risk_floor == 100
    assert hook_verdict.policy.mode is ReviewMode.ITERATIVE

    binary = _repo(tmp_path / "binary-case", path="docs/blob.md", binary=True)
    binary_verdict = begin_round(
        binary, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert binary_verdict.policy.risk_floor == 100
    assert "binary-change" in binary_verdict.policy.reasons


def test_mixed_risk_and_rename_fail_closed_to_iterative(tmp_path):
    mixed = _repo(tmp_path / "mixed-case")
    _write(mixed, "docs/guide.md", "feature docs\n")
    _git(mixed, "add", ".")
    _git(mixed, "commit", "-qm", "add docs")
    mixed_verdict = begin_round(
        mixed, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert mixed_verdict.policy.risk_floor == 100
    assert "mixed-risk-change" in mixed_verdict.policy.reasons

    renamed = _repo(tmp_path / "rename-case")
    _write(renamed, "src/app.py", "base\n")
    _git(renamed, "mv", "src/app.py", "src/renamed.py")
    _git(renamed, "add", ".")
    _git(renamed, "commit", "-qm", "rename source")
    renamed_verdict = begin_round(
        renamed, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert renamed_verdict.policy.risk_floor == 100
    assert any(
        reason.startswith("unsafe-status:R")
        for reason in renamed_verdict.policy.reasons
    )


def test_repository_config_cannot_lower_builtin_floor(tmp_path):
    repo = _repo(tmp_path, config='[policy]\n"src/**" = "off"\n')
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.risk_floor == 50
    assert verdict.policy.mode is ReviewMode.SINGLE


def test_overlapping_repository_rules_use_highest_matching_intensity(tmp_path):
    config = '[policy]\n"**" = "iterative"\n"src/**" = "single"\n'
    repo = _repo(tmp_path, config=config)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.risk_floor == 100
    assert verdict.policy.mode is ReviewMode.ITERATIVE


def test_invalid_or_repeated_intensity_fails_closed_to_three_reviewers(tmp_path):
    repo = _repo(tmp_path, path="docs/guide.md")
    verdict = begin_round(
        repo,
        base="master",
        runtime="codex",
        round_number=1,
        intensity_values=("0", "50"),
        now=NOW,
    )
    assert verdict.policy.request.source == "fail_closed"
    assert verdict.policy.effective_intensity == 100
    assert verdict.policy.active_reviewers == ("A", "B", "C")


@pytest.mark.parametrize(
    "duplicate_flag",
    ("--intensity-requester", "--intensity-reason"),
)
def test_cli_repeated_intensity_attribution_fails_closed(duplicate_flag):
    arguments = [
        "begin",
        "--base",
        "master",
        "--runtime",
        "codex",
        "--round",
        "1",
        "--intensity",
        "0",
        "--intensity-requester",
        "maintainer",
        "--intensity-reason",
        "bounded review",
        duplicate_flag,
        "duplicate",
    ]
    parsed = _parser().parse_args(arguments)
    request = parse_intensity_request(
        parsed.intensity,
        requester=parsed.intensity_requester,
        reason=parsed.intensity_reason,
    )
    assert request.source == "fail_closed"
    assert request.value == 100
    assert request.fail_closed_reason == "INTENSITY_ATTRIBUTION_MULTIPLE"


def test_all_reviewers_may_be_disabled_only_for_off_mode(tmp_path):
    config = """
[reviewer.A]
enabled = false
[reviewer.B]
enabled = false
[reviewer.C]
enabled = false
""".lstrip()
    docs = _repo(tmp_path / "docs-case", path="docs/guide.md", config=config)
    docs_verdict = begin_round(
        docs, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert docs_verdict.policy.mode is ReviewMode.OFF
    assert docs_verdict.policy.active_reviewers == ()

    source = _repo(tmp_path / "source-case", config=config)
    with pytest.raises(SchemaError, match="^CONFIG_INVALID$"):
        begin_round(source, base="master", runtime="codex", round_number=1, now=NOW)


DECLARED_ALL_DISABLED = """
[unexecutable]
"src/**" = "NO_CROSS_SDK"

[reviewer.A]
enabled = false
[reviewer.B]
enabled = false
[reviewer.C]
enabled = false
""".lstrip()


def test_all_reviewers_disabled_with_a_declaration_still_fails_closed(tmp_path):
    """A declaration must not stand in for a reviewer the configuration disabled.

    Regression for tribunal round-1 finding A-R1-002: forcing Reviewer B made
    the all-disabled check unreachable, so "at least one reviewer must be
    enabled for a non-off mode" silently stopped holding for exactly the
    configurations that declare a pattern.
    """
    repo = _repo(tmp_path, config=DECLARED_ALL_DISABLED)
    with pytest.raises(SchemaError, match="^CONFIG_INVALID$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)


def test_all_reviewers_disabled_with_a_declaration_stays_off_for_docs(tmp_path):
    """Preservation check: the invariant still binds only the non-off modes.

    Passes both before and after the A-R1-002 fix; it pins the `off` half of
    `test_all_reviewers_may_be_disabled_only_for_off_mode` against the added
    pre-override check.
    """
    repo = _repo(tmp_path, path="docs/guide.md", config=DECLARED_ALL_DISABLED)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is ReviewMode.OFF
    assert verdict.policy.active_reviewers == ()


def test_persisted_request_source_semantics_are_strict(tmp_path):
    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    payload = verdict.policy.to_json()
    payload["request"] = {
        **payload["request"],
        "value": 50,
        "source": "default",
    }
    with pytest.raises(SchemaError, match="^POLICY_INVALID$"):
        parse_policy_binding(payload, runtime="codex")


def test_config_selects_enabled_reviewers_and_runtime_models(tmp_path):
    config = """
[policy]
"src/**" = "iterative"
[reviewer.A]
enabled = true
model = { claude = "sonnet", codex = "gpt-6-astra" }
[reviewer.B]
enabled = false
[reviewer.C]
enabled = true
model = { codex = "gpt-5.6-sol" }
""".lstrip()
    repo = _repo(tmp_path, config=config)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.active_reviewers == ("A", "C")
    assert verdict.policy.reviewers["A"].model == "gpt-6-astra"
    assert verdict.policy.reviewers["C"].model == "gpt-5.6-sol"


def test_unknown_model_stops_before_dispatch(tmp_path):
    repo = _repo(
        tmp_path,
        config='[reviewer.A]\nmodel = { codex = "unknown-model" }\n',
    )
    with pytest.raises(SchemaError, match="^MODEL_UNKNOWN$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)


@pytest.mark.parametrize("reason", (
    "The required SDK is unavailable.",
    "BUDGET_EXHAUSTED: fresh-execution cap",
))
def test_active_only_finalize_and_unverified_is_inconclusive(tmp_path, reason):
    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(
        repo, reviewer=Reviewer.A, raw=_report(verdict, "A"), now=NOW
    )
    claim = {
        "id": "B-R1-C001",
        "statement": "The documented entry path runs.",
        "result": "unverified",
        "execution_ids": [],
        "reason": reason,
    }
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.B,
        raw=_report(verdict, "B", claims=(claim,)),
        now=NOW,
    )
    final = finalize_round(repo, now=NOW)
    assert final.gate.status is GateStatus.INCONCLUSIVE
    assert evaluate_gate(repo, BOUND_COMMAND).code is GateCode.VERIFICATION_INCOMPLETE


def _refuted_report(verdict, *, findings=()):
    execution_id = f"B-R{verdict.round}-E001"
    claim_id = f"B-R{verdict.round}-C001"
    evidence_findings = tuple(
        {**finding, "execution_ids": [execution_id]}
        for finding in findings
    )
    claim = {
        "id": claim_id,
        "statement": "The documented primary entry path runs.",
        "result": "refuted",
        "execution_ids": [execution_id],
        "reason": "",
    }
    return _report(
        verdict,
        "B",
        findings=evidence_findings,
        executions=(_execution(execution_id),),
        claims=(claim,),
        coverage={
            "complete": True,
            "primary_entry_paths": [
                {"path": "src/app.py", "claim_id": claim_id},
            ],
        },
    )


def test_refuted_claim_without_blocker_is_retryable_and_cannot_seal(tmp_path):
    from pre_pr_tribunal.attempt_store import REPORT_RETRYABLE_CODES

    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)

    with pytest.raises(SchemaError) as error:
        submit_reviewer_report(
            repo,
            reviewer=Reviewer.B,
            raw=_refuted_report(verdict),
            now=NOW,
        )

    assert error.value.code == "REFUTED_CLAIM_REQUIRES_BLOCKER"
    assert error.value.code in REPORT_RETRYABLE_CODES


def test_refuted_claim_with_blocker_produces_terminal_failure(tmp_path):
    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.A,
        raw=_report(verdict, "A"),
        now=NOW,
    )
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.B,
        raw=_refuted_report(
            verdict,
            findings=(_finding("B-R1-001", "B", severity="HIGH"),),
        ),
        now=NOW,
    )

    final = finalize_round(repo, now=NOW)

    assert final.gate.status is GateStatus.FAIL
    assert final.gate.blocking_count == 1
    assert evaluate_gate(repo, BOUND_COMMAND).code is GateCode.BLOCKERS_OPEN


def test_high_risk_budget_exhaustion_preserves_refuted_blocker(tmp_path):
    repo = _repo(tmp_path, path="config/security.yaml")
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.risk_floor == 100
    submit_reviewer_report(repo, reviewer=Reviewer.A, raw=_report(verdict, "A"), now=NOW)

    value = json.loads(_refuted_report(
        verdict, findings=(_finding("B-R1-001", "B", severity="HIGH"),)
    ))
    value["findings"][0]["path"] = "config/security.yaml"
    value["coverage"]["primary_entry_paths"][0]["path"] = "config/security.yaml"
    value["claims"].extend({
        "id": f"B-R1-C{index:03d}",
        "statement": f"Additional required behavior {index} is safe.",
        "result": "supported",
        "execution_ids": ["B-R1-E001"],
        "reason": "",
    } for index in range(2, 17))
    value["claims"].append({
        "id": "B-R1-C017",
        "statement": "The remaining required path preserves permissions.",
        "result": "unverified",
        "execution_ids": [],
        "reason": "BUDGET_EXHAUSTED: verified-claim cap",
    })
    submit_reviewer_report(
        repo, reviewer=Reviewer.B, raw=json.dumps(value).encode(), now=NOW,
    )

    final = finalize_round(repo, now=NOW)
    assert final.gate.status is GateStatus.FAIL
    assert final.gate.blocking_count == 1
    assert evaluate_gate(repo, BOUND_COMMAND).code is GateCode.BLOCKERS_OPEN


def test_high_risk_budget_keeps_independent_refuted_blockers(tmp_path):
    repo = _repo(tmp_path, path="config/security.yaml")
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.risk_floor == 100
    submit_reviewer_report(repo, reviewer=Reviewer.A, raw=_report(verdict, "A"), now=NOW)

    executions = []
    claims = []
    findings = []
    for index in range(1, 18):
        execution_id = f"B-R1-E{index:03d}"
        claim_id = f"B-R1-C{index:03d}"
        finding_id = f"B-R1-{index:03d}"
        evidence = _execution(execution_id)
        evidence.update(
            exit_code=1,
            stdout_excerpt="refuted",
            capture_sha256=hashlib.sha256(b"refuted").hexdigest(),
        )
        executions.append(evidence)
        claims.append({
            "id": claim_id,
            "statement": f"Required behavior {index} remains safe.",
            "result": "refuted",
            "execution_ids": [execution_id],
            "reason": "",
        })
        finding = _finding(finding_id, "B", severity="HIGH")
        finding.update(path="config/security.yaml", execution_ids=[execution_id])
        findings.append(finding)

    raw = _report(
        verdict,
        "B",
        findings=findings,
        executions=executions,
        claims=claims,
        coverage={
            "complete": True,
            "primary_entry_paths": [
                {"path": "config/security.yaml", "claim_id": "B-R1-C001"},
            ],
        },
    )
    receipt = submit_reviewer_report(repo, reviewer=Reviewer.B, raw=raw, now=NOW)
    assert receipt.raw_sha256 == hashlib.sha256(raw).hexdigest()

    final = finalize_round(repo, now=NOW)
    assert final.gate.status is GateStatus.FAIL
    assert final.gate.blocking_count == 17
    assert evaluate_gate(repo, BOUND_COMMAND).code is GateCode.BLOCKERS_OPEN


def test_inconclusive_restart_cannot_lower_same_snapshot_intensity(tmp_path):
    repo = _repo(tmp_path, path="docs/guide.md")
    verdict = begin_round(
        repo,
        base="master",
        runtime="codex",
        round_number=1,
        intensity_values=("50",),
        intensity_requester="maintainer",
        intensity_reason="verify the documented flow",
        now=NOW,
    )
    submit_reviewer_report(
        repo, reviewer=Reviewer.A, raw=_report(verdict, "A"), now=NOW
    )
    claim = {
        "id": "B-R1-C001",
        "statement": "The documented entry path runs.",
        "result": "unverified",
        "execution_ids": [],
        "reason": "The required SDK is unavailable.",
    }
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.B,
        raw=_report(verdict, "B", claims=(claim,)),
        now=NOW,
    )
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.INCONCLUSIVE
    with pytest.raises(SchemaError, match="^ROUND_TRANSITION_INVALID$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)

    _git(repo, "commit", "--allow-empty", "-qm", "metadata only")
    with pytest.raises(SchemaError, match="^ROUND_TRANSITION_INVALID$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)

    _authorize_next_round(
        repo, intensity_values=("50",), intensity_requester="maintainer",
        intensity_reason="retry with newly available evidence",
    )
    restarted = begin_round(
        repo,
        base="master",
        runtime="codex",
        round_number=1,
        intensity_values=("50",),
        intensity_requester="maintainer",
        intensity_reason="retry with newly available evidence",
        now=NOW,
    )
    assert restarted.policy.effective_intensity == 50


def test_single_failure_requires_changed_snapshot_for_round_one_retry(tmp_path):
    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.A,
        raw=_report(verdict, "A", findings=(_finding("A-R1-001", "A"),)),
        now=NOW,
    )
    submit_reviewer_report(
        repo, reviewer=Reviewer.B, raw=_report(verdict, "B"), now=NOW
    )
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.FAIL
    with pytest.raises(SchemaError, match="^ROUND_TRANSITION_INVALID$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    _git(repo, "commit", "--allow-empty", "-qm", "metadata only")
    with pytest.raises(SchemaError, match="^ROUND_TRANSITION_INVALID$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    _git(repo, "branch", "base-metadata", "refs/remotes/origin/master")
    _git(repo, "checkout", "-q", "base-metadata")
    _git(repo, "commit", "--allow-empty", "-qm", "base metadata only")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "base-metadata")
    _git(repo, "branch", "-D", "base-metadata")
    with pytest.raises(SchemaError, match="^ROUND_TRANSITION_INVALID$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    _write(repo, "src/app.py", "fixed\n")
    _git(repo, "commit", "-qam", "fix")
    _authorize_next_round(repo)
    restarted = begin_round(
        repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert restarted.round == 1
    assert restarted.head_sha != verdict.head_sha


def test_terminal_failure_requires_one_shot_re_review_grant(tmp_path):
    repo = _repo(tmp_path)
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(
        repo, reviewer=Reviewer.A,
        raw=_report(first, "A", findings=(_finding("A-R1-001", "A"),)), now=NOW,
    )
    submit_reviewer_report(repo, reviewer=Reviewer.B, raw=_report(first, "B"), now=NOW)
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.FAIL
    _write(repo, "src/app.py", "fixed\n")
    _git(repo, "commit", "-qam", "fix")
    verdict_path = repo / ".review/verdict.json"
    before = verdict_path.read_bytes()

    with pytest.raises(SchemaError, match="^RE_REVIEW_GRANT_REQUIRED$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict_path.read_bytes() == before

    approval = _authorize_next_round(repo)
    grant_path = repo / ".review/re-review-grant.json"
    assert approval["verdict_sha256"] == hashlib.sha256(before).hexdigest()
    assert grant_path.stat().st_mode & 0o777 == 0o600
    restarted = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert restarted.head_sha != first.head_sha
    assert restarted.gate.status is GateStatus.IN_PROGRESS
    assert not grant_path.exists()


def test_re_review_grant_accepts_large_valid_binding(tmp_path):
    pattern_prefix = "src/" + "*" * 700
    config = "[policy]\n" + "".join(
        f'"{pattern_prefix}mod{index}.py" = 50\n' for index in range(48)
    )
    repo = _repo(tmp_path, config=config)
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    _seal_empty(repo, first)
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.PASS
    for index in range(48):
        _write(repo, f"src/mod{index}.py", "new\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add matching sources")

    binding = round_grant.preview_grant_binding(
        repo, base="master", runtime="codex", round_number=1, now=NOW,
    )
    assert len(json.dumps(binding, ensure_ascii=False).encode()) > round_grant.MAX_GRANT_BYTES
    _authorize_next_round(repo)
    grant_path = repo / ".review/re-review-grant.json"
    payload = json.loads(grant_path.read_text(encoding="utf-8"))
    assert payload["schema"] == 2
    assert "binding" not in payload
    assert payload["verdict_sha256"] == binding["verdict_sha256"]
    assert payload["binding_sha256"] == binding["binding_sha256"]
    assert len(grant_path.read_bytes()) <= round_grant.MAX_GRANT_BYTES
    assert begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert not grant_path.exists()


def test_re_review_grant_rejects_tampered_binding_digest(tmp_path):
    repo = _repo(tmp_path)
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    _seal_empty(repo, first)
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.PASS
    _write(repo, "src/app.py", "next target\n")
    _git(repo, "commit", "-qam", "next target")
    _authorize_next_round(repo)
    grant_path = repo / ".review/re-review-grant.json"
    payload = json.loads(grant_path.read_text(encoding="utf-8"))
    payload["binding_sha256"] = "0" * 64
    grant_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SchemaError, match="^RE_REVIEW_GRANT_REQUIRED$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert grant_path.exists()


def test_re_review_grant_rejects_target_snapshot_drift(tmp_path):
    repo = _repo(tmp_path)
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    _seal_empty(repo, first)
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.PASS
    _authorize_next_round(repo)
    _write(repo, "src/app.py", "changed after approval\n")
    _git(repo, "commit", "-qam", "post-approval change")

    with pytest.raises(SchemaError, match="^RE_REVIEW_GRANT_REQUIRED$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert (repo / ".review/re-review-grant.json").exists()
    assert (repo / ".review/verdict.json").exists()


def test_re_review_grant_rejects_drift_between_preview_and_record(tmp_path):
    repo = _repo(tmp_path, path="docs/guide.md")
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert first.gate.status is GateStatus.SKIPPED
    preview = round_grant.preview_grant_binding(
        repo, base="master", runtime="codex", round_number=1, now=NOW,
    )
    _write(repo, "docs/guide.md", "changed after preview\n")
    _git(repo, "commit", "-qam", "post-preview change")

    with pytest.raises(SchemaError, match="^RE_REVIEW_BINDING_CHANGED$"):
        round_grant.record_grant(
            repo, base="master", runtime="codex", round_number=1,
            expected_verdict_sha256=preview["verdict_sha256"],
            expected_binding_sha256=preview["binding_sha256"],
            reason="Fresh user request for the previewed target", channel="relayed",
            now=NOW,
        )
    assert not (repo / ".review/re-review-grant.json").exists()


def test_re_review_grant_binds_evidence_selection(tmp_path):
    repo = _repo(tmp_path)
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    _seal_empty(repo, first)
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.PASS
    _write(repo, "src/app.py", "next target\n")
    _git(repo, "commit", "-qam", "next target")
    _authorize_next_round(repo)
    verdict_before = (repo / ".review/verdict.json").read_bytes()

    with pytest.raises(SchemaError, match="^RE_REVIEW_GRANT_REQUIRED$"):
        begin_round(
            repo, base="master", runtime="codex", round_number=1,
            evidence_bundle_sha256="0" * 64, now=NOW,
        )
    assert (repo / ".review/verdict.json").read_bytes() == verdict_before
    assert (repo / ".review/re-review-grant.json").exists()
    second = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert second.head_sha != first.head_sha


def test_cli_requires_grant_for_new_begin_after_terminal_verdict(
    tmp_path, monkeypatch, capsys,
):
    repo = _repo(tmp_path, path="docs/guide.md")
    monkeypatch.chdir(repo)
    args = ["--base", "master", "--runtime", "codex", "--round", "1"]
    assert main(["policy-preview", "--base", "master", "--runtime", "codex"], wall_clock=NOW) == 0
    capsys.readouterr()
    assert main(["begin", *args], wall_clock=NOW) == 0
    capsys.readouterr()
    _write(repo, "docs/guide.md", "changed after first verdict\n")
    _git(repo, "commit", "-qam", "change")

    assert main(["begin", *args], wall_clock=NOW) == 1
    assert "PRE_PR_TRIBUNAL:RE_REVIEW_GRANT_REQUIRED" in capsys.readouterr().err
    assert main(["re-review-preview", *args], wall_clock=NOW) == 0
    preview = json.loads(capsys.readouterr().out)
    assert main([
        "re-review-grant", *args,
        "--verdict-sha256", preview["verdict_sha256"],
        "--binding-sha256", preview["binding_sha256"],
        "--reason", "Fresh user approval for this exact target", "--relayed",
    ], wall_clock=NOW) == 0
    capsys.readouterr()
    assert main(["begin", *args], wall_clock=NOW) == 0
    capsys.readouterr()
    assert not (repo / ".review/re-review-grant.json").exists()


def test_cli_re_review_preview_binds_evidence_argument(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, path="docs/guide.md")
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert first.gate.status is GateStatus.SKIPPED
    _write(repo, "docs/guide.md", "next target\n")
    _git(repo, "commit", "-qam", "next target")
    monkeypatch.chdir(repo)
    args = ["--base", "master", "--runtime", "codex", "--round", "1"]
    digest = "0" * 64
    assert main(["re-review-preview", *args, "--evidence-bundle", digest], wall_clock=NOW) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["target"]["evidence"]["requested_bundle_sha256"] == digest
    assert main([
        "re-review-grant", *args, "--evidence-bundle", digest,
        "--verdict-sha256", preview["verdict_sha256"],
        "--binding-sha256", preview["binding_sha256"],
        "--reason", "Fresh user approval for this exact target", "--relayed",
    ], wall_clock=NOW) == 0


def test_re_review_grant_file_must_be_private_regular_file(tmp_path):
    repo = _repo(tmp_path)
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    _seal_empty(repo, first)
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.PASS
    grant_path = repo / ".review/re-review-grant.json"
    grant_path.symlink_to("verdict.json")
    verdict_before = (repo / ".review/verdict.json").read_bytes()

    with pytest.raises(SchemaError, match="^RE_REVIEW_GRANT_FILE_UNSAFE$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert (repo / ".review/verdict.json").read_bytes() == verdict_before


def test_re_review_grant_rejects_world_readable_file(tmp_path):
    repo = _repo(tmp_path, path="docs/guide.md")
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert first.gate.status is GateStatus.SKIPPED
    _write(repo, "docs/guide.md", "next target\n")
    _git(repo, "commit", "-qam", "next target")
    _authorize_next_round(repo)
    grant_path = repo / ".review/re-review-grant.json"
    grant_path.chmod(0o644)
    verdict_before = (repo / ".review/verdict.json").read_bytes()

    with pytest.raises(SchemaError, match="^RE_REVIEW_GRANT_FILE_UNSAFE$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert (repo / ".review/verdict.json").read_bytes() == verdict_before


def test_iterative_second_round_requires_decisions_bound_approval(tmp_path):
    repo = _repo(tmp_path, path="hooks/guard.py")
    first = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert first.policy.mode is ReviewMode.ITERATIVE
    for key in first.policy.active_reviewers:
        findings = (_finding("A-R1-001", "A"),) if key == "A" else ()
        submit_reviewer_report(
            repo, reviewer=Reviewer(key), raw=_report(first, key, findings=findings),
            now=NOW,
        )
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.FAIL
    _write(repo, "hooks/guard.py", "fixed\n")
    _git(repo, "commit", "-qam", "fix")
    decisions_path = repo / ".review/inbox/round-1/decisions.json"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    decisions_path.write_text(json.dumps([{
        "id": "D-R1-A-001",
        "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
        "disposition": "fixed", "rationale": "Covered by a regression test.",
        "executions": [_execution("D-R1-E001")],
    }]), encoding="utf-8")
    decisions_path.chmod(0o600)

    with pytest.raises(SchemaError, match="^RE_REVIEW_GRANT_REQUIRED$"):
        begin_round(
            repo, base="master", runtime="codex", round_number=2,
            decisions_path=decisions_path, now=NOW,
        )
    _authorize_next_round(repo, round_number=2, decisions_path=decisions_path)
    second = begin_round(
        repo, base="master", runtime="codex", round_number=2,
        decisions_path=decisions_path, now=NOW,
    )
    assert second.round == 2
    assert second.head_sha != first.head_sha


def test_failed_single_mode_cannot_restart_same_snapshot_at_iterative_intensity(
    tmp_path,
):
    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.A,
        raw=_report(verdict, "A", findings=(_finding("A-R1-001", "A"),)),
        now=NOW,
    )
    submit_reviewer_report(
        repo, reviewer=Reviewer.B, raw=_report(verdict, "B"), now=NOW
    )
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.FAIL
    with pytest.raises(SchemaError, match="^ROUND_TRANSITION_INVALID$"):
        begin_round(
            repo,
            base="master",
            runtime="codex",
            round_number=1,
            intensity_values=("80",),
            intensity_requester="maintainer",
            intensity_reason="escalate after blocker",
            now=NOW,
        )


def test_failed_iterative_round_cannot_restart_at_higher_iterative_intensity(tmp_path):
    repo = _repo(tmp_path, path="docs/guide.md")
    verdict = begin_round(
        repo,
        base="master",
        runtime="codex",
        round_number=1,
        intensity_values=("67",),
        intensity_requester="maintainer",
        intensity_reason="run iterative review",
        now=NOW,
    )
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.A,
        raw=_report(verdict, "A", findings=(_finding("A-R1-001", "A"),)),
        now=NOW,
    )
    submit_reviewer_report(
        repo, reviewer=Reviewer.B, raw=_report(verdict, "B"), now=NOW
    )
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.FAIL

    with pytest.raises(SchemaError, match="^ROUND_TRANSITION_INVALID$"):
        begin_round(
            repo,
            base="master",
            runtime="codex",
            round_number=1,
            intensity_values=("68",),
            intensity_requester="maintainer",
            intensity_reason="retry iterative review",
            now=NOW,
        )


def test_pr_appendix_contains_only_nonblocking_findings(tmp_path):
    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    advisory = _finding("A-R1-001", "A", severity="MEDIUM")
    escaped_advisory = {
        **_finding("A-R1-002", "A", severity="LOW"),
        "title": "Unsafe [link](relative)",
        "path": "src/[app].py",
    }
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.A,
        raw=_report(verdict, "A", findings=(advisory, escaped_advisory)),
        now=NOW,
    )
    submit_reviewer_report(
        repo, reviewer=Reviewer.B, raw=_report(verdict, "B"), now=NOW
    )
    final = finalize_round(repo, now=NOW)
    status = _status(final)
    assert final.gate.status is GateStatus.PASS
    assert "[MEDIUM] Contract regression" in status["pr_appendix"]
    assert "[link](relative)" not in status["pr_appendix"]
    assert r"\[link\]\(relative\)" in status["pr_appendix"]
    assert r"src/\[app\]\.py" in status["pr_appendix"]


def test_pr_appendix_is_visibly_truncated_before_byte_limit(tmp_path):
    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    findings = tuple(
        {
            **_finding(f"A-R1-{index:03d}", "A", severity="MEDIUM"),
            "title": "x" * 8000,
        }
        for index in range(1, 6)
    )
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.A,
        raw=_report(verdict, "A", findings=findings),
        now=NOW,
    )
    submit_reviewer_report(
        repo, reviewer=Reviewer.B, raw=_report(verdict, "B"), now=NOW
    )
    final = finalize_round(repo, now=NOW)
    appendix = _status(final)["pr_appendix"]
    assert final.gate.status is GateStatus.PASS
    assert len(appendix.encode("utf-8")) <= 32 * 1024
    assert "Additional advisory findings were omitted" in appendix


UNEXECUTABLE_CONFIG = '[unexecutable]\n"src/**" = "NO_CROSS_SDK"\n'


def _unexecutable_report(verdict, *, result, cover=True, path="src/app.py"):
    execution_id = f"B-R{verdict.round}-E001"
    claim_id = f"B-R{verdict.round}-C001"
    if result == "unverified":
        executions = ()
        claims = ({
            "id": claim_id,
            "statement": "The documented primary entry path runs.",
            "result": "unverified",
            "execution_ids": [],
            "reason": "This view has no cross SDK.",
        },)
    else:
        executions = (_execution(execution_id),)
        claims = ({
            "id": claim_id,
            "statement": "The documented primary entry path runs.",
            "result": result,
            "execution_ids": [execution_id],
            "reason": "",
        },)
    paths = [{"path": path, "claim_id": claim_id}] if cover else []
    return _report(
        verdict,
        "B",
        claims=claims,
        executions=executions,
        coverage={"complete": True, "primary_entry_paths": paths},
    )


def test_declared_unexecutable_path_cannot_be_claimed_supported(tmp_path):
    repo = _repo(tmp_path, config=UNEXECUTABLE_CONFIG)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    with pytest.raises(SchemaError) as error:
        submit_reviewer_report(
            repo,
            reviewer=Reviewer.B,
            raw=_unexecutable_report(verdict, result="supported"),
            now=NOW,
        )
    assert error.value.code == "UNEXECUTABLE_PATH_NOT_UNVERIFIED"


def test_declared_unexecutable_path_must_appear_in_coverage(tmp_path):
    repo = _repo(tmp_path, config=UNEXECUTABLE_CONFIG)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    with pytest.raises(SchemaError) as error:
        submit_reviewer_report(
            repo,
            reviewer=Reviewer.B,
            raw=_unexecutable_report(verdict, result="unverified", cover=False),
            now=NOW,
        )
    assert error.value.code == "UNEXECUTABLE_PATH_UNCOVERED"


def test_declared_unexecutable_path_seals_when_unverified(tmp_path):
    repo = _repo(tmp_path, config=UNEXECUTABLE_CONFIG)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.B,
        raw=_unexecutable_report(verdict, result="unverified"),
        now=NOW,
    )


def test_supported_claim_seals_without_a_declaration(tmp_path):
    """Control: the same report seals when nothing is declared unexecutable."""
    repo = _repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.B,
        raw=_unexecutable_report(verdict, result="supported"),
        now=NOW,
    )


def test_unexecutable_declaration_parses_and_requires_a_reason():
    from pre_pr_tribunal.policy import _validate_config

    config = _validate_config(b'[unexecutable]\n"src/**" = "NO_CROSS_SDK"\n')
    assert config.unexecutable == (("src/**", "NO_CROSS_SDK"),)
    for invalid in (b'[unexecutable]\n"src/**" = ""\n', b'[unexecutable]\n"src/**" = 1\n'):
        with pytest.raises(SchemaError) as error:
            _validate_config(invalid)
        assert error.value.code == "CONFIG_INVALID"


def test_unexecutable_rejections_are_retryable():
    from pre_pr_tribunal.attempt_store import REPORT_RETRYABLE_CODES

    assert "UNEXECUTABLE_PATH_NOT_UNVERIFIED" in REPORT_RETRYABLE_CODES
    assert "UNEXECUTABLE_PATH_UNCOVERED" in REPORT_RETRYABLE_CODES


def _rename_repo(tmp_path, *, old="src/app.py", new="lib/app.py"):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "feature")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", "https://github.com/jhw7500/claude-config.git")
    _write(repo, ".gitignore", ".review/\n")
    _write(repo, old, "base\n")
    _write(repo, ".pre-pr-tribunal.toml", UNEXECUTABLE_CONFIG)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    (repo / new).parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", old, new)
    _git(repo, "commit", "-qm", "rename")
    return repo


def test_declared_unexecutable_path_cannot_be_claimed_refuted(tmp_path):
    """`refuted` demands execution evidence exactly as `supported` does.

    Regression for the round-1 CRITICAL: permitting `refuted` sealed the report
    and finalized to PASS, because gate aggregation only downgrades on
    `unverified`.
    """
    repo = _repo(tmp_path, config=UNEXECUTABLE_CONFIG)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    with pytest.raises(SchemaError) as error:
        submit_reviewer_report(
            repo,
            reviewer=Reviewer.B,
            raw=_unexecutable_report(verdict, result="refuted"),
            now=NOW,
        )
    assert error.value.code == "UNEXECUTABLE_PATH_NOT_UNVERIFIED"


def test_declared_unexecutable_rename_accepts_either_side(tmp_path):
    """A rename matching on the old side may be covered by the new path."""
    repo = _rename_repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.B,
        raw=_unexecutable_report(verdict, result="unverified", path="lib/app.py"),
        now=NOW,
    )


def test_declared_unexecutable_rename_still_accepts_old_side(tmp_path):
    repo = _rename_repo(tmp_path)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(
        repo,
        reviewer=Reviewer.B,
        raw=_unexecutable_report(verdict, result="unverified", path="src/app.py"),
        now=NOW,
    )


UNEXECUTABLE_B_DISABLED = (
    '[unexecutable]\n"src/**" = "NO_CROSS_SDK"\n\n[reviewer.B]\nenabled = false\n'
)


def test_unexecutable_declaration_forces_reviewer_b(tmp_path):
    """A declaration is enforced only through B, so disabling B must not void it.

    Regression for tribunal round-2 finding A-R2-001: with B inactive,
    `_validate_unexecutable_claims` never runs and the gate finalizes to PASS.
    """
    repo = _repo(tmp_path, config=UNEXECUTABLE_B_DISABLED)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert "B" in verdict.policy.active_reviewers
    assert verdict.policy.reviewers["B"].enabled is True
    assert "unexecutable-requires-reviewer-b" in verdict.policy.reasons


UNEXECUTABLE_UNMATCHED_B_DISABLED = (
    '[unexecutable]\n"drivers/**" = "NO_CROSS_SDK"\n\n[reviewer.B]\nenabled = false\n'
)


def test_unexecutable_forcing_is_config_conditioned_not_diff_conditioned(tmp_path):
    """Forcing depends on the committed config alone, never on the changed paths.

    Round transitions reject a changed reviewer set (`POLICY_CHANGED`), and a
    declared path may legitimately disappear between rounds because auto-fix
    scope may shrink. Keying the decision to the config keeps it stable.

    The declared pattern and the changed path are deliberately disjoint while
    the mode stays non-`off`. An earlier version of this test used a
    documentation-only change, which is risk floor 0 and therefore `off`, so it
    conflated "the diff does not match" with "no reviewer runs at all" and
    pinned the A-R1-001 behaviour by accident. The mode assertion keeps the two
    axes separate.
    """
    repo = _repo(tmp_path, config=UNEXECUTABLE_UNMATCHED_B_DISABLED)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is not ReviewMode.OFF
    assert not any(path.startswith("drivers/") for path in verdict.initial_paths)
    assert verdict.policy.reviewers["B"].enabled is True
    assert "unexecutable-requires-reviewer-b" in verdict.policy.reasons


def test_reviewer_b_stays_disabled_without_a_declaration(tmp_path):
    """Control: the forcing is caused by the declaration, nothing else."""
    repo = _repo(tmp_path, config='[reviewer.B]\nenabled = false\n')
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.reviewers["B"].enabled is False
    assert "B" not in verdict.policy.active_reviewers
    assert "unexecutable-requires-reviewer-b" not in verdict.policy.reasons


def test_unexecutable_reason_is_recorded_even_when_b_was_already_enabled(tmp_path):
    """The override reason records the declaration, not a change to the config.

    Regression for tribunal claim B-R1-C003: the reason used to be appended only
    when the config had disabled B, so the common case recorded nothing while the
    design note claimed it always did.
    """
    repo = _repo(tmp_path, config=UNEXECUTABLE_CONFIG)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.reviewers["B"].enabled is True
    assert "unexecutable-requires-reviewer-b" in verdict.policy.reasons


def test_unexecutable_reason_survives_the_policy_reason_cap(tmp_path):
    """The override reason must outlive truncation on a wide change.

    Regression for tribunal finding A-R1-001: the reason was appended last, so
    `MAX_POLICY_REASONS` discarded it first — exactly on the large changes most
    likely to touch a declared path.
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "feature")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", "https://github.com/jhw7500/claude-config.git")
    _write(repo, ".gitignore", ".review/\n")
    _write(repo, ".pre-pr-tribunal.toml", UNEXECUTABLE_CONFIG)
    for index in range(130):
        _write(repo, f"src/mod{index}.py", "base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    for index in range(130):
        _write(repo, f"src/mod{index}.py", "feature\n")
    _git(repo, "commit", "-qam", "feature")

    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert len(verdict.policy.reasons) == MAX_POLICY_REASONS
    assert verdict.policy.reasons[-1] == "reason-limit"
    assert "unexecutable-requires-reviewer-b" in verdict.policy.reasons
    assert verdict.policy.reviewers["B"].enabled is True


def test_off_mode_records_no_unexecutable_override(tmp_path):
    """`off` dispatches no reviewer, so no override may be claimed.

    Regression for tribunal round-1 finding A-R1-001: the forcing ran before the
    mode was consulted, so an `off` run persisted `reviewers.B.enabled` and the
    override reason while `active_reviewers` was empty and every slot disabled --
    an audit record asserting a security override that never happened.
    """
    repo = _repo(tmp_path, path="docs/guide.md", config=UNEXECUTABLE_B_DISABLED)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is ReviewMode.OFF
    assert "unexecutable-requires-reviewer-b" not in verdict.policy.reasons
    assert verdict.policy.reviewers["B"].enabled is False


def _review_tree_digest(repo):
    review = repo / ".review"
    if not review.exists():
        return None
    digest = hashlib.sha256()
    for path in sorted(review.rglob("*")):
        digest.update(str(path.relative_to(review)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _preview(repo, monkeypatch, capsys, *extra):
    from pre_pr_tribunal.cli import main

    monkeypatch.chdir(repo)
    status = main(["policy-preview", "--base", "master", "--runtime", "codex", *extra])
    captured = capsys.readouterr()
    return status, captured


@pytest.mark.parametrize(
    ("score", "mode"),
    (
        (0, ReviewMode.OFF),
        (1, ReviewMode.SINGLE),
        (66, ReviewMode.SINGLE),
        (67, ReviewMode.ITERATIVE),
        (100, ReviewMode.ITERATIVE),
    ),
)
def test_compatibility_intensity_scores_map_to_explicit_modes(score, mode):
    assert intensity_mode(score) is mode


@pytest.mark.parametrize(
    ("path", "mode"),
    (
        ("docs/guide.md", ReviewMode.OFF),
        ("src/app.py", ReviewMode.SINGLE),
        ("hooks/guard.py", ReviewMode.ITERATIVE),
    ),
)
def test_policy_preview_matches_begin_without_creating_review_state(
    tmp_path, monkeypatch, capsys, path, mode
):
    repo = _repo(tmp_path, path=path)
    status, captured = _preview(repo, monkeypatch, capsys)
    assert status == 0, captured.err
    assert not (repo / ".review").exists()
    payload = json.loads(captured.out)
    assert payload["round_authority"] is False
    assert payload["begin_admissible"] == "not-evaluated"
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is mode
    assert payload["policy"]["risk_floor"] == verdict.policy.risk_floor
    assert payload["policy"]["effective_intensity"] == verdict.policy.effective_intensity
    assert payload["policy"]["mode"] == verdict.policy.mode.value
    assert payload["policy"] == verdict.policy.to_json()
    assert payload["active_reviewers"] == list(verdict.policy.active_reviewers)
    assert payload["snapshot"]["head_sha"] == verdict.head_sha
    assert payload["snapshot"]["diff_sha256"] == verdict.diff_sha256


def test_policy_preview_passes_the_same_intensity_request_as_begin(
    tmp_path, monkeypatch, capsys
):
    repo = _repo(tmp_path)
    request = ("--intensity", "100", "--intensity-requester", "maintainer",
               "--intensity-reason", "wider review")
    status, captured = _preview(repo, monkeypatch, capsys, *request)
    assert status == 0, captured.err
    payload = json.loads(captured.out)
    verdict = begin_round(
        repo, base="master", runtime="codex", round_number=1, now=NOW,
        intensity_values=["100"], intensity_requester=["maintainer"],
        intensity_reason=["wider review"],
    )
    assert verdict.policy.mode is ReviewMode.ITERATIVE
    assert payload["policy"]["risk_floor"] == 50
    assert payload["policy"]["effective_intensity"] == 100
    assert payload["policy"]["mode"] == "iterative"
    assert payload["policy"] == verdict.policy.to_json()


def test_policy_preview_leaves_existing_review_state_byte_identical(
    tmp_path, monkeypatch, capsys
):
    from pre_pr_tribunal.cli import main

    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    assert main(["begin", "--base", "master", "--runtime", "codex", "--round", "1"]) == 0
    begin_payload = json.loads(capsys.readouterr().out)
    assert begin_payload["policy"]["risk_floor"] == 50
    assert begin_payload["policy"]["effective_intensity"] == 50
    assert begin_payload["policy"]["mode"] == "single"
    before = _review_tree_digest(repo)
    assert before is not None
    assert (repo / ".review" / "telemetry.json").is_file()
    assert (repo / ".review" / "verdict.json").is_file()
    status, captured = _preview(repo, monkeypatch, capsys)
    assert status == 0, captured.err
    assert _review_tree_digest(repo) == before


def test_policy_preview_refuses_what_begin_refuses_before_policy(
    tmp_path, monkeypatch, capsys
):
    repo = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    status, captured = _preview(repo, monkeypatch, capsys)
    assert status == 1
    assert captured.err == "PRE_PR_TRIBUNAL:EMPTY_DIFF\n"
    assert not (repo / ".review").exists()
    with pytest.raises(GitStateError, match="^EMPTY_DIFF$"):
        begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)


def _grant(repo, value, reason="lower a reviewed-by-hand change"):
    from pre_pr_tribunal.intensity_grant import record_grant
    from pre_pr_tribunal.verdict_store import preview_policy

    snapshot, policy = preview_policy(repo, base="master", runtime="codex", now=NOW)
    return record_grant(
        repo, snapshot=snapshot, policy=policy, value=value, reason=reason, now=NOW
    )


def test_matching_human_grant_lowers_below_the_floor(tmp_path):
    repo = _repo(tmp_path, path="hooks/guard.py")
    _grant(repo, 50)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    policy = verdict.policy
    assert (policy.risk_floor, policy.effective_intensity) == (100, 50)
    assert policy.mode is ReviewMode.SINGLE
    assert (policy.request.source, policy.request.requester) == ("human_grant", "human-direct")
    assert parse_policy_binding(policy.to_json(), runtime="codex") == policy


def test_human_grant_to_zero_is_skipped_with_its_provenance(tmp_path):
    repo = _repo(tmp_path, path="hooks/guard.py")
    _grant(repo, 0)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is ReviewMode.OFF
    assert verdict.gate.status is GateStatus.SKIPPED
    assert verdict.policy.risk_floor == 100
    assert verdict.policy.request.source == "human_grant"
    assert evaluate_gate(repo, BOUND_COMMAND).code is GateCode.PASS


def test_human_grant_for_another_snapshot_is_ignored(tmp_path):
    repo = _repo(tmp_path, path="hooks/guard.py")
    _grant(repo, 0)
    _write(repo, "hooks/guard.py", "changed after the grant\n")
    _git(repo, "commit", "-qam", "change")
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is ReviewMode.ITERATIVE
    assert verdict.policy.request.source == "default"


def test_controller_intensity_conflicts_with_a_matching_grant(tmp_path):
    repo = _repo(tmp_path, path="hooks/guard.py")
    _grant(repo, 50)
    with pytest.raises(SchemaError, match="^INTENSITY_GRANT_CONFLICT$"):
        begin_round(
            repo, base="master", runtime="codex", round_number=1, now=NOW,
            intensity_values=("100",), intensity_requester="maintainer",
            intensity_reason="wider review",
        )


def test_grant_must_lower_the_floor_and_change_the_mode(tmp_path):
    repo = _repo(tmp_path, path="hooks/guard.py")
    with pytest.raises(SchemaError, match="^GRANT_MODE_UNCHANGED$"):
        _grant(repo, 80)
    docs = _repo(tmp_path / "docs", path="docs/guide.md")
    with pytest.raises(SchemaError, match="^GRANT_NOT_LOWER$"):
        _grant(docs, 50)
    assert not (repo / ".review").exists()


def test_lowered_policy_is_valid_only_for_a_human_grant(tmp_path):
    repo = _repo(tmp_path, path="hooks/guard.py")
    _grant(repo, 50)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    forged = verdict.policy.to_json()
    forged["request"] = {**forged["request"], "source": "cli", "requester": "maintainer"}
    with pytest.raises(SchemaError, match="^POLICY_INVALID$"):
        parse_policy_binding(forged, runtime="codex")
    wrong_requester = verdict.policy.to_json()
    wrong_requester["request"] = {**wrong_requester["request"], "requester": "agent"}
    with pytest.raises(SchemaError, match="^POLICY_INVALID$"):
        parse_policy_binding(wrong_requester, runtime="codex")


def test_human_grant_may_restart_an_inconclusive_snapshot_lower(tmp_path):
    repo = _repo(tmp_path, path="hooks/guard.py")
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    submit_reviewer_report(repo, reviewer=Reviewer.A, raw=_report(verdict, "A"), now=NOW)
    claim = {
        "id": "B-R1-C001",
        "statement": "The hook entry path runs.",
        "result": "unverified",
        "execution_ids": [],
        "reason": "The required runtime is unavailable.",
    }
    submit_reviewer_report(
        repo, reviewer=Reviewer.B, raw=_report(verdict, "B", claims=(claim,)), now=NOW
    )
    assert finalize_round(repo, now=NOW).gate.status is GateStatus.INCONCLUSIVE
    _grant(repo, 50)
    _authorize_next_round(repo)
    restarted = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert restarted.policy.effective_intensity == 50
    assert restarted.policy.request.source == "human_grant"


class _FakeTty:
    def __init__(self, lines):
        self._lines = list(lines)

    def isatty(self):
        return True

    def readline(self):
        return self._lines.pop(0) if self._lines else ""


def test_intensity_grant_cli_requires_a_terminal(tmp_path, monkeypatch, capsys):
    from pre_pr_tribunal.cli import main

    repo = _repo(tmp_path, path="hooks/guard.py")
    monkeypatch.chdir(repo)
    status = main(["intensity-grant", "--base", "master", "--runtime", "codex",
                   "--intensity", "50", "--reason", "small change"])
    assert status == 1
    assert capsys.readouterr().err == "PRE_PR_TRIBUNAL:GRANT_TTY_REQUIRED\n"
    assert not (repo / ".review").exists()


def test_intensity_grant_cli_records_only_after_typed_confirmation(
    tmp_path, monkeypatch, capsys
):
    import sys as _sys
    from pre_pr_tribunal.cli import main

    repo = _repo(tmp_path, path="hooks/guard.py")
    arguments = ["intensity-grant", "--base", "master", "--runtime", "codex",
                 "--intensity", "50", "--reason", "small change"]
    monkeypatch.chdir(repo)
    monkeypatch.setattr(_sys, "stdin", _FakeTty(["nope\n"]))
    assert main(arguments) == 1
    assert capsys.readouterr().err.endswith("PRE_PR_TRIBUNAL:GRANT_CONFIRMATION_MISMATCH\n")
    assert not (repo / ".review" / "intensity-grant.json").exists()

    monkeypatch.setattr(_sys, "stdin", _FakeTty(["lower\n"]))
    assert main(arguments) == 0
    grant_prompt = capsys.readouterr().err
    assert "risk_floor: 100  floor mode: iterative" in grant_prompt
    assert "current effective_intensity: 100  current mode: iterative" in grant_prompt
    assert "proposed effective_intensity: 50  resulting mode: single" in grant_prompt
    assert "mode transition from floor: iterative -> single" in grant_prompt
    assert "0=off, 1-66=single, 67-100=iterative" in grant_prompt
    assert "off skips review" in grant_prompt
    assert "single runs one decision round" in grant_prompt
    assert "iterative allows up to three decision rounds" in grant_prompt
    assert "scores within a mode do not change review depth or reviewer selection" in grant_prompt
    assert "numeric scores can still affect policy floors" in grant_prompt
    assert ((repo / ".review" / "intensity-grant.json").stat().st_mode & 0o777) == 0o600
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.request.source == "human_grant"
    assert verdict.policy.effective_intensity == 50


def test_relayed_grant_instruction_explains_each_mode_before_choice():
    skill = (
        Path(__file__).resolve().parents[2] / "skills/pre-pr-tribunal/SKILL.md"
    ).read_text(encoding="utf-8")
    question_instruction = skill.split("4. Generate one projection", 1)[0]
    assert "Before presenting any lower-mode choice" in question_instruction
    assert "`off` skips review" in question_instruction
    assert "`single` runs one decision round" in question_instruction
    assert "`iterative` allows up to three decision rounds" in question_instruction


@pytest.mark.parametrize(
    "tamper",
    (
        lambda grant: {**grant, "reason": ""},
        lambda grant: {**grant, "reason": "x" * 2048},
        lambda grant: {**grant, "reason": "bell\u0007"},
        lambda grant: {**grant, "value": 80},
        lambda grant: {**grant, "value": True},
        None,
    ),
    ids=("empty-reason", "long-reason", "control-reason", "mode-unchanged", "bool-value", "deep-nesting"),
)
def test_malformed_grant_is_ignored_and_state_stays_readable(tmp_path, tamper):
    from pre_pr_tribunal.verdict_store import read_verdict

    repo = _repo(tmp_path, path="hooks/guard.py")
    _grant(repo, 0)
    path = repo / ".review" / "intensity-grant.json"
    raw = (
        b"[" * 5000
        if tamper is None
        else json.dumps(tamper(json.loads(path.read_bytes()))).encode()
    )
    path.write_bytes(raw)
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is ReviewMode.ITERATIVE
    assert verdict.policy.request.source == "default"
    assert read_verdict(repo).policy == verdict.policy


def _relayed(repo, monkeypatch, capsys, *extra):
    from pre_pr_tribunal.cli import main

    monkeypatch.chdir(repo)
    status = main(["intensity-grant", "--base", "master", "--runtime", "codex",
                   "--intensity", "50", "--reason", "user chose single in the prompt",
                   "--relayed", *extra])
    return status, capsys.readouterr()


def test_relayed_grant_needs_no_terminal_and_records_its_channel(
    tmp_path, monkeypatch, capsys
):
    repo = _repo(tmp_path, path="hooks/guard.py")
    status, captured = _relayed(repo, monkeypatch, capsys)
    assert status == 0, captured.err
    assert json.loads(captured.out)["channel"] == "relayed"
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    policy = verdict.policy
    assert (policy.request.source, policy.request.requester) == ("human_grant", "human-relayed")
    assert (policy.risk_floor, policy.effective_intensity) == (100, 50)
    assert parse_policy_binding(policy.to_json(), runtime="codex") == policy


def test_relayed_grant_binds_the_snapshot_it_was_recorded_for(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, path="hooks/guard.py")
    assert _relayed(repo, monkeypatch, capsys)[0] == 0
    _write(repo, "hooks/guard.py", "changed after the user answered\n")
    _git(repo, "commit", "-qam", "change")
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is ReviewMode.ITERATIVE
    assert verdict.policy.request.source == "default"


def test_human_grant_accepts_only_direct_or_relayed_requesters(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, path="hooks/guard.py")
    assert _relayed(repo, monkeypatch, capsys)[0] == 0
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    for requester, valid in (("human-direct", True), ("human-relayed", True), ("agent", False)):
        candidate = verdict.policy.to_json()
        candidate["request"] = {**candidate["request"], "requester": requester}
        if valid:
            assert parse_policy_binding(candidate, runtime="codex").request.requester == requester
        else:
            with pytest.raises(SchemaError, match="^POLICY_INVALID$"):
                parse_policy_binding(candidate, runtime="codex")


def test_revoke_keeps_the_floor_over_an_earlier_grant(tmp_path, monkeypatch, capsys):
    from pre_pr_tribunal.cli import main

    repo = _repo(tmp_path, path="hooks/guard.py")
    _grant(repo, 0)
    monkeypatch.chdir(repo)
    assert main(["intensity-grant", "--base", "master", "--runtime", "codex", "--revoke"]) == 0
    capsys.readouterr()
    assert not (repo / ".review" / "intensity-grant.json").exists()
    verdict = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert verdict.policy.mode is ReviewMode.ITERATIVE
    assert verdict.policy.request.source == "default"


def test_revoke_without_a_grant_is_a_no_op_and_rejects_value_flags(
    tmp_path, monkeypatch, capsys
):
    from pre_pr_tribunal.cli import main

    repo = _repo(tmp_path, path="hooks/guard.py")
    monkeypatch.chdir(repo)
    base = ["intensity-grant", "--base", "master", "--runtime", "codex", "--revoke"]
    assert main(base) == 0
    assert json.loads(capsys.readouterr().out) == {"revoked": False}
    assert main([*base, "--intensity", "50"]) == 1
    assert capsys.readouterr().err == "PRE_PR_TRIBUNAL:GRANT_INVALID\n"
    assert main(["intensity-grant", "--base", "master", "--runtime", "codex",
                 "--relayed", "--reason", "no value"]) == 1
    assert capsys.readouterr().err == "PRE_PR_TRIBUNAL:GRANT_INVALID\n"
