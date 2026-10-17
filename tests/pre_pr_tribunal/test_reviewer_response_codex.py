"""`reviewer-response --runtime codex` takes a reviewer's final answer from the controller's own rollout."""

import hashlib
import json
import os
import stat

import pytest

from pre_pr_tribunal import cli
from pre_pr_tribunal.verdict_store import begin_round
from tests.pre_pr_tribunal.test_model_store import NOW, finding, report, run_cli_bytes
from tests.pre_pr_tribunal.test_reviewer_response import TERMINAL, write_transcript


THREAD = "01a143ed-0000-7000-8000-79bb8a2fa277"
AGENT_ID = "01a143ed-0000-7000-8000-0000000000aa"
OTHER_THREAD = "01a143ed-0000-7000-8000-000000000000"
AGENT = "reviewer_a"
OTHER_AGENT = "reviewer_b"
AGENT_ERROR = (
    "Agent errored: Selected model is at capacity. Please try a different model.\n\n"
    "This agent's turn failed. If you still need this agent, use the available collaboration "
    "tools to give it another task."
)


def session_meta(thread=THREAD, source="exec"):
    return {"timestamp": "2026-10-16T09:00:00Z", "type": "session_meta",
            "payload": {"id": thread, "session_id": thread, "source": source, "cwd": "/fixture"}}


def call(name, arguments, call_id="c1"):
    return {"type": "response_item", "payload": {
        "type": "function_call", "status": "completed", "name": name, "namespace": "collaboration",
        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        "call_id": call_id,
    }}


def spawn(agent=AGENT):
    return call("spawn_agent", {"task_name": agent, "agent_type": "default", "fork_turns": "none",
                                "message": "review"})


def follow_up(target=f"/root/{AGENT}"):
    return call("followup_task", {"target": target, "message": "retry with the format fixed"})


def envelope(sender):
    return f"Message Type: FINAL_ANSWER\nTask name: /root\nSender: {sender}\nPayload:\n"


def final_answer(payload, agent=AGENT, *, recipient="/root", text=None, blocks=None):
    author = f"/root/{agent}"
    text = envelope(author) + payload if text is None else text
    return {"type": "response_item", "payload": {
        "type": "agent_message", "author": author, "recipient": recipient,
        "content": [{"type": "input_text", "text": text}] if blocks is None else blocks,
    }}


def progress(agent=AGENT):
    # A reviewer's mid-turn message to the controller is not its answer.
    author = f"/root/{agent}"
    return {"type": "response_item", "payload": {
        "type": "agent_message", "author": author, "recipient": "/root",
        "content": [
            {"type": "input_text", "text": f"Message Type: MESSAGE\nTask name: /root\nSender: {author}\nPayload:\n"},
            {"type": "encrypted_content", "encrypted_content": "fixture"},
        ],
    }}


def controller_turn():
    # The controller's own records between a dispatch and the extraction.
    return [
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "waiting"}]}},
        call("exec_command", {"cmd": "true"}, "c9"),
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c9", "output": "ok"}},
        {"type": "event_msg", "payload": {"type": "token_count"}},
    ]


def rollout_path(tmp_path, thread=THREAD):
    return tmp_path / f"rollout-2026-10-16T18-00-00-{thread}.jsonl"


def extract(tmp_path, records, *, head=None, tail=b"", agent=AGENT, thread=THREAD, transcript=None,
            output=None):
    if transcript is None:
        head = [session_meta()] if head is None else head
        transcript = write_transcript(rollout_path(tmp_path), [*head, *records], tail=tail)
    output = tmp_path / "response.json" if output is None else output
    result = run_cli_bytes(
        tmp_path, "reviewer-response", "--runtime", "codex", "--transcript", str(transcript),
        "--thread-id", thread, "--agent", agent, "--output", str(output),
    )
    return result, output


def test_writes_the_final_answer_payload_bytes_privately_and_reports_their_digest(tmp_path):
    expected = TERMINAL.encode("utf-8")

    result, output = extract(tmp_path, [
        spawn(),
        spawn(OTHER_AGENT),
        *controller_turn(),
        progress(),
        final_answer("other reviewer's answer", OTHER_AGENT),
        final_answer(TERMINAL),
        *controller_turn(),
    ])

    assert result.returncode == 0 and result.stderr == b""
    assert output.read_bytes() == expected
    info = os.lstat(output)
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    assert json.loads(result.stdout) == {
        "runtime": "codex", "agent": AGENT, "bytes": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
    }


