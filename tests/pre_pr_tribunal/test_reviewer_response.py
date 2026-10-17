"""`reviewer-response` moves a reviewer's terminal reply from its transcript to a private file."""

import hashlib
import json
import os
import stat

import pytest

from pre_pr_tribunal import cli
from pre_pr_tribunal.verdict_store import begin_round
from tests.pre_pr_tribunal.test_model_store import NOW, finding, report, run_cli_bytes


AGENT = "a0123456789abcdef"
OTHER_AGENT = "a9999999999999999"
# Characters a display channel escapes, plus non-ASCII text, so bytes copied
# from a rendered completion notification cannot match the reviewer's reply.
TERMINAL = '{"verdict":"a -> b && c <d>","note":"검증"}'
THINKING = {"type": "thinking", "thinking": "", "signature": "fixture"}


def prompt(value, agent=AGENT):
    return {
        "type": "user", "agentId": agent, "isSidechain": True, "uuid": f"u-{value}",
        "message": {"role": "user", "content": value},
    }


def follow_up(value, agent=AGENT):
    # The controller's same-handle follow-up arrives as a meta prompt from the coordinator.
    return {**prompt(value, agent), "isMeta": True, "origin": {"kind": "coordinator"}}


def reopened(agent=AGENT):
    # A finished background task reopens a stopped reviewer without any controller request.
    return {**prompt("<task-notification>done</task-notification>", agent), "isMeta": True,
            "origin": {"kind": "task-notification"}}


def assistant(message_id, blocks, stop_reason=None, agent=AGENT):
    return {
        "type": "assistant", "agentId": agent, "isSidechain": True,
        "uuid": f"{message_id}-{blocks[0]['type']}",
        "message": {
            "id": message_id, "type": "message", "role": "assistant", "model": "fixture",
            "content": blocks, "stop_reason": stop_reason, "stop_sequence": None,
        },
    }


def text(value):
    return {"type": "text", "text": value}


def tool_use(tool_id):
    return {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "/dev/null"}}


def tool_result(tool_id, agent=AGENT):
    return {
        "type": "user", "agentId": agent, "isSidechain": True, "uuid": f"r-{tool_id}",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"},
        ]},
    }


def hook(agent=AGENT):
    return {"type": "attachment", "agentId": agent, "isSidechain": True,
            "attachment": {"type": "hook_success"}}


def queued(kind, value="retry with the format fixed", agent=AGENT):
    # A message that reaches a running reviewer is queued into its turn as an attachment.
    return {"type": "attachment", "agentId": agent, "isSidechain": True,
            "attachment": {"type": "queued_command", "prompt": value, "origin": {"kind": kind},
                           "isMeta": True}}


def subagent_stop(agent=AGENT):
    # Recorded when the reviewer stops, whatever stop_reason its final record carries.
    return {"type": "attachment", "agentId": agent, "isSidechain": True,
            "attachment": {"type": "hook_success", "hookEvent": "SubagentStop"}}


def request_blob(role, value, agent=AGENT):
    # Request bookkeeping that repeats conversation text under its own type.
    return {"type": "api-request-blob", "agentId": agent, "hash": "fixture",
            "message": {"role": role, "content": [text(value)]}}


def write_transcript(path, records, *, tail=b""):
    lines = b"".join(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n" for record in records)
    path.write_bytes(lines + tail)
    return path


def extract(tmp_path, records, *, tail=b"", agent=AGENT, output=None, transcript=None):
    if transcript is None:
        transcript = write_transcript(tmp_path / f"agent-{AGENT}.jsonl", records, tail=tail)
    output = tmp_path / "response.json" if output is None else output
    result = run_cli_bytes(
        tmp_path, "reviewer-response", "--runtime", "claude", "--transcript", str(transcript),
        "--agent", agent, "--output", str(output),
    )
    return result, output


def test_writes_the_terminal_reply_bytes_privately_and_reports_their_digest(tmp_path):
    expected = '{"verdict":"a -> b && c <d>","note":"검증"}'.encode("utf-8")

    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [THINKING]),
        assistant("m1", [tool_use("t1")], "tool_use"),
        tool_result("t1"),
        assistant("m2", [THINKING]),
        assistant("m2", [text(TERMINAL)], "end_turn"),
        request_blob("assistant", "not the reply"),
        hook(),
        hook(),
    ])

    assert result.returncode == 0 and result.stderr == b""
    assert output.read_bytes() == expected
    info = os.lstat(output)
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    assert json.loads(result.stdout) == {
        "runtime": "claude", "agent": AGENT, "bytes": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
    }


