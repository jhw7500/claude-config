import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "stop-text-required.py"


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
