import hashlib
import json
import stat
import subprocess

import pytest

from pre_pr_tribunal import auto_run, round_grant
from pre_pr_tribunal import cli
from pre_pr_tribunal.model import Reviewer, SchemaError
from pre_pr_tribunal.verdict_store import (
    begin_round,
    finalize_round,
    read_verdict,
    submit_reviewer_report,
)
from tests.pre_pr_tribunal.test_model_store import (
    NOW,
    execution,
    finding,
    honest_captures,
    report,
    write_json,
)


def _submit_round(repo, snapshot, *, round_number, fail, unverified=False, a_execution=False):
    for reviewer in "ABC":
        findings = ()
        executions = ()
        if fail and reviewer == "A":
            execution_ids = (f"A-R{round_number}-E001",) if a_execution else ()
            findings = (finding(f"A-R{round_number}-001", execution_ids=execution_ids),)
            if a_execution:
                executions = (execution(f"A-R{round_number}-E001"),)
        prior = ()
        if round_number > 1 and reviewer == "A":
            prior = ({
                "decision_id": f"D-R{round_number - 1}-A-001",
                "outcome": "accepted",
                "replacement_finding_id": None,
            },)
        claims = ()
        if reviewer == "B" and unverified:
            claims = ({
                "id": f"B-R{round_number}-C001",
                "statement": "The required environment was unavailable.",
                "result": "unverified",
                "execution_ids": [],
                "reason": "UNAVAILABLE: the test target was unavailable.",
            },)
        raw = json.dumps(report(
            snapshot,
            reviewer,
            round_number=round_number,
            findings=findings,
            executions=executions,
            claims=claims,
            prior_decisions=prior,
        )).encode()
        captures = honest_captures(repo, raw) if reviewer == "B" else None
        submit_reviewer_report(
            repo,
            reviewer=Reviewer(reviewer),
            raw=raw,
            now=NOW,
            captures=captures,
        )
    return finalize_round(repo, now=NOW)


