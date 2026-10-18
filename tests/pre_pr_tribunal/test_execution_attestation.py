"""In a Claude round, Reviewer B's fresh executions cite only digests its own tool calls printed."""

import hashlib
import json

import pytest

from pre_pr_tribunal.model import Reviewer, ReviewerTranscript, SchemaError
from pre_pr_tribunal.verdict_store import begin_round, read_verdict, submit_reviewer_report
from tests.pre_pr_tribunal.test_evidence_lifecycle import bundle, reused
from tests.pre_pr_tribunal.test_model_store import NOW, execution, report, run_cli_bytes
from tests.pre_pr_tribunal.test_reviewer_response import (
    AGENT, assistant, follow_up, prompt, text, write_transcript,
)

PRINTED = hashlib.sha256(b"1 passed").hexdigest()


def bash(tool_id, command):
    return {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}


def result(tool_id, content):
    return {
        "type": "user", "agentId": AGENT, "isSidechain": True, "uuid": f"r-{tool_id}",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": content},
        ]},
    }


def b_report(snapshot, digest=PRINTED, command="python3 -m pytest -q", evidence=None):
    item = evidence or {**execution("B-R1-E001", command=command), "capture_sha256": digest}
    return json.dumps(report(snapshot, "B", executions=[item], claims=[{
        "id": "B-R1-C001",
        "statement": "The reviewed behavior is executable.",
        "result": "supported",
        "execution_ids": [item["id"]],
        "reason": "",
    }], coverage={"complete": True, "primary_entry_paths": []}), ensure_ascii=False)


def reviewer_transcript(tmp_path, reply, *, printed=PRINTED, command=None):
    """A reviewer that ran one command whose output shows `printed`, then replied."""
    run = command or "python3 -m pytest -q > out 2>&1; echo rc=$?; sha256sum out"
    return write_transcript(tmp_path / f"agent-{AGENT}.jsonl", [
        prompt("review"),
        assistant("m1", [bash("t1", run)], "tool_use"),
        result("t1", f"rc=0\n{printed}  out\n"),
        assistant("m2", [text(reply)], "end_turn"),
    ])


def submit(repo, raw, transcript=None, reviewer="B"):
    arguments = ["submit-report", "--reviewer", reviewer]
    if transcript is not None:
        arguments += ["--transcript", str(transcript), "--agent", AGENT]
    return run_cli_bytes(repo, *arguments, input=raw)


def claude_round(repo):
    return begin_round(repo, base="master", runtime="claude", round_number=1, now=NOW)


def test_a_digest_its_own_command_printed_seals(git_repo, tmp_path):
    pending = claude_round(git_repo)
    reply = b_report(pending.snapshot)

    submitted = submit(git_repo, reply.encode(), reviewer_transcript(tmp_path, reply))

    assert submitted.returncode == 0, submitted.stderr
    assert read_verdict(git_repo).reviewers["B"].status == "sealed"


@pytest.mark.parametrize(("digest", "command"), (
    # Run 57959256/36368b82: two-character digests and a command that cannot run.
    ("0" * 63 + "1", "python3 -I -c (direct call of catalog_warnings on synthetic homes)"),
    ("0" * 63 + "2", "python3 -I -c <tmp-home-with-symlink-loop-fifo-and-unreadable-SKILL.md>"),
    # Uses all sixteen hex characters, so no shape check rejects it.
    ("1e0c3b7a5d9f4b2c8a6e" + "0123456789abcdef" * 2 + "4f6a8c0e1b3d", "python3 -m pytest -q"),
), ids=("prose-command", "placeholder-command", "plausible-shape"))
def test_a_digest_no_tool_call_printed_is_rejected_for_retry(git_repo, tmp_path, digest, command):
    pending = claude_round(git_repo)
    reply = b_report(pending.snapshot, digest=digest, command=command)

    submitted = submit(git_repo, reply.encode(), reviewer_transcript(tmp_path, reply))

    assert submitted.returncode == 1
    assert submitted.stderr.splitlines() == [
        b"PRE_PR_TRIBUNAL:EXECUTION_UNATTESTED",
        b"PRE_PR_TRIBUNAL_DETAIL:reviewer=B path=executions[0].capture_sha256",
    ]
    slot = read_verdict(git_repo).reviewers["B"]
    assert slot.status == "pending" and slot.last_error == "EXECUTION_UNATTESTED"
    assert not (git_repo / ".review/inbox/round-1/B.json").exists()


def test_a_digest_printed_before_a_format_retry_still_attests(git_repo, tmp_path):
    # The retried reply reuses a digest the reviewer computed before the follow-up.
    pending = claude_round(git_repo)
    reply = b_report(pending.snapshot)
    transcript = write_transcript(tmp_path / f"agent-{AGENT}.jsonl", [
        prompt("review"),
        assistant("m1", [bash("t1", "python3 -m pytest -q > out; sha256sum out")], "tool_use"),
        result("t1", [{"type": "text", "text": f"{PRINTED}  out"}]),
        assistant("m2", [text("not json")], "end_turn"),
        follow_up("retry with the format fixed"),
        assistant("m3", [text(reply)], "end_turn"),
    ])

    submitted = submit(git_repo, reply.encode(), transcript)

    assert submitted.returncode == 0, submitted.stderr


