#!/usr/bin/env python3
"""Validate Markdown against Change Evidence Contract v1."""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import errno
import html
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata
from typing import Sequence


CONTRACT_VERSION = "v1"
MAX_BYTES = 64 * 1024
MAX_FINDINGS = 128
MAX_REPORT_JSON_UTF16_BYTES = 512 * 1024
REPORT_ENVELOPE_UTF16_RESERVE = 4 * 1024
MAX_REPORTED_VERSION_CHARACTERS = 256
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*$")
EMPTY_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]*$")
FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<run>`{3,}|~{3,})(?P<rest>[^\r\n]*)$")
SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
PLACEHOLDER_VALUE_RE = re.compile(
    r"(?i)^(?:tbd|todo|fixme|n/?a|none|unknown|미정|추후|없음|\?{2,})$"
)
ANGLE_PLACEHOLDER_RE = re.compile(
    r"(?i)^<(?:(?:fill|insert|describe|add)[^>]*|[^>]+ here)>$"
)
CHANGE_URL_QUERY_OR_FRAGMENT = r"(?:[?#][^\s<>\[\]]*)?"
CHANGE_URL_END = r"(?=$|[\s)\]}>\"']|[.,!;:](?=$|[\s)\]}>\"']))"
ISSUE_URL_PATTERN = (
    r"(?<![A-Za-z0-9_./?=&%+-])"
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*"
    + CHANGE_URL_QUERY_OR_FRAGMENT
    + CHANGE_URL_END
)
ISSUE_URL_RE = re.compile(ISSUE_URL_PATTERN, re.IGNORECASE)
CHANGE_URL_PATTERN = (
    r"(?<![A-Za-z0-9_./?=&%+-])"
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[1-9][0-9]*"
    + CHANGE_URL_QUERY_OR_FRAGMENT
    + CHANGE_URL_END
)
CHANGE_URL_RE = re.compile(CHANGE_URL_PATTERN, re.IGNORECASE)
BARE_REFERENCE_PATTERN = (
    r"(?<![A-Za-z0-9_./?=&%+#-])#[1-9][0-9]*" + CHANGE_URL_END
)
BARE_REFERENCE_RE = re.compile(BARE_REFERENCE_PATTERN)
PR_LABEL_RE = re.compile(r"(?i)\b(?:pr|pull request)\s*:?[ \t]*$")
INLINE_LINK_RE = re.compile(
    r"\[(?P<label>[^\]\r\n]*)\]\([ \t]*"
    r"(?P<destination><[^>\r\n]*>|[^\s)\r\n]+)"
)
CHANGE_REFERENCE_RE = re.compile(
    BARE_REFERENCE_PATTERN + "|" + CHANGE_URL_PATTERN,
    re.IGNORECASE,
)
LIST_MARKER = r"(?:[-*+]|[0-9]{1,9}[.)])"
STRUCTURAL_LIST_ITEM_RE = re.compile(
    rf"^(?P<indent>[ \t]*)(?P<marker>{LIST_MARKER})"
    r"(?P<spacing>[ \t]+|(?=$))(?P<value>.*)$"
)
BLOCKQUOTE_RE = re.compile(r"^ {0,3}>")
BLOCKQUOTE_CONTENT_RE = re.compile(r"^ {0,3}>[ \t]?(?P<value>.*)$")
CHECKBOX_VALUE_RE = re.compile(r"^\[[ xX]\][ \t]+(?P<value>.*)$")
CHECKBOX_ITEM_RE = re.compile(r"^\[[ xX]\](?:[ \t]+(?P<value>.*))?$")
RAW_HTML_START_RE = re.compile(
    r"(?i)(?:</?[A-Za-z][A-Za-z0-9-]*(?=[\s/>])|<![A-Z]|<!\[CDATA\[|<\?)"
)
ATX_HEADING_RE = re.compile(r"^#{1,6}(?:[ \t]+(?P<value>.*))?$")
THEMATIC_BREAK_RE = re.compile(
    r"^(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"
)
UNSUPPORTED_LINE_SEPARATOR_RE = re.compile(
    "[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]"
)
MARKDOWN_LINE_ENDING_RE = re.compile(r"\r\n|\r|\n")
LIST_ITEM_RE = re.compile(
    rf"^ {{0,3}}{LIST_MARKER}\s+(?:\[[ xX]\]\s+)?(?P<value>.*)$"
)
EMPTY_LIST_MARKER_RE = re.compile(rf"^ {{0,3}}{LIST_MARKER}[ \t]*$")
LIST_SENTINEL_RE = re.compile(
    rf"(?im)^ {{0,3}}{LIST_MARKER}\s+(?:\[[ xX]\]\s+)?"
    r"(?:unknown|not applicable|not run):"
)
ABSENCE_RE = re.compile(
    r"(?is)^\s*(?P<kind>unknown|not applicable|not run):\s*(?P<reason>.*?)\s*$"
)
GENERIC_COMMIT_TITLES = {
    "change",
    "changes",
    "fix",
    "misc",
    "update",
    "wip",
    "변경",
    "수정",
    "작업",
}
DEFAULT_IGNORABLE_CODE_POINT_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
LINK_DEFINITION_INVALID = 0
LINK_DEFINITION_INCOMPLETE = 1
LINK_DEFINITION_VALID = 2
ASCII_MARKDOWN_PUNCTUATION = frozenset(
    character
    for character in map(chr, range(0x21, 0x7F))
    if unicodedata.category(character).startswith(("P", "S"))
)
MAX_LINK_LABEL_CHARACTERS = 999
# Normalized labels of the link reference definitions in the document being
# validated; reference-style links and images resolve only against these.
LINK_REFERENCE_LABELS: ContextVar[frozenset[str]] = ContextVar(
    "link_reference_labels", default=frozenset()
)


@dataclass(frozen=True)
class ContractDefinition:
    headings: tuple[str, ...]
    checklist_fields: frozenset[str] = frozenset()
    bullet_fields: frozenset[str] = frozenset()
    issue_reference_fields: frozenset[str] = frozenset()
    change_reference_fields: frozenset[str] = frozenset()
    absence_allowed_fields: frozenset[str] = frozenset()


CONTRACTS = {
    "issue": ContractDefinition(
        headings=(
            "Contract version",
            "Context and problem",
            "Goal",
            "Non-goals",
            "Acceptance criteria",
            "Constraints and impact",
        ),
        checklist_fields=frozenset({"Acceptance criteria"}),
        absence_allowed_fields=frozenset({"Non-goals", "Constraints and impact"}),
    ),
    "pull-request": ContractDefinition(
        headings=(
            "Contract version",
            "Summary",
            "Changes",
            "Validation",
            "Impact and risks",
            "Related issue",
        ),
        bullet_fields=frozenset({"Changes", "Validation"}),
        issue_reference_fields=frozenset({"Related issue"}),
        absence_allowed_fields=frozenset({"Impact and risks"}),
    ),
    "commit": ContractDefinition(
        headings=(
            "Contract version",
            "Why",
            "Changes",
            "Validation",
            "References",
        ),
        bullet_fields=frozenset({"Changes", "Validation"}),
        change_reference_fields=frozenset({"References"}),
    ),
}


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    line: int | None = None
    field: str | None = None


class FindingAccumulator(list[Finding]):
    """Keep diagnostics deterministic and safe for GitHub output framing."""

    def __init__(self) -> None:
        super().__init__()
        self.omitted = 0
        self.payload_utf16_bytes = 0

    @property
    def has_findings(self) -> bool:
        return bool(self) or self.omitted > 0

    def append(self, item: Finding) -> None:
        encoded = json.dumps(
            asdict(item), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-16-le")
        item_size = len(encoded) + 2
        if (
            len(self) >= MAX_FINDINGS - 1
            or self.payload_utf16_bytes + item_size
            > MAX_REPORT_JSON_UTF16_BYTES - REPORT_ENVELOPE_UTF16_RESERVE
        ):
            self.omitted += 1
            return
        super().append(item)
        self.payload_utf16_bytes += item_size

    def extend(self, items: Sequence[Finding]) -> None:
        for item in items:
            self.append(item)

    def freeze(self) -> tuple[Finding, ...]:
        items = list(self)
        if self.omitted:
            items.append(
                Finding(
                    "findings-truncated",
                    f"{self.omitted} additional findings were omitted to keep output bounded.",
                )
            )
        return tuple(items)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    kind: str
    version: str | None
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "kind": self.kind,
            "version": self.version,
            "findings": [asdict(item) for item in self.findings],
        }


def _masked_line(line: str) -> str:
    return "".join(
        character if character in "\r\n" else " " for character in line
    )


def _indent_columns(line: str) -> int:
    columns = 0
    for character in line:
        if character == " ":
            columns += 1
        elif character == "\t":
            columns += 4 - (columns % 4)
        else:
            break
    return columns


def _column_width(value: str) -> int:
    columns = 0
    for character in value:
        if character == "\t":
            columns += 4 - (columns % 4)
        else:
            columns += 1
    return columns


def _list_marker_padding_columns(
    line: str, list_match: re.Match[str]
) -> int:
    marker_end = _column_width(line[: list_match.end("marker")])
    value_start = _column_width(line[: list_match.start("value")])
    return value_start - marker_end


def _list_content_indent(line: str, list_match: re.Match[str]) -> int:
    marker_end = _column_width(line[: list_match.end("marker")])
    padding = _list_marker_padding_columns(line, list_match)
    if not list_match.group("value").strip():
        padding = 1
    return marker_end + (padding if 1 <= padding <= 4 else 1)


def _list_marker_value(line: str, list_match: re.Match[str]) -> str:
    padding = _list_marker_padding_columns(line, list_match)
    if padding <= 4:
        return list_match.group("value")
    return " " * (padding - 1) + list_match.group("value")


def _list_marker_starts_indented_code(
    line: str, list_match: re.Match[str]
) -> bool:
    marker_value = _list_marker_value(line, list_match)
    return bool(marker_value.strip()) and _indent_columns(marker_value) >= 4


def _mask_list_marker_code(line: str, list_match: re.Match[str]) -> str:
    marker_end = list_match.end("marker")
    padding_end = marker_end + 1
    return line[:marker_end] + " " + _masked_line(line[padding_end:])


def _markdown_lines(value: str, *, keepends: bool = False) -> list[str]:
    lines: list[str] = []
    cursor = 0
    for match in MARKDOWN_LINE_ENDING_RE.finditer(value):
        end = match.end() if keepends else match.start()
        lines.append(value[cursor:end])
        cursor = match.end()
    if cursor < len(value):
        lines.append(value[cursor:])
    return lines


def _blockquote_allows_lazy_continuation(value: str) -> bool:
    candidate = value
    while True:
        if _indent_columns(candidate) >= 4:
            return False
        blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
        if blockquote:
            candidate = blockquote.group("value")
            continue
        list_match = STRUCTURAL_LIST_ITEM_RE.fullmatch(candidate)
        if list_match:
            candidate = _list_marker_value(candidate, list_match)
            continue
        break

    stripped = candidate.strip()
    return bool(
        stripped
        and not EMPTY_LIST_MARKER_RE.fullmatch(stripped)
        and not ATX_HEADING_RE.fullmatch(stripped)
        and not THEMATIC_BREAK_RE.fullmatch(stripped)
        and not FENCE_RE.fullmatch(candidate)
        and not _is_link_definition(candidate)
    )


def _list_can_interrupt_paragraph(list_match: re.Match[str]) -> bool:
    if not list_match.group("value").strip():
        return False
    marker = list_match.group("marker")
    if marker[0].isdigit():
        return int(marker[:-1]) == 1
    return True


def _parse_link_definition_status(
    value: str,
    title_present: list[bool] | None = None,
    label: list[str] | None = None,
) -> int:
    """Classify a complete or extendable CommonMark link definition."""

    if title_present is not None:
        title_present[:] = [False]

    index = 0
    while index < len(value) and value[index] == " " and index < 3:
        index += 1
    if index >= len(value) or value[index] != "[":
        return LINK_DEFINITION_INVALID

    index += 1
    label_start = index
    label_length = 0
    label_has_content = False
    while index < len(value):
        character = value[index]
        if (
            character == "\\"
            and index + 1 < len(value)
            and value[index + 1] in ASCII_MARKDOWN_PUNCTUATION
        ):
            label_length += 1
            label_has_content = True
            index += 2
            continue
        if character == "[":
            return LINK_DEFINITION_INVALID
        if character == "]":
            break
        if character not in " \t\n":
            label_has_content = True
        label_length += 1
        if label_length > MAX_LINK_LABEL_CHARACTERS:
            return LINK_DEFINITION_INVALID
        index += 1
    else:
        return LINK_DEFINITION_INCOMPLETE

    if not label_has_content:
        return LINK_DEFINITION_INVALID
    if label is not None:
        label[:] = [value[label_start:index]]
    index += 1
    if index >= len(value) or value[index] != ":":
        return LINK_DEFINITION_INVALID
    index += 1

    while index < len(value) and value[index] in " \t":
        index += 1
    if index == len(value):
        return LINK_DEFINITION_INCOMPLETE
    if value[index] == "\n":
        index += 1
        while index < len(value) and value[index] in " \t":
            index += 1
        if index == len(value):
            return LINK_DEFINITION_INCOMPLETE
        if value[index] == "\n":
            return LINK_DEFINITION_INVALID

    if value[index] == "<":
        index += 1
        while index < len(value):
            character = value[index]
            if (
                character == "\\"
                and index + 1 < len(value)
                and value[index + 1] in ASCII_MARKDOWN_PUNCTUATION
            ):
                index += 2
                continue
            if character == ">":
                index += 1
                break
            if character in "<\n" or ord(character) < 0x20:
                return LINK_DEFINITION_INVALID
            index += 1
        else:
            return LINK_DEFINITION_INVALID
    else:
        destination_start = index
        parenthesis_depth = 0
        while index < len(value) and value[index] not in " \t\n":
            character = value[index]
            if (
                character == "\\"
                and index + 1 < len(value)
                and value[index + 1] in ASCII_MARKDOWN_PUNCTUATION
            ):
                index += 2
                continue
            if character == "(":
                parenthesis_depth += 1
                if parenthesis_depth > 32:
                    return LINK_DEFINITION_INVALID
            elif character == ")":
                if not parenthesis_depth:
                    return LINK_DEFINITION_INVALID
                parenthesis_depth -= 1
            if ord(character) < 0x20 or ord(character) == 0x7F:
                return LINK_DEFINITION_INVALID
            index += 1
        if index == destination_start or parenthesis_depth:
            return LINK_DEFINITION_INVALID

    if index == len(value):
        return LINK_DEFINITION_VALID
    if value[index] not in " \t\n":
        return LINK_DEFINITION_INVALID
    while index < len(value) and value[index] in " \t":
        index += 1
    if index == len(value):
        return LINK_DEFINITION_VALID
    if value[index] == "\n":
        index += 1
        while index < len(value) and value[index] in " \t":
            index += 1
        if index == len(value):
            return LINK_DEFINITION_VALID
        if value[index] == "\n":
            return LINK_DEFINITION_INVALID

    opener = value[index]
    if opener not in "\"'(":
        return LINK_DEFINITION_INVALID
    closer = ")" if opener == "(" else opener
    index += 1
    while index < len(value):
        character = value[index]
        if (
            character == "\\"
            and index + 1 < len(value)
            and value[index + 1] in ASCII_MARKDOWN_PUNCTUATION
        ):
            index += 2
            continue
        if opener == "(" and character == "(":
            return LINK_DEFINITION_INVALID
        if character == closer:
            index += 1
            if value[index:].strip(" \t"):
                return LINK_DEFINITION_INVALID
            if title_present is not None:
                title_present[0] = True
            return LINK_DEFINITION_VALID
        index += 1
    return LINK_DEFINITION_INCOMPLETE


def _link_definition_status(value: str) -> int:
    return _parse_link_definition_status(value)


def _link_definition_has_inline_title(value: str) -> bool:
    title_present: list[bool] = []
    status = _parse_link_definition_status(value, title_present)
    return status == LINK_DEFINITION_VALID and title_present[0]


def _is_link_definition(value: str) -> bool:
    return _link_definition_status(value) == LINK_DEFINITION_VALID


def _normalize_link_label(value: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", value.strip(" \t\r\n")).casefold()


def _line_opens_paragraph(line: str) -> bool:
    return not _line_interrupts_paragraph(line)


def _line_interrupts_open_paragraph(line: str) -> bool:
    if _indent_columns(line) >= 4:
        return False
    candidate = line.strip()
    return bool(
        ATX_HEADING_RE.fullmatch(candidate)
        or THEMATIC_BREAK_RE.fullmatch(candidate)
        or FENCE_RE.fullmatch(line)
        or BLOCKQUOTE_RE.match(line)
    )


def _line_interrupts_paragraph(line: str) -> bool:
    return _line_interrupts_open_paragraph(line) or _is_link_definition(line)


def _structural_lines(
    value: str,
) -> list[tuple[str, bool, re.Match[str] | None]]:
    """Classify visible lines without treating nested list content as top-level."""

    classified: list[tuple[str, bool, re.Match[str] | None]] = []
    list_content_indents: list[int] = []
    list_paragraphs: list[bool] = []
    previous_line_blank = True
    blockquote_paragraph = False
    blockquote_list_indent: int | None = None
    paragraph_open = False
    for line in _markdown_lines(value):
        if not line.strip():
            classified.append((line, False, None))
            previous_line_blank = True
            blockquote_paragraph = False
            blockquote_list_indent = None
            paragraph_open = False
            if list_paragraphs:
                list_paragraphs[-1] = False
            continue

        if BLOCKQUOTE_RE.match(line):
            classified.append((line, False, None))
            previous_line_blank = False
            blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(line)
            blockquote_content = blockquote.group("value") if blockquote else ""
            blockquote_paragraph = bool(
                blockquote
                and (
                    blockquote_paragraph
                    and bool(blockquote_content.strip())
                    and not _line_starts_paragraph_interrupt(
                        blockquote_content
                    )
                    or _blockquote_allows_lazy_continuation(
                        blockquote_content
                    )
                )
            )
            blockquote_list_indent = None
            paragraph_open = False
            if list_paragraphs:
                list_paragraphs[-1] = False
            continue

        indent = _indent_columns(line)
        list_match = STRUCTURAL_LIST_ITEM_RE.fullmatch(line)
        if list_match and indent >= 4:
            remaining_containers = len(list_content_indents)
            while (
                remaining_containers
                and indent < list_content_indents[remaining_containers - 1]
            ):
                remaining_containers -= 1
            if not remaining_containers:
                list_match = None
        if list_match:
            exits_nested_blockquote = bool(
                blockquote_list_indent is not None
                and indent < blockquote_list_indent
            )
            if (
                blockquote_paragraph
                and not exits_nested_blockquote
                and not _list_can_interrupt_paragraph(list_match)
            ):
                classified.append((line, False, None))
                previous_line_blank = False
                paragraph_open = False
                continue
            blockquote_paragraph = False
            blockquote_list_indent = None
            if (
                paragraph_open
                and not list_content_indents
                and not _list_can_interrupt_paragraph(list_match)
            ):
                classified.append((line, True, None))
                previous_line_blank = False
                continue
            blockquote_paragraph = False
            while list_content_indents and indent < list_content_indents[-1]:
                list_content_indents.pop()
                list_paragraphs.pop()
            top_level = not list_content_indents
            classified.append((line, top_level, list_match))
            content_indent = _list_content_indent(line, list_match)
            list_content_indents.append(content_indent)
            marker_value = _list_marker_value(line, list_match)
            nested_blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(marker_value)
            blockquote_paragraph = bool(
                nested_blockquote
                and _blockquote_allows_lazy_continuation(
                    nested_blockquote.group("value")
                )
            )
            blockquote_list_indent = (
                content_indent if blockquote_paragraph else None
            )
            list_paragraphs.append(
                bool(marker_value.strip())
                and not _list_marker_starts_indented_code(line, list_match)
                and not _line_interrupts_paragraph(marker_value)
            )
            previous_line_blank = not marker_value.strip()
            paragraph_open = False
            continue

        while (
            list_content_indents
            and indent < list_content_indents[-1]
            and not list_paragraphs[-1]
        ):
            list_content_indents.pop()
            list_paragraphs.pop()
        relative_line = (
            _drop_indent_columns(line, list_content_indents[-1])
            if list_content_indents and indent >= list_content_indents[-1]
            else line
        )
        paragraph_interrupt = (
            _line_interrupts_open_paragraph(relative_line)
            if list_paragraphs and list_paragraphs[-1]
            else _line_interrupts_paragraph(relative_line)
        )
        if blockquote_paragraph and not paragraph_interrupt:
            classified.append((line, False, None))
            previous_line_blank = False
            paragraph_open = False
            continue
        if paragraph_interrupt:
            blockquote_paragraph = False
            blockquote_list_indent = None

        if previous_line_blank or paragraph_interrupt:
            while list_content_indents and indent < list_content_indents[-1]:
                list_content_indents.pop()
                list_paragraphs.pop()
        classified.append((line, not list_content_indents, None))
        previous_line_blank = False
        if list_content_indents:
            list_paragraphs[-1] = bool(
                relative_line.strip()
                and not paragraph_interrupt
                and (
                    list_paragraphs[-1]
                    or _indent_columns(relative_line) < 4
                )
            )
            paragraph_open = False
        else:
            paragraph_open = (
                indent < 4
                and not paragraph_interrupt
                and _line_opens_paragraph(line)
            )
    return classified


def _mask_comment_spans(
    line: str,
    in_comment: bool,
    *,
    line_offset: int = 0,
    ambiguous_comment_offsets: list[int] | None = None,
) -> tuple[str, bool]:
    characters = list(line)
    cursor = 0
    while cursor < len(line):
        if in_comment:
            end = line.find("-->", cursor)
            stop = len(line) if end == -1 else end + 3
            for index in range(cursor, stop):
                if characters[index] not in {"\r", "\n"}:
                    characters[index] = " "
            if end == -1:
                return "".join(characters), True
            in_comment = False
            cursor = stop
            continue
        start = line.find("<!--", cursor)
        if start == -1:
            break
        if ambiguous_comment_offsets is not None and line[:start].strip():
            ambiguous_comment_offsets.append(line_offset + start)
        cursor = start
        in_comment = True
    return "".join(characters), in_comment


def _strip_blockquote_prefixes(line: str) -> tuple[str, int]:
    candidate = line
    depth = 0
    while True:
        blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
        if not blockquote:
            return candidate, depth
        candidate = blockquote.group("value")
        depth += 1


def _container_line_info(line: str) -> tuple[str, bool, int, int]:
    candidate, blockquote_depth = _strip_blockquote_prefixes(line)
    list_indent = 0
    while True:
        list_match = STRUCTURAL_LIST_ITEM_RE.fullmatch(candidate)
        if not list_match:
            break
        list_indent += _list_content_indent(candidate, list_match)
        candidate = _list_marker_value(candidate, list_match)
    return (
        candidate,
        bool(blockquote_depth or list_indent),
        blockquote_depth,
        list_indent,
    )


def _effective_container_line_info(
    line: str,
    structural_list_match: re.Match[str] | None,
    *,
    blockquote_list_rendered: bool = True,
) -> tuple[str, bool, int, int]:
    relative_line, nested, blockquote_depth, list_indent = _container_line_info(line)
    if list_indent and not blockquote_depth and structural_list_match is None:
        return line, False, 0, 0
    if list_indent and blockquote_depth and not blockquote_list_rendered:
        quoted_line, _depth = _strip_blockquote_prefixes(line)
        return quoted_line, True, blockquote_depth, 0
    return relative_line, nested, blockquote_depth, list_indent


def _blockquote_list_rendering(value: str) -> list[bool]:
    rendered_flags: list[bool] = []
    active_depth = 0
    paragraph_open = False
    for line in _markdown_lines(value):
        candidate, blockquote_depth = _strip_blockquote_prefixes(line)
        if not blockquote_depth:
            rendered_flags.append(True)
            active_depth = 0
            paragraph_open = False
            continue
        if blockquote_depth != active_depth:
            active_depth = blockquote_depth
            paragraph_open = False
        list_match = STRUCTURAL_LIST_ITEM_RE.fullmatch(candidate)
        if list_match:
            rendered = not paragraph_open or _list_can_interrupt_paragraph(
                list_match
            )
            rendered_flags.append(rendered)
            if rendered:
                paragraph_open = False
            continue
        rendered_flags.append(True)
        stripped = candidate.strip()
        if not stripped:
            paragraph_open = False
        elif (
            HEADING_RE.fullmatch(candidate)
            or THEMATIC_BREAK_RE.fullmatch(stripped)
            or _is_link_definition(candidate)
            or FENCE_RE.fullmatch(candidate)
        ):
            paragraph_open = False
        else:
            paragraph_open = True
    return rendered_flags


def _line_in_container(
    line: str, *, blockquote_depth: int, list_indent: int
) -> str | None:
    candidate = line
    for _ in range(blockquote_depth):
        blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
        if not blockquote:
            return None
        candidate = blockquote.group("value")
    if list_indent:
        if not candidate.strip():
            return ""
        if _indent_columns(candidate) < list_indent:
            return None
        candidate = _drop_indent_columns(candidate, list_indent)
    return candidate


def _ordered_container_line_info(
    line: str,
    structural_list_match: re.Match[str] | None,
    *,
    blockquote_list_rendered: bool,
    allow_unclassified_list: bool = False,
) -> tuple[str, tuple[tuple[str, int], ...], bool]:
    candidate = line
    prefixes: list[tuple[str, int]] = []
    has_list_marker = False
    while True:
        blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
        if blockquote:
            prefixes.append(("blockquote", 0))
            candidate = blockquote.group("value")
            continue

        list_match = STRUCTURAL_LIST_ITEM_RE.fullmatch(candidate)
        if not list_match:
            break
        if (
            not prefixes
            and structural_list_match is None
            and not allow_unclassified_list
        ):
            break
        if (
            prefixes
            and not has_list_marker
            and not blockquote_list_rendered
        ):
            break
        prefixes.append(("list", _list_content_indent(candidate, list_match)))
        candidate = _list_marker_value(candidate, list_match)
        has_list_marker = True
    return candidate, tuple(prefixes), has_list_marker


def _line_in_ordered_container(
    line: str, prefixes: Sequence[tuple[str, int]]
) -> str | None:
    candidate = line
    for kind, indent in prefixes:
        if kind == "blockquote":
            blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
            if not blockquote:
                return None
            candidate = blockquote.group("value")
            continue
        if kind != "list":
            return None
        if not candidate.strip():
            return ""
        if _indent_columns(candidate) < indent:
            return None
        candidate = _drop_indent_columns(candidate, indent)
    return candidate


def _line_in_ordered_container_prefix(
    line: str, prefixes: Sequence[tuple[str, int]]
) -> tuple[str, tuple[tuple[str, int], ...]] | None:
    """Return content relative to the longest matching container prefix."""

    candidate = line.expandtabs(4)
    consumed: list[tuple[str, int]] = []
    for kind, indent in prefixes:
        if kind == "blockquote":
            blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
            if not blockquote:
                return (candidate, tuple(consumed)) if consumed else None
            candidate = blockquote.group("value")
            consumed.append((kind, indent))
            continue
        if kind != "list":
            return None
        if not candidate.strip():
            return "", tuple(prefixes)
        if _indent_columns(candidate) < indent:
            return (candidate, tuple(consumed)) if consumed else None
        leading_columns = _indent_columns(candidate)
        leading_length = len(candidate) - len(candidate.lstrip(" \t"))
        candidate = (
            " " * (leading_columns - indent) + candidate[leading_length:]
        )
        consumed.append((kind, indent))
    return candidate, tuple(consumed)


def _line_in_ordered_marker_prefix(
    line: str, prefixes: Sequence[tuple[str, int]]
) -> tuple[str, tuple[tuple[str, int], ...]]:
    """Consume rendered marker prefixes without promoting indented code."""

    candidate = line.expandtabs(4)
    consumed: list[tuple[str, int]] = []
    for kind, indent in prefixes:
        if kind == "blockquote":
            blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
            if not blockquote:
                break
            candidate = blockquote.group("value")
            consumed.append((kind, indent))
            continue
        if kind != "list" or _indent_columns(candidate) >= 4:
            break
        list_match = STRUCTURAL_LIST_ITEM_RE.fullmatch(candidate)
        if not list_match:
            break
        candidate = _list_marker_value(candidate, list_match)
        consumed.append((kind, indent))
    return candidate, tuple(consumed)


def _line_starts_paragraph_interrupt(line: str) -> bool:
    list_match = STRUCTURAL_LIST_ITEM_RE.fullmatch(line)
    return bool(
        _line_interrupts_open_paragraph(line)
        or (
            list_match
            and _indent_columns(line) < 4
            and _list_can_interrupt_paragraph(list_match)
        )
    )


def _line_in_open_paragraph_container(
    line: str, prefixes: Sequence[tuple[str, int]]
) -> str | None:
    candidate = line
    if not prefixes and _line_starts_paragraph_interrupt(candidate):
        return None
    for kind, indent in prefixes:
        if kind == "blockquote":
            blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
            if blockquote:
                candidate = blockquote.group("value")
            elif _line_starts_paragraph_interrupt(candidate):
                return None
            continue
        if kind != "list":
            return None
        if not candidate.strip():
            return ""
        if _indent_columns(candidate) >= indent:
            candidate = _drop_indent_columns(candidate, indent)
        elif _line_starts_paragraph_interrupt(candidate):
            return None
    return None if _line_starts_paragraph_interrupt(candidate) else candidate


def _scan_markdown(
    value: str,
    *,
    mask_code: bool,
    mask_indented_code: bool | None = None,
    ambiguous_comment_offsets: list[int] | None = None,
) -> str:
    """Mask comments and optionally code using one ordered Markdown state machine."""

    if mask_indented_code is None:
        mask_indented_code = mask_code
    output: list[str] = []
    fence_character = ""
    fence_length = 0
    fence_blockquote_depth = 0
    fence_list_indent = 0
    in_comment = False
    offset = 0
    structural_lines = iter(_structural_lines(value))
    blockquote_list_rendering = _blockquote_list_rendering(value)
    for line_index, line in enumerate(_markdown_lines(value, keepends=True)):
        line_without_ending = line.rstrip("\r\n")
        _classified_line, _top_level, structural_list_match = next(
            structural_lines
        )
        if fence_character:
            relative_line = _line_in_container(
                line_without_ending,
                blockquote_depth=fence_blockquote_depth,
                list_indent=fence_list_indent,
            )
            if relative_line is not None:
                output.append(_masked_line(line) if mask_code else line)
                fence_match = FENCE_RE.match(relative_line)
                if fence_match:
                    run = fence_match.group("run")
                    if (
                        run[0] == fence_character
                        and len(run) >= fence_length
                        and not fence_match.group("rest").strip()
                    ):
                        fence_character = ""
                        fence_length = 0
                        fence_blockquote_depth = 0
                        fence_list_indent = 0
                offset += len(line)
                continue
            fence_character = ""
            fence_length = 0
            fence_blockquote_depth = 0
            fence_list_indent = 0
        relative_line, _nested, blockquote_depth, list_indent = (
            _effective_container_line_info(
                line_without_ending,
                structural_list_match,
                blockquote_list_rendered=blockquote_list_rendering[line_index],
            )
        )
        fence_match = FENCE_RE.match(relative_line)
        if in_comment:
            masked, in_comment = _mask_comment_spans(
                line,
                in_comment,
                line_offset=offset,
                ambiguous_comment_offsets=ambiguous_comment_offsets,
            )
            output.append(masked)
            offset += len(line)
            continue
        if fence_match:
            run = fence_match.group("run")
            rest = fence_match.group("rest")
            if run[0] != "`" or "`" not in rest:
                fence_character = run[0]
                fence_length = len(run)
                fence_blockquote_depth = blockquote_depth
                fence_list_indent = list_indent
                output.append(_masked_line(line) if mask_code else line)
                offset += len(line)
                continue
        if _indent_columns(line) >= 4:
            if ambiguous_comment_offsets is not None:
                cursor = 0
                while True:
                    comment_start = line.find("<!--", cursor)
                    if comment_start == -1:
                        break
                    ambiguous_comment_offsets.append(offset + comment_start)
                    cursor = comment_start + 4
            if mask_indented_code:
                output.append(_masked_line(line) if mask_code else line)
            else:
                masked, in_comment = _mask_comment_spans(
                    line,
                    False,
                    line_offset=offset,
                    ambiguous_comment_offsets=ambiguous_comment_offsets,
                )
                output.append(masked)
        else:
            masked, in_comment = _mask_comment_spans(
                line,
                False,
                line_offset=offset,
                ambiguous_comment_offsets=ambiguous_comment_offsets,
            )
            output.append(masked)
        offset += len(line)
    return "".join(output)


def _scan_visible_markdown(
    value: str, ambiguous_comment_offsets: list[int] | None = None
) -> str:
    return _mask_top_level_indented_code(
        _scan_markdown(
            value,
            mask_code=True,
            mask_indented_code=False,
            ambiguous_comment_offsets=ambiguous_comment_offsets,
        )
    )


def _multiline_link_title_end(
    lines: Sequence[str],
    start: int,
    *,
    blockquote_depth: int,
    list_indent: int,
    container_prefixes: Sequence[tuple[str, int]] | None = None,
    starts_list_items: Sequence[bool] | None = None,
) -> int | None:
    if starts_list_items is not None and starts_list_items[start]:
        return None
    title = (
        _line_in_ordered_container(lines[start], container_prefixes)
        if container_prefixes is not None
        else _line_in_container(
            lines[start],
            blockquote_depth=blockquote_depth,
            list_indent=list_indent,
        )
    )
    if title is None and container_prefixes is not None:
        title = _line_in_open_paragraph_container(
            lines[start], container_prefixes
        )
    if not title:
        return None
    title = title.lstrip(" \t")
    if not title.startswith(("\"", "'", "(")):
        return None

    opener = title[0]
    closer = ")" if opener == "(" else opener
    cursor = start
    title = title[1:]
    while True:
        index = 0
        while index < len(title):
            character = title[index]
            if (
                character == "\\"
                and index + 1 < len(title)
                and title[index + 1] in ASCII_MARKDOWN_PUNCTUATION
            ):
                index += 2
                continue
            if opener == "(" and character == "(":
                return None
            if character == closer:
                return cursor if not title[index + 1 :].strip(" \t") else None
            index += 1

        cursor += 1
        if cursor >= len(lines):
            return None
        if starts_list_items is not None and starts_list_items[cursor]:
            return None
        title = (
            _line_in_ordered_container(lines[cursor], container_prefixes)
            if container_prefixes is not None
            else _line_in_container(
                lines[cursor],
                blockquote_depth=blockquote_depth,
                list_indent=list_indent,
            )
        )
        if title is None and container_prefixes is not None:
            title = _line_in_open_paragraph_container(
                lines[cursor], container_prefixes
            )
        if title is None or not title.strip():
            return None
        if _line_starts_paragraph_interrupt(title):
            return None


def _link_definition_line_ranges(
    value: str, labels: list[str] | None = None
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    candidate = ""
    candidate_prefixes: tuple[tuple[str, int], ...] = ()
    lines = _markdown_lines(value)
    classified = _structural_lines(value)
    blockquote_list_rendering = _blockquote_list_rendering(value)
    ordered_lines = [
        _ordered_container_line_info(
            line,
            classified[index][2],
            blockquote_list_rendered=blockquote_list_rendering[index],
        )
        for index, line in enumerate(lines)
    ]
    starts_list_items = tuple(item[2] for item in ordered_lines)
    skip_through = -1
    paragraph_open = False
    active_prefixes: tuple[tuple[str, int], ...] = ()
    for index, line in enumerate(lines):
        if index <= skip_through:
            continue
        relative_line, current_prefixes, starts_list_item = ordered_lines[index]
        if paragraph_open and not starts_list_item:
            lazy_continuation = _line_in_open_paragraph_container(
                line, active_prefixes
            )
            if lazy_continuation is not None:
                four_space_nested_list = False
                if active_prefixes and _indent_columns(lazy_continuation) >= 4:
                    nested_candidate = _drop_indent_columns(
                        lazy_continuation, 4
                    )
                    (
                        nested_relative,
                        _nested_prefixes,
                        nested_list_item,
                    ) = _ordered_container_line_info(
                        nested_candidate,
                        None,
                        blockquote_list_rendered=True,
                        allow_unclassified_list=True,
                    )
                    if (
                        nested_list_item
                        and BLOCKQUOTE_RE.match(nested_candidate)
                    ):
                        relative_line = nested_relative
                        current_prefixes = active_prefixes
                        starts_list_item = True
                        four_space_nested_list = True
                if not four_space_nested_list:
                    relative_line = lazy_continuation
                    current_prefixes = active_prefixes
                    starts_list_item = False
            elif active_prefixes:
                strict_continuation = _line_in_ordered_container(
                    line, active_prefixes
                )
                if strict_continuation is not None:
                    (
                        nested_relative,
                        nested_prefixes,
                        nested_list_item,
                    ) = _ordered_container_line_info(
                        strict_continuation,
                        None,
                        blockquote_list_rendered=True,
                        allow_unclassified_list=True,
                    )
                    if nested_prefixes:
                        relative_line = nested_relative
                        current_prefixes = active_prefixes + nested_prefixes
                        starts_list_item = nested_list_item
                elif _indent_columns(line) >= 4:
                    nested_candidate = _drop_indent_columns(line, 4)
                    (
                        nested_relative,
                        _nested_prefixes,
                        nested_list_item,
                    ) = _ordered_container_line_info(
                        nested_candidate,
                        None,
                        blockquote_list_rendered=True,
                        allow_unclassified_list=True,
                    )
                    if (
                        nested_list_item
                        and BLOCKQUOTE_RE.match(nested_candidate)
                    ):
                        relative_line = nested_relative
                        current_prefixes = active_prefixes
                        starts_list_item = True
        container_changed = current_prefixes != active_prefixes
        opener = bool(re.match(r"^ {0,3}\[", relative_line))
        if start is None:
            if starts_list_item or container_changed:
                paragraph_open = False
            active_prefixes = current_prefixes
            if not relative_line.strip():
                paragraph_open = False
                continue
            if not opener or paragraph_open:
                if not opener:
                    paragraph_open = bool(
                        paragraph_open
                        and not _line_interrupts_open_paragraph(relative_line)
                        or not paragraph_open
                        and _indent_columns(relative_line) < 4
                        and not _line_interrupts_paragraph(relative_line)
                    )
                continue
            start = index
            candidate = relative_line
            candidate_prefixes = current_prefixes
        else:
            continuation = None
            if not starts_list_item:
                continuation = _line_in_ordered_container(
                    line, candidate_prefixes
                )
            if (
                continuation is not None
                and _line_starts_paragraph_interrupt(continuation)
            ):
                continuation = None
            if continuation is None and not starts_list_item:
                continuation = _line_in_open_paragraph_container(
                    line, candidate_prefixes
                )
            if not line.strip() or continuation is None:
                start = None
                candidate = ""
                candidate_prefixes = ()
                if not relative_line.strip():
                    paragraph_open = False
                    active_prefixes = current_prefixes
                    continue
                paragraph_open = False
                active_prefixes = current_prefixes
                if not opener:
                    paragraph_open = bool(
                        _indent_columns(relative_line) < 4
                        and not _line_interrupts_paragraph(relative_line)
                    )
                    continue
                start = index
                candidate = relative_line
                candidate_prefixes = current_prefixes
            elif opener:
                if current_prefixes != candidate_prefixes or starts_list_item:
                    start = index
                    candidate = relative_line
                    candidate_prefixes = current_prefixes
                    paragraph_open = False
                else:
                    start = None
                    candidate = ""
                    candidate_prefixes = ()
                    paragraph_open = True
                active_prefixes = current_prefixes
                if start is None:
                    continue
            else:
                candidate += "\n" + continuation

        status = _link_definition_status(candidate)
        if status == LINK_DEFINITION_VALID:
            end = index
            if (
                index + 1 < len(lines)
                and not _link_definition_has_inline_title(candidate)
            ):
                title_end = _multiline_link_title_end(
                    lines,
                    index + 1,
                    blockquote_depth=0,
                    list_indent=0,
                    container_prefixes=candidate_prefixes,
                    starts_list_items=starts_list_items,
                )
                if title_end is not None:
                    end = title_end
            ranges.append((start + 1, end + 1))
            if labels is not None:
                label: list[str] = []
                _parse_link_definition_status(candidate, label=label)
                labels.append(_normalize_link_label(label[0]))
            skip_through = end
            start = None
            candidate = ""
            candidate_prefixes = ()
            paragraph_open = False
            active_prefixes = current_prefixes
        elif status == LINK_DEFINITION_INVALID or len(candidate) > 4096:
            start = None
            candidate = ""
            candidate_prefixes = ()
            paragraph_open = True
            active_prefixes = current_prefixes
    return ranges


def _link_definition_start_lines(
    value: str, labels: list[str] | None = None
) -> list[int]:
    return [start for start, _end in _link_definition_line_ranges(value, labels)]


def _mask_line_ranges(value: str, ranges: Sequence[tuple[int, int]]) -> str:
    if not ranges:
        return value
    masked_lines: list[str] = []
    for line_number, line in enumerate(_markdown_lines(value, keepends=True), start=1):
        if any(start <= line_number <= end for start, end in ranges):
            masked_lines.append(_masked_line(line))
        else:
            masked_lines.append(line)
    return "".join(masked_lines)


def _content_without_comments(value: str) -> str:
    return _scan_markdown(value, mask_code=False).strip()


def _mask_inline_code_spans(value: str) -> str:
    """Mask complete inline code spans while preserving line positions."""

    characters = list(value)
    cursor = 0
    while cursor < len(value):
        if value[cursor] != "`" or _backslash_escaped(value, cursor):
            cursor += 1
            continue
        opener_end = cursor
        while opener_end < len(value) and value[opener_end] == "`":
            opener_end += 1
        run_length = opener_end - cursor
        search = opener_end
        closer_end: int | None = None
        while search < len(value):
            next_tick = value.find("`", search)
            if next_tick == -1:
                break
            candidate_end = next_tick
            while candidate_end < len(value) and value[candidate_end] == "`":
                candidate_end += 1
            if candidate_end - next_tick == run_length:
                closer_end = candidate_end
                break
            search = candidate_end
        if closer_end is None:
            cursor = opener_end
            continue
        for index in range(cursor, closer_end):
            if characters[index] not in {"\r", "\n"}:
                characters[index] = " "
        cursor = closer_end
    return "".join(characters)


