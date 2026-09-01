from pathlib import Path
import importlib.util
import os
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(
        os.environ,
        LC_ALL="C",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "init", "-q", "-b", "feature"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        env=env,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repo),
            "config",
            "user.email",
            "test@example.com",
        ],
        check=True,
        env=env,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://github.com/jhw7500/claude-config.git",
        ],
        check=True,
        env=env,
    )
    (repo / ".gitignore").write_text(".review/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "add", "."], check=True, env=env
    )
    subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "commit", "-qm", "base"],
        check=True,
        env=env,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repo),
            "update-ref",
            "refs/remotes/origin/master",
            "HEAD",
        ],
        check=True,
        env=env,
    )
    (repo / "tracked.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "commit", "-qam", "feature"],
        check=True,
        env=env,
    )
    return repo
