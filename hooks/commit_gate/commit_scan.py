"""Classify `git commit` invocations inside a shell command string.

This scanner only classifies. It is an allowlist: a determinate result (a
message file, a literal message, a commit directory) is produced only for the
small grammar documented below. Any other construct makes every commit in the
command ``unknown``, and the commit gate lets ``unknown`` pass with a warning,
because a wrong determinate result is worse than ``unknown``. It never executes
the command and never raises.

Grammar that can yield a determinate result:

* simple commands joined only by ``&&``, ``;`` or newline (backslash-newline
  continuations are removed first, outside single quotes);
* ``cd <literal>``, ``true``, ``git [-C <literal>] [-c k=v] ...`` and, only
  before a commit, ``git add|rm|status|diff``; an optional prefix of
  ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` assignments or a bare ``env`` of those;
* message sources: ``-F/--file``, ``-m/--message`` and the canonical quoted
  heredoc ``-m "$(cat <<'D' ... D\n)"``.
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
_MAX_PATH = 1024
_MAX_DASH_C = 32

_HEREDOC_HEADER = re.compile(
    r"""(?P<opt>(?<![\w-])(?:-m|--message))[ \t]+"\$\(cat[ \t]+<<"""
    r"""(?P<q>['"])(?P<delim>[A-Za-z_]\w*)(?P=q)[ \t]*\n"""
)
_PLACEHOLDER_PREFIX = "__COMMITGATE_HEREDOC_"
_ASSIGNMENT = re.compile(r"^([A-Za-z_]\w*)=")
_LOOSE_SPLIT = re.compile(r"[\s;&|()`<>]+")
_ALLOWED_ENV = frozenset({
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
})
_KEYWORDS = frozenset({
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "function", "select", "in", "time", "coproc",
})
_BEFORE_COMMIT_GIT = frozenset({"add", "rm", "status", "diff"})
_GIT_GLOBAL_WITH_ARG = frozenset({
    "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix",
    "--config-env",
})
_PATH_BAD = frozenset("$~*?[{}`")
_AUTO_FILES = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")

# commit options that take a separate/attached argument
_SHORT_WITH_ARG = frozenset("mFCct")
_SHORT_OPTIONAL_ARG = frozenset("uS")
_LONG_WITH_ARG = frozenset({
    "file", "message", "template", "reuse-message", "reedit-message", "fixup",
    "squash", "author", "date", "trailer", "pathspec-from-file", "cleanup",
})


class _Unsupported(Exception):
    """The command uses a construct outside the allowlisted grammar."""


@dataclass(frozen=True)
class _Word:
    text: str
    dyn: bool      # contains an expansion the scanner does not model
    quoted: bool   # some part was quoted or escaped


def _unknown(directory: Path, reason: str) -> CommitInvocation:
    return CommitInvocation(directory, "unknown", None, None, reason)


# ---- loose detection (no grammar; only counts `git ... commit`) -------------

def _count_loose(text: str) -> int:
    """Count `git [options] commit` sequences in a flat word list (linear)."""
    toks = [t.strip("\"'") for t in _LOOSE_SPLIT.split(text)]
    count = 0
    i = 0
    n = len(toks)
    while i < n:
        if toks[i] and os.path.basename(toks[i]) == "git":
            j = i + 1
            while j < n and toks[j].startswith("-") and len(toks[j]) > 1:
                j += 2 if toks[j] in ("-C", "-c") or toks[j] in _GIT_GLOBAL_WITH_ARG else 1
            if j < n and toks[j] == "commit":
                count += 1
                i = j
        i += 1
    return count


def _loose_result(text: str, cwd: Path, reason: str) -> ScanResult:
    return ScanResult(tuple(_unknown(cwd, reason) for _ in range(_count_loose(text))))


def scan_commits(command: str, *, cwd: Path) -> ScanResult:
    try:
        cwd = Path(cwd)
        if not isinstance(command, str):
            return ScanResult(())
        if len(command) > _MAX_SCAN_CHARS:
            return _loose_result(command, cwd, "command too long to analyse")
        return _scan(command, cwd)
    except Exception:  # never raise on arbitrary input
        try:
            return _loose_result(command if isinstance(command, str) else "",
                                 Path(cwd), "command could not be analysed")
        except Exception:
            return ScanResult(())