def _backslash_escaped(value: str, offset: int) -> bool:
    backslashes = 0
    cursor = offset - 1
    while cursor >= 0 and value[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _drop_indent_columns(value: str, columns: int) -> str:
    cursor = 0
    width = 0
    while cursor < len(value) and width < columns:
        character = value[cursor]
        if character == " ":
            width += 1
        elif character == "\t":
            width += 4 - (width % 4)
        else:
            break
        cursor += 1
    return value[cursor:]


def _mask_top_level_indented_code(value: str) -> str:
    """Mask indented code while retaining list-relative Markdown content."""

    output: list[str] = []
    list_content_indents: list[int] = []
    previous_line_blank = True
    in_indented_code = False
    paragraph_open = False
    fence_character = ""
    fence_length = 0
    for line in _markdown_lines(value, keepends=True):
        line_without_ending = line.rstrip("\r\n")
        if not line_without_ending.strip():
            output.append(line)
            previous_line_blank = True
            paragraph_open = False
            continue

        indent = _indent_columns(line_without_ending)
        relative_line = _drop_indent_columns(
            line_without_ending,
            list_content_indents[-1] if list_content_indents else 0,
        )
        if fence_character:
            output.append(_masked_line(line))
            fence_match = FENCE_RE.match(relative_line)
            if fence_match:
                run = fence_match.group("run")
                if (
                    run[0] == fence_character
                    and len(run) >= fence_length
                    and not fence_match.group("rest").strip()
                ):
                    fence_character = ""
                    fence_length = 0
            previous_line_blank = False
            paragraph_open = False
            continue

        list_match = STRUCTURAL_LIST_ITEM_RE.fullmatch(line_without_ending)
        if list_match and not list_content_indents and indent >= 4:
            list_match = None
        if (
            list_match
            and not list_content_indents
            and paragraph_open
            and not _list_can_interrupt_paragraph(list_match)
        ):
            list_match = None
        if list_match:
            while list_content_indents and indent < list_content_indents[-1]:
                list_content_indents.pop()
            list_content_indents.append(
                _list_content_indent(line_without_ending, list_match)
            )
            marker_value = _list_marker_value(line_without_ending, list_match)
            in_indented_code = _list_marker_starts_indented_code(
                line_without_ending, list_match
            )
            fence_match = None if in_indented_code else FENCE_RE.match(marker_value)
            if in_indented_code:
                output.append(_mask_list_marker_code(line, list_match))
            elif fence_match:
                run = fence_match.group("run")
                rest = fence_match.group("rest")
                if run[0] != "`" or "`" not in rest:
                    fence_character = run[0]
                    fence_length = len(run)
                    output.append(_masked_line(line))
                else:
                    output.append(line)
            else:
                output.append(line)
            previous_line_blank = not marker_value.strip()
            paragraph_open = False
            continue

        paragraph_interrupt = _line_interrupts_paragraph(line_without_ending)
        if previous_line_blank or paragraph_interrupt:
            while list_content_indents and indent < list_content_indents[-1]:
                list_content_indents.pop()
        relative_line = _drop_indent_columns(
            line_without_ending,
            list_content_indents[-1] if list_content_indents else 0,
        )
        fence_match = FENCE_RE.match(relative_line)
        if fence_match:
            run = fence_match.group("run")
            rest = fence_match.group("rest")
            if run[0] != "`" or "`" not in rest:
                fence_character = run[0]
                fence_length = len(run)
                output.append(_masked_line(line))
                previous_line_blank = False
                in_indented_code = False
                paragraph_open = False
                continue
        code_indent = (
            list_content_indents[-1] + 4 if list_content_indents else 4
        )
        if in_indented_code and indent < code_indent:
            in_indented_code = False
        can_start_indented_code = (
            previous_line_blank
            if list_content_indents
            else not paragraph_open
        )
        if (
            not in_indented_code
            and can_start_indented_code
            and indent >= code_indent
        ):
            in_indented_code = True
        output.append(_masked_line(line) if in_indented_code else line)
        previous_line_blank = False
        paragraph_open = (
            not in_indented_code
            and not list_content_indents
            and not paragraph_interrupt
            and _line_opens_paragraph(line_without_ending)
        )
    return "".join(output)


def _blockquote_relative_depth(line: str) -> tuple[str, int]:
    depth = 0
    candidate = line
    while True:
        blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
        if not blockquote:
            return candidate, depth
        candidate = blockquote.group("value")
        depth += 1


def _inline_soft_break_interrupts(current_line: str, next_line: str) -> bool:
    current_relative, current_depth = _blockquote_relative_depth(current_line)
    next_relative, next_depth = _blockquote_relative_depth(next_line)
    current_list = STRUCTURAL_LIST_ITEM_RE.fullmatch(current_relative)
    next_list = STRUCTURAL_LIST_ITEM_RE.fullmatch(next_relative)
    if (
        current_list
        and next_list
        and _indent_columns(current_relative) == _indent_columns(next_relative)
    ):
        return True
    if not _line_starts_paragraph_interrupt(next_line):
        return False
    return not (
        current_depth
        and current_depth == next_depth
        and bool(next_relative.strip())
        and not _line_starts_paragraph_interrupt(next_relative)
    )


def _inline_label_pairs(value: str) -> dict[int, int]:
    pairs: dict[int, int] = {}
    openers: list[int] = []
    cursor = 0
    while cursor < len(value):
        character = value[cursor]
        if character in "\r\n":
            line_end = cursor + 1
            if character == "\r" and line_end < len(value) and value[line_end] == "\n":
                line_end += 1
            next_line = line_end
            while next_line < len(value) and value[next_line] in " \t":
                next_line += 1
            next_line_end = next_line
            while next_line_end < len(value) and value[next_line_end] not in "\r\n":
                next_line_end += 1
            current_line_start = max(
                value.rfind("\n", 0, cursor), value.rfind("\r", 0, cursor)
            ) + 1
            current_line = value[current_line_start:cursor]
            next_line_value = value[line_end:next_line_end]
            if (
                next_line < len(value)
                and value[next_line] in "\r\n"
                or _inline_soft_break_interrupts(current_line, next_line_value)
            ):
                openers.clear()
            cursor = line_end - 1
        elif (
            character == "\\"
            and cursor + 1 < len(value)
            and value[cursor + 1] in ASCII_MARKDOWN_PUNCTUATION
        ):
            cursor += 1
        elif character == "[":
            openers.append(cursor)
        elif character == "]" and openers:
            pairs[openers.pop()] = cursor
        cursor += 1
    return pairs


def _inline_title_end(value: str, cursor: int) -> int | None:
    opener = value[cursor]
    closer = ")" if opener == "(" else opener
    cursor += 1
    while cursor < len(value):
        character = value[cursor]
        if character in "\r\n":
            line_end = cursor + 1
            if character == "\r" and line_end < len(value) and value[line_end] == "\n":
                line_end += 1
            next_line = line_end
            while next_line < len(value) and value[next_line] in " \t":
                next_line += 1
            next_line_end = next_line
            while next_line_end < len(value) and value[next_line_end] not in "\r\n":
                next_line_end += 1
            current_line_start = max(
                value.rfind("\n", 0, cursor), value.rfind("\r", 0, cursor)
            ) + 1
            if (
                next_line < len(value)
                and value[next_line] in "\r\n"
                or _inline_soft_break_interrupts(
                    value[current_line_start:cursor], value[line_end:next_line_end]
                )
            ):
                return None
            cursor = line_end
            continue
        if (
            character == "\\"
            and cursor + 1 < len(value)
            and value[cursor + 1] in ASCII_MARKDOWN_PUNCTUATION
        ):
            cursor += 2
            continue
        if character == closer:
            return cursor + 1
        if opener == "(" and character == "(":
            return None
        cursor += 1
    return None


def _inline_destination_end(value: str, cursor: int) -> int | None:
    while cursor < len(value) and value[cursor] in " \t":
        cursor += 1
    if cursor >= len(value):
        return None
    if value[cursor] == ")":
        return cursor + 1

    if value[cursor] == "<":
        cursor += 1
        while cursor < len(value):
            character = value[cursor]
            if character in "\r\n<":
                return None
            if (
                character == "\\"
                and cursor + 1 < len(value)
                and value[cursor + 1] in ASCII_MARKDOWN_PUNCTUATION
            ):
                cursor += 2
                continue
            if character == ">":
                cursor += 1
                break
            cursor += 1
        else:
            return None
    else:
        destination_start = cursor
        depth = 0
        while cursor < len(value):
            character = value[cursor]
            if (
                character == "\\"
                and cursor + 1 < len(value)
                and value[cursor + 1] in ASCII_MARKDOWN_PUNCTUATION
            ):
                cursor += 2
                continue
            if character in " \t\r\n":
                break
            if character == "(":
                depth += 1
                if depth > 32:
                    return None
            elif character == ")":
                if not depth:
                    return cursor + 1
                depth -= 1
            if ord(character) < 0x20 or ord(character) == 0x7F:
                return None
            cursor += 1
        if cursor == destination_start or depth:
            return None

    whitespace_start = cursor
    while cursor < len(value) and value[cursor] in " \t":
        cursor += 1
    if cursor < len(value) and value[cursor] == ")":
        return cursor + 1
    if cursor == whitespace_start or cursor >= len(value):
        return None
    if value[cursor] not in {'"', "'", "("}:
        return None
    title_end = _inline_title_end(value, cursor)
    if title_end is None:
        return None
    cursor = title_end
    while cursor < len(value) and value[cursor] in " \t":
        cursor += 1
    return cursor + 1 if cursor < len(value) and value[cursor] == ")" else None


def _inline_link_token(
    value: str,
    opener: int,
    label_end: int,
    *,
    allow_reference: bool,
    reference_labels: frozenset[str] = frozenset(),
    bracket_openers: Sequence[int] = (),
) -> tuple[int, int, int, bool, bool] | None:
    """Return end, label bounds, and image status for valid link syntax.

    Reference forms count only when their normalized label is defined.
    """

    delimiter = value[label_end + 1] if label_end + 1 < len(value) else ""
    end = None
    is_inline = delimiter == "("
    if is_inline:
        end = _inline_destination_end(value, label_end + 2)
    if end is None and allow_reference:
        is_inline = False
        reference_label = None
        if delimiter == "[":
            cursor = label_end + 2
            while cursor < len(value):
                character = value[cursor]
                if character in "\r\n":
                    return None
                if character == "[":
                    break
                if (
                    character == "\\"
                    and cursor + 1 < len(value)
                    and value[cursor + 1] in ASCII_MARKDOWN_PUNCTUATION
                ):
                    cursor += 2
                    continue
                if character == "]":
                    break
                cursor += 1
            if (
                cursor < len(value)
                and value[cursor] == "]"
                and cursor - label_end - 2 <= MAX_LINK_LABEL_CHARACTERS
            ):
                end = cursor + 1
                if cursor > label_end + 2:
                    reference_label = value[label_end + 2 : cursor]
        if reference_label is None:
            if end is None:
                end = label_end + 1
            inner_opener = bisect_right(bracket_openers, opener)
            if (
                label_end - opener - 1 > MAX_LINK_LABEL_CHARACTERS
                or inner_opener < len(bracket_openers)
                and bracket_openers[inner_opener] < label_end
            ):
                return None
            reference_label = value[opener + 1 : label_end]
        if _normalize_link_label(reference_label) not in reference_labels:
            return None
    if end is None:
        return None
    is_image = (
        opener > 0
        and value[opener - 1] == "!"
        and not _backslash_escaped(value, opener - 1)
    )
    return end, opener + 1, label_end, is_image, is_inline


def _inline_link_tokens(
    value: str,
    *,
    allow_reference: bool,
    reference_labels: frozenset[str] = frozenset(),
) -> list[tuple[int, int, int, int, bool, bool]]:
    tokens: list[tuple[int, int, int, int, bool, bool]] = []
    pairs = _inline_label_pairs(value)
    bracket_openers = sorted(pairs)
    for opener, label_end in pairs.items():
        token = _inline_link_token(
            value,
            opener,
            label_end,
            allow_reference=allow_reference and bool(reference_labels),
            reference_labels=reference_labels,
            bracket_openers=bracket_openers,
        )
        if token is not None:
            end, label_start, parsed_label_end, is_image, is_inline = token
            tokens.append(
                (
                    opener,
                    end,
                    label_start,
                    parsed_label_end,
                    is_image,
                    is_inline,
                )
            )
    # Drop links whose label contains an inline link; suffix minima of the
    # nested links' ends keep this check logarithmic per token.
    nested_links = sorted(
        (token[0], token[1]) for token in tokens if token[5] and not token[4]
    )
    nested_starts = [start for start, _end in nested_links]
    nested_min_ends = [end for _start, end in nested_links]
    for index in range(len(nested_min_ends) - 2, -1, -1):
        nested_min_ends[index] = min(
            nested_min_ends[index], nested_min_ends[index + 1]
        )
    return [
        token
        for token in tokens
        if token[4]
        or (index := bisect_left(nested_starts, token[2])) == len(nested_starts)
        or nested_min_ends[index] > token[3]
    ]


def _inline_link_destination(
    value: str, label_end: int, token_end: int
) -> str:
    cursor = label_end + 2
    while cursor < token_end and value[cursor] in " \t":
        cursor += 1
    if cursor >= token_end - 1 or value[cursor] == ")":
        return ""
    if value[cursor] == "<":
        destination_start = cursor + 1
        cursor = destination_start
        while cursor < token_end and value[cursor] != ">":
            if (
                value[cursor] == "\\"
                and cursor + 1 < token_end
                and value[cursor + 1] in ASCII_MARKDOWN_PUNCTUATION
            ):
                cursor += 2
            else:
                cursor += 1
        return value[destination_start:cursor]

    destination_start = cursor
    depth = 0
    while cursor < token_end:
        character = value[cursor]
        if (
            character == "\\"
            and cursor + 1 < token_end
            and value[cursor + 1] in ASCII_MARKDOWN_PUNCTUATION
        ):
            cursor += 2
            continue
        if character in " \t\r\n":
            break
        if character == "(":
            depth += 1
        elif character == ")":
            if not depth:
                break
            depth -= 1
        cursor += 1
    return value[destination_start:cursor]


def _mask_ranges(value: str, ranges: Sequence[tuple[int, int]]) -> str:
    deltas = [0] * (len(value) + 1)
    for start, end in ranges:
        deltas[start] += 1
        deltas[end] -= 1
    depth = 0
    characters: list[str] = []
    for index, character in enumerate(value):
        depth += deltas[index]
        characters.append(
            " " if depth and character not in {"\r", "\n"} else character
        )
    return "".join(characters)


def _inline_visibility_ranges(value: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for (
        opener,
        end,
        _label_start,
        label_end,
        is_image,
        _is_inline,
    ) in _inline_link_tokens(
        value,
        allow_reference=True,
        reference_labels=LINK_REFERENCE_LABELS.get(),
    ):
        if is_image:
            ranges.append((opener - 1, end))
        else:
            ranges.extend(((opener, opener + 1), (label_end, end)))
    return ranges


def _mask_inline_link_destinations(value: str) -> str:
    return _mask_ranges(
        value,
        [
            (label_end + 1, end)
            for _opener, end, _label_start, label_end, _is_image, _is_inline in (
                _inline_link_tokens(value, allow_reference=False)
            )
        ],
    )


def _raw_html_offsets(value: str) -> list[int]:
    candidates = _scan_markdown(
        value,
        mask_code=True,
        mask_indented_code=False,
    )
    candidates = _mask_inline_code_spans(
        _mask_top_level_indented_code(candidates)
    )
    candidates = _mask_inline_link_destinations(candidates)
    return [
        match.start()
        for match in RAW_HTML_START_RE.finditer(candidates)
        if not _backslash_escaped(candidates, match.start())
    ]


def _is_placeholder_value(value: str) -> bool:
    candidate = value.rstrip()
    if PLACEHOLDER_VALUE_RE.fullmatch(candidate):
        return True
    while candidate and unicodedata.category(candidate[-1]).startswith("P"):
        candidate = candidate[:-1].rstrip()
        if PLACEHOLDER_VALUE_RE.fullmatch(candidate):
            return True
    return False


def _without_terminal_punctuation(value: str) -> str:
    candidate = value.rstrip()
    while candidate and unicodedata.category(candidate[-1]).startswith("P"):
        candidate = candidate[:-1].rstrip()
    return candidate


def _bounded_reported_version(value: str) -> str:
    if len(value) <= MAX_REPORTED_VERSION_CHARACTERS:
        return value
    return value[: MAX_REPORTED_VERSION_CHARACTERS - 1] + "…"


def _contains_placeholder_value(value: str) -> bool:
    for line in _markdown_lines(value):
        candidate = line.strip()
        if not candidate:
            continue
        while candidate:
            blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
            if blockquote:
                candidate = blockquote.group("value").strip()
                continue
            list_match = LIST_ITEM_RE.fullmatch(candidate)
            if list_match:
                candidate = list_match.group("value").strip()
                continue
            break
        if ANGLE_PLACEHOLDER_RE.fullmatch(candidate):
            return True
        candidate = _inline_visible_text(candidate).strip()
        if _is_placeholder_value(candidate) or ANGLE_PLACEHOLDER_RE.fullmatch(candidate):
            return True
    return False


def _inline_visible_text(value: str) -> str:
    value = _mask_ranges(value, _inline_visibility_ranges(value))
    value = re.sub(
        r"</?[A-Za-z][A-Za-z0-9-]*(?:[ \t][^>\r\n]*)?/?>",
        "",
        value,
    )
    value = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~\\])", r"\1", value)
    value = value.translate(str.maketrans("", "", "`*_~[]()"))
    value = html.unescape(value)
    return "".join(character for character in value if _is_visible_character(character))


