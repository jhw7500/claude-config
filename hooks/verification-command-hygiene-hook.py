#!/usr/bin/env python3
"""Verification Command Hygiene Guard (PreToolUse: Bash).

Warn when a real shell ``|| echo`` fallback embeds a conclusion marker (``→``
or ``뿐``). Simple absence status and neutral ``(no match)`` output remain
silent: the 2026-09-01 transcript audit showed that broadly matching ``없음``
would make most candidates noise.

Quoted documentation/search literals, comments, heredoc data, later commands,
and redirection targets are not echo output and are ignored. Executable command
substitutions are checked recursively. The hook never denies a tool call and
returns zero on malformed input.
Kill switch: CLAUDE_SKIP_VERIFICATION_COMMAND_HYGIENE=1.
"""

import json
import os
import shlex
import subprocess
import sys
import time


REMINDER = """<system-reminder>
[VERIFICATION-COMMAND-HYGIENE] `|| echo` fallback에 실행 전 작성된 결론이 들어 있다.

검증 명령의 fallback은 `(no match)`처럼 중립적으로 바꾸고, 원인·유일성 판단은
명령 결과와 별도의 반증 확인을 마친 뒤 작성하라.
</system-reminder>"""

CONTROL_OPERATORS = frozenset(
    {"||", "&&", "|", "|&", ";", ";;", ";&", ";;&", "&"}
)
REDIRECTION_OPERATORS = frozenset(
    {"<", ">", ">>", "<<", "<<-", "<<<", "<>", ">&", "<&", ">|", "&>", "&>>"}
)


def collapse_line_continuations(command: str) -> str:
    """Apply Bash's backslash-newline removal outside single quotes."""
    out = []
    quote = ""
    cursor = 0
    while cursor < len(command):
        if (
            quote != "'"
            and command[cursor] == "\\"
            and cursor + 1 < len(command)
            and command[cursor + 1] == "\n"
        ):
            cursor += 2
            continue
        char = command[cursor]
        out.append(char)
        if quote:
            if char == "\\" and quote == '"' and cursor + 1 < len(command):
                cursor += 1
                out.append(command[cursor])
            elif char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == "\\" and cursor + 1 < len(command):
            cursor += 1
            out.append(command[cursor])
        cursor += 1
    return "".join(out)


def strip_shell_comments(command: str) -> str:
    """Remove unquoted comments while preserving their terminating newline."""
    out = []
    quote = ""
    cursor = 0
    word_start = True
    while cursor < len(command):
        char = command[cursor]
        if quote:
            out.append(char)
            if char == "\\" and quote == '"' and cursor + 1 < len(command):
                cursor += 1
                out.append(command[cursor])
            elif char == quote:
                quote = ""
            cursor += 1
            continue
        if char in ("'", '"'):
            quote = char
            word_start = False
            out.append(char)
            cursor += 1
            continue
        if char == "\\" and cursor + 1 < len(command):
            out.extend((char, command[cursor + 1]))
            word_start = False
            cursor += 2
            continue
        if char == "#" and word_start:
            newline = command.find("\n", cursor)
            if newline < 0:
                break
            out.append("\n")
            cursor = newline + 1
            word_start = True
            continue
        out.append(char)
        if char.isspace() or char in ";&|()<>":
            word_start = True
        else:
            word_start = False
        cursor += 1
    return "".join(out)


def read_heredoc_word(line: str, cursor: int) -> "tuple[str, bool, int]":
    """Read one delimiter word while retaining whether shell quoting was used."""
    value = []
    quote = ""
    quoted = False
    while cursor < len(line):
        char = line[cursor]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"' and cursor + 1 < len(line):
                quoted = True
                next_char = line[cursor + 1]
                if next_char in '$`"\\\n':
                    cursor += 1
                    if next_char != "\n":
                        value.append(next_char)
                else:
                    value.append(char)
            else:
                value.append(char)
            cursor += 1
            continue
        if char in ("'", '"'):
            quote = char
            quoted = True
            cursor += 1
            continue
        if char == "\\" and cursor + 1 < len(line):
            quoted = True
            cursor += 1
            value.append(line[cursor])
            cursor += 1
            continue
        if char.isspace() or char in ";&|()<>":
            break
        value.append(char)
        cursor += 1
    return "".join(value), quoted, cursor


