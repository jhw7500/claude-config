import json
import subprocess
import sys
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
