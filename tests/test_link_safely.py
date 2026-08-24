"""scripts/lib/link-safely.sh — 심링크 배포 헬퍼.

ln -sfn 은 목적지가 실디렉터리일 때 실패하지 않고 그 안에 링크를 만든다.
배포는 조용히 실패하고, 훅은 안 도는 것과 정상인 것이 겉으로 같아 오래 숨는다.
"""
import subprocess
from pathlib import Path

import pytest


LIB = Path(__file__).parents[1] / "scripts" / "lib" / "link-safely.sh"


def run_link(src: Path, dest: Path) -> subprocess.CompletedProcess[str]:
    script = f'. "{LIB}"\nlink_safely "{src}" "{dest}"\n'
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)


@pytest.fixture
def src(tmp_path: Path) -> Path:
    path = tmp_path / "source.sh"
    path.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    return path


def test_creates_symlink_when_dest_absent(tmp_path: Path, src: Path) -> None:
    dest = tmp_path / "dest.sh"

    result = run_link(src, dest)

    assert result.returncode == 0
    assert dest.is_symlink()
    assert dest.resolve() == src.resolve()


def test_replaces_real_file_and_keeps_backup(tmp_path: Path, src: Path) -> None:
    dest = tmp_path / "dest.sh"
    dest.write_text("기존 실체 파일\n", encoding="utf-8")

    result = run_link(src, dest)

    assert result.returncode == 0
    assert dest.is_symlink()
    backups = list(tmp_path.glob("dest.sh.replaced.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "기존 실체 파일\n"


def test_directory_dest_does_not_become_nested_link(tmp_path: Path, src: Path) -> None:
    """ln -sfn 단독이면 dest/source.sh 를 만들어 배포가 조용히 실패한다."""
    dest = tmp_path / "dest.sh"
    dest.mkdir()
    (dest / "keep.txt").write_text("x", encoding="utf-8")

    result = run_link(src, dest)

    assert result.returncode == 0
    assert dest.is_symlink()
    assert not (dest / src.name).exists()          # 중첩 링크가 생기지 않았다
    backups = list(tmp_path.glob("dest.sh.replaced.*"))
    assert len(backups) == 1
    assert (backups[0] / "keep.txt").is_file()     # 내용은 보존됐다


def test_existing_symlink_replaced_without_backup(tmp_path: Path, src: Path) -> None:
    other = tmp_path / "other.sh"
    other.write_text("old target\n", encoding="utf-8")
    dest = tmp_path / "dest.sh"
    dest.symlink_to(other)

    result = run_link(src, dest)

    assert result.returncode == 0
    assert dest.resolve() == src.resolve()
    assert list(tmp_path.glob("dest.sh.replaced.*")) == []


def test_skips_and_reports_when_backup_impossible(tmp_path: Path, src: Path) -> None:
    """치우지 못하면 조용히 덮지 않고 건너뛴다."""
    parent = tmp_path / "locked"
    parent.mkdir()
    dest = parent / "dest.sh"
    dest.write_text("건드리면 안 되는 파일\n", encoding="utf-8")
    parent.chmod(0o500)                            # 쓰기 금지 → mv 실패
    try:
        result = run_link(src, dest)
    finally:
        parent.chmod(0o700)

    assert result.returncode == 1
    assert "건너뛴다" in result.stderr
    assert not dest.is_symlink()
    assert dest.read_text(encoding="utf-8") == "건드리면 안 되는 파일\n"
