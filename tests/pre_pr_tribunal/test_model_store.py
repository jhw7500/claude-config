import fcntl
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from pre_pr_tribunal.git_state import DIFF_RECIPE_VERSION, capture_snapshot
from pre_pr_tribunal.model import (
    SCHEMA_VERSION,
    SLOT_VERDICT_SCHEMA_VERSION,
    VERDICT_SCHEMA_VERSION,
    ContractBinding,
    MAX_EVIDENCE_TEXT_BYTES,
    MAX_REPORT_BYTES,
    REPORT_TEXT_CONTRACT_VERSION,
    ReportReceipt,
    Reviewer,
    ReviewerSlot,
    SchemaError,
    parse_decisions,
    parse_reviewer_report,
    validate_report_bytes,
)
from pre_pr_tribunal.verdict_store import (
    begin_round,
    finalize_round,
    read_verdict,
    require_current_in_progress,
    store_reviewer_report,
    validate_stored_reviewer_report,
)
from pre_pr_tribunal.review_context import context_sha256, current_contract_binding


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
        path = repo / f".review/inbox/round-{round_number}/{reviewer}.json"
        if read_verdict(repo).schema == 1:
            store_reviewer_report(repo, reviewer=Reviewer(reviewer), raw=json.dumps(value).encode())
        else:
            submit_reviewer_report(repo, reviewer=Reviewer(reviewer), raw=json.dumps(value).encode(), now=NOW)
        result[reviewer] = path
    return result


def legacy_pending(repo):
    pending = begin_round(repo, base="master", runtime="codex", round_number=1, now=NOW)
    legacy = replace(pending, schema=1, contract=None, lifecycle_id=None,
                     reviewers={key: ReviewerSlot("pending") for key in "ABC"})
    write_json(repo / ".review/verdict.json", legacy.to_json())
    return legacy


def begin_legacy_round(repo, **kwargs):
    pending = begin_round(repo, **kwargs)
    legacy = replace(pending, schema=1, contract=None, lifecycle_id=None,
                     reviewers={key: ReviewerSlot("pending") for key in "ABC"})
    write_json(repo / ".review/verdict.json", legacy.to_json())
    return legacy


@pytest.mark.parametrize("reviewer", "ABC")
@pytest.mark.parametrize("tamper", ("bytes", "mode", "symlink", "owner", "context", "parsed", "contract"))
def test_finalize_authenticates_every_sealed_receipt(git_repo, tamper, reviewer, monkeypatch):
    pending = write_current_pending(git_repo)
    for key in "ABC":
        submit_reviewer_report(git_repo, reviewer=Reviewer(key),
                               raw=json.dumps(report(pending.snapshot, key)).encode(), now=NOW)
    target = git_repo / f".review/inbox/round-1/{reviewer}.json"
    expected = "FILE_UNSAFE"
    if tamper == "bytes":
        target.write_bytes(target.read_bytes() + b" ")
        expected = "REPORT_BYTES_MISMATCH"
    elif tamper == "mode":
        target.chmod(0o644)
    elif tamper == "symlink":
        target.rename(target.with_suffix(".original"))
        target.symlink_to(f"{reviewer}.original")
    elif tamper == "owner":
        original = os.fstat
        inode = target.stat().st_ino
        def wrong_owner(fd):
            info = original(fd)
            if info.st_ino == inode:
                fields = list(info)
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info
        monkeypatch.setattr(os, "fstat", wrong_owner)
    else:
        payload = read_verdict(git_repo).to_json()
        if tamper == "context":
            payload["reviewers"][reviewer]["receipt"]["context_sha256"] = "0" * 64
            expected = "CONTEXT_DRIFT"
        elif tamper == "parsed":
            payload["reviewers"][reviewer]["report"]["executions"] = [execution(f"{reviewer}-R1-E001")]
            expected = "REPORT_RECEIPT_MISMATCH"
        else:
            payload["contract"]["diff_recipe"] = 99
            expected = "CONTRACT_DRIFT"
        write_json(git_repo / ".review/verdict.json", payload)
    before = (git_repo / ".review/verdict.json").read_bytes()
    with pytest.raises(SchemaError, match=f"^{expected}$"):
        finalize_round(git_repo, now=NOW)
    assert (git_repo / ".review/verdict.json").read_bytes() == before


@pytest.mark.parametrize("sealed", (0, 2, 3))
def test_v2_round_not_ready_and_successful_terminal_roundtrip(git_repo, sealed):
    pending = write_current_pending(git_repo)
    for key in "ABC"[:sealed]:
        submit_reviewer_report(git_repo, reviewer=Reviewer(key),
                               raw=json.dumps(report(pending.snapshot, key)).encode(), now=NOW)
    loaded = read_verdict(git_repo)
    assert sum(slot.status == "sealed" for slot in loaded.reviewers.values()) == sealed
    if sealed < 3:
        with pytest.raises(SchemaError, match="^ROUND_NOT_READY$"):
            finalize_round(git_repo, now=NOW)
    else:
        final = finalize_round(git_repo, now=NOW)
        assert final.schema == 3 and final.gate.status.value == "pass"
        assert read_verdict(git_repo) == final


@pytest.mark.parametrize(("severity", "status", "count"), (
    ("CRITICAL", "fail", 1), ("HIGH", "fail", 1), ("LOW", "pass", 0),
))
def test_v2_terminal_aggregates_every_sealed_report(git_repo, severity, status, count):
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    report_paths(git_repo, pending.snapshot,
                 overrides={"C": {"findings": [finding("C-R1-001", reviewer="C", severity=severity)]}})
    sealed = read_verdict(git_repo)
    final = finalize_round(git_repo, now=NOW)
    assert final.gate.status.value == status
    assert final.gate.blocking_count == count
    assert final.reviewers == sealed.reviewers
    assert read_verdict(git_repo) == final


@pytest.mark.parametrize("version", (1, 2))
def test_begin_refuses_pending_reset_without_losing_reports(git_repo, version):
    pending = legacy_pending(git_repo) if version == 1 else write_current_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A", findings=[finding()])).encode()
    if version == 1:
        store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
    else:
        submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    before = (git_repo / ".review/verdict.json").read_bytes()
    with pytest.raises(SchemaError, match="^ROUND_NOT_IN_PROGRESS$|^ROUND_TRANSITION_INVALID$"):
        begin_legacy_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert (git_repo / ".review/inbox/round-1/A.json").read_bytes() == raw


def test_new_round_writer_uses_schema_three(git_repo):
    assert begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW).schema == 3


@pytest.mark.parametrize("replacement", (False, True))
@pytest.mark.parametrize("orphan", (False, True))
def test_legacy_store_never_mutates_v2_canonical_or_orphan(git_repo, replacement, orphan):
    pending = write_current_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A", findings=[finding()])).encode()
    path = git_repo / ".review/inbox/round-1/A.json"
    if orphan:
        path.write_bytes(raw)
        path.chmod(0o600)
    before = (git_repo / ".review/verdict.json").read_bytes()
    with pytest.raises(SchemaError, match="^LEGACY_COMMAND_NOT_ALLOWED$"):
        store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=b"replacement",
                              replace_pending_recovery=replacement)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert path.read_bytes() == raw if orphan else not path.exists()


def test_legacy_migration_retains_all_bytes_without_inventing_provenance(git_repo):
    from pre_pr_tribunal import verdict_store
    pending = legacy_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A", findings=[finding()])).encode()
    for key, value in (("A", raw), ("B", b'{"schema":1')):
        store_reviewer_report(git_repo, reviewer=Reviewer(key), raw=value)
    result = verdict_store.migrate_legacy_pending_round(
        git_repo, token_hex=lambda _size: "d" * 32,
    )
    assert result.reviewers == {"A": "pending:LEGACY_PROVENANCE_UNAVAILABLE",
                                "B": "pending:JSON_INVALID", "C": "pending:REVIEWER_REPORT_MISSING"}
    loaded = read_verdict(git_repo)
    assert loaded.schema == 3
    assert loaded.lifecycle_id == "d" * 32
    assert all(slot.status == "pending" for slot in loaded.reviewers.values())
    assert (git_repo / ".review/attempts/round-1/A/attempt-1.raw").read_bytes() == raw
    assert (git_repo / ".review/attempts/round-1/B/attempt-1.raw").read_bytes() == b'{"schema":1'
    assert not (git_repo / ".review/inbox/round-1/A.json").exists()
    assert loaded.reviewers["A"].attempt_count == 1
    assert loaded.reviewers["C"].attempt_count == 0


