"""Classify `git commit` invocations inside a shell command string.

This scanner only classifies. Anything it cannot determine with certainty is
reported as ``unknown`` and the commit gate lets ``unknown`` pass with a
warning. It never executes the command and never raises.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

MessageSource = Literal["file", "literal", "auto", "unknown"]


@dataclass(frozen=True)
class CommitInvocation:
    directory: Path
    source: MessageSource
    message_path: Optional[Path]
    message_text: Optional[str]
    reason: str


@dataclass(frozen=True)
class ScanResult:
    invocations: tuple[CommitInvocation, ...]


_MAX_SCAN_CHARS = 200_000

_NESTED_COMMIT = re.compile(
    r"\bgit\b(?:\s+(?:-C|-c)\s+\S+|\s+--?[A-Za-z][\w-]*(?:=\S+)?)*\s+commit\b"
)
_HEREDOC_MESSAGE = re.compile(
    r"""(?P<opt>(?<![\w-])(?:-m|--message))[ \t]+"\$\(cat[ \t]+<<-?[ \t]*"""
    r"""(?P<q>['"])(?P<delim>[A-Za-z_]\w*)(?P=q)[ \t]*\n"""
    r"""(?P<body>.*?)^(?P=delim)[ \t]*\n?[ \t]*\)\"""",
    re.DOTALL | re.MULTILINE,
)
_DYNAMIC = re.compile(r"[$`]|<<|\beval\b|\b(?:ba|z|da|k)?sh\s+(?:-\w+\s+)*-\w*c\b")
_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
_SEPARATOR_CHARS = frozenset("&|;\n()")
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time"})
_AUTO_FILES = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")

# commit options that take a separate/attached argument
_SHORT_WITH_ARG = frozenset("mFCct")
_SHORT_OPTIONAL_ARG = frozenset("uS")
_LONG_WITH_ARG = frozenset({
    "file", "message", "template", "reuse-message", "reedit-message", "fixup",
    "squash", "author", "date", "trailer", "pathspec-from-file", "cleanup",
})


def _fallback(command: str, cwd: Path) -> ScanResult:
    if _NESTED_COMMIT.search(command):
        return ScanResult((CommitInvocation(
            cwd, "unknown", None, None, "command could not be analysed"),))
    return ScanResult(())


def scan_commits(command: str, *, cwd: Path) -> ScanResult:
    try:
        cwd = Path(cwd)
        if not isinstance(command, str):
            return ScanResult(())
        if len(command) > _MAX_SCAN_CHARS:
            return _fallback(command, cwd)
        return _scan(command, cwd)
    except Exception:  # never raise on arbitrary input
        try:
            return _fallback(command if isinstance(command, str) else "", Path(cwd))
        except Exception:
            return ScanResult(())


