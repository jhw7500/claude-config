import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from pre_pr_tribunal import gate, hook_common
from pre_pr_tribunal.gate import GateCode, evaluate_gate
from pre_pr_tribunal.model import SchemaError
from pre_pr_tribunal.verdict_store import begin_round, finalize_round, read_verdict


PACKAGE = Path(__file__).resolve().parents[2] / "hooks" / "pre_pr_tribunal"
MAX_PAYLOAD_BYTES = 1024 * 1024
COMMAND = "gh pr create"
BOUND_COMMAND = "gh pr create --base master"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, text: str) -> None:
    target = repo / "tracked.txt"
    target.write_text(target.read_text(encoding="utf-8") + text, encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", text.strip())


def _execution(identifier: str) -> dict[str, object]:
    stdout = "1 passed"
    return {
        "id": identifier,
        "command": "python3 -m pytest -q",
        "exit_code": 0,
        "stdout_excerpt": stdout,
        "stderr_excerpt": "",
        "capture_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "truncated": False,
    }


def _finding(identifier: str) -> dict[str, object]:
    reviewer = identifier[0]
    return {
        "id": identifier,
        "reviewer": reviewer,
        "severity": "HIGH",
        "title": "Incorrect boundary",
        "rationale": "The boundary permits an invalid state.",
        "path": "tracked.txt",
        "line": 1,
        "execution_ids": [],
        "acceptance_condition": "The invalid state is rejected.",
    }


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _reports(
    repo: Path,
    snapshot,
    *,
    round_number: int = 1,
    finding_id: str | None = None,
    prior_response: dict[str, object] | None = None,
) -> dict[str, Path]:
    result = {}
    for reviewer in "ABC":
        value = {
            "schema": 1,
            "reviewer": reviewer,
            "round": round_number,
            "snapshot": {
                "head_sha": snapshot.head_sha,
                "diff_sha256": snapshot.diff_sha256,
            },
            "status": "complete",
            "findings": (
                [_finding(finding_id)]
                if finding_id is not None and finding_id.startswith(reviewer)
                else []
            ),
            "executions": [],
            "claims": [],
            "prior_decisions": (
                [prior_response]
                if prior_response is not None
                and prior_response["decision_id"].split("-")[2] == reviewer
                else []
            ),
        }
        result[reviewer] = _write_json(
            repo / f".review/inbox/round-{round_number}/{reviewer}.json", value
        )
    return result


def _finish_round(
    repo: Path,
    *,
    round_number: int = 1,
    finding_id: str | None = None,
    prior_response: dict[str, object] | None = None,
):
    pending = begin_round(
        repo, base="master", runtime="codex", round_number=round_number
    )
    return finalize_round(
        repo,
        reviewer_paths=_reports(
            repo,
            pending.snapshot,
            round_number=round_number,
            finding_id=finding_id,
            prior_response=prior_response,
        ),
    )


def _passing_verdict(repo: Path):
    return _finish_round(repo)


def _decision(round_number: int, finding_id: str) -> dict[str, object]:
    reviewer = finding_id[0]
    return {
        "id": f"D-R{round_number}-{reviewer}-001",
        "finding_ref": {
            "round": round_number,
            "id": finding_id,
            "reviewer": reviewer,
        },
        "disposition": "fixed",
        "rationale": "Covered by a new regression test.",
        "executions": [_execution(f"D-R{round_number}-E001")],
    }


def _round_three_failure(repo: Path):
    first = _finish_round(repo, finding_id="A-R1-001")
    _commit(repo, "round two fix\n")
    decisions_one = _write_json(
        repo / ".review/inbox/round-1/decisions.json",
        [_decision(1, "A-R1-001")],
    )
    second = begin_round(
        repo,
        base="master",
        runtime="codex",
        round_number=2,
        decisions_path=decisions_one,
    )
    second_response = {
        "decision_id": "D-R1-A-001",
        "outcome": "reissued",
        "replacement_finding_id": "A-R2-001",
    }
    finalize_round(
        repo,
        reviewer_paths=_reports(
            repo,
            second.snapshot,
            round_number=2,
            finding_id="A-R2-001",
            prior_response=second_response,
        ),
    )
    _commit(repo, "round three fix\n")
    decisions_two = _write_json(
        repo / ".review/inbox/round-2/decisions.json",
        [_decision(2, "A-R2-001")],
    )
    third = begin_round(
        repo,
        base="master",
        runtime="codex",
        round_number=3,
        decisions_path=decisions_two,
    )
    third_response = {
        "decision_id": "D-R2-A-001",
        "outcome": "reissued",
        "replacement_finding_id": "A-R3-001",
    }
    terminal = finalize_round(
        repo,
        reviewer_paths=_reports(
            repo,
            third.snapshot,
            round_number=3,
            finding_id="A-R3-001",
            prior_response=third_response,
        ),
    )
    assert first.gate.blocking_count == terminal.gate.blocking_count == 1
    return terminal


def _verdict_payload(repo: Path) -> dict[str, object]:
    return json.loads((repo / ".review/verdict.json").read_text(encoding="utf-8"))


def _replace_verdict(repo: Path, value: object) -> None:
    _write_json(repo / ".review/verdict.json", value)


def payload(
    repo: Path,
    command: object,
    *,
    camel: bool = False,
    tool: object = "Bash",
) -> dict[str, object]:
    if camel:
        return {
            "hookEventName": "PreToolUse",
            "toolName": tool,
            "cwd": str(repo),
            "toolInput": {"command": command},
        }
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "cwd": str(repo),
        "tool_input": {"command": command},
    }