@pytest.mark.parametrize("unsafe", ("symlink", "fifo", "mode"))
def test_legacy_migration_prescans_all_slots_before_mutation(git_repo, unsafe):
    from pre_pr_tribunal import verdict_store
    pending = legacy_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A")).encode()
    store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
    target = git_repo / ".review/inbox/round-1/C.json"
    if unsafe == "symlink":
        target.symlink_to("A.json")
    elif unsafe == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(raw)
        target.chmod(0o644)
    before = (git_repo / ".review/verdict.json").read_bytes()
    with pytest.raises(SchemaError, match="^FILE_UNSAFE$"):
        verdict_store.migrate_legacy_pending_round(git_repo)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert (git_repo / ".review/inbox/round-1/A.json").read_bytes() == raw
    assert not (git_repo / ".review/attempts").exists()


@pytest.mark.parametrize("failure", ("verdict", "second_unlink"))
def test_legacy_migration_retry_recovers_preserved_pending_evidence(git_repo, monkeypatch, failure):
    from pre_pr_tribunal import verdict_store
    pending = legacy_pending(git_repo)
    raws = {key: json.dumps(report(pending.snapshot, key)).encode() for key in "ABC"}
    for key, raw in raws.items():
        store_reviewer_report(git_repo, reviewer=Reviewer(key), raw=raw)
    before = (git_repo / ".review/verdict.json").read_bytes()
    with monkeypatch.context() as patch:
        if failure == "verdict":
            def fail(*args):
                raise SchemaError("VERDICT_WRITE_FAILED")
            patch.setattr(verdict_store, "_atomic_write", fail)
        else:
            original = os.unlink
            def fail(path, *args, **kwargs):
                if path == "B.json":
                    raise OSError("injected removal failure")
                return original(path, *args, **kwargs)
            patch.setattr(os, "unlink", fail)
        with pytest.raises(SchemaError):
            verdict_store.migrate_legacy_pending_round(git_repo)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    verdict_store.migrate_legacy_pending_round(git_repo)
    restored = read_verdict(git_repo)
    for key in "ABC":
        assert restored.reviewers[key].status == "pending"
        assert restored.reviewers[key].attempt_count == 1
        assert restored.reviewers[key].last_error == "LEGACY_PROVENANCE_UNAVAILABLE"
        assert (git_repo / f".review/attempts/round-1/{key}/attempt-1.raw").read_bytes() == raws[key]
        assert not (git_repo / f".review/inbox/round-1/{key}.json").exists()
    replacement = json.dumps(report(pending.snapshot, "A", findings=[finding()])).encode()
    submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=replacement, now=NOW)
    assert read_verdict(git_repo).reviewers["A"].report.findings[0].severity.value == "HIGH"


def test_legacy_migration_rejects_evidence_reason_inconsistent_with_raw(git_repo, monkeypatch):
    from pre_pr_tribunal import verdict_store
    pending = legacy_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A")).encode()
    store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
    with monkeypatch.context() as patch:
        def fail(*args):
            raise SchemaError("VERDICT_WRITE_FAILED")
        patch.setattr(verdict_store, "_atomic_write", fail)
        with pytest.raises(SchemaError, match="VERDICT_WRITE_FAILED"):
            verdict_store.migrate_legacy_pending_round(git_repo)
    path = git_repo / ".review/attempts/round-1/A/attempt-1.meta.json"
    value = json.loads(path.read_bytes())
    value["reason_code"] = "JSON_INVALID"
    write_json(path, value)
    before = (git_repo / ".review/verdict.json").read_bytes()
    with pytest.raises(SchemaError, match="^ATTEMPT_EVIDENCE_UNSAFE$"):
        verdict_store.migrate_legacy_pending_round(git_repo)
    assert (git_repo / ".review/verdict.json").read_bytes() == before


def test_legacy_migration_refuses_subset_and_terminal_v1(git_repo):
    from pre_pr_tribunal import verdict_store
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    report_paths(git_repo, pending.snapshot)
    native = finalize_round(git_repo, now=NOW)
    terminal = replace(native, schema=1, contract=None, lifecycle_id=None,
                       reviewers={key: ReviewerSlot("complete", slot.report)
                                  for key, slot in native.reviewers.items()})
    write_json(git_repo / ".review/verdict.json", terminal.to_json())
    before = (git_repo / ".review/verdict.json").read_bytes()
    with pytest.raises(TypeError):
        verdict_store.migrate_legacy_pending_round(git_repo, reviewers=("A",))
    with pytest.raises(SchemaError, match="^LEGACY_MIGRATION_NOT_ALLOWED$"):
        verdict_store.migrate_legacy_pending_round(git_repo)
    assert (git_repo / ".review/verdict.json").read_bytes() == before


def test_legacy_migration_snapshot_drift_preserves_canonical_and_verdict(git_repo):
    from pre_pr_tribunal.verdict_store import migrate_legacy_pending_round
    pending = legacy_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A")).encode()
    store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
    before = (git_repo / ".review/verdict.json").read_bytes()
    commit_fix(git_repo)
    with pytest.raises(SchemaError, match="^SNAPSHOT_CHANGED$"):
        migrate_legacy_pending_round(git_repo)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert (git_repo / ".review/inbox/round-1/A.json").read_bytes() == raw
    assert not (git_repo / ".review/attempts").exists()


def write_current_pending(git_repo):
    return begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )


def submit_reviewer_report(*args, **kwargs):
    from pre_pr_tribunal.verdict_store import submit_reviewer_report as submit
    return submit(*args, **kwargs)


def record_reviewer_failure(*args, **kwargs):
    from pre_pr_tribunal.verdict_store import record_reviewer_failure as record
    return record(*args, **kwargs)


def test_begin_rejects_individual_ignores_before_creating_review_state(git_repo):
    """Primary begin must reject unsafe storage independently of advisory telemetry."""
    (git_repo / ".gitignore").write_text(
        "/.review/verdict.json\n/.review/lock\n/.review/inbox/\n", encoding="utf-8",
    )
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "commit", "-qam", "partial exclusion"],
        check=True,
    )
    with pytest.raises(SchemaError, match="^VERDICT_NOT_IGNORED$"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert not (git_repo / ".review").exists()


@pytest.mark.parametrize("operation", ("malformed", "secret-marker", "valid", "failure", "legacy"))
def test_storage_rechecks_exclusion_before_report_or_attempt_publication(git_repo, operation):
    """Dropping the storage guard leaks raw evidence or advances a slot after ignore drift."""
    (git_repo / ".gitignore").write_text("", encoding="utf-8")
    subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "commit", "-qam", "use local exclusion"],
        check=True,
    )
    exclude = git_repo / ".git/info/exclude"
    exclude.write_text(".review/\n", encoding="utf-8")
    pending = legacy_pending(git_repo) if operation == "legacy" else write_current_pending(git_repo)
    value = report(pending.snapshot, "A")
    if operation == "secret-marker":
        value["executions"] = [{**execution(), "stdout_excerpt": "ghp_nonfunctional_fixture_marker"}]
    raw = b"not-json" if operation == "malformed" else json.dumps(value).encode()
    if operation == "legacy":
        store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
    before = (git_repo / ".review/verdict.json").read_bytes()
    # A local exclusion change leaves HEAD and the committed diff unchanged.
    exclude.write_text(
        "/.review/verdict.json\n/.review/lock\n/.review/inbox/\n", encoding="utf-8",
    )
    with pytest.raises(SchemaError, match="^VERDICT_NOT_IGNORED$"):
        if operation == "failure":
            record_reviewer_failure(git_repo, reviewer=Reviewer.A, reason_code="DISPATCH_FAILED")
        elif operation == "legacy":
            from pre_pr_tribunal.verdict_store import migrate_legacy_pending_round
            migrate_legacy_pending_round(git_repo)
        else:
            submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert not (git_repo / ".review/attempts").exists()
    canonical = git_repo / ".review/inbox/round-1/A.json"
    if operation == "legacy":
        assert canonical.read_bytes() == raw
    else:
        assert not canonical.exists()


