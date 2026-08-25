"""사설 설정 파일의 원자적 갱신과 백업 계약.

install.sh 가 CLAUDE.md 와 settings.json 을 제자리에서 truncate 후 쓰면 중단 시
부분 파일이 남는다. 또 내용이 그대로인 재실행도 매번 백업을 남겨 백업 디렉터리가
동일 사본으로 채워진다(실측: CLAUDE.md 백업 14개 중 13개가 byte-identical).

write_private 는 세 가지를 보장한다.
  1. 내용이 같으면 아무것도 하지 않는다 — 백업도 만들지 않는다.
  2. 실제로 바뀔 때만 백업을 정확히 하나, 0600 으로 만든다.
  3. 같은 디렉터리의 임시 파일에 완성한 뒤 os.replace 로 교체한다. 교체 전에
     실패하면 기존 파일이 그대로 남는다.
"""
from __future__ import annotations

import datetime
import os
import tempfile

PRIVATE_MODE = 0o600


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _write_new(path: str, data: bytes, mode: int) -> None:
    """같은 디렉터리 임시 파일에 완성한 뒤 원자적으로 교체한다."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_private(path: str, text: str, mode: int = PRIVATE_MODE, now=None) -> str:
    """'unchanged' | 'created' | 'updated' 를 반환한다."""
    data = text.encode("utf-8")
    current = _read(path)

    if current == data:
        os.chmod(path, mode)          # 내용은 같아도 mode 계약은 강제한다
        return "unchanged"

    if current is not None:
        stamp = (now or datetime.datetime.now()).strftime("%Y%m%d%H%M%S")
        _write_new(f"{path}.bak.{stamp}", current, mode)
        result = "updated"
    else:
        result = "created"

    _write_new(path, data, mode)
    return result