@pytest.fixture
def installed_package(tmp_path: Path) -> Path:
    target = tmp_path / "installed" / "pre_pr_tribunal"
    shutil.copytree(PACKAGE, target)
    return target


def run_adapter(
    installed_package: Path,
    name: str,
    body: object,
    repo: Path,
) -> subprocess.CompletedProcess[str]:
    raw = body if isinstance(body, str) else json.dumps(body)
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(HOME=str(repo.parent), PYTHONNOUSERSITE="1")
    return subprocess.run(
        [sys.executable, str(installed_package / name)],
        input=raw,
        text=True,
        capture_output=True,
        check=False,
        cwd=repo,
        env=environment,
    )


def assert_decision(repo: Path, expected: GateCode) -> None:
    decision = evaluate_gate(repo, BOUND_COMMAND)
    assert decision.code is expected
    assert decision.block is (expected is not GateCode.PASS)


def test_scan_precedes_repository_and_verdict_work(git_repo: Path):
    outside = git_repo.parent / "not-a-repository"
    outside.mkdir()
    assert evaluate_gate(outside, "gh pr view").code is GateCode.NOT_PR_CREATE
    assert evaluate_gate(outside, "gh pr view").block is False
    ambiguous = evaluate_gate(outside, "gh pr create > # missing operand")
    assert ambiguous == (type(ambiguous))(True, GateCode.COMMAND_AMBIGUOUS)


@pytest.mark.parametrize(
    "command",
    [
        "exec -- gh pr create --draft",
        "exec -cl gh pr create --draft",
        "/usr/bin/time --format elapsed gh pr create --draft",
        "g$'\\150' pr create --draft",
        "gh $'--re\\160o=owner/repo' pr create --draft",
        "gh p$'r' create --draft",
    ],
)
def test_shell_forms_cannot_bypass_state_preflight(tmp_path: Path, command: str):
    decision = evaluate_gate(tmp_path, command)

    assert decision.block is True
    assert decision.code is not GateCode.NOT_PR_CREATE


@pytest.mark.parametrize(
    "command",
    (
        "g$'\\x68' p$'\\x72' c$'\\x72'eate --base master",
        "bash -c $'g\\x68 pr create --base master'",
        'bash -c "gh pr create --base master $DYNAMIC"',
        "env -S 'gh pr create --base master'",
        "env --split-string='gh pr create --base master'",
    ),
)
def test_alternate_argv_cannot_bypass_state_preflight(tmp_path: Path, command: str):
    decision = evaluate_gate(tmp_path, command)

    assert decision.block is True
    assert decision.code is not GateCode.NOT_PR_CREATE


