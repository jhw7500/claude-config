import importlib.util
import io
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "stop-text-required.py"


def load_hook_module():
    """하이픈이 들어간 훅 스크립트를 모듈로 로드한다 (단위 테스트용)."""
    spec = importlib.util.spec_from_file_location("stop_text_required", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = load_hook_module()
NO_SLEEP = lambda _delay: None


def run_hook(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
    )


def tool_only_messages(user_text: str = "계속") -> list[dict]:
    return [
        {"role": "user", "content": user_text},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}},
            ],
        },
    ]


def write_jsonl_records(tmp_path: Path, records: list[dict]) -> Path:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    return transcript


def write_jsonl_transcript(tmp_path: Path, messages: list[dict]) -> Path:
    return write_jsonl_records(
        tmp_path,
        [
            {"type": "file-history-snapshot", "snapshot": {}},
            *({"type": message["role"], "message": message} for message in messages),
        ],
    )


def test_blocks_tool_only_turn_from_inline_messages() -> None:
    result = run_hook({"messages": tool_only_messages()})

    assert result.returncode == 2
    assert "STOP_HOOK_BLOCK" in result.stderr


def test_allows_explicit_stop_request() -> None:
    result = run_hook({"messages": tool_only_messages("이제 종료")})

    assert result.returncode == 0
    assert result.stderr == ""


def test_blocks_tool_only_turn_from_jsonl_transcript(tmp_path: Path) -> None:
    transcript = write_jsonl_transcript(tmp_path, tool_only_messages())

    result = run_hook({"transcript_path": str(transcript)})

    assert result.returncode == 2
    assert "STOP_HOOK_BLOCK" in result.stderr


def test_allows_reentrant_stop_hook() -> None:
    result = run_hook(
        {
            "messages": tool_only_messages(),
            "stop_hook_active": True,
        }
    )

    assert result.returncode == 0
    assert result.stderr == ""


def test_preserves_single_json_transcript_schema(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.json"
    transcript.write_text(
        json.dumps({"messages": tool_only_messages()}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = run_hook({"transcript_path": str(transcript)})

    assert result.returncode == 2
    assert "STOP_HOOK_BLOCK" in result.stderr


def test_malformed_jsonl_fails_open(tmp_path: Path) -> None:
    transcript = write_jsonl_transcript(tmp_path, tool_only_messages())
    with transcript.open("a", encoding="utf-8") as file:
        file.write("{malformed-json\n")

    result = run_hook({"transcript_path": str(transcript)})

    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.parametrize("ignored_flag", ["isSidechain", "isApiErrorMessage"])
def test_ignores_non_primary_tool_only_record_after_primary_text(
    tmp_path: Path,
    ignored_flag: str,
) -> None:
    primary_messages = [
        {"role": "user", "content": "계속"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "완료"}],
        },
    ]
    records = [
        *({"type": message["role"], "message": message} for message in primary_messages),
        {
            "type": "assistant",
            ignored_flag: True,
            "message": tool_only_messages()[1],
        },
    ]
    transcript = write_jsonl_records(tmp_path, records)

    result = run_hook({"transcript_path": str(transcript)})

    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.parametrize("ignored_flag", ["isSidechain", "isApiErrorMessage"])
def test_ignores_non_primary_text_record_after_primary_tool_only(
    tmp_path: Path,
    ignored_flag: str,
) -> None:
    primary_messages = tool_only_messages()
    records = [
        *({"type": message["role"], "message": message} for message in primary_messages),
        {
            "type": "assistant",
            ignored_flag: True,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "sidechain output"}],
            },
        },
    ]
    transcript = write_jsonl_records(tmp_path, records)

    result = run_hook({"transcript_path": str(transcript)})

    assert result.returncode == 2
    assert "STOP_HOOK_BLOCK" in result.stderr


