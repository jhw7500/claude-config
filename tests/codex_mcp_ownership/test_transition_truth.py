import pytest

from codex_mcp_ownership.transition_truth import (
    RecoveryContradiction,
    RecoveryDecision,
    RecoveryEvidence,
    decide_recovery,
    derive_transition_id,
)


@pytest.mark.parametrize(
    ("current", "phase", "has_receipt", "expected"),
    [
        ("e", "prepared", False, RecoveryDecision.DISCARD_PREPARED),
        ("u", "prepared", False, RecoveryDecision.FINALIZE_UPDATED),
        ("u", "committed", False, RecoveryDecision.FINALIZE_UPDATED),
        ("u", "prepared", True, RecoveryDecision.ALREADY_RECEIPTED),
        ("u", "committed", True, RecoveryDecision.ALREADY_RECEIPTED),
    ],
)
def test_recovery_truth_table_accepts_only_consistent_states(
    current, phase, has_receipt, expected
):
    evidence = RecoveryEvidence(
        phase=phase,
        current_digest=current,
        expected_digest="e",
        updated_digest="u",
        has_matching_receipt=has_receipt,
    )
    assert decide_recovery(evidence) is expected


@pytest.mark.parametrize(
    ("current", "phase", "has_receipt"),
    [
        ("e", "committed", False),
        ("e", "committed", True),
        ("e", "prepared", True),
        ("third", "prepared", False),
        ("third", "prepared", True),
        ("third", "committed", False),
        ("third", "committed", True),
    ],
)
def test_recovery_truth_table_rejects_semantic_contradictions(
    current, phase, has_receipt
):
    evidence = RecoveryEvidence(
        phase=phase,
        current_digest=current,
        expected_digest="e",
        updated_digest="u",
        has_matching_receipt=has_receipt,
    )
    with pytest.raises(RecoveryContradiction):
        decide_recovery(evidence)


def test_recovery_truth_table_rejects_same_digest_and_unknown_phase():
    same = RecoveryEvidence("prepared", "e", "e", "e", False)
    unknown = RecoveryEvidence("invented", "e", "e", "u", False)
    with pytest.raises(RecoveryContradiction):
        decide_recovery(same)
    with pytest.raises(RecoveryContradiction):
        decide_recovery(unknown)


def test_transition_id_matches_schema_one_canonical_fixture():
    event = {
        "schema_version": 1,
        "event": "owner_loss_observed",
        "reason_codes": ["owner_session_ended"],
    }
    assert (
        derive_transition_id("processes", "1" * 64, "2" * 64, "3" * 64, event)
        == "c18cca6e7e8ef9ab2b68480c5ea2fe384d59552eb26089ebc5b2aa63d293e44a"
    )