def _is_visible_character(character: str) -> bool:
    category = unicodedata.category(character)
    if category in {"Cc", "Cf", "Cs"} or category.startswith("M"):
        return False
    code_point = ord(character)
    return not any(
        start <= code_point <= end
        for start, end in DEFAULT_IGNORABLE_CODE_POINT_RANGES
    )


def _visible_evidence_text(value: str) -> str:
    inline_candidates = _mask_inline_code_spans(
        _scan_markdown(value, mask_code=True, mask_indented_code=False)
    )
    value = _mask_ranges(value, _inline_visibility_ranges(inline_candidates))
    visible: list[str] = []
    fence_character = ""
    fence_length = 0
    fence_blockquote_depth = 0
    fence_list_indent = 0
    structural_lines = iter(_structural_lines(value))
    blockquote_list_rendering = _blockquote_list_rendering(value)
    for line_index, line in enumerate(_markdown_lines(value)):
        _classified_line, _top_level, structural_list_match = next(
            structural_lines
        )
        if fence_character:
            relative_line = _line_in_container(
                line,
                blockquote_depth=fence_blockquote_depth,
                list_indent=fence_list_indent,
            )
            if relative_line is not None:
                fence_match = FENCE_RE.match(relative_line)
                if fence_match:
                    run = fence_match.group("run")
                    if (
                        run[0] == fence_character
                        and len(run) >= fence_length
                        and not fence_match.group("rest").strip()
                    ):
                        fence_character = ""
                        fence_length = 0
                        fence_blockquote_depth = 0
                        fence_list_indent = 0
                        continue
                visible.append(
                    "".join(
                        character
                        for character in relative_line
                        if _is_visible_character(character)
                    )
                )
                continue
            fence_character = ""
            fence_length = 0
            fence_blockquote_depth = 0
            fence_list_indent = 0
        relative_line, _nested, blockquote_depth, list_indent = (
            _effective_container_line_info(
                line,
                structural_list_match,
                blockquote_list_rendered=blockquote_list_rendering[line_index],
            )
        )
        fence_match = FENCE_RE.match(relative_line)
        if fence_match:
            run = fence_match.group("run")
            rest = fence_match.group("rest")
            if run[0] != "`" or "`" not in rest:
                fence_character = run[0]
                fence_length = len(run)
                fence_blockquote_depth = blockquote_depth
                fence_list_indent = list_indent
                continue

        candidate = line.strip()
        while candidate:
            if THEMATIC_BREAK_RE.fullmatch(candidate):
                candidate = ""
                break
            list_match = LIST_ITEM_RE.fullmatch(candidate)
            if list_match:
                candidate = list_match.group("value").strip()
                continue
            checkbox = CHECKBOX_VALUE_RE.fullmatch(candidate)
            if checkbox:
                candidate = checkbox.group("value").strip()
                continue
            blockquote = BLOCKQUOTE_CONTENT_RE.fullmatch(candidate)
            if blockquote:
                candidate = blockquote.group("value").strip()
                continue
            if EMPTY_LIST_MARKER_RE.fullmatch(candidate):
                candidate = ""
            break
        heading = ATX_HEADING_RE.fullmatch(candidate)
        if heading:
            candidate = (heading.group("value") or "").strip()
            candidate = re.sub(r"[ \t]+#+[ \t]*$", "", candidate)
            if re.fullmatch(r"#+", candidate):
                candidate = ""
        if THEMATIC_BREAK_RE.fullmatch(candidate):
            candidate = ""
        visible.append(_inline_visible_text(candidate))
    return "\n".join(visible).strip()


