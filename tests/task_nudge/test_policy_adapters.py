import json

import pytest


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"status": "registered", "work": "excluded"}, "no_task"),
        ({"status": "unregistered", "work": "backlog"}, "github_issue_only"),
        ({"status": "unknown", "work": "backlog"}, "github_issue_only"),
        ({"status": "unknown", "work": "immediate"}, "stop_for_classification"),
        ({"status": "registered", "work": "immediate", "existing_issue": True}, "formal_issue_task"),
        ({"status": "registered", "work": "immediate", "recurring": True}, "formal_issue_task"),
        ({"status": "registered", "work": "immediate", "bounded": True}, "temporary_task"),
        ({"status": "registered", "work": "immediate"}, "no_task"),
        ({"status": "unregistered", "work": "immediate", "recurring": True}, "register_repository"),
        ({"status": "unregistered", "work": "immediate"}, "no_task"),
    ],
)
def test_policy_matrix(core, context, expected):
    policy = core.PolicyContext.from_strings(**context)
    assert core.suggest_action(policy).value == expected


def test_recurring_evidence_requires_an_explicit_permitted_signal(core):
    assert core.has_recurring_evidence(explicit_long_running=True)
    assert core.has_recurring_evidence(existing_issue_plan_or_handoff=True)
    assert core.has_recurring_evidence(architectural_multistage=True)
    assert not core.has_recurring_evidence(file_count=99)
    assert not core.has_recurring_evidence(repository_present=True)


def test_nudge_message_is_canonical_safe_korean_policy(core):
    message = core.render_nudge_message(
        core.RegistrationResult(core.RegistrationStatus.UNKNOWN, None, "PORTFOLIO_UNAVAILABLE")
    )
    assert message.startswith("[TASK-NUDGE]")
    assert "즉시 작업의 unknown이면" in message
    assert "backlog(unknown 포함)" in message
    assert "GitHub Issue만 제안하고 Task/Claim을 선점하거나 시작하지 않음" in message
    assert "GitHub Issue 생성" in message
    assert "Project/Repository 등록" in message
    assert "Task 시작" in message
    assert "장기·반복·여러 세션" in message
    assert "기존 GitHub Issue·승인된 계획·Handoff" in message
    assert "여러 구현 단계와 검증" in message
    assert "subagent" in message and "이미 결정" in message
    assert "/home/" not in message and "session-secret" not in message
    order = [
        "(1) 이미 결정됨·제외 작업",
        "(2) backlog(unknown 포함)",
        "(3) 즉시 작업의 unknown",
        "(4) 등록 저장소의 즉시 작업",
        "(5) 미등록 저장소의 즉시 작업",
    ]
    assert [message.index(item) for item in order] == sorted(message.index(item) for item in order)
    assert message.index("backlog(unknown 포함)") < message.index("즉시 작업의 unknown이면")


def _payload(repo, *, runtime):
    base = {
        "session_id": f"{runtime}-session-secret-token",
        "cwd": str(repo),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / "private-secret.py")},
    }
    if runtime == "codex":
        base["hook_event_name"] = "PreToolUse"
    return base


def test_codex_adapter_emits_only_system_message_once(repo, registered_home, run_adapter):
    payload = _payload(repo, runtime="codex")
    first = run_adapter("task-nudge-codex.py", payload, registered_home)
    second = run_adapter("task-nudge-codex.py", payload, registered_home)
    assert first.returncode == 0 and second.returncode == 0
    decoded = json.loads(first.stdout)
    assert set(decoded) == {"systemMessage"}
    assert decoded["systemMessage"].startswith("[TASK-NUDGE]")
    assert second.stdout == second.stderr == ""


def test_claude_adapter_preserves_plaintext_contract(repo, registered_home, run_adapter):
    result = run_adapter("task-nudge-claude.py", _payload(repo, runtime="claude"), registered_home)
    assert result.returncode == 0
    assert result.stdout.startswith("[TASK-NUDGE]")
    assert not result.stdout.lstrip().startswith("{")
    assert result.stderr == ""


@pytest.mark.parametrize("name", ["task-nudge-claude.py", "task-nudge-codex.py"])
def test_adapters_emit_bounded_invalid_input_without_canaries(name, home, run_adapter):
    secret = "super-secret-/home/leak"
    result = run_adapter(name, {"secret": secret}, home)
    assert result.returncode == 0 and result.stderr == ""
    output = result.stdout
    if name.endswith("codex.py"):
        output = json.loads(output)["systemMessage"]
    assert "HOOK_INPUT_INVALID" in output
    assert secret not in output and "/home/" not in output


@pytest.mark.parametrize("name", ["task-nudge-claude.py", "task-nudge-codex.py"])
def test_adapters_reject_oversized_stdin_with_bounded_output(name, home, run_adapter):
    result = run_adapter(name, {"padding": "x" * (1024 * 1024 + 1)}, home)
    assert result.returncode == 0 and result.stderr == ""
    output = json.loads(result.stdout)["systemMessage"] if name.endswith("codex.py") else result.stdout
    assert "HOOK_INPUT_INVALID" in output
    assert "padding" not in output and "x" * 32 not in output


@pytest.mark.parametrize("name", ["task-nudge-claude.py", "task-nudge-codex.py"])
@pytest.mark.parametrize(
    "raw_input",
    [
        "{malformed-json",
        "[]",
        "{\"nested\":" * 1100 + "null" + "}" * 1100,
    ],
    ids=["malformed", "non-object", "deeply-nested"],
)
def test_adapters_classify_invalid_json_without_creating_a_marker(name, raw_input, home, run_adapter):
    result = run_adapter(name, raw_input, home)
    output = json.loads(result.stdout)["systemMessage"] if name.endswith("codex.py") else result.stdout
    assert result.returncode == 0 and result.stderr == ""
    assert "HOOK_INPUT_INVALID" in output
    assert not list((home / "runtime").rglob("*"))


