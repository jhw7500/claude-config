"""Independent drift and failure checks for explicitly granted automatic runs."""

from dataclasses import replace
import hashlib
import json
import subprocess

import pytest

from pre_pr_tribunal import auto_run, verdict_store
from pre_pr_tribunal.gate import evaluate_gate
from pre_pr_tribunal.model import SchemaError, TribunalError
from pre_pr_tribunal.verdict_store import begin_round, finalize_round, read_verdict
from tests.pre_pr_tribunal.test_auto_run import _commit, _grant, _submit_round
from tests.pre_pr_tribunal.test_model_store import NOW, execution, write_json


def _begin(repo, grant, *, runtime="codex", expected_head=None):
    return begin_round(
        repo, base="master", runtime=runtime, round_number=1,
        auto_run_id=grant["id"],
        expected_head=expected_head or grant["binding"]["snapshot"]["head_sha"],
        now=NOW,
    )


@pytest.mark.parametrize("runtime", ("claude", "codex"))
def test_changed_head_cannot_use_initial_auto_consent(git_repo, runtime):
    grant = _grant(git_repo, runtime)
    intended_head = _commit(git_repo, "foreign change before the first round")
    original_store = (git_repo / ".review/auto-runs.json").read_bytes()
    with pytest.raises(SchemaError, match="AUTO_RUN_BINDING_CHANGED"):
        _begin(git_repo, grant, runtime=runtime, expected_head=intended_head)
    assert not (git_repo / ".review/verdict.json").exists()
    assert (git_repo / ".review/auto-runs.json").read_bytes() == original_store


def test_different_runtime_cannot_use_auto_consent(git_repo):
    grant = _grant(git_repo, "codex")
    with pytest.raises(SchemaError):
        _begin(git_repo, grant, runtime="claude")
    assert not (git_repo / ".review/verdict.json").exists()


def test_renamed_branch_cannot_use_auto_consent(git_repo):
    grant = _grant(git_repo)
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "branch", "-m", "different-writer"],
        check=True,
    )
    with pytest.raises(SchemaError, match="AUTO_RUN_BINDING_CHANGED"):
        _begin(git_repo, grant)
    assert not (git_repo / ".review/verdict.json").exists()


def test_changed_base_cannot_use_auto_consent(git_repo):
    grant = _grant(git_repo)
    # Move the remote-tracking base without editing the implementation HEAD.
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "update-ref",
         "refs/remotes/origin/master", "HEAD"], check=True,
    )
    with pytest.raises(TribunalError):
        _begin(git_repo, grant)
    assert not (git_repo / ".review/verdict.json").exists()
    assert auto_run.status(git_repo)["rounds"] == []


def test_changed_contract_cannot_use_auto_consent(git_repo, monkeypatch):
    grant = _grant(git_repo)
    contract = auto_run.current_contract_binding()
    monkeypatch.setattr(
        auto_run, "current_contract_binding",
        lambda: replace(contract, report_text=contract.report_text + 1),
    )
    with pytest.raises(SchemaError, match="AUTO_RUN_BINDING_CHANGED"):
        _begin(git_repo, grant)
    assert not (git_repo / ".review/verdict.json").exists()


@pytest.mark.parametrize("unsafe", ("public_mode", "symlink", "invalid_json"))
def test_unsafe_auto_state_stops_before_publishing_a_round(git_repo, unsafe):
    grant = _grant(git_repo)
    store = git_repo / ".review/auto-runs.json"
    if unsafe == "public_mode":
        store.chmod(0o644)
    elif unsafe == "symlink":
        target = store.with_name("moved-auto-state.json")
        store.rename(target)
        store.symlink_to(target.name)
    else:
        store.write_text("{", encoding="utf-8")
    with pytest.raises(SchemaError, match="AUTO_RUN_(FILE_UNSAFE|INVALID)"):
        _begin(git_repo, grant)
    assert not (git_repo / ".review/verdict.json").exists()


@pytest.mark.parametrize("reason", ("CANCELLED", "PROVIDER_FAILURE", "INTEGRITY_ERROR"))
def test_controller_stop_preserves_consent_and_prevents_another_begin(git_repo, reason):
    grant = _grant(git_repo)
    stopped = auto_run.stop(git_repo, auto_run_id=grant["id"], reason=reason, now=NOW)
    assert stopped["stop_reason"] == reason
    assert stopped["binding"] == grant["binding"]
    assert stopped["rounds"] == []
    raw = (git_repo / ".review/auto-runs.json").read_bytes()
    with pytest.raises(SchemaError, match="AUTO_RUN_STOPPED"):
        _begin(git_repo, grant)
    assert (git_repo / ".review/auto-runs.json").read_bytes() == raw
    assert json.loads(raw)["runs"][0]["id"] == grant["id"]