def _top_level_candidate(
    line: str, list_match: re.Match[str] | None
) -> str:
    return _list_marker_value(line, list_match) if list_match else line.strip()


def _top_level_reference_blocks(value: str) -> list[str]:
    blocks: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(" ".join(paragraph))
            paragraph.clear()

    for line, top_level, list_match in _structural_lines(value):
        if not line.strip() or not top_level:
            flush_paragraph()
            continue
        candidate = _top_level_candidate(line, list_match)
        if list_match:
            flush_paragraph()
        else:
            paragraph.append(candidate)
    flush_paragraph()
    for marker_value, continuations in _top_level_list_items(value):
        parts = [
            part.strip()
            for part in (marker_value, *continuations)
            if part.strip()
        ]
        if parts:
            blocks.append(" ".join(parts))
    return blocks


def _has_issue_reference(value: str) -> bool:
    for candidate in _top_level_reference_blocks(value):
        candidate = _mask_inline_code_spans(candidate)
        visible_candidate = _inline_visible_text(candidate)
        if ISSUE_URL_RE.search(visible_candidate):
            return True
        for (
            _opener,
            end,
            label_start,
            label_end,
            is_image,
            _is_inline,
        ) in _inline_link_tokens(candidate, allow_reference=False):
            if is_image or not _inline_visible_text(
                candidate[label_start:label_end]
            ).strip():
                continue
            destination = _inline_link_destination(candidate, label_end, end)
            if ISSUE_URL_RE.fullmatch(destination):
                return True
        for match in BARE_REFERENCE_RE.finditer(visible_candidate):
            prefix = visible_candidate[: match.start()]
            if not PR_LABEL_RE.search(prefix):
                return True
    return False


