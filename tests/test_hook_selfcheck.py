import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "hook-selfcheck.py"


def load_module():
    spec = importlib.util.spec_from_file_location("hook_selfcheck", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HS = load_module()


@pytest.fixture
def env(tmp_path: Path):
    """가짜 저장소 / settings.json / transcript 디렉터리를 만든다."""
    repo = tmp_path / "repo"
    (repo / "hooks").mkdir(parents=True)
    transcripts = tmp_path / "projects"
    transcripts.mkdir()

    def add_hook(name: str, body: str) -> Path:
        path = repo / "hooks" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def wire(entries: list[tuple[str, str, str]]) -> Path:
        settings = tmp_path / "settings.json"
        hooks: dict = {}
        for event, matcher, command in entries:
            hooks.setdefault(event, []).append(
                {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}
            )
        settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        return settings

    def transcript(records: list[dict], name: str = "s.jsonl") -> Path:
        path = transcripts / name
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return path

    heartbeats = tmp_path / "heartbeat"
    heartbeats.mkdir()

    def beat(name: str, age_days: float = 0) -> Path:
        path = heartbeats / name
        path.write_text("x", encoding="utf-8")
        if age_days:
            stamp = time.time() - age_days * 86400
            os.utime(path, (stamp, stamp))
        return path

    def run(settings: Path, days: int = 7):
        return HS.audit(settings, repo, transcripts, days, heartbeats)

    ns = type("Env", (), {})()
    ns.repo, ns.transcripts, ns.tmp = repo, transcripts, tmp_path
    ns.add_hook, ns.wire, ns.transcript, ns.run = add_hook, wire, transcript, run
    ns.beat, ns.heartbeats = beat, heartbeats
    return ns


def status_of(result: dict, basename: str) -> str:
    return row_of(result, basename)["status"]


def row_of(result: dict, basename: str, event: str | None = None) -> dict:
    matches = [
        row for row in result["rows"]
        if row["script"] and row["script"].endswith(basename)
        and (event is None or row["event"] == event)
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one {basename} row for event={event!r}, got {len(matches)}"
        )
    return matches[0]


MARKED = 'print("<system-reminder>[DEMO-MARKER] hi</system-reminder>")\n'


def hook_attachment(attachment: dict, timestamp: str = "2026-08-24T01:00:00Z") -> dict:
    """Claude Code 2.1.241의 실제 hook attachment 바깥 envelope."""
    return {
        "attachment": attachment,
        "cwd": "/tmp/project",
        "entrypoint": "cli",
        "gitBranch": "master",
        "isSidechain": False,
        "parentUuid": "parent-uuid",
        "sessionId": "session-id",
        "session_id": "session-id",
        "timestamp": timestamp,
        "type": "attachment",
        "userType": "external",
        "uuid": "attachment-uuid",
        "version": "2.1.241",
    }


def hook_success(event: str, stdout: str, script: Path) -> dict:
    """실제 hook_success attachment: marker payload는 stdout에 저장된다."""
    return hook_attachment({
        "command": f"python3 {script}",
        "content": "",
        "durationMs": 12,
        "exitCode": 0,
        "hookEvent": event,
        "hookName": event,
        "stderr": "",
        "stdout": stdout,
        "toolUseID": "tool-use-id",
        "type": "hook_success",
    })


def hook_system_message(event: str, content: str) -> dict:
    """실제 hook_system_message attachment: marker payload는 content 문자열이다."""
    return hook_attachment({
        "content": content,
        "hookEvent": event,
        "hookName": event,
        "toolUseID": "tool-use-id",
        "type": "hook_system_message",
    })


def stop_hook_summary(error: str) -> dict:
    """Claude Code가 non-zero Stop hook stderr를 기록하는 실제 summary envelope."""
    return {
        "cwd": "/tmp/project",
        "entrypoint": "cli",
        "gitBranch": "master",
        "hasOutput": True,
        "hookAdditionalContext": [],
        "hookCount": 1,
        "hookErrors": [error],
        "hookInfos": [],
        "isSidechain": False,
        "level": "suggestion",
        "parentUuid": "parent-uuid",
        "preventedContinuation": False,
        "sessionId": "session-id",
        "session_id": "session-id",
        "stopReason": "",
        "subtype": "stop_hook_summary",
        "timestamp": "2026-08-24T01:00:00Z",
        "toolUseID": "tool-use-id",
        "type": "system",
        "userType": "external",
        "uuid": "summary-uuid",
        "version": "2.1.241",
    }


def test_silent_when_marker_never_appears(env) -> None:
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "안녕"}}])

    assert status_of(env.run(settings), "demo-hook.py") == "SILENT"


