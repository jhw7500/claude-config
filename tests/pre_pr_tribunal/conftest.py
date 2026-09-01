from pathlib import Path
import importlib.util
import os
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "hooks"
INSTALLER_PATH = REPO / "scripts" / "install-pre-pr-tribunal.py"
_COLLIDING_TEST_MODULES = {"test_installer", "test_install_integration"}
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))


def pytest_collectreport(report):
    """Release planned duplicate basenames after their tribunal collection."""
    if report.failed or not report.nodeid.startswith("tests/pre_pr_tribunal/"):
        return
    module_name = Path(report.nodeid).stem
    if module_name in _COLLIDING_TEST_MODULES:
        sys.modules.pop(module_name, None)


@pytest.fixture(scope="session")
def installer():
    spec = importlib.util.spec_from_file_location(
        "install_pre_pr_tribunal", INSTALLER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path):
    value = tmp_path / "home"
    value.mkdir(mode=0o700)
    return value


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