def _commit(repo, text):
    path = repo / "tracked.txt"
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")
    subprocess.run(["/usr/bin/git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(repo), "commit", "-qm", text], check=True)
    return subprocess.check_output(
        ["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()


def _grant(repo, runtime="codex"):
    preview = auto_run.preview(repo, base="master", runtime=runtime, now=NOW)
    return auto_run.record_grant(
        repo,
        base="master",
        runtime=runtime,
        expected_binding_sha256=preview["binding_sha256"],
        choice="100 자동",
        reason="세 라운드 자동 수정과 재심사를 승인",
        channel="relayed",
        now=NOW,
        token_hex=lambda _size: "1" * 32,
    )


@pytest.mark.parametrize("choice", ("100", "proceed", "자동", "100%"))
def test_grant_requires_unambiguous_auto_run_choice(git_repo, choice):
    preview = auto_run.preview(git_repo, base="master", runtime="codex", now=NOW)
    with pytest.raises(SchemaError, match="^AUTO_RUN_CONSENT_REQUIRED$"):
        auto_run.record_grant(
            git_repo,
            base="master",
            runtime="codex",
            expected_binding_sha256=preview["binding_sha256"],
            choice=choice,
            reason="test",
            channel="relayed",
            now=NOW,
        )


def test_auto_consent_raises_lower_floor_to_exactly_100(git_repo):
    config = git_repo / ".pre-pr-tribunal.toml"
    config.write_text(config.read_text(encoding="utf-8").replace('"iterative"', '"single"'), encoding="utf-8")
    subprocess.run(["/usr/bin/git", "-C", str(git_repo), "commit", "-qam", "lower floor"], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "update-ref", "refs/remotes/origin/master", "HEAD"],
        check=True,
    )
    (git_repo / "feature.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "-C", str(git_repo), "add", "feature.py"], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(git_repo), "commit", "-qm", "feature at lower floor"], check=True)
    preview = auto_run.preview(git_repo, base="master", runtime="codex", now=NOW)
    assert preview["policy"]["risk_floor"] < 100
    assert preview["policy"]["effective_intensity"] == 100
    grant = auto_run.record_grant(
        git_repo, base="master", runtime="codex",
        expected_binding_sha256=preview["binding_sha256"], choice="100 자동",
        reason="explicit bounded auto run", channel="relayed", now=NOW,
        token_hex=lambda _size: "2" * 32,
    )
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1,
        auto_run_id=grant["id"], expected_head=preview["snapshot"]["head_sha"], now=NOW,
    )
    assert pending.policy.effective_intensity == 100
    assert pending.policy.mode.value == "iterative"


def test_grant_is_private_and_binds_preview(git_repo):
    grant = _grant(git_repo)
    path = git_repo / ".review/auto-runs.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert grant["id"] == "1" * 32
    assert grant["max_rounds"] == 3
    assert grant["status"] == "granted"
    assert grant["binding"]["snapshot"]["head_sha"] == subprocess.check_output(
        ["/usr/bin/git", "-C", str(git_repo), "rev-parse", "HEAD"], text=True
    ).strip()

    changed = _commit(git_repo, "stale preview")
    assert changed != grant["binding"]["snapshot"]["head_sha"]
    with pytest.raises(SchemaError, match="^AUTO_RUN_BINDING_CHANGED$"):
        auto_run.record_grant(
            git_repo,
            base="master",
            runtime="codex",
            expected_binding_sha256=grant["binding_sha256"],
            choice="100 자동",
            reason="stale",
            channel="relayed",
            now=NOW,
        )


def test_active_grant_cannot_fall_through_to_manual_begin(git_repo):
    _grant(git_repo)
    with pytest.raises(SchemaError, match="^AUTO_RUN_ID_REQUIRED$"):
        begin_round(
            git_repo, base="master", runtime="codex", round_number=1, now=NOW
        )


def test_missing_active_store_cannot_fall_through_to_manual_begin(git_repo):
    _grant(git_repo)
    (git_repo / ".review/auto-runs.json").unlink()
    with pytest.raises(SchemaError, match="^AUTO_RUN_ID_REQUIRED$"):
        begin_round(
            git_repo, base="master", runtime="codex", round_number=1, now=NOW
        )


def test_missing_store_during_pending_round_fails_finalize_closed(git_repo):
    grant = _grant(git_repo)
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1,
        auto_run_id=grant["id"], expected_head=grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )
    (git_repo / ".review/auto-runs.json").unlink()
    with pytest.raises(SchemaError, match="^AUTO_RUN_STATE_INCONSISTENT$"):
        _submit_round(git_repo, first.snapshot, round_number=1, fail=False)
    assert (git_repo / ".review/auto-run-active.json").exists()


def test_missing_auto_store_cannot_consume_stale_manual_re_review_grant(git_repo):
    grant = _grant(git_repo)
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1,
        auto_run_id=grant["id"], expected_head=grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )
    _submit_round(git_repo, first.snapshot, round_number=1, fail=True)
    _commit(git_repo, "fix")
    decisions = write_json(
        git_repo / ".review/inbox/round-1/decisions.json",
        [{
            "id": "D-R1-A-001",
            "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
            "disposition": "fixed",
            "rationale": "Covered by a new regression test.",
            "executions": [execution("D-R1-E001")],
        }],
    )
    preview = round_grant.preview_grant_binding(
        git_repo, base="master", runtime="codex", round_number=2,
        decisions_path=decisions, now=NOW,
    )
    round_grant.record_grant(
        git_repo, base="master", runtime="codex", round_number=2,
        decisions_path=decisions,
        expected_verdict_sha256=preview["verdict_sha256"],
        expected_binding_sha256=preview["binding_sha256"],
        reason="stale manual grant", channel="relayed", now=NOW,
    )
    (git_repo / ".review/auto-runs.json").unlink()
    with pytest.raises(SchemaError, match="^AUTO_RUN_ID_REQUIRED$"):
        begin_round(
            git_repo, base="master", runtime="codex", round_number=2,
            decisions_path=decisions, now=NOW,
        )


def test_cli_preview_grant_status_and_stop(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    assert cli.main(["auto-run-preview", "--base", "master", "--runtime", "codex"], wall_clock=NOW) == 0
    preview = json.loads(capsys.readouterr().out)
    assert cli.main([
        "auto-run-grant", "--base", "master", "--runtime", "codex",
        "--binding-sha256", preview["binding_sha256"],
        "--choice", "100 자동", "--reason", "bounded auto run", "--relayed",
    ], wall_clock=NOW) == 0
    grant = json.loads(capsys.readouterr().out)
    assert cli.main(["auto-run-status", "--auto-run", grant["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "granted"
    assert cli.main([
        "auto-run-stop", "--auto-run", grant["id"], "--reason", "USER_STOPPED"
    ], wall_clock=NOW) == 0
    assert json.loads(capsys.readouterr().out)["stop_reason"] == "USER_STOPPED"


@pytest.mark.parametrize("runtime", ("claude", "codex"))
def test_auto_run_fail_fix_pass_lifecycle(git_repo, runtime):
    grant = _grant(git_repo, runtime)
    first = begin_round(
        git_repo,
        base="master",
        runtime=runtime,
        round_number=1,
        auto_run_id=grant["id"],
        expected_head=grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )
    failed = _submit_round(git_repo, first.snapshot, round_number=1, fail=True)
    assert failed.gate.status.value == "fail"
    assert auto_run.status(git_repo, auto_run_id=grant["id"])["status"] == "awaiting_fix"
    archive = git_repo / f".review/auto-run-archive/{grant['id']}/round-1"
    assert (archive / "verdict.json").read_bytes() == (
        git_repo / ".review/verdict.json"
    ).read_bytes()
    assert all(stat.S_IMODE((archive / f"{key}.json").stat().st_mode) == 0o600 for key in "ABC")

    fix = _commit(git_repo, "fix one")
    decisions = write_json(
        git_repo / ".review/inbox/round-1/decisions.json",
        [{
            "id": "D-R1-A-001",
            "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
            "disposition": "fixed",
            "rationale": "Covered by a new regression test.",
            "executions": [execution("D-R1-E001")],
        }],
    )
    second = begin_round(
        git_repo,
        base="master",
        runtime=runtime,
        round_number=2,
        decisions_path=decisions,
        auto_run_id=grant["id"],
        expected_head=fix,
        fix_commits=(fix,),
        now=NOW,
    )
    assert (archive / "decisions.json").read_bytes() == decisions.read_bytes()
    passed = _submit_round(git_repo, second.snapshot, round_number=2, fail=False)
    assert passed.gate.status.value == "pass"
    state = auto_run.status(git_repo, auto_run_id=grant["id"])
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "PASS"
    assert [item["round"] for item in state["rounds"]] == [1, 2]
    assert state["rounds"][1]["fix_commits"] == [fix]
    assert state["rounds"][0]["verdict_sha256"] == hashlib.sha256(
        json.dumps(failed.to_json(), ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def test_revoked_or_foreign_fix_commit_fails_closed(git_repo):
    grant = _grant(git_repo)
    first = begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=1,
        auto_run_id=grant["id"],
        expected_head=grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )
    _submit_round(git_repo, first.snapshot, round_number=1, fail=True)
    fix = _commit(git_repo, "fix")
    decisions = write_json(
        git_repo / ".review/inbox/round-1/decisions.json",
        [{
            "id": "D-R1-A-001",
            "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
            "disposition": "fixed",
            "rationale": "Covered by a new regression test.",
            "executions": [execution("D-R1-E001")],
        }],
    )
    with pytest.raises(SchemaError, match="^AUTO_RUN_FIX_COMMITS_CHANGED$"):
        begin_round(
            git_repo,
            base="master",
            runtime="codex",
            round_number=2,
            decisions_path=decisions,
            auto_run_id=grant["id"],
            expected_head=fix,
            fix_commits=(),
            now=NOW,
        )
    with pytest.raises(SchemaError, match="^AUTO_RUN_EXPECTED_HEAD_CHANGED$"):
        begin_round(
            git_repo,
            base="master",
            runtime="codex",
            round_number=2,
            decisions_path=decisions,
            auto_run_id=grant["id"],
            expected_head=first.head_sha,
            fix_commits=(fix,),
            now=NOW,
        )
    auto_run.stop(git_repo, auto_run_id=grant["id"], reason="USER_STOPPED", now=NOW)
    with pytest.raises(SchemaError, match="^AUTO_RUN_STOPPED$"):
        begin_round(
            git_repo,
            base="master",
            runtime="codex",
            round_number=2,
            decisions_path=decisions,
            auto_run_id=grant["id"],
            expected_head=fix,
            fix_commits=(fix,),
            now=NOW,
        )


def test_fail_with_unverified_evidence_stops_instead_of_continuing(git_repo):
    grant = _grant(git_repo)
    first = begin_round(
        git_repo,
        base="master",
        runtime="codex",
        round_number=1,
        auto_run_id=grant["id"],
        expected_head=grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )
    final = _submit_round(
        git_repo, first.snapshot, round_number=1, fail=True, unverified=True
    )
    assert final.gate.status.value == "fail"
    state = auto_run.status(git_repo, auto_run_id=grant["id"])
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "INCONCLUSIVE_EVIDENCE"


def test_reserved_transition_is_not_retried_after_verdict_write_crash(git_repo, monkeypatch):
    from pre_pr_tribunal import verdict_store

    grant = _grant(git_repo)
    original = verdict_store._atomic_write

    def fail_write(*_args, **_kwargs):
        raise SchemaError("VERDICT_WRITE_FAILED")

    monkeypatch.setattr(verdict_store, "_atomic_write", fail_write)
    with pytest.raises(SchemaError, match="^VERDICT_WRITE_FAILED$"):
        begin_round(
            git_repo,
            base="master",
            runtime="codex",
            round_number=1,
            auto_run_id=grant["id"],
            expected_head=grant["binding"]["snapshot"]["head_sha"],
            now=NOW,
        )
    monkeypatch.setattr(verdict_store, "_atomic_write", original)
    with pytest.raises(SchemaError, match="^AUTO_RUN_STATE_INCONSISTENT$"):
        begin_round(
            git_repo,
            base="master",
            runtime="codex",
            round_number=1,
            auto_run_id=grant["id"],
            expected_head=grant["binding"]["snapshot"]["head_sha"],
            now=NOW,
        )


def test_each_fix_commit_must_stay_within_initial_scope(git_repo):
    grant = _grant(git_repo)
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1,
        auto_run_id=grant["id"], expected_head=grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )
    _submit_round(git_repo, first.snapshot, round_number=1, fail=True)
    (git_repo / "foreign.txt").write_text("temporary\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "-C", str(git_repo), "add", "foreign.txt"], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(git_repo), "commit", "-qm", "foreign"], check=True)
    foreign = subprocess.check_output(
        ["/usr/bin/git", "-C", str(git_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    (git_repo / "foreign.txt").unlink()
    subprocess.run(["/usr/bin/git", "-C", str(git_repo), "add", "-u"], check=True)
    fix = _commit(git_repo, "fix after foreign")
    decisions = write_json(
        git_repo / ".review/inbox/round-1/decisions.json",
        [{
            "id": "D-R1-A-001",
            "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
            "disposition": "fixed",
            "rationale": "Covered by a new regression test.",
            "executions": [execution("D-R1-E001")],
        }],
    )
    with pytest.raises(SchemaError, match="^AUTO_RUN_FIX_SCOPE_VIOLATION$"):
        begin_round(
            git_repo, base="master", runtime="codex", round_number=2,
            decisions_path=decisions, auto_run_id=grant["id"],
            expected_head=fix, fix_commits=(foreign, fix), now=NOW,
        )


def test_empty_fix_commit_is_not_progress(git_repo):
    grant = _grant(git_repo)
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1,
        auto_run_id=grant["id"], expected_head=grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )
    _submit_round(git_repo, first.snapshot, round_number=1, fail=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "commit", "--allow-empty", "-qm", "empty"],
        check=True,
    )
    empty = subprocess.check_output(
        ["/usr/bin/git", "-C", str(git_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    decisions = write_json(
        git_repo / ".review/inbox/round-1/decisions.json",
        [{
            "id": "D-R1-A-001",
            "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
            "disposition": "fixed",
            "rationale": "Covered by a new regression test.",
            "executions": [execution("D-R1-E001")],
        }],
    )
    with pytest.raises(SchemaError, match="^AUTO_RUN_NO_PROGRESS$"):
        begin_round(
            git_repo, base="master", runtime="codex", round_number=2,
            decisions_path=decisions, auto_run_id=grant["id"],
            expected_head=empty, fix_commits=(empty,), now=NOW,
        )


def test_reworded_rebuttal_with_same_execution_is_not_progress(git_repo):
    grant = _grant(git_repo)
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1,
        auto_run_id=grant["id"], expected_head=grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )
    _submit_round(
        git_repo, first.snapshot, round_number=1, fail=True, a_execution=True
    )
    decisions = write_json(
        git_repo / ".review/inbox/round-1/decisions.json",
        [{
            "id": "D-R1-A-099",
            "finding_ref": {"round": 1, "id": "A-R1-001", "reviewer": "A"},
            "disposition": "rebutted",
            "rationale": "Different prose around the same old command output.",
            "executions": [execution("D-R1-E099")],
        }],
    )
    with pytest.raises(SchemaError, match="^AUTO_RUN_IDENTICAL_RETRY$"):
        begin_round(
            git_repo, base="master", runtime="codex", round_number=2,
            decisions_path=decisions, auto_run_id=grant["id"],
            expected_head=first.head_sha, fix_commits=(), now=NOW,
        )


def test_stop_during_round_revokes_future_continuation(git_repo):
    grant = _grant(git_repo)
    first = begin_round(
        git_repo, base="master", runtime="codex", round_number=1,
        auto_run_id=grant["id"], expected_head=grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )
    stopped = auto_run.stop(
        git_repo, auto_run_id=grant["id"], reason="USER_STOPPED", now=NOW
    )
    assert stopped["status"] == "stopped"
    final = _submit_round(git_repo, first.snapshot, round_number=1, fail=True)
    assert final.gate.status.value == "fail"
    state = auto_run.status(git_repo, auto_run_id=grant["id"])
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "USER_STOPPED"


def test_third_failed_round_exhausts_auto_run(git_repo):
    grant = _grant(git_repo)
    expected_head = grant["binding"]["snapshot"]["head_sha"]
    decisions = None
    fix_commits = ()
    for round_number in (1, 2, 3):
        pending = begin_round(
            git_repo, base="master", runtime="codex", round_number=round_number,
            decisions_path=decisions, auto_run_id=grant["id"],
            expected_head=expected_head, fix_commits=fix_commits, now=NOW,
        )
        final = _submit_round(
            git_repo, pending.snapshot, round_number=round_number, fail=True
        )
        if round_number < 3:
            fix = _commit(git_repo, f"fix {round_number}")
            expected_head = fix
            decisions = write_json(
                git_repo / f".review/inbox/round-{round_number}/decisions.json",
                [{
                    "id": f"D-R{round_number}-A-001",
                    "finding_ref": {
                        "round": round_number,
                        "id": f"A-R{round_number}-001",
                        "reviewer": "A",
                    },
                    "disposition": "fixed",
                    "rationale": "Covered by a new regression test.",
                    "executions": [execution(f"D-R{round_number}-E001")],
                }],
            )
            fix_commits = (fix,)
    state = auto_run.status(git_repo, auto_run_id=grant["id"])
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "MAX_ROUNDS"
    assert len(state["rounds"]) == 3


def test_stop_cannot_overwrite_terminal_pass(git_repo):
    grant = _grant(git_repo)
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1,
        auto_run_id=grant["id"], expected_head=grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )
    _submit_round(git_repo, pending.snapshot, round_number=1, fail=False)
    before = (git_repo / ".review/auto-runs.json").read_bytes()
    result = auto_run.stop(
        git_repo, auto_run_id=grant["id"], reason="CANCELLED", now=NOW
    )
    assert result["stop_reason"] == "PASS"
    assert (git_repo / ".review/auto-runs.json").read_bytes() == before


def test_stop_rejects_finalizer_owned_reason(git_repo):
    grant = _grant(git_repo)
    before = (git_repo / ".review/auto-runs.json").read_bytes()
    with pytest.raises(SchemaError, match="^AUTO_RUN_STOP_INVALID$"):
        auto_run.stop(git_repo, auto_run_id=grant["id"], reason="PASS", now=NOW)
    assert (git_repo / ".review/auto-runs.json").read_bytes() == before


def test_sixty_fifth_run_is_rejected_without_mutation(git_repo):
    grant = _grant(git_repo)
    auto_run.stop(git_repo, auto_run_id=grant["id"], reason="CANCELLED", now=NOW)
    path = git_repo / ".review/auto-runs.json"
    seed = json.loads(path.read_bytes())
    template = seed["runs"][0]
    seed["runs"] = [
        {**template, "id": f"{number:032x}"}
        for number in range(1, 65)
    ]
    raw = json.dumps(seed, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(raw) <= auto_run.MAX_STORE_BYTES
    path.write_bytes(raw)
    path.chmod(0o600)
    preview = auto_run.preview(git_repo, base="master", runtime="codex", now=NOW)
    before = path.read_bytes()
    with pytest.raises(SchemaError, match="^AUTO_RUN_STORE_FULL$"):
        auto_run.record_grant(
            git_repo, base="master", runtime="codex",
            expected_binding_sha256=preview["binding_sha256"], choice="100 자동",
            reason="capacity boundary", channel="relayed", now=NOW,
        )
    assert path.read_bytes() == before
    assert not (git_repo / ".review/auto-run-active.json").exists()
