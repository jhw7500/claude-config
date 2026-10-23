"""One-shot, TTL-bound approval for a single rule-violating ``git commit``.

A grant records what a policy-following agent says the user chose in its
prompt.  It is friction against accidental bypass, not cryptographic proof of
human presence.  The gate consumes a matching grant while holding an exclusive
lock on the state directory, so at most one commit can use it.

Grants live outside the repository in ``<home>/.local/state/claude-config/
commit-gate`` (directory 0700, files exactly 0600, at most 16 KiB).  Symlinks
are never followed, and anything not owned by the current user, wider than
expected, oversized or malformed is treated as "no grant".
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal, Optional

GRANT_TTL_SECONDS = 600
MAX_GRANT_BYTES = 16 * 1024
MAX_REASON_CHARS = 512
UNBORN = "UNBORN"
_SCHEMA = 1
RUNTIMES = frozenset(("claude", "codex"))
_CHANNELS = frozenset(("tty", "relayed"))
_BINDING_KEYS = frozenset(("repo_root", "head", "message_sha256", "runtime"))
_HEX = frozenset("0123456789abcdef")


def state_dir(home: Path) -> Path:
    return Path(home) / ".local" / "state" / "claude-config" / "commit-gate"


def is_hex(value: object, lengths: tuple[int, ...]) -> bool:
    return (
        isinstance(value, str)
        and len(value) in lengths
        and all(c in _HEX for c in value)
    )


def make_binding(
    *, repo_root: Path, head: Optional[str], message_sha256: str, runtime: str
) -> dict[str, str]:
    return {
        "repo_root": os.path.realpath(repo_root),
        "head": head or UNBORN,
        "message_sha256": message_sha256,
        "runtime": runtime,
    }


def _valid_binding(binding: object) -> bool:
    return (
        isinstance(binding, dict)
        and set(binding) == _BINDING_KEYS
        and isinstance(binding["repo_root"], str)
        and bool(binding["repo_root"])
        and (binding["head"] == UNBORN or is_hex(binding["head"], (40, 64)))
        and is_hex(binding["message_sha256"], (64,))
        and isinstance(binding["runtime"], str)
        and binding["runtime"] in RUNTIMES
    )


def binding_name(binding: dict[str, str]) -> str:
    raw = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _check_dir_fd(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise OSError("commit-gate state directory is unsafe")


def _open_dir(home: Path, *, create: bool) -> int:
    path = state_dir(home)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _check_dir_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def _locked_dir(home: Path, *, create: bool) -> Iterator[int]:
    fd = _open_dir(home, create=create)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)  # also releases the flock


def _read_grant(dir_fd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > MAX_GRANT_BYTES
        ):
            raise OSError("commit-gate grant file is unsafe")
        data = os.read(fd, MAX_GRANT_BYTES + 1)
        if len(data) > MAX_GRANT_BYTES:
            raise OSError("commit-gate grant file is too large")
        return data
    finally:
        os.close(fd)


def _parse(raw: bytes, name: str) -> Optional[dict]:
    """Return the validated payload, or None when anything is off."""
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    created = payload.get("created_at")
    if (
        payload.get("schema") != _SCHEMA
        or not _valid_binding(payload.get("binding"))
        or binding_name(payload["binding"]) != name
        or not isinstance(payload.get("channel"), str)
        or payload["channel"] not in _CHANNELS
        or not isinstance(payload.get("reason"), str)
        or len(payload["reason"]) > MAX_REASON_CHARS
        or isinstance(created, bool)
        or not isinstance(created, (int, float))
        or not math.isfinite(created)
    ):
        return None
    return payload


def _unlink(dir_fd: int, name: str) -> None:
    os.unlink(name, dir_fd=dir_fd)
    os.fsync(dir_fd)


def record_grant(
    home: Path,
    *,
    binding: dict[str, str],
    reason: str,
    channel: Literal["tty", "relayed"],
    now: float,
) -> Path:
    if not _valid_binding(binding):
        raise ValueError("invalid commit-gate binding")
    if channel not in _CHANNELS:
        raise ValueError("invalid commit-gate channel")
    payload = {
        "schema": _SCHEMA,
        "binding": binding,
        "reason": str(reason)[:MAX_REASON_CHARS],
        "channel": channel,
        "created_at": now,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_GRANT_BYTES:
        raise ValueError("commit-gate grant is too large")
    name = binding_name(binding)
    temp = f".{name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    with _locked_dir(home, create=True) as dir_fd:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, raw)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            os.unlink(temp, dir_fd=dir_fd)
            raise
        os.close(fd)
        try:
            os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            os.unlink(temp, dir_fd=dir_fd)
            raise
        os.fsync(dir_fd)
    return state_dir(home) / name


def consume_grant(home: Path, *, binding: dict[str, str], now: float) -> bool:
    """Atomically consume one exact, unexpired grant.  Never raises."""
    try:
        if not _valid_binding(binding):
            return False
        name = binding_name(binding)
        with _locked_dir(home, create=False) as dir_fd:
            try:
                raw = _read_grant(dir_fd, name)
            except FileNotFoundError:
                return False
            payload = _parse(raw, name)
            if payload is None or payload["binding"] != binding:
                return False
            age = now - payload["created_at"]
            if age > GRANT_TTL_SECONDS:
                _unlink(dir_fd, name)
                return False
            if age < 0:
                return False
            _unlink(dir_fd, name)
            return True
    except Exception:  # noqa: BLE001 - the hook must fail closed, not crash
        return False


def revoke_repo(home: Path, *, repo_root: Path) -> int:
    """Delete every readable grant bound to ``repo_root``; return the count."""
    return revoke_repo_report(home, repo_root=repo_root)[0]


def revoke_repo_report(home: Path, *, repo_root: Path) -> tuple[int, int]:
    """Like ``revoke_repo`` but returns ``(removed, skipped)``.

    A grant file that cannot be read or parsed is skipped, never fatal.
    """
    root = os.path.realpath(repo_root)
    removed = 0
    skipped = 0
    try:
        with _locked_dir(home, create=False) as dir_fd:
            for name in os.listdir(dir_fd):
                if not is_hex(name, (64,)):
                    continue
                try:
                    payload = _parse(_read_grant(dir_fd, name), name)
                    if payload is not None and payload["binding"]["repo_root"] == root:
                        _unlink(dir_fd, name)
                        removed += 1
                    elif payload is None:
                        skipped += 1
                except Exception:  # noqa: BLE001 - one bad file must not stop revocation
                    skipped += 1
    except FileNotFoundError:
        return 0, 0
    return removed, skipped
