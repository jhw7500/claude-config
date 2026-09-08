"""Bounded, non-executing recognition of direct ``gh pr create`` commands."""

from dataclasses import dataclass, field
from enum import Enum
import re


MAX_COMMAND_BYTES = 256 * 1024
MAX_TOKENS = 4096
MAX_RECURSION = 16
TARGET_ENV_NAMES = frozenset(
    {
        "GH_REPO",
        "GH_HOST",
        "GH_CONFIG_DIR",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
    }
)
_ENV_ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
_SAFE_GIT_ENV_NAMES = frozenset(
    {
        "GIT_CURL_VERBOSE",
        "GIT_FLUSH",
        "GIT_OPTIONAL_LOCKS",
        "GIT_PAGER",
        "GIT_TERMINAL_PROMPT",
        "GIT_TRACE",
        "GIT_TRACE_PACKET",
        "GIT_TRACE_PERFORMANCE",
        "GIT_TRACE_SETUP",
    }
)
_TRUSTED_GH_EXECUTABLES = frozenset({"gh", "/usr/bin/gh"})

_ALWAYS_AMBIGUOUS_FAILURES = {
    "ANSI_C_QUOTE",
    "BASE_OPTION",
    "DYNAMIC_PR_ARGUMENT",
    "DYNAMIC_SHELL_SCRIPT",
    "ENV_SPLIT_STRING",
    "EXECUTABLE_UNTRUSTED",
    "PR_CREATE_ALIAS",
    "TARGET_BINDING",
    "TARGET_OVERRIDE",
    "UNSAFE_PR_CONTEXT",
}


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
    ansi_c: bool = False
    assignment: bool = False
    shell_expansion: bool = False
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
_PR_CONTENT_LONG_VALUE_OPTIONS = {
    "--assignee",
    "--body",
    "--body-file",
    "--label",
    "--milestone",
    "--project",
    "--recover",
    "--reviewer",
    "--template",
    "--title",
}
_PR_CONTENT_SHORT_VALUE_OPTIONS = set("abFlmprTt")
_PR_BOOLEAN_SHORT_OPTIONS = set("defw")
_ANSI_C_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
}
_ENV_SPLIT_LONG_OPTION = "--split-string"
_ENV_LONG_OPTIONS = frozenset(
    {
        "--block-signal",
        "--chdir",
        "--debug",
        "--default-signal",
        "--help",
        "--ignore-environment",
        "--ignore-signal",
        "--list-signal-handling",
        "--null",
        _ENV_SPLIT_LONG_OPTION,
        "--unset",
        "--version",
    }
)
_ENV_LONG_VALUE_OPTIONS = {"--chdir", "--unset"}
_ENV_SHORT_FLAGS = frozenset("iv0")
_SHELLS = {"sh", "bash", "dash"}
_SHELL_LONG_VALUE_OPTIONS = {"--init-file", "--rcfile"}
_SHELL_CONTROL_PREFIXES = {
    "!",
    "{",
    "do",
    "elif",
    "else",
    "for",
    "if",
    "select",
    "then",
    "until",
    "while",
}
_TIME_FLAGS = {
    "-a",
    "--append",
    "-p",
    "--portability",
    "-q",
    "--quiet",
    "-v",
    "--verbose",
}
_TIME_VALUE_OPTIONS = {"-f", "--format", "-o", "--output"}
_SHELL_OPERATORS = ("&&", "||", ";", "|", "&")
_SHELL_REDIRECTIONS = (
    "&>>",
    "&>",
    "<<<",
    "<<-",
    "<<",
    ">>",
    "<&",
    ">&",
    "<>",
    ">|",
    "<",
    ">",
)