# ---- preprocessing -----------------------------------------------------------

def _extract_heredocs(command: str) -> tuple[str, dict[str, str]]:
    """Replace canonical `-m "$(cat <<'D' ... D\n)"` with a placeholder word.

    The body ends at the FIRST line that is exactly the delimiter (bash rule)
    and that line must be followed directly by `)"` and then end-of-command,
    whitespace or a separator. Anything else is left in place (and later
    rejected by the lexer).
    """
    placeholders: dict[str, str] = {}
    out: list[str] = []
    pos = 0
    missing: set[str] = set()
    while True:
        m = _HEREDOC_HEADER.search(command, pos)
        if m is None:
            break
        delim = m.group("delim")
        end = _heredoc_end(command, m, delim, missing)
        if end is None:
            out.append(command[pos:m.end()])
            pos = m.end()
            continue
        close_start, after = end
        name = f"{_PLACEHOLDER_PREFIX}{len(placeholders)}__"
        while name in command:
            name += "_"
        placeholders[name] = command[m.end():close_start].rstrip("\n")
        out.append(command[pos:m.start()])
        out.append(f"{m.group('opt')} {name}")
        pos = after
    out.append(command[pos:])
    return "".join(out), placeholders


def _heredoc_end(command: str, m: "re.Match[str]", delim: str,
                 missing: set[str]) -> Optional[tuple[int, int]]:
    if delim in missing:
        return None
    needle = "\n" + delim
    k = m.end() - 1  # the newline that ends the header line
    while True:
        k = command.find(needle, k)
        if k < 0:
            missing.add(delim)
            return None
        e = k + len(needle)
        if e < len(command) and command[e] == "\n":
            break
        if e >= len(command):
            return None
        k = e - 1 if e - 1 > k else k + 1
    # `k` is the newline before the first exact delimiter line; `e` the newline after
    tail = e + 1
    if command[tail:tail + 2] != ')"':
        return None
    after = tail + 2
    if after < len(command) and command[after] not in " \t\r\n;&|":
        return None
    return k if k >= m.end() else m.end(), after


def _join_continuations(text: str) -> str:
    """Drop backslash-newline outside single quotes (bash does, even inside "")."""
    if "\\\n" not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    single = double = False
    while i < n:
        c = text[i]
        if single:
            single = c != "'"
            out.append(c)
            i += 1
        elif c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
            elif i + 1 < n:
                out.append(text[i:i + 2])
                i += 2
            else:
                out.append(c)
                i += 1
        else:
            if c == "'" and not double:
                single = True
            elif c == '"':
                double = not double
            out.append(c)
            i += 1
    return "".join(out)


# ---- lexer --------------------------------------------------------------------

_WORD_END = frozenset(" \t\r\n;&|()<>")


def _lex(text: str) -> list[object]:
    """Return words (`_Word`) and separators (`str`); raise for anything else."""
    items: list[object] = []
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c in " \t\r":
            i += 1
        elif c == "\n":
            items.append("\n")
            i += 1
        elif c == ";":
            if text[i + 1:i + 2] == ";":
                raise _Unsupported("';;'")
            items.append(";")
            i += 1
        elif c == "&":
            if text[i + 1:i + 2] != "&":
                raise _Unsupported("background '&'")
            items.append("&&")
            i += 2
        elif c == "|":
            raise _Unsupported("pipe or '||'")
        elif c in "()":
            raise _Unsupported("subshell or group parentheses")
        elif c in "<>":
            raise _Unsupported("redirection or heredoc")
        else:
            word, i = _lex_word(text, i)
            items.append(word)
    return items