def test_adapters_suppress_skip_subagent_and_already_decided(repo, registered_home, run_adapter):
    skipped = _payload(repo, runtime="claude")
    skipped["tool_input"] = {"file_path": str(registered_home / ".claude" / "settings.json")}
    subagent = _payload(repo, runtime="codex")
    subagent["is_subagent"] = True
    decided = _payload(repo, runtime="codex")
    decided["already_decided"] = True
    for name, payload in (("task-nudge-claude.py", skipped), ("task-nudge-codex.py", subagent), ("task-nudge-codex.py", decided)):
        result = run_adapter(name, payload, registered_home)
        assert result.returncode == 0 and result.stdout == result.stderr == ""


def test_unregistered_message_and_unknown_repeats_without_marker(repo, unregistered_home, run_adapter):
    unregistered = run_adapter("task-nudge-claude.py", _payload(repo, runtime="claude"), unregistered_home)
    assert "unregistered" in unregistered.stdout
    unknown_payload = _payload(repo, runtime="codex")
    unknown_home = repo.parent / "unknown-home"
    unknown_home.mkdir(mode=0o700)
    (unknown_home / "runtime").mkdir(mode=0o700)
    (unknown_home / "scratch").mkdir(mode=0o700)
    first = run_adapter("task-nudge-codex.py", unknown_payload, unknown_home)
    second = run_adapter("task-nudge-codex.py", unknown_payload, unknown_home)
    assert "PORTFOLIO_UNAVAILABLE" in json.loads(first.stdout)["systemMessage"]
    assert "PORTFOLIO_UNAVAILABLE" in json.loads(second.stdout)["systemMessage"]


def test_manual_check_is_stateless_bounded_json(repo, registered_home, run_manual):
    first = run_manual(repo, registered_home)
    second = run_manual(repo, registered_home)
    expected = {"repository_slug": "jhw7500/claude-config", "registration_status": "registered"}
    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout) == expected
    assert json.loads(second.stdout) == expected
    assert first.stderr == second.stderr == ""


def _adapter_message(name, result):
    assert result.returncode == 0
    if name.endswith("codex.py"):
        assert set(json.loads(result.stdout)) == {"systemMessage"}
        return json.loads(result.stdout)["systemMessage"]
    return result.stdout


@pytest.mark.parametrize("name", ["task-nudge-claude.py", "task-nudge-codex.py"])
def test_runtime_outputs_never_leak_success_or_unknown_canaries(name, repo, registered_home, run_adapter):
    payload = _payload(repo, runtime="codex" if name.endswith("codex.py") else "claude")
    payload.update({
        "credential": "CREDENTIAL_CANARY", "project_id": "PROJECT_ID_CANARY",
        "repository_id": "REPOSITORY_ID_CANARY", "task_id": "TASK_ID_CANARY",
        "claim_id": "CLAIM_ID_CANARY",
    })
    success = run_adapter(name, payload, registered_home)
    _adapter_message(name, success)
    unknown_home = repo.parent / f"unknown-{name}"
    unknown_home.mkdir(mode=0o700)
    (unknown_home / "runtime").mkdir(mode=0o700)
    (unknown_home / "scratch").mkdir(mode=0o700)
    unknown = run_adapter(name, payload, unknown_home)
    _adapter_message(name, unknown)
    canaries = [
        str(repo), "private-secret.py", payload["session_id"], "CREDENTIAL_CANARY",
        "PROJECT_ID_CANARY", "REPOSITORY_ID_CANARY", "TASK_ID_CANARY", "CLAIM_ID_CANARY",
    ]
    for text in (success.stdout + success.stderr, unknown.stdout + unknown.stderr):
        assert all(canary not in text for canary in canaries)


@pytest.mark.parametrize("name", ["task-nudge-claude.py", "task-nudge-codex.py"])
def test_runtime_outputs_never_leak_launcher_or_invalid_input_canaries(name, repo, home, run_adapter, install_launcher):
    payload = _payload(repo, runtime="codex" if name.endswith("codex.py") else "claude")
    payload["exception_detail"] = "EXCEPTION_TEXT_CANARY"
    install_launcher(
        home,
        {
            "command": "portfolio status",
            "result": {
                "project_id": "PROJECT_ID_CANARY", "repo_id": "REPOSITORY_ID_CANARY",
                "task_id": "TASK_ID_CANARY", "claim_id": "CLAIM_ID_CANARY",
                "credential": "CREDENTIAL_CANARY",
            },
        },
        exit_code=1,
        stdout_prefix="RAW_CHILD_OUTPUT_CANARY",
        stderr="RAW_CHILD_STDERR_CANARY EXCEPTION_TEXT_CANARY",
    )
    launcher = run_adapter(name, payload, home)
    invalid = run_adapter(name, "{EXCEPTION_TEXT_CANARY", home)
    _adapter_message(name, launcher)
    _adapter_message(name, invalid)
    canaries = [
        str(repo), "private-secret.py", payload["session_id"], "EXCEPTION_TEXT_CANARY",
        "RAW_CHILD_OUTPUT_CANARY", "RAW_CHILD_STDERR_CANARY", "PROJECT_ID_CANARY",
        "REPOSITORY_ID_CANARY", "TASK_ID_CANARY", "CLAIM_ID_CANARY", "CREDENTIAL_CANARY",
    ]
    for text in (launcher.stdout + launcher.stderr, invalid.stdout + invalid.stderr):
        assert all(canary not in text for canary in canaries)