def _has_change_reference(value: str) -> bool:
    for candidate in _top_level_reference_blocks(value):
        candidate = _mask_inline_code_spans(candidate)
        if CHANGE_REFERENCE_RE.search(_inline_visible_text(candidate)):
            return True
        for (
            _opener,
            end,
            label_start,
            label_end,
            is_image,
            _is_inline,
        ) in _inline_link_tokens(candidate, allow_reference=False):
            if is_image or not _inline_visible_text(
                candidate[label_start:label_end]
            ).strip():
                continue
            destination = _inline_link_destination(candidate, label_end, end)
            if CHANGE_URL_RE.fullmatch(destination):
                return True
    return False


def _absence_uses_markdown_container(value: str) -> bool:
    for line, top_level, list_match in _structural_lines(value):
        if not line.strip():
            continue
        candidate = _top_level_candidate(line, list_match)
        if ABSENCE_RE.fullmatch(_visible_evidence_text(candidate)):
            return list_match is not None or not top_level
    return False


def _top_level_list_items(value: str) -> list[tuple[str, tuple[str, ...]]]:
    """Return marker-line values and direct continuation lines for top-level items."""

    classified = _structural_lines(value)
    items: list[tuple[str, tuple[str, ...]]] = []
    for index, (line, top_level, list_match) in enumerate(classified):
        if not top_level or list_match is None:
            continue

        marker_value = _list_marker_value(line, list_match)
        marker_candidate = marker_value.strip()
        marker_opens_nested_block = bool(
            BLOCKQUOTE_RE.match(marker_value)
            or STRUCTURAL_LIST_ITEM_RE.fullmatch(marker_value)
            or EMPTY_LIST_MARKER_RE.fullmatch(marker_candidate)
            or ATX_HEADING_RE.fullmatch(marker_candidate)
            or THEMATIC_BREAK_RE.fullmatch(marker_candidate)
            or FENCE_RE.fullmatch(marker_value)
            or _is_link_definition(marker_value)
        )
        content_indent = _list_content_indent(line, list_match)
        continuations: list[str] = []
        for continuation, continuation_top_level, continuation_match in classified[
            index + 1 :
        ]:
            if marker_opens_nested_block:
                break
            if not continuation.strip():
                break
            if continuation_top_level or continuation_match is not None:
                break
            continuation_indent = _indent_columns(continuation)
            relative = (
                _drop_indent_columns(continuation, content_indent)
                if continuation_indent >= content_indent
                else continuation
            )
            candidate = relative.strip()
            if (
                BLOCKQUOTE_RE.match(relative)
                or STRUCTURAL_LIST_ITEM_RE.fullmatch(relative)
                or EMPTY_LIST_MARKER_RE.fullmatch(candidate)
                or ATX_HEADING_RE.fullmatch(candidate)
                or THEMATIC_BREAK_RE.fullmatch(candidate)
                or FENCE_RE.fullmatch(relative)
                or _is_link_definition(relative)
            ):
                break
            continuations.append(relative)

        items.append((marker_value, tuple(continuations)))
    return items