def heredoc_specs(line: str) -> "list[tuple[str, bool, bool]]":
    """Return delimiter, tab-strip, and quoted state for one command line."""
    specs = []
    quote = ""
    arithmetic_depth = 0
    cursor = 0
    word_start = True
    while cursor < len(line):
        char = line[cursor]
        if quote:
            if char == "\\" and quote == '"':
                cursor += 2
                continue
            if char == quote:
                quote = ""
            cursor += 1
            continue
        if arithmetic_depth:
            if char == "\\":
                cursor += 2
                continue
            if char in ("'", '"'):
                quote = char
                cursor += 1
                continue
            if line.startswith("((", cursor):
                arithmetic_depth += 1
                cursor += 2
                continue
            if line.startswith("))", cursor):
                arithmetic_depth -= 1
                cursor += 2
                word_start = False
                continue
            cursor += 1
            continue
        if char == "\\":
            cursor += 2
            word_start = False
            continue
        if char in ("'", '"'):
            quote = char
            word_start = False
            cursor += 1
            continue
        if line.startswith("((", cursor):
            arithmetic_depth = 1
            cursor += 2
            word_start = False
            continue
        if char == "#" and word_start:
            break
        if line.startswith("<<<", cursor):
            cursor += 3
            word_start = True
            continue
        if line.startswith("<<", cursor):
            cursor += 2
            strip_tabs = cursor < len(line) and line[cursor] == "-"
            if strip_tabs:
                cursor += 1
            while cursor < len(line) and line[cursor] in " \t":
                cursor += 1
            delimiter, quoted, cursor = read_heredoc_word(line, cursor)
            if delimiter:
                specs.append((delimiter, strip_tabs, quoted))
            word_start = False
            continue
        word_start = char.isspace() or char in ";&|()<>"
        cursor += 1
    return specs


def has_open_shell_context(command: str) -> bool:
    """Return whether quotes or arithmetic keep a command fragment open."""
    quote = ""
    arithmetic_depth = 0
    cursor = 0
    word_start = True
    while cursor < len(command):
        char = command[cursor]
        if quote:
            if char == "\\" and quote in ('"', "`"):
                cursor += 2
                continue
            if char == quote:
                quote = ""
            cursor += 1
            continue
        if arithmetic_depth:
            if char == "\\":
                cursor += 2
                continue
            if char in ("'", '"', "`"):
                quote = char
                cursor += 1
                continue
            if command.startswith("((", cursor):
                arithmetic_depth += 1
                cursor += 2
                continue
            if command.startswith("))", cursor):
                arithmetic_depth -= 1
                cursor += 2
                word_start = False
                continue
            cursor += 1
            continue
        if char == "\\":
            cursor += 2
            word_start = False
            continue
        if char in ("'", '"', "`"):
            quote = char
            word_start = False
            cursor += 1
            continue
        if command.startswith("((", cursor):
            arithmetic_depth = 1
            cursor += 2
            word_start = False
            continue
        if char == "#" and word_start:
            newline = command.find("\n", cursor)
            if newline < 0:
                break
            cursor = newline + 1
            word_start = True
            continue
        word_start = char.isspace() or char in ";&|()<>"
        cursor += 1
    return bool(quote or arithmetic_depth)


def mask_heredoc_bodies(command: str) -> str:
    """Remove heredoc data, where shell-looking text is not command syntax."""
    out = []
    pending = []
    declaration = ""
    for line in command.splitlines(keepends=True):
        if pending:
            delimiter, strip_tabs, quoted, prefix = pending[0]
            content = line.rstrip("\r\n")
            comparable = content.lstrip("\t") if strip_tabs else content
            if not quoted:
                comparable = prefix + comparable
                trailing = len(comparable) - len(comparable.rstrip("\\"))
                if trailing % 2:
                    pending[0] = (delimiter, strip_tabs, quoted, comparable[:-1])
                    comparable = ""
                else:
                    pending[0] = (delimiter, strip_tabs, quoted, "")
            if comparable == delimiter:
                pending.pop(0)
            out.append(
                "".join(char if char in "\r\n" else " " for char in line)
            )
            continue
        out.append(line)
        declaration += line
        logical_declaration = collapse_line_continuations(declaration)
        if line.endswith(("\n", "\r\n")) and (
            not logical_declaration.endswith(("\n", "\r\n"))
            or has_open_shell_context(logical_declaration)
        ):
            continue
        pending.extend(
            (*spec, "") for spec in heredoc_specs(logical_declaration)
        )
        declaration = ""
    return "".join(out)