def test_scanner_exception_on_unrelated_request_is_silent(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    canary = "ghp_scanner_exception_canary_123456"
    raw = json.dumps(payload(git_repo, "gh pr view")).encode()

    class Stdin:
        buffer = io.BytesIO(raw)

    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(hook_common.sys, "stdin", Stdin())
    monkeypatch.setattr(hook_common.sys, "stdout", stdout)
    monkeypatch.setattr(hook_common.sys, "stderr", stderr)
    monkeypatch.setattr(
        gate,
        "scan_pr_create",
        lambda _command: (_ for _ in ()).throw(RuntimeError(canary)),
    )

    assert hook_common.adapter_main("claude") == 0
    assert stdout.getvalue() == stderr.getvalue() == ""


def test_missing_verdict_requires_tribunal(git_repo: Path):
    assert_decision(git_repo, GateCode.TRIBUNAL_REQUIRED)


@pytest.mark.parametrize(
    "unsafe", ("review_symlink", "verdict_symlink", "mode", "lock")
)
def test_unsafe_store_state_is_bounded(git_repo: Path, unsafe: str):
    held_lock = None
    if unsafe == "review_symlink":
        outside = git_repo.parent / "outside-review"
        outside.mkdir(mode=0o700)
        (git_repo / ".review").symlink_to(outside, target_is_directory=True)
        exclude = git_repo / ".git/info/exclude"
        exclude.write_text(exclude.read_text(encoding="utf-8") + "\n.review\n")
    else:
        _passing_verdict(git_repo)
        verdict = git_repo / ".review/verdict.json"
        if unsafe == "verdict_symlink":
            outside = _write_json(git_repo.parent / "outside.json", {})
            verdict.unlink()
            verdict.symlink_to(outside)
        elif unsafe == "mode":
            verdict.chmod(0o644)
        else:
            held_lock = os.open(git_repo / ".review/lock", os.O_RDWR)
            fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert_decision(git_repo, GateCode.VERDICT_UNSAFE)
    finally:
        if held_lock is not None:
            os.close(held_lock)


@pytest.mark.parametrize(
    "raw",
    (
        "not json",
        '{"schema":1,"schema":1}',
        "[]",
        "{}",
        '{"schema":true}',
        '{"schema":"99"}',
        '{"schema":99.0}',
    ),
)
def test_malformed_or_non_strict_verdict_is_invalid(git_repo: Path, raw: str):
    _passing_verdict(git_repo)
    verdict = git_repo / ".review/verdict.json"
    verdict.write_text(raw, encoding="utf-8")
    verdict.chmod(0o600)
    assert_decision(git_repo, GateCode.VERDICT_INVALID)


def test_newer_integer_verdict_schema_is_distinct_and_stale(git_repo: Path):
    _passing_verdict(git_repo)
    verdict = git_repo / ".review/verdict.json"
    verdict.write_text('{"schema":99}', encoding="utf-8")
    verdict.chmod(0o600)

    with pytest.raises(SchemaError, match="VERDICT_SCHEMA_UNSUPPORTED"):
        read_verdict(git_repo)
    assert_decision(git_repo, GateCode.VERDICT_STALE)


def test_in_progress_review_is_incomplete(git_repo: Path):
    begin_round(git_repo, base="master", runtime="codex", round_number=1)
    assert_decision(git_repo, GateCode.REVIEW_INCOMPLETE)


def test_open_blocker_and_round_limit_are_distinct(git_repo: Path):
    _finish_round(git_repo, finding_id="A-R1-001")
    assert_decision(git_repo, GateCode.BLOCKERS_OPEN)


def test_round_three_failure_exhausts_the_gate(git_repo: Path):
    _round_three_failure(git_repo)
    assert_decision(git_repo, GateCode.ROUND_LIMIT_EXHAUSTED)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("repository", GateCode.VERDICT_STALE),
        ("head_ref", GateCode.VERDICT_STALE),
        ("base_sha", GateCode.VERDICT_STALE),
        ("merge_base", GateCode.VERDICT_STALE),
        ("digest", GateCode.VERDICT_STALE),
        ("round", GateCode.VERDICT_INVALID),
    ),
)
def test_persisted_binding_mutations_fail_closed(
    git_repo: Path, mutation: str, expected: GateCode
):
    _passing_verdict(git_repo)
    value = _verdict_payload(git_repo)
    if mutation == "repository":
        value["repository"] = "other/repository"
    elif mutation == "head_ref":
        value["head_ref"] = "refs/heads/alternate"
    elif mutation == "base_sha":
        value["base"]["sha"] = value["head_sha"]
    elif mutation == "merge_base":
        value["merge_base_sha"] = value["head_sha"]
    elif mutation == "digest":
        value["diff_sha256"] = "0" * 64
    else:
        value["round"] = 4
    _replace_verdict(git_repo, value)
    assert_decision(git_repo, expected)