def _lex_word(text: str, i: int) -> tuple[_Word, int]:
    n = len(text)
    buf: list[str] = []
    dyn = quoted = False
    first = True
    while i < n:
        c = text[i]
        if c in _WORD_END:
            break
        if c == "#" and first:
            raise _Unsupported("comment")
        first = False
        if c == "'":
            close = text.find("'", i + 1)
            if close < 0:
                raise ValueError("unterminated single quote")
            buf.append(text[i + 1:close])
            quoted = True
            i = close + 1
        elif c == '"':
            quoted = True
            i += 1
            while True:
                if i >= n:
                    raise ValueError("unterminated double quote")
                d = text[i]
                if d == '"':
                    i += 1
                    break
                if d == "`":
                    raise _Unsupported("command substitution")
                if d == "$":
                    if text[i + 1:i + 2] in ("(", "{"):
                        raise _Unsupported("command or parameter substitution")
                    dyn = True
                if d == "\\" and i + 1 < n and text[i + 1] in '\\$"`':
                    d = text[i + 1]
                    i += 1
                buf.append(d)
                i += 1
        elif c == "\\":
            quoted = True
            if i + 1 < n:
                buf.append(text[i + 1])
                i += 2
            else:
                buf.append(c)
                i += 1
        else:
            if c == "`":
                raise _Unsupported("command substitution")
            if c == "$":
                if text[i + 1:i + 2] in ("(", "{"):
                    raise _Unsupported("command or parameter substitution")
                dyn = True
            elif c in "*?[{" or (c == "~" and not buf):
                dyn = True
            buf.append(c)
            i += 1
    return _Word("".join(buf), dyn, quoted), i


def _split_commands(items: list[object]) -> list[list[_Word]]:
    commands: list[list[_Word]] = [[]]
    for item in items:
        if isinstance(item, str):
            commands.append([])
        else:
            commands[-1].append(item)  # type: ignore[arg-type]
    commands = [c for c in commands if c]
    for words in commands:
        head_seen = False
        for word in words:
            if not word.quoted and word.text in ("{", "}", "!"):
                raise _Unsupported(f"'{word.text}'")
            if not head_seen and not _ASSIGNMENT.match(word.text):
                head_seen = True
                if not word.quoted and word.text in _KEYWORDS:
                    raise _Unsupported(f"shell keyword '{word.text}'")
    return commands


# ---- scanning -----------------------------------------------------------------

def _scan(command: str, cwd: Path) -> ScanResult:
    reduced, placeholders = _extract_heredocs(command)
    reduced = _join_continuations(reduced)
    try:
        commands = _split_commands(_lex(reduced))
    except _Unsupported as exc:
        return _loose_result(reduced, cwd, f"unsupported shell construct: {exc}")
    except ValueError:
        return _loose_result(reduced, cwd, "command could not be parsed")

    invocations: list[CommitInvocation] = []
    state = {"directory": cwd, "cwd": cwd, "poisoned": False}
    for words in commands:
        _handle(words, cwd, state, placeholders, invocations)
    return ScanResult(tuple(invocations))


def _literal(path: str) -> bool:
    return bool(path) and not any(c in _PATH_BAD for c in path)


def _handle(words: list[_Word], cwd: Path, state: dict, placeholders: dict[str, str],
            out: list[CommitInvocation]) -> None:
    def other(reason: str, args: list[_Word]) -> None:
        state["poisoned"] = True
        text = " ".join(w.text for w in args)
        for _ in range(_count_loose(text)):
            out.append(_unknown(cwd, reason))

    i = 0
    env_ok = True
    while i < len(words) and (m := _ASSIGNMENT.match(words[i].text)):
        env_ok = env_ok and m.group(1) in _ALLOWED_ENV and not words[i].dyn
        i += 1
    rest = words[i:]
    if rest and os.path.basename(rest[0].text) == "env" and not rest[0].dyn:
        k = 1
        while k < len(rest) and (m := _ASSIGNMENT.match(rest[k].text)):
            env_ok = env_ok and m.group(1) in _ALLOWED_ENV and not rest[k].dyn
            k += 1
        if k < len(rest) and rest[k].text.startswith("-"):
            env_ok = False
        rest = rest[k:]
    if not rest:
        state["poisoned"] = True
        return
    if not env_ok:
        other("environment prefix or env option the scanner does not model", rest)
        return
    head = rest[0]
    name = os.path.basename(head.text)
    if head.dyn:
        other("dynamic command name", rest)
    elif name == "cd":
        _apply_cd(rest[1:], state)
    elif name == "true":
        pass
    elif name == "git":
        _handle_git(rest, state, placeholders, out)
    else:
        other("git commit inside a command the scanner does not model", rest[1:])


