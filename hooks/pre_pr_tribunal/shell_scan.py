"""Bounded, non-executing recognition of direct ``gh pr create`` commands."""

from dataclasses import dataclass, field
from enum import Enum
import re


MAX_COMMAND_BYTES = 256 * 1024
MAX_TOKENS = 4096
MAX_RECURSION = 16


class ScanKind(str, Enum):
    NO_MATCH = "no_match"
    PR_CREATE = "pr_create"
    AMBIGUOUS_CANDIDATE = "ambiguous_candidate"


@dataclass(frozen=True)
class ScanResult:
    kind: ScanKind
    reason: str | None = None


class ScanFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class _Budget:
    count: int = 0
    limited: bool = True

    def take(self) -> None:
        self.count += 1
        if self.limited and self.count > MAX_TOKENS:
            raise ScanFailure("TOKEN_LIMIT")

    def refund(self) -> None:
        self.count -= 1


@dataclass
class _Word:
    text: str
    quoted: bool = False
    dynamic: bool = False
    assignment: bool = False
    nested: list["_Context"] = field(default_factory=list)


@dataclass
class _Token:
    kind: str
    start: int
    end: int
    value: str = ""
    word: _Word | None = None
    role: str = "NORMAL"
    nested: "_Context | None" = None


@dataclass
class _Context:
    depth: int
    tokens: list[_Token] = field(default_factory=list)


_GH_VALUE_OPTIONS = {"--repo", "-R", "--hostname", "--config"}
_GH_FLAG_OPTIONS = {"--help", "--version"}
_ENV_FLAGS = {"-i", "--ignore-environment"}
_ENV_VALUE_OPTIONS = {"-u", "-C"}
_SHELLS = {"sh", "bash", "dash"}


def scan_pr_create(command: str) -> ScanResult:
    if not isinstance(command, str) or "\x00" in command:
        return ScanResult(ScanKind.NO_MATCH)
    candidate_hint = _has_unquoted_candidate_hint(command)
    if len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND_BYTES:
        kind = ScanKind.AMBIGUOUS_CANDIDATE if candidate_hint else ScanKind.NO_MATCH
        return ScanResult(kind, "COMMAND_LIMIT")
    try:
        return _scan_context(command, depth=0)
    except (RecursionError, ScanFailure) as error:
        code = "RECURSION_LIMIT" if isinstance(error, RecursionError) else error.code
        kind = ScanKind.AMBIGUOUS_CANDIDATE if candidate_hint else ScanKind.NO_MATCH
        return ScanResult(kind, code)