@pytest.mark.parametrize("mask", (0o000, 0o022, 0o077))
def test_submit_valid_report_seals_only_its_slot(git_repo, mask):
    pending = write_current_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode() + b"\r\n"
    previous = os.umask(mask)
    try:
        receipt = submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    finally:
        os.umask(previous)
    stored = read_verdict(git_repo)
    assert stored.reviewers["A"].status == "sealed"
    assert stored.reviewers["B"] == pending.reviewers["B"]
    assert stored.reviewers["C"] == pending.reviewers["C"]
    assert stored.reviewers["A"].receipt == receipt
    assert stored.reviewers["A"].attempt_count == receipt.attempt == 1
    assert receipt.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.context_sha256 == context_sha256(pending, Reviewer.A)
    assert receipt.report_contract_version == REPORT_TEXT_CONTRACT_VERSION
    assert receipt.provenance == "native_submit"
    target = git_repo / ".review/inbox/round-1/A.json"
    assert target.read_bytes() == raw
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600
    assert target.lstat().st_uid == os.geteuid()


@pytest.mark.parametrize(("raw", "code"), (
    (b'{"schema":1', "JSON_INVALID"),
    (b'{"schema":1,"schema":1}', "JSON_DUPLICATE_KEY"),
    (b"x" * (MAX_REPORT_BYTES + 1), "REPORT_TOO_LARGE"),
), ids=("malformed", "duplicate", "oversize"))
def test_submit_malformed_report_records_attempt_without_touching_peers(git_repo, raw, code):
    pending = write_current_pending(git_repo)
    with pytest.raises(SchemaError, match=f"^{code}$"):
        submit_reviewer_report(git_repo, reviewer=Reviewer.C, raw=raw, now=NOW)
    stored = read_verdict(git_repo)
    assert stored.reviewers["C"].attempt_count == 1
    assert stored.reviewers["C"].last_error == code
    assert all(stored.reviewers[key].status == "pending" for key in "ABC")
    assert stored.reviewers["A"] == pending.reviewers["A"]
    assert stored.reviewers["B"] == pending.reviewers["B"]
    assert not (git_repo / ".review/inbox/round-1/C.json").exists()
    assert (git_repo / ".review/attempts/round-1/C/attempt-1.raw").read_bytes() == raw


def test_record_timeout_changes_only_pending_slot(git_repo):
    pending = write_current_pending(git_repo)
    slot = record_reviewer_failure(git_repo, reviewer=Reviewer.B, reason_code="REVIEWER_TIMEOUT")
    assert slot.attempt_count == 1 and slot.last_error == "REVIEWER_TIMEOUT"
    stored = read_verdict(git_repo)
    assert stored.reviewers["B"] == slot
    assert stored.reviewers["A"] == pending.reviewers["A"]
    assert stored.reviewers["C"] == pending.reviewers["C"]


def test_submit_after_failure_clears_error_and_preserves_peer_context(git_repo):
    pending = write_current_pending(git_repo)
    digest = context_sha256(pending, Reviewer.C)
    record_reviewer_failure(git_repo, reviewer=Reviewer.C, reason_code="REVIEWER_FAILED")
    submit_reviewer_report(git_repo, reviewer=Reviewer.A,
                           raw=json.dumps(report(pending.snapshot, "A")).encode(), now=NOW)
    receipt = submit_reviewer_report(git_repo, reviewer=Reviewer.C,
                                    raw=json.dumps(report(pending.snapshot, "C")).encode(), now=NOW)
    slot = read_verdict(git_repo).reviewers["C"]
    assert slot.last_error is None
    assert receipt.attempt == slot.attempt_count == 2
    assert receipt.context_sha256 == digest


def test_submit_blocker_containing_report_seals(git_repo):
    pending = write_current_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A", findings=(finding(),))).encode()
    submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    stored = read_verdict(git_repo)
    assert stored.reviewers["A"].status == "sealed"
    assert stored.reviewers["A"].report.findings[0].severity.value == "HIGH"
    assert stored.gate.status.value == "in_progress"


@pytest.mark.parametrize("operation", ("submit", "failure"))
def test_sealed_slot_rejects_report_or_reviewer_failure_without_mutation(git_repo, operation):
    pending = write_current_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A")).encode()
    submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    verdict_path = git_repo / ".review/verdict.json"
    before = verdict_path.read_bytes()
    with pytest.raises(SchemaError, match="^REVIEWER_SLOT_SEALED$"):
        if operation == "submit":
            submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=b"replacement", now=NOW)
        else:
            record_reviewer_failure(git_repo, reviewer=Reviewer.A, reason_code="REVIEWER_TIMEOUT")
    assert verdict_path.read_bytes() == before
    assert (git_repo / ".review/inbox/round-1/A.json").read_bytes() == raw


@pytest.mark.parametrize("operation", ("submit", "failure"))
@pytest.mark.parametrize("drift", ("contract", "snapshot"))
def test_submit_and_reviewer_failure_drift_preserve_all_bytes(git_repo, operation, drift):
    pending = write_current_pending(git_repo)
    if drift == "contract":
        pending = replace(pending, contract=replace(pending.contract, report_text=99))
        write_json(git_repo / ".review/verdict.json", pending.to_json())
        code = "CONTRACT_DRIFT"
    else:
        commit_fix(git_repo)
        code = "SNAPSHOT_CHANGED"
    verdict_path = git_repo / ".review/verdict.json"
    before = verdict_path.read_bytes()
    with pytest.raises(SchemaError, match=f"^{code}$"):
        if operation == "submit":
            submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=b"invalid", now=NOW)
        else:
            record_reviewer_failure(git_repo, reviewer=Reviewer.A, reason_code="REVIEWER_TIMEOUT")
    assert verdict_path.read_bytes() == before
    assert not (git_repo / ".review/attempts").exists()


@pytest.mark.parametrize("replacement", (b"invalid", b"x" * (MAX_REPORT_BYTES + 2), None),
                         ids=("malformed", "oversize", "valid"))
def test_submit_report_published_before_verdict_failure_is_sealed_without_replacement(
    git_repo, monkeypatch, replacement
):
    from pre_pr_tribunal import verdict_store

    pending = write_current_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A", findings=(finding(),))).encode() + b"\n"
    if replacement is None:
        replacement = json.dumps(report(pending.snapshot, "A")).encode()
    before = (git_repo / ".review/verdict.json").read_bytes()
    with monkeypatch.context() as patch:
        def fail_verdict(*args, **kwargs):
            raise SchemaError("VERDICT_WRITE_FAILED")
        patch.setattr(verdict_store, "_atomic_write", fail_verdict)
        with pytest.raises(SchemaError, match="^VERDICT_WRITE_FAILED$"):
            submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    target = git_repo / ".review/inbox/round-1/A.json"
    assert target.read_bytes() == raw
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    receipt = submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=replacement, now=NOW)
    assert target.read_bytes() == raw
    assert receipt.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.attempt == 1
    assert read_verdict(git_repo).reviewers["A"].report.findings[0].severity.value == "HIGH"


@pytest.mark.parametrize("kind", ("invalid", "symlink", "fifo", "mode", "owner"))
def test_submit_never_overwrites_invalid_or_unsafe_canonical(git_repo, monkeypatch, kind):
    pending = write_current_pending(git_repo)
    target = git_repo / ".review/inbox/round-1/A.json"
    raw = json.dumps(report(pending.snapshot, "A")).encode()
    if kind == "symlink":
        target.symlink_to(git_repo / "tracked.txt")
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.write_bytes(b"invalid" if kind == "invalid" else raw)
        target.chmod(0o644 if kind == "mode" else 0o600)
    if kind == "owner":
        inode = target.stat().st_ino
        real_fstat = os.fstat
        def wrong_owner(fd):
            info = real_fstat(fd)
            if info.st_ino == inode:
                fields = list(info)
                fields[4] += 1
                return os.stat_result(fields)
            return info
        monkeypatch.setattr(os, "fstat", wrong_owner)
    before = (git_repo / ".review/verdict.json").read_bytes()
    code = "JSON_INVALID" if kind == "invalid" else "FILE_UNSAFE"
    with pytest.raises(SchemaError, match=f"^{code}$"):
        submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert not (git_repo / ".review/attempts").exists()
    if kind == "invalid":
        assert target.read_bytes() == b"invalid"


@pytest.mark.parametrize("code", ("JSON_INVALID", "FILE_UNSAFE", "REPORT_WRITE_FAILED", "unknown"))
def test_reviewer_failure_rejects_non_operational_reasons(git_repo, code):
    write_current_pending(git_repo)
    before = (git_repo / ".review/verdict.json").read_bytes()
    with pytest.raises(SchemaError, match="^REVIEWER_FAILURE_INVALID$"):
        record_reviewer_failure(git_repo, reviewer=Reviewer.A, reason_code=code)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert not (git_repo / ".review/attempts").exists()


