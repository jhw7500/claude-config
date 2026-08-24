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


def test_marker_takes_precedence_over_heartbeat(env) -> None:
    hook = env.add_hook("demo-hook.py", MARKED)
    settings = env.wire([("Stop", "*", f"python3 {hook}")])
    env.transcript([{"type": "attachment", "attachment": {"stdout": "[DEMO-MARKER] hi"},
                     "timestamp": "2026-08-24T01:00:00Z"}])
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
        {"type": "attachment", "attachment": {"stdout": "🕐 prompt @ 2026-08-24 17:00:00"},
         "timestamp": "2026-08-24T08:00:00Z"},
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
