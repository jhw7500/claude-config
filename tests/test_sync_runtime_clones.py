"""The runtime-clone sync must fast-forward, and must refuse to guess.

A runtime checkout is what ~/.claude symlinks into, so a wrong move here goes
live immediately. These pin the two halves that matter: it fast-forwards a clean
clone, and it reports rather than rewrites anything else.
"""

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "sync-runtime-clones.sh"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def make_pair(tmp_path: Path) -> tuple[Path, Path]:
    """An origin with one commit, plus a clone tracking its default branch."""
    origin = tmp_path / "origin"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git(work, "config", "user.email", "t@example.invalid")
    git(work, "config", "user.name", "t")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "base")
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(origin)], check=True)

    runtime = tmp_path / "runtime"
    subprocess.run(["git", "clone", "-q", str(origin), str(runtime)], check=True)
    git(runtime, "config", "user.email", "t@example.invalid")
    git(runtime, "config", "user.name", "t")
    git(work, "remote", "add", "origin", str(origin))
    return work, runtime


def push_change(work: Path, rel: str, body: str = "x\n") -> None:
    target = work / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", f"add {rel}")
    git(work, "push", "-q", "origin", "main")


def run(runtime: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["RUNTIME_SYNC_PAIRS"] = str(runtime)
    env["RUNTIME_SYNC_LOG"] = str(tmp_path / "sync.log")
    env["RUNTIME_SYNC_MARKER"] = str(tmp_path / "action-required")
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, check=False, env=env,
    )


def test_fast_forwards_a_clean_clone(tmp_path: Path) -> None:
    work, runtime = make_pair(tmp_path)
    before = git(runtime, "rev-parse", "HEAD").strip()
    push_change(work, "docs/note.md")

    result = run(runtime, tmp_path)

    assert "[sync]" in result.stdout, result.stdout
    assert git(runtime, "rev-parse", "HEAD").strip() != before
    assert result.returncode == 0, result.stdout


def test_reports_when_hook_wiring_went_stale(tmp_path: Path) -> None:
    work, runtime = make_pair(tmp_path)
    push_change(work, "hooks/new-hook.py")

    result = run(runtime, tmp_path)

    assert "[ACTION]" in result.stdout and "install.sh" in result.stdout, result.stdout
    assert result.returncode == 1


def test_reports_when_the_mcp_build_went_stale(tmp_path: Path) -> None:
    work, runtime = make_pair(tmp_path)
    push_change(work, "mcp-server/src/index.ts")

    result = run(runtime, tmp_path)

    assert "npm run build" in result.stdout, result.stdout
    assert result.returncode == 1


def test_marker_names_the_command_to_run(tmp_path: Path) -> None:
    work, runtime = make_pair(tmp_path)
    push_change(work, "hooks/new-hook.py")

    run(runtime, tmp_path)

    marker = (tmp_path / "action-required").read_text(encoding="utf-8")
    assert f"bash {runtime}/install.sh" in marker, marker


def test_marker_is_emptied_once_nothing_is_pending(tmp_path: Path) -> None:
    """A stale marker would keep asking for work that is already done."""
    work, runtime = make_pair(tmp_path)
    push_change(work, "hooks/new-hook.py")
    run(runtime, tmp_path)
    assert (tmp_path / "action-required").read_text(encoding="utf-8").strip()

    run(runtime, tmp_path)

    assert (tmp_path / "action-required").read_text(encoding="utf-8").strip() == ""


def test_leaves_a_dirty_clone_alone(tmp_path: Path) -> None:
    work, runtime = make_pair(tmp_path)
    push_change(work, "docs/note.md")
    (runtime / "README.md").write_text("locally edited\n", encoding="utf-8")
    before = git(runtime, "rev-parse", "HEAD").strip()

    result = run(runtime, tmp_path)

    assert "[skip]" in result.stdout and "modified" in result.stdout, result.stdout
    assert git(runtime, "rev-parse", "HEAD").strip() == before
    assert (runtime / "README.md").read_text(encoding="utf-8") == "locally edited\n"


def test_leaves_a_clone_with_unmerged_commits_alone(tmp_path: Path) -> None:
    work, runtime = make_pair(tmp_path)
    (runtime / "local.md").write_text("local\n", encoding="utf-8")
    git(runtime, "add", "-A")
    git(runtime, "commit", "-qm", "local only")
    before = git(runtime, "rev-parse", "HEAD").strip()

    result = run(runtime, tmp_path)

    assert "ahead" in result.stdout, result.stdout
    assert git(runtime, "rev-parse", "HEAD").strip() == before


def test_says_nothing_to_do_when_already_current(tmp_path: Path) -> None:
    _, runtime = make_pair(tmp_path)

    result = run(runtime, tmp_path)

    assert "[ok]" in result.stdout and "already at" in result.stdout, result.stdout
    assert result.returncode == 0
