import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))


def make_repo(path: Path, origin: str | None = "https://github.com/jhw7500/demo.git") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    if origin:
        run("remote", "add", "origin", origin)
    (path / "f.txt").write_text("x\n")
    run("add", "f.txt")
    run("commit", "-q", "-m", "init")
    return path


@pytest.fixture
def repo_factory():
    return make_repo


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"