class _Parser:
    def __init__(
        self,
        command: str,
        budget: _Budget,
        *,
        tolerant: bool = False,
    ) -> None:
        self.command = command
        self.length = len(command)
        self.index = 0
        self.budget = budget
        self.tolerant = tolerant

    def parse(self, depth: int, terminator: str | None = None) -> _Context:
        if not self.tolerant and depth > MAX_RECURSION:
            raise ScanFailure("RECURSION_LIMIT")
        context = _Context(depth)
        heredocs: list[tuple[str, bool]] = []

        while self.index < self.length:
            char = self.command[self.index]
            if terminator is not None and char == terminator:
                self.index += 1
                return context
            if char in " \t":
                self.index += 1
                continue
            if char == "\\" and self._peek(1) == "\n":
                self.index += 2
                continue
            if char == "#":
                newline = self.command.find("\n", self.index)
                self.index = self.length if newline < 0 else newline
                continue
            if char == "\n":
                self._add(context, _Token("OP", self.index, self.index + 1, "\n"))
                self.index += 1
                if heredocs:
                    self._consume_heredocs(heredocs)
                    heredocs.clear()
                continue
            if char == ")":
                if self.tolerant:
                    self._add(context, _Token("OP", self.index, self.index + 1, ";"))
                    self.index += 1
                    continue
                raise ScanFailure("PARSE_ERROR")
            if char == "(":
                if context.tokens and context.tokens[-1].kind not in {"OP", "NESTED"}:
                    if not self.tolerant:
                        raise ScanFailure("PARSE_ERROR")
                start = self.index
                self.index += 1
                nested = self.parse(depth + 1, ")")
                self._add(
                    context,
                    _Token("NESTED", start, self.index, nested=nested),
                )
                continue
            operator = self._operator()
            if operator is not None:
                start = self.index
                self.index += len(operator)
                self._add(context, _Token("OP", start, self.index, operator))
                continue
            redirection = self._redirection()
            if redirection is not None:
                self._parse_redirection(
                    context, heredocs, redirection, depth, terminator
                )
                continue

            word, start, end = self._parse_word(depth, terminator)
            self._add(context, _Token("WORD", start, end, word=word))

        if heredocs and not self.tolerant:
            raise ScanFailure("PARSE_ERROR")
        if terminator is not None and not self.tolerant:
            raise ScanFailure("PARSE_ERROR")
        return context

    def _parse_word(
        self, depth: int, terminator: str | None = None
    ) -> tuple[_Word, int, int]:
        start = self.index
        pieces: list[str] = []
        word = _Word("")
        assignment_prefix_valid = True
        while self.index < self.length:
            char = self.command[self.index]
            if terminator is not None and char == terminator:
                break
            if char in " \t\n;<>()|&":
                break
            if char in "<>":
                break
            if char == "\\":
                if self.index + 1 >= self.length:
                    if not self.tolerant:
                        raise ScanFailure("PARSE_ERROR")
                    self.index += 1
                    break
                escaped = self.command[self.index + 1]
                self.index += 2
                if escaped != "\n":
                    if not word.assignment:
                        assignment_prefix_valid = False
                    pieces.append(escaped)
                continue
            if char == "'":
                if not word.assignment:
                    assignment_prefix_valid = False
                word.quoted = True
                self.index += 1
                closing = self.command.find("'", self.index)
                if closing < 0:
                    pieces.append(self.command[self.index :])
                    self.index = self.length
                    if not self.tolerant:
                        raise ScanFailure("PARSE_ERROR")
                    break
                pieces.append(self.command[self.index : closing])
                self.index = closing + 1
                continue
            if char == '"':
                if not word.assignment:
                    assignment_prefix_valid = False
                word.quoted = True
                self.index += 1
                self._parse_double_quoted(word, pieces, depth)
                continue
            if char == "$" and self._peek(1) == "(":
                if not word.assignment:
                    assignment_prefix_valid = False
                word.dynamic = True
                self.index += 2
                word.nested.append(self.parse(depth + 1, ")"))
                continue
            if char == "`":
                if not word.assignment:
                    assignment_prefix_valid = False
                word.dynamic = True
                self.index += 1
                word.nested.append(self.parse(depth + 1, "`"))
                continue
            if char == "$":
                if not word.assignment:
                    assignment_prefix_valid = False
                word.dynamic = True
            if (
                char == "="
                and not word.assignment
                and assignment_prefix_valid
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", "".join(pieces))
            ):
                word.assignment = True
            pieces.append(char)
            self.index += 1
        word.text = "".join(pieces)
        return word, start, self.index

    def _parse_double_quoted(
        self, word: _Word, pieces: list[str], depth: int
    ) -> None:
        while self.index < self.length:
            char = self.command[self.index]
            if char == '"':
                self.index += 1
                return
            if char == "\\":
                following = self._peek(1)
                if following in {'$', '`', '"', "\\"}:
                    pieces.append(following)
                    self.index += 2
                    continue
                if following == "\n":
                    self.index += 2
                    continue
                pieces.append("\\")
                self.index += 1
                continue
            if char == "$" and self._peek(1) == "(":
                word.dynamic = True
                self.index += 2
                word.nested.append(self.parse(depth + 1, ")"))
                continue
            if char == "`":
                word.dynamic = True
                self.index += 1
                word.nested.append(self.parse(depth + 1, "`"))
                continue
            if char == "$":
                word.dynamic = True
            pieces.append(char)
            self.index += 1
        if not self.tolerant:
            raise ScanFailure("PARSE_ERROR")

    def _parse_redirection(
        self,
        context: _Context,
        heredocs: list[tuple[str, bool]],
        operator: str,
        depth: int,
        terminator: str | None,
    ) -> None:
        start = self.index
        if (
            context.tokens
            and context.tokens[-1].kind == "WORD"
            and context.tokens[-1].end == start
            and context.tokens[-1].word is not None
            and not context.tokens[-1].word.quoted
            and context.tokens[-1].word.text.isdigit()
        ):
            context.tokens.pop()
            self.budget.refund()
        self.index += len(operator)
        self._add(context, _Token("REDIR", start, self.index, operator))
        while self.index < self.length and self.command[self.index] in " \t":
            self.index += 1
        if self.index >= self.length or self.command[self.index] == "\n":
            if not self.tolerant:
                raise ScanFailure("PARSE_ERROR")
            return
        word, word_start, word_end = self._parse_word(depth, terminator)
        role = "HEREDOC" if operator in {"<<", "<<-"} else "REDIR"
        self._add(
            context,
            _Token("WORD", word_start, word_end, word=word, role=role),
        )
        if role == "HEREDOC":
            heredocs.append((word.text, operator == "<<-"))

    def _consume_heredocs(self, heredocs: list[tuple[str, bool]]) -> None:
        for delimiter, strip_tabs in heredocs:
            while self.index <= self.length:
                newline = self.command.find("\n", self.index)
                end = self.length if newline < 0 else newline
                line = self.command[self.index : end]
                compared = line.lstrip("\t") if strip_tabs else line
                if compared == delimiter:
                    self.index = end if newline < 0 else end + 1
                    break
                if newline < 0:
                    self.index = self.length
                    if not self.tolerant:
                        raise ScanFailure("PARSE_ERROR")
                    break
                self.index = newline + 1

    def _operator(self) -> str | None:
        for operator in ("&&", "||", ";", "|", "&"):
            if self.command.startswith(operator, self.index):
                return operator
        return None

    def _redirection(self) -> str | None:
        for operator in ("<<<", "<<-", "<<", ">>", "<&", ">&", "<>", ">|", "<", ">"):
            if self.command.startswith(operator, self.index):
                return operator
        return None

    def _peek(self, offset: int) -> str:
        index = self.index + offset
        return "" if index >= self.length else self.command[index]

    def _add(self, context: _Context, token: _Token) -> None:
        self.budget.take()
        context.tokens.append(token)