COMMAND = "PATH=/usr/bin:/bin /usr/bin/gh pr create --base master"


def _replacement_grant(repo):
    preview = auto_run.preview(repo, base="master", runtime="codex", now=NOW)
    return auto_run.record_grant(
        repo, base="master", runtime="codex",
        expected_binding_sha256=preview["binding_sha256"], choice="100 자동",
        reason="replacement consent", channel="relayed", now=NOW,
        token_hex=lambda _size: "2" * 32,
    )


@pytest.mark.parametrize("completed_round", (False, True))
def test_cancelled_lineage_cannot_reset_round_budget(git_repo, completed_round):
    grant = _grant(git_repo)
    if completed_round:
        pending = _begin(git_repo, grant)
        _submit_round(git_repo, pending.snapshot, round_number=1, fail=True)
    auto_run.stop(git_repo, auto_run_id=grant["id"], reason="CANCELLED", now=NOW)
    _commit(git_repo, "fix after cancellation")
    path = git_repo / ".review/auto-runs.json"
    before = path.read_bytes()
    with pytest.raises(SchemaError, match="^AUTO_RUN_LINEAGE_RESTART_FORBIDDEN$"):
        _replacement_grant(git_repo)
    assert path.read_bytes() == before
    assert not (git_repo / ".review/auto-run-active.json").exists()


def test_new_branch_can_receive_separate_automatic_consent(git_repo):
    grant = _grant(git_repo)
    auto_run.stop(git_repo, auto_run_id=grant["id"], reason="CANCELLED", now=NOW)
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "checkout", "-qb", "separate-work"],
        check=True,
    )
    result = _replacement_grant(git_repo)
    assert result["status"] == "granted"
    assert len(json.loads((git_repo / ".review/auto-runs.json").read_bytes())["runs"]) == 2


@pytest.mark.parametrize("failure", ("store", "active"))
def test_stop_write_failure_preserves_authority_and_can_be_reconciled(
    git_repo, monkeypatch, failure,
):
    grant = _grant(git_repo)

    def fail(*_args, **_kwargs):
        raise SchemaError("INJECTED_WRITE_FAILURE")

    with monkeypatch.context() as patch:
        target = "_write_store_locked" if failure == "store" else "_remove_active_locked"
        patch.setattr(auto_run, target, fail)
        with pytest.raises(SchemaError, match="^INJECTED_WRITE_FAILURE$"):
            auto_run.stop(git_repo, auto_run_id=grant["id"], reason="CANCELLED", now=NOW)
    assert (git_repo / ".review/auto-run-active.json").exists()
    state = auto_run.status(git_repo, auto_run_id=grant["id"])
    assert state["status"] == ("granted" if failure == "store" else "stopped")
    reconciled = auto_run.stop(
        git_repo, auto_run_id=grant["id"], reason="USER_STOPPED", now=NOW,
    )
    assert reconciled["stop_reason"] == ("USER_STOPPED" if failure == "store" else "CANCELLED")
    assert not (git_repo / ".review/auto-run-active.json").exists()


@pytest.mark.parametrize("failure", ("archive", "store", "capacity", "active", "verdict"))
def test_interrupted_finalize_never_exposes_pass_and_can_retry(
    git_repo, monkeypatch, failure,
):
    grant = _grant(git_repo)
    pending = _begin(git_repo, grant)
    pending_path = git_repo / ".review/verdict.json"

    def fail(*_args, **_kwargs):
        raise SchemaError("INJECTED_WRITE_FAILURE")

    with monkeypatch.context() as patch:
        if failure == "archive":
            original = auto_run._archive_create

            def fail_after_verdict(directory, name, raw, maximum):
                if name == "A.json":
                    fail()
                return original(directory, name, raw, maximum)

            patch.setattr(auto_run, "_archive_create", fail_after_verdict)
        elif failure == "store":
            patch.setattr(auto_run, "_write_store_locked", fail)
        elif failure == "capacity":
            size = len((git_repo / ".review/auto-runs.json").read_bytes())
            patch.setattr(auto_run, "MAX_STORE_BYTES", size + 1)
        elif failure == "active":
            patch.setattr(auto_run, "_remove_active_locked", fail)
        else:
            original_write = verdict_store._atomic_write

            def fail_terminal(fd, verdict):
                if verdict.gate.status.value == "pass":
                    fail()
                return original_write(fd, verdict)

            patch.setattr(verdict_store, "_atomic_write", fail_terminal)
        with pytest.raises(SchemaError, match="INJECTED_WRITE_FAILURE|AUTO_RUN_TOO_LARGE"):
            _submit_round(git_repo, pending.snapshot, round_number=1, fail=False)

    assert read_verdict(git_repo).gate.status.value == "in_progress"
    assert json.loads(pending_path.read_bytes())["gate"]["status"] == "in_progress"
    assert evaluate_gate(git_repo, COMMAND, runtime="codex").block
    if failure == "capacity":
        assert not (git_repo / f".review/auto-run-archive/{grant['id']}/round-1").exists()
    interrupted = auto_run.status(git_repo, auto_run_id=grant["id"])
    assert interrupted["current"] is not None or len(interrupted["rounds"]) == 1

    # A later wall clock must not make authentic orphaned archives unrecoverable.
    final = finalize_round(git_repo, now=lambda: "2026-09-01T00:00:10Z")
    assert final.gate.status.value == "pass"
    assert not evaluate_gate(git_repo, COMMAND, runtime="codex").block
    completed = auto_run.status(git_repo, auto_run_id=grant["id"])
    assert completed["status"] == "stopped"
    assert completed["stop_reason"] == "PASS"
    assert completed["current"] is None
    assert len(completed["rounds"]) == 1
    assert completed["rounds"][0]["verdict_sha256"] == hashlib.sha256(
        pending_path.read_bytes()
    ).hexdigest()
    assert not (git_repo / ".review/auto-run-active.json").exists()


