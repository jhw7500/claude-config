import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from pre_pr_tribunal.cli import _status
from pre_pr_tribunal.gate import GateCode, evaluate_gate
from pre_pr_tribunal.model import GateStatus, ReviewMode, Reviewer, SchemaError
from pre_pr_tribunal.policy import parse_policy_binding
from pre_pr_tribunal.verdict_store import (
    begin_round,
    finalize_round,
    submit_reviewer_report,
)


NOW = lambda: "2026-09-17T00:00:00Z"
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


def _report(verdict, reviewer, *, findings=(), claims=(), executions=()):
    return json.dumps(
        {
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
        },
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


def test_active_only_finalize_and_unverified_is_inconclusive(tmp_path):
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
        "reason": "The required SDK is unavailable.",
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
    _write(repo, "src/app.py", "fixed\n")
    _git(repo, "commit", "-qam", "fix")
    restarted = begin_round(
        repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    assert restarted.round == 1
    assert restarted.head_sha != verdict.head_sha


def test_same_snapshot_can_be_re_requested_at_higher_intensity(tmp_path):
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
    elevated = begin_round(
        repo,
        base="master",
        runtime="codex",
        round_number=1,
        intensity_values=("80",),
        intensity_requester="maintainer",
        intensity_reason="escalate after blocker",
        now=NOW,
    )
    assert elevated.head_sha == verdict.head_sha
    assert elevated.policy.mode is ReviewMode.ITERATIVE
    assert elevated.policy.request.value == 80


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
