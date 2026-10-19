"""In every round, each fresh Reviewer B execution must match a capture the installed helper recorded."""

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pre_pr_tribunal import cli as tribunal_cli
from pre_pr_tribunal.model import MAX_REPORT_BYTES, CaptureRecord, Reviewer, SchemaError
from pre_pr_tribunal.verdict_store import begin_round, read_verdict, submit_reviewer_report
from tests.pre_pr_tribunal.test_evidence_lifecycle import bundle, reused
from tests.pre_pr_tribunal.test_model_store import NOW, execution, report, run_cli_bytes

ROLE_PROMPT = Path(__file__).resolve().parents[2] / "skills/pre-pr-tribunal/references/reviewer-b.md"
PLAUSIBLE = "1e0c3b7a5d9f4b2c8a6e" + "0123456789abcdef" * 2 + "4f6a8c0e1b3d"
OUTPUT_LIMIT = getattr(tribunal_cli, "_CAPTURE_OUTPUT_BYTES", 64 * 1024 * 1024)


def capture_store(tmp_path):
    path = tmp_path / "captures"
    path.mkdir()
    path.chmod(0o700)
    return path


def capture(repo, store, command, *, cwd=None, timeout=None):
    arguments = ["capture-run", "--store", str(store), "--cwd", str(cwd or repo), "--command", command]
    if timeout is not None:
        arguments += ["--timeout", str(timeout)]
    result = run_cli_bytes(repo, *arguments)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def execution_from(captured, identifier="B-R1-E001"):
    """The execution an honest reviewer copies from the helper's output."""
    return {
        "id": identifier,
        "command": captured["command"],
        "exit_code": captured["exit_code"],
        "stdout_excerpt": captured["stdout_excerpt"],
        "stderr_excerpt": captured["stderr_excerpt"],
        "capture_sha256": captured["capture_sha256"],
        "truncated": captured["truncated"],
    }


def b_report(snapshot, item):
    return json.dumps(report(snapshot, "B", executions=[item], claims=[{
        "id": "B-R1-C001",
        "statement": "The reviewed behavior is executable.",
        "result": "supported",
        "execution_ids": [item["id"]],
        "reason": "",
    }], coverage={"complete": True, "primary_entry_paths": []}), ensure_ascii=False)


def submit(repo, raw, store=None, reviewer="B"):
    arguments = ["submit-report", "--reviewer", reviewer]
    if store is not None:
        arguments += ["--captures", str(store)]
    return run_cli_bytes(repo, *arguments, input=raw)


def claude_round(repo):
    return begin_round(repo, base="master", runtime="claude", round_number=1, now=NOW)


def assert_unattested(repo, submitted, field):
    assert submitted.returncode == 1
    assert submitted.stderr.splitlines() == [
        b"PRE_PR_TRIBUNAL:EXECUTION_UNATTESTED",
        f"PRE_PR_TRIBUNAL_DETAIL:reviewer=B path=executions[0].{field}".encode(),
    ]
    slot = read_verdict(repo).reviewers["B"]
    assert slot.status == "pending" and slot.last_error == "EXECUTION_UNATTESTED"
    assert not (repo / ".review/inbox/round-1/B.json").exists()


def test_the_helper_runs_the_command_and_records_its_capture(git_repo, tmp_path):
    store = capture_store(tmp_path)

    captured = capture(git_repo, store, "printf out; printf err >&2; exit 3")

    assert captured["exit_code"] == 3
    assert captured["capture_sha256"] == hashlib.sha256(b"outerr").hexdigest()
    assert (captured["stdout_excerpt"], captured["stderr_excerpt"]) == ("out", "err")
    records = list(store.iterdir())
    assert len(records) == 1 and records[0].stat().st_mode & 0o777 == 0o600


def test_an_execution_copied_from_the_helper_output_seals(git_repo, tmp_path):
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    item = execution_from(capture(git_repo, store, "printf '1 passed'"))

    submitted = submit(git_repo, b_report(pending.snapshot, item).encode(), store)

    assert submitted.returncode == 0, submitted.stderr
    assert read_verdict(git_repo).reviewers["B"].status == "sealed"