@pytest.mark.parametrize("name", ("verdict", "A"))
def test_completed_ledger_retry_rejects_changed_archive(git_repo, monkeypatch, name):
    grant = _grant(git_repo)
    pending = _begin(git_repo, grant)
    original = verdict_store._atomic_write

    def fail_terminal(fd, verdict):
        if verdict.gate.status.value == "pass":
            raise SchemaError("INJECTED_WRITE_FAILURE")
        return original(fd, verdict)

    with monkeypatch.context() as patch:
        patch.setattr(verdict_store, "_atomic_write", fail_terminal)
        with pytest.raises(SchemaError, match="^INJECTED_WRITE_FAILURE$"):
            _submit_round(git_repo, pending.snapshot, round_number=1, fail=False)
    archive = git_repo / f".review/auto-run-archive/{grant['id']}/round-1/{name}.json"
    archive.write_bytes(b"[]")
    with pytest.raises(SchemaError, match="^AUTO_RUN_ARCHIVE_UNSAFE$"):
        finalize_round(git_repo, now=NOW)
    assert read_verdict(git_repo).gate.status.value == "in_progress"
    assert evaluate_gate(git_repo, COMMAND, runtime="codex").block
    assert len(auto_run.status(git_repo)["rounds"]) == 1


@pytest.mark.parametrize("orphan", ("exact", "different", "public", "symlink"))
def test_decision_archive_after_store_failure_is_authenticated_on_retry(
    git_repo, monkeypatch, orphan,
):
    grant = _grant(git_repo)
    first = _begin(git_repo, grant)
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
    arguments = dict(
        base="master", runtime="codex", round_number=2,
        decisions_path=decisions, auto_run_id=grant["id"],
        expected_head=fix, fix_commits=(fix,), now=NOW,
    )

    def fail(*_args, **_kwargs):
        raise SchemaError("INJECTED_WRITE_FAILURE")

    before = (git_repo / ".review/auto-runs.json").read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(auto_run, "_write_store_locked", fail)
        with pytest.raises(SchemaError, match="^INJECTED_WRITE_FAILURE$"):
            begin_round(git_repo, **arguments)
    archive = git_repo / f".review/auto-run-archive/{grant['id']}/round-1/decisions.json"
    assert archive.read_bytes() == decisions.read_bytes()
    assert (git_repo / ".review/auto-runs.json").read_bytes() == before
    if orphan == "different":
        archive.write_bytes(b"[]")
    elif orphan == "public":
        archive.chmod(0o644)
    elif orphan == "symlink":
        archive.unlink()
        archive.symlink_to(decisions)
    if orphan != "exact":
        with pytest.raises(SchemaError, match="^AUTO_RUN_ARCHIVE_(UNSAFE|EXISTS)$"):
            begin_round(git_repo, **arguments)
        assert (git_repo / ".review/auto-runs.json").read_bytes() == before
        assert read_verdict(git_repo).gate.status.value == "fail"
    else:
        recovered = begin_round(git_repo, **arguments)
        assert recovered.round == 2
        state = auto_run.status(git_repo, auto_run_id=grant["id"])
        assert state["status"] == "begin_reserved"
        assert len(state["rounds"]) == 1
        assert state["current"]["round"] == 2
        assert archive.read_bytes() == decisions.read_bytes()
