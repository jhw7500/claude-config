from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "scripts/backlog-checkpoint.py"


def _run(
    *args: str,
    input_value: object | None = None,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
):
    env = {key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(ENTRY), *args],
        input=None if input_value is None else json.dumps(input_value),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd or REPO,
        env=env,
        check=False,
    )


def test_queue_plan_reads_repository_from_stdin_and_emits_collected_json(
    tmp_path: Path,
) -> None:
    gh = tmp_path / "gh"
    gh.write_text(
        """#!/usr/bin/python3
import json, sys
url = sys.argv[-1]
if url.endswith('/issues?state=open&per_page=100'):
    body = [
        {'number': 1, 'html_url': 'https://github.com/o/r/issues/1', 'state': 'open'},
        {'number': 2, 'html_url': 'https://github.com/o/r/issues/2', 'state': 'open'},
    ]
elif url.endswith('/issues/2/dependencies/blocked_by?per_page=100'):
    body = [{'number': 1, 'html_url': 'https://github.com/o/r/issues/1', 'state': 'open'}]
else:
    body = []
print('HTTP/2.0 200 OK\\n\\n' + json.dumps(body))
""",
        encoding="utf-8",
    )
    gh.chmod(0o700)

    result = _run(
        "queue",
        "plan",
        "--repo",
        "o/r",
        "--explicit-edges",
        "-",
        input_value=[],
        env_extra={"PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {
            "status": "COMPLETE",
            "queue": [
                "https://github.com/o/r/issues/1",
                "https://github.com/o/r/issues/2",
            ],
            "blocked": [],
            "reasons": [],
        },
    }
    assert result.stderr == ""


def test_git_inspect_uses_json_file_and_rejects_next_action_execution(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps({"issues": [], "dependencies": [], "next_action": "touch owned"}),
        encoding="utf-8",
    )
    result = _run("queue", "plan", "--input", str(payload), cwd=tmp_path)

    assert result.returncode == 1
    error = json.loads(result.stderr)
    assert error["ok"] is False
    assert error["status"] == "ERROR"
    assert not (tmp_path / "owned").exists()


def test_queue_plan_pure_graph_path_does_not_require_gh() -> None:
    result = _run(
        "queue",
        "plan",
        "--input",
        "-",
        input_value={"issues": [], "dependencies": []},
        env_extra={"PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"] == {
        "status": "COMPLETE",
        "queue": [],
        "blocked": [],
        "reasons": [],
    }


def test_missing_exact_snapshot_path_is_bounded_error(tmp_path: Path) -> None:
    result = _run("snapshot", "read", "--path", str(tmp_path / "missing.md"))

    assert result.returncode == 1
    error = json.loads(result.stderr)
    assert error["ok"] is False
    assert error["status"] == "ERROR"
    assert "Traceback" not in result.stderr


def test_cli_requires_explicit_grouped_argv() -> None:
    result = _run("snapshot")

    assert result.returncode == 2
    assert result.stdout == ""
