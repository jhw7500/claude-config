import importlib.util
import json
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

    def run(settings: Path, days: int = 7):
        return HS.audit(settings, repo, transcripts, days)

    ns = type("Env", (), {})()
    ns.repo, ns.transcripts, ns.tmp = repo, transcripts, tmp_path
    ns.add_hook, ns.wire, ns.transcript, ns.run = add_hook, wire, transcript, run
    return ns


def status_of(result: dict, basename: str) -> str:
    for row in result["rows"]:
        if row["script"] and row["script"].endswith(basename):
            return row["status"]
    raise AssertionError(f"{basename} not in result")


MARKED = 'print("<system-reminder>[DEMO-MARKER] hi</system-reminder>")\n'


def test_silent_when_marker_never_appears(env) -> None:
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([{"type": "user", "message": {"role": "user", "content": "안녕"}}])

    assert status_of(env.run(settings), "demo-hook.py") == "SILENT"


def test_counts_marker_in_attachment_stdout(env) -> None:
    """훅 출력은 attachment.stdout 에 저장된다 — 이 경로를 놓치면 전부 SILENT 로 오진한다."""
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([
        {"type": "attachment", "attachment": {"stdout": "[DEMO-MARKER] hi"},
         "timestamp": "2026-08-24T01:00:00Z"},
    ])

    result = env.run(settings)
    assert status_of(result, "demo-hook.py") == "ok"
    row = next(r for r in result["rows"] if r["script"].endswith("demo-hook.py"))
    assert row["fired"] == 1
    assert row["last_seen"] == "2026-08-24"


def test_counts_marker_in_nested_content_block(env) -> None:
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("UserPromptSubmit", "*", f"python3 {hook}")])
    env.transcript([
        {"type": "user", "timestamp": "2026-08-24T02:00:00Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "content": "[DEMO-MARKER] hi"}]}},
    ])

    assert status_of(env.run(settings), "demo-hook.py") == "ok"


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