def turn_with_trailing_tool_use() -> list[dict]:
    """도입 text 뒤 tool_use로 끝나는 turn — 마무리 보고가 없다."""
    return [
        {"role": "user", "content": "계속"},
        {"role": "assistant", "content": [{"type": "text", "text": "확인하겠습니다."}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1"}]},
    ]


def test_blocks_when_text_precedes_last_tool_use(tmp_path: Path) -> None:
    """도입 text만으로는 통과하지 않는다 — 마지막 tool_use 이후 text가 필요하다."""
    transcript = write_jsonl_transcript(tmp_path, turn_with_trailing_tool_use())

    result = run_hook({"transcript_path": str(transcript)})

    assert result.returncode == 2
    assert "STOP_HOOK_BLOCK" in result.stderr


def test_allows_text_after_last_tool_use(tmp_path: Path) -> None:
    messages = [
        *turn_with_trailing_tool_use(),
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]},
        {"role": "assistant", "content": [{"type": "text", "text": "결과를 보고합니다."}]},
    ]
    transcript = write_jsonl_transcript(tmp_path, messages)

    result = run_hook({"transcript_path": str(transcript)})

    assert result.returncode == 0
    assert result.stderr == ""


def test_blocks_when_only_thinking_follows_tool_use(tmp_path: Path) -> None:
    messages = [
        *turn_with_trailing_tool_use(),
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]},
    ]
    transcript = write_jsonl_transcript(tmp_path, messages)

    result = run_hook({"transcript_path": str(transcript)})

    assert result.returncode == 2
    assert "텍스트 응답이 0줄" in result.stderr


def test_blocks_silent_end_and_names_the_tool(tmp_path: Path) -> None:
    messages = [
        {"role": "user", "content": "계속"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool-1", "name": "AskUserQuestion", "input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1"}]},
    ]
    transcript = write_jsonl_transcript(tmp_path, messages)

    result = run_hook({"transcript_path": str(transcript)})

    assert result.returncode == 2
    assert "AskUserQuestion" in result.stderr
    # turn에 tool_use가 있었으므로 "action 0개"는 사실과 다르다
    assert "action 0개" not in result.stderr


def test_previous_turn_text_does_not_satisfy_current_turn(tmp_path: Path) -> None:
    """turn 경계: 이전 turn의 보고가 이번 turn의 침묵을 면제하지 않는다."""
    messages = [
        {"role": "user", "content": "첫 요청"},
        {"role": "assistant", "content": [{"type": "text", "text": "첫 턴 보고입니다."}]},
        {"role": "user", "content": "두 번째 요청"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool-2", "name": "Read", "input": {}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-2"}]},
    ]
    transcript = write_jsonl_transcript(tmp_path, messages)

    result = run_hook({"transcript_path": str(transcript)})

    assert result.returncode == 2
    assert "STOP_HOOK_BLOCK" in result.stderr


def test_settle_retry_allows_late_flushed_text(tmp_path: Path) -> None:
    """transcript는 content block 단위로 append된다 — 늦게 도착한 최종 text를 기다린다."""
    transcript = write_jsonl_transcript(tmp_path, turn_with_trailing_tool_use())
    late_record = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "늦게 도착한 보고."}]},
    }

    def append_late() -> None:
        with transcript.open("a", encoding="utf-8") as file:
            file.write(json.dumps(late_record, ensure_ascii=False) + "\n")

    timer = threading.Timer(0.2, append_late)
    timer.start()
    try:
        result = run_hook({"transcript_path": str(transcript)})
    finally:
        timer.cancel()

    assert result.returncode == 0
    assert result.stderr == ""


def test_settle_retry_survives_partially_written_record(tmp_path: Path) -> None:
    """최종 record를 쓰는 도중의 부분 JSON은 일시적 상태다 — 포기하지 않고 계속 기다린다."""
    transcript = write_jsonl_transcript(tmp_path, turn_with_trailing_tool_use())
    record = json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "최종 보고."}]},
        },
        ensure_ascii=False,
    )
    head, tail = record[:20], record[20:]

    def write_head() -> None:
        with transcript.open("a", encoding="utf-8") as file:
            file.write(head)

    def write_tail() -> None:
        with transcript.open("a", encoding="utf-8") as file:
            file.write(tail + "\n")

    timers = [threading.Timer(0.15, write_head), threading.Timer(0.35, write_tail)]
    for timer in timers:
        timer.start()
    try:
        result = run_hook({"transcript_path": str(transcript)})
    finally:
        for timer in timers:
            timer.cancel()

    assert result.returncode == 0
    assert result.stderr == ""