def _apply_cd(args: list[_Word], state: dict) -> None:
    args = [a for a in args if a.text not in ("-L", "-P", "--") or a.quoted]
    if len(args) != 1 or args[0].dyn or not _literal(args[0].text) or args[0].text == "-":
        state["poisoned"] = True
        return
    if state["poisoned"]:
        return
    joined = os.path.normpath(os.path.join(str(state["directory"]), args[0].text))
    if len(joined) > _MAX_PATH:
        state["poisoned"] = True
    else:
        state["directory"] = Path(joined)


def _handle_git(words: list[_Word], state: dict, placeholders: dict[str, str],
                out: list[CommitInvocation]) -> None:
    base: Path = state["directory"]
    clean = True
    dash_c = 0
    i = 1
    while i < len(words):
        text = words[i].text
        if text == "-C":
            dash_c += 1
            if (dash_c > _MAX_DASH_C or i + 1 >= len(words) or words[i + 1].dyn
                    or not _literal(words[i + 1].text)):
                clean = False
            else:
                joined = os.path.normpath(os.path.join(str(base), words[i + 1].text))
                clean = clean and len(joined) <= _MAX_PATH
                base = Path(joined if len(joined) <= _MAX_PATH else str(base))
            i += 2
        elif text == "-c":
            clean = clean and i + 1 < len(words) and not words[i + 1].dyn
            i += 2
        elif text in _GIT_GLOBAL_WITH_ARG:
            clean = False
            i += 2
        elif text.startswith("-"):
            clean = False
            i += 1
        else:
            break
    if i >= len(words):
        state["poisoned"] = True
        return
    sub = words[i].text
    if sub != "commit":
        if sub not in _BEFORE_COMMIT_GIT or not clean or words[i].dyn:
            state["poisoned"] = True
        return
    if state["poisoned"]:
        out.append(_unknown(state["cwd"],
                            "preceded by a command the scanner does not model"))
        return
    if not clean:
        out.append(_unknown(base, "unsupported git option or commit directory"))
        return
    out.append(_classify(words[i + 1:], base, placeholders))


def _classify(
    args: list[_Word], directory: Path, placeholders: dict[str, str]
) -> CommitInvocation:
    def unknown(reason: str) -> CommitInvocation:
        return CommitInvocation(directory, "unknown", None, None, reason)

    if any(a.dyn for a in args):
        return unknown("dynamic shell construct in commit arguments")
    texts = [a.text for a in args]
    messages: list[str] = []
    files: list[str] = []
    fixup = amend = no_edit = editorish = False
    missing = False
    j = 0

    def take(attached: Optional[str]) -> str:
        nonlocal j, missing
        if attached is not None:
            return attached
        j += 1
        if j < len(texts):
            return texts[j]
        missing = True
        return ""

    while j < len(texts):
        arg = texts[j]
        if arg == "--":
            break
        if arg.startswith("--"):
            name, eq, value = arg[2:].partition("=")
            attached = value if eq else None
            if name in ("fixup", "squash"):
                val = take(attached)
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

    if missing:
        return unknown("option is missing its argument")
    if editorish:
        return unknown("editor, template or reused message")
    if files and messages:
        return unknown("both file and message given")
    if files:
        value = files[0]
        if (len(files) != 1 or not _literal(value) or value == "-"
                or value.startswith(("/dev/", "/proc/"))):
            return unknown("message file is not a concrete path")
        path = Path(value)
        if not path.is_absolute():
            path = Path(os.path.normpath(os.path.join(str(directory), value)))
        return make("file", "message file", path=path)
    if fixup:
        return make("auto", "fixup or squash commit")
    if messages:
        parts = [placeholders.get(m, m) for m in messages]
        if any(_PLACEHOLDER_PREFIX in p and p not in placeholders.values() for p in parts):
            return unknown("message is not a statically known string")
        return make("literal", "message option", text="\n\n".join(parts))
    if amend and no_edit:
        return make("auto", "amend without editing")
    if no_edit and _in_progress_operation(directory):
        return make("auto", "merge, cherry-pick or revert in progress")
    return unknown("no message option")


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