def test_counts_marker_in_hook_success_stdout(env) -> None:
    """Mutation: hook_success.stdout 경로를 빼면 실제 hook 발화를 SILENT로 오진한다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([hook_success("Stop", "[DEMO-MARKER] hi", hook)])

    result = env.run(settings)
    assert status_of(result, "demo-hook.py") == "ok"
    row = next(r for r in result["rows"] if r["script"].endswith("demo-hook.py"))
    assert row["fired"] == 1
    assert row["last_seen"] == "2026-08-24"


def test_counts_marker_in_hook_system_message_content(env) -> None:
    """Mutation: hook_system_message.content 경로를 빼면 timestamp 계열 훅을 놓친다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("UserPromptSubmit", "*", f"python3 {hook}")])
    env.transcript([hook_system_message("UserPromptSubmit", "[DEMO-MARKER] hi")])

    assert status_of(env.run(settings), "demo-hook.py") == "ok"


def test_counts_stop_hook_summary_error_as_stop_evidence(env) -> None:
    """Mutation: stop_hook_summary를 일반 system으로 버리면 실제 Stop 차단을 놓친다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([stop_hook_summary("[DEMO-MARKER] blocked")])

    row = row_of(env.run(settings), "demo-hook.py", "Stop")
    assert row["status"] == "ok"
    assert row["evidence"] == "marker"


@pytest.mark.parametrize(
    "attachment",
    [
        {
            "type": "hook_success",
            "hookEvent": "Stop",
            "hookName": "Stop",
            "toolUseID": "tool-use-id",
            "stdout": "",
            "stderr": "[DEMO-MARKER] stderr",
            "content": "",
        },
        {
            "type": "hook_additional_context",
            "hookEvent": "Stop",
            "hookName": "Stop",
            "toolUseID": "tool-use-id",
            "content": ["[DEMO-MARKER] context"],
        },
        {
            "type": "hook_non_blocking_error",
            "hookEvent": "Stop",
            "hookName": "Stop",
            "toolUseID": "tool-use-id",
            "stdout": "[DEMO-MARKER] warning",
            "stderr": "",
        },
        {
            "type": "hook_blocking_error",
            "hookEvent": "Stop",
            "hookName": "Stop",
            "toolUseID": "tool-use-id",
            "blockingError": {
                "blockingError": "[DEMO-MARKER] blocked",
                "command": "python3 /tmp/project/hooks/demo-hook.py",
            },
        },
        {
            "type": "async_hook_response",
            "hookEvent": "Stop",
            "hookName": "Stop",
            "processId": "process-id",
            "stdout": "[DEMO-MARKER] async",
            "stderr": "",
            "response": {"continue": True},
        },
    ],
    ids=[
        "success-stderr",
        "additional-context-list",
        "non-blocking-stdout",
        "blocking-error-message",
        "async-stdout",
    ],
)
def test_counts_only_supported_hook_payload_fields(env, attachment: dict) -> None:
    """실제 schema별 출력 필드는 놓치지 않고 구조화된 값도 안전하게 읽는다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    if attachment["type"] == "hook_blocking_error":
        attachment["blockingError"]["command"] = f"python3 {hook}"
    env.transcript([hook_attachment(attachment)])

    assert status_of(env.run(settings), "demo-hook.py") == "ok"


def test_blocking_error_nested_command_mismatch_is_unknown(env) -> None:
    """blockingError.command가 다른 script면 marker가 같아도 발화로 귀속하지 않는다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    attachment = {
        "type": "hook_blocking_error",
        "hookEvent": "Stop",
        "hookName": "Stop",
        "toolUseID": "tool-use-id",
        "blockingError": {
            "blockingError": "[DEMO-MARKER] blocked",
            "command": "python3 /old/deleted/demo-hook.py",
        },
    }
    env.transcript([hook_attachment(attachment)])

    assert status_of(env.run(settings), "demo-hook.py") == "UNKNOWN"


@pytest.mark.parametrize(
    "attachment",
    [
        {
            "type": "hook_blocking_error",
            "hookEvent": "Stop",
            "hookName": "Stop",
            "toolUseID": "tool-use-id",
            "blockingError": {
                "blockingError": "ordinary error",
                "command": "echo [DEMO-MARKER]",
            },
        },
        {
            "type": "async_hook_response",
            "hookEvent": "Stop",
            "hookName": "Stop",
            "processId": "process-id",
            "stdout": "",
            "stderr": "",
            "response": {"diagnostic": "[DEMO-MARKER] metadata"},
        },
    ],
    ids=["blocking-command", "async-response-metadata"],
)
def test_ignores_marker_in_hook_provenance_fields(env, attachment: dict) -> None:
    """Mutation: hook payload 객체 전체를 검색하면 command/response 메타를 발화로 센다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([hook_attachment(attachment)])

    assert status_of(env.run(settings), "demo-hook.py") == "SILENT"