def test_selects_the_reply_to_the_latest_prompt_after_a_same_handle_retry(tmp_path):
    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [text("rejected format")], "end_turn"),
        hook(),
        follow_up("retry with the format fixed"),
        assistant("m2", [text(TERMINAL)], "end_turn"),
        hook(),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_keeps_the_delivered_reply_when_a_background_task_reopens_the_reviewer(tmp_path):
    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [text(TERMINAL)], "end_turn"),
        hook(),
        reopened(),
        assistant("m2", [text("my report was already submitted")], "end_turn"),
        hook(),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_answers_a_follow_up_sent_after_a_background_reopen(tmp_path):
    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [text("rejected format")], "end_turn"),
        reopened(),
        assistant("m2", [text("unrequested reply")], "end_turn"),
        follow_up("retry with the format fixed"),
        assistant("m3", [text(TERMINAL)], "end_turn"),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_answers_a_follow_up_queued_while_the_reviewer_runs(tmp_path):
    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [text("rejected format")], "end_turn"),
        subagent_stop(),
        reopened(),
        assistant("m2", [tool_use("t1")], "tool_use"),
        tool_result("t1"),
        queued("coordinator"),
        assistant("m3", [text(TERMINAL)], "end_turn"),
        subagent_stop(),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_does_not_treat_a_queued_background_notification_as_a_request(tmp_path):
    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [text(TERMINAL)], "end_turn"),
        subagent_stop(),
        reopened(),
        assistant("m2", [tool_use("t1")], "tool_use"),
        tool_result("t1"),
        queued("task-notification", "<task-notification>done</task-notification>"),
        assistant("m3", [text("unrequested reply")], "end_turn"),
        subagent_stop(),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_accepts_a_delivered_reply_whose_final_record_has_a_null_stop_reason(tmp_path):
    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [THINKING]),
        assistant("m1", [text(TERMINAL)]),
        subagent_stop(),
        subagent_stop(),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_accepts_a_null_stop_reason_reply_to_a_follow_up(tmp_path):
    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [text("rejected format")], "end_turn"),
        subagent_stop(),
        follow_up("retry with the format fixed"),
        assistant("m2", [text(TERMINAL)]),
        subagent_stop(),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_keeps_a_null_stop_reason_reply_when_a_background_task_reopens_the_reviewer(tmp_path):
    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [text(TERMINAL)]),
        subagent_stop(),
        reopened(),
        assistant("m2", [text("unrequested reply")], "end_turn"),
        subagent_stop(),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_takes_the_last_reply_when_a_stop_hook_continues_the_turn(tmp_path):
    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [text("first stop")], "end_turn"),
        subagent_stop(),
        assistant("m2", [THINKING]),
        assistant("m2", [tool_use("t1")], "tool_use"),
        tool_result("t1"),
        assistant("m3", [text(TERMINAL)], "end_turn"),
        hook(),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_reads_the_transcript_through_the_agent_output_link(tmp_path):
    real = write_transcript(tmp_path / f"agent-{AGENT}.jsonl", [
        prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn"),
    ])
    link = tmp_path / "task.output"
    link.symlink_to(real)

    result, output = extract(tmp_path, [], transcript=link)

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


PARTIAL_HOOK_LINE = json.dumps(hook()).encode("utf-8")[:20]