@pytest.mark.parametrize("target", [f"/root/{AGENT}", AGENT])
def test_selects_the_answer_to_the_latest_follow_up(tmp_path, target):
    result, output = extract(tmp_path, [
        spawn(),
        final_answer("rejected format"),
        follow_up(target),
        *controller_turn(),
        final_answer(TERMINAL),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_keeps_the_first_answer_when_the_reviewer_answers_again_unrequested(tmp_path):
    result, output = extract(tmp_path, [
        spawn(),
        final_answer(TERMINAL),
        final_answer("a later unrequested answer"),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_a_request_to_another_reviewer_does_not_move_the_boundary(tmp_path):
    result, output = extract(tmp_path, [
        spawn(),
        spawn(OTHER_AGENT),
        final_answer(TERMINAL),
        follow_up(f"/root/{OTHER_AGENT}"),
        follow_up(f"/root/{OTHER_AGENT}/{AGENT}"),
        spawn(f"{AGENT}_2"),
        final_answer("not this one"),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


@pytest.mark.parametrize("records", [
    pytest.param([spawn("helper"), follow_up(AGENT_ID), spawn(), final_answer(TERMINAL)], id="agent-id-before-spawn"),
    pytest.param([call("followup_task", {"target": 7, "message": "retry"}), spawn(), final_answer(TERMINAL)],
                 id="non-string-target-before-spawn"),
    pytest.param([spawn(), final_answer("rejected format"), follow_up(AGENT_ID), spawn(), final_answer(TERMINAL)],
                 id="agent-id-before-respawn"),
])
def test_ignores_an_unattributable_follow_up_before_the_reviewers_latest_spawn(tmp_path, records):
    # A spawn restarts the request boundary, so no earlier request can change the selected answer.
    result, output = extract(tmp_path, records)

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_writes_an_empty_answer_as_zero_bytes(tmp_path):
    result, output = extract(tmp_path, [spawn(), final_answer("")])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == b""


def test_ignores_the_controllers_partially_written_last_record(tmp_path):
    partial = json.dumps(call("exec_command", {"cmd": "true"}, "c9")).encode("utf-8")[:40]

    result, output = extract(tmp_path, [spawn(), final_answer(TERMINAL)], tail=partial)

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_parses_only_records_that_can_be_a_request_or_an_answer(tmp_path):
    # Controller rollouts reach hundreds of megabytes; other records are never decoded.
    unrelated = b"[" * 100_000 + b"]" * 100_000 + b"\n"

    result, output = extract(tmp_path, [spawn(), final_answer(TERMINAL)], tail=unrelated)

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


ANSWER_LINE = json.dumps(final_answer(TERMINAL)).encode("utf-8")


@pytest.mark.parametrize("records, tail", [
    pytest.param([spawn(), *controller_turn()], b"", id="spawned-unanswered"),
    pytest.param([spawn(), progress()], b"", id="progress-only"),
    pytest.param([spawn(), final_answer("rejected format"), follow_up()], b"", id="follow-up-unanswered"),
    pytest.param([spawn(), spawn(OTHER_AGENT), final_answer(TERMINAL, OTHER_AGENT)], b"",
                 id="other-reviewer-answered"),
    pytest.param([spawn()], ANSWER_LINE[:60], id="answer-still-being-written"),
])
def test_reports_pending_without_writing_while_the_answer_is_unrecorded(tmp_path, records, tail):
    result, output = extract(tmp_path, records, tail=tail)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_PENDING\n"
    assert not os.path.lexists(output)


DEEP_ANSWER_LINE = b'{"payload":{"type":"agent_message"},"x":' + b"[" * 100_000 + b"]" * 100_000 + b"}\n"


@pytest.mark.parametrize("records, options", [
    pytest.param([], {"head": []}, id="empty-rollout"),
    pytest.param([spawn(), final_answer(TERMINAL)],
                 {"head": [{"type": "turn_context", "payload": {"id": THREAD, "source": "exec"}}]},
                 id="first-record-not-session-meta"),
    pytest.param([spawn(), final_answer(TERMINAL)],
                 {"head": [session_meta(source={"subagent": {"thread_spawn": {}}})]}, id="subagent-rollout"),
    pytest.param([spawn(), final_answer(TERMINAL)], {"head": [session_meta(OTHER_THREAD)]},
                 id="session-of-another-thread"),
    pytest.param([spawn(), final_answer(TERMINAL)], {"thread": OTHER_THREAD}, id="thread-argument-mismatch"),
    pytest.param([spawn(), final_answer(TERMINAL)], {"thread": "../x"}, id="malformed-thread-argument"),
    pytest.param([spawn(), final_answer(TERMINAL)], {"agent": f"../{AGENT}"}, id="malformed-agent-argument"),
    pytest.param([final_answer(TERMINAL)], {}, id="answer-without-request"),
    pytest.param([spawn(), final_answer(TERMINAL, recipient="/root/elsewhere")], {}, id="answer-to-another-recipient"),
    pytest.param([spawn(), final_answer("", text=envelope(f"/root/{OTHER_AGENT}") + TERMINAL)], {},
                 id="envelope-names-another-sender"),
    pytest.param([spawn(), final_answer("", text=envelope(f"/root/{AGENT}").replace("Payload:\n", "") + TERMINAL)],
                 {}, id="envelope-without-payload-marker"),
    pytest.param([spawn(), final_answer("", blocks=[
        {"type": "input_text", "text": envelope(f"/root/{AGENT}") + TERMINAL},
        {"type": "encrypted_content", "encrypted_content": "fixture"},
    ])], {}, id="two-content-blocks"),
    pytest.param([spawn(), final_answer("", blocks=[{"type": "input_text", "text": None}])], {},
                 id="text-not-a-string"),
    pytest.param([spawn(), final_answer("rejected format"), follow_up(AGENT_ID), final_answer(TERMINAL)], {},
                 id="follow-up-by-agent-id"),
    pytest.param([spawn(), final_answer("rejected format"), follow_up(AGENT_ID), follow_up(AGENT),
                  final_answer(TERMINAL)], {}, id="agent-id-follow-up-then-named-follow-up"),
    pytest.param([spawn(), final_answer(TERMINAL), call("followup_task", {"target": 7, "message": "retry"})], {},
                 id="follow-up-target-not-a-string"),
    pytest.param([spawn(), call("followup_task", "{not json")], {}, id="unreadable-request-arguments"),
    pytest.param([spawn(), final_answer(TERMINAL), call("followup_task", "[1]")], {},
                 id="request-arguments-not-an-object"),
    pytest.param([spawn(), final_answer(TERMINAL), {"type": "response_item", "payload": {
        "type": "function_call", "name": "followup_task", "namespace": "collaboration", "arguments": None,
    }}], {}, id="request-arguments-not-encoded"),
    pytest.param([spawn(), final_answer(TERMINAL)], {"tail": b'{"type":"response_item","function_call"\n'},
                 id="invalid-json-relevant-line"),
    pytest.param([spawn(), final_answer(TERMINAL)], {"tail": DEEP_ANSWER_LINE}, id="relevant-line-beyond-parser"),
])
def test_rejects_a_rollout_whose_answer_cannot_be_identified(tmp_path, records, options):
    result, output = extract(tmp_path, records, **options)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


def test_rejects_the_rollout_of_another_thread_by_file_name(tmp_path):
    transcript = write_transcript(rollout_path(tmp_path, OTHER_THREAD), [
        session_meta(), spawn(), final_answer(TERMINAL),
    ])

    result, output = extract(tmp_path, [], transcript=transcript)

    assert result.returncode == 1
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


def test_rejects_a_thread_id_that_codex_does_not_issue(tmp_path):
    thread = THREAD.upper()
    transcript = write_transcript(rollout_path(tmp_path, thread), [
        session_meta(thread), spawn(), final_answer(TERMINAL),
    ])

    result, output = extract(tmp_path, [], transcript=transcript, thread=thread)

    assert result.returncode == 1
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


@pytest.mark.parametrize("records", [
    pytest.param([spawn(), final_answer(TERMINAL)], id="spawned"),
    pytest.param([spawn(), final_answer("rejected format"), follow_up(AGENT), final_answer(TERMINAL)],
                 id="followed-up"),
])
def test_accepts_the_task_path_that_spawn_agent_returns(tmp_path, records):
    result, output = extract(tmp_path, records, agent=f"/root/{AGENT}")

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")
    assert json.loads(result.stdout)["agent"] == f"/root/{AGENT}"


@pytest.mark.parametrize("agent, task", [
    pytest.param("Reviewer-A", "Reviewer-A", id="name"),
    pytest.param("/root/Reviewer-A", "Reviewer-A", id="path"),
    pytest.param(f"/root/{AGENT}/child", f"{AGENT}/child", id="nested-path"),
])
def test_rejects_a_task_name_that_codex_does_not_issue(tmp_path, agent, task):
    result, output = extract(tmp_path, [spawn(task), final_answer(TERMINAL, task)], agent=agent)

    assert result.returncode == 1
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


def test_rejects_an_answer_that_is_not_utf8_encodable(tmp_path):
    line = json.dumps(final_answer("\ud800")).encode("ascii") + b"\n"

    result, output = extract(tmp_path, [spawn()], tail=line)

    assert result.returncode == 1
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


def test_reports_a_runtime_turn_failure_as_a_reviewer_failure(tmp_path):
    result, output = extract(tmp_path, [spawn(), final_answer(AGENT_ERROR)])

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_REVIEWER_FAILED\n"
    assert not os.path.lexists(output)


@pytest.mark.parametrize("runtime, thread_arguments", [
    pytest.param("codex", [], id="codex-without-thread"),
    pytest.param("claude", ["--thread-id", THREAD], id="claude-with-thread"),
])
def test_requires_a_thread_id_exactly_for_codex(tmp_path, runtime, thread_arguments):
    transcript = write_transcript(rollout_path(tmp_path), [session_meta(), spawn(), final_answer(TERMINAL)])
    output = tmp_path / "response.json"

    result = run_cli_bytes(
        tmp_path, "reviewer-response", "--runtime", runtime, "--transcript", str(transcript),
        *thread_arguments, "--agent", AGENT, "--output", str(output),
    )

    assert result.returncode == 2
    assert result.stderr == b"PRE_PR_TRIBUNAL:USAGE\n"
    assert not os.path.lexists(output)


def test_applies_the_line_bound_to_records_it_does_not_parse(tmp_path, capsys, monkeypatch):
    records = [session_meta(), spawn(), {"type": "event_msg", "payload": {"type": "token_count"},
                                         "padding": "x" * 2000}, final_answer(TERMINAL)]
    transcript = write_transcript(rollout_path(tmp_path), records)
    sizes = [len(line) for line in transcript.read_bytes().splitlines(keepends=True)]
    assert sizes[2] > 1000 and max(sizes[:2] + sizes[3:]) < 1000

    def run(output):
        status = cli.main([
            "reviewer-response", "--runtime", "codex", "--transcript", str(transcript),
            "--thread-id", THREAD, "--agent", AGENT, "--output", str(output),
        ])
        return status, capsys.readouterr().err

    assert run(tmp_path / "unbounded.json") == (0, "")
    monkeypatch.setattr(cli, "_MAX_TRANSCRIPT_LINE_BYTES", 1000)
    output = tmp_path / "response.json"

    assert run(output) == (1, "PRE_PR_TRIBUNAL:RESPONSE_INVALID\n")
    assert not os.path.lexists(output)


def test_extracted_answer_seals_with_a_receipt_matching_the_reviewer_bytes(git_repo, tmp_path):
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    terminal = json.dumps(
        report(pending.snapshot, "A", findings=[{**finding(), "title": "Boundary a -> b && c <d> 검증"}]),
        ensure_ascii=False, separators=(",", ":"),
    )
    raw = terminal.encode("utf-8")
    transcript = write_transcript(rollout_path(tmp_path), [session_meta(), spawn(), final_answer(terminal)])
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "A.json"

    extracted = run_cli_bytes(
        git_repo, "reviewer-response", "--runtime", "codex", "--transcript", str(transcript),
        "--thread-id", THREAD, "--agent", AGENT, "--output", str(output),
    )
    assert extracted.returncode == 0, extracted.stderr
    submitted = run_cli_bytes(git_repo, "submit-report", "--reviewer", "A", input=output.read_bytes())

    assert submitted.returncode == 0, submitted.stderr
    digest = hashlib.sha256(raw).hexdigest()
    assert json.loads(extracted.stdout)["sha256"] == digest
    assert json.loads(submitted.stdout)["raw_sha256"] == digest
    assert (git_repo / ".review/inbox/round-1/A.json").read_bytes() == raw
