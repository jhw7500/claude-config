from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "scripts/install-backlog-drain.py"


def _install(home: Path, repo: Path = REPO):
    env = {key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"}
    return subprocess.run(
        [sys.executable, str(INSTALLER), "--repo", str(repo), "--home", str(home)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )


def _targets(home: Path) -> tuple[Path, ...]:
    return (
        home / ".claude/skills/backlog-drain",
        home / ".codex/skills/backlog-drain",
        home / ".claude/scripts/backlog-checkpoint.py",
        home / ".codex/scripts/backlog-checkpoint.py",
    )


def test_temp_home_install_discovers_both_skills_and_runs_helper_from_foreign_cwd(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    first = _install(home)
    second = _install(home)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    skill_source = REPO / "skills/backlog-drain"
    helper_source = REPO / "scripts/backlog-checkpoint.py"
    for target in _targets(home)[:2]:
        assert target.is_symlink() and target.resolve() == skill_source
        assert (target / "SKILL.md").is_file()
    for target in _targets(home)[2:]:
        assert target.is_symlink() and target.resolve() == helper_source

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    invoked = subprocess.run(
        [
            sys.executable,
            str(home / ".codex/scripts/backlog-checkpoint.py"),
            "queue",
            "plan",
            "--input",
            "-",
        ],
        input=json.dumps({"issues": [], "dependencies": []}),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=foreign,
        check=False,
    )
    assert invoked.returncode == 0, invoked.stderr
    assert json.loads(invoked.stdout)["result"]["status"] == "COMPLETE"


def test_foreign_file_or_symlink_conflict_preserves_every_target(
    tmp_path: Path,
) -> None:
    for kind in ("file", "symlink"):
        home = tmp_path / kind
        home.mkdir(mode=0o700)
        conflict = home / ".codex/skills/backlog-drain"
        conflict.parent.mkdir(parents=True)
        if kind == "file":
            conflict.write_text("keep\n", encoding="utf-8")
        else:
            foreign = tmp_path / "foreign-skill"
            foreign.mkdir(exist_ok=True)
            conflict.symlink_to(foreign, target_is_directory=True)
        before = (
            os.readlink(conflict) if conflict.is_symlink() else conflict.read_bytes()
        )

        result = _install(home)

        assert result.returncode == 1
        after = (
            os.readlink(conflict) if conflict.is_symlink() else conflict.read_bytes()
        )
        assert after == before
        assert all(
            not os.path.lexists(path) for path in _targets(home) if path != conflict
        )


def test_launcher_only_does_not_install_backlog_assets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    env = dict(os.environ, HOME=str(home))

    result = subprocess.run(
        ["/bin/bash", str(REPO / "install.sh"), "--launcher-only"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert all(not os.path.lexists(path) for path in _targets(home))