# --- settle() 단위 테스트 --------------------------------------------------
# 아래 테스트는 read_messages/sleep을 주입해 재시도 경로를 타이밍 없이 검증한다.
# 위쪽의 subprocess+Timer 기반 테스트는 실제 배선을 훑는 smoke 테스트이며,
# 느린 CI에서는 최초 읽기만으로 통과해 재시도 경로를 안 거칠 수 있다.

def resolved_messages() -> list[dict]:
    return [
        *turn_with_trailing_tool_use(),
        {"role": "assistant", "content": [{"type": "text", "text": "최종 보고."}]},
    ]


def test_settle_passes_when_retry_sees_final_text() -> None:
    reads = [resolved_messages()]

    verdict, _tool_id, _messages = HOOK.settle(
        reads.pop, turn_with_trailing_tool_use(), "empty", "tool-1",
        attempts=3, delay=0, sleep=NO_SLEEP,
    )

    assert verdict == "ok"
    assert reads == []


def test_settle_skips_unreadable_reads_and_keeps_trying() -> None:
    reads = [None, None, resolved_messages()]

    verdict, _tool_id, _messages = HOOK.settle(
        lambda: reads.pop(0), turn_with_trailing_tool_use(), "empty", "tool-1",
        attempts=5, delay=0, sleep=NO_SLEEP,
    )

    assert verdict == "ok"
    assert reads == []  # 부분 JSON 회차에서 포기하지 않았다


def test_settle_fails_open_when_transcript_never_readable() -> None:
    verdict, _tool_id, _messages = HOOK.settle(
        lambda: None, turn_with_trailing_tool_use(), "empty", "tool-1",
        attempts=3, delay=0, sleep=NO_SLEEP,
    )

    assert verdict == "ok"


def test_settle_keeps_blocking_when_text_never_arrives() -> None:
    blocking = turn_with_trailing_tool_use()

    verdict, _tool_id, _messages = HOOK.settle(
        lambda: blocking, blocking, "empty", "tool-1",
        attempts=3, delay=0, sleep=NO_SLEEP,
    )

    assert verdict in ("empty", "tool_only")


# Invented text only: these fixtures contain no actual session content.
ENGLISH_REPORT = (
    "The requested change is complete and all targeted checks passed. "
    "The updated implementation preserves the existing behavior and handles "
    "the additional cases covered by the regression tests. "
    "The results are ready for review and the remaining work is documented."
)
KOREAN_REPORT = (
    "요청한 변경을 구현하고 관련 회귀 테스트가 모두 통과하는 것을 확인했습니다. "
    "기존 동작을 유지하면서 새 예외 조건을 처리하며, 변경 내용과 검증 결과를 함께 보고합니다."
)


@pytest.fixture(autouse=True)
def enable_language_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_STOP_KOREAN", raising=False)


def final_messages(text: str) -> list[dict]:
    return [
        {"role": "user", "content": "결과를 알려줘"},
        {"role": "assistant", "content": [{"type": "text", "text": text}]},
    ]


@pytest.mark.parametrize("content_kind", ["text", "output_text", "string"])
def test_blocks_english_final_answer_with_korean_rewrite_request(content_kind: str) -> None:
    messages = final_messages(ENGLISH_REPORT)
    messages[-1]["content"] = (
        ENGLISH_REPORT if content_kind == "string"
        else [{"type": content_kind, "text": ENGLISH_REPORT}]
    )

    result = run_hook({"messages": messages})

    assert result.returncode == 2
    assert "STOP_HOOK_KOREAN" in result.stderr
    assert "한국어" in result.stderr
    assert "다시" in result.stderr
    assert result.stdout == ""


def test_a_few_korean_words_do_not_mask_a_mostly_english_answer() -> None:
    result = run_hook({"messages": final_messages("완료. " + ENGLISH_REPORT)})

    assert result.returncode == 2
    assert "STOP_HOOK_KOREAN" in result.stderr


TECHNICAL_PATH = "generated_component_runtime_configuration/" * 8 + "result.py:42"