def test_remote_base_move_is_stale(git_repo: Path):
    _passing_verdict(git_repo)
    _git(git_repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    assert_decision(git_repo, GateCode.VERDICT_STALE)


def test_deleted_remote_base_ref_is_stale(git_repo: Path):
    _passing_verdict(git_repo)
    _git(git_repo, "update-ref", "-d", "refs/remotes/origin/master")
    assert_decision(git_repo, GateCode.VERDICT_STALE)


def test_malformed_stored_base_ref_is_invalid(git_repo: Path):
    _passing_verdict(git_repo)
    value = _verdict_payload(git_repo)
    value["base"]["ref"] = "bad..ref"
    _replace_verdict(git_repo, value)
    assert_decision(git_repo, GateCode.VERDICT_INVALID)


def test_head_move_is_stale(git_repo: Path):
    _passing_verdict(git_repo)
    _commit(git_repo, "head moved\n")
    assert_decision(git_repo, GateCode.VERDICT_STALE)


def test_symbolic_head_change_at_same_commit_is_stale(git_repo: Path):
    _passing_verdict(git_repo)
    _git(git_repo, "branch", "alternate")
    _git(git_repo, "checkout", "-q", "alternate")

    assert_decision(git_repo, GateCode.VERDICT_STALE)


def test_dirty_worktree_precedes_unsafe_verdict(git_repo: Path):
    _passing_verdict(git_repo)
    (git_repo / ".review/verdict.json").chmod(0o644)
    (git_repo / "dirty-canary.txt").write_text("dirty", encoding="utf-8")
    assert_decision(git_repo, GateCode.WORKTREE_DIRTY)


def test_strict_invalid_verdict_precedes_later_snapshot_drift(git_repo: Path):
    _passing_verdict(git_repo)
    _commit(git_repo, "head moved after verdict corruption\n")
    verdict = git_repo / ".review/verdict.json"
    verdict.write_text("not-json", encoding="utf-8")
    verdict.chmod(0o600)
    assert_decision(git_repo, GateCode.VERDICT_INVALID)


def test_snapshot_drift_precedes_schema_valid_in_progress_state(git_repo: Path):
    begin_round(git_repo, base="master", runtime="codex", round_number=1)
    _commit(git_repo, "head moved during review\n")
    assert_decision(git_repo, GateCode.VERDICT_STALE)


def test_exact_root_precedes_dirty_and_malformed_verdict(git_repo: Path):
    _passing_verdict(git_repo)
    nested = git_repo / ".review/nested"
    nested.mkdir()
    verdict = git_repo / ".review/verdict.json"
    verdict.write_text("not-json", encoding="utf-8")
    verdict.chmod(0o600)
    (git_repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    assert_decision(nested, GateCode.REPOSITORY_UNSUPPORTED)


def test_subdirectory_non_github_and_non_git_roots_are_unsupported(
    git_repo: Path, tmp_path: Path
):
    _passing_verdict(git_repo)
    child = git_repo / "child"
    child.mkdir()
    assert_decision(child, GateCode.REPOSITORY_UNSUPPORTED)
    _git(git_repo, "remote", "set-url", "origin", "https://gitlab.com/o/r.git")
    assert_decision(git_repo, GateCode.REPOSITORY_UNSUPPORTED)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert_decision(outside, GateCode.REPOSITORY_UNSUPPORTED)


def test_exact_current_terminal_pass_allows_runtime_policy(git_repo: Path):
    _passing_verdict(git_repo)
    assert evaluate_gate(git_repo, BOUND_COMMAND).code is GateCode.PASS
    assert evaluate_gate(git_repo, BOUND_COMMAND).block is False


@pytest.mark.parametrize(
    "command",
    (
        COMMAND,
        "GH_REPO=other/repo gh pr create --base master",
        "env GH_HOST=github.example gh pr create --base master",
        "gh --repo other/repo pr create --base master",
        "gh pr create --base other",
        "gh pr create --base master --head other:branch",
    ),
)
def test_passing_verdict_rejects_unbound_target(git_repo: Path, command: str):
    _passing_verdict(git_repo)

    decision = evaluate_gate(git_repo, command)

    assert decision.block is True
    assert decision.code is GateCode.COMMAND_AMBIGUOUS


@pytest.mark.parametrize(
    "command",
    (
        "gh pr create --base master --{head,head}=other",
        r"env -S 'gh\_pr\_create\_--base\_master'",
        "env G'H'_REPO=other/repo gh pr create --base master",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.other.insteadOf "
        "GIT_CONFIG_VALUE_0=origin gh pr create --base master",
        "bash -c 'gh pr create --base master'",
        'gh pr create --base master --title "${VALUE@P}"',
    ),
)
def test_passing_verdict_rejects_round_one_bypasses(git_repo: Path, command: str):
    _passing_verdict(git_repo)

    decision = evaluate_gate(git_repo, command)

    assert decision.block is True
    assert decision.code is GateCode.COMMAND_AMBIGUOUS


@pytest.mark.parametrize(
    "command",
    (
        "env -vS 'gh pr create --base master'",
        "env -iv -S 'gh pr create --base master'",
        "env --unset OLD --debug --s='gh pr create --base master'",
        "env --split-str='gh pr create --base master'",
        "env -S 'gh' pr create --base master",
        "bash -O extglob -c 'gh pr create --base master'",
        "bash -o posix -c 'gh pr create --base master'",
        "gh pr --repo owner/repository create --base master",
    ),
)
def test_passing_verdict_rejects_round_two_bypasses(git_repo: Path, command: str):
    _passing_verdict(git_repo)

    decision = evaluate_gate(git_repo, command)

    assert decision.block is True
    assert decision.code is GateCode.COMMAND_AMBIGUOUS


@pytest.mark.parametrize(
    "command",
    (
        'env -- "gh" pr create --base master',
        'command -p "gh" pr create --base master',
        r"env -S 'gh\cignored' pr create --base master",
        r"env --split-string='gh\cignored' pr create --base master",
    ),
)
def test_round_three_forms_cannot_bypass_state_preflight(
    tmp_path: Path, command: str
):
    decision = evaluate_gate(tmp_path, command)

    assert decision.block is True
    assert decision.code is not GateCode.NOT_PR_CREATE


@pytest.mark.parametrize(
    "name",
    (
        "GH_REPO",
        "GH_HOST",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_PARAMETERS",
        "GIT_SSH_COMMAND",
    ),
)
def test_passing_verdict_rejects_inherited_target_override(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, name: str
):
    _passing_verdict(git_repo)
    monkeypatch.setenv(name, "other")

    decision = evaluate_gate(git_repo, BOUND_COMMAND)

    assert decision.block is True
    assert decision.code is GateCode.COMMAND_AMBIGUOUS


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("GH_HOST", "github.com"),
        ("GH_CONFIG_DIR", "/isolated/gh-config"),
        ("GIT_PAGER", "cat"),
        ("GIT_OPTIONAL_LOCKS", "0"),
    ),
)
def test_passing_verdict_allows_inherited_non_target_runtime_config(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
):
    _passing_verdict(git_repo)
    monkeypatch.setenv(name, value)

    decision = evaluate_gate(git_repo, BOUND_COMMAND)

    assert decision.block is False
    assert decision.code is GateCode.PASS