@pytest.mark.parametrize(("fields", "code"), (
    ({"findings": [{}]}, "FINDING_SCHEMA_INVALID"),
    ({"findings": [finding(severity="unknown")]}, "FINDING_SCHEMA_INVALID"),
    ({"findings": [{**finding(), "line": -1}]}, "FINDING_SCHEMA_INVALID"),
    ({"findings": [finding()] * 129}, "FINDING_LIMIT_EXCEEDED"),
    ({"executions": [{}] * 129}, "EXECUTION_LIMIT_EXCEEDED"),
    ({"claims": [{}] * 129}, "CLAIM_LIMIT_EXCEEDED"),
))
@pytest.mark.parametrize("legacy", (False, True))
def test_bounded_content_errors_preserve_exact_evidence_and_pending_slot(git_repo, fields, code, legacy):
    from pre_pr_tribunal import verdict_store

    pending = legacy_pending(git_repo) if legacy else write_current_pending(git_repo)
    peer_raw = json.dumps(report(pending.snapshot, "B", findings=[
        finding("B-R1-001", reviewer="B", execution_ids=["B-R1-E001"])],
        executions=[execution("B-R1-E001")])).encode()
    if legacy:
        store_reviewer_report(git_repo, reviewer=Reviewer.B, raw=peer_raw)
    else:
        submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=peer_raw, now=NOW)
    peer = read_verdict(git_repo).reviewers["B"]
    raw = json.dumps(report(pending.snapshot, "A", **fields)).encode() + b" \n"
    if legacy:
        store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
        verdict_store.migrate_legacy_pending_round(git_repo)
    else:
        with pytest.raises(SchemaError, match=f"^{code}$"):
            submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    loaded = read_verdict(git_repo)
    assert loaded.reviewers["A"].status == "pending"
    assert loaded.reviewers["A"].attempt_count == 1
    assert loaded.reviewers["A"].last_error == code
    evidence = git_repo / ".review/attempts/round-1/A/attempt-1.raw"
    assert evidence.read_bytes() == raw
    assert stat.S_ISREG(evidence.lstat().st_mode)
    assert evidence.lstat().st_uid == os.geteuid()
    assert stat.S_IMODE(evidence.lstat().st_mode) == 0o600
    metadata = json.loads(evidence.with_name("attempt-1.meta.json").read_bytes())
    assert metadata["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert not (git_repo / ".review/inbox/round-1/A.json").exists()
    if legacy:
        assert all(slot.status == "pending" for slot in loaded.reviewers.values())
        assert (git_repo / ".review/attempts/round-1/B/attempt-1.raw").read_bytes() == peer_raw
    else:
        assert loaded.reviewers["B"] == peer
        assert (git_repo / ".review/inbox/round-1/B.json").read_bytes() == peer_raw
    receipt = submit_reviewer_report(git_repo, reviewer=Reviewer.A,
        raw=json.dumps(report(pending.snapshot, "A")).encode(), now=NOW)
    assert receipt.attempt == 2


def test_submit_orphan_content_error_is_integrity_stop_without_failure_evidence(git_repo):
    pending = write_current_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A", findings=[finding()] * 129)).encode()
    target = git_repo / ".review/inbox/round-1/A.json"
    target.write_bytes(raw)
    target.chmod(0o600)
    before = (git_repo / ".review/verdict.json").read_bytes()
    with pytest.raises(SchemaError, match="^FINDING_LIMIT_EXCEEDED$"):
        submit_reviewer_report(git_repo, reviewer=Reviewer.A,
            raw=json.dumps(report(pending.snapshot, "A")).encode(), now=NOW)
    assert target.read_bytes() == raw
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert not (git_repo / ".review/attempts").exists()


def test_submit_accepts_cumulative_attempts_after_three_failures(git_repo):
    pending = write_current_pending(git_repo)
    for _ in range(5):
        record_reviewer_failure(git_repo, reviewer=Reviewer.C, reason_code="REVIEWER_TIMEOUT")
    assert read_verdict(git_repo).reviewers["C"].attempt_count == 5
    directory = git_repo / ".review/attempts/round-1/C"
    assert {path.name for path in directory.iterdir()} == {
        "attempt-3.meta.json", "attempt-4.meta.json", "attempt-5.meta.json",
    }
    receipt = submit_reviewer_report(git_repo, reviewer=Reviewer.C,
                                    raw=json.dumps(report(pending.snapshot, "C")).encode(), now=NOW)
    slot = read_verdict(git_repo).reviewers["C"]
    assert slot.attempt_count == receipt.attempt == 6
    assert slot.receipt == receipt
    assert slot.status == "sealed"


def test_v2_pending_cumulative_attempt_count_roundtrips_above_three(git_repo):
    pending = write_current_pending(git_repo)
    pending = replace(pending, reviewers={**pending.reviewers,
        "C": ReviewerSlot("pending", attempt_count=5, last_error="REVIEWER_TIMEOUT")})
    write_json(git_repo / ".review/verdict.json", pending.to_json())
    assert read_verdict(git_repo).reviewers["C"].attempt_count == 5


def test_submit_unbounded_raw_input_preserves_verdict_without_evidence(git_repo):
    write_current_pending(git_repo)
    before = (git_repo / ".review/verdict.json").read_bytes()
    with pytest.raises(SchemaError, match="^REPORT_TOO_LARGE$"):
        submit_reviewer_report(git_repo, reviewer=Reviewer.C,
                               raw=b"x" * (MAX_REPORT_BYTES + 2), now=NOW)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert not (git_repo / ".review/attempts").exists()
    assert not (git_repo / ".review/inbox/round-1/C.json").exists()


def test_submit_report_write_failure_is_not_a_retryable_attempt(git_repo, monkeypatch):
    pending = write_current_pending(git_repo)
    before = (git_repo / ".review/verdict.json").read_bytes()
    raw = json.dumps(report(pending.snapshot, "A")).encode()

    real_write = os.write

    def fail_write(fd, data):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("injected report write failure")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(SchemaError, match="^REPORT_WRITE_FAILED$"):
        submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert not (git_repo / ".review/attempts").exists()
    assert not (git_repo / ".review/inbox/round-1/A.json").exists()


def test_submit_evidence_write_failure_preserves_all_slots(git_repo, monkeypatch):
    write_current_pending(git_repo)
    before = (git_repo / ".review/verdict.json").read_bytes()

    real_write = os.write

    def fail_write(fd, data):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("injected evidence write failure")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(SchemaError, match="^ATTEMPT_EVIDENCE_UNSAFE$"):
        submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=b"invalid", now=NOW)
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert not (git_repo / ".review/inbox/round-1/A.json").exists()


def test_reviewer_failure_verdict_publication_failure_preserves_prior_verdict_and_evidence(
    git_repo, monkeypatch
):
    write_current_pending(git_repo)
    before = (git_repo / ".review/verdict.json").read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("injected verdict rename failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(SchemaError, match="^VERDICT_WRITE_FAILED$"):
        record_reviewer_failure(git_repo, reviewer=Reviewer.B, reason_code="REVIEWER_TIMEOUT")
    assert (git_repo / ".review/verdict.json").read_bytes() == before
    assert json.loads((git_repo / ".review/attempts/round-1/B/attempt-1.meta.json").read_bytes()) == {
        "attempt": 1, "raw_sha256": None, "reason_code": "REVIEWER_TIMEOUT",
        "reviewer": "B", "round": 1,
    }


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


def test_new_verdict_owns_lifecycle_id_and_preserves_it_through_finalize(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW,
        token_hex=lambda size: "a" * (size * 2),
    )
    assert pending.schema == VERDICT_SCHEMA_VERSION == 3
    assert pending.lifecycle_id == "a" * 32
    assert pending.to_json()["lifecycle_id"] == "a" * 32
    paths = report_paths(git_repo, pending.snapshot)
    final = finalize_round(git_repo, reviewer_paths=paths, now=NOW)
    assert final.lifecycle_id == "a" * 32
    assert read_verdict(git_repo).lifecycle_id == "a" * 32


@pytest.mark.parametrize("value", (None, "A" * 32, "a" * 31, "g" * 32, 7))
def test_schema_three_rejects_invalid_lifecycle_id(git_repo, value):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW,
        token_hex=lambda _size: "b" * 32,
    )
    raw = pending.to_json()
    raw["lifecycle_id"] = value
    write_json(git_repo / ".review/verdict.json", raw)
    with pytest.raises(SchemaError, match="^VERDICT_INVALID$"):
        read_verdict(git_repo)