@pytest.mark.parametrize(("command", "output"), (
    (f"echo {PRINTED}", f"{PRINTED}\n"),
    ("sha512sum out", f"{PRINTED}{'a' * 64}  out\n"),
), ids=("the-call-contains-it", "inside-a-longer-hex-run"))
def test_output_that_does_not_show_a_computed_digest_does_not_attest(git_repo, tmp_path, command, output):
    pending = claude_round(git_repo)
    reply = b_report(pending.snapshot)
    transcript = write_transcript(tmp_path / f"agent-{AGENT}.jsonl", [
        prompt("review"),
        assistant("m1", [bash("t1", command)], "tool_use"),
        result("t1", output),
        assistant("m2", [text(reply)], "end_turn"),
    ])

    submitted = submit(git_repo, reply.encode(), transcript)

    assert submitted.stderr.splitlines()[0] == b"PRE_PR_TRIBUNAL:EXECUTION_UNATTESTED"


def test_a_claude_reviewer_b_submission_requires_its_transcript(git_repo):
    pending = claude_round(git_repo)

    submitted = submit(git_repo, b_report(pending.snapshot).encode())

    assert submitted.stderr == b"PRE_PR_TRIBUNAL:SUBMISSION_TRANSCRIPT_REQUIRED\n"
    slot = read_verdict(git_repo).reviewers["B"]
    assert slot.status == "pending" and slot.attempt_count == 0


def test_submitted_bytes_must_be_the_reply_in_that_transcript(git_repo, tmp_path):
    pending = claude_round(git_repo)
    reply = b_report(pending.snapshot)

    submitted = submit(git_repo, (reply + "\n").encode(), reviewer_transcript(tmp_path, reply))

    assert submitted.stderr == b"PRE_PR_TRIBUNAL:SUBMISSION_TRANSCRIPT_MISMATCH\n"
    slot = read_verdict(git_repo).reviewers["B"]
    assert slot.status == "pending" and slot.attempt_count == 0


@pytest.mark.parametrize(("runtime", "reviewer"), (("codex", "B"), ("claude", "A")))
def test_a_transcript_is_refused_where_no_check_uses_it(git_repo, tmp_path, runtime, reviewer):
    pending = begin_round(git_repo, base="master", runtime=runtime, round_number=1, now=NOW)
    raw = b_report(pending.snapshot) if reviewer == "B" else json.dumps(report(pending.snapshot, "A"))

    submitted = submit(git_repo, raw.encode(), reviewer_transcript(tmp_path, raw), reviewer=reviewer)

    assert submitted.stderr == b"PRE_PR_TRIBUNAL:SUBMISSION_TRANSCRIPT_UNEXPECTED\n"


def test_a_codex_round_keeps_unchecked_fresh_executions(git_repo):
    # Codex reviewers' tool calls live in child rollouts this check does not read yet.
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    unprinted = "1e0c3b7a5d9f4b2c8a6e" + "0123456789abcdef" * 2 + "4f6a8c0e1b3d"

    submitted = submit(git_repo, b_report(pending.snapshot, digest=unprinted).encode())

    assert submitted.returncode == 0, submitted.stderr


def test_a_bundle_execution_is_authenticated_by_its_bundle_not_the_transcript(git_repo):
    frozen = bundle(git_repo)
    pending = begin_round(
        git_repo, base="master", runtime="claude", round_number=1,
        evidence_bundle_sha256=frozen["bundle_sha256"],
    )
    raw = b_report(pending.snapshot, evidence=reused(git_repo, frozen)).encode()

    receipt = submit_reviewer_report(
        git_repo, reviewer=Reviewer.B, raw=raw,
        transcript=ReviewerTranscript(reply=raw, printed_digests=frozenset()),
    )

    assert receipt.raw_sha256 == hashlib.sha256(raw).hexdigest()


def test_an_unattested_fresh_execution_beside_a_bundle_one_is_still_rejected(git_repo):
    frozen = bundle(git_repo)
    pending = begin_round(
        git_repo, base="master", runtime="claude", round_number=1,
        evidence_bundle_sha256=frozen["bundle_sha256"],
    )
    value = json.loads(b_report(pending.snapshot, evidence=reused(git_repo, frozen)))
    value["executions"].append(execution("B-R1-E002"))
    value["claims"][0]["execution_ids"].append("B-R1-E002")
    raw = json.dumps(value).encode()

    with pytest.raises(SchemaError, match="^EXECUTION_UNATTESTED$") as raised:
        submit_reviewer_report(
            git_repo, reviewer=Reviewer.B, raw=raw,
            transcript=ReviewerTranscript(reply=raw, printed_digests=frozenset()),
        )
    assert raised.value.path == "executions[1].capture_sha256"


@pytest.mark.parametrize("given", ("--transcript", "--agent"))
def test_transcript_and_agent_are_given_together(git_repo, tmp_path, given):
    claude_round(git_repo)
    value = str(tmp_path / "t.jsonl") if given == "--transcript" else AGENT

    submitted = run_cli_bytes(git_repo, "submit-report", "--reviewer", "B", given, value, input=b"{}")

    assert submitted.returncode == 2
    assert read_verdict(git_repo).reviewers["B"].attempt_count == 0