@pytest.mark.parametrize("technical_text", [
    "```python\n" + ENGLISH_REPORT * 4 + "\n```",
    "~~~text\n" + ENGLISH_REPORT * 4 + "\n~~~",
    "````markdown\n```text\n" + ENGLISH_REPORT * 4 + "\n```\n````",
    "```text\n" + ENGLISH_REPORT * 4,  # an unfinished code fence is still code
    "    " + ENGLISH_REPORT * 4,
    "`" + ENGLISH_REPORT * 4 + "`",
    "``command `argument` " + ENGLISH_REPORT * 4 + "``",
    "https://example.com/" + TECHNICAL_PATH,
    "[검증 결과](https://example.com/" + TECHNICAL_PATH + ")",
    "/tmp/" + TECHNICAL_PATH,
    "./" + TECHNICAL_PATH,
    "../" + TECHNICAL_PATH,
    "~/" + TECHNICAL_PATH,
    "$HOME/" + TECHNICAL_PATH,
    "${HOME}/" + TECHNICAL_PATH,
    TECHNICAL_PATH,
    "C:\\workspace\\" + TECHNICAL_PATH.replace("/", "\\"),
    "generated_component_runtime_configuration_identifier " * 12,
    "generatedComponentRuntimeConfigurationIdentifier " * 12,
    "python -m pytest tests/test_stop_text_required.py -q\n" * 10,
    "rtk proxy python3 -m pytest tests/test_stop_text_required.py -q\n" * 10,
    "--include-generated-components --preserve-existing-settings " * 10,
    "generated-component-runtime-configuration-identifier " * 12,
    "- 코드 예시:\n\n      " + ENGLISH_REPORT * 4,
    "> ```text\n> " + ENGLISH_REPORT * 4 + "\n> ```",
    "-     " + ENGLISH_REPORT * 4,
], ids=[
    "backtick-fence", "tilde-fence", "nested-fence", "open-fence", "indented-code",
    "inline-code", "multi-backtick-inline", "url", "markdown-link", "absolute-path",
    "dot-path", "parent-path", "home-path", "home-variable", "braced-home-variable",
    "relative-path", "windows-path", "snake-identifier", "camel-identifier",
    "plain-command", "rtk-command", "cli-options", "kebab-identifier",
    "list-indented-code", "quoted-fence",
    "list-initial-code",
])
def test_technical_content_does_not_overwhelm_korean_prose(technical_text: str) -> None:
    result = run_hook({
        "messages": final_messages("검증을 마쳤습니다.\n\n" + technical_text),
    })

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize("text", [KOREAN_REPORT, "Done.", "All checks passed.", "", "```python\npass\n```"])
def test_korean_short_and_code_only_answers_are_allowed(text: str) -> None:
    result = run_hook({"messages": final_messages(text)})

    assert result.returncode == 0
    assert result.stderr == ""


def test_only_text_after_last_tool_counts_for_language() -> None:
    messages = [
        *turn_with_trailing_tool_use(),
        {"role": "assistant", "content": [{"type": "text", "text": ENGLISH_REPORT}]},
    ]
    messages[1]["content"][0]["text"] = KOREAN_REPORT * 10

    result = run_hook({"messages": messages})

    assert result.returncode == 2
    assert "STOP_HOOK_KOREAN" in result.stderr


