#!/usr/bin/env python3
"""Commit title rules shared by the commit-time gate and the CI audit.

Python 3.10 standard library only. The rule text lives in
docs/commit-title-rules.md; keep both in sync.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


RULES_VERSION = "1"

TYPES: tuple[str, ...] = (
    "feat",
    "fix",
    "chore",
    "refactor",
    "docs",
    "test",
    "build",
    "ci",
    "perf",
    "style",
)

# conventional-changelog-conventionalcommits 7.0.2 headerPattern; JS \w is ASCII.
HEADER_RE = re.compile(r"^(\w*)(?:\((.*)\))?!?: (.*)$", re.ASCII)


# Same first-line boundary as validate_change_evidence.MARKDOWN_LINE_ENDING_RE.
_LINE_ENDING_RE = re.compile(r"\r\n|\r|\n")


def _first_line(message: str) -> str:
    return _LINE_ENDING_RE.split(message, maxsplit=1)[0]


@dataclass(frozen=True)
class TitleFinding:
    code: str
    message: str


def check_title(message: str) -> tuple[TitleFinding, ...]:
    """Check the first line of a commit message. Auto messages are not considered."""
    header = _first_line(message)

    type_name: str | None = None
    subject: str | None = None
    match = HEADER_RE.match(header)
    if match:
        type_name = match.group(1) or None
        subject = match.group(3) or None

    findings: list[TitleFinding] = []
    if not type_name:
        findings.append(TitleFinding("title-type-empty", "Title needs a type before the colon."))
    else:
        if type_name != type_name.lower():
            findings.append(TitleFinding("title-type-case", "Title type must be lower case."))
        if type_name not in TYPES:
            findings.append(
                TitleFinding(
                    "title-type-enum",
                    "Title type must be one of: " + ", ".join(TYPES) + ".",
                )
            )
    if not subject:
        findings.append(TitleFinding("title-subject-empty", "Title needs a subject after the colon."))
    if header.endswith(".") and not header.endswith("..."):
        findings.append(TitleFinding("title-subject-full-stop", "Title subject must not end with a period."))
    if header != header.strip():
        findings.append(
            TitleFinding("title-header-trim", "Title must not have leading or trailing whitespace.")
        )
    return tuple(findings)


# --- automatic messages: @commitlint/is-ignored 19.8.1 default list ---------
# JS "." excludes \n \r U+2028 U+2029; JS multiline ^ and $ break on the same set.
_D = "[^\\n\\r\\u2028\\u2029]"
_BOL = "(?:\\A|(?<=[\\n\\r\\u2028\\u2029]))"
_EOL = "(?=[\\n\\r\\u2028\\u2029]|\\Z)"

_MULTILINE_PATTERNS = (
    _BOL + f"((Merge pull request)|(Merge ({_D}*?) into ({_D}*?)|(Merge branch ({_D}*?)))(?:\\r?\\n)*" + _EOL + ")",
    _BOL + f"(Merge tag ({_D}*?))(?:\\r?\\n)*" + _EOL,
)
_PLAIN_PATTERNS = (
    f"^(R|r)evert ({_D}*)",
    f"^(R|r)eapply ({_D}*)",
    "^(amend|fixup|squash)!",
    f"^(Merged ({_D}*?)(in|into) ({_D}*)|Merged PR ({_D}*): ({_D}*))",
    f"^Merge remote-tracking branch(\\s*)({_D}*)",
    f"^Automatic merge({_D}*)",
    f"^Auto-merged ({_D}*?) into ({_D}*)",
)
_AUTO_RES = tuple(re.compile(p) for p in _MULTILINE_PATTERNS) + tuple(
    re.compile(p) for p in _PLAIN_PATTERNS
)

_CHORE_PREFIX_RE = re.compile(r"^chore(\([^)]+\))?:")
_SEMVER_LEAD_RE = re.compile(r"^v?")
_NUM = r"(0|[1-9][0-9]*)"
_PRE_ID = r"(0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)"
_SEMVER_RE = re.compile(
    rf"{_NUM}\.{_NUM}\.{_NUM}(-{_PRE_ID}(\.{_PRE_ID})*)?(\+[0-9a-zA-Z-]+(\.[0-9a-zA-Z-]+)*)?",
    re.ASCII,
)
_MAX_SAFE_INTEGER = 9007199254740991
_SEMVER_MAX_LENGTH = 256


def _is_semver(value: str) -> bool:
    """npm semver.valid(): at most one leading v, SemVer 2.0.0 strict grammar."""
    value = _SEMVER_LEAD_RE.sub("", value.strip())
    if len(value) > _SEMVER_MAX_LENGTH or not _SEMVER_RE.fullmatch(value):
        return False
    core = value.split("-", 1)[0].split("+", 1)[0]
    return all(int(part) <= _MAX_SAFE_INTEGER for part in core.split("."))


def is_auto_message(message: str) -> bool:
    """True for merge/revert/fixup/release messages that skip every title and evidence rule."""
    if any(pattern.search(message) for pattern in _AUTO_RES):
        return True
    first_line = _first_line(message)
    stripped = _CHORE_PREFIX_RE.sub("", first_line, count=1)
    return _is_semver(stripped)