def _scan_context(command: str, depth: int) -> ScanResult:
    budget = _Budget()
    context = _Parser(command, budget).parse(depth)
    if _scan_parsed_context(context, budget):
        return ScanResult(ScanKind.PR_CREATE)
    return ScanResult(ScanKind.NO_MATCH)


def _scan_parsed_context(context: _Context, budget: _Budget) -> bool:
    for token in context.tokens:
        if token.kind == "NESTED" and token.nested is not None:
            if _scan_parsed_context(token.nested, budget):
                return True
        if token.kind == "WORD" and token.word is not None and token.role != "HEREDOC":
            for nested in token.word.nested:
                if _scan_parsed_context(nested, budget):
                    return True

    segment: list[_Token] = []
    for token in context.tokens + [_Token("OP", -1, -1, ";")]:
        if token.kind in {"OP", "NESTED"}:
            if _scan_simple_command(segment, context.depth, budget):
                return True
            segment = []
        else:
            segment.append(token)
    return False


def _scan_simple_command(tokens: list[_Token], depth: int, budget: _Budget) -> bool:
    words = [
        token.word
        for token in tokens
        if token.kind == "WORD" and token.role == "NORMAL" and token.word is not None
    ]
    if not words:
        return False

    index = 0
    while index < len(words) and _is_assignment(words[index]):
        index += 1
    while index < len(words):
        executable = words[index]
        if executable.dynamic:
            return False
        name = _basename(executable.text)
        if name == "command":
            index += 1
            if index < len(words) and words[index].text == "--":
                index += 1
            elif index < len(words) and words[index].text.startswith("-"):
                if _has_remaining_gh(words, index + 1):
                    raise ScanFailure("UNKNOWN_COMMAND_OPTION")
                return False
            continue
        if name == "env":
            index = _skip_env(words, index + 1)
            continue
        break

    if index >= len(words):
        return False
    executable = words[index]
    if executable.dynamic:
        return False
    name = _basename(executable.text)
    arguments = words[index + 1 :]
    if name == "gh":
        return _scan_gh(arguments)
    if name in _SHELLS and len(arguments) >= 2 and arguments[0].text == "-c":
        script = arguments[1]
        if not script.dynamic and not script.nested:
            parser = _Parser(script.text, budget)
            nested = parser.parse(depth + 1)
            return _scan_parsed_context(nested, budget)
    return False