@pytest.mark.parametrize(
    ("name", "camel", "tool"),
    (
        ("claude_hook.py", False, "Bash"),
        ("claude_hook.py", True, "Bash"),
        ("codex_hook.py", True, "exec_command"),
        ("codex_hook.py", False, "Bash"),
        ("codex_hook.py", False, "shell"),
        ("codex_hook.py", True, "future_shell_tool"),
    ),
)
def test_missing_verdict_denies_supported_runtime_payloads_from_copied_package(
    installed_package: Path,
    name: str,
    camel: bool,
    tool: str,
    git_repo: Path,
):
    result = run_adapter(
        installed_package,
        name,
        payload(git_repo, COMMAND, camel=camel, tool=tool),
        git_repo,
    )
    decoded = json.loads(result.stdout)
    specific = decoded["hookSpecificOutput"]
    assert result.returncode == 0 and result.stderr == ""
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert "TRIBUNAL_REQUIRED" in specific["permissionDecisionReason"]
    assert result.stdout.endswith("\n")
    assert (
        result.stdout
        == json.dumps(decoded, ensure_ascii=False, separators=(",", ":")) + "\n"
    )


def test_claude_rejects_non_bash_but_codex_accepts_unfamiliar_command_tool(
    installed_package: Path, git_repo: Path
):
    body = payload(git_repo, COMMAND, tool="future_shell_tool")
    claude = run_adapter(installed_package, "claude_hook.py", body, git_repo)
    codex = run_adapter(installed_package, "codex_hook.py", body, git_repo)
    assert claude.returncode == codex.returncode == 0
    assert claude.stdout == claude.stderr == codex.stderr == ""
    assert "TRIBUNAL_REQUIRED" in codex.stdout