def test_schema_three_lifecycle_id_survives_failure_submit_and_finalize(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW,
        token_hex=lambda _size: "c" * 32,
    )
    record_reviewer_failure(
        git_repo, reviewer=Reviewer.A, reason_code="REVIEWER_TIMEOUT"
    )
    assert read_verdict(git_repo).lifecycle_id == "c" * 32
    paths = report_paths(git_repo, pending.snapshot)
    assert read_verdict(git_repo).lifecycle_id == "c" * 32
    final = finalize_round(git_repo, reviewer_paths=paths, now=NOW)
    assert final.lifecycle_id == "c" * 32


def test_verdict_schema_two_does_not_change_report_or_snapshot_schema(git_repo):
    legacy = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    pending = replace(
        legacy,
        schema=SLOT_VERDICT_SCHEMA_VERSION,
        contract=ContractBinding(
            REPORT_TEXT_CONTRACT_VERSION,
            DIFF_RECIPE_VERSION,
            SLOT_VERDICT_SCHEMA_VERSION,
        ),
        lifecycle_id=None,
    )
    assert pending.schema == SLOT_VERDICT_SCHEMA_VERSION == 2
    assert pending.snapshot.schema == SCHEMA_VERSION == 1
    assert all(slot.status == "pending" for slot in pending.reviewers.values())
    assert pending.contract.to_json() == {
        "report_text": REPORT_TEXT_CONTRACT_VERSION,
        "diff_recipe": DIFF_RECIPE_VERSION,
        "verdict_schema": SLOT_VERDICT_SCHEMA_VERSION,
    }
    assert pending.to_json()["reviewers"]["A"] == {
        "state": "pending",
        "attempt_count": 0,
        "last_error": None,
    }


def _schema_two_mixed_verdict(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A")).encode()
    parsed_report = parse_reviewer_report(
        raw,
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=pending.snapshot,
    )
    sealed = replace(
        pending,
        schema=SLOT_VERDICT_SCHEMA_VERSION,
        contract=ContractBinding(
            REPORT_TEXT_CONTRACT_VERSION,
            DIFF_RECIPE_VERSION,
            SLOT_VERDICT_SCHEMA_VERSION,
        ),
        lifecycle_id=None,
        reviewers={
            "A": ReviewerSlot(
                "sealed",
                report=parsed_report,
                receipt=ReportReceipt(
                    reviewer=Reviewer.A,
                    round=1,
                    path=".review/inbox/round-1/A.json",
                    raw_sha256=hashlib.sha256(raw).hexdigest(),
                    context_sha256="2" * 64,
                    report_contract_version=REPORT_TEXT_CONTRACT_VERSION,
                    attempt=1,
                    provenance="native_submit",
                ),
                attempt_count=1,
            ),
            "B": ReviewerSlot("pending", attempt_count=1, last_error="JSON_INVALID"),
            "C": ReviewerSlot("pending"),
        },
    )
    return sealed


def _v2_mixed_pending(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW,
        token_hex=lambda _size: "a" * 32,
    )
    raw = json.dumps(report(pending.snapshot, "A")).encode()
    submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    with pytest.raises(SchemaError, match="^JSON_INVALID$"):
        submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=b"{", now=NOW)
    native = read_verdict(git_repo)
    legacy = replace(
        native,
        schema=SLOT_VERDICT_SCHEMA_VERSION,
        contract=replace(native.contract, verdict_schema=SLOT_VERDICT_SCHEMA_VERSION),
        lifecycle_id=None,
    )
    write_json(git_repo / ".review/verdict.json", legacy.to_json())
    return legacy, raw


def test_migrate_v2_pending_preserves_authenticated_slots_and_attempts(git_repo):
    from pre_pr_tribunal import verdict_store

    legacy, raw = _v2_mixed_pending(git_repo)
    canonical = git_repo / ".review/inbox/round-1/A.json"

    result = verdict_store.migrate_v2_pending_round(
        git_repo, token_hex=lambda _size: "c" * 32,
    )

    migrated = read_verdict(git_repo)
    assert result.telemetry_history == "unknown"
    assert migrated.schema == 3
    assert migrated.lifecycle_id == "c" * 32
    assert migrated.reviewers["A"] == legacy.reviewers["A"]
    assert migrated.reviewers["B"].attempt_count == 1
    assert migrated.reviewers["B"].last_error == "JSON_INVALID"
    assert canonical.read_bytes() == raw
    assert stat.S_IMODE(canonical.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("tamper", "expected"),
    (
        ("snapshot", "SNAPSHOT_CHANGED"),
        ("report_bytes", "REPORT_BYTES_MISMATCH"),
        ("report_mode", "FILE_UNSAFE"),
        ("receipt_digest", "REPORT_BYTES_MISMATCH"),
        ("report_text_contract", "CONTRACT_DRIFT"),
        ("diff_recipe_contract", "CONTRACT_DRIFT"),
    ),
)
def test_migrate_v2_pending_refuses_without_mutating_verdict_or_report(
    git_repo, tamper, expected,
):
    from pre_pr_tribunal import verdict_store

    legacy, raw = _v2_mixed_pending(git_repo)
    canonical = git_repo / ".review/inbox/round-1/A.json"
    if tamper == "snapshot":
        commit_fix(git_repo)
    elif tamper == "report_bytes":
        canonical.write_bytes(raw + b"\n")
        canonical.chmod(0o600)
    elif tamper == "report_mode":
        canonical.chmod(0o644)
    elif tamper == "receipt_digest":
        slot = legacy.reviewers["A"]
        legacy = replace(
            legacy,
            reviewers={
                **legacy.reviewers,
                "A": replace(slot, receipt=replace(slot.receipt, raw_sha256="0" * 64)),
            },
        )
        write_json(git_repo / ".review/verdict.json", legacy.to_json())
    elif tamper == "report_text_contract":
        slot = legacy.reviewers["A"]
        legacy = replace(
            legacy,
            contract=replace(legacy.contract, report_text=99),
            reviewers={
                **legacy.reviewers,
                "A": replace(slot, receipt=replace(slot.receipt, report_contract_version=99)),
            },
        )
        write_json(git_repo / ".review/verdict.json", legacy.to_json())
    else:
        legacy = replace(legacy, contract=replace(legacy.contract, diff_recipe=99))
        write_json(git_repo / ".review/verdict.json", legacy.to_json())
    verdict_before = (git_repo / ".review/verdict.json").read_bytes()
    report_before = canonical.read_bytes()

    with pytest.raises(SchemaError, match=f"^{expected}$"):
        verdict_store.migrate_v2_pending_round(git_repo)

    assert (git_repo / ".review/verdict.json").read_bytes() == verdict_before
    assert canonical.read_bytes() == report_before


def test_cli_migrate_v2_pending_returns_stable_migration_projection(git_repo):
    _v2_mixed_pending(git_repo)

    result = run_cli_bytes(git_repo, "migrate-v2-pending")

    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout) == {
        "round": 1,
        "reviewers": {"A": "sealed", "B": "pending", "C": "pending"},
        "telemetry_history": "unknown",
    }


def test_schema_two_parser_accepts_mixed_pending_and_sealed_slots(git_repo):
    sealed = _schema_two_mixed_verdict(git_repo)
    verdict_path = write_json(git_repo / ".review/verdict.json", sealed.to_json())
    verdict_path.chmod(0o600)
    before = verdict_path.read_bytes()
    parsed = read_verdict(git_repo)
    assert parsed.lifecycle_id is None
    assert verdict_path.read_bytes() == before
    assert parsed.reviewers["A"].status == "sealed"
    assert parsed.reviewers["B"].last_error == "JSON_INVALID"
    assert parsed.reviewers["A"].receipt == sealed.reviewers["A"].receipt
    assert sealed.to_json()["reviewers"]["A"]["receipt"] == {
        "raw_sha256": sealed.reviewers["A"].receipt.raw_sha256,
        "context_sha256": "2" * 64,
        "report_contract_version": REPORT_TEXT_CONTRACT_VERSION,
        "attempt": 1,
        "provenance": "native_submit",
    }


