import json
import os
from pathlib import Path
import re
import shlex
import subprocess

import pytest

from pre_pr_tribunal import cli
from pre_pr_tribunal.telemetry import read_ledger
from pre_pr_tribunal.model import Reviewer, Snapshot, parse_decisions, parse_reviewer_report


ROOT = Path(__file__).resolve().parents[2] / "skills" / "pre-pr-tribunal"
REPOSITORY_ROOT = ROOT.parents[1]
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
    head_ref="refs/heads/feature",
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
    assert 'cli.py" finalize' in steps[7]
    assert "finalize --reviewer-a" not in steps[7]
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


def test_skill_requires_detached_per_reviewer_views_and_bounded_failure_cleanup():
    skill = text("SKILL.md")
    steps = numbered_steps(skill)
    view_contract = re.search(
        r"<!-- reviewer-view-contract -->(.*?)<!-- reviewer-view-contract-end -->",
        skill,
        re.DOTALL,
    )
    assert view_contract is not None
    body = view_contract.group(1)
    assert "git worktree add --detach" in steps[5]
    assert "BOUND_HEAD" in steps[5]
    assert "VIEW_A" in steps[5] and "VIEW_B" in steps[5] and "VIEW_C" in steps[5]
    assert "dedicated cwd" in steps[5]
    assert "not an OS security sandbox" in body
    assert "does not expose a cwd argument" in body
    assert "do not fabricate a cwd field" in body
    assert "every tool call's workdir" in body
    assert "test ! -e \"$VIEW/.review\"" in body
    assert "git worktree remove \"$VIEW\"" in steps[6]
    assert "git worktree remove --force" not in steps[6]
    for token in ("failed", "timed-out", "malformed", "user intervention"):
        assert token in steps[6]
    assert "do not fabricate" in steps[6]
    assert "do not reset" in steps[6]
    assert "do not delete" in steps[6]


def test_partial_view_or_dispatch_failure_tracks_and_cleans_only_acquired_resources():
    steps = numbered_steps(text("SKILL.md"))
    creation = steps[5]
    cleanup = steps[6]
    assert "CREATED_VIEWS" in creation
    assert "STARTED_REVIEWERS" in creation
    assert creation.index("CREATED_VIEWS") < creation.index("git worktree add --detach")
    assert creation.index("git worktree add --detach") < creation.index(
        "immediately append"
    )
    assert "do not start another reviewer" in creation
    assert "every started reviewer" in cleanup
    assert "every successfully created view" in cleanup
    assert cleanup.index("every started reviewer") < cleanup.index(
        "every successfully created view"
    )
    assert "one bounded non-force cleanup attempt" in cleanup
    assert cleanup.index("one bounded non-force cleanup attempt") < cleanup.index(
        "stop for user intervention"
    )
    assert "do not reset" in cleanup and "do not delete" in cleanup