def substitution_close_candidates(command: str, body_start: int):
    """Yield only ``)`` positions that can lexically close a substitution."""
    searchable = mask_heredoc_bodies(command[body_start:])
    quote = ""
    cursor = 0
    word_start = True
    while cursor < len(searchable):
        char = searchable[cursor]
        if quote:
            if char == "\\" and quote in ('"', "`"):
                cursor += 2
                continue
            if char == quote:
                quote = ""
            cursor += 1
            continue
        if char == "\\":
            cursor += 2
            word_start = False
            continue
        if char in ("'", '"', "`"):
            quote = char
            cursor += 1
            word_start = False
            continue
        if char == "#" and word_start:
            newline = searchable.find("\n", cursor)
            if newline < 0:
                return
            cursor = newline + 1
            word_start = True
            continue
        if char == ")":
            yield body_start + cursor
        word_start = char.isspace() or char in ";&|()<>"
        cursor += 1


class BoundaryOracle:
    """Share a bounded Bash parse-only budget across one hook invocation."""

    MAX_ATTEMPTS = 64
    MAX_SECONDS = 0.5
    ATTEMPT_TIMEOUT = 0.2

    def __init__(self) -> None:
        self.attempts = 0
        self.deadline = time.monotonic() + self.MAX_SECONDS
        self.failed = False
        self._cache = {}
        self._disabled = False

    def find_end(self, command: str, open_paren: int) -> int:
        key = (command, open_paren)
        if key in self._cache:
            return self._cache[key]
        body_start = open_paren + 1
        result = -1
        for cursor in substitution_close_candidates(command, body_start):
            remaining = self.deadline - time.monotonic()
            if (
                self._disabled
                or self.attempts >= self.MAX_ATTEMPTS
                or remaining <= 0
            ):
                break
            self.attempts += 1
            probe = ': "$(' + command[body_start : cursor + 1] + '"\n'
            try:
                parsed = subprocess.run(
                    ["/bin/bash", "--noprofile", "--norc", "-n"],
                    input=probe,
                    encoding="utf-8",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=min(self.ATTEMPT_TIMEOUT, remaining),
                    env={"LC_ALL": "C"},
                )
            except (OSError, subprocess.SubprocessError, ValueError):
                self._disabled = True
                break
            if parsed.returncode == 0:
                result = cursor
                break
        self._cache[key] = result
        if result < 0:
            self.failed = True
        return result


def command_substitution_end(
    command: str,
    open_paren: int,
    oracle: "BoundaryOracle | None" = None,
) -> int:
    """Ask Bash's parse-only mode which ``)`` closes a substitution."""
    if oracle is None:
        oracle = BoundaryOracle()
    return oracle.find_end(command, open_paren)


def command_substitutions(command: str, oracle: BoundaryOracle):
    """Yield executable ``$(...)`` bodies, including those in double quotes."""
    quote = ""
    cursor = 0
    while cursor < len(command):
        char = command[cursor]
        if quote == "'":
            if char == "'":
                quote = ""
            cursor += 1
            continue
        if char == "\\":
            cursor += 2
            continue
        if char == "'":
            quote = "'"
            cursor += 1
            continue
        if char == '"':
            quote = "" if quote == '"' else '"'
            cursor += 1
            continue
        if command.startswith("$(", cursor) and not command.startswith("$((", cursor):
            end = command_substitution_end(command, cursor + 1, oracle)
            if end < 0:
                return
            yield command[cursor + 2 : end]
            cursor = end + 1
            continue
        cursor += 1


def process_substitution_spans(
    command: str,
    oracle: BoundaryOracle,
) -> "tuple[tuple[int, int, str], ...]":
    """Return executable ``<(...)``/``>(...)`` spans outside shell quotes."""
    spans = []
    quote = ""
    cursor = 0
    while cursor < len(command):
        char = command[cursor]
        if quote:
            if char == "\\" and quote in ('"', "`"):
                cursor += 2
                continue
            if char == quote:
                quote = ""
            cursor += 1
            continue
        if char == "\\":
            cursor += 2
            continue
        if char in ("'", '"', "`"):
            quote = char
            cursor += 1
            continue
        if char in "<>" and command.startswith("(", cursor + 1):
            end = command_substitution_end(command, cursor + 1, oracle)
            if end < 0:
                break
            spans.append((cursor, end, command[cursor + 2 : end]))
            cursor = end + 1
            continue
        cursor += 1
    return tuple(spans)


def mask_process_substitutions(
    command: str,
    spans: "tuple[tuple[int, int, str], ...]",
) -> str:
    """Hide process substitutions while retaining outer command boundaries."""
    masked = list(command)
    for start, end, _ in spans:
        masked[start : end + 1] = " " * (end - start + 1)
    return "".join(masked)


