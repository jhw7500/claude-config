"""Independent drift and failure checks for explicitly granted automatic runs."""

from dataclasses import replace
import json
import subprocess

import pytest

from pre_pr_tribunal import auto_run
from pre_pr_tribunal.model import SchemaError, TribunalError
from pre_pr_tribunal.verdict_store import begin_round
from tests.pre_pr_tribunal.test_auto_run import _commit, _grant
from tests.pre_pr_tribunal.test_model_store import NOW


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