@pytest.mark.parametrize(
    "record",
    [
        {
            "cwd": "/tmp/project",
            "entrypoint": "cli",
            "gitBranch": "master",
            "isSidechain": False,
            "message": {"role": "user", "content": "[DEMO-MARKER] source text"},
            "parentUuid": "parent-uuid",
            "promptId": "prompt-id",
            "sessionId": "session-id",
            "session_id": "session-id",
            "timestamp": "2026-08-24T02:00:00Z",
            "type": "user",
            "userType": "external",
            "uuid": "user-uuid",
            "version": "2.1.241",
        },
        {
            "cwd": "/tmp/project",
            "entrypoint": "sdk-cli",
            "gitBranch": "master",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [{
                    "content": "[DEMO-MARKER] command output",
                    "is_error": False,
                    "tool_use_id": "tool-use-id",
                    "type": "tool_result",
                }],
            },
            "parentUuid": "parent-uuid",
            "promptId": "prompt-id",
            "sessionId": "session-id",
            "sourceToolAssistantUUID": "assistant-uuid",
            "timestamp": "2026-08-24T02:00:00Z",
            "toolUseResult": {
                "interrupted": False,
                "isImage": False,
                "noOutputExpected": False,
                "stderr": "",
                "stdout": "[DEMO-MARKER] command output",
            },
            "type": "user",
            "userType": "external",
            "uuid": "tool-result-uuid",
            "version": "2.1.241",
        },
        {
            "attachment": {
                "content": {
                    "file": {
                        "content": "print('[DEMO-MARKER] source code')",
                        "filePath": "/tmp/project/demo.py",
                        "numLines": 1,
                        "startLine": 1,
                        "totalLines": 1,
                    },
                    "type": "text",
                },
                "displayPath": "demo.py",
                "filename": "demo.py",
                "type": "file",
            },
            "cwd": "/tmp/project",
            "entrypoint": "cli",
            "gitBranch": "master",
            "isSidechain": False,
            "parentUuid": "parent-uuid",
            "sessionId": "session-id",
            "session_id": "session-id",
            "slug": "sample-session",
            "timestamp": "2026-08-24T02:00:00Z",
            "type": "attachment",
            "userType": "external",
            "uuid": "file-uuid",
            "version": "2.1.241",
        },
        {
            "content": "[DEMO-MARKER] ordinary system notice",
            "cwd": "/tmp/project",
            "entrypoint": "cli",
            "gitBranch": "master",
            "isMeta": False,
            "isSidechain": False,
            "level": "info",
            "parentUuid": "parent-uuid",
            "sessionId": "session-id",
            "subtype": "informational",
            "timestamp": "2026-08-24T02:00:00Z",
            "type": "system",
            "userType": "external",
            "uuid": "system-uuid",
            "version": "2.1.241",
        },
    ],
    ids=["raw-user", "tool-result", "file-attachment", "general-system"],
)
def test_ignores_marker_outside_actual_hook_evidence(env, record: dict) -> None:
    """Mutation: 승인된 hook payload 대신 레코드 전체를 검색하면 이 오염을 센다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([record])

    assert status_of(env.run(settings), "demo-hook.py") == "SILENT"


def test_marker_evidence_is_scoped_to_script_event(env) -> None:
    """Mutation: hit를 marker/script 전역으로 합치면 Stop 발화가 UserPromptSubmit도 구제한다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([
        ("UserPromptSubmit", "*", f"python3 {hook}"),
        ("Stop", "*", f"python3 {hook}"),
    ])
    env.transcript([hook_success("Stop", "[DEMO-MARKER] hi", hook)])

    result = env.run(settings)
    assert row_of(result, "demo-hook.py", "Stop")["status"] == "ok"
    assert row_of(result, "demo-hook.py", "UserPromptSubmit")["status"] == "SILENT"