def _has_top_level_checklist_item(value: str) -> bool:
    for marker_value, continuations in _top_level_list_items(value):
        checkbox = CHECKBOX_ITEM_RE.fullmatch(marker_value)
        if checkbox and _visible_evidence_text(
            "\n".join(((checkbox.group("value") or ""), *continuations))
        ):
            return True
    return False


def _has_top_level_list_item(value: str) -> bool:
    return any(
        _visible_evidence_text("\n".join((marker_value, *continuations)))
        for marker_value, continuations in _top_level_list_items(value)
    )


def _line_starts(text: str) -> list[int]:
    return [0, *(match.end() for match in MARKDOWN_LINE_ENDING_RE.finditer(text))]


def _line_number(line_starts: Sequence[int], offset: int) -> int:
    return bisect_right(line_starts, offset)


def _setext_heading_findings(value: str) -> list[Finding]:
    lines = _markdown_lines(value)
    classified = _structural_lines(value)
    blockquote_list_rendering = _blockquote_list_rendering(value)
    findings: list[Finding] = []
    for index in range(1, len(lines)):
        previous = lines[index - 1]
        current = lines[index]
        (
            previous_relative,
            _previous_nested,
            previous_blockquote_depth,
            previous_list_indent,
        ) = _effective_container_line_info(
            previous,
            classified[index - 1][2],
            blockquote_list_rendered=blockquote_list_rendering[index - 1],
        )
        (
            current_relative,
            _current_nested,
            current_blockquote_depth,
            _current_list_indent,
        ) = _effective_container_line_info(
            current,
            classified[index][2],
            blockquote_list_rendered=blockquote_list_rendering[index],
        )
        nested_setext = False
        if (
            previous_blockquote_depth
            and not previous_list_indent
            and previous_blockquote_depth == current_blockquote_depth
            and previous_relative.strip()
            and HEADING_RE.fullmatch(previous_relative) is None
            and _line_opens_paragraph(previous_relative)
            and SETEXT_UNDERLINE_RE.fullmatch(current_relative)
        ):
            nested_setext = True
        elif previous_list_indent:
            continuation = _line_in_container(
                current,
                blockquote_depth=previous_blockquote_depth,
                list_indent=previous_list_indent,
            )
            nested_setext = bool(
                continuation is not None
                and previous_relative.strip()
                and HEADING_RE.fullmatch(previous_relative) is None
                and _line_opens_paragraph(previous_relative)
                and SETEXT_UNDERLINE_RE.fullmatch(continuation)
            )
        if nested_setext:
            findings.append(
                Finding(
                    "nested-heading",
                    "Contract headings must be top-level Markdown structures.",
                    index + 1,
                    _visible_evidence_text(previous_relative) or None,
                )
            )
            continue

        previous_line, previous_top_level, previous_list_match = classified[
            index - 1
        ]
        current_line, current_top_level, current_list_match = classified[index]
        if (
            current_top_level
            and current_list_match is None
            and SETEXT_UNDERLINE_RE.fullmatch(current_line)
            and previous_top_level
            and previous_list_match is None
            and previous_line.strip()
            and HEADING_RE.fullmatch(previous_line) is None
            and _line_opens_paragraph(previous_line)
        ):
            findings.append(
                Finding(
                    "setext-heading",
                    "Contract documents must not contain Setext-style Markdown headings.",
                    index + 1,
                )
            )
    return findings