def scan_pr_create(command: str, *, expected_base: str | None = None) -> ScanResult:
    if not isinstance(command, str) or "\x00" in command:
        return ScanResult(ScanKind.NO_MATCH)
    if expected_base is not None and (
        not isinstance(expected_base, str) or not expected_base
    ):
        return ScanResult(ScanKind.AMBIGUOUS_CANDIDATE, "TARGET_BINDING")
    if len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND_BYTES:
        candidate_hint = _has_unquoted_candidate_hint(command)
        kind = ScanKind.AMBIGUOUS_CANDIDATE if candidate_hint else ScanKind.NO_MATCH
        return ScanResult(kind, "COMMAND_LIMIT")
    try:
        return _scan_context(command, depth=0, expected_base=expected_base)
    except (RecursionError, ScanFailure) as error:
        code = "RECURSION_LIMIT" if isinstance(error, RecursionError) else error.code
        if code in _ALWAYS_AMBIGUOUS_FAILURES:
            return ScanResult(ScanKind.AMBIGUOUS_CANDIDATE, code)
        candidate_hint = _has_unquoted_candidate_hint(command)
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
                if self._peek(1) == "(":
                    start = self.index
                    self.index += 2
                    word = _Word("arithmetic", dynamic=True)
                    word.nested.extend(self._parse_arithmetic(depth))
                    self._add(
                        context,
                        _Token("WORD", start, self.index, word=word),
                    )
                    continue
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
            redirection = self._redirection()
            if redirection is not None:
                self._parse_redirection(
                    context, heredocs, redirection, depth, terminator
                )
                continue
            operator = self._operator()
            if operator is not None:
                start = self.index
                self.index += len(operator)
                self._add(context, _Token("OP", start, self.index, operator))
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
            if char == "$" and self._peek(1) == "'":
                if not word.assignment:
                    assignment_prefix_valid = False
                word.quoted = True
                word.ansi_c = True
                self._parse_ansi_c_quoted(word, pieces)
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
            if char == "$" and self.command.startswith("$((", self.index):
                word.dynamic = True
                self.index += 3
                word.nested.extend(self._parse_arithmetic(depth))
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
            if char in "*?[{}":
                word.shell_expansion = True
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

    def _parse_ansi_c_quoted(self, word: _Word, pieces: list[str]) -> None:
        self.index += 2
        while self.index < self.length:
            char = self.command[self.index]
            if char == "'":
                self.index += 1
                return
            if char == "\\":
                word.dynamic = True
                self.index += 1
                if self.index >= self.length:
                    break
                escape = self.command[self.index]
                if escape in _ANSI_C_SIMPLE_ESCAPES:
                    pieces.append(_ANSI_C_SIMPLE_ESCAPES[escape])
                    self.index += 1
                    continue
                if escape == "\n":
                    self.index += 1
                    continue
                if escape in {"x", "u", "U"}:
                    limit = {"x": 2, "u": 4, "U": 8}[escape]
                    start = self.index + 1
                    end = start
                    while (
                        end < self.length
                        and end - start < limit
                        and self.command[end] in "0123456789abcdefABCDEF"
                    ):
                        end += 1
                    if end == start:
                        pieces.extend(("\\", escape))
                        self.index += 1
                        continue
                    try:
                        pieces.append(chr(int(self.command[start:end], 16)))
                    except ValueError:
                        pieces.append("\ufffd")
                    self.index = end
                    continue
                if escape in "01234567":
                    start = self.index
                    end = start
                    while (
                        end < self.length
                        and end - start < 3
                        and self.command[end] in "01234567"
                    ):
                        end += 1
                    pieces.append(chr(int(self.command[start:end], 8)))
                    self.index = end
                    continue
                if escape == "c" and self.index + 1 < self.length:
                    controlled = self.command[self.index + 1]
                    pieces.append(chr(ord(controlled.upper()) & 0x1F))
                    self.index += 2
                    continue
                pieces.extend(("\\", escape))
                self.index += 1
                continue
            pieces.append(char)
            self.index += 1
        if not self.tolerant:
            raise ScanFailure("PARSE_ERROR")

    def _parse_double_quoted(self, word: _Word, pieces: list[str], depth: int) -> None:
        while self.index < self.length:
            char = self.command[self.index]
            if char == '"':
                self.index += 1
                return
            if char == "\\":
                following = self._peek(1)
                if following in {"$", "`", '"', "\\"}:
                    pieces.append(following)
                    self.index += 2
                    continue
                if following == "\n":
                    self.index += 2
                    continue
                pieces.append("\\")
                self.index += 1
                continue
            if char == "$" and self.command.startswith("$((", self.index):
                word.dynamic = True
                self.index += 3
                word.nested.extend(self._parse_arithmetic(depth))
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

    def _parse_arithmetic(self, depth: int) -> list[_Context]:
        nested: list[_Context] = []
        parentheses = 2
        while self.index < self.length:
            char = self.command[self.index]
            if char == "\\":
                self.index += min(2, self.length - self.index)
                continue
            if char == "'":
                closing = self.command.find("'", self.index + 1)
                if closing < 0:
                    self.index = self.length
                    if not self.tolerant:
                        raise ScanFailure("PARSE_ERROR")
                    return nested
                self.index = closing + 1
                continue
            if char == '"':
                self.index += 1
                quoted = _Word("")
                self._parse_double_quoted(quoted, [], depth)
                nested.extend(quoted.nested)
                continue
            if char == "$" and self.command.startswith("$((", self.index):
                parentheses += 2
                self.index += 3
                continue
            if char == "$" and self._peek(1) == "(":
                self.index += 2
                nested.append(self.parse(depth + 1, ")"))
                continue
            if char == "`":
                self.index += 1
                nested.append(self.parse(depth + 1, "`"))
                continue
            if char == "(":
                parentheses += 1
            elif char == ")":
                parentheses -= 1
                if parentheses == 0:
                    self.index += 1
                    return nested
            self.index += 1
        if not self.tolerant:
            raise ScanFailure("PARSE_ERROR")
        return nested

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
        if (
            self.index >= self.length
            or self.command[self.index] == "\n"
            or self.command[self.index] == "#"
        ):
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
        for operator in _SHELL_OPERATORS:
            if self.command.startswith(operator, self.index):
                return operator
        return None

    def _redirection(self) -> str | None:
        for operator in _SHELL_REDIRECTIONS:
            if self.command.startswith(operator, self.index):
                return operator
        return None

    def _peek(self, offset: int) -> str:
        index = self.index + offset
        return "" if index >= self.length else self.command[index]

    def _add(self, context: _Context, token: _Token) -> None:
        self.budget.take()
        context.tokens.append(token)


def _scan_context(
    command: str, depth: int, expected_base: str | None = None
) -> ScanResult:
    budget = _Budget()
    context = _Parser(command, budget).parse(depth)
    if _scan_parsed_context(context, budget, expected_base):
        return ScanResult(ScanKind.PR_CREATE)
    return ScanResult(ScanKind.NO_MATCH)


def _scan_parsed_context(
    context: _Context, budget: _Budget, expected_base: str | None = None
) -> bool:
    has_nested_execution = False
    for token in context.tokens:
        if token.kind == "NESTED" and token.nested is not None:
            has_nested_execution = True
            if _scan_parsed_context(token.nested, budget, expected_base):
                raise ScanFailure("UNSAFE_PR_CONTEXT")
        if token.kind == "WORD" and token.word is not None and token.role != "HEREDOC":
            if token.word.nested:
                has_nested_execution = True
            for nested in token.word.nested:
                if _scan_parsed_context(nested, budget, expected_base):
                    raise ScanFailure("UNSAFE_PR_CONTEXT")

    segments: list[list[_Token]] = []
    segment: list[_Token] = []
    for token in context.tokens + [_Token("OP", -1, -1, ";")]:
        if token.kind in {"OP", "NESTED"}:
            if segment:
                segments.append(segment)
            segment = []
        else:
            segment.append(token)

    candidate: list[_Token] | None = None
    for segment in segments:
        if _scan_simple_command(segment, context.depth, budget, expected_base):
            if candidate is not None:
                raise ScanFailure("UNSAFE_PR_CONTEXT")
            candidate = segment
    if candidate is not None:
        if (
            has_nested_execution
            or len(segments) != 1
            or any(token.kind == "REDIR" for token in candidate)
            or any(
                token.kind == "WORD"
                and token.word is not None
                and bool(token.word.nested)
                for token in candidate
            )
        ):
            raise ScanFailure("UNSAFE_PR_CONTEXT")
        return True
    return False


