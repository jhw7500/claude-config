from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
CORE_PATH = REPO / "hooks" / "task_nudge.py"
INSTALLER_PATH = REPO / "scripts" / "install-task-nudge.py"


@pytest.fixture(scope="session")
def core():
    spec = importlib.util.spec_from_file_location("task_nudge", CORE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def installer():
    spec = importlib.util.spec_from_file_location("install_task_nudge", INSTALLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir(mode=0o700)
    (path / "scratch").mkdir(mode=0o700)
    (path / "runtime").mkdir(mode=0o700)
    return path


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["/usr/bin/git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(path), "remote", "add", "origin", "https://github.com/jhw7500/claude-config.git"],
        check=True,
    )
    return path


def install_fake_launcher(home, payload, exit_code=0, *, stdout_prefix="", stderr=""):
    launcher = home / ".local" / "bin" / "jhw-control-host"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        "#!/usr/bin/python3\n"
        "import json, sys\n"
        f"payload = {payload!r}\n"
        f"sys.stdout.write({stdout_prefix!r})\n"
        "sys.stdout.write(json.dumps(payload))\n"
        f"sys.stderr.write({stderr!r})\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    launcher.chmod(0o500)
    return launcher


@pytest.fixture
def install_launcher():
    return install_fake_launcher


@pytest.fixture
def registered_home(home):
    payload = {
        "command": "portfolio status",
        "result": {
            "page_id": "page-1",
            "items": [{"project_id": "project-1", "title": "Project", "repo_ids": ["repo-1"]}],
            "repositories": [{"repo_id": "repo-1", "slug": "jhw7500/claude-config", "allow_public": False}],
            "truncated": False,
            "total_items": 1,
        },
    }
    install_fake_launcher(home, payload)
    return home


@pytest.fixture
def unregistered_home(home):
    install_fake_launcher(
        home,
        {
            "command": "portfolio status",
            "result": {"page_id": "page-1", "items": [], "repositories": [], "truncated": False, "total_items": 0},
        },
    )
    return home


@pytest.fixture
def run_adapter():
    def invoke(name, payload, home):
        env = dict(os.environ, HOME=str(home), XDG_RUNTIME_DIR=str(home / "runtime"), TMPDIR=str(home / "scratch"))
        return subprocess.run(
            [sys.executable, str(REPO / "hooks" / name)],
            input=payload if isinstance(payload, str) else json.dumps(payload), text=True, capture_output=True, check=False, env=env,
        )
    return invoke


@pytest.fixture
def run_manual():
    def invoke(repo, home):
        env = dict(os.environ, HOME=str(home), TMPDIR=str(home / "scratch"))
        return subprocess.run(
            [sys.executable, str(REPO / "hooks" / "task-nudge-codex.py"), "--manual-check", "--cwd", str(repo)],
            text=True, capture_output=True, check=False, env=env,
        )
    return invoke