def test_unrelated_and_valid_pass_produce_no_output(
    installed_package: Path, git_repo: Path
):
    unrelated = run_adapter(
        installed_package,
        "codex_hook.py",
        payload(git_repo, "gh pr view", camel=True, tool="exec_command"),
        git_repo,
    )
    _passing_verdict(git_repo)
    allowed = run_adapter(
        installed_package,
        "claude_hook.py",
        payload(git_repo, BOUND_COMMAND),
        git_repo,
    )
    assert unrelated.returncode == allowed.returncode == 0
    assert unrelated.stdout == unrelated.stderr == ""
    assert allowed.stdout == allowed.stderr == ""


def test_ambiguous_command_denies_without_reading_state(
    installed_package: Path, git_repo: Path
):
    result = run_adapter(
        installed_package,
        "claude_hook.py",
        payload(git_repo, "gh pr create > # missing operand"),
        git_repo,
    )
    assert result.returncode == 0 and result.stderr == ""
    assert "COMMAND_AMBIGUOUS" in result.stdout
    assert "TRIBUNAL_REQUIRED" not in result.stdout


@pytest.mark.parametrize(
    "body",
    (
        "not-json",
        "[]",
        '{"hook_event_name":"PreToolUse","hook_event_name":"PreToolUse"}',
        '{"hook_event_name":"PostToolUse","tool_name":"Bash","cwd":"/tmp",'
        '"tool_input":{"command":"gh pr create"}}',
        '{"hook_event_name":"PreToolUse","tool_name":"Bash","cwd":"/tmp",'
        '"tool_input":{}}',
    ),
)
@pytest.mark.parametrize("name", ("claude_hook.py", "codex_hook.py"))
def test_unreliable_payloads_are_unrelated_and_silent(
    installed_package: Path, name: str, body: str, git_repo: Path
):
    result = run_adapter(installed_package, name, body, git_repo)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("command", (None, 1, [], {}))
def test_non_string_command_is_unrelated(
    installed_package: Path, command: object, git_repo: Path
):
    result = run_adapter(
        installed_package,
        "codex_hook.py",
        payload(git_repo, command, camel=True, tool="unknown"),
        git_repo,
    )
    assert result.returncode == 0 and result.stdout == result.stderr == ""


@pytest.mark.parametrize("collision", ("event", "tool", "input"))
@pytest.mark.parametrize("name", ("claude_hook.py", "codex_hook.py"))
def test_alias_collisions_are_rejected_instead_of_preferred(
    installed_package: Path, name: str, collision: str, git_repo: Path
):
    body = payload(git_repo, COMMAND)
    if collision == "event":
        body["hookEventName"] = "PreToolUse"
    elif collision == "tool":
        body["toolName"] = "Bash"
    else:
        body["toolInput"] = {"command": COMMAND}
    result = run_adapter(installed_package, name, body, git_repo)
    assert result.returncode == 0 and result.stdout == result.stderr == ""


def test_payload_extra_keys_and_canaries_are_not_reflected(
    installed_package: Path, git_repo: Path
):
    secret = "ghp_payloadcanary123456"
    home = "/home/payload-canary/private"
    body = payload(git_repo, COMMAND, camel=True, tool="new-command-tool")
    body.update(unrelated={"secret": secret, "path": home})
    body["toolInput"]["extra"] = secret
    result = run_adapter(installed_package, "codex_hook.py", body, git_repo)
    assert "TRIBUNAL_REQUIRED" in result.stdout
    assert secret not in result.stdout and home not in result.stdout
    assert result.stderr == ""


def test_verdict_command_and_path_canaries_never_reach_deny_output(
    installed_package: Path, git_repo: Path
):
    _passing_verdict(git_repo)
    secret = "github_pat_verdict_canary_123456"
    absolute = "/home/verdict-canary/private"
    verdict = git_repo / ".review/verdict.json"
    verdict.write_text(f'{{"secret":"{secret}","path":"{absolute}"}}', encoding="utf-8")
    verdict.chmod(0o600)
    command = f"CANARY={secret} gh pr create --body {absolute}"
    result = run_adapter(
        installed_package,
        "claude_hook.py",
        payload(git_repo, command),
        git_repo,
    )
    assert "VERDICT_INVALID" in result.stdout
    assert secret not in result.stdout and absolute not in result.stdout
    assert str(git_repo) not in result.stdout and result.stderr == ""


