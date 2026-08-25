"""scripts/lib/link-safely.sh — 심링크 배포 헬퍼.

ln -sfn 은 목적지가 실디렉터리일 때 실패하지 않고 그 안에 링크를 만든다.
배포는 조용히 실패하고, 훅은 안 도는 것과 정상인 것이 겉으로 같아 오래 숨는다.
"""
import os
import subprocess
from pathlib import Path

import pytest


LIB = Path(__file__).parents[1] / "scripts" / "lib" / "link-safely.sh"


def run_link(src: Path, dest: Path, archive: Path | None = None) -> subprocess.CompletedProcess[str]:
    args = f'"{src}" "{dest}"' + (f' "{archive}"' if archive else "")
    script = f'. "{LIB}"\nlink_safely {args}\n'
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


@pytest.mark.skipif(os.getuid() == 0, reason="root 는 chmod 권한 차단을 무시한다")
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


INSTALL = Path(__file__).parents[1] / "install.sh"


def test_install_never_ends_an_and_list_with_link_safely() -> None:
    """set -e 아래에서 link_safely 가 && 목록의 마지막이면 실패 시 설치가 중단된다.

    루프의 `link_safely ... && COUNTER=...` 처럼 앞쪽에 오는 것은 안전하다.
    뒤쪽에 오는 형태만 문제이므로 그 패턴이 남아 있지 않은지 확인한다.
    """
    offenders = [
        (num, line.rstrip())
        for num, line in enumerate(INSTALL.read_text(encoding="utf-8").splitlines(), 1)
        if "&& link_safely" in line and "|| true" not in line
    ]

    assert offenders == [], f"set -e 중단 위험: {offenders}"


def test_install_aborts_without_the_guard(tmp_path: Path) -> None:
    """가드가 없으면 실제로 중단된다는 것을 최소 재현으로 고정한다."""
    unguarded = tmp_path / "unguarded.sh"
    unguarded.write_text(
        f'set -e\n. "{LIB}"\n'
        f'[ -f "{LIB}" ] && link_safely /nonexistent/src /proc/1/cannot-write\n'
        'echo REACHED\n',
        encoding="utf-8",
    )
    guarded = tmp_path / "guarded.sh"
    guarded.write_text(
        f'set -e\n. "{LIB}"\n'
        f'if [ -f "{LIB}" ]; then\n'
        '  link_safely /nonexistent/src /proc/1/cannot-write || true\n'
        'fi\n'
        'echo REACHED\n',
        encoding="utf-8",
    )

    bad = subprocess.run(["bash", str(unguarded)], text=True, capture_output=True, check=False)
    good = subprocess.run(["bash", str(guarded)], text=True, capture_output=True, check=False)

    assert "REACHED" not in bad.stdout      # 가드 없으면 중단
    assert "REACHED" in good.stdout          # 가드 있으면 계속


# --- archive_dir 옵션 ------------------------------------------------------
# ~/.claude/skills/ 에서는 SKILL.md 보유가 곧 스킬 인식 조건이다. 스킬 디렉터리를
# 옆에 `.replaced.*` 로 백업하면 그 사본이 중복 스킬로 로드된다. 그래서 스킬
# 배포는 백업을 스캔 범위 밖(아카이브)으로 빼야 한다.

def test_archive_dir_moves_backup_outside_dest_parent(tmp_path: Path, src: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    dest = skills / "my-skill"
    dest.mkdir()
    (dest / "SKILL.md").write_text("---\nname: my-skill\n---\n", encoding="utf-8")
    archive = tmp_path / "archive" / "20260825-1"

    result = run_link(src, dest, archive)

    assert result.returncode == 0
    assert dest.is_symlink()
    # 백업이 skills/ 안에 남지 않았다 — 남으면 중복 스킬로 로드된다
    assert list(skills.glob("*.replaced.*")) == []
    assert [p.name for p in skills.iterdir()] == ["my-skill"]
    assert (archive / "my-skill" / "SKILL.md").is_file()


def test_archive_dir_created_lazily(tmp_path: Path, src: Path) -> None:
    """백업할 게 없으면 아카이브 디렉터리를 만들지 않는다."""
    dest = tmp_path / "absent"
    archive = tmp_path / "archive" / "20260825-1"

    result = run_link(src, dest, archive)

    assert result.returncode == 0
    assert dest.is_symlink()
    assert not archive.exists()


def test_archive_dir_failure_skips_link(tmp_path: Path, src: Path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "keep.txt").write_text("x", encoding="utf-8")
    # chmod 로 막으면 root 에서 무시돼 환경 의존이 된다. 부모를 일반 파일로 두면
    # mkdir 이 ENOTDIR 로 실패하므로 uid 와 무관하게 결정적이다.
    blocked = tmp_path / "blocked"
    blocked.write_text("나는 디렉터리가 아니다\n", encoding="utf-8")

    result = run_link(src, dest, blocked / "archive")

    assert result.returncode == 1
    assert "건너뛴다" in result.stderr
    assert not dest.is_symlink()
    assert (dest / "keep.txt").is_file()       # 원본 보존


def test_without_archive_dir_backup_stays_beside(tmp_path: Path, src: Path) -> None:
    """archive_dir 를 안 주면 기존 동작(옆에 .replaced.*)을 유지한다."""
    dest = tmp_path / "dest.sh"
    dest.write_text("old\n", encoding="utf-8")

    result = run_link(src, dest)

    assert result.returncode == 0
    assert len(list(tmp_path.glob("dest.sh.replaced.*"))) == 1