@pytest.mark.parametrize(("digest", "command"), (
    # Run 57959256/36368b82: two-character digests and a command that cannot run.
    ("0" * 63 + "1", "python3 -I -c (direct call of catalog_warnings on synthetic homes)"),
    # Uses all sixteen hex characters, so no shape check rejects it.
    (PLAUSIBLE, "python3 -m pytest -q"),
    # The role prompt's own example execution, which Reviewer B reads.
    (re.search(r'"capture_sha256": "([0-9a-f]{64})"', ROLE_PROMPT.read_text(encoding="utf-8")).group(1),
     "python3 -m pytest -q tests/example.py"),
), ids=("prose-command", "plausible-shape", "role-prompt-example"))
def test_a_digest_the_helper_never_recorded_is_rejected_for_retry(git_repo, tmp_path, digest, command):
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    capture(git_repo, store, "printf unrelated")
    item = {**execution("B-R1-E001", command=command), "capture_sha256": digest}

    submitted = submit(git_repo, b_report(pending.snapshot, item).encode(), store)

    assert_unattested(git_repo, submitted, "capture_sha256")


@pytest.mark.parametrize(("field", "value"), (
    ("command", "printf '2 passed'"),
    ("exit_code", 1),
    ("stdout_excerpt", "2 passed"),
    ("stderr_excerpt", "warning"),
))
def test_every_copied_field_must_match_the_capture(git_repo, tmp_path, field, value):
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    item = {**execution_from(capture(git_repo, store, "printf '1 passed'")), field: value}

    submitted = submit(git_repo, b_report(pending.snapshot, item).encode(), store)

    assert_unattested(git_repo, submitted, field)


def test_a_capture_outside_the_bound_snapshot_does_not_attest(git_repo, tmp_path):
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    item = execution_from(capture(git_repo, store, "printf '1 passed'", cwd=elsewhere))

    submitted = submit(git_repo, b_report(pending.snapshot, item).encode(), store)

    assert_unattested(git_repo, submitted, "capture_sha256")


@pytest.mark.parametrize("change", ("another-helper", "shared-mode", "no-completeness"))
def test_a_record_the_installed_helper_did_not_leave_does_not_attest(git_repo, tmp_path, change):
    # The candidate source's own cli.py, run from a reviewer view, is not the installed helper.
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    item = execution_from(capture(git_repo, store, "printf '1 passed'"))
    (record,) = store.iterdir()
    value = json.loads(record.read_text(encoding="utf-8"))
    if change == "another-helper":
        record.write_text(json.dumps({**value, "helper": str(tmp_path / "view/hooks/pre_pr_tribunal/cli.py")}))
    elif change == "no-completeness":
        record.write_text(json.dumps({key: entry for key, entry in value.items() if key != "complete"}))
    else:
        record.chmod(0o644)

    submitted = submit(git_repo, b_report(pending.snapshot, item).encode(), store)

    assert_unattested(git_repo, submitted, "capture_sha256")


def test_an_unsafe_capture_store_is_refused_by_the_helper_and_the_submission(git_repo, tmp_path):
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    item = execution_from(capture(git_repo, store, "printf '1 passed'"))
    store.chmod(0o755)

    ran = run_cli_bytes(git_repo, "capture-run", "--store", str(store), "--cwd", str(git_repo),
                        "--command", "true")
    submitted = submit(git_repo, b_report(pending.snapshot, item).encode(), store)

    assert ran.stderr == b"PRE_PR_TRIBUNAL:CAPTURE_STORE_UNSAFE\n"
    assert submitted.stderr == b"PRE_PR_TRIBUNAL:CAPTURE_STORE_UNSAFE\n"
    assert read_verdict(git_repo).reviewers["B"].attempt_count == 0


def test_the_helper_stops_a_command_at_its_timeout(git_repo, tmp_path):
    store = capture_store(tmp_path)
    started = time.monotonic()

    captured = capture(git_repo, store, "sleep 30 & sleep 30", timeout=1)

    assert time.monotonic() - started < 15
    assert (captured["exit_code"], captured["timed_out"]) == (124, True)


