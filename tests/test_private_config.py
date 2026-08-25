"""scripts/lib/private_config.py — 사설 설정 원자적 갱신·백업 계약."""
import importlib.util
import os
import stat
from pathlib import Path

import pytest


LIB = Path(__file__).parents[1] / "scripts" / "lib" / "private_config.py"


def load():
    spec = importlib.util.spec_from_file_location("private_config", LIB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PC = load()


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak.*"))


def test_creates_file_with_private_mode(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"

    assert PC.write_private(str(target), "새 내용\n") == "created"
    assert target.read_text(encoding="utf-8") == "새 내용\n"
    assert mode_of(target) == 0o600
    assert backups(target) == []


def test_identical_rerun_makes_no_backup(tmp_path: Path) -> None:
    """동일 내용 재실행이 백업을 쌓으면 백업 디렉터리가 사본으로 채워진다."""
    target = tmp_path / "CLAUDE.md"
    PC.write_private(str(target), "같은 내용\n")

    assert PC.write_private(str(target), "같은 내용\n") == "unchanged"
    assert backups(target) == []


def test_identical_rerun_still_enforces_mode(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    PC.write_private(str(target), "같은 내용\n")
    target.chmod(0o644)                                  # 외부에서 느슨해진 상황

    assert PC.write_private(str(target), "같은 내용\n") == "unchanged"
    assert mode_of(target) == 0o600


def test_change_makes_exactly_one_private_backup(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    PC.write_private(str(target), "이전\n")

    assert PC.write_private(str(target), "이후\n") == "updated"

    saved = backups(target)
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == "이전\n"
    assert mode_of(saved[0]) == 0o600
    assert target.read_text(encoding="utf-8") == "이후\n"


def test_replace_failure_preserves_existing_file(tmp_path: Path, monkeypatch) -> None:
    """교체 직전에 실패해도 기존 파일은 유효한 상태로 남아야 한다."""
    target = tmp_path / "settings.json"
    PC.write_private(str(target), "원본 유지\n")
    before = backups(target)

    real_replace = os.replace

    def explode(src, dst):
        if str(dst) == str(target):
            raise OSError("주입된 실패")
        return real_replace(src, dst)

    monkeypatch.setattr(PC.os, "replace", explode)

    with pytest.raises(OSError):
        PC.write_private(str(target), "쓰다 만 내용\n")

    assert target.read_text(encoding="utf-8") == "원본 유지\n"
    assert mode_of(target) == 0o600
    # 임시 파일이 남지 않는다
    assert list(tmp_path.glob(".tmp-*")) == []
    # 백업은 교체 실패 전에 이미 만들어졌을 수 있으나, 원본은 그대로다
    assert len(backups(target)) >= len(before)