def _scan(command: str, cwd: Path) -> ScanResult:
    placeholders: dict[str, str] = {}

    def _substitute(match: "re.Match[str]") -> str:
        name = f"__COMMITGATE_HEREDOC_{len(placeholders)}__"
        while name in command:
            name += "_"
        placeholders[name] = match.group("body").rstrip("\n")
        return f"{match.group('opt')} {name}"

    reduced = _HEREDOC_MESSAGE.sub(_substitute, command)
    dynamic = bool(_DYNAMIC.search(reduced))

    lexer = shlex.shlex(reduced, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return _fallback(command, cwd)

    invocations: list[CommitInvocation] = []
    directory: Optional[Path] = cwd
    stack: list[Optional[Path]] = []
    argv: list[str] = []

    def flush() -> None:
        nonlocal directory
        if not argv:
            return
        words = list(argv)
        argv.clear()
        words = _strip_wrappers(words)
        if not words:
            return
        head = os.path.basename(words[0])
        if head == "cd":
            directory = _apply_cd(directory, words[1:], cwd)
        elif head == "git":
            inv = _git_invocation(words, directory, cwd, dynamic, placeholders)
            if inv is not None:
                invocations.append(inv)
        elif any(_NESTED_COMMIT.search(w) for w in words[1:]):
            invocations.append(CommitInvocation(
                directory or cwd, "unknown", None, None,
                "git commit inside a string handed to another command"))

    for token in tokens:
        if token and set(token) <= _SEPARATOR_CHARS:
            flush()
            for ch in token:
                if ch == "(":
                    stack.append(directory)
                elif ch == ")" and stack:
                    directory = stack.pop()
            continue
        argv.append(token)
    flush()
    return ScanResult(tuple(invocations))


def _strip_wrappers(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        word = words[i]
        if _ASSIGNMENT.match(word):
            i += 1
        elif os.path.basename(word) in _WRAPPERS:
            i += 1
            while i < len(words) and (
                words[i].startswith("-") or _ASSIGNMENT.match(words[i])
            ):
                if words[i] in ("-u", "-C", "-S") and i + 1 < len(words):
                    i += 1
                i += 1
        else:
            break
    return words[i:]


def _apply_cd(directory: Optional[Path], args: list[str], cwd: Path) -> Optional[Path]:
    args = [a for a in args if a not in ("-L", "-P", "--")]
    if len(args) != 1 or directory is None:
        return None
    target = args[0]
    if not target or target == "-" or any(c in target for c in "$`*?[{"):
        return None
    if target.startswith("~"):
        target = os.path.expanduser(target)
        if target.startswith("~"):
            return None
    return Path(os.path.normpath(os.path.join(str(directory), target)))


def _git_invocation(
    words: list[str],
    directory: Optional[Path],
    cwd: Path,
    dynamic: bool,
    placeholders: dict[str, str],
) -> Optional[CommitInvocation]:
    base: Optional[Path] = directory
    unresolved = directory is None
    i = 1
    while i < len(words):
        word = words[i]
        if word == "-C":
            if i + 1 >= len(words):
                return None
            path = words[i + 1]
            if base is None or any(c in path for c in "$`~"):
                base = None
            elif path:
                base = Path(os.path.normpath(os.path.join(str(base), path)))
            i += 2
        elif word == "-c":
            i += 2
        elif word in ("--git-dir", "--work-tree", "--namespace", "--exec-path"):
            unresolved = unresolved or word != "--namespace"
            i += 2
        elif word.startswith(("--git-dir=", "--work-tree=")):
            unresolved = True
            i += 1
        elif word.startswith("-"):
            i += 1
        else:
            break
    if i >= len(words) or words[i] != "commit":
        return None
    where = base or directory or cwd
    if dynamic:
        return CommitInvocation(where, "unknown", None, None,
                                "command contains dynamic shell constructs")
    if base is None or unresolved:
        return CommitInvocation(where, "unknown", None, None,
                                "commit directory could not be resolved")
    return _classify(words[i + 1:], base, placeholders)


def _classify(
    args: list[str], directory: Path, placeholders: dict[str, str]
) -> CommitInvocation:
    messages: list[str] = []
    files: list[str] = []
    fixup = amend = no_edit = editorish = False
    j = 0

    def take(attached: Optional[str]) -> Optional[str]:
        nonlocal j
        if attached:
            return attached
        j += 1
        return args[j] if j < len(args) else ""

    while j < len(args):
        arg = args[j]
        if arg == "--":
            break
        if arg.startswith("--"):
            name, eq, value = arg[2:].partition("=")
            attached = value if eq else None
            if name in ("fixup", "squash"):
                val = take(attached) if (attached is not None or name == "squash") else ""
                if val.startswith(("amend:", "reword:")):
                    editorish = True
                fixup = True
            elif name in _LONG_WITH_ARG:
                val = take(attached)
                if name == "message":
                    messages.append(val)
                elif name == "file":
                    files.append(val)
                elif name in ("template", "reuse-message", "reedit-message"):
                    editorish = True
            elif name == "edit":
                editorish = True
            elif name == "no-edit":
                no_edit = True
            elif name == "amend":
                amend = True
        elif arg.startswith("-") and len(arg) > 1:
            k = 1
            while k < len(arg):
                ch = arg[k]
                if ch in _SHORT_WITH_ARG:
                    val = take(arg[k + 1:] or None)
                    if ch == "m":
                        messages.append(val)
                    elif ch == "F":
                        files.append(val)
                    else:
                        editorish = True
                    break
                if ch in _SHORT_OPTIONAL_ARG:
                    break
                if ch == "e":
                    editorish = True
                k += 1
        j += 1

    def make(source: MessageSource, reason: str, path: Optional[Path] = None,
             text: Optional[str] = None) -> CommitInvocation:
        return CommitInvocation(directory, source, path, text, reason)

    if editorish:
        return make("unknown", "editor, template or reused message")
    if files and messages:
        return make("unknown", "both file and message given")
    if files:
        if len(files) != 1 or files[0] in ("", "-"):
            return make("unknown", "message file is not a concrete path")
        path = Path(files[0])
        if not path.is_absolute():
            path = Path(os.path.normpath(os.path.join(str(directory), files[0])))
        return make("file", "message file", path=path)
    if fixup:
        return make("auto", "fixup or squash commit")
    if messages:
        text = "\n\n".join(placeholders.get(m, m) for m in messages)
        return make("literal", "message option", text=text)
    if amend and no_edit:
        return make("auto", "amend without editing")
    if no_edit and _in_progress_operation(directory):
        return make("auto", "merge, cherry-pick or revert in progress")
    return make("unknown", "no message option")


def _in_progress_operation(directory: Path) -> bool:
    try:
        env = {k: v for k, v in os.environ.items()
               if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        env["GIT_OPTIONAL_LOCKS"] = "0"
        done = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, timeout=5, env=env, check=False,
        )
        if done.returncode != 0:
            return False
        git_dir = Path(done.stdout.strip())
        return any((git_dir / name).exists() for name in _AUTO_FILES)
    except Exception:
        return False