def _skip_env(words: list[_Word], index: int) -> int:
    while index < len(words):
        word = words[index]
        if word.dynamic:
            return len(words)
        value = word.text
        if _is_assignment(word):
            index += 1
            continue
        if value in _ENV_FLAGS:
            index += 1
            continue
        if value in _ENV_VALUE_OPTIONS:
            if index + 1 >= len(words):
                raise ScanFailure("INCOMPLETE_ENV_OPTION")
            index += 2
            continue
        if value.startswith("--unset=") or value.startswith("--chdir="):
            index += 1
            continue
        if value.startswith("-"):
            if _has_remaining_gh(words, index + 1):
                raise ScanFailure("UNKNOWN_ENV_OPTION")
            return len(words)
        return index
    return index


def _scan_gh(arguments: list[_Word]) -> bool:
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word.dynamic:
            return False
        value = word.text
        if value in _GH_VALUE_OPTIONS:
            if index + 1 >= len(arguments):
                raise ScanFailure("INCOMPLETE_GH_OPTION")
            index += 2
            continue
        if (
            value.startswith("--repo=")
            or value.startswith("--hostname=")
            or value.startswith("--config=")
        ):
            index += 1
            continue
        if value.startswith("-R") and value != "-R":
            index += 1
            continue
        if value in _GH_FLAG_OPTIONS:
            index += 1
            continue
        if value.startswith("-"):
            if _contains_pr_create(arguments[index + 1 :]):
                raise ScanFailure("UNKNOWN_GH_OPTION")
            return False
        break
    return (
        index + 1 < len(arguments)
        and not arguments[index].dynamic
        and not arguments[index + 1].dynamic
        and arguments[index].text == "pr"
        and arguments[index + 1].text == "create"
    )


def _contains_pr_create(words: list[_Word]) -> bool:
    return any(
        not left.dynamic
        and not right.dynamic
        and left.text == "pr"
        and right.text == "create"
        for left, right in zip(words, words[1:])
    )


def _has_remaining_gh(words: list[_Word], index: int) -> bool:
    return any(
        not word.dynamic and not word.quoted and _basename(word.text) == "gh"
        for word in words[index:]
    )


def _is_assignment(word: _Word) -> bool:
    return word.assignment


def _basename(value: str) -> str:
    return value.rsplit("/", 1)[-1]


def _has_unquoted_candidate_hint(command: str) -> bool:
    try:
        parser = _Parser(command, _Budget(limited=False), tolerant=True)
        context = parser.parse(0)
        return _context_has_hint(context)
    except (RecursionError, ScanFailure):
        return False


def _context_has_hint(context: _Context) -> bool:
    for token in context.tokens:
        if token.kind == "NESTED" and token.nested is not None:
            if _context_has_hint(token.nested):
                return True
        if token.kind == "WORD" and token.word is not None and token.role != "HEREDOC":
            if any(_context_has_hint(nested) for nested in token.word.nested):
                return True

    segment: list[_Token] = []
    for token in context.tokens + [_Token("OP", -1, -1, ";")]:
        if token.kind in {"OP", "NESTED"}:
            if _simple_command_has_hint(segment, context.depth):
                return True
            segment = []
        else:
            segment.append(token)
    return False


def _simple_command_has_hint(tokens: list[_Token], depth: int) -> bool:
    words = [
        token.word
        for token in tokens
        if token.kind == "WORD" and token.role == "NORMAL" and token.word is not None
    ]
    index = 0
    while index < len(words) and _is_assignment(words[index]):
        index += 1
    unknown_wrapper = False
    while index < len(words):
        word = words[index]
        if not word.quoted and not word.dynamic and _basename(word.text) == "gh":
            return True
        name = "" if word.dynamic else _basename(word.text)
        if name == "command":
            index += 1
            if index < len(words) and words[index].text == "--":
                index += 1
            elif index < len(words) and words[index].text.startswith("-"):
                unknown_wrapper = True
                index += 1
            continue
        if name == "env":
            index += 1
            while index < len(words):
                value = words[index].text
                if _is_assignment(words[index]) or value in _ENV_FLAGS:
                    index += 1
                elif value in _ENV_VALUE_OPTIONS:
                    index += 2
                elif value.startswith("--unset=") or value.startswith("--chdir="):
                    index += 1
                elif value.startswith("-"):
                    unknown_wrapper = True
                    index += 1
                else:
                    break
            continue
        if name in _SHELLS and index + 2 < len(words) and words[index + 1].text == "-c":
            script = words[index + 2]
            if not script.dynamic and not script.nested:
                if _has_unquoted_candidate_hint(script.text):
                    return True
        if unknown_wrapper:
            index += 1
            continue
        return False
    return False