def test_hook_command_mismatch_is_unknown(env) -> None:
    """Mutation: 명시된 command가 다른데 marker만 같다고 해당 script에 귀속하면 실패한다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    record = hook_success("Stop", "[DEMO-MARKER] hi", hook)
    record["attachment"]["command"] = "python3 /old/deleted/demo-hook.py"
    env.transcript([record])

    assert status_of(env.run(settings), "demo-hook.py") == "UNKNOWN"


def test_duplicate_hook_envelopes_count_once(env) -> None:
    """같은 hook 호출의 success/system_message 이중 기록은 발화 한 번이다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([
        hook_success("Stop", "[DEMO-MARKER] hi", hook),
        hook_system_message("Stop", "[DEMO-MARKER] hi"),
    ])

    assert row_of(env.run(settings), "demo-hook.py", "Stop")["fired"] == 1


def unsupported_hook_attachment(event: str = "Stop") -> dict:
    """미래 Claude 버전의 미지원 hook attachment를 본뜬 완전한 outer envelope."""
    return hook_attachment({
        "content": "[DEMO-MARKER] future payload",
        "hookEvent": event,
        "hookName": event,
        "toolUseID": "tool-use-id",
        "type": "hook_future_result",
    })


def test_unknown_hook_attachment_schema_is_unknown(env) -> None:
    """Mutation: 미지원 hook_* schema를 raw marker만 보고 정상으로 추정하면 실패한다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([unsupported_hook_attachment()])

    assert status_of(env.run(settings), "demo-hook.py") == "UNKNOWN"


def test_strict_fails_for_unknown_hook_schema(env, capsys) -> None:
    """Mutation: UNKNOWN을 strict 차단 상태에서 누락하면 회귀를 통과시킨다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([unsupported_hook_attachment()])
    args = [
        "--settings", str(settings),
        "--repo", str(env.repo),
        "--transcripts", str(env.transcripts),
        "--heartbeats", str(env.heartbeats),
        "--strict",
    ]

    assert HS.main(args) == 1
    assert "UNKNOWN" in capsys.readouterr().out


def test_assistant_self_mention_is_not_a_firing(env) -> None:
    """모델이 마커를 언급한 텍스트를 발화로 세면 무동작 훅이 정상으로 보인다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([
        {"type": "assistant", "timestamp": "2026-08-24T03:00:00Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "[DEMO-MARKER] 얘기를 해봅시다"}]}},
    ])

    assert status_of(env.run(settings), "demo-hook.py") == "SILENT"


def test_flags_missing_script(env) -> None:
    settings = env.wire([("Stop", "*", f"python3 {env.repo}/hooks/gone.py")])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "x"}}])

    assert status_of(env.run(settings), "gone.py") == "MISSING"


def test_flags_unmanaged_script(env) -> None:
    outside = env.tmp / "outside.sh"
    outside.write_text('echo "[DEMO-MARKER] hi"\n', encoding="utf-8")
    settings = env.wire([("PreCompact", "*", str(outside))])
    env.transcript([{"type": "attachment", "attachment": {"stdout": "[DEMO-MARKER] hi"}}])

    assert status_of(env.run(settings), "outside.sh") == "UNMANAGED"


def test_flags_unobservable_script(env) -> None:
    hook = env.add_hook("quiet-hook.py", "import sys\nsys.exit(0)\n")
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "x"}}])

    assert status_of(env.run(settings), "quiet-hook.py") == "UNOBSERVABLE"


def test_inline_command_is_not_flagged(env) -> None:
    settings = env.wire([("SessionStart", "*", "repowire hook session")])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "x"}}])

    result = env.run(settings)
    assert [r["status"] for r in result["rows"]] == ["inline"]


def test_bare_marker_only_from_string_literal(env) -> None:
    """대문자 상수(SETTLE_ATTEMPTS 등)를 마커로 오인하면 허위 SILENT 가 쏟아진다."""
    hook = env.add_hook(
        "bare-hook.py",
        'SETTLE_ATTEMPTS = 8\nMAX_BYTES = 5\nprint("STOP_LIKE_BLOCK: 경고")\n',
    )

    markers = HS.extract_markers(hook)

    assert markers == {"STOP_LIKE_BLOCK"}


def test_strict_exit_code(env, capsys) -> None:
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "x"}}])
    args = ["--settings", str(settings), "--repo", str(env.repo),
            "--transcripts", str(env.transcripts)]

    assert HS.main(args) == 0            # 기본은 보고만
    assert HS.main(args + ["--strict"]) == 1
    assert "SILENT" in capsys.readouterr().out


def test_ignores_marker_in_comment_only_line(env) -> None:
    """주석의 [TODO] 같은 표기는 주입되지 않는다 — 세면 영구 SILENT 소음이 된다."""
    hook = env.add_hook("commented-hook.py", "# [TODO-LATER] 나중에 처리\nprint('hi')\n")

    assert HS.extract_markers(hook) == set()


def test_rejects_tokens_without_separator(env) -> None:
    """CRITICAL / MANDATORY 처럼 구분자 없는 낱말은 마커가 아니다."""
    hook = env.add_hook(
        "wordy-hook.py",
        'print("CRITICAL: 위험")\nprint("MANDATORY: 필수")\nprint("REAL_HOOK_MARKER: 진짜")\n',
    )

    assert HS.extract_markers(hook) == {"REAL_HOOK_MARKER"}


def test_rejects_regex_character_class_fragment(env) -> None:
    """소스에 정규식이 들어 있으면 [A-Z0-9] 같은 조각이 마커로 잡힌다."""
    hook = env.add_hook(
        "regexy-hook.py",
        'import re\nPAT = re.compile(r"[A-Z0-9]+")\nprint("<x>[REAL-MARKER] hi</x>")\n',
    )

    assert HS.extract_markers(hook) == {"REAL-MARKER"}


# --- 발화 하트비트 ---------------------------------------------------------
# 훅 대부분은 조건부 출력이라 "출력 없음"이 정상일 수 있다. 마커만 보면
# 조건 미충족과 무동작이 구분되지 않는다 — 하트비트가 그 구멍을 막는다.

def test_heartbeat_clears_silent(env) -> None:
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "안녕"}}])
    env.beat("demo-hook.py")

    result = env.run(settings)

    assert status_of(result, "demo-hook.py") == "ok"
    row = next(r for r in result["rows"] if r["script"].endswith("demo-hook.py"))
    assert row["evidence"] == "heartbeat"


def test_stale_heartbeat_stays_silent(env) -> None:
    """관측 창 밖의 하트비트는 발화 증거가 아니다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "안녕"}}])
    env.beat("demo-hook.py", age_days=30)

    assert status_of(env.run(settings, days=7), "demo-hook.py") == "SILENT"