def _payload_with_byte_size(repo: Path, size: int, marker: str = "") -> str:
    body = payload(repo, COMMAND)
    body["padding"] = marker
    raw = json.dumps(body, separators=(",", ":"))
    remaining = size - len(raw.encode())
    assert remaining >= 0
    body["padding"] = marker + "x" * remaining
    raw = json.dumps(body, separators=(",", ":"))
    assert len(raw.encode()) == size
    return raw


@pytest.mark.parametrize("name", ("claude_hook.py", "codex_hook.py"))
def test_payload_byte_boundary_fails_closed_above_limit(
    installed_package: Path, name: str, git_repo: Path
):
    canary = "payload_boundary_canary_123456"
    at_limit = run_adapter(
        installed_package,
        name,
        _payload_with_byte_size(git_repo, MAX_PAYLOAD_BYTES),
        git_repo,
    )
    over_limit = run_adapter(
        installed_package,
        name,
        _payload_with_byte_size(git_repo, MAX_PAYLOAD_BYTES + 1, canary),
        git_repo,
    )

    assert at_limit.returncode == over_limit.returncode == 0
    assert "TRIBUNAL_REQUIRED" in at_limit.stdout
    assert "COMMAND_AMBIGUOUS" in over_limit.stdout
    assert canary not in over_limit.stdout
    assert at_limit.stderr == over_limit.stderr == ""


@pytest.mark.parametrize("name", ("claude_hook.py", "codex_hook.py"))
def test_excessively_nested_payload_is_silent(
    installed_package: Path, name: str, git_repo: Path
):
    prefix = (
        '{"hook_event_name":"PreToolUse","tool_name":"Bash",'
        f'"cwd":{json.dumps(str(git_repo))},'
        '"tool_input":{"command":"gh pr create","nested":'
    )
    raw = prefix + "[" * 2000 + "0" + "]" * 2000 + "}}"
    result = run_adapter(installed_package, name, raw, git_repo)
    assert result.returncode == 0 and result.stdout == result.stderr == ""


def test_copied_cli_and_both_adapters_bootstrap_without_repository_pythonpath(
    installed_package: Path, git_repo: Path
):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(HOME=str(git_repo.parent), PYTHONNOUSERSITE="1")
    cli = subprocess.run(
        [sys.executable, str(installed_package / "cli.py"), "--help"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert cli.returncode == 2 and cli.stdout == ""
    assert cli.stderr == "PRE_PR_TRIBUNAL:USAGE\n"
    for name in ("claude_hook.py", "codex_hook.py"):
        result = run_adapter(
            installed_package, name, payload(git_repo, COMMAND), git_repo
        )
        assert result.returncode == 0 and result.stderr == ""
        assert "TRIBUNAL_REQUIRED" in result.stdout


def test_reliably_extracted_request_fails_closed_on_unexpected_evaluation_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    canary = "ghp_unexpected_evaluation_canary_123456"
    raw = json.dumps(payload(git_repo, f"CANARY={canary} {COMMAND}")).encode()

    class Stdin:
        buffer = io.BytesIO(raw)

    stdout = io.StringIO()
    monkeypatch.setattr(hook_common.sys, "stdin", Stdin())
    monkeypatch.setattr(hook_common.sys, "stdout", stdout)
    monkeypatch.setattr(
        hook_common,
        "evaluate_gate",
        lambda _cwd, _command: (_ for _ in ()).throw(RuntimeError(canary)),
    )

    assert hook_common.adapter_main("claude") == 0
    assert "VERDICT_INVALID" in stdout.getvalue()
    assert canary not in stdout.getvalue()


def test_direct_gate_fails_closed_on_unexpected_core_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    canary = "ghp_unexpected_core_canary_123456"
    monkeypatch.setattr(
        gate,
        "_exact_clean_root",
        lambda _cwd: (_ for _ in ()).throw(RuntimeError(canary)),
    )

    decision = evaluate_gate(git_repo, COMMAND)
    assert decision.block is True
    assert decision.code is GateCode.VERDICT_INVALID
