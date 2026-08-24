#!/usr/bin/env python3
"""Read a repo-local env file through one verified file descriptor."""

from __future__ import annotations

import os
import stat
import sys
from os import PathLike


MAX_SECRET_BYTES = 1024 * 1024
UNSAFE_MESSAGE = (
    "unsafe secret file: require a regular non-symlink file owned by the "
    "current user with mode 0600 and one hard link"
)


class UnsafeSecretFile(RuntimeError):
    """Raised when a secret file cannot be opened and validated safely."""


def read_private_env(path: str | PathLike[str]) -> bytes:
    """Open, validate, and read *path* without reopening its directory entry."""

    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        descriptor = os.open(os.fspath(path), flags)
    except (OSError, TypeError):
        raise UnsafeSecretFile from None

    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise UnsafeSecretFile

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SECRET_BYTES:
                raise UnsafeSecretFile
            chunks.append(chunk)

        payload = b"".join(chunks)
        if b"\0" in payload:
            raise UnsafeSecretFile
        return payload
    except OSError:
        raise UnsafeSecretFile from None
    finally:
        os.close(descriptor)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(UNSAFE_MESSAGE, file=sys.stderr)
        return 1

    try:
        payload = read_private_env(argv[1])
    except UnsafeSecretFile:
        print(UNSAFE_MESSAGE, file=sys.stderr)
        return 1

    try:
        sys.stdout.buffer.write(payload)
    except OSError:
        print(UNSAFE_MESSAGE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