def backtick_substitutions(command: str):
    """Yield executable legacy backtick bodies, including inside double quotes."""
    quote = ""
    cursor = 0
    while cursor < len(command):
        char = command[cursor]
        if quote == "'":
            if char == "'":
                quote = ""
            cursor += 1
            continue
        if char == "\\":
            cursor += 2
            continue
        if char == "'":
            quote = "'"
            cursor += 1
            continue
        if char == '"':
            quote = "" if quote == '"' else '"'
            cursor += 1
            continue
        if char != "`":
            cursor += 1
            continue

        body = []
        cursor += 1
        while cursor < len(command):
            char = command[cursor]
            if char == "\\" and cursor + 1 < len(command):
                cursor += 1
                body.append(command[cursor])
                cursor += 1
                continue
            if char == "`":
                yield "".join(body)
                cursor += 1
                break
            body.append(char)
            cursor += 1
        else:
            return


def unquoted_or_suffixes(command: str):
    """Yield text after real ``||`` operators, excluding quoted literals."""
    quote = ""
    cursor = 0
    while cursor < len(command):
        char = command[cursor]
        if quote:
            if char == "\\" and quote == '"':
                cursor += 2
                continue
            if char == quote:
                quote = ""
            cursor += 1
            continue
        if char == "\\":
            cursor += 2
            continue
        if char in ("'", '"'):
            quote = char
            cursor += 1
            continue
        if command.startswith("||", cursor):
            yield command[cursor + 2 :]
            cursor += 2
            continue
        cursor += 1


def first_simple_command(suffix: str) -> str:
    """Return the first command after ``||``, bounded by its real terminator."""
    cursor = 0
    while cursor < len(suffix) and suffix[cursor].isspace():
        cursor += 1
    start = cursor
    quote = ""
    while cursor < len(suffix):
        char = suffix[cursor]
        if quote:
            if char == "\\" and quote == '"':
                cursor += 2
                continue
            if char == quote:
                quote = ""
            cursor += 1
            continue
        if char == "\\":
            cursor += 2
            continue
        if char in ("'", '"'):
            quote = char
            cursor += 1
            continue
        if char == "\n" or char in ";&|":
            break
        cursor += 1
    return suffix[start:cursor]


def echo_message(suffix: str) -> str:
    lexer = shlex.shlex(
        first_simple_command(suffix),
        posix=True,
        punctuation_chars="|;&<>",
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        token = next(lexer, "")
        if token != "echo":
            return ""
        token = next(lexer, "")
        while (
            len(token) > 1
            and token.startswith("-")
            and set(token[1:]).issubset({"n", "e", "E"})
        ):
            token = next(lexer, "")

        parts = []
        while token and token not in CONTROL_OPERATORS:
            if token in REDIRECTION_OPERATORS:
                next(lexer, "")
                token = next(lexer, "")
                continue
            parts.append(token)
            token = next(lexer, "")
        return " ".join(parts)
    except ValueError:
        return ""


def has_conclusion_fallback(
    command: str,
    oracle: "BoundaryOracle | None" = None,
) -> bool:
    if oracle is None:
        oracle = BoundaryOracle()
    prepared = mask_heredoc_bodies(command)
    prepared = collapse_line_continuations(prepared)
    prepared = strip_shell_comments(prepared)
    process_spans = process_substitution_spans(prepared, oracle)
    if oracle.failed:
        return False
    outer_command = mask_process_substitutions(prepared, process_spans)
    command_bodies = tuple(command_substitutions(outer_command, oracle))
    if oracle.failed:
        return False
    detected = False
    for substitution in command_bodies:
        detected |= has_conclusion_fallback(substitution, oracle)
        if oracle.failed:
            return False
    for substitution in backtick_substitutions(outer_command):
        detected |= has_conclusion_fallback(substitution, oracle)
        if oracle.failed:
            return False
    for _, _, substitution in process_spans:
        detected |= has_conclusion_fallback(substitution, oracle)
        if oracle.failed:
            return False
    if detected:
        return True
    for suffix in unquoted_or_suffixes(outer_command):
        message = echo_message(suffix)
        if "→" in message or "뿐" in message:
            return True

    return False


def main() -> int:
    if os.environ.get("CLAUDE_SKIP_VERIFICATION_COMMAND_HYGIENE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    tool_name = payload.get("tool_name") or payload.get("toolName")
    if tool_name != "Bash":
        return 0

    tool_input = payload.get("tool_input") or payload.get("toolInput")
    if not isinstance(tool_input, dict):
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str):
        return 0

    if has_conclusion_fallback(command):
        sys.stdout.write(REMINDER)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