def _scan_simple_command(
    tokens: list[_Token],
    depth: int,
    budget: _Budget,
    expected_base: str | None = None,
) -> bool:
    words = [
        token.word
        for token in tokens
        if token.kind == "WORD" and token.role == "NORMAL" and token.word is not None
    ]
    if not words:
        return False

    index = 0
    target_assignment = False
    coproc_context = False
    while index < len(words) and _is_assignment(words[index]):
        assignment = words[index]
        assignment_targets_repository = _is_target_assignment(assignment)
        if expected_base is not None and (
            assignment.dynamic or assignment.shell_expansion
        ) and _could_contain_gh_pr_create(words[index + 1 :]):
            if assignment_targets_repository:
                code = "TARGET_OVERRIDE"
            else:
                code = (
                    "ANSI_C_QUOTE" if assignment.ansi_c else "DYNAMIC_PR_ARGUMENT"
                )
            raise ScanFailure(code)
        target_assignment = target_assignment or assignment_targets_repository
        index += 1
    while index < len(words):
        executable = words[index]
        if executable.dynamic or executable.shell_expansion:
            if _could_form_pr_create(words[index + 1 :]) or _could_contain_gh_pr_create(
                words[index + 1 :]
            ):
                code = "ANSI_C_QUOTE" if executable.ansi_c else "DYNAMIC_PR_ARGUMENT"
                raise ScanFailure(code)
            return False
        name = _basename(executable.text)
        if not executable.quoted and name in _SHELL_CONTROL_PREFIXES:
            index += 1
            continue
        if not executable.quoted and name == "coproc":
            coproc_context = True
            index += 1
            if (
                index + 1 < len(words)
                and not words[index].quoted
                and not words[index].dynamic
                and not words[index].shell_expansion
                and not words[index + 1].quoted
                and words[index + 1].text == "{"
            ):
                index += 1
            continue
        if name == "exec":
            index = _skip_exec(words, index + 1)
            continue
        if name == "time":
            index = _skip_time(words, index + 1)
            continue
        if name == "command":
            index = _skip_command(words, index + 1)
            continue
        if name == "env":
            index = _skip_env(
                words,
                index + 1,
                depth=depth,
                budget=budget,
                expected_base=expected_base,
                enforce_target_binding=expected_base is not None,
            )
            continue
        break

    if index >= len(words):
        return False
    executable = words[index]
    arguments = words[index + 1 :]
    if executable.dynamic or executable.shell_expansion:
        if _could_form_pr_create(arguments) or _could_contain_gh_pr_create(arguments):
            code = "ANSI_C_QUOTE" if executable.ansi_c else "DYNAMIC_PR_ARGUMENT"
            raise ScanFailure(code)
        return False
    name = _basename(executable.text)
    if name == "gh":
        matched = _scan_gh(arguments, expected_base=expected_base)
        if (
            matched
            and expected_base is not None
            and executable.text not in _TRUSTED_GH_EXECUTABLES
        ):
            raise ScanFailure("EXECUTABLE_UNTRUSTED")
        if matched and coproc_context:
            raise ScanFailure("UNSAFE_PR_CONTEXT")
        if matched and expected_base is not None and target_assignment:
            raise ScanFailure("TARGET_OVERRIDE")
        return matched
    if name in _SHELLS:
        script = _shell_command_string(arguments)
        if script is None:
            return False
        if script.dynamic:
            if script.ansi_c and _has_unquoted_candidate_hint(script.text):
                raise ScanFailure("ANSI_C_QUOTE")
            if not script.ansi_c and _has_unquoted_candidate_hint(script.text):
                raise ScanFailure("DYNAMIC_SHELL_SCRIPT")
            return False
        if not script.dynamic and not script.nested:
            parser = _Parser(script.text, budget)
            nested = parser.parse(depth + 1)
            matched = _scan_parsed_context(nested, budget, expected_base)
            if matched and coproc_context:
                raise ScanFailure("UNSAFE_PR_CONTEXT")
            if matched and expected_base is not None:
                raise ScanFailure("UNSAFE_PR_CONTEXT")
            if matched and expected_base is not None and target_assignment:
                raise ScanFailure("TARGET_OVERRIDE")
            return matched
    return False


def _shell_command_string(arguments: list[_Word]) -> _Word | None:
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word.dynamic or word.shell_expansion:
            if any(
                _has_unquoted_candidate_hint(candidate.text)
                for candidate in arguments[index + 1 :]
            ):
                raise ScanFailure("DYNAMIC_SHELL_SCRIPT")
            return None
        action = _shell_option_action(word.text)
        if action == "value":
            if index + 1 >= len(arguments):
                return None
            index += 2
            continue
        if action == "option":
            index += 1
            continue
        if action == "end":
            return None
        if action == "script":
            if index + 1 >= len(arguments):
                raise ScanFailure("DYNAMIC_SHELL_SCRIPT")
            return arguments[index + 1]
    return None


def _shell_option_action(value: str) -> str:
    if value in _SHELL_LONG_VALUE_OPTIONS:
        return "value"
    if any(value.startswith(option + "=") for option in _SHELL_LONG_VALUE_OPTIONS):
        return "option"
    if value == "--" or (not value.startswith(("-", "+"))) or value in {"-", "+"}:
        return "end"
    if value.startswith("--"):
        return "option"
    options = value[1:]
    if "c" in options:
        return "script"
    if "O" in options or "o" in options:
        return "value"
    return "option"


def _skip_command(words: list[_Word], index: int) -> int:
    while index < len(words):
        word = words[index]
        if word.dynamic or word.shell_expansion:
            if _could_form_pr_create(
                words[index + 1 :]
            ) or _could_contain_gh_pr_create(words[index + 1 :]):
                code = "ANSI_C_QUOTE" if word.ansi_c else "DYNAMIC_PR_ARGUMENT"
                raise ScanFailure(code)
            return len(words)
        value = word.text
        if value == "--":
            return index + 1
        if value == "-p":
            index += 1
            continue
        if value.startswith("-"):
            if _has_remaining_gh(words, index + 1):
                raise ScanFailure("UNKNOWN_COMMAND_OPTION")
            return len(words)
        return index
    return index


def _skip_exec(words: list[_Word], index: int) -> int:
    while index < len(words):
        word = words[index]
        if word.dynamic or word.shell_expansion:
            if _could_form_pr_create(
                words[index + 1 :]
            ) or _could_contain_gh_pr_create(words[index + 1 :]):
                code = "ANSI_C_QUOTE" if word.ansi_c else "DYNAMIC_PR_ARGUMENT"
                raise ScanFailure(code)
            return len(words)
        value = word.text
        if value == "--":
            return index + 1
        if not value.startswith("-") or value == "-":
            return index
        short = value[1:]
        alternate_name = short.find("a")
        flag_prefix = short if alternate_name < 0 else short[:alternate_name]
        if any(flag not in "cl" for flag in flag_prefix):
            if _could_contain_gh_pr_create(words[index + 1 :]):
                raise ScanFailure("UNKNOWN_EXEC_OPTION")
            return len(words)
        if alternate_name < 0:
            index += 1
            continue
        if alternate_name == len(short) - 1:
            if index + 1 >= len(words):
                raise ScanFailure("INCOMPLETE_EXEC_OPTION")
            index += 2
        else:
            index += 1
    return index


