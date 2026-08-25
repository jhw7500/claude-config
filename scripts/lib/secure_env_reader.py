#!/usr/bin/env python3
"""Read the repo-local Slack env file through one verified file descriptor."""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Mapping
from os import PathLike


MAX_SECRET_BYTES = 1024 * 1024
UNSAFE_MESSAGE = (
    "unsafe secret file: require a regular non-symlink file owned by the "
    "current user with mode 0600 and one hard link"
)
INVALID_CONTENT_MESSAGE = "unsafe secret file content: expected data-only KEY=VALUE assignments"
ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$"
)
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
UNQUOTED_CONTROL_CHARACTERS = frozenset(";&|<>()")
ALLOWED_ENV_NAMES = frozenset(
    {
        "SLACK_ALLOWED_USER_ID",
        "SLACK_APP_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_CHANNEL_ID",
    }
)


class UnsafeSecretFile(RuntimeError):
    """Raised when a secret file cannot be opened and validated safely."""


class InvalidEnvFile(ValueError):
    """Raised when a verified file contains unsupported environment syntax."""

    def __init__(self, line_number: int, reason: str) -> None:
        super().__init__(reason)
        self.line_number = line_number
        self.reason = reason


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


def _invalid(line_number: int, reason: str) -> InvalidEnvFile:
    return InvalidEnvFile(line_number, reason)


def _expand_reference(
    text: str,
    offset: int,
    variables: Mapping[str, str],
    line_number: int,
) -> tuple[str, int]:
    next_offset = offset + 1
    if next_offset >= len(text):
        raise _invalid(line_number, "unsupported variable expansion")

    if text[next_offset] == "{":
        closing = text.find("}", next_offset + 1)
        if closing == -1:
            raise _invalid(line_number, "unterminated variable expansion")
        name = text[next_offset + 1 : closing]
        if NAME_RE.fullmatch(name) is None:
            raise _invalid(line_number, "unsupported variable expansion")
        end_offset = closing + 1
    else:
        match = NAME_RE.match(text, next_offset)
        if match is None:
            raise _invalid(line_number, "unsupported variable expansion")
        name = match.group(0)
        end_offset = match.end()

    if name not in variables:
        raise _invalid(line_number, "undefined variable reference")
    return variables[name], end_offset


def _parse_double_quoted(
    raw_value: str,
    variables: Mapping[str, str],
    line_number: int,
) -> tuple[str, str]:
    value: list[str] = []
    offset = 1
    while offset < len(raw_value):
        character = raw_value[offset]
        if character == '"':
            return "".join(value), raw_value[offset + 1 :]
        if character == "`":
            raise _invalid(line_number, "command syntax is not allowed")
        if character == "\\":
            if offset + 1 >= len(raw_value):
                raise _invalid(line_number, "unterminated quoted value")
            escaped = raw_value[offset + 1]
            if escaped in {'"', "\\", "$"}:
                value.append(escaped)
                offset += 2
                continue
            value.append("\\")
            offset += 1
            continue
        if character == "$":
            expanded, offset = _expand_reference(
                raw_value,
                offset,
                variables,
                line_number,
            )
            value.append(expanded)
            continue
        value.append(character)
        offset += 1
    raise _invalid(line_number, "unterminated quoted value")


def _validate_quoted_tail(tail: str, line_number: int) -> None:
    if not tail:
        return
    if tail[0] not in " \t":
        raise _invalid(line_number, "quoted and unquoted fragments cannot be combined")
    remainder = tail.lstrip(" \t")
    if remainder and not remainder.startswith("#"):
        raise _invalid(line_number, "unexpected content after quoted value")


def _parse_unquoted(
    raw_value: str,
    variables: Mapping[str, str],
    line_number: int,
) -> str:
    value: list[str] = []
    offset = 0
    while offset < len(raw_value):
        character = raw_value[offset]
        if character in " \t":
            remainder = raw_value[offset:].lstrip(" \t")
            if not remainder or remainder.startswith("#"):
                break
            raise _invalid(line_number, "unquoted whitespace is not allowed")
        if character in {'"', "'"}:
            raise _invalid(line_number, "quoted and unquoted fragments cannot be combined")
        if character == "`" or character in UNQUOTED_CONTROL_CHARACTERS:
            raise _invalid(line_number, "command syntax is not allowed")
        if character == "\\":
            if offset + 1 == len(raw_value):
                value.append("\\")
                offset += 1
                continue
            escaped = raw_value[offset + 1]
            if escaped in {" ", "\t", "#", "$", "\\"}:
                value.append(escaped)
                offset += 2
                continue
            value.append("\\")
            offset += 1
            continue
        if character == "$":
            expanded, offset = _expand_reference(
                raw_value,
                offset,
                variables,
                line_number,
            )
            value.append(expanded)
            continue
        value.append(character)
        offset += 1
    return "".join(value)


def _parse_value(
    raw_value: str,
    variables: Mapping[str, str],
    line_number: int,
) -> str:
    raw_value = raw_value.lstrip(" \t")
    if not raw_value or raw_value.startswith("#"):
        return ""

    if raw_value.startswith("'"):
        closing = raw_value.find("'", 1)
        if closing == -1:
            raise _invalid(line_number, "unterminated quoted value")
        value = raw_value[1:closing]
        _validate_quoted_tail(raw_value[closing + 1 :], line_number)
        return value

    if raw_value.startswith('"'):
        value, tail = _parse_double_quoted(raw_value, variables, line_number)
        _validate_quoted_tail(tail, line_number)
        return value

    return _parse_unquoted(raw_value, variables, line_number)


def parse_env_assignments(
    payload: bytes,
    environ: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Parse a strict data-only subset of dotenv syntax without shell evaluation."""

    if b"\0" in payload:
        raise _invalid(1, "NUL bytes are not allowed")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid(1, "content must be UTF-8") from None

    variables = dict(os.environ if environ is None else environ)
    assignments: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(text.split("\n"), 1):
        if raw_line.endswith("\r"):
            raw_line = raw_line[:-1]
        if "\r" in raw_line:
            raise _invalid(line_number, "bare carriage returns are not allowed")

        stripped = raw_line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue

        match = ASSIGNMENT_RE.fullmatch(raw_line)
        if match is None:
            raise _invalid(line_number, "only one KEY=VALUE assignment is allowed per line")
        name, raw_value = match.groups()
        if name not in ALLOWED_ENV_NAMES:
            raise _invalid(line_number, "unsupported environment variable")
        if name in seen:
            raise _invalid(line_number, "duplicate assignment")

        value = _parse_value(raw_value, variables, line_number)
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise _invalid(line_number, "control characters are not allowed in values")
        assignments.append((name, value))
        variables[name] = value
        seen.add(name)
    return assignments


def encode_env_assignments(assignments: list[tuple[str, str]]) -> bytes:
    """Encode assignments as NUL-delimited key/value records for Bash."""

    fields: list[bytes] = []
    for name, value in assignments:
        fields.extend((name.encode("ascii"), value.encode("utf-8")))
    return b"\0".join(fields) + (b"\0" if fields else b"")


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
        assignments = parse_env_assignments(payload)
    except InvalidEnvFile as error:
        print(
            f"{INVALID_CONTENT_MESSAGE} (line {error.line_number}: {error.reason})",
            file=sys.stderr,
        )
        return 1

    try:
        sys.stdout.buffer.write(encode_env_assignments(assignments))
    except OSError:
        print(UNSAFE_MESSAGE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
