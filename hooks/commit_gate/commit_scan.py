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

Commit options are an exact-spelling allowlist (no abbreviations). A ``cd``
only keeps a determinate directory when it is joined to the commit by ``&&``;
after ``cd X;`` or ``cd X`` + newline the commit is ``unknown`` because a
failed ``cd`` does not stop the next command. Paths containing a ``..``
component, ``cd -P/-L`` and ``-c core.worktree/core.bare/include*`` are
``unknown``. ``-c alias.*`` makes the whole git command ``unknown``; aliases
defined in git *config files* are out of scope here (the CI audit still sees
such commits). ``-c``/``--config-env`` keys ``commit.cleanup``,
``core.commentChar``, ``core.commentString`` and ``commit.template`` make the
command ``unknown``. The same keys set in git CONFIG FILES are not visible to
this static scanner: the gate must read ``commit.cleanup``/``core.commentChar``
from the commit directory before calling ``effective_message``.
``--fixup``/``--squash`` together with ``-m``/``-F`` is ``unknown`` (git prepends
an autosquash line). ``effective_message`` gives the bytes git records after its
message cleanup; pass ``from_literal=True`` for ``-m`` text, to which git
appends a final newline before cleanup.
"""

from __future__ import annotations

import os
import re
import subprocess
from bisect import bisect_left
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
    cleanup: str = "default"


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
# Anything but \n and \t: bash keeps e.g. CR inside a word, this scanner would not.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
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

_CLEANUP_MODES = frozenset({"default", "verbatim", "whitespace", "strip", "scissors"})
_WS = " \t\r\n"  # git's isspace (measured: \v and \f are kept)
# commit options with no effect on the recorded message (exact spellings only)
_LONG_IGNORED_FLAG = frozenset({
    "all", "quiet", "verbose", "no-verify", "verify", "allow-empty",
    "no-post-rewrite", "no-gpg-sign", "reset-author", "only", "include",
    "status", "no-status",
})
_LONG_IGNORED_ARG = frozenset({"author", "date"})
_SHORT_FLAGS = frozenset("aqvnoi")
_CONFIG_REDIRECT = ("core.worktree", "core.bare")
# config keys that change the recorded message bytes
_CONFIG_MESSAGE = frozenset({
    "commit.cleanup", "core.commentchar", "core.commentstring", "commit.template",
})
_DELIM_LINE = re.compile(r"\n([A-Za-z_]\w*)(?=\n)")


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
    """Over-approximate the number of commits: a `git` word later followed by
    the word `commit` (quoted option values and separators are ignored)."""
    count = 0
    seen_git = False
    for tok in _LOOSE_SPLIT.split(text):
        tok = tok.strip("\"'")
        if not tok:
            continue
        if tok == "commit":
            if seen_git:
                count += 1
                seen_git = False
        elif os.path.basename(tok) == "git":
            seen_git = True
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
        if _CONTROL_CHARS.search(command):
            return _loose_result(command, cwd, "control character in command")
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
    lines: dict[str, list[int]] = {}  # ident -> offsets of "\nIDENT\n" lines (one pass)
    scanned = False
    while True:
        m = _HEREDOC_HEADER.search(command, pos)
        if m is None:
            break
        delim = m.group("delim")
        if not scanned:
            scanned = True
            for f in _DELIM_LINE.finditer(command):
                lines.setdefault(f.group(1), []).append(f.start())
        end = _heredoc_end(command, m, delim, lines)
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
                 lines: dict[str, list[int]]) -> Optional[tuple[int, int]]:
    positions = lines.get(delim)
    if not positions:
        return None
    idx = bisect_left(positions, m.end() - 1)  # newline that ends the header line
    if idx >= len(positions):
        return None
    k = positions[idx]
    e = k + 1 + len(delim)  # the newline after the delimiter line
    tail = e + 1
    if command[tail:tail + 2] != ')"':
        return None
    after = tail + 2
    if after < len(command) and command[after] not in " \t\r\n;&|":
        return None
    return (k if k >= m.end() else m.end()), after


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


def _split_commands(items: list[object]) -> list[tuple[bool, list[_Word]]]:
    """Return (unconditional, words): True when `;` or a newline precedes it."""
    commands: list[tuple[bool, list[_Word]]] = []
    current: list[_Word] = []
    unconditional = False
    for item in items:
        if isinstance(item, str):
            if current:
                commands.append((unconditional, current))
                current = []
                unconditional = False
            if item != "&&":
                unconditional = True
        else:
            current.append(item)
    if current:
        commands.append((unconditional, current))
    for _, words in commands:
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
    for unconditional, words in commands:
        if unconditional and state["directory"] != cwd:
            state["poisoned"] = True  # a failed `cd` would not stop this command
        _handle(words, cwd, state, placeholders, invocations)
    return ScanResult(tuple(invocations))


def _literal(path: str) -> bool:
    return (bool(path) and not any(c in _PATH_BAD for c in path)
            and ".." not in path.split("/"))


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
    args = [a for a in args if a.text != "--" or a.quoted]
    if (len(args) != 1 or args[0].dyn or not _literal(args[0].text)
            or args[0].text.startswith("-")):
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
    alias = False
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
            if i + 1 >= len(words) or words[i + 1].dyn:
                clean = False
            else:
                key = words[i + 1].text.partition("=")[0].lower()
                if key.startswith("alias."):
                    alias = True
                elif (key in _CONFIG_REDIRECT or key in _CONFIG_MESSAGE
                      or key.startswith(("include.", "includeif."))):
                    clean = False
            i += 2
        elif text in _GIT_GLOBAL_WITH_ARG or text.startswith("--config-env="):
            clean = False
            value = text.partition("=")[2] or (
                words[i + 1].text if i + 1 < len(words) else "")
            if text.startswith("--config-env") and "alias." in value.lower():
                alias = True
            i += 1 if "=" in text else 2
        elif text.startswith("-"):
            clean = False
            i += 1
        else:
            break
    if i >= len(words):
        state["poisoned"] = True
        return
    sub = words[i].text
    if alias:
        out.append(_unknown(state["cwd"] if state["poisoned"] else base,
                            "git alias given on the command line"))
        state["poisoned"] = True
        return
    if sub != "commit":
        if (sub not in _BEFORE_COMMIT_GIT or not clean or words[i].dyn
                or _writes_files(sub, words[i + 1:])):
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


def _writes_files(sub: str, args: list[_Word]) -> bool:
    """`git diff --output` writes a file; `--pathspec-from-file` reads one."""
    for a in args:
        name = a.text.partition("=")[0]
        if not name.startswith("--") or len(name) < 3:
            continue
        if sub == "diff" and ("--output".startswith(name) or name.startswith("--output")):
            return True
        if sub != "diff" and ("--pathspec-from-file".startswith(name)
                              or name.startswith("--pathspec")):
            return True
    return False


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
    fixup = amend = no_edit = False
    cleanup = "default"
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
                if take(attached).startswith(("amend:", "reword:")):
                    return unknown("editor, template or reused message")
                fixup = True
            elif name == "message":
                messages.append(take(attached))
            elif name == "file":
                files.append(take(attached))
            elif name == "cleanup":
                cleanup = take(attached)
                if cleanup not in _CLEANUP_MODES:
                    return unknown("unknown --cleanup mode")
            elif name in _LONG_IGNORED_ARG:
                take(attached)
            elif name == "gpg-sign":
                pass
            elif name in _LONG_IGNORED_FLAG and not eq:
                pass
            elif name == "no-edit" and not eq:
                no_edit = True
            elif name == "amend" and not eq:
                amend = True
            else:
                return unknown(f"commit option --{name} is outside the allowlist")
        elif arg.startswith("-S"):
            pass
        elif arg.startswith("-") and len(arg) > 1:
            k = 1
            while k < len(arg):
                ch = arg[k]
                if ch in _SHORT_FLAGS:
                    k += 1
                    continue
                if ch in "mF":
                    val = take(arg[k + 1:] or None)
                    (messages if ch == "m" else files).append(val)
                    break
                return unknown(f"commit option -{ch} is outside the allowlist")
        elif arg == "-":
            return unknown("unrecognised commit argument")
        j += 1

    def make(source: MessageSource, reason: str, path: Optional[Path] = None,
             text: Optional[str] = None) -> CommitInvocation:
        return CommitInvocation(directory, source, path, text, reason, cleanup)

    if missing:
        return unknown("option is missing its argument")
    if files and messages:
        return unknown("both file and message given")
    if fixup and (files or messages):
        return unknown("--fixup/--squash with a message source adds an autosquash line")
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
        if cleanup == "verbatim" and any(m == "" for m in messages):
            return unknown("empty -m with --cleanup=verbatim is not modelled")
        parts = [placeholders.get(m, m) for m in messages]
        if any(_PLACEHOLDER_PREFIX in p and p not in placeholders.values() for p in parts):
            return unknown("message is not a statically known string")
        return make("literal", "message option", text=_join_messages(parts))
    if amend and no_edit:
        return make("auto", "amend without editing")
    if no_edit and _in_progress_operation(directory):
        return make("auto", "merge, cherry-pick or revert in progress")
    return unknown("no message option")


def _join_messages(parts: list[str]) -> str:
    """git's `-m` accumulation without the final newline: a blank line between
    values, each value ended with a newline first (strbuf_complete_line)."""
    acc = ""
    for part in parts:
        if acc:
            acc += ("" if acc.endswith("\n") else "\n") + "\n"
        acc += part
    return acc


def effective_message(raw: str, cleanup: str, *, from_literal: bool = False) -> Optional[str]:
    """The bytes git records for `raw` under `--cleanup=<cleanup>` (no editor).

    ``from_literal`` marks text built from ``-m``: git ends it with a newline
    before cleanup. File text (default) is used as read.

    ``None`` when the result depends on ``core.commentChar`` or the mode is
    unknown, so the caller must treat the message as unknown.
    """
    if from_literal and raw and not raw.endswith("\n"):
        raw += "\n"
    if cleanup == "verbatim":
        return raw
    if cleanup not in ("default", "whitespace"):
        return None
    out: list[str] = []
    blank = False
    for line in raw.split("\n"):
        line = line.rstrip(_WS)
        if not line:
            blank = bool(out)
            continue
        if blank:
            out.append("")
            blank = False
        out.append(line)
    return "\n".join(out) + "\n" if out else ""


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