def _skip_time(words: list[_Word], index: int) -> int:
    while index < len(words):
        word = words[index]
        if word.dynamic or word.shell_expansion:
            if _could_form_pr_create(
                words[index + 1 :]
            ) or _could_contain_gh_pr_create(words[index + 1 :]):
                code = "ANSI_C_QUOTE" if word.ansi_c else "DYNAMIC_PR_ARGUMENT"
                raise ScanFailure(code)
            return len(words)
        value = word.text
        if value in _TIME_FLAGS:
            index += 1
            continue
        if value in _TIME_VALUE_OPTIONS:
            if index + 1 >= len(words):
                raise ScanFailure("INCOMPLETE_TIME_OPTION")
            if value in {"-o", "--output"} and _could_contain_gh_pr_create(
                words[index + 2 :]
            ):
                raise ScanFailure("UNSAFE_PR_CONTEXT")
            index += 2
            continue
        if (
            value.startswith("--format=")
            or value.startswith("--output=")
            or (len(value) > 2 and value[:2] in {"-f", "-o"})
        ):
            if (
                value.startswith("--output=") or value.startswith("-o")
            ) and _could_contain_gh_pr_create(words[index + 1 :]):
                raise ScanFailure("UNSAFE_PR_CONTEXT")
            index += 1
            continue
        if value == "--":
            return index + 1
        if value.startswith("-"):
            if _could_contain_gh_pr_create(words[index + 1 :]):
                raise ScanFailure("UNKNOWN_TIME_OPTION")
            return len(words)
        return index
    return index


def _skip_env(
    words: list[_Word],
    index: int,
    *,
    depth: int,
    budget: _Budget,
    expected_base: str | None = None,
    enforce_target_binding: bool = False,
) -> int:
    while index < len(words):
        word = words[index]
        value = word.text
        assignment_name = _env_assignment_name(word)
        if assignment_name is not None:
            if enforce_target_binding and (
                word.dynamic or word.shell_expansion
            ) and _could_contain_gh_pr_create(words[index + 1 :]):
                if is_target_environment_name(assignment_name):
                    code = "TARGET_OVERRIDE"
                else:
                    code = "ANSI_C_QUOTE" if word.ansi_c else "DYNAMIC_PR_ARGUMENT"
                raise ScanFailure(code)
            if (
                enforce_target_binding
                and is_target_environment_name(assignment_name)
                and _could_contain_gh_pr_create(words[index + 1 :])
            ):
                raise ScanFailure("TARGET_OVERRIDE")
            index += 1
            continue
        if value == "--":
            return index + 1
        long_option = _env_long_option(value)
        if long_option is not None:
            separator = value.find("=")
            has_attached_value = separator >= 0
            attached = value[separator + 1 :] if has_attached_value else ""
            if long_option == _ENV_SPLIT_LONG_OPTION:
                if word.dynamic or word.shell_expansion:
                    raise ScanFailure("ENV_SPLIT_STRING")
                if has_attached_value:
                    split_word = _Word(
                        attached,
                        quoted=word.quoted,
                        dynamic=word.dynamic,
                        ansi_c=word.ansi_c,
                        shell_expansion=word.shell_expansion,
                        nested=list(word.nested),
                    )
                    suffix = words[index + 1 :]
                else:
                    if index + 1 >= len(words):
                        raise ScanFailure("INCOMPLETE_ENV_OPTION")
                    split_word = words[index + 1]
                    suffix = words[index + 2 :]
                _scan_env_split_string(
                    split_word,
                    suffix,
                    depth,
                    budget,
                    expected_base,
                )
                return len(words)
            if long_option in _ENV_LONG_VALUE_OPTIONS:
                if has_attached_value:
                    next_index = index + 1
                else:
                    if index + 1 >= len(words):
                        raise ScanFailure("INCOMPLETE_ENV_OPTION")
                    next_index = index + 2
                if long_option == "--chdir" and _could_contain_gh_pr_create(
                    words[next_index:]
                ):
                    raise ScanFailure("UNSAFE_PR_CONTEXT")
                index = next_index
                continue
            index += 1
            continue
        short_option = _env_short_option(value)
        if short_option is not None:
            option, attached = short_option
            if option == "flag":
                index += 1
                continue
            if attached:
                next_index = index + 1
                split_word = _Word(
                    attached,
                    quoted=word.quoted,
                    dynamic=word.dynamic,
                    ansi_c=word.ansi_c,
                    shell_expansion=word.shell_expansion,
                    nested=list(word.nested),
                )
            else:
                if index + 1 >= len(words):
                    raise ScanFailure("INCOMPLETE_ENV_OPTION")
                next_index = index + 2
                split_word = words[index + 1]
            if option == "S":
                if word.dynamic or word.shell_expansion:
                    raise ScanFailure("ENV_SPLIT_STRING")
                _scan_env_split_string(
                    split_word,
                    words[next_index:],
                    depth,
                    budget,
                    expected_base,
                )
                return len(words)
            if option == "C" and _could_contain_gh_pr_create(words[next_index:]):
                raise ScanFailure("UNSAFE_PR_CONTEXT")
            index = next_index
            continue
        if word.dynamic or word.shell_expansion:
            raise ScanFailure("DYNAMIC_PR_ARGUMENT")
        if value == "-":
            index += 1
            continue
        if value.startswith("-"):
            if _has_remaining_gh(words, index + 1):
                raise ScanFailure("UNKNOWN_ENV_OPTION")
            return len(words)
        return index
    return index


def _scan_env_split_string(
    word: _Word,
    suffix: list[_Word],
    depth: int,
    budget: _Budget,
    expected_base: str | None,
) -> None:
    if word.dynamic or word.shell_expansion:
        raise ScanFailure("ENV_SPLIT_STRING")
    if "$" in word.text or "`" in word.text or "\\c" in word.text:
        raise ScanFailure("ENV_SPLIT_STRING")
    try:
        candidate_text = word.text.replace("\\_", " ")
        parser = _Parser(candidate_text, budget)
        nested = parser.parse(depth + 1)
        if all(
            token.kind == "WORD" and token.role == "NORMAL" and token.word is not None
            for token in nested.tokens
        ):
            nested = _Context(
                nested.depth,
                [_Token("WORD", -1, -1, word=_Word("env"))]
                + nested.tokens
                + [_Token("WORD", -1, -1, word=suffix_word) for suffix_word in suffix],
            )
        if not _scan_parsed_context(nested, budget, expected_base):
            return
    except (RecursionError, ScanFailure) as error:
        if isinstance(error, ScanFailure) and error.code == "ENV_SPLIT_STRING":
            raise
        raise ScanFailure("ENV_SPLIT_STRING")
    raise ScanFailure("ENV_SPLIT_STRING")