def test_heartbeat_makes_markerless_hook_observable(env) -> None:
    """마커를 낼 수 없는 훅도 하트비트가 있으면 관측 가능하다 (issue #33)."""
    hook = env.add_hook("quiet-hook.py", "import sys\nsys.exit(0)\n")
    settings = env.wire([("PreCompact", "*", f"python3 {hook}")])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "x"}}])
    env.beat("quiet-hook.py")

    assert status_of(env.run(settings), "quiet-hook.py") == "ok"


def test_basename_heartbeat_is_unknown_for_multi_event_script(env) -> None:
    """Mutation: event 없는 legacy heartbeat를 script 모든 event에 공유하면 둘 다 ok가 된다."""
    hook = env.add_hook("quiet-hook.py", "import sys\nsys.exit(0)\n")
    settings = env.wire([
        ("PreToolUse", "*", f"python3 {hook}"),
        ("PostToolUse", "*", f"python3 {hook}"),
    ])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "x"}}])
    env.beat("quiet-hook.py")

    result = env.run(settings)
    assert row_of(result, "quiet-hook.py", "PreToolUse")["status"] == "UNKNOWN"
    assert row_of(result, "quiet-hook.py", "PostToolUse")["status"] == "UNKNOWN"


def test_basename_heartbeat_is_unknown_for_distinct_scripts_with_same_name(env) -> None:
    """같은 heartbeat 파일명을 공유하는 두 script에는 발화를 안전하게 귀속할 수 없다."""
    first = env.add_hook("first/shared-hook.py", "import sys\nsys.exit(0)\n")
    second = env.add_hook("second/shared-hook.py", "import sys\nsys.exit(0)\n")
    settings = env.wire([
        ("PreToolUse", "*", f"python3 {first}"),
        ("PostToolUse", "*", f"python3 {second}"),
    ])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "x"}}])
    env.beat("shared-hook.py")

    rows = [row for row in env.run(settings)["rows"] if row["script"]]
    assert [row["status"] for row in rows] == ["UNKNOWN", "UNKNOWN"]