@pytest.mark.parametrize(
    "mutation",
    (
        "sealed_without_report",
        "sealed_without_receipt",
        "pending_with_report",
        "invalid_provenance",
        "attempt_below_one",
        "attempt_count_mismatch",
        "pass_with_pending",
        "fail_with_pending",
        "in_progress_with_blocker",
    ),
)
def test_schema_two_parser_rejects_invalid_slot_and_gate_shapes(git_repo, mutation):
    payload = _schema_two_mixed_verdict(git_repo).to_json()
    if mutation == "sealed_without_report":
        del payload["reviewers"]["A"]["report"]
    elif mutation == "sealed_without_receipt":
        del payload["reviewers"]["A"]["receipt"]
    elif mutation == "pending_with_report":
        payload["reviewers"]["B"]["report"] = payload["reviewers"]["A"]["report"]
    elif mutation == "invalid_provenance":
        payload["reviewers"]["A"]["receipt"]["provenance"] = "manual"
    elif mutation == "attempt_below_one":
        payload["reviewers"]["A"]["receipt"]["attempt"] = 0
    elif mutation == "attempt_count_mismatch":
        payload["reviewers"]["A"]["attempt_count"] = 2
    elif mutation == "pass_with_pending":
        payload["gate"]["status"] = "pass"
    elif mutation == "fail_with_pending":
        payload["gate"].update(status="fail", blocking_count=1)
    else:
        payload["gate"]["blocking_count"] = 1
    write_json(git_repo / ".review/verdict.json", payload)
    with pytest.raises(SchemaError, match="^VERDICT_INVALID$"):
        read_verdict(git_repo)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("reviewer", Reviewer.B),
        ("round", 2),
        ("round", True),
        ("path", ".review/inbox/round-1/B.json"),
    ),
)
def test_schema_two_projection_rejects_receipt_binding_mismatch(
    git_repo, field, value
):
    verdict = _schema_two_mixed_verdict(git_repo)
    slot = verdict.reviewers["A"]
    mismatched = replace(slot.receipt, **{field: value})
    verdict = replace(
        verdict,
        reviewers={**verdict.reviewers, "A": replace(slot, receipt=mismatched)},
    )
    with pytest.raises(SchemaError, match="^VERDICT_INVALID$"):
        verdict.to_json()