def _env_long_option(value: str) -> str | None:
    name = value.split("=", 1)[0]
    if name in _ENV_LONG_OPTIONS:
        return name
    if len(name) <= 2:
        return None
    matches = [option for option in _ENV_LONG_OPTIONS if option.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def _env_short_option(value: str) -> tuple[str, str] | None:
    if not value.startswith("-") or value.startswith("--"):
        return None
    if value == "-":
        return "flag", ""
    for index, option in enumerate(value[1:], start=1):
        if option in {"u", "C", "S"}:
            return option, value[index + 1 :]
        if option not in _ENV_SHORT_FLAGS:
            return None
    return "flag", ""


def _scan_gh(arguments: list[_Word], expected_base: str | None = None) -> bool:
    index = 0
    target_override = False
    while index < len(arguments):
        word = arguments[index]
        if word.dynamic or word.shell_expansion:
            if _could_contain_pr_create(arguments[index:]):
                code = "ANSI_C_QUOTE" if word.ansi_c else "DYNAMIC_PR_ARGUMENT"
                raise ScanFailure(code)
            return False
        option = _gh_global_option(arguments, index)
        if option is not None:
            index, overrides_target = option
            target_override = target_override or overrides_target
            continue
        value = word.text
        if value.startswith("-"):
            if _contains_pr_create(arguments[index + 1 :]):
                raise ScanFailure("UNKNOWN_GH_OPTION")
            return False
        break
    if index >= len(arguments) or not _could_equal(arguments[index], "pr"):
        return False
    if arguments[index].dynamic or arguments[index].shell_expansion:
        code = "ANSI_C_QUOTE" if arguments[index].ansi_c else "DYNAMIC_PR_ARGUMENT"
        raise ScanFailure(code)
    index += 1

    while index < len(arguments):
        word = arguments[index]
        if word.dynamic or word.shell_expansion:
            if any(
                _could_equal(candidate, "create") for candidate in arguments[index:]
            ):
                code = "ANSI_C_QUOTE" if word.ansi_c else "DYNAMIC_PR_ARGUMENT"
                raise ScanFailure(code)
            return False
        option = _gh_global_option(arguments, index)
        if option is not None:
            index, overrides_target = option
            target_override = target_override or overrides_target
            continue
        if word.text.startswith("-"):
            if any(
                _could_equal(candidate, "create")
                for candidate in arguments[index + 1 :]
            ):
                raise ScanFailure("UNKNOWN_GH_OPTION")
            return False
        break

    if (
        index < len(arguments)
        and not arguments[index].dynamic
        and not arguments[index].shell_expansion
        and arguments[index].text == "new"
    ):
        raise ScanFailure("PR_CREATE_ALIAS")
    if index >= len(arguments) or not _could_equal(arguments[index], "create"):
        return False
    if arguments[index].dynamic or arguments[index].shell_expansion:
        code = "ANSI_C_QUOTE" if arguments[index].ansi_c else "DYNAMIC_PR_ARGUMENT"
        raise ScanFailure(code)
    if expected_base is not None:
        if target_override:
            raise ScanFailure("TARGET_OVERRIDE")
        _validate_pr_create_target(arguments[index + 1 :], expected_base)
    return True


def _gh_global_option(arguments: list[_Word], index: int) -> tuple[int, bool] | None:
    value = arguments[index].text
    if value in _GH_VALUE_OPTIONS:
        if index + 1 >= len(arguments):
            raise ScanFailure("INCOMPLETE_GH_OPTION")
        return index + 2, True
    if (
        value.startswith("--repo=")
        or value.startswith("--hostname=")
        or value.startswith("--config=")
    ):
        return index + 1, True
    if value.startswith("-R") and value != "-R":
        return index + 1, True
    if value in _GH_FLAG_OPTIONS:
        return index + 1, False
    return None


def _validate_pr_create_target(arguments: list[_Word], expected_base: str) -> None:
    bases: list[str] = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        value = word.text

        if word.shell_expansion:
            raise ScanFailure("DYNAMIC_PR_ARGUMENT")

        if value in {"--repo", "-R", "--hostname", "--config"}:
            raise ScanFailure("TARGET_OVERRIDE")
        if (
            value.startswith("--repo=")
            or value.startswith("--hostname=")
            or value.startswith("--config=")
        ):
            raise ScanFailure("TARGET_OVERRIDE")
        if value in {"--head", "-H"} or value.startswith("--head="):
            raise ScanFailure("TARGET_OVERRIDE")
        if value == "--base":
            base, index = _required_static_value(arguments, index, "BASE_OPTION")
            bases.append(base)
            continue
        if value.startswith("--base="):
            if word.dynamic:
                raise ScanFailure("TARGET_BINDING")
            bases.append(value.split("=", 1)[1])
            index += 1
            continue
        if value in _PR_CONTENT_LONG_VALUE_OPTIONS:
            _, index = _required_content_value(arguments, index)
            continue
        if any(
            value.startswith(option + "=") for option in _PR_CONTENT_LONG_VALUE_OPTIONS
        ):
            if word.dynamic:
                raise ScanFailure("DYNAMIC_PR_ARGUMENT")
            index += 1
            continue
        if value.startswith("--"):
            if word.dynamic:
                raise ScanFailure("DYNAMIC_PR_ARGUMENT")
            index += 1
            continue
        if value.startswith("-") and value != "-":
            index = _scan_pr_short_options(arguments, index, bases)
            continue
        if word.dynamic:
            raise ScanFailure("DYNAMIC_PR_ARGUMENT")
        index += 1

    if bases != [expected_base]:
        raise ScanFailure("TARGET_BINDING")


def _required_static_value(
    arguments: list[_Word], index: int, code: str
) -> tuple[str, int]:
    if index + 1 >= len(arguments):
        raise ScanFailure("INCOMPLETE_GH_OPTION")
    value = arguments[index + 1]
    if value.dynamic or value.shell_expansion:
        raise ScanFailure(code)
    return value.text, index + 2


def _required_content_value(arguments: list[_Word], index: int) -> tuple[str, int]:
    if index + 1 >= len(arguments):
        raise ScanFailure("INCOMPLETE_GH_OPTION")
    value = arguments[index + 1]
    if value.dynamic or value.shell_expansion:
        raise ScanFailure("DYNAMIC_PR_ARGUMENT")
    return value.text, index + 2


def _scan_pr_short_options(arguments: list[_Word], index: int, bases: list[str]) -> int:
    word = arguments[index]
    if word.shell_expansion:
        raise ScanFailure("DYNAMIC_PR_ARGUMENT")
    options = word.text[1:]
    position = 0
    while position < len(options):
        option = options[position]
        remainder = options[position + 1 :]
        if option in {"R", "H"}:
            raise ScanFailure("TARGET_OVERRIDE")
        if option == "B":
            if remainder:
                if word.dynamic:
                    raise ScanFailure("TARGET_BINDING")
                bases.append(remainder)
                return index + 1
            base, next_index = _required_static_value(arguments, index, "BASE_OPTION")
            bases.append(base)
            return next_index
        if option in _PR_CONTENT_SHORT_VALUE_OPTIONS:
            if remainder:
                if word.dynamic:
                    raise ScanFailure("DYNAMIC_PR_ARGUMENT")
                return index + 1
            _, next_index = _required_content_value(arguments, index)
            return next_index
        if option in _PR_BOOLEAN_SHORT_OPTIONS:
            position += 1
            continue
        if word.dynamic:
            raise ScanFailure("DYNAMIC_PR_ARGUMENT")
        return index + 1
    return index + 1


def _could_form_pr_create(words: list[_Word]) -> bool:
    return (
        len(words) >= 2
        and _could_equal(words[0], "pr")
        and _could_equal(words[1], "create")
    )


def _could_equal(word: _Word, value: str) -> bool:
    return (
        word.shell_expansion
        or word.ansi_c
        and word.dynamic
        or not word.dynamic
        and word.text == value
    )


def _could_contain_pr_create(words: list[_Word]) -> bool:
    return any(_could_form_pr_create(words[index:]) for index in range(len(words) - 1))


def _could_contain_gh_pr_create(words: list[_Word]) -> bool:
    return any(
        not word.dynamic
        and _basename(word.text) == "gh"
        and _scan_gh(words[index + 1 :])
        for index, word in enumerate(words)
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
        not word.dynamic and not word.shell_expansion and _basename(word.text) == "gh"
        for word in words[index:]
    )


def _is_assignment(word: _Word) -> bool:
    return word.assignment


def _is_target_assignment(word: _Word) -> bool:
    return _is_assignment(word) and is_target_environment_name(
        word.text.split("=", 1)[0]
    )


def _env_assignment_name(word: _Word) -> str | None:
    match = _ENV_ASSIGNMENT.match(word.text)
    return None if match is None else match.group(1)


def is_target_environment_name(name: str) -> bool:
    if name == "PATH" or name.startswith(("LD_", "DYLD_")):
        return True
    if name.startswith("GIT_"):
        return name not in _SAFE_GIT_ENV_NAMES
    return name in TARGET_ENV_NAMES


def _basename(value: str) -> str:
    return value.rsplit("/", 1)[-1]


_HINT_FRAME_LIMIT = MAX_RECURSION + 1
_HINT_CAPTURE_LIMIT = MAX_COMMAND_BYTES


@dataclass
class _HintFrame:
    kind: str = "COMMAND"
    terminator: str | None = None
    phase: str = "EXEC"
    quote: str | None = None
    chars: list[str] = field(default_factory=list)
    started: bool = False
    quoted: bool = False
    dynamic: bool = False
    assignment: bool = False
    assignment_prefix_valid: bool = True
    truncated: bool = False
    skip_operand: str | None = None
    saved_phase: str = "EXEC"
    heredocs: list[tuple[str, bool]] = field(default_factory=list)
    arithmetic_depth: int = 0

    def reset_word(self) -> None:
        self.chars.clear()
        self.started = False
        self.quoted = False
        self.dynamic = False
        self.assignment = False
        self.assignment_prefix_valid = True
        self.truncated = False


@dataclass
class _HintWork:
    pending: list[str] = field(default_factory=list)
    queued_bytes: int = 0

    def add(self, script: str) -> None:
        encoded = len(script.encode("utf-8", "surrogatepass"))
        if (
            len(self.pending) >= MAX_TOKENS
            or self.queued_bytes + encoded > _HINT_CAPTURE_LIMIT
        ):
            return
        self.pending.append(script)
        self.queued_bytes += encoded

    def pop(self) -> str:
        script = self.pending.pop()
        self.queued_bytes -= len(script.encode("utf-8", "surrogatepass"))
        return script


class _StreamingHint:
    """Find a real unquoted command-position ``gh`` without building an AST."""

    def __init__(self, command: str, work: _HintWork) -> None:
        self.command = command
        self.length = len(command)
        self.index = 0
        self.frames = [_HintFrame()]
        self.overflow_depth = 0
        self.overflow = _HintFrame(terminator=")")
        self.overflow_checkpoints: list[tuple[int, _HintFrame]] = []
        self.work = work

    def run(self) -> bool:
        while self.index < self.length:
            frame = self.overflow if self.overflow_depth else self.frames[-1]
            if frame.kind == "ARITHMETIC":
                if self._scan_arithmetic_char(frame):
                    return True
            elif self._scan_command_char(frame):
                return True
        if self.overflow_depth and self._finish_word(self.overflow):
            return True
        return any(self._finish_word(frame) for frame in self.frames)

    def _scan_command_char(self, frame: _HintFrame) -> bool:
        char = self.command[self.index]
        if frame.quote == "SINGLE":
            if char == "'":
                frame.quote = None
                self.index += 1
            else:
                self._append(frame, char)
                self.index += 1
            return False
        if frame.quote == "DOUBLE":
            if char == '"':
                frame.quote = None
                self.index += 1
                return False
            if char == "\\":
                following = self._peek(1)
                if following in {"$", "`", '"', "\\"}:
                    self._append(frame, following)
                    self.index += 2
                elif following == "\n":
                    self.index += 2
                else:
                    self._append(frame, "\\")
                    self.index += 1
                return False
            if self.command.startswith("$((", self.index):
                frame.dynamic = True
                self.index += 3
                self._push_arithmetic()
                return False
            if self.command.startswith("$(", self.index):
                frame.dynamic = True
                self.index += 2
                self._push_command(")")
                return False
            if char == "`":
                frame.dynamic = True
                self.index += 1
                self._push_command("`")
                return False
            if char == "$":
                frame.dynamic = True
            self._append(frame, char)
            self.index += 1
            return False

        if frame.terminator is not None and char == frame.terminator:
            if self._finish_word(frame):
                return True
            self.index += 1
            self._pop_frame()
            return False
        if char in " \t":
            if self._finish_word(frame):
                return True
            self.index += 1
            return False
        if char == "\\" and self._peek(1) == "\n":
            self.index += 2
            return False
        if char == "#" and not frame.started:
            newline = self.command.find("\n", self.index)
            self.index = self.length if newline < 0 else newline
            return False
        if char == "\n":
            if self._finish_word(frame):
                return True
            self.index += 1
            if frame.heredocs:
                self._consume_heredocs(frame)
            frame.phase = "EXEC"
            frame.skip_operand = None
            return False
        redirection = self._redirection()
        if redirection is not None:
            if self._is_numeric_fd(frame):
                frame.reset_word()
            elif self._finish_word(frame):
                return True
            frame.saved_phase = frame.phase
            frame.skip_operand = "HEREDOC" if redirection in {"<<", "<<-"} else "REDIR"
            if frame.skip_operand == "HEREDOC":
                frame.saved_phase += "\t" if redirection == "<<-" else ""
            self.index += len(redirection)
            return False
        operator = self._operator()
        if operator is not None:
            if self._finish_word(frame):
                return True
            frame.phase = "EXEC"
            frame.skip_operand = None
            self.index += len(operator)
            return False
        if self.command.startswith("$((", self.index):
            frame.started = True
            frame.dynamic = True
            self.index += 3
            self._push_arithmetic()
            return False
        if self.command.startswith("$(", self.index):
            frame.started = True
            frame.dynamic = True
            self.index += 2
            self._push_command(")")
            return False
        if char == "`":
            frame.started = True
            frame.dynamic = True
            self.index += 1
            self._push_command("`")
            return False
        if char == "(" and self._peek(1) == "(" and not frame.started:
            frame.started = True
            frame.dynamic = True
            self.index += 2
            self._push_arithmetic()
            return False
        if char == "(" and not frame.started:
            self.index += 1
            self._push_command(")")
            return False
        if char == ")":
            if self._finish_word(frame):
                return True
            frame.phase = "EXEC"
            self.index += 1
            return False
        if char == "\\":
            frame.started = True
            following = self._peek(1)
            if following:
                if following != "\n":
                    frame.quoted = True
                    self._append(frame, following)
                self.index += 2
            else:
                frame.quoted = True
                self.index += 1
            return False
        if char == "'":
            frame.started = True
            frame.quoted = True
            if not frame.assignment:
                frame.assignment_prefix_valid = False
            frame.quote = "SINGLE"
            self.index += 1
            return False
        if char == '"':
            frame.started = True
            frame.quoted = True
            if not frame.assignment:
                frame.assignment_prefix_valid = False
            frame.quote = "DOUBLE"
            self.index += 1
            return False
        if char == "$":
            frame.dynamic = True
            if not frame.assignment:
                frame.assignment_prefix_valid = False
        if (
            char == "="
            and not frame.assignment
            and frame.assignment_prefix_valid
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", "".join(frame.chars))
        ):
            frame.assignment = True
        frame.started = True
        self._append(frame, char)
        self.index += 1
        return False

    def _scan_arithmetic_char(self, frame: _HintFrame) -> bool:
        char = self.command[self.index]
        if frame.quote is not None:
            if char == frame.quote:
                frame.quote = None
                self.index += 1
                return False
            if char == "\\":
                self.index += min(2, self.length - self.index)
                return False
            if frame.quote == '"' and self.command.startswith("$(", self.index):
                self.index += 2
                self._push_command(")")
                return False
            if frame.quote == '"' and char == "`":
                self.index += 1
                self._push_command("`")
                return False
            self.index += 1
            return False
        if char in {"'", '"'}:
            frame.quote = char
            self.index += 1
            return False
        if char == "\\":
            self.index += min(2, self.length - self.index)
            return False
        if self.command.startswith("$((", self.index):
            frame.arithmetic_depth += 2
            self.index += 3
            return False
        if self.command.startswith("$(", self.index):
            self.index += 2
            self._push_command(")")
            return False
        if char == "`":
            self.index += 1
            self._push_command("`")
            return False
        if char == "(":
            frame.arithmetic_depth += 1
        elif char == ")":
            frame.arithmetic_depth -= 1
            if frame.arithmetic_depth == 0:
                self.index += 1
                self._pop_frame()
                return False
        self.index += 1
        return False

    def _finish_word(self, frame: _HintFrame) -> bool:
        if not frame.started:
            return False
        text = "".join(frame.chars)
        if frame.skip_operand is not None:
            if frame.skip_operand == "HEREDOC" and not frame.truncated:
                strip_tabs = frame.saved_phase.endswith("\t")
                if len(frame.heredocs) < MAX_TOKENS:
                    frame.heredocs.append((text, strip_tabs))
            frame.phase = frame.saved_phase.rstrip("\t")
            frame.skip_operand = None
            frame.reset_word()
            return False

        static = not frame.dynamic and not frame.truncated
        name = _basename(text) if static else ""
        executable_gh = static and name == "gh"
        if frame.phase in {"EXEC", "WRAPPED_EXEC"}:
            if frame.phase == "EXEC" and frame.assignment:
                pass
            elif not static:
                frame.phase = "UNKNOWN_WRAPPER"
            elif self._enter_executable(frame, name, executable_gh):
                return True
        elif frame.phase == "COMMAND":
            if not static:
                frame.phase = "UNKNOWN_WRAPPER"
            elif text == "--":
                frame.phase = "WRAPPED_EXEC"
            elif text == "-p":
                pass
            elif text.startswith("-"):
                frame.phase = "UNKNOWN_WRAPPER"
            elif self._enter_executable(frame, name, executable_gh):
                return True
        elif frame.phase == "ENV":
            short_option = _env_short_option(text) if static else None
            long_option = _env_long_option(text) if static else None
            split_attached: str | None = None
            if (
                short_option is not None
                and short_option[0] == "S"
                and short_option[1]
            ):
                split_attached = short_option[1]
            elif long_option == _ENV_SPLIT_LONG_OPTION and "=" in text:
                split_attached = text.split("=", 1)[1]
            if frame.assignment or (
                static
                and (
                    text == "-"
                    or short_option == ("flag", "")
                    or long_option is not None
                    and long_option
                    not in _ENV_LONG_VALUE_OPTIONS | {_ENV_SPLIT_LONG_OPTION}
                )
            ):
                pass
            elif not static:
                frame.phase = "UNKNOWN_WRAPPER"
            elif text == "--":
                frame.phase = "WRAPPED_EXEC"
            elif short_option is not None and short_option[0] == "S" or (
                long_option == _ENV_SPLIT_LONG_OPTION
            ):
                if split_attached is None:
                    frame.phase = "ENV_SPLIT_VALUE"
                elif self._consume_env_split_hint(frame, split_attached):
                    return True
            elif (
                short_option is not None
                and short_option[0] in {"u", "C"}
                and not short_option[1]
                or long_option in _ENV_LONG_VALUE_OPTIONS and "=" not in text
            ):
                frame.phase = "ENV_VALUE"
            elif short_option is not None or long_option is not None:
                pass
            elif text.startswith("-"):
                frame.phase = "UNKNOWN_WRAPPER"
            elif self._enter_executable(frame, name, executable_gh):
                return True
        elif frame.phase == "ENV_VALUE":
            frame.phase = "ENV"
        elif frame.phase == "ENV_SPLIT_VALUE":
            if not static or self._consume_env_split_hint(frame, text):
                return True
        elif frame.phase == "EXEC_OPTION":
            if not static:
                frame.phase = "UNKNOWN_WRAPPER"
            elif text == "--":
                frame.phase = "WRAPPED_EXEC"
            elif text.startswith("-") and text != "-":
                short = text[1:]
                alternate_name = short.find("a")
                flag_prefix = short if alternate_name < 0 else short[:alternate_name]
                if any(flag not in "cl" for flag in flag_prefix):
                    frame.phase = "UNKNOWN_WRAPPER"
                elif alternate_name == len(short) - 1:
                    frame.phase = "EXEC_VALUE"
            elif self._enter_executable(frame, name, executable_gh):
                return True
        elif frame.phase == "EXEC_VALUE":
            frame.phase = "EXEC_OPTION"
        elif frame.phase == "TIME_OPTION":
            if not static:
                frame.phase = "UNKNOWN_WRAPPER"
            elif text in _TIME_FLAGS:
                pass
            elif text in _TIME_VALUE_OPTIONS:
                frame.phase = "TIME_VALUE"
            elif (
                text.startswith("--format=")
                or text.startswith("--output=")
                or len(text) > 2
                and text[:2] in {"-f", "-o"}
            ):
                pass
            elif text == "--":
                frame.phase = "WRAPPED_EXEC"
            elif text.startswith("-"):
                frame.phase = "UNKNOWN_WRAPPER"
            elif self._enter_executable(frame, name, executable_gh):
                return True
        elif frame.phase == "TIME_VALUE":
            frame.phase = "TIME_OPTION"
        elif frame.phase == "UNKNOWN_WRAPPER":
            if executable_gh:
                return True
        elif frame.phase == "SHELL_OPTION":
            if not static:
                frame.phase = "UNKNOWN_WRAPPER"
            else:
                action = _shell_option_action(text)
                if action == "script":
                    frame.phase = "SHELL_SCRIPT"
                elif action == "value":
                    frame.phase = "SHELL_OPTION_VALUE"
                elif action == "end":
                    frame.phase = "ARGS"
        elif frame.phase == "SHELL_OPTION_VALUE":
            frame.phase = "SHELL_OPTION"
        elif frame.phase == "SHELL_SCRIPT":
            if static:
                self.work.add(text)
            frame.phase = "ARGS"
        frame.reset_word()
        return False

    def _consume_env_split_hint(self, frame: _HintFrame, text: str) -> bool:
        if "\\c" in text:
            return True
        script = text.replace("\\_", " ")
        if script:
            self.work.add(script)
            frame.phase = "ARGS"
        else:
            frame.phase = "ENV"
        return False

    @staticmethod
    def _enter_executable(
        frame: _HintFrame, name: str, executable_gh: bool
    ) -> bool:
        if executable_gh:
            return True
        if name == "command":
            frame.phase = "COMMAND"
        elif name == "env":
            frame.phase = "ENV"
        elif name == "exec":
            frame.phase = "EXEC_OPTION"
        elif name == "time":
            frame.phase = "TIME_OPTION"
        elif name == "coproc":
            frame.phase = "UNKNOWN_WRAPPER"
        elif name in _SHELLS:
            frame.phase = "SHELL_OPTION"
        else:
            frame.phase = "ARGS"
        return False

    def _push_command(self, terminator: str) -> None:
        if self.overflow_depth:
            self._checkpoint_overflow()
            self.overflow_depth += 1
            self.overflow = _HintFrame(terminator=terminator)
        elif len(self.frames) < _HINT_FRAME_LIMIT:
            self.frames.append(_HintFrame(terminator=terminator))
        else:
            self.overflow_depth = 1
            self.overflow = _HintFrame(terminator=terminator)

    def _push_arithmetic(self) -> None:
        frame = _HintFrame(kind="ARITHMETIC", arithmetic_depth=2)
        if self.overflow_depth:
            self._checkpoint_overflow()
            self.overflow_depth += 1
            self.overflow = frame
        elif len(self.frames) < _HINT_FRAME_LIMIT:
            self.frames.append(frame)
        else:
            self.overflow_depth = 1
            self.overflow = frame

    def _pop_frame(self) -> None:
        if self.overflow_depth:
            self.overflow_depth -= 1
            if self.overflow_depth:
                if (
                    self.overflow_checkpoints
                    and self.overflow_checkpoints[-1][0] == self.overflow_depth
                ):
                    _, self.overflow = self.overflow_checkpoints.pop()
                else:
                    self.overflow = _HintFrame(terminator=")", phase="ARGS")
            else:
                parent = self.frames[-1]
                parent.started = True
                parent.dynamic = True
            return
        if len(self.frames) > 1:
            self.frames.pop()
            parent = self.frames[-1]
            if parent.kind == "COMMAND":
                parent.started = True
                parent.dynamic = True

    def _checkpoint_overflow(self) -> None:
        frame = self.overflow
        needs_restore = (
            frame.kind != "COMMAND"
            or frame.terminator != ")"
            or frame.quote is not None
            or frame.phase != "EXEC"
            or frame.assignment
            or frame.skip_operand is not None
            or bool(frame.heredocs)
        )
        if not needs_restore:
            return
        if len(self.overflow_checkpoints) == _HINT_FRAME_LIMIT:
            self.overflow_checkpoints.pop(0)
        self.overflow_checkpoints.append((self.overflow_depth, frame))

    @staticmethod
    def _is_numeric_fd(frame: _HintFrame) -> bool:
        return (
            frame.started
            and not frame.quoted
            and not frame.dynamic
            and not frame.truncated
            and bool(frame.chars)
            and all(char.isascii() and char.isdigit() for char in frame.chars)
        )

    def _consume_heredocs(self, frame: _HintFrame) -> None:
        for delimiter, strip_tabs in frame.heredocs:
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
                    break
                self.index = newline + 1
        frame.heredocs.clear()

    def _append(self, frame: _HintFrame, value: str) -> None:
        if len(frame.chars) < _HINT_CAPTURE_LIMIT:
            frame.chars.append(value)
        else:
            frame.truncated = True

    def _operator(self) -> str | None:
        for operator in _SHELL_OPERATORS:
            if self.command.startswith(operator, self.index):
                return operator
        return None

    def _redirection(self) -> str | None:
        for operator in _SHELL_REDIRECTIONS:
            if self.command.startswith(operator, self.index):
                return operator
        return None

    def _peek(self, offset: int) -> str:
        index = self.index + offset
        return "" if index >= self.length else self.command[index]


def _has_unquoted_candidate_hint(command: str) -> bool:
    work = _HintWork()
    script = command
    while True:
        scanner = _StreamingHint(script, work)
        if scanner.run():
            return True
        if not work.pending:
            break
        script = work.pop()
    return False
