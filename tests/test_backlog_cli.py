from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "scripts/backlog-checkpoint.py"


def _run(
    *args: str,
    input_value: object | None = None,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    timeout: float | None = None,
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
        timeout=timeout,
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


def test_file_input_accepts_exactly_one_mib_and_rejects_one_byte_more(
    tmp_path: Path,
) -> None:
    # Wrong branch: reading the complete file before enforcing the one-MiB boundary.
    prefix = b'{"issues":[],"dependencies":[]}'
    exact = tmp_path / "exact.json"
    exact.write_bytes(prefix + b" " * (1_048_576 - len(prefix)))
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(prefix + b" " * (1_048_577 - len(prefix)))

    accepted = _run("queue", "plan", "--input", str(exact))
    rejected = _run("queue", "plan", "--input", str(oversized))

    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["result"]["status"] == "COMPLETE"
    assert rejected.returncode == 1
    assert json.loads(rejected.stderr)["error"] == "input JSON exceeds 1 MiB"


def test_file_input_rejects_fifo_without_waiting_for_a_writer(tmp_path: Path) -> None:
    # Wrong branch: blocking in Path.read_bytes while opening a FIFO input.
    fifo = tmp_path / "payload.fifo"
    os.mkfifo(fifo, 0o600)

    try:
        result = _run(
            "queue", "plan", "--input", str(fifo), cwd=tmp_path, timeout=5
        )
    except subprocess.TimeoutExpired:
        pytest.fail("owned CLI child blocked while opening FIFO input")

    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == "input JSON must be a regular file"


@pytest.mark.parametrize("kind", ["symlink", "device"])
def test_file_input_rejects_nonregular_or_symlink_input(
    tmp_path: Path, kind: str
) -> None:
    # Wrong branch: following a symlink or reading a non-regular file as JSON input.
    regular = tmp_path / "regular.json"
    regular.write_text('{"issues":[],"dependencies":[]}', encoding="utf-8")
    supplied = tmp_path / "input.json"
    if kind == "symlink":
        supplied.symlink_to(regular)
    else:
        supplied = Path("/dev/null")

    result = _run("queue", "plan", "--input", str(supplied), cwd=tmp_path)

    assert result.returncode == 1
    error = json.loads(result.stderr)["error"]
    if kind == "symlink":
        assert error == "input JSON is unavailable"
    else:
        assert error == "input JSON must be a regular file"


def test_sparse_file_input_is_bounded_under_child_address_space_limit(
    tmp_path: Path,
) -> None:
    # Wrong branch: allocating storage proportional to a sparse file before size rejection.
    sparse = tmp_path / "sparse.json"
    with sparse.open("wb") as stream:
        stream.truncate(1024 * 1024 * 1024)
    env = {key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"}

    def cap_address_space() -> None:
        resource.setrlimit(
            resource.RLIMIT_AS,
            (128 * 1024 * 1024, 128 * 1024 * 1024),
        )

    result = subprocess.run(
        [sys.executable, str(ENTRY), "queue", "plan", "--input", str(sparse)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO,
        env=env,
        check=False,
        timeout=5,
        preexec_fn=cap_address_space,
    )

    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == "input JSON exceeds 1 MiB"


def test_cli_requires_explicit_grouped_argv() -> None:
    result = _run("snapshot")

    assert result.returncode == 2
    assert result.stdout == ""


@pytest.mark.parametrize(("field", "value"), [("change", []), ("mode", {})])
def test_git_bind_malformed_change_or_mode_returns_bounded_json_error(
    tmp_path: Path, field: str, value: object
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=repo,
        check=True,
    )
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True)
    parent = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "base.txt").write_text("changed\n", encoding="utf-8")
    collected = _run(
        "git", "collect", "--root", str(repo), "--input", "-", input_value={"paths": ["base.txt"]}
    )
    manifest = json.loads(collected.stdout)["result"]
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "change"], cwd=repo, check=True)
    commit_oid = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    manifest["sources"][0][field] = value

    result = _run(
        "git",
        "bind",
        "--root",
        str(repo),
        "--parent-head",
        parent,
        "--commit-oid",
        commit_oid,
        "--input",
        "-",
        input_value=manifest,
    )

    assert result.returncode == 1
    error = json.loads(result.stderr)
    assert error["ok"] is False
    assert error["status"] == "ERROR"
    assert field in error["error"]
    assert "Traceback" not in result.stderr