def test_schema_one_terminal_verdict_remains_readable_without_rewrite(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    finalize_round(
        git_repo, reviewer_paths=report_paths(git_repo, pending.snapshot), now=NOW
    )
    path = git_repo / ".review/verdict.json"
    native = read_verdict(git_repo)
    legacy = replace(native, schema=1, contract=None, lifecycle_id=None,
                     reviewers={key: ReviewerSlot("complete", slot.report)
                                for key, slot in native.reviewers.items()})
    write_json(path, legacy.to_json())
    before = path.read_bytes()
    parsed = read_verdict(git_repo)
    assert parsed.schema == 1
    assert parsed.gate.status.value == "pass"
    assert path.read_bytes() == before


def test_schema_one_pending_is_readable_but_new_submit_requires_migration(git_repo):
    legacy_pending(git_repo)
    assert read_verdict(git_repo).schema == 1
    with pytest.raises(SchemaError, match="^LEGACY_ADOPTION_REQUIRED$"):
        require_current_in_progress(read_verdict(git_repo))


@pytest.mark.parametrize("mask", (0o000, 0o022, 0o077))
def test_store_reviewer_report_preserves_bytes_and_forces_mode(git_repo, mask):
    pending = begin_legacy_round(
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
    pending = begin_legacy_round(
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
    begin_legacy_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
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
    pending = begin_legacy_round(
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
    pending = begin_legacy_round(
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
    pending = begin_legacy_round(
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
def test_pending_slot_recovers_from_invalid_text_preserving_sealed_blockers(
    git_repo, control
):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    verdict_path = git_repo / ".review/verdict.json"
    invalid = finding()
    invalid["rationale"] = f"first line{control}second line"
    stale_blocker_b = finding(identifier="B-R1-001", reviewer="B")
    stale_execution_b = execution("B-R1-E001")
    stale_blocker_b["execution_ids"] = [stale_execution_b["id"]]
    stale_blocker_c = finding(identifier="C-R1-001", reviewer="C")
    with pytest.raises(SchemaError, match="TEXT_INVALID"):
        submit_reviewer_report(git_repo, reviewer=Reviewer.A,
                               raw=json.dumps(report(pending.snapshot, "A", findings=[invalid])).encode(), now=NOW)
    for key, value in (("B", report(pending.snapshot, "B", findings=[stale_blocker_b], executions=[stale_execution_b])),
                       ("C", report(pending.snapshot, "C", findings=[stale_blocker_c]))):
        submit_reviewer_report(git_repo, reviewer=Reviewer(key), raw=json.dumps(value).encode(), now=NOW)

    still_pending = read_verdict(git_repo)
    assert still_pending.gate.status.value == "in_progress"
    assert still_pending.snapshot == pending.snapshot
    assert still_pending.reviewers["A"].last_error == "TEXT_INVALID"
    submit_reviewer_report(git_repo, reviewer=Reviewer.A,
                           raw=json.dumps(report(pending.snapshot, "A")).encode(), now=NOW)
    recovered = finalize_round(git_repo, now=NOW)
    assert recovered.gate.status.value == "fail"
    assert recovered.gate.blocking_count == 2
    assert recovered.snapshot == pending.snapshot
    assert all(slot.status == "sealed" for slot in recovered.reviewers.values())


def test_empty_reports_pass_and_round_one_restart_resets_pending(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="claude", round_number=1, now=NOW
    )
    paths = report_paths(git_repo, pending.snapshot)
    # Compare the immediate predecessor: each submit also atomically replaces
    # verdict.json, and a previously freed inode may be reused by the filesystem.
    pending_inode = (git_repo / ".review/verdict.json").stat().st_ino
    final = finalize_round(
        git_repo, reviewer_paths=paths, now=NOW
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


@pytest.mark.parametrize("reviewer", "ABC")
@pytest.mark.parametrize("failure", ("malformed", "operational"))
def test_pass_restart_can_record_another_failure(git_repo, reviewer, failure):
    from pre_pr_tribunal.verdict_store import record_reviewer_failure
    first = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    with pytest.raises(SchemaError, match="^JSON_INVALID$"):
        submit_reviewer_report(git_repo, reviewer=Reviewer(reviewer), raw=b"{", now=NOW)
    report_paths(git_repo, first.snapshot)
    assert finalize_round(git_repo, now=NOW).gate.status.value == "pass"
    begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    if failure == "malformed":
        with pytest.raises(SchemaError, match="^JSON_INVALID$"):
            submit_reviewer_report(git_repo, reviewer=Reviewer(reviewer), raw=b"new invalid", now=NOW)
    else:
        record_reviewer_failure(git_repo, reviewer=Reviewer(reviewer), reason_code="REVIEWER_TIMEOUT")
    slot = read_verdict(git_repo).reviewers[reviewer]
    assert slot.attempt_count == 1
    assert slot.last_error == ("JSON_INVALID" if failure == "malformed" else "REVIEWER_TIMEOUT")


@pytest.mark.parametrize("last_round", (2, 3))
def test_later_pass_can_reuse_every_round_attempt_namespace(git_repo, last_round):
    from pre_pr_tribunal.verdict_store import record_reviewer_failure
    for cycle in range(2):
        decisions_path = None
        for round_number in range(1, last_round + 1):
            pending = begin_round(
                git_repo, base="master", runtime="codex", round_number=round_number,
                decisions_path=decisions_path, now=NOW,
            )
            for key in "ABC":
                assert pending.reviewers[key].attempt_count == 0
                record_reviewer_failure(git_repo, reviewer=Reviewer(key), reason_code="REVIEWER_TIMEOUT")
            findings = [finding(f"A-R{round_number}-001")] if round_number < last_round else []
            prior = [{"decision_id": f"D-R{round_number - 1}-A-001", "outcome": "accepted",
                      "replacement_finding_id": None}] if round_number > 1 else []
            report_paths(git_repo, pending.snapshot, round_number=round_number,
                         overrides={"A": {"findings": findings, "prior_decisions": prior}})
            final = finalize_round(git_repo, now=NOW)
            assert all(slot.receipt.attempt == slot.attempt_count == 2 for slot in final.reviewers.values())
            if round_number < last_round:
                assert final.gate.status.value == "fail"
                commit_fix(git_repo)
                value = {"id": f"D-R{round_number}-A-001",
                         "finding_ref": {"round": round_number, "id": f"A-R{round_number}-001", "reviewer": "A"},
                         "disposition": "fixed", "rationale": "Covered by regression test.",
                         "executions": [execution(f"D-R{round_number}-E001")]}
                decisions_path = write_json(git_repo / f".review/inbox/round-{round_number}/decisions.json", [value])
        assert final.gate.status.value == "pass"


def test_restart_rejects_unknown_attempt_target_before_changing_verdict_or_reports(git_repo):
    from pre_pr_tribunal.verdict_store import record_reviewer_failure
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    record_reviewer_failure(git_repo, reviewer=Reviewer.A, reason_code="REVIEWER_TIMEOUT")
    paths = report_paths(git_repo, pending.snapshot)
    finalize_round(git_repo, now=NOW)
    unknown = git_repo / ".review/attempts/round-1/unknown"
    unknown.write_bytes(b"must remain")
    unknown.chmod(0o600)
    protected = [*paths.values(), git_repo / ".review/verdict.json", unknown,
                 git_repo / ".review/attempts/round-1/A/attempt-1.meta.json"]
    before = {path: path.read_bytes() for path in protected}
    with pytest.raises(SchemaError, match="^ATTEMPT_EVIDENCE_UNSAFE$"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert {path: path.read_bytes() for path in protected} == before


@pytest.mark.parametrize("evidence_round", (1, 2, 3))
def test_initial_begin_preserves_unexpected_attempt_evidence_without_verdict(git_repo, evidence_round):
    from pre_pr_tribunal.attempt_store import append_attempt_evidence
    from pre_pr_tribunal.review_store import locked_review
    with locked_review(git_repo, create=True) as review_fd:
        evidence = append_attempt_evidence(review_fd, round_number=evidence_round, reviewer=Reviewer.C,
                                           sequence=1, raw=b"{", reason_code="JSON_INVALID")
    path = git_repo / evidence.raw_path
    with pytest.raises(SchemaError, match="^ATTEMPT_EVIDENCE_UNSAFE$"):
        begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    assert path.read_bytes() == b"{"
    assert not (git_repo / ".review/verdict.json").exists()


def test_round_one_replaces_owner_private_readonly_verdict(git_repo):
    """Checking a prior verdict's exact mode would reject safe owner-only state."""
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    report_paths(git_repo, pending.snapshot)
    finalize_round(git_repo, now=NOW)
    verdict_path = git_repo / ".review/verdict.json"
    verdict_path.chmod(0o400)

    restarted = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )

    assert restarted.gate.status.value == "in_progress"
    assert stat.S_IMODE(verdict_path.stat().st_mode) == 0o600


def test_verdict_persistence_failure_keeps_write_failure_code(git_repo, monkeypatch):
    """Mapping failed replacement persistence to file-unsafe breaks stable callers."""
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    report_paths(git_repo, pending.snapshot)
    finalize_round(git_repo, now=NOW)
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
    with pytest.raises(SchemaError, match="ROUND_NOT_READY"):
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
    legacy["schema"] = 1
    del legacy["contract"]
    del legacy["lifecycle_id"]
    legacy["reviewers"] = {key: slot["report"] for key, slot in legacy["reviewers"].items()}
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
    payload["reviewers"]["A"]["report"]["prior_decisions"] = []
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
        ([{"decision_id": "D-R1-B-001", "outcome": "accepted",
           "replacement_finding_id": None}], "PRIOR_DECISION_RESPONSE_MISSING"),
        ([{"decision_id": "D-R1-A-001", "outcome": "reissued",
           "replacement_finding_id": "A-R2-001"}], "REPLACEMENT_FINDING_INVALID"),
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
@pytest.mark.parametrize("orphan", (False, True))
def test_only_originating_reviewer_can_close_decision(git_repo, owner_payload, code, orphan):
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
    for key in "BC":
        submit_reviewer_report(git_repo, reviewer=Reviewer(key),
            raw=json.dumps(report(second.snapshot, key, round_number=2)).encode(), now=NOW)
    peers = read_verdict(git_repo).reviewers
    peer_bytes = {key: (git_repo / f".review/inbox/round-2/{key}.json").read_bytes()
                  for key in "BC"}
    raw = json.dumps(report(second.snapshot, "A", round_number=2,
                            prior_decisions=owner_payload)).encode() + b" \n"
    target = git_repo / ".review/inbox/round-2/A.json"
    if orphan:
        target.write_bytes(raw)
        target.chmod(0o600)
    before = (git_repo / ".review/verdict.json").read_bytes()
    with pytest.raises(SchemaError, match=f"^{code}$"):
        submit_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw, now=NOW)
    loaded = read_verdict(git_repo)
    assert loaded.reviewers["A"].status == "pending"
    assert all(loaded.reviewers[key] == peers[key] for key in "BC")
    assert all((git_repo / f".review/inbox/round-2/{key}.json").read_bytes() == peer_bytes[key]
               for key in "BC")
    if orphan:
        assert target.read_bytes() == raw
        assert (git_repo / ".review/verdict.json").read_bytes() == before
        assert not (git_repo / ".review/attempts/round-2/A").exists()
        return
    assert not target.exists()
    assert loaded.reviewers["A"].attempt_count == 1
    assert loaded.reviewers["A"].last_error == code
    assert (git_repo / ".review/attempts/round-2/A/attempt-1.raw").read_bytes() == raw
    corrected = report(second.snapshot, "A", round_number=2, prior_decisions=[{
        "decision_id": "D-R1-A-001", "outcome": "accepted", "replacement_finding_id": None}])
    receipt = submit_reviewer_report(git_repo, reviewer=Reviewer.A,
                                    raw=json.dumps(corrected).encode(), now=NOW)
    assert receipt.attempt == 2
    assert finalize_round(git_repo, now=NOW).gate.status.value == "pass"


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
        failed.stderr == "PRE_PR_TRIBUNAL:ROUND_NOT_READY\n"
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
        "verdict_schema": 3,
    }
    finalized_payload = json.loads(finalized.stdout)
    assert {key: finalized_payload[key] for key in expected} == expected
    assert set(finalized_payload) == {*expected, "reviewers"}
    assert all(slot["state"] == "sealed" for slot in finalized_payload["reviewers"].values())
    assert finalized.stderr == ""
    status = subprocess.run(
        [sys.executable, str(cli), "status"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(status.stdout) == finalized_payload and status.stderr == ""
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
    pending = begin_legacy_round(
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
    pending = begin_legacy_round(
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
    pending = begin_legacy_round(
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
    begin_legacy_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    oversize = b"{" + b" " * MAX_REPORT_BYTES + b"}"
    result = run_cli_bytes(git_repo, "store-report", "--reviewer", "A", input=oversize)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"PRE_PR_TRIBUNAL:REPORT_TOO_LARGE\n"
    assert not (git_repo / ".review/inbox/round-1/A.json").exists()


def test_cli_recovery_flag_replaces_only_explicit_fresh_panel_and_normal_refuses(git_repo):
    pending = begin_legacy_round(
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
    pending = begin_legacy_round(
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


def test_cli_submit_seals_one_slot_and_status_exposes_no_report_body(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = (
        json.dumps(
            report(pending.snapshot, "A", findings=[finding()]),
            separators=(",", ":"),
        ).encode()
        + b"\r\n"
    )

    submitted = run_cli_bytes(
        git_repo, "submit-report", "--reviewer", "A", input=raw
    )

    assert submitted.returncode == 0 and submitted.stderr == b""
    assert json.loads(submitted.stdout) == {
        "reviewer": "A",
        "round": 1,
        "state": "sealed",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "context_sha256": context_sha256(pending, Reviewer.A),
        "report_contract_version": REPORT_TEXT_CONTRACT_VERSION,
        "attempt": 1,
        "provenance": "native_submit",
    }
    assert submitted.stdout.count(b"\n") == 1
    assert len(submitted.stdout) < 1024
    assert (git_repo / ".review/inbox/round-1/A.json").read_bytes() == raw
    status_result = run_cli_bytes(git_repo, "status")
    status = json.loads(status_result.stdout)
    assert status_result.returncode == 0 and status_result.stderr == b""
    assert status["verdict_schema"] == 3
    assert status["reviewers"] == {
        "A": {
            "state": "sealed",
            "attempt_count": 1,
            "last_error": None,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "context_sha256": context_sha256(pending, Reviewer.A),
            "report_contract_version": REPORT_TEXT_CONTRACT_VERSION,
            "provenance": "native_submit",
        },
        "B": {"state": "pending", "attempt_count": 0, "last_error": None},
        "C": {"state": "pending", "attempt_count": 0, "last_error": None},
    }
    assert status_result.stdout.count(b"\n") == 1
    assert len(status_result.stdout) < 2048
    assert b"findings" not in status_result.stdout
    assert b"Incorrect boundary" not in status_result.stdout


def test_cli_submit_parser_failure_updates_only_requested_slot(git_repo):
    begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    before = read_verdict(git_repo)

    failed = run_cli_bytes(
        git_repo, "submit-report", "--reviewer", "B", input=b'{"schema":1'
    )

    assert (failed.returncode, failed.stdout, failed.stderr) == (
        1,
        b"",
        b"PRE_PR_TRIBUNAL:JSON_INVALID\n",
    )
    assert len(failed.stderr) < 128
    status = json.loads(run_cli_bytes(git_repo, "status").stdout)
    assert status["reviewers"] == {
        "A": {"state": "pending", "attempt_count": 0, "last_error": None},
        "B": {
            "state": "pending",
            "attempt_count": 1,
            "last_error": "JSON_INVALID",
        },
        "C": {"state": "pending", "attempt_count": 0, "last_error": None},
    }
    assert read_verdict(git_repo).reviewers["A"] == before.reviewers["A"]
    assert read_verdict(git_repo).reviewers["C"] == before.reviewers["C"]


def test_cli_record_failure_returns_exact_cumulative_pending_projection(git_repo):
    begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)

    first = run_cli_bytes(
        git_repo,
        "record-failure",
        "--reviewer",
        "C",
        "--reason",
        "DISPATCH_FAILED",
    )
    second = run_cli_bytes(
        git_repo,
        "record-failure",
        "--reviewer",
        "C",
        "--reason",
        "REVIEWER_TIMEOUT",
    )

    assert json.loads(first.stdout) == {
        "state": "pending",
        "attempt_count": 1,
        "last_error": "DISPATCH_FAILED",
    }
    assert json.loads(second.stdout) == {
        "state": "pending",
        "attempt_count": 2,
        "last_error": "REVIEWER_TIMEOUT",
    }
    for result in (first, second):
        assert result.returncode == 0 and result.stderr == b""
        assert result.stdout.count(b"\n") == 1
        assert len(result.stdout) < 256


@pytest.mark.parametrize(
    "arguments",
    (
        ("submit-report", "--reviewer", "D"),
        ("submit-report",),
        ("record-failure", "--reviewer", "A", "--reason", "JSON_INVALID"),
        ("record-failure", "--reviewer", "D", "--reason", "REVIEWER_FAILED"),
        ("record-failure", "--reviewer", "A"),
        ("migrate-legacy-pending", "--reviewer", "A"),
    ),
)
def test_cli_selective_recovery_usage_is_exact_bounded_exit_two(git_repo, arguments):
    result = run_cli_bytes(git_repo, *arguments)
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        b"",
        b"PRE_PR_TRIBUNAL:USAGE\n",
    )


def test_cli_context_rejects_a_sealed_slot_but_keeps_peers_available(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode()
    assert run_cli_bytes(
        git_repo, "submit-report", "--reviewer", "A", input=raw
    ).returncode == 0

    sealed = run_cli_bytes(git_repo, "context", "--reviewer", "A")
    peer = run_cli_bytes(git_repo, "context", "--reviewer", "B")

    assert (sealed.returncode, sealed.stdout, sealed.stderr) == (
        1,
        b"",
        b"PRE_PR_TRIBUNAL:REVIEWER_SLOT_SEALED\n",
    )
    assert peer.returncode == 0 and json.loads(peer.stdout)["reviewer"] == "B"
    assert len(peer.stdout) <= MAX_REPORT_BYTES


def test_cli_migrate_legacy_pending_reports_all_three_stable_states(git_repo):
    pending = legacy_pending(git_repo)
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode()
    store_reviewer_report(git_repo, reviewer=Reviewer.A, raw=raw)
    store_reviewer_report(git_repo, reviewer=Reviewer.B, raw=b'{"schema":1')

    migrated = run_cli_bytes(git_repo, "migrate-legacy-pending")

    assert migrated.returncode == 0 and migrated.stderr == b""
    assert json.loads(migrated.stdout) == {
        "round": 1,
        "reviewers": {
            "A": "pending:LEGACY_PROVENANCE_UNAVAILABLE",
            "B": "pending:JSON_INVALID",
            "C": "pending:REVIEWER_REPORT_MISSING",
        },
    }
    assert migrated.stdout.count(b"\n") == 1
    assert len(migrated.stdout) < 512


def test_cli_status_discriminates_all_pending_v1_from_v2(git_repo):
    native = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    native_status = json.loads(run_cli_bytes(git_repo, "status").stdout)
    legacy = replace(
        native,
        schema=1,
        contract=None,
        lifecycle_id=None,
        reviewers={key: ReviewerSlot("pending") for key in "ABC"},
    )
    write_json(git_repo / ".review/verdict.json", legacy.to_json())
    legacy_status = json.loads(run_cli_bytes(git_repo, "status").stdout)

    assert native_status["verdict_schema"] == 3
    assert native_status["reviewers"] == {
        key: {"state": "pending", "attempt_count": 0, "last_error": None}
        for key in "ABC"
    }
    assert legacy_status == {
        "round": 1,
        "gate_status": "in_progress",
        "blocking_count": 0,
        "verdict_path": ".review/verdict.json",
        "verdict_schema": 1,
    }


def test_cli_finalize_without_paths_authenticates_all_sealed_reports(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    for reviewer in "ABC":
        raw = json.dumps(
            report(pending.snapshot, reviewer), separators=(",", ":")
        ).encode()
        assert run_cli_bytes(
            git_repo, "submit-report", "--reviewer", reviewer, input=raw
        ).returncode == 0

    finalized = run_cli_bytes(git_repo, "finalize")

    assert finalized.returncode == 0 and finalized.stderr == b""
    payload = json.loads(finalized.stdout)
    assert payload["gate_status"] == "pass"
    assert payload["verdict_schema"] == 3
    assert all(slot["state"] == "sealed" for slot in payload["reviewers"].values())
    assert finalized.stdout.count(b"\n") == 1
    assert len(finalized.stdout) < 4096


@pytest.mark.parametrize(
    "arguments",
    (
        ("--reviewer-a", ".review/inbox/round-1/A.json"),
        (
            "--reviewer-a",
            ".review/inbox/round-1/A.json",
            "--reviewer-b",
            ".review/inbox/round-1/B.json",
        ),
    ),
)
def test_cli_finalize_rejects_partial_legacy_path_sets_as_usage(git_repo, arguments):
    result = run_cli_bytes(git_repo, "finalize", *arguments)
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        b"",
        b"PRE_PR_TRIBUNAL:USAGE\n",
    )


def test_cli_validate_stored_supports_pending_orphan_and_sealed_receipt(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    raw = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode()
    target = git_repo / ".review/inbox/round-1/A.json"
    target.write_bytes(raw)
    target.chmod(0o600)

    pending_validation = run_cli_bytes(
        git_repo, "validate-report", "--reviewer", "A", "--source", "stored"
    )
    assert json.loads(pending_validation.stdout)["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    target.unlink()
    submitted = run_cli_bytes(
        git_repo, "submit-report", "--reviewer", "A", input=raw
    )
    assert submitted.returncode == 0
    sealed_validation = run_cli_bytes(
        git_repo, "validate-report", "--reviewer", "A", "--source", "stored"
    )
    assert sealed_validation.returncode == 0 and sealed_validation.stderr == b""
    assert json.loads(sealed_validation.stdout) == {
        "reviewer": "A",
        "round": 1,
        "status": "valid",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }
    target.write_bytes(raw + b" ")
    drifted = run_cli_bytes(
        git_repo, "validate-report", "--reviewer", "A", "--source", "stored"
    )
    assert (drifted.returncode, drifted.stdout, drifted.stderr) == (
        1,
        b"",
        b"PRE_PR_TRIBUNAL:REPORT_BYTES_MISMATCH\n",
    )


def test_cli_store_report_rejects_v2_before_touching_canonical(git_repo):
    pending = begin_round(
        git_repo, base="master", runtime="codex", round_number=1, now=NOW
    )
    target = git_repo / ".review/inbox/round-1/A.json"
    sentinel = json.dumps(report(pending.snapshot, "A"), separators=(",", ":")).encode()
    target.write_bytes(sentinel)
    target.chmod(0o600)
    verdict_before = (git_repo / ".review/verdict.json").read_bytes()

    result = run_cli_bytes(
        git_repo,
        "store-report",
        "--reviewer",
        "A",
        "--replace-pending-recovery",
        input=b"replacement",
    )

    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        b"",
        b"PRE_PR_TRIBUNAL:LEGACY_COMMAND_NOT_ALLOWED\n",
    )
    assert target.read_bytes() == sentinel
    assert (git_repo / ".review/verdict.json").read_bytes() == verdict_before