def test_previous_turn_and_thinking_do_not_mask_current_english() -> None:
    messages = [
        *final_messages(KOREAN_REPORT * 10),
        {"role": "user", "content": "다음 결과를 알려줘"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": KOREAN_REPORT * 10}]},
        {"role": "assistant", "content": [{"type": "text", "text": ENGLISH_REPORT}]},
    ]

    result = run_hook({"messages": messages})

    assert result.returncode == 2
    assert "STOP_HOOK_KOREAN" in result.stderr


def test_korean_inside_code_does_not_mask_english_prose() -> None:
    result = run_hook({
        "messages": final_messages(ENGLISH_REPORT + "\n\n```text\n" + KOREAN_REPORT * 10 + "\n```"),
    })

    assert result.returncode == 2
    assert "STOP_HOOK_KOREAN" in result.stderr


@pytest.mark.parametrize("prefix, indentation", [
    ("- Result:", "    "),
    ("1. Result:", "    "),
    ("- Result:\n  - Details:", "      "),
    ("- Result:", "        "),  # indented code cannot interrupt a paragraph
    ("Result:", "    "),
])
def test_indented_list_prose_is_not_treated_as_code(prefix: str, indentation: str) -> None:
    result = run_hook({"messages": final_messages(prefix + "\n" + indentation + ENGLISH_REPORT)})

    assert result.returncode == 2
    assert "STOP_HOOK_KOREAN" in result.stderr


def test_unclosed_list_fence_ends_when_the_list_ends() -> None:
    result = run_hook({
        "messages": final_messages("- ```text\n  " + KOREAN_REPORT * 10 + "\n\n" + ENGLISH_REPORT),
    })

    assert result.returncode == 2
    assert "STOP_HOOK_KOREAN" in result.stderr


@pytest.mark.parametrize("payload_extra", [{"stop_hook_active": True}, {"hook_event_name": "SubagentStop"}])
def test_language_check_skips_reentrant_and_subagent_stops(payload_extra: dict) -> None:
    result = run_hook({"messages": final_messages(ENGLISH_REPORT), **payload_extra})

    assert result.returncode == 0
    assert result.stderr == ""


def test_language_opt_out_allows_english(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_STOP_KOREAN", "0")

    result = run_hook({"messages": final_messages(ENGLISH_REPORT)})

    assert result.returncode == 0
    assert result.stderr == ""


def test_language_opt_out_keeps_empty_response_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_STOP_KOREAN", "0")

    result = run_hook({"messages": tool_only_messages()})

    assert result.returncode == 2
    assert "STOP_HOOK_BLOCK" in result.stderr
    assert "STOP_HOOK_KOREAN" not in result.stderr


def test_authoritative_english_final_text_blocks_before_transcript_flush() -> None:
    result = run_hook({"last_assistant_message": ENGLISH_REPORT})

    assert result.returncode == 2
    assert "STOP_HOOK_KOREAN" in result.stderr


def test_authoritative_final_text_overrides_stale_transcript_text() -> None:
    result = run_hook({
        "messages": final_messages(ENGLISH_REPORT),
        "last_assistant_message": KOREAN_REPORT,
    })

    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.parametrize("initial_text", ["", "The work is complete."])
@pytest.mark.parametrize("late_text, expected", [(ENGLISH_REPORT, 2), ("Done.", 0)])
def test_file_language_check_waits_even_when_first_read_is_short(
    monkeypatch: pytest.MonkeyPatch, initial_text: str, late_text: str, expected: int,
) -> None:
    reads = [final_messages(initial_text), final_messages(initial_text)]
    reads.extend([final_messages(late_text)] * 7)
    monkeypatch.setattr(HOOK, "read_transcript_messages", lambda _payload: reads.pop(0))
    monkeypatch.setattr(HOOK.sys, "stdin", io.StringIO(json.dumps({"transcript_path": "unused.jsonl"})))

    result = HOOK.main()

    assert result == expected
    assert reads == []


def test_settle_can_replace_english_with_late_korean_text() -> None:
    english = final_messages(ENGLISH_REPORT)
    korean = final_messages(KOREAN_REPORT)
    reads = [english, None, korean]

    verdict, _tool_id, messages = HOOK.settle(
        lambda: reads.pop(0), english, "non_korean", "",
        attempts=3, delay=0, sleep=NO_SLEEP,
    )

    assert verdict == "ok"
    assert messages == korean
    assert reads == []


def test_settle_keeps_blocking_a_persistently_english_answer() -> None:
    english = final_messages(ENGLISH_REPORT)

    verdict, _tool_id, _messages = HOOK.settle(
        lambda: english, english, "non_korean", "",
        attempts=3, delay=0, sleep=NO_SLEEP,
    )

    assert verdict == "non_korean"


def test_language_opt_out_is_preserved_after_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_STOP_KOREAN", "0")
    reads = [tool_only_messages(), final_messages(ENGLISH_REPORT)]
    monkeypatch.setattr(HOOK, "read_transcript_messages", lambda _payload: reads.pop(0))
    monkeypatch.setattr(HOOK.sys, "stdin", io.StringIO(json.dumps({"transcript_path": "unused.jsonl"})))

    assert HOOK.main() == 0
    assert reads == []
