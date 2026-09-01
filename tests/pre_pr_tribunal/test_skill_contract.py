import json
from pathlib import Path
import re

from pre_pr_tribunal.model import Reviewer, Snapshot, parse_decisions, parse_reviewer_report


ROOT = Path(__file__).resolve().parents[2] / "skills" / "pre-pr-tribunal"
REFERENCES = (
    "references/reviewer-a.md",
    "references/reviewer-b.md",
    "references/reviewer-c.md",
    "references/report-schema.md",
)
REPORT_KEYS = {
    "schema",
    "reviewer",
    "round",
    "snapshot",
    "status",
    "findings",
    "executions",
    "claims",
    "prior_decisions",
}
CLI = '/usr/bin/python3 "$HOME/.local/share/claude-config/pre_pr_tribunal/cli.py"'
SNAPSHOT = Snapshot(
    schema=1,
    repository="owner/repository",
    base_ref="main",
    base_sha="0" * 40,
    head_sha="1" * 40,
    merge_base_sha="0" * 40,
    diff_sha256="a" * 64,
    paths=(),
    initial_paths=("src/example.py",),
    created_at="2026-09-01T00:00:00Z",
)


def text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def json_example(markdown: str, label: str):
    pattern = rf"<!-- {re.escape(label)} -->\s*```json\s*(.*?)\s*```"
    match = re.search(pattern, markdown, re.DOTALL)
    assert match is not None, f"missing JSON example: {label}"
    return json.loads(match.group(1))


def numbered_steps(skill: str) -> dict[int, str]:
    matches = list(re.finditer(r"(?m)^(\d+)\. (.*?)(?=^\d+\. |\Z)", skill, re.DOTALL))
    return {int(match.group(1)): match.group(2) for match in matches}


def test_one_shared_skill_has_valid_front_matter_and_explicit_references():
    skill = text("SKILL.md")
    front_matter = re.match(r"\A---\n(.*?)\n---\n", skill, re.DOTALL)
    assert front_matter is not None
    assert re.search(r"(?m)^name:\s*pre-pr-tribunal\s*$", front_matter.group(1))
    assert not (ROOT.parent / "pre-pr-tribunal-claude").exists()
    assert not (ROOT.parent / "pre-pr-tribunal-codex").exists()
    for reference in REFERENCES:
        assert reference in skill


def test_skill_encodes_the_exact_ordered_ten_step_state_machine():
    skill = text("SKILL.md")
    steps = numbered_steps(skill)
    assert list(steps) == list(range(1, 11))
    assert all(reference in steps[1] for reference in REFERENCES)
    assert "clean worktree" in steps[2] and "no other writer" in steps[2]
    assert "codex features list" in steps[2]
    assert "hooks" in steps[2] and "multi_agent" in steps[2]
    assert "Agent" in steps[2]
    assert "begin --base" in steps[3] and "--decisions" in steps[3]
    assert CLI in steps[3]
    assert CLI in steps[4]
    assert "context --reviewer A" in steps[4]
    assert "context --reviewer B" in steps[4]
    assert "context --reviewer C" in steps[4]
    assert "exactly three" in steps[5] and "parallel" in steps[5]
    assert "peer" in steps[5]
    assert "all three" in steps[6] and "terminal" in steps[6]
    assert "malformed" in steps[6] and "non-pass" in steps[6]
    assert "only after" in steps[7] and "terminal" in steps[7]
    assert "finalize --reviewer-a" in steps[7]
    assert CLI in steps[7]
    assert "no decisions" in steps[7]
    assert "never execute" in steps[8] and "verbatim" in steps[8]
    assert "initial_paths" in steps[9] and "review-fix round N" in steps[9]
    assert "one decision" in steps[10] and "round 4" in steps[10]
    assert "gh pr create" in steps[10]


