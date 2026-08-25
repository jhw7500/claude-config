"""install.sh 회귀 테스트 — 임시 HOME 에서만 돈다. 실제 사용자 HOME 을 건드리지 않는다."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).parents[1]
INSTALL = REPO / "install.sh"


def run_install(home: Path, umask: str = "022") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, HOME=str(home))
    # permissive umask 에서도 결과 mode 가 결정적이어야 한다
    return subprocess.run(
        ["bash", "-c", f'umask {umask}; exec bash "{INSTALL}"'],
        env=env, text=True, capture_output=True, check=False,
    )


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak.*"))


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_installs_with_deterministic_modes(home: Path) -> None:
    result = run_install(home)

    assert result.returncode == 0, result.stderr
    claude = home / ".claude"
    assert mode_of(claude) == 0o700
    assert mode_of(claude / "CLAUDE.md") == 0o600
    guidance = claude / "global-guidance.md"
    assert mode_of(guidance) == 0o644
    assert not guidance.stat().st_mode & stat.S_IXUSR      # 실행 비트 없음


def test_identical_rerun_adds_no_backup(home: Path) -> None:
    run_install(home)
    claude_md = home / ".claude" / "CLAUDE.md"
    before = len(backups(claude_md))

    run_install(home)

    assert len(backups(claude_md)) == before


def test_settings_change_makes_exactly_one_private_backup(home: Path) -> None:
    run_install(home)
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
    settings.chmod(0o744)                                   # 이슈 #50 이 보고한 실제 조건

    run_install(home)                                       # 배선 추가 → 변경 발생

    saved = backups(settings)
    assert len(saved) == 1
    assert mode_of(saved[0]) == 0o600
    assert mode_of(settings) == 0o600

    run_install(home)                                       # 이제 변경 없음
    assert len(backups(settings)) == 1


def test_hook_wiring_stays_idempotent(home: Path) -> None:
    run_install(home)
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
    run_install(home)

    def entry_count() -> int:
        data = json.loads(settings.read_text(encoding="utf-8"))
        return sum(len(g.get("hooks", [])) for gs in data["hooks"].values() for g in gs)

    first = entry_count()
    run_install(home)

    assert entry_count() == first
    assert first > 0


def test_links_are_still_created(home: Path) -> None:
    run_install(home)
    claude = home / ".claude"

    assert (claude / "hooks" / "task-nudge.sh").is_symlink()
    assert (claude / "scripts" / "hook-selfcheck.py").is_symlink()
    assert (claude / "skills" / "task-observer").is_symlink()