def test_the_helper_returns_when_the_command_exits_and_kills_its_session(git_repo, tmp_path):
    # Output goes to files, so a background child that keeps writing cannot hold the helper.
    store = capture_store(tmp_path)
    pidfile = tmp_path / "child"
    started = time.monotonic()

    captured = capture(git_repo, store, f"sleep 30 & echo $! > {pidfile}; exit 0", timeout=20)

    assert time.monotonic() - started < 10
    assert (captured["exit_code"], captured["timed_out"]) == (0, False)
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while True:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, "a background child outlived the helper"
        time.sleep(0.05)


def test_a_signal_to_the_helper_kills_the_command_session(git_repo, tmp_path):
    store = capture_store(tmp_path)
    pidfile = tmp_path / "pid"
    cli_path = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    helper = subprocess.Popen(
        [sys.executable, str(cli_path), "capture-run", "--store", str(store), "--cwd", str(git_repo),
         "--command", f"echo $$ > {pidfile}; exec sleep 30"],
        cwd=git_repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while not pidfile.exists() or not pidfile.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    command_pid = int(pidfile.read_text())

    helper.send_signal(signal.SIGTERM)
    helper.wait(timeout=10)

    deadline = time.monotonic() + 5
    while True:
        try:
            os.kill(command_pid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, "the command outlived the helper"
        time.sleep(0.05)
    assert helper.returncode != 0


def test_the_helper_stops_unbounded_output_soon_after_its_limit(git_repo, tmp_path):
    store = capture_store(tmp_path)
    started = time.monotonic()

    captured = capture(git_repo, store, "yes", timeout=60)

    assert time.monotonic() - started < 30
    (record,) = store.iterdir()
    assert OUTPUT_LIMIT < json.loads(record.read_text(encoding="utf-8"))["stdout_bytes"] < 4 * OUTPUT_LIMIT
    assert (captured["timed_out"], captured["truncated"]) == (False, True)


def test_the_helper_does_not_limit_files_the_command_writes(git_repo, tmp_path):
    # Only the captured streams are bounded; a large artifact the command builds is not.
    store = capture_store(tmp_path)
    artifact = tmp_path / "artifact"

    captured = capture(git_repo, store, f"python3 -c 'open(\"{artifact}\", \"wb\").truncate({OUTPUT_LIMIT + 4096})'")

    assert captured["exit_code"] == 0, captured["stderr_excerpt"]
    assert artifact.stat().st_size == OUTPUT_LIMIT + 4096


def test_the_helper_reaps_the_background_children_it_kills(git_repo, tmp_path):
    # Under a subreaper that never reaps, as in a container whose PID 1 does not,
    # a killed orphan stays a zombie unless the helper reaps it itself.
    store = capture_store(tmp_path)
    pidfile = tmp_path / "child"
    cli_path = Path(__file__).resolve().parents[2] / "hooks/pre_pr_tribunal/cli.py"
    wrapper = (
        "import ctypes, json, subprocess, sys\n"
        "ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)\n"
        f"subprocess.run([sys.executable, {str(cli_path)!r}, 'capture-run', '--store', {str(store)!r},"
        f" '--cwd', {str(git_repo)!r}, '--command', 'sleep 30 & echo $! > {pidfile}; exit 0'],"
        " capture_output=True, check=True)\n"
        f"pid = int(open({str(pidfile)!r}).read())\n"
        "try:\n"
        "    state = open(f'/proc/{pid}/stat').read().rsplit(')', 1)[1].split()[0]\n"
        "except FileNotFoundError:\n"
        "    state = 'gone'\n"
        "print(state)\n"
    )

    result = subprocess.run([sys.executable, "-c", wrapper], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "gone"


def test_a_report_that_hides_omitted_output_does_not_attest(git_repo, tmp_path):
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    item = {**execution_from(capture(git_repo, store, "printf '1 passed'")), "stdout_excerpt": ""}
    assert item["truncated"] is False

    submitted = submit(git_repo, b_report(pending.snapshot, item).encode(), store)

    assert_unattested(git_repo, submitted, "truncated")


def test_a_printed_excerpt_is_a_leading_slice_a_report_accepts(git_repo, tmp_path):
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    captured = capture(git_repo, store, r"printf 'ok\r\nmore'")
    assert captured["stdout_excerpt"] == "ok" and captured["truncated"] is True

    submitted = submit(git_repo, b_report(pending.snapshot, execution_from(captured)).encode(), store)

    assert submitted.returncode == 0, submitted.stderr


def test_the_helper_prints_the_end_of_a_whole_stream_and_the_end_attests(git_repo, tmp_path):
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    captured = capture(git_repo, store, "python3 -c $'print(\".\" * 3000)\\nprint(\"1 failed\")\\nraise SystemExit(1)'")
    assert (captured["stdout_excerpt"], captured["stdout_tail_excerpt"]) == ("." * 2000, "1 failed\n")
    item = {**execution_from(captured), "stdout_excerpt": captured["stdout_tail_excerpt"], "truncated": True}

    submitted = submit(git_repo, b_report(pending.snapshot, item).encode(), store)

    assert submitted.returncode == 0, submitted.stderr
    assert read_verdict(git_repo).reviewers["B"].status == "sealed"


def test_a_tail_is_empty_when_it_adds_nothing_or_the_stream_is_not_recorded_whole(git_repo, tmp_path):
    store = capture_store(tmp_path)

    short = capture(git_repo, store, "printf 'ok\\n'")
    large = capture(git_repo, store, "python3 -c $'print(\"x\" * 1100000)\\nprint(\"end\")'")

    assert (short["stdout_tail_excerpt"], short["stderr_tail_excerpt"]) == ("", "")
    assert (large["truncated"], large["stdout_tail_excerpt"]) == (True, "")


@pytest.mark.parametrize(("text", "limit", "expected"), (
    ("x" * 30 + "\n/home/user/notes\nend\n", 20, "end\n"),
    ("ok\n-----BEGIN PRIVATE KEY-----\n" + "QUJD\n" * 5 + "-----END PRIVATE KEY-----\n", 60, ""),
    ("progress\r50%\rdone\nsummary\n", 100, "summary\n"),
    ("fine\nghp_" + "A" * 36 + "\n", 30, ""),
), ids=("mid-line-home-path", "private-key", "carriage-return", "mid-token"))
def test_a_tail_excerpt_starts_at_a_line_so_no_secret_check_loses_its_prefix(text, limit, expected):
    assert tribunal_cli.citable_tail_excerpt(text, limit) == expected


def test_a_large_honest_capture_still_attests(git_repo, tmp_path):
    # A megabyte of NUL bytes per stream serializes to six times its size in the record.
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    captured = capture(git_repo, store, "head -c 1048576 /dev/zero; head -c 1048576 /dev/zero >&2")

    submitted = submit(git_repo, b_report(pending.snapshot, execution_from(captured)).encode(), store)

    assert submitted.returncode == 0, submitted.stderr


@pytest.mark.parametrize(("store_argument", "cwd_argument", "command"), (
    ("", None, "true"),
    ("relative-store", None, "true"),
    (None, "", "true"),
    (None, None, "cat /home/someone/notes"),
), ids=("empty-store", "relative-store", "empty-cwd", "home-path-command"))
def test_the_helper_refuses_inputs_a_report_cannot_carry(git_repo, tmp_path, store_argument, cwd_argument, command):
    store = capture_store(tmp_path)

    ran = run_cli_bytes(
        git_repo, "capture-run",
        "--store", str(store) if store_argument is None else store_argument,
        "--cwd", str(git_repo) if cwd_argument is None else cwd_argument,
        "--command", command,
    )

    assert ran.stderr == b"PRE_PR_TRIBUNAL:CAPTURE_COMMAND_INVALID\n"
    assert list(store.iterdir()) == []


def test_an_empty_capture_store_argument_is_unsafe(git_repo, tmp_path):
    pending = claude_round(git_repo)
    item = execution_from(capture(git_repo, capture_store(tmp_path), "printf '1 passed'"))

    submitted = run_cli_bytes(git_repo, "submit-report", "--reviewer", "B", "--captures", "",
                              input=b_report(pending.snapshot, item).encode())

    assert submitted.stderr == b"PRE_PR_TRIBUNAL:CAPTURE_STORE_UNSAFE\n"


@pytest.mark.parametrize("runtime", ("claude", "codex"))
def test_a_reviewer_b_submission_requires_its_capture_store(git_repo, tmp_path, runtime):
    pending = begin_round(git_repo, base="master", runtime=runtime, round_number=1, now=NOW)
    item = execution_from(capture(git_repo, capture_store(tmp_path), "printf '1 passed'"))

    submitted = submit(git_repo, b_report(pending.snapshot, item).encode())

    assert submitted.stderr == b"PRE_PR_TRIBUNAL:SUBMISSION_CAPTURES_REQUIRED\n"
    slot = read_verdict(git_repo).reviewers["B"]
    assert slot.status == "pending" and slot.attempt_count == 0


@pytest.mark.parametrize("runtime", ("claude", "codex"))
def test_a_capture_store_is_refused_where_no_check_uses_it(git_repo, tmp_path, runtime):
    pending = begin_round(git_repo, base="master", runtime=runtime, round_number=1, now=NOW)
    store = capture_store(tmp_path)
    capture(git_repo, store, "printf '1 passed'")
    raw = json.dumps(report(pending.snapshot, "A"))

    submitted = submit(git_repo, raw.encode(), store, reviewer="A")

    assert submitted.stderr == b"PRE_PR_TRIBUNAL:SUBMISSION_CAPTURES_UNEXPECTED\n"


def test_a_codex_round_attests_fresh_executions_like_a_claude_round(git_repo, tmp_path):
    pending = begin_round(git_repo, base="master", runtime="codex", round_number=1, now=NOW)
    store = capture_store(tmp_path)
    honest = execution_from(capture(git_repo, store, "printf '1 passed'"))
    typed = {**execution("B-R1-E001"), "capture_sha256": PLAUSIBLE}

    assert_unattested(git_repo, submit(git_repo, b_report(pending.snapshot, typed).encode(), store), "capture_sha256")
    submitted = submit(git_repo, b_report(pending.snapshot, honest).encode(), store)

    assert submitted.returncode == 0, submitted.stderr
    assert read_verdict(git_repo).reviewers["B"].status == "sealed"


def test_a_bundle_execution_is_authenticated_by_its_bundle_not_a_capture(git_repo):
    frozen = bundle(git_repo)
    pending = begin_round(
        git_repo, base="master", runtime="claude", round_number=1,
        evidence_bundle_sha256=frozen["bundle_sha256"],
    )
    raw = b_report(pending.snapshot, reused(git_repo, frozen)).encode()

    receipt = submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=raw, captures=())

    assert receipt.raw_sha256 == hashlib.sha256(raw).hexdigest()


def test_an_unattested_fresh_execution_beside_a_bundle_one_is_still_rejected(git_repo):
    frozen = bundle(git_repo)
    pending = begin_round(
        git_repo, base="master", runtime="claude", round_number=1,
        evidence_bundle_sha256=frozen["bundle_sha256"],
    )
    value = json.loads(b_report(pending.snapshot, reused(git_repo, frozen)))
    value["executions"].append(execution("B-R1-E002"))
    value["claims"][0]["execution_ids"].append("B-R1-E002")
    raw = json.dumps(value).encode()
    unrelated = CaptureRecord(
        command="true", head=pending.head_sha, exit_code=0,
        capture_sha256=hashlib.sha256(b"").hexdigest(), stdout="", stderr="", complete=True,
    )

    with pytest.raises(SchemaError, match="^EXECUTION_UNATTESTED$") as raised:
        submit_reviewer_report(git_repo, reviewer=Reviewer.B, raw=raw, captures=(unrelated,))
    assert raised.value.path == "executions[1].capture_sha256"


def test_an_oversized_claude_b_reply_takes_the_format_retry_path(git_repo, tmp_path):
    pending = claude_round(git_repo)
    store = capture_store(tmp_path)
    value = json.loads(b_report(pending.snapshot, execution_from(capture(git_repo, store, "printf '1 passed'"))))
    value["claims"][0]["statement"] = "x" * 1024
    value["claims"] = [{**value["claims"][0], "id": f"B-R1-C{index:03d}"} for index in range(1, 129)]
    raw = json.dumps(value).encode()
    assert len(raw) > MAX_REPORT_BYTES

    submitted = submit(git_repo, raw, store)

    assert submitted.stderr.splitlines()[0] == b"PRE_PR_TRIBUNAL:REPORT_TOO_LARGE"
    slot = read_verdict(git_repo).reviewers["B"]
    assert slot.status == "pending" and slot.attempt_count == 1 and slot.last_error == "REPORT_TOO_LARGE"