def test_runtime_dispatch_is_native_parallel_and_does_not_invent_omx_authority():
    skill = text("SKILL.md")
    assert "Claude" in skill and "Agent" in skill
    assert "Codex" in skill and "collaboration.spawn_agent" in skill
    assert "foreground" in skill
    assert "agent_type" not in skill
    assert "OMX" not in skill


def test_later_round_context_is_originating_reviewer_only():
    skill = text("SKILL.md")
    isolation = re.search(
        r"<!-- isolation-contract -->(.*?)<!-- isolation-contract-end -->",
        skill,
        re.DOTALL,
    )
    assert isolation is not None
    body = isolation.group(1)
    for reviewer in "ABC":
        assert (
            f"Reviewer {reviewer} receives only Reviewer {reviewer}'s prior findings "
            f"and decisions"
        ) in body
    assert "Do not send any peer report" in body
    assert "controlling session" in body and "writes" in body


def test_skill_enforces_fix_rebuttal_boundaries_and_three_round_stop():
    skill = text("SKILL.md")
    for token in (
        "Reviewer A",
        "Reviewer B",
        "Reviewer C",
        "병렬",
        "peer",
        "최대 3",
        "CRITICAL",
        "HIGH",
        "새 dependency",
        "secret",
    ):
        assert token in skill
    assert "fixed" in skill and "rebutted" in skill
    assert "originating reviewer" in skill


def test_each_reviewer_prompt_is_read_only_self_contained_and_exactly_bounded():
    expected_mandates = {
        "references/reviewer-a.md": ("correctness", "security"),
        "references/reviewer-b.md": ("실행", "증명"),
        "references/reviewer-c.md": ("더 작은", "범위"),
    }
    for name, mandates in expected_mandates.items():
        body = text(name)
        assert "source를 수정하지" in body
        assert "report-schema.md 없이도" in body
        assert "128 KiB" in body
        assert "8 KiB" in body
        assert "4 KiB" in body
        assert "128 findings" in body
        assert "128 executions" in body
        assert all(f'"{key}"' in body for key in REPORT_KEYS)
        assert all(mandate in body for mandate in mandates)


def test_empirical_reviewer_forbids_unsupported_claims_and_requires_capture_fields():
    reviewer = text("references/reviewer-b.md")
    for token in (
        "추론만으로",
        "command",
        "exit_code",
        "stdout_excerpt",
        "stderr_excerpt",
        "capture_sha256",
        "truncated",
        "unverified",
    ):
        assert token in reviewer


def test_documented_report_and_decision_examples_pass_the_real_strict_parsers():
    schema = text("references/report-schema.md")
    for label, reviewer in (
        ("valid-reviewer-a", Reviewer.A),
        ("valid-reviewer-b", Reviewer.B),
        ("valid-reviewer-c", Reviewer.C),
    ):
        value = json_example(schema, label)
        assert set(value) == REPORT_KEYS
        parsed = parse_reviewer_report(
            json.dumps(value).encode(),
            expected_reviewer=reviewer,
            expected_round=1,
            snapshot=SNAPSHOT,
        )
        assert parsed.reviewer is reviewer

    for label in ("valid-fixed-decision", "valid-rebutted-decision"):
        parsed = parse_decisions(
            json.dumps(json_example(schema, label)).encode(),
            prior_blockers=("A-R1-001",),
        )
        assert len(parsed) == 1
        assert parsed[0].disposition == label.removeprefix("valid-").removesuffix("-decision")


def test_each_reviewer_carries_a_standalone_parser_valid_empty_report():
    for name, reviewer in (
        ("references/reviewer-a.md", Reviewer.A),
        ("references/reviewer-b.md", Reviewer.B),
        ("references/reviewer-c.md", Reviewer.C),
    ):
        value = json_example(text(name), "standalone-empty-report")
        parsed = parse_reviewer_report(
            json.dumps(value).encode(),
            expected_reviewer=reviewer,
            expected_round=1,
            snapshot=SNAPSHOT,
        )
        assert parsed.findings == ()