def test_duplicate_matchers_share_one_unambiguous_heartbeat_owner(env) -> None:
    """같은 script/event의 matcher 여러 개는 heartbeat 귀속 모호성이 아니다."""
    hook = env.add_hook("quiet-hook.py", "import sys\nsys.exit(0)\n")
    settings = env.wire([
        ("PreToolUse", "Bash", f"python3 {hook}"),
        ("PreToolUse", "Edit", f"python3 {hook}"),
    ])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "x"}}])
    env.beat("quiet-hook.py")

    rows = [row for row in env.run(settings)["rows"] if row["script"]]
    assert [row["status"] for row in rows] == ["ok", "ok"]


def test_marker_takes_precedence_over_heartbeat(env) -> None:
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([hook_success("Stop", "[DEMO-MARKER] hi", hook)])
    env.beat("demo-hook.py")

    row = next(r for r in env.run(settings)["rows"] if r["script"].endswith("demo-hook.py"))
    assert row["evidence"] == "marker"
    assert row["fired"] == 1


def test_missing_script_not_rescued_by_heartbeat(env) -> None:
    """파일이 없는 배선은 하트비트가 있어도 MISSING 이다."""
    settings = env.wire([("Stop", "*", f"python3 {env.repo}/hooks/gone.py")])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "x"}}])
    env.beat("gone.py")

    assert status_of(env.run(settings), "gone.py") == "MISSING"


def test_heartbeat_filename_follows_invoked_script_name(tmp_path: Path) -> None:
    """하트비트 파일명은 호출 경로의 basename 이어야 한다.

    hook-selfcheck 는 settings.json 에 배선된 경로의 basename 으로 하트비트를
    찾는다. 훅이 파일명을 하드코딩하면 이름을 바꿨을 때 자가진단이 조용히
    하트비트를 못 찾고, 그게 이 장치가 막으려던 실패 양식이다.
    """
    hook = Path(__file__).parents[1] / "hooks" / "precompact-handoff.sh"
    beats = tmp_path / "beats"
    project = tmp_path / "project"
    project.mkdir()
    # 다른 이름으로 호출해도 그 이름으로 남아야 한다.
    alias = tmp_path / "renamed-gate.sh"
    alias.symlink_to(hook)

    for target in (hook, alias):
        subprocess.run(
            ["bash", str(target)],
            input='{"trigger":"manual"}',
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ,
                 "CLAUDE_HOOK_HEARTBEAT_DIR": str(beats),
                 "CLAUDE_PROJECT_DIR": str(project)},
        )

    assert (beats / hook.name).is_file()
    assert (beats / alias.name).is_file()


# --- HOOK-OBSERVABLE 선언 --------------------------------------------------
# 출력이 이미 고유하지만 마커 형태가 아닌 훅(예: systemMessage 를 내는 훅)은
# 사용자 노출 텍스트를 바꾸지 않고 관측 문자열을 소스에 선언한다.

def test_declared_string_is_used_as_marker(env) -> None:
    hook = env.add_hook(
        "declared-hook.py",
        '# HOOK-OBSERVABLE: 🕐 prompt @\nprint("🕐 prompt @ 2026-08-24")\n',
    )

    assert HS.extract_markers(hook) == {"🕐 prompt @"}


def test_declaration_keyword_is_not_a_marker(env) -> None:
    """HOOK-OBSERVABLE 자체는 마커 형태에 맞아서 그냥 두면 영구 SILENT 가 된다."""
    hook = env.add_hook("declared-hook.py", "# HOOK-OBSERVABLE: 🕐 prompt @\n")

    assert HS.DECLARE_KEYWORD not in HS.extract_markers(hook)


def test_declared_marker_detected_in_transcript(env) -> None:
    hook = env.add_hook(
        "declared-hook.py",
        '# HOOK-OBSERVABLE: 🕐 prompt @\nprint("🕐 prompt @ x")\n',
    )
    settings = env.wire([("UserPromptSubmit", "*", f"python3 {hook}")])
    env.transcript([
        hook_system_message("UserPromptSubmit", "🕐 prompt @ 2026-08-24 17:00:00")
    ])

    result = env.run(settings)
    row = next(r for r in result["rows"] if r["script"].endswith("declared-hook.py"))
    assert row["status"] == "ok"
    assert row["evidence"] == "marker"
    assert row["fired"] == 1


def test_multiple_declarations_all_counted(env) -> None:
    hook = env.add_hook(
        "declared-hook.py",
        '# HOOK-OBSERVABLE: 🕐 prompt @\n# HOOK-OBSERVABLE: ✅ done @\nprint("x")\n',
    )

    assert HS.extract_markers(hook) == {"🕐 prompt @", "✅ done @"}