def _parse_sections(
    text: str,
) -> tuple[list[tuple[str, int, int, int]], list[Finding]]:
    """Return heading, line, body-start, body-end tuples for level-three fields."""

    scan_text = _scan_visible_markdown(text)
    headings: list[tuple[str, int, int, int]] = []
    findings: list[Finding] = []
    offset = 0
    structural_lines = iter(_structural_lines(scan_text))
    blockquote_list_rendering = _blockquote_list_rendering(scan_text)
    active_list_prefixes: tuple[tuple[str, int], ...] = ()
    for line_index, line in enumerate(_markdown_lines(scan_text, keepends=True)):
        _classified_line, top_level, list_match = next(structural_lines)
        line_without_ending = line.rstrip("\r\n")
        relative_line, nested, _blockquote_depth, _list_indent = (
            _effective_container_line_info(
                line_without_ending,
                list_match,
                blockquote_list_rendered=blockquote_list_rendering[line_index],
            )
        )
        ordered_relative, ordered_prefixes, starts_list_item = (
            _ordered_container_line_info(
                line_without_ending,
                list_match,
                blockquote_list_rendered=blockquote_list_rendering[line_index],
            )
        )
        empty_heading_line = relative_line
        active_continuation = (
            _line_in_ordered_container_prefix(
                line_without_ending, active_list_prefixes
            )
            if active_list_prefixes
            else None
        )
        if active_continuation is not None:
            previous_list_prefixes = active_list_prefixes
            continuation, matched_prefixes = active_continuation
            active_list_prefixes = (
                matched_prefixes
                if any(kind == "list" for kind, _indent in matched_prefixes)
                else ()
            )
            empty_heading_line = continuation
            if continuation.strip() and _indent_columns(continuation) < 4:
                nested_relative, nested_prefixes, _nested_list = (
                    _ordered_container_line_info(
                        continuation,
                        None,
                        blockquote_list_rendered=True,
                        allow_unclassified_list=True,
                    )
                )
                if nested_prefixes:
                    marker_line, rendered_nested_prefixes = (
                        _line_in_ordered_marker_prefix(
                            continuation, nested_prefixes
                        )
                    )
                    empty_heading_line = marker_line
                    combined_prefixes = (
                        matched_prefixes + rendered_nested_prefixes
                    )
                    active_list_prefixes = (
                        combined_prefixes
                        if any(
                            kind == "list"
                            for kind, _indent in combined_prefixes
                        )
                        else ()
                    )
                elif (
                    not active_list_prefixes
                    and not top_level
                    and not _line_starts_paragraph_interrupt(continuation)
                ):
                    active_list_prefixes = previous_list_prefixes
        elif active_list_prefixes and not top_level:
            if _line_starts_paragraph_interrupt(line_without_ending):
                active_list_prefixes = ()
        elif starts_list_item:
            marker_line, rendered_prefixes = _line_in_ordered_marker_prefix(
                line_without_ending, ordered_prefixes
            )
            active_list_prefixes = (
                rendered_prefixes
                if any(kind == "list" for kind, _indent in rendered_prefixes)
                else ()
            )
            empty_heading_line = marker_line
        else:
            active_list_prefixes = ()
        match = HEADING_RE.match(line_without_ending)
        if match is None and nested:
            match = HEADING_RE.match(relative_line)
        empty_heading = EMPTY_ATX_HEADING_RE.fullmatch(empty_heading_line)
        if empty_heading:
            findings.append(
                Finding(
                    "empty-heading",
                    "Markdown headings must include text.",
                    line_index + 1,
                )
            )
            offset += len(line)
            continue
        if match:
            level = len(match.group(1))
            name = match.group(2)
            line_number = line_index + 1
            if nested or not top_level:
                findings.append(
                    Finding(
                        "nested-heading",
                        "Contract headings must be top-level Markdown structures.",
                        line_number,
                        name,
                    )
                )
            elif level != 3:
                findings.append(
                    Finding(
                        "unexpected-heading-level",
                        "Contract fields must use level-three Markdown headings.",
                        line_number,
                        name,
                    )
                )
            else:
                headings.append((name, line_number, offset, offset + len(line)))
        offset += len(line)

    sections: list[tuple[str, int, int, int]] = []
    for index, (name, line_number, _heading_start, body_start) in enumerate(headings):
        body_end = headings[index + 1][2] if index + 1 < len(headings) else len(text)
        sections.append((name, line_number, body_start, body_end))
    return sections, findings


def validate_text(
    text: str, *, kind: str, expected_version: str = CONTRACT_VERSION
) -> ValidationResult:
    token = LINK_REFERENCE_LABELS.set(frozenset())
    try:
        return _validate_text(text, kind=kind, expected_version=expected_version)
    finally:
        LINK_REFERENCE_LABELS.reset(token)