def test_detached_reviewer_views_empirically_exclude_controller_review_state(
    git_repo, tmp_path
):
    peer_state = git_repo / ".review/inbox/round-1"
    peer_state.mkdir(parents=True)
    (peer_state / "A.json").write_text('{"peer":"A"}\n', encoding="utf-8")
    (peer_state / "B.json").write_text('{"peer":"B"}\n', encoding="utf-8")
    head = subprocess.run(
        ["/usr/bin/git", "-C", str(git_repo), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    root = tmp_path / "reviewer-views"
    root.mkdir(mode=0o700)
    views = [root / reviewer for reviewer in "ABC"]
    try:
        for view in views:
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(git_repo),
                    "worktree",
                    "add",
                    "--detach",
                    str(view),
                    head,
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            view_head = subprocess.run(
                ["/usr/bin/git", "-C", str(view), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            status = subprocess.run(
                ["/usr/bin/git", "-C", str(view), "status", "--porcelain", "-uall"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            assert view_head == head
            assert status == ""
            assert not (view / ".review").exists()
            assert not any(view.rglob("A.json"))
            assert not any(view.rglob("B.json"))
    finally:
        for view in views:
            if view.exists():
                subprocess.run(
                    [
                        "/usr/bin/git",
                        "-C",
                        str(git_repo),
                        "worktree",
                        "remove",
                        str(view),
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                )
    assert stat_mode(root) == 0o700


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


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


def test_skill_submits_and_seals_each_terminal_report_before_finalize():
    steps = numbered_steps(text("SKILL.md"))
    for token in (
        "submit-report --reviewer X",
        "validate-report --reviewer",
        "--source stored",
        "exact bytes",
        "sealed receipt",
        "0600",
        "current-user-owned",
        "regular",
        "non-symlink",
        "raw_sha256",
    ):
        assert token in steps[6] or token in steps[7]
    assert steps[6].index("exact bytes") < steps[6].index("submit-report --reviewer X")
    assert steps[6].index("submit-report --reviewer X") < steps[6].index("sealed receipt")
    assert steps[7].index("validate-report") < steps[7].index('cli.py" finalize')
    assert "do not call `finalize`" in steps[7]


def test_skill_records_every_required_telemetry_stage_without_making_it_a_gate():
    skill = text("SKILL.md")
    for stage in (
        "snapshot_preflight",
        "view_create",
        "reviewer_dispatch_wait",
        "reviewer_total",
        "report_store",
        "report_validation",
        "finalize",
        "view_cleanup",
        "recovery_retry",
    ):
        assert stage in skill
    assert "telemetry" in skill and "does not change" in skill


def test_skill_failure_order_preserves_peer_privacy_and_cleanup():
    steps = numbered_steps(text("SKILL.md"))
    failure = steps[6]
    assert "peer output/status" in failure
    assert "already-started peer" in failure
    assert "do not cancel or disturb an already-started peer" in failure
    assert "do not call `finalize`" in failure
    assert "non-force cleanup" in failure
    assert "known terminal" in failure and "exact view identity" in failure
    assert failure.index("known terminal") < failure.index("cleanup refusal")


def test_every_report_contract_matches_the_decoded_text_parser_boundary():
    for name in REFERENCES:
        body = text(name)
        for token in (
            "stdout_excerpt",
            "stderr_excerpt",
            "LF",
            "TAB",
            "only",
            "CR",
            "NUL",
            "ESC",
            "do not trim",
            "do not reserialize",
        ):
            assert token in body, f"{name} omits decoded-text rule: {token}"
    assert "strict parser is authoritative" in text("references/report-schema.md")


def test_approved_design_and_plan_use_the_same_decoded_text_contract():
    documents = (
        REPOSITORY_ROOT
        / "docs/superpowers/specs/2026-09-01-pre-pr-adversarial-tribunal-design.md",
        REPOSITORY_ROOT
        / "docs/superpowers/plans/2026-09-01-pre-pr-adversarial-tribunal.md",
    )
    for document in documents:
        body = document.read_text(encoding="utf-8")
        for token in ("JSON-decoded string", "NFC", "`Cc`", "`Cs`", "LF", "TAB"):
            assert token in body, f"{document.name} omits decoded-text rule: {token}"


def pending_recovery_contract() -> str:
    skill = text("SKILL.md")
    recovery = re.search(
        r"<!-- pending-recovery-contract -->(.*?)"
        r"<!-- pending-recovery-contract-end -->",
        skill,
        re.DOTALL,
    )
    assert recovery is not None
    return recovery.group(1)


def test_pending_recovery_dispatches_only_pending_slots_and_never_replaces_sealed():
    body = pending_recovery_contract()
    for token in (
        "explicit user intervention",
        "status",
        "verdict_schema",
        "migrate-legacy-pending",
        "migrate-v2-pending",
        "in_progress",
        "same bound snapshot",
        "do not run `begin`",
        "do not reset",
        "do not delete `.review`",
        "dispatch only reviewers whose slot is `pending`",
        "never rerun or replace a `sealed` slot",
        "submit-report --reviewer X",
        "snapshot changed",
    ):
        assert token.lower() in body.lower()
    assert re.search(r"fresh rerun.{0,120}A, B, and C", body, re.DOTALL) is None
    assert "store-report --reviewer" not in body


def test_pending_recovery_uses_status_driven_command_sequence():
    body = pending_recovery_contract()
    status = "status"
    legacy_migration = "migrate-legacy-pending"
    v2_migration = "migrate-v2-pending"
    context = "context --reviewer X"
    dispatch = "dispatch only reviewers whose slot is `pending`"
    submit = "submit-report --reviewer X"
    validation = "validate-report --reviewer X --source stored"
    finalize = "finalize"
    assert body.index(status) < body.index(legacy_migration)
    assert body.index(legacy_migration) < body.index(v2_migration)
    assert body.index(v2_migration) < body.index(context)
    assert body.index(context) < body.index(dispatch)
    assert body.index(dispatch) < body.index(submit)
    assert body.index(submit) < body.index(validation)
    assert body.index(validation) < body.rindex(finalize)
    assert "verdict_schema == 1" in body
    assert "verdict_schema == 2" in body
    assert "verdict_schema == 3" in body
    assert "reviewer/subset" in body


def test_pending_recovery_documents_exact_migration_route_by_schema():
    migration_command_by_schema = {}
    for line in pending_recovery_contract().splitlines():
        match = re.match(r"- If `verdict_schema == ([123])`, (.*)", line)
        if match is None:
            continue
        command = re.search(r"run `([^`]+)`", match.group(2))
        migration_command_by_schema[int(match.group(1))] = (
            command.group(1) if command is not None else None
        )
    assert migration_command_by_schema == {
        1: "migrate-legacy-pending",
        2: "migrate-v2-pending",
        3: None,
    }


def test_operational_failure_policy_does_not_weaken_the_gate():
    skill = text("SKILL.md")
    for token in (
        "format-only retry",
        "same reviewer handle",
        "same-role fresh replacement",
        "ATTEMPTS_THIS_INVOCATION[X]",
        "maximum 3 attempts",
        "REVIEWER_UNAVAILABLE",
        "never pass with only two sealed slots",
        "DISPATCH_FAILED",
        "REVIEWER_FAILED",
        "REVIEWER_TIMEOUT",
    ):
        assert token in skill
    assert "CRITICAL" in skill and "HIGH" in skill
    assert "persisted attempt_count is cumulative" in skill
    assert "wait-interface timeout" in skill
    assert "still-running handle" in skill
    assert "do not call `record-failure`" in skill


def test_observation_and_terminal_cleanup_failures_are_warnings_only():
    skill = text("SKILL.md")
    for token in (
        "telemetry failure is an observation warning",
        "terminal cleanup refusal does not change the verdict",
        "uncertain reviewer process",
        "uncertain worktree identity",
        "uncertain ownership",
        "changed bytes",
        "changed contract",
    ):
        assert token in skill


def test_report_reference_documents_v3_shapes_and_real_controller_commands():
    schema = text("references/report-schema.md")
    status = json_example(schema, "v3-status")
    assert set(status) == {
        "round", "gate_status", "blocking_count", "verdict_path",
        "verdict_schema", "reviewers",
    }
    assert status["verdict_schema"] == 3
    assert status["reviewers"]["A"]["state"] == "sealed"
    assert set(status["reviewers"]["A"]) == {
        "state", "attempt_count", "last_error", "raw_sha256",
        "context_sha256", "report_contract_version", "provenance",
    }
    assert set(status["reviewers"]["B"]) == {
        "state", "attempt_count", "last_error",
    }

    receipt = json_example(schema, "v3-submit-receipt")
    assert set(receipt) == {
        "reviewer", "round", "state", "raw_sha256", "context_sha256",
        "report_contract_version", "attempt", "provenance",
    }
    assert receipt["state"] == "sealed"

    match = re.search(
        r"<!-- controller-command-examples -->(.*?)"
        r"<!-- controller-command-examples-end -->",
        schema,
        re.DOTALL,
    )
    assert match is not None
    commands = [
        shlex.split(command)
        for command in re.findall(r"`((?:status|context|submit-report|record-failure|"
                                   r"migrate-legacy-pending|migrate-v2-pending|"
                                   r"validate-report|finalize)[^`]*)`",
                                   match.group(1))
    ]
    parsed = [cli._parser().parse_args(command) for command in commands]
    by_command = {arguments.command for arguments in parsed}
    assert by_command == {
        "status", "context", "submit-report", "record-failure",
        "migrate-legacy-pending", "migrate-v2-pending", "validate-report", "finalize",
    }
    reasons = {
        arguments.reason for arguments in parsed
        if arguments.command == "record-failure"
    }
    assert reasons == {"DISPATCH_FAILED", "REVIEWER_FAILED", "REVIEWER_TIMEOUT"}


def test_report_reference_documents_internal_v3_lifecycle_identity():
    stored_verdict = json_example(
        text("references/report-schema.md"), "v3-stored-verdict",
    )
    assert stored_verdict["schema"] == 3
    assert re.fullmatch(r"[0-9a-f]{32}", stored_verdict["lifecycle_id"])


def test_docs_separate_retryable_formats_operations_integrity_and_warnings():
    for body in (text("SKILL.md"), text("references/report-schema.md")):
        format_start = body.index("Format failures")
        operation_start = body.index("Operational failures")
        warning_start = body.index("Observation warnings")
        integrity_start = body.index("Integrity stops")
        assert format_start < operation_start < warning_start < integrity_start
        for code in ("JSON_INVALID", "REPORT_TOO_LARGE", "TEXT_INVALID"):
            assert code in body[format_start:operation_start]
        for code in ("DISPATCH_FAILED", "REVIEWER_FAILED", "REVIEWER_TIMEOUT"):
            assert code in body[operation_start:warning_start]


def test_operator_telemetry_command_examples_parse_through_the_cli_contract():
    operator = (REPOSITORY_ROOT / "hooks" / "README.md").read_text(encoding="utf-8")
    match = re.search(
        r"<!-- telemetry-command-examples -->(.*?)"
        r"<!-- telemetry-command-examples-end -->",
        operator,
        re.DOTALL,
    )
    assert match is not None
    commands = [
        line.strip()
        for line in match.group(1).splitlines()
        if line.strip().startswith("/usr/bin/python3")
    ]
    parsed = [
        cli._parser().parse_args(shlex.split(command.removeprefix(CLI).strip()))
        for command in commands
    ]
    by_command = {}
    for arguments in parsed:
        by_command.setdefault(arguments.command, []).append(arguments)

    start = by_command["telemetry-start"][0]
    assert (start.stage, start.reviewer, start.attempt) == ("reviewer_total", "A", 1)
    assert any(
        item.outcome == "success" and item.reason_code is None
        for item in by_command["telemetry-finish"]
    )
    for outcome in ("failure", "timeout", "incomplete", "clock_anomaly"):
        assert any(
            item.outcome == outcome and item.reason_code is not None
            for item in (*by_command["telemetry-finish"], *by_command["telemetry-close"])
        )
    assert by_command["telemetry-recover"][0].run_id == "$RUN_ID"
    assert by_command["telemetry-summary"][0].run_id == "$RUN_ID"


def telemetry_lifecycle_commands():
    match = re.search(
        r"<!-- telemetry-lifecycle-commands -->(.*?)"
        r"<!-- telemetry-lifecycle-commands-end -->",
        text("SKILL.md"), re.DOTALL,
    )
    assert match is not None, "missing executable telemetry lifecycle contract"
    commands = {}
    for line in match.group(1).splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and "`telemetry-" in cells[2]:
            key = (cells[0].strip("`"), cells[1].strip("`"))
            assert key not in commands
            commands[key] = [shlex.split(value) for value in re.findall(r"`(telemetry-[^`]+)`", cells[2])]
    return commands


def run_lifecycle_commands(monkeypatch, capsys, variables, commands, second):
    results = []
    for arguments in commands:
        expanded = [variables.get(item, item) for item in arguments]
        # The executable CLI parser, not a second argument schema, owns usage.
        cli._parser().parse_args(expanded)
        result = cli.main(
            expanded,
            wall_clock=lambda: f"2026-09-09T00:00:{second:02d}Z",
            monotonic_ns=lambda: second * 1_000_000_000,
        )
        captured = capsys.readouterr()
        assert (result, captured.err) == (0, "")
        payload = json.loads(captured.out)
        if arguments[0] == "begin":
            variables["$RUN_ID"] = payload["telemetry"]["run_id"]
        elif arguments[0] == "telemetry-start":
            slot = {
                "reviewer_dispatch_wait": "$DISPATCH_SPAN_ID",
                "reviewer_total": "$TOTAL_SPAN_ID",
                "recovery_retry": "$RETRY_SPAN_ID",
            }[payload["stage"]]
            variables[slot] = payload["span_id"]
        results.append(payload)
    return results


@pytest.mark.parametrize(("signal", "total_ms"), (("accepted", 5000), ("unavailable", 7000)))
def test_skill_dispatch_lifecycle_measures_observed_boundaries(git_repo, monkeypatch, capsys, signal, total_ms):
    commands = telemetry_lifecycle_commands()
    monkeypatch.chdir(git_repo)
    variables = {"$REVIEWER": "A", "$ATTEMPT": "1"}
    run = lambda rows, second: run_lifecycle_commands(monkeypatch, capsys, variables, rows, second)
    run([["begin", "--base", "master", "--runtime", "codex", "--round", "1"]], 0)
    before = (git_repo / ".review/verdict.json").read_bytes()
    run(commands[signal, "dispatch_request"], 1)
    if signal == "accepted":
        run(commands[signal, "dispatch_accepted"], 3)
    run(commands[signal, "terminal_response"], 8)
    run(commands["exit", "success"], 9)
    stored = read_ledger(git_repo).runs[0]
    totals = [span for span in stored.spans if span.stage.value == "reviewer_total"]
    dispatches = [span for span in stored.spans if span.stage.value == "reviewer_dispatch_wait"]
    assert len(totals) == len(dispatches) == 1
    assert totals[0].duration_ms == total_ms
    assert totals[0].attempt == 1 and totals[0].outcome.value == "success"
    if signal == "unavailable":
        assert (dispatches[0].outcome.value, dispatches[0].reason_code, dispatches[0].duration_ms) == (
            "incomplete", "RUNTIME_SIGNAL_UNAVAILABLE", 7000,
        )
    else:
        assert (dispatches[0].outcome.value, dispatches[0].reason_code, dispatches[0].duration_ms) == (
            "success", None, 2000,
        )
    assert stored.outcome.value == "success"
    assert all(span.outcome is not None for span in stored.spans)
    assert (git_repo / ".review/verdict.json").read_bytes() == before


@pytest.mark.parametrize(("outcome", "reason"), (
    ("failure", "REPORT_SCHEMA_INVALID"),
    ("timeout", "REVIEWER_TIMEOUT"),
    ("incomplete", "CONTROLLER_INTERRUPTED"),
))
def test_skill_interrupted_lifecycle_recovers_then_closes_without_changing_gate(
    git_repo, monkeypatch, capsys, outcome, reason,
):
    commands = telemetry_lifecycle_commands()
    monkeypatch.chdir(git_repo)
    variables = {"$REVIEWER": "A", "$ATTEMPT": "1", "$RETRY_ATTEMPT": "1", "$PRIMARY_CODE": reason}
    run = lambda rows, second: run_lifecycle_commands(monkeypatch, capsys, variables, rows, second)
    run([["begin", "--base", "master", "--runtime", "codex", "--round", "1"]], 0)
    before = (git_repo / ".review/verdict.json").read_bytes()
    run(commands["recovery", "retry_start"], 0)
    run(commands["unavailable", "dispatch_request"], 1)
    recovered = run(commands["recovery", "interrupted"], 4)
    assert recovered == [{"run_id": variables["$RUN_ID"], "recovered_count": 3}]
    variables["$RETRY_ATTEMPT"] = "2"
    run(commands["recovery", "retry_start"], 5)
    run(commands["recovery", "retry_terminal"], 6)
    run(commands["exit", outcome], 7)
    stored = read_ledger(git_repo).runs[0]
    assert (stored.outcome.value, stored.reason_code) == (outcome, reason)
    assert all(span.outcome is not None for span in stored.spans)
    retries = [span for span in stored.spans if span.stage.value == "recovery_retry"]
    assert [(span.attempt, span.outcome.value) for span in retries] == [(1, "incomplete"), (2, "success")]
    assert (git_repo / ".review/verdict.json").read_bytes() == before


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

    execution_report = json_example(schema, "valid-reviewer-b")
    assert execution_report["executions"][0]["stdout_excerpt"] == "col1\n\t1 passed"
    parsed_execution = parse_reviewer_report(
        json.dumps(execution_report).encode(),
        expected_reviewer=Reviewer.B,
        expected_round=1,
        snapshot=SNAPSHOT,
    )
    assert parsed_execution.executions[0].stdout_excerpt == "col1\n\t1 passed"

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


def test_findings_may_report_normalized_repository_paths_outside_the_diff():
    for name in (
        "references/reviewer-a.md",
        "references/reviewer-b.md",
        "references/reviewer-c.md",
        "references/report-schema.md",
    ):
        body = text(name)
        assert "outside the diff" in body
        assert "initial_paths" in body
        assert "finding path" in body

    value = json_example(text("references/report-schema.md"), "valid-reviewer-a")
    value["findings"][0]["path"] = "docs/outside-diff.md"
    parsed = parse_reviewer_report(
        json.dumps(value).encode(),
        expected_reviewer=Reviewer.A,
        expected_round=1,
        snapshot=SNAPSHOT,
    )
    assert parsed.findings[0].path == "docs/outside-diff.md"
    assert parsed.findings[0].path not in SNAPSHOT.initial_paths