@pytest.mark.parametrize("records, tail", [
    pytest.param(
        [prompt("review"), assistant("m1", [text("rejected")], "end_turn"), follow_up("retry")],
        b"", id="retry-unanswered"),
    pytest.param(
        [prompt("review"), assistant("m1", [text("first")], "end_turn"), hook(),
         assistant("m2", [tool_use("t1")], "tool_use")],
        b"", id="continuation-running"),
    pytest.param([prompt("review"), assistant("m1", [THINKING])], b"", id="no-reply-yet"),
    pytest.param(
        [prompt("review"), assistant("m1", [text(TERMINAL)]), hook()],
        b"", id="null-stop-reason-without-stop-record"),
    pytest.param(
        [prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn")],
        PARTIAL_HOOK_LINE, id="partial-last-line"),
])
def test_reports_pending_without_writing_while_the_reply_is_unrecorded(tmp_path, records, tail):
    result, output = extract(tmp_path, records, tail=tail)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_PENDING\n"
    assert not os.path.lexists(output)


def _without_agent(record):
    del record["agentId"]
    return record


_MISSING = object()


def _stop_with_content(content):
    # A stop whose message content is not a well-formed block list.
    record = assistant("m1", [THINKING], "end_turn")
    if content is _MISSING:
        del record["message"]["content"]
    else:
        record["message"]["content"] = content
    return record


@pytest.mark.parametrize("records, tail, agent", [
    pytest.param(
        [prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn", agent=OTHER_AGENT)],
        b"", AGENT, id="foreign-agent-record"),
    pytest.param(
        [prompt("review"), _without_agent(assistant("m1", [text(TERMINAL)], "end_turn"))],
        b"", AGENT, id="record-without-agent"),
    pytest.param(
        [prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn")],
        b"", OTHER_AGENT, id="wrong-agent-argument"),
    pytest.param(
        [prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn")],
        b"", "../agent", id="malformed-agent-argument"),
    pytest.param(
        [prompt("review"), assistant("m1", [text("part one"), text("part two")], "end_turn")],
        b"", AGENT, id="two-text-blocks"),
    pytest.param(
        [prompt("review"), assistant(None, [text(TERMINAL)], "end_turn")],
        b"", AGENT, id="reply-without-message-id"),
    pytest.param([prompt("review"), _stop_with_content(_MISSING)], b"", AGENT, id="reply-without-content"),
    pytest.param([prompt("review"), _stop_with_content(TERMINAL)], b"", AGENT, id="reply-content-not-a-list"),
    pytest.param([prompt("review"), _stop_with_content([])], b"", AGENT, id="reply-content-empty"),
    pytest.param([prompt("review"), _stop_with_content([1])], b"", AGENT, id="reply-content-not-objects"),
    pytest.param([prompt("review"), _stop_with_content([THINKING, 5])], b"", AGENT,
                 id="reply-content-partly-not-objects"),
    pytest.param(
        [prompt("review"), assistant("m1", [text("part one")]),
         assistant("m1", [text("part two")], "end_turn")],
        b"", AGENT, id="text-split-across-records"),
    pytest.param(
        [prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn")],
        b"{not json\n", AGENT, id="invalid-json-line"),
    pytest.param(
        [prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn")],
        b'{"type":"attachment","agentId":"\xff"}\n', AGENT, id="invalid-utf8-line"),
    pytest.param(
        [prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn")],
        b"[1,2]\n", AGENT, id="non-object-line"),
    pytest.param(
        [prompt("review")], b"[" * 100_000 + b"]" * 100_000 + b"\n", AGENT,
        id="nesting-beyond-the-parser"),
    pytest.param(
        [prompt("review")],
        json.dumps(assistant("m1", [text("\ud800")], "end_turn")).encode("ascii") + b"\n", AGENT,
        id="lone-surrogate-reply"),
])
def test_rejects_a_transcript_whose_reply_cannot_be_identified(tmp_path, records, tail, agent):
    result, output = extract(tmp_path, records, tail=tail, agent=agent)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


@pytest.mark.parametrize("blocks", [
    pytest.param([text("")], id="empty-text"),
    pytest.param([THINKING], id="no-text-block"),
])
def test_writes_an_empty_reply_when_the_reviewer_stops_without_report_text(tmp_path, blocks):
    result, output = extract(tmp_path, [prompt("review"), assistant("m1", blocks, "end_turn")])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == b""
    assert json.loads(result.stdout)["bytes"] == 0


def test_an_empty_reply_takes_the_format_retry_path(git_repo, tmp_path):
    # An empty terminal response is a reviewer format failure, not an unidentifiable transcript.
    begin_round(git_repo, base="master", runtime="claude", round_number=1, now=NOW)
    transcript = write_transcript(tmp_path / f"agent-{AGENT}.jsonl", [
        prompt("review"), assistant("m1", [THINKING], "end_turn"),
    ])
    output = tmp_path / "A.json"

    extracted = run_cli_bytes(
        git_repo, "reviewer-response", "--runtime", "claude", "--transcript", str(transcript),
        "--agent", AGENT, "--output", str(output),
    )
    assert extracted.returncode == 0, extracted.stderr
    submitted = run_cli_bytes(git_repo, "submit-report", "--reviewer", "A", input=output.read_bytes())

    assert submitted.returncode == 1
    assert submitted.stderr.splitlines()[0] == b"PRE_PR_TRIBUNAL:JSON_INVALID"


def test_rejects_a_missing_transcript(tmp_path):
    result, output = extract(tmp_path, [], transcript=tmp_path / "absent.jsonl")

    assert result.returncode == 1
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


def test_rejects_a_transcript_that_is_not_a_regular_file(tmp_path):
    fifo = tmp_path / "agent.fifo"
    os.mkfifo(fifo)

    result, output = extract(tmp_path, [], transcript=fifo)

    assert result.returncode == 1
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


def test_treats_a_plain_list_prompt_as_a_controller_request(tmp_path):
    list_prompt = {**prompt("retry"), "message": {"role": "user", "content": [text("retry with the format fixed")]}}

    result, output = extract(tmp_path, [
        prompt("review"),
        assistant("m1", [text("rejected format")], "end_turn"),
        list_prompt,
        assistant("m2", [text(TERMINAL)], "end_turn"),
    ])

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def run_in_process(capsys, tmp_path, *, output=None):
    transcript = write_transcript(tmp_path / f"agent-{AGENT}.jsonl", [
        prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn"),
    ])
    output = tmp_path / "response.json" if output is None else output
    status = cli.main([
        "reviewer-response", "--runtime", "claude", "--transcript", str(transcript),
        "--agent", AGENT, "--output", str(output),
    ])
    return status, capsys.readouterr(), output


def test_rejects_a_transcript_owned_by_another_user(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: os.getuid() + 1)

    status, captured, output = run_in_process(capsys, tmp_path)

    assert status == 1
    assert captured.err == "PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


def test_rejects_a_transcript_beyond_the_processing_bound(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_MAX_TRANSCRIPT_BYTES", 128)

    status, captured, output = run_in_process(capsys, tmp_path)

    assert status == 1
    assert captured.err == "PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


def test_rejects_a_transcript_record_beyond_the_line_bound(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_MAX_TRANSCRIPT_LINE_BYTES", 128)

    status, captured, output = run_in_process(capsys, tmp_path)

    assert status == 1
    assert captured.err == "PRE_PR_TRIBUNAL:RESPONSE_INVALID\n"
    assert not os.path.lexists(output)


def test_creates_the_output_as_0600_under_a_restrictive_umask(tmp_path, capsys):
    previous = os.umask(0o277)
    try:
        status, captured, output = run_in_process(capsys, tmp_path)
    finally:
        os.umask(previous)

    assert status == 0, captured.err
    assert stat.S_IMODE(os.lstat(output).st_mode) == 0o600
    assert output.read_bytes() == TERMINAL.encode("utf-8")


def test_removes_an_output_whose_mode_cannot_be_made_private(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.os, "fchmod", lambda descriptor, mode: None)
    previous = os.umask(0o277)
    try:
        status, captured, output = run_in_process(capsys, tmp_path)
    finally:
        os.umask(previous)

    assert status == 1
    assert captured.err == "PRE_PR_TRIBUNAL:RESPONSE_OUTPUT_UNSAFE\n"
    assert not os.path.lexists(output)


def test_a_failed_write_never_removes_a_file_that_replaced_the_output(tmp_path, capsys, monkeypatch):
    output = tmp_path / "response.json"

    def replace_then_fail(descriptor):
        os.rename(output, tmp_path / "created.json")
        output.write_bytes(b"replacement")
        raise OSError("injected fsync failure")

    monkeypatch.setattr(cli.os, "fsync", replace_then_fail)

    status, captured, _ = run_in_process(capsys, tmp_path, output=output)

    assert status == 1
    assert captured.err == "PRE_PR_TRIBUNAL:RESPONSE_OUTPUT_UNSAFE\n"
    assert output.read_bytes() == b"replacement"


@pytest.mark.parametrize("shape", ["regular", "link-to-file", "dangling-link"])
def test_never_replaces_or_follows_an_existing_output_path(tmp_path, shape):
    output = tmp_path / "response.json"
    target = tmp_path / "elsewhere.json"
    if shape == "regular":
        output.write_bytes(b"keep")
    elif shape == "link-to-file":
        target.write_bytes(b"keep")
        output.symlink_to(target)
    else:
        output.symlink_to(target)

    result, _ = extract(tmp_path, [
        prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn"),
    ], output=output)

    assert result.returncode == 1
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_OUTPUT_EXISTS\n"
    if shape == "regular":
        assert output.read_bytes() == b"keep"
    elif shape == "link-to-file":
        assert target.read_bytes() == b"keep"
    else:
        assert not target.exists()


def test_reports_an_output_path_that_cannot_be_created(tmp_path):
    output = tmp_path / "missing-directory" / "response.json"

    result, _ = extract(tmp_path, [
        prompt("review"), assistant("m1", [text(TERMINAL)], "end_turn"),
    ], output=output)

    assert result.returncode == 1
    assert result.stderr == b"PRE_PR_TRIBUNAL:RESPONSE_OUTPUT_UNSAFE\n"
    assert not os.path.lexists(output)


def test_extracted_reply_seals_with_a_receipt_matching_the_reviewer_bytes(git_repo, tmp_path):
    pending = begin_round(git_repo, base="master", runtime="claude", round_number=1, now=NOW)
    terminal = json.dumps(
        report(pending.snapshot, "A", findings=[{**finding(), "title": "Boundary a -> b && c <d> 검증"}]),
        ensure_ascii=False, separators=(",", ":"),
    )
    raw = terminal.encode("utf-8")
    transcript = write_transcript(tmp_path / f"agent-{AGENT}.jsonl", [
        prompt("review"), assistant("m1", [text(terminal)], "end_turn"), hook(),
    ])
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "A.json"

    extracted = run_cli_bytes(
        git_repo, "reviewer-response", "--runtime", "claude", "--transcript", str(transcript),
        "--agent", AGENT, "--output", str(output),
    )
    assert extracted.returncode == 0, extracted.stderr
    submitted = run_cli_bytes(git_repo, "submit-report", "--reviewer", "A", input=output.read_bytes())

    assert submitted.returncode == 0, submitted.stderr
    digest = hashlib.sha256(raw).hexdigest()
    assert json.loads(extracted.stdout)["sha256"] == digest
    assert json.loads(submitted.stdout)["raw_sha256"] == digest
    assert (git_repo / ".review/inbox/round-1/A.json").read_bytes() == raw