def _validate_text(
    text: str, *, kind: str, expected_version: str
) -> ValidationResult:
    if kind not in CONTRACTS:
        raise ValueError(f"unsupported evidence kind: {kind}")
    if expected_version != CONTRACT_VERSION:
        raise ValueError(f"unsupported contract version: {expected_version}")

    findings = FindingAccumulator()
    line_starts = _line_starts(text)
    if "\x00" in text:
        findings.append(Finding("nul-byte", "NUL bytes are not valid Markdown evidence."))
    if not text.strip():
        findings.append(Finding("empty-document", "Evidence document is empty."))
        return ValidationResult(False, kind, None, findings.freeze())
    unsupported_separator = UNSUPPORTED_LINE_SEPARATOR_RE.search(text)
    if unsupported_separator:
        findings.append(
            Finding(
                "unsupported-line-separator",
                "Markdown evidence may use only LF, CR, or CRLF line endings.",
                _line_number(line_starts, unsupported_separator.start()),
            )
        )

    ambiguous_comment_offsets: list[int] = []
    visible_document = _scan_visible_markdown(text, ambiguous_comment_offsets)
    for offset in ambiguous_comment_offsets:
        findings.append(
            Finding(
                "ambiguous-comment-opener",
                "HTML comment openers must begin a line; use fenced code for literal '<!--'.",
                _line_number(line_starts, offset),
            )
        )
    for offset in _raw_html_offsets(text):
        findings.append(
            Finding(
                "raw-html",
                "Raw HTML is not valid contract evidence; use Markdown or fenced code.",
                _line_number(line_starts, offset),
            )
        )
    reference_labels: list[str] = []
    for line_number in _link_definition_start_lines(
        visible_document, reference_labels
    ):
        findings.append(
            Finding(
                "non-rendered-link-definition",
                "Link reference definitions do not count as rendered field evidence; use an inline link.",
                line_number,
            )
        )
    LINK_REFERENCE_LABELS.set(frozenset(reference_labels))
    findings.extend(_setext_heading_findings(visible_document))

    definition = CONTRACTS[kind]
    if kind == "commit":
        text_lines = _markdown_lines(text)
        title_source = _content_without_comments(text_lines[0])
        title = _visible_evidence_text(title_source)
        if _contains_placeholder_value(title_source):
            findings.append(
                Finding(
                    "commit-title-placeholder",
                    "Commit title must replace the authoring placeholder with a delivered result.",
                    1,
                )
            )
        elif not title:
            findings.append(Finding("commit-title-empty", "Commit title is required.", 1))
        elif len(text_lines[0]) > 72:
            findings.append(
                Finding("commit-title-too-long", "Commit title must be 72 characters or fewer.", 1)
            )
        elif _without_terminal_punctuation(title).casefold() in GENERIC_COMMIT_TITLES:
            findings.append(
                Finding(
                    "commit-title-generic",
                    "Commit title must describe the result, not a generic activity.",
                    1,
                )
            )
        if len(text_lines) < 2 or text_lines[1].strip():
            findings.append(
                Finding("commit-title-separator", "Commit title must be followed by a blank line.", 2)
            )

    sections, heading_findings = _parse_sections(text)
    findings.extend(heading_findings)
    actual_headings = tuple(section[0] for section in sections)
    if actual_headings != definition.headings:
        findings.append(
            Finding(
                "heading-contract",
                "Expected headings in order: " + " | ".join(definition.headings),
            )
        )

    version_heading = re.search(
        r"(?m)^ {0,3}###[ \t]+Contract version[ \t]*$",
        _scan_visible_markdown(text),
    )
    if version_heading:
        preamble = text[: version_heading.start()]
        if kind == "commit":
            preamble_lines = _markdown_lines(preamble)
            extra_preamble = "\n".join(preamble_lines[1:]) if preamble_lines else ""
        else:
            extra_preamble = preamble
        if _content_without_comments(extra_preamble):
            findings.append(
                Finding(
                    "unexpected-preamble",
                    "Only the commit title may appear before the first contract field.",
                )
            )

    seen: set[str] = set()
    version: str | None = None
    for field, line_number, body_start, body_end in sections:
        if field in seen:
            findings.append(
                Finding("duplicate-heading", "Contract heading appears more than once.", line_number, field)
            )
        seen.add(field)
        if field not in definition.headings:
            findings.append(
                Finding("unknown-heading", "Heading is not part of this contract kind.", line_number, field)
            )
            continue

        raw_body = text[body_start:body_end]
        scanned_body = _scan_visible_markdown(raw_body)
        link_definition_ranges = _link_definition_line_ranges(scanned_body)
        body = _mask_line_ranges(
            _scan_markdown(raw_body, mask_code=False),
            link_definition_ranges,
        ).strip()
        visible_body = _mask_line_ranges(
            scanned_body,
            link_definition_ranges,
        ).strip()
        visible_evidence = _visible_evidence_text(body)
        visible_structure = _visible_evidence_text(visible_body)
        contains_placeholder = _contains_placeholder_value(visible_body)
        if not visible_evidence:
            findings.append(
                Finding(
                    "placeholder"
                    if field != "Contract version" and contains_placeholder
                    else "empty-field",
                    "Use concrete evidence or an allowed absence sentinel with a reason."
                    if field != "Contract version" and contains_placeholder
                    else "Contract field must contain evidence.",
                    line_number,
                    field,
                )
            )
            continue
        if field == "Contract version":
            version = _bounded_reported_version(body)
            if body != expected_version:
                findings.append(
                    Finding(
                        "version-mismatch",
                        f"Contract version must be exactly {expected_version}.",
                        line_number,
                        field,
                    )
                )
            continue

        if LIST_SENTINEL_RE.search(visible_body) or _absence_uses_markdown_container(
            visible_body
        ):
            findings.append(
                Finding(
                    "invalid-absence-syntax",
                    "Absence sentinels must be the complete field value, not a list item.",
                    line_number,
                    field,
                )
            )
            continue

        absence_match = ABSENCE_RE.fullmatch(visible_structure)
        absence_kind = absence_match.group("kind").casefold() if absence_match else None
        absence_reason = absence_match.group("reason").strip() if absence_match else ""
        absence_is_complete = visible_structure == visible_evidence
        allowed_absence_kinds = (
            {"unknown", "not applicable"}
            if field in definition.absence_allowed_fields
            else {"not run"}
            if field == "Validation"
            else set()
        )
        explicit_absence = (
            absence_kind in allowed_absence_kinds
            and bool(absence_reason)
            and not _contains_placeholder_value(absence_reason)
            and absence_is_complete
        )
        if absence_kind and not explicit_absence:
            code = (
                "absence-not-allowed"
                if absence_kind not in allowed_absence_kinds
                else "invalid-absence-syntax"
                if not absence_is_complete
                else "invalid-absence-reason"
            )
            findings.append(
                Finding(
                    code,
                    "Use an allowed sentinel with a concrete, non-placeholder reason.",
                    line_number,
                    field,
                )
            )
            continue
        if not absence_kind and contains_placeholder:
            findings.append(
                Finding(
                    "placeholder",
                    "Use concrete evidence or an allowed absence sentinel with a reason.",
                    line_number,
                    field,
                )
            )
        if (
            field in definition.checklist_fields
            and not _has_top_level_checklist_item(visible_body)
        ):
            findings.append(
                Finding(
                    "checklist-required",
                    "Field must contain at least one Markdown task-list item.",
                    line_number,
                    field,
                )
            )
        if field in definition.bullet_fields and not (
            _has_top_level_list_item(visible_body)
            or (field == "Validation" and explicit_absence)
        ):
            findings.append(
                Finding(
                    "bullet-required",
                    "Field must contain at least one Markdown list item.",
                    line_number,
                    field,
                )
            )
        reference_valid = (
            _has_issue_reference(visible_body)
            if field in definition.issue_reference_fields
            else _has_change_reference(visible_body)
            if field in definition.change_reference_fields
            else True
        )
        if not reference_valid:
            findings.append(
                Finding(
                    "reference-required",
                    "Field must contain an Issue or pull request reference.",
                    line_number,
                    field,
                )
            )

    return ValidationResult(
        not findings.has_findings, kind, version, findings.freeze()
    )


def _read_regular_descriptor(descriptor: int) -> str:
    item_stat = os.fstat(descriptor)
    if not stat.S_ISREG(item_stat.st_mode):
        raise ValueError("evidence path must be a non-symlink regular file")
    if item_stat.st_size > MAX_BYTES:
        raise ValueError(f"evidence file exceeds {MAX_BYTES} bytes")

    content = bytearray()
    while len(content) <= MAX_BYTES:
        chunk = os.read(descriptor, min(64 * 1024, MAX_BYTES + 1 - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    if len(content) > MAX_BYTES:
        raise ValueError(f"evidence file exceeds {MAX_BYTES} bytes")
    return bytes(content).decode("utf-8")


def _open_no_follow(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
    try:
        return os.open(path, flags, dir_fd=dir_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("path must not contain symlinks") from error
        raise


def _open_regular_file(path: Path, workspace: Path | None) -> str:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None or os.open not in os.supports_dir_fd:
        raise ValueError("platform does not support secure workspace-relative file reads")

    nonblock = getattr(os, "O_NONBLOCK", None)
    if nonblock is None:
        raise ValueError("platform does not support nonblocking evidence-file reads")

    base_flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
    final_flags = base_flags | nonblock
    if workspace is None:
        item_stat = path.lstat()
        if stat.S_ISLNK(item_stat.st_mode):
            raise ValueError("path must not contain symlinks")
        descriptor = _open_no_follow(path, final_flags)
        try:
            return _read_regular_descriptor(descriptor)
        finally:
            os.close(descriptor)

    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path must be a normalized workspace-relative path")
    workspace = workspace.resolve(strict=True)
    descriptor = _open_no_follow(workspace, base_flags | directory_flag)
    try:
        for index, part in enumerate(path.parts):
            component_stat = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(component_stat.st_mode):
                raise ValueError("path must not contain symlinks")
            component_flags = final_flags
            if index < len(path.parts) - 1:
                component_flags = base_flags
                component_flags |= directory_flag
            next_descriptor = _open_no_follow(
                part,
                component_flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return _read_regular_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _write_github_output(path: Path, result: ValidationResult) -> None:
    payload = json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-16-le")) > MAX_REPORT_JSON_UTF16_BYTES:
        raise ValueError("report JSON exceeds the GitHub output budget")
    safe_version = result.version if result.version == CONTRACT_VERSION else ""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"valid={'true' if result.valid else 'false'}\n")
        stream.write(f"kind={result.kind}\n")
        stream.write(f"version={safe_version}\n")
        stream.write(f"report-json={payload}\n")


def _render_text(result: ValidationResult, source: Path) -> str:
    if result.valid:
        return f"PASS: {source} satisfies Change Evidence Contract {result.version} ({result.kind})"
    lines = [f"FAIL: {source} does not satisfy Change Evidence Contract {CONTRACT_VERSION} ({result.kind})"]
    for finding in result.findings:
        location = f"line {finding.line}: " if finding.line is not None else ""
        field = f"[{finding.field}] " if finding.field else ""
        lines.append(f"- {finding.code}: {location}{field}{finding.message}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--kind", required=True, choices=tuple(CONTRACTS))
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument("--expected-version", default=CONTRACT_VERSION)
    parser.add_argument("--mode", choices=("enforce", "audit"), default="enforce")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--github-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text = _open_regular_file(args.path, args.workspace)
        result = validate_text(text, kind=args.kind, expected_version=args.expected_version)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    try:
        if args.github_output:
            _write_github_output(args.github_output, result)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(result, args.path))
    if result.valid or args.mode == "audit":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
