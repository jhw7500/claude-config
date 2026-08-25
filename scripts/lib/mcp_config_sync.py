#!/usr/bin/env python3
"""Plan and atomically synchronize Claude MCP user configuration."""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import json
import os
import posixpath
import re
import secrets
import shlex
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, unquote, urlsplit


PENDING_EXIT = 2
DIFF_FIELDS = ("type", "command", "args", "env")
PLACEHOLDER = re.compile(
    r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<default>:-[^}]*)?\}$"
)
CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|TOKEN|PASSWORD|PASSWD|"
    r"PASS|PWD|PASS_?PHRASE|PAT|SECRET|CREDENTIAL|AUTH|"
    r"AUTHORIZATION|BEARER)"
    r"(?:_|$)|"
    r"(?:PASSWORD|PASSWD|PASS_?PHRASE|CLIENT_?SECRET|"
    r"(?:ACCESS|REFRESH)_?TOKEN)(?:_|$)",
    re.IGNORECASE,
)
PROVIDER_PAT_NAME = re.compile(
    r"(?:^|_)(?:GITHUB|GITLAB)_?PAT(?:_|$)",
    re.IGNORECASE,
)
CONNECTION_CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:(?:DATABASE|DB|POSTGRES(?:QL)?|MYSQL|MARIADB|MONGO(?:DB)?|"
    r"REDIS|REDISS|AMQP|AMQPS|RABBITMQ|KAFKA|BROKER|ELASTIC(?:SEARCH)?|"
    r"OPENSEARCH|CLOUDAMQP|CLOUDINARY|REDISCLOUD|JAWSDB|MONGOLAB|MONGOHQ)"
    r"_?(?:URL|URI|DSN)|DSN|CONNECTION_?STRING|CONNSTR)$",
    re.IGNORECASE,
)
CREDENTIAL_FLAG = re.compile(
    r"^--?(?:[a-z0-9]+[-_])*(?:api[-_]?key|access[-_]?key|"
    r"private[-_]?key|client[-_]?secret|(?:access|refresh)[-_]?token|"
    r"token|password|passwd|passphrase|pwd|secret|dsn|"
    r"database[-_]?(?:url|uri|dsn)|connection[-_]?(?:string|uri)|connstr|"
    r"credential|auth|authorization|bearer|headers?|env|environment|"
    r"env[-_]?(?:var|variable))$",
    re.IGNORECASE,
)
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?:^|[#?&=,;{(\[\s])[\"']?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.\[\]+-]*)"
    r"[\"']?(?=\s*[:=])"
)
CREDENTIAL_LABEL = re.compile(
    r"(?:^|[#?&=,;{(\[\s])[\"']?(?:x[-_])?(?:api[-_]?key|access[-_]?key|"
    r"private[-_]?key|client[-_]?secret|(?:access|refresh)[-_]?token|"
    r"token|password|passwd|passphrase|pwd|secret|"
    r"credential|auth|authorization|bearer)[\"']?\s*[:=]",
    re.IGNORECASE,
)
SHORT_ENV_ASSIGNMENT = re.compile(
    r"^-e=?(?P<name>[A-Za-z_][A-Za-z0-9_]*)="
)
NAME_SEPARATOR = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[^A-Za-z0-9]+")
ENCODED_OPTION_TOKEN_START = re.compile(
    r"(?:^|[\\\s\"'\[\]{}(),;|])%2d",
    re.IGNORECASE,
)
ENCODED_SQL_OPTION_START = re.compile(
    r"(?:^|[\\\s\"'\[\]{}(),;|])%2[fF]"
    r"(?:[PUSZdz]|%[0-9a-fA-F]{2})"
)
ENCODED_SLASH_OPTION_START = re.compile(
    r"(?:^|[\\\s\"'\[\]{}(),;|])/%[0-9a-fA-F]{2}"
)
ENCODED_PLUS_OPTION_START = re.compile(
    r"(?:^|[\\\s\"'\[\]{}(),;|])%2[bB](?:[Oo]|%[0-9a-fA-F]{2})"
)
ENCODED_PLUS_OPTION_NAME = re.compile(
    r"(?:^|[\\\s\"'\[\]{}(),;|])\+[^=\\\s\"'\[\]{}(),;|]*"
    r"%[0-9a-fA-F]{2}"
)
ENCODED_OPTION_NAME = re.compile(
    r"(?:^|[\\\s\"'\[\]{}(),;|])--?[^=\\\s\"'\[\]{}(),;|]*"
    r"%[0-9a-fA-F]{2}"
)
URL_REFERENCE_START = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*:|(?<![A-Za-z0-9/\\]))"
    r"[\\/]{2}"
)
URL_REFERENCE_END = re.compile(r"[\s\"'<>}]")
ORACLE_IDENTIFIER_ATOM = r"(?:\"[^\"]*\"|'[^']*'|[\w.%+$#-]+)"
ORACLE_USER_ATOM = (
    rf"{ORACLE_IDENTIFIER_ATOM}(?:\[{ORACLE_IDENTIFIER_ATOM}\])?"
)
ORACLE_PASSWORD_ATOM = r"(?:\"[^\"]*\"|'[^']*'|[^/@\s\"']+)"
ORACLE_CREDENTIAL_PREFIX = re.compile(
    rf"(?:^|[=,;\s\"']){ORACLE_USER_ATOM}/{ORACLE_PASSWORD_ATOM}@"
)
TARGETLESS_ORACLE_LOGON = re.compile(
    rf"^{ORACLE_USER_ATOM}/{ORACLE_PASSWORD_ATOM}"
    rf"(?:\s+AS\s+[A-Za-z0-9_]+)?"
    rf"(?:\s+edition\s*=\s*{ORACLE_IDENTIFIER_ATOM})?$",
    re.IGNORECASE,
)
ORACLE_LOGON_FLAG = re.compile(r"^--(?:connect|logon)$", re.IGNORECASE)
JDBC_ORACLE_CREDENTIAL = re.compile(
    rf"jdbc:oracle:[A-Za-z0-9_+-]+:{ORACLE_USER_ATOM}/"
    rf"{ORACLE_PASSWORD_ATOM}@",
    re.IGNORECASE,
)
USERNAME_ONLY_SAFE_SCHEMES = frozenset({"ftp", "git+ssh", "sftp", "ssh"})
SAFE_ARG_EXPANSIONS = frozenset(
    {"${FILESYSTEM_MCP_ROOT}", "${JHW_NOTION_DIST}/index.js"}
)
SAFE_CREDENTIAL_SOURCE_FLAGS = frozenset({"--password-stdin"})
SIGNED_URL_PARAMETER_NAMES = frozenset(
    {"SIG", "SIGNATURE", "X_AMZ_SIGNATURE", "X_GOOG_SIGNATURE"}
)
PROVIDER_SIGNATURE_PARAMETER_NAMES = frozenset(
    {"X_AMZ_SIGNATURE", "X_GOOG_SIGNATURE"}
)
PROVIDER_SIGNATURE_LABEL = re.compile(
    r"(?:^|[?&#=,;\s(\[{\"'])(?:x[-_]amz|x[-_]goog)[-_]signature\s*=\s*\S",
    re.IGNORECASE,
)
AZURE_SAS_COMPANION_NAMES = frozenset({"SE", "SP", "SR", "SS", "SRT"})
SLACK_CAPABILITY_ROUTES = frozenset({"actions", "app", "services", "triggers"})
ORACLE_CLIENT_COMMANDS = frozenset({"sql", "sqlcl", "sqlplus"})
MYSQL_PASSWORD_CLIENTS = frozenset(
    {
        "mariadb",
        "mariadb-admin",
        "mariadb-binlog",
        "mariadb-check",
        "mariadb-dump",
        "mariadb-import",
        "mariadb-show",
        "mariadb-slap",
        "mysql",
        "mysqladmin",
        "mysqlbinlog",
        "mysqlcheck",
        "mysqldump",
        "mysqlimport",
        "mysqlpump",
        "mysqlshow",
        "mysqlsh",
        "mysqlslap",
    }
)
SHELL_COMMANDS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
CONTAINER_COMMANDS = frozenset({"docker", "podman", "podman-remote"})
SQLCMD_PASSWORD_CLIENTS = frozenset({"osql", "sqlcmd"})
EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat")
MAX_COMMAND_CONTEXT_DEPTH = 8
CURL_CREDENTIAL_HEADER = re.compile(
    r"^\s*(?:authorization|proxy-authorization|cookie)\s*:\s*\S",
    re.IGNORECASE,
)
CURL_CREDENTIAL_SHORT_OPTIONS = frozenset({"E", "H", "U", "b", "u"})
CURL_USER_PASSWORD_LONG_OPTIONS = frozenset({"--proxy-user", "--user"})
CURL_HEADER_LONG_OPTIONS = frozenset({"--header", "--proxy-header"})
CURL_CERTIFICATE_LONG_OPTIONS = frozenset({"--cert", "--proxy-cert"})
CURL_COOKIE_LONG_OPTIONS = frozenset({"--cookie"})
CURL_OPAQUE_CREDENTIAL_LONG_OPTIONS = frozenset(
    {
        "--oauth2-bearer",
        "--pass",
        "--proxy-pass",
        "--proxy-tlspassword",
        "--tlspassword",
    }
)
CURL_EXACT_PREFIX_SAFE_LONG_OPTIONS = frozenset({"--head", "--proxy"})
CURL_LONG_OPTIONS_WITHOUT_VALUE = frozenset(
    {"--cert-status", "--head"}
)
CURL_PROXY_URL_LONG_OPTIONS = frozenset(
    {
        "--preproxy",
        "--proxy",
        "--socks4",
        "--socks4a",
        "--socks5",
        "--socks5-hostname",
    }
)
CURL_LONG_OPTIONS_WITH_VALUE = frozenset(
    {
        "--cookie-jar",
        "--output",
        "--request",
        "--url",
        "--user-agent",
    }
) | CURL_PROXY_URL_LONG_OPTIONS
CURL_SHORT_OPTIONS_WITHOUT_VALUE = frozenset(
    "#012346:BGIJLMNORSVZabfgijklnpqsv"
)
CURL_SHORT_OPTIONS_WITH_VALUE = frozenset(
    {
        "A",
        "C",
        "D",
        "F",
        "K",
        "P",
        "Q",
        "T",
        "X",
        "Y",
        "c",
        "d",
        "e",
        "h",
        "m",
        "o",
        "r",
        "t",
        "w",
        "x",
        "y",
        "z",
    }
)
SHELL_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
SHELL_ARRAY_ASSIGNMENT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*\[[^\]\r\n]+\]\+?="
)
SHELL_CONTROL_CHARACTERS = frozenset(";&|(){}<>\n")
SHELL_OPTIONS_WITH_VALUE = frozenset(
    {"-O", "+O", "-o", "+o", "--init-file", "--rcfile"}
)
ENV_OPTIONS_WITH_VALUE = frozenset({"-u", "--unset", "-C", "--chdir"})
ENV_OPTIONS_WITHOUT_VALUE = frozenset(
    {"-", "-0", "--null", "-i", "--ignore-environment", "-v", "--debug"}
)
SUDO_OPTIONS_WITH_VALUE = frozenset(
    {
        "-C",
        "--close-from",
        "-D",
        "--chdir",
        "-g",
        "--group",
        "-h",
        "--host",
        "-p",
        "--prompt",
        "-R",
        "--chroot",
        "-T",
        "--command-timeout",
        "-u",
        "--user",
    }
)
SUDO_OPTIONS_WITHOUT_VALUE = frozenset(
    {
        "-A",
        "--askpass",
        "-b",
        "--background",
        "-E",
        "--preserve-env",
        "-e",
        "--edit",
        "-H",
        "--set-home",
        "-i",
        "--login",
        "-K",
        "--remove-timestamp",
        "-k",
        "--reset-timestamp",
        "-n",
        "--non-interactive",
        "-P",
        "--preserve-groups",
        "-S",
        "--stdin",
        "-s",
        "--shell",
        "-V",
        "--version",
        "-v",
        "--validate",
    }
)
DOCKER_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "--config",
        "-c",
        "--context",
        "-H",
        "--host",
        "-l",
        "--log-level",
        "--tlscacert",
        "--tlscert",
        "--tlskey",
    }
)
DOCKER_GLOBAL_OPTIONS_WITHOUT_VALUE = frozenset(
    {"-D", "--debug", "--help", "--tls", "--tlsverify", "-v", "--version"}
)
PODMAN_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "--cdi-spec-dir",
        "--cgroup-manager",
        "--config",
        "--conmon",
        "-c",
        "--connection",
        "--events-backend",
        "--hooks-dir",
        "--identity",
        "--imagestore",
        "--log-level",
        "--module",
        "--network-cmd-path",
        "--network-config-dir",
        "--out",
        "--root",
        "--runroot",
        "--runtime",
        "--runtime-flag",
        "--ssh",
        "--storage-driver",
        "--storage-opt",
        "--tls-ca",
        "--tls-cert",
        "--tls-details",
        "--tls-key",
        "--tmpdir",
        "--url",
        "--volumepath",
    }
)
PODMAN_GLOBAL_OPTIONS_WITHOUT_VALUE = frozenset(
    {
        "--help",
        "-r",
        "--remote",
        "--syslog",
        "--transient-store",
        "-v",
        "--version",
    }
)


class BlockedPlan(ValueError):
    """A validation failure whose value-free diagnostic is safe to display."""


@dataclass(frozen=True)
class ConfigSnapshot:
    raw: bytes | None
    data: dict[str, Any]


class _SemanticNormalizationLimit(RuntimeError):
    """Internal signal to reject inputs beyond bounded semantic decoding."""


def _contains_credential(token: str, credential_values: list[str]) -> bool:
    if not credential_values:
        return False
    try:
        return any(
            candidate == secret or (len(secret) >= 8 and secret in candidate)
            for candidate in _credential_comparison_variants(token)
            for secret in credential_values
        )
    except _SemanticNormalizationLimit:
        return True


def _normalized_name(name: str) -> str:
    return NAME_SEPARATOR.sub("_", name).strip("_").upper()


def _is_direct_credential_name(name: str) -> bool:
    normalized = _normalized_name(name)
    if normalized in {"OLDPWD", "PWD"}:
        return False
    if normalized == "SSHPASS":
        return True
    return bool(
        CREDENTIAL_NAME.search(normalized) or PROVIDER_PAT_NAME.search(normalized)
    )


def _is_connection_credential_name(name: str) -> bool:
    return bool(CONNECTION_CREDENTIAL_NAME.search(_normalized_name(name)))


def _is_credential_name(name: str) -> bool:
    return _is_direct_credential_name(name) or _is_connection_credential_name(name)


def _decoded_variants(token: str) -> Iterator[str]:
    current = token
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        yield current
        current = unquote(current)


def _fully_decoded(token: str) -> str:
    decoded = token
    for decoded in _decoded_variants(token):
        pass
    return decoded


def _contains_encoded_option_token(token: str) -> bool:
    return any(
        ENCODED_OPTION_TOKEN_START.search(variant)
        or ENCODED_SQL_OPTION_START.search(variant)
        or ENCODED_SLASH_OPTION_START.search(variant)
        or ENCODED_PLUS_OPTION_START.search(variant)
        or ENCODED_PLUS_OPTION_NAME.search(variant)
        or ENCODED_OPTION_NAME.search(variant)
        for variant in _decoded_variants(token)
    )


def _json_scalar_strings(value: Any, *, depth: int) -> Iterator[str]:
    if depth >= MAX_COMMAND_CONTEXT_DEPTH:
        raise _SemanticNormalizationLimit
    if isinstance(value, str):
        yield from _decoded_variants(value)
        yield from _json_decoded_strings(value, depth=depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield from _json_scalar_strings(key, depth=depth + 1)
            yield from _json_scalar_strings(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _json_scalar_strings(item, depth=depth + 1)


def _json_decoded_strings(token: str, *, depth: int = 0) -> Iterator[str]:
    if depth >= MAX_COMMAND_CONTEXT_DEPTH:
        raise _SemanticNormalizationLimit
    decoder = json.JSONDecoder()
    for variant in _decoded_variants(token):
        index = 0
        while index < len(variant):
            if variant[index] not in '"[{':
                index += 1
                continue
            try:
                value, end = decoder.raw_decode(variant[index:])
            except json.JSONDecodeError:
                index += 1
                continue
            except RecursionError as error:
                raise _SemanticNormalizationLimit from error
            yield from _json_scalar_strings(value, depth=depth + 1)
            index += max(end, 1)


def _credential_comparison_variants(token: str) -> Iterator[str]:
    yield from _decoded_variants(token)
    yield from _json_decoded_strings(token)


def _url_references(token: str) -> Iterator[str]:
    for match in URL_REFERENCE_START.finditer(token):
        end = URL_REFERENCE_END.search(token, match.end())
        stop = end.start() if end is not None else len(token)
        candidate = token[match.start() : stop].replace("\\", "/")
        if candidate.startswith("//"):
            candidate = f"https:{candidate}"
        yield candidate


def _contains_url_userinfo(token: str) -> bool:
    for candidate in _url_references(token):
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            authority = candidate.split("//", 1)[-1].split("/", 1)[0]
            if "@" in authority:
                return True
            continue
        if parsed.password is not None:
            return True
        if (
            parsed.username is not None
            and parsed.scheme.lower() not in USERNAME_ONLY_SAFE_SCHEMES
        ):
            return True
    return False


def _contains_url_credential_parameters(token: str) -> bool:
    for candidate in _url_references(token):
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        parameter_groups = [parsed.query, parsed.fragment]
        if "?" in parsed.fragment:
            parameter_groups.append(parsed.fragment.rsplit("?", 1)[-1])
        for parameters in parameter_groups:
            for key, _value in parse_qsl(parameters, keep_blank_values=True):
                normalized = _normalized_name(key)
                if (
                    _is_credential_name(normalized)
                    or normalized in SIGNED_URL_PARAMETER_NAMES
                ):
                    return True
    return False


def _contains_standalone_signed_query(token: str) -> bool:
    parameter_groups = [(token, False)]
    parameter_groups.extend(
        (token[index + 1 :], delimiter in "?#")
        for index, delimiter in enumerate(token)
        if delimiter in "=?#,; \t\r\n"
    )
    for parameters, delimited in parameter_groups:
        keys = {
            _normalized_name(key)
            for key, _value in parse_qsl(parameters, keep_blank_values=True)
        }
        signed = keys & SIGNED_URL_PARAMETER_NAMES
        if signed and (
            bool(signed & PROVIDER_SIGNATURE_PARAMETER_NAMES)
            or delimited
            or ("SV" in keys and bool(keys & AZURE_SAS_COMPANION_NAMES))
        ):
            return True
    return False


def _normalized_capability_path(path: str) -> str:
    collapsed = re.sub(r"/+", "/", path.replace("\\", "/"))
    normalized = posixpath.normpath(collapsed)
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized.lower()


def _contains_capability_url(token: str) -> bool:
    for candidate in _url_references(token):
        try:
            parsed = urlsplit(candidate)
            hostname = (parsed.hostname or "").rstrip(".").lower()
        except ValueError:
            continue
        if parsed.scheme.lower() not in {"http", "https"}:
            continue
        path = _normalized_capability_path(parsed.path)
        segments = [segment for segment in path.split("/") if segment]
        if (
            hostname in {"hooks.slack.com", "hooks.slack-gov.com"}
            and segments
            and segments[0] in SLACK_CAPABILITY_ROUTES
        ):
            return True
        if (
            hostname in {"discord.com", "discordapp.com"}
            or hostname.endswith(".discord.com")
            or hostname.endswith(".discordapp.com")
        ) and re.match(r"^/api(?:/v[0-9]+)?/webhooks(?:/|$)", path):
            return True
    return False


def _is_non_json_credential_carrier(token: str) -> bool:
    flag = token.partition("=")[0]
    short_env = SHORT_ENV_ASSIGNMENT.match(token)
    safe_credential_source = token.casefold() in SAFE_CREDENTIAL_SOURCE_FLAGS
    if (
        (short_env is not None and _is_credential_name(short_env.group("name")))
        or (
            not safe_credential_source
            and (
                CREDENTIAL_FLAG.fullmatch(flag)
                or (flag.startswith("-") and _is_credential_name(flag.lstrip("-")))
            )
        )
        or CREDENTIAL_LABEL.search(token)
        or PROVIDER_SIGNATURE_LABEL.search(token)
        or _contains_url_userinfo(token)
        or _contains_url_credential_parameters(token)
        or _contains_standalone_signed_query(token)
        or _contains_capability_url(token)
        or ORACLE_CREDENTIAL_PREFIX.search(token)
        or JDBC_ORACLE_CREDENTIAL.search(token)
    ):
        return True
    return any(
        _is_credential_name(match.group("name"))
        for match in CREDENTIAL_ASSIGNMENT.finditer(token)
    )


def _json_value_contains_credential(value: Any) -> bool:
    if isinstance(value, dict):
        command = value.get("command")
        args = value.get("args")
        command_object = (
            isinstance(command, str)
            and isinstance(args, list)
            and all(isinstance(item, str) for item in args)
        )
        if any(
            isinstance(key, str)
            and (
                _is_credential_name(key)
                or _normalized_name(key) in PROVIDER_SIGNATURE_PARAMETER_NAMES
            )
            for key in value
        ):
            return True
        if command_object:
            if _command_context_contains_credential(command, args):
                return True
            return any(
                _json_value_contains_credential(item)
                for key, item in value.items()
                if key not in {"command", "args"}
            )
        return any(_json_value_contains_credential(item) for item in value.values())
    if isinstance(value, list):
        if value and all(isinstance(item, str) for item in value):
            return _command_context_contains_credential(value[0], value[1:])
        return any(_json_value_contains_credential(item) for item in value)
    if isinstance(value, str):
        return any(
            _is_non_json_credential_carrier(variant)
            or _contains_json_credential(variant)
            for variant in _decoded_variants(value)
        )
    return False


def _contains_json_credential(token: str) -> bool:
    decoder = json.JSONDecoder()
    index = 0
    while index < len(token):
        character = token[index]
        if character not in '"[{':
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(token[index:])
            if _json_value_contains_credential(value):
                return True
            index += max(end, 1)
        except json.JSONDecodeError:
            index += 1
        except RecursionError:
            return True
    return False


def _json_value_contains_placeholder(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and _contains_placeholder_syntax(key))
            or _json_value_contains_placeholder(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_json_value_contains_placeholder(item) for item in value)
    if isinstance(value, str):
        return _contains_placeholder_syntax(value)
    return False


def _contains_json_placeholder(token: str) -> bool:
    decoder = json.JSONDecoder()
    for index, character in enumerate(token):
        if character not in '"[{':
            continue
        try:
            value, _end = decoder.raw_decode(token[index:])
            if _json_value_contains_placeholder(value):
                return True
        except json.JSONDecodeError:
            continue
        except RecursionError:
            return True
    return False


def _is_credential_carrier(token: str) -> bool:
    return any(
        _is_non_json_credential_carrier(variant)
        or _contains_json_credential(variant)
        for variant in _decoded_variants(token)
    )


def _is_targetless_oracle_logon(token: str) -> bool:
    return any(
        TARGETLESS_ORACLE_LOGON.fullmatch(variant.strip())
        for variant in _decoded_variants(token)
    )


def _is_oracle_logon_flag(token: str) -> bool:
    return any(
        ORACLE_LOGON_FLAG.fullmatch(variant.strip())
        for variant in _decoded_variants(token)
    )


def _is_oracle_logon_assignment(token: str) -> bool:
    for variant in _decoded_variants(token):
        flag, separator, value = variant.strip().partition("=")
        if (
            separator
            and ORACLE_LOGON_FLAG.fullmatch(flag)
            and _is_targetless_oracle_logon(value)
        ):
            return True
    return False


def _json_value_contains_oracle_logon(value: Any) -> bool:
    if isinstance(value, dict):
        command = value.get("command")
        args = value.get("args")
        if (
            isinstance(command, str)
            and isinstance(args, list)
            and all(isinstance(item, str) for item in args)
            and _args_contain_targetless_oracle_logon(command, args)
        ):
            return True
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if _is_oracle_logon_assignment(key):
                return True
            if (
                _is_oracle_logon_flag(key)
                and isinstance(item, str)
                and _is_targetless_oracle_logon(item)
            ):
                return True
        return any(
            _json_value_contains_oracle_logon(item) for item in value.values()
        )
    if isinstance(value, list):
        if (
            value
            and all(isinstance(item, str) for item in value)
            and _is_oracle_client_command(value[0])
            and _args_contain_targetless_oracle_logon(value[0], value[1:])
        ):
            return True
        for index, item in enumerate(value):
            if not isinstance(item, str):
                continue
            if _is_oracle_logon_assignment(item):
                return True
            if (
                _is_oracle_logon_flag(item)
                and index + 1 < len(value)
                and isinstance(value[index + 1], str)
                and _is_targetless_oracle_logon(value[index + 1])
            ):
                return True
        return any(_json_value_contains_oracle_logon(item) for item in value)
    if isinstance(value, str):
        return _is_oracle_logon_assignment(value) or _contains_json_oracle_logon(
            value
        )
    return False


def _contains_json_oracle_logon(token: str) -> bool:
    decoder = json.JSONDecoder()
    for variant in _decoded_variants(token):
        for index, character in enumerate(variant):
            if character not in '"[{':
                continue
            try:
                value, _end = decoder.raw_decode(variant[index:])
                if _json_value_contains_oracle_logon(value):
                    return True
            except json.JSONDecodeError:
                continue
            except RecursionError:
                return True
    return False


def _command_basename(command: str) -> str:
    basename = command.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in EXECUTABLE_SUFFIXES:
        if basename.endswith(suffix):
            basename = basename[: -len(suffix)]
            break
    return basename


def _is_oracle_client_command(command: str) -> bool:
    return _command_basename(command) in ORACLE_CLIENT_COMMANDS


def _scan_cli_options(
    args: list[str],
    *,
    short_required: frozenset[str] = frozenset(),
    short_required_separate: frozenset[str] = frozenset(),
    short_optional_attached: frozenset[str] = frozenset(),
    short_without_value: frozenset[str] = frozenset(),
    long_required: frozenset[str] = frozenset(),
    long_optional_attached: frozenset[str] = frozenset(),
    stop_at_positional: bool = False,
) -> tuple[bool, list[tuple[str, str | None]]]:
    """Parse a bounded getopt-style grammar while preserving consumed `--`."""

    occurrences: list[tuple[str, str | None]] = []
    sensitive_short = short_optional_attached
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        if token == "-" or not token.startswith("-"):
            if stop_at_positional:
                break
            index += 1
            continue
        if token.startswith("--"):
            name, separator, attached = token.partition("=")
            if separator:
                occurrences.append((name, attached))
                index += 1
                continue
            if name in long_required:
                value = args[index + 1] if index + 1 < len(args) else None
                occurrences.append((name, value))
                index += 2 if value is not None else 1
                continue
            if name in long_optional_attached:
                occurrences.append((name, None))
                index += 1
                continue
            if index + 1 < len(args) and args[index + 1] == "--":
                return False, []
            occurrences.append((name, None))
            index += 1
            continue

        options = token[1:]
        option_index = 0
        consumed_next = False
        while option_index < len(options):
            option = options[option_index]
            remainder = options[option_index + 1 :]
            if option in short_required_separate:
                if remainder:
                    occurrences.append((f"-{option}", None))
                else:
                    value = args[index + 1] if index + 1 < len(args) else None
                    occurrences.append((f"-{option}", value))
                    consumed_next = value is not None
                break
            if option in short_required:
                value = remainder or (
                    args[index + 1] if index + 1 < len(args) else None
                )
                occurrences.append((f"-{option}", value))
                consumed_next = not remainder and value is not None
                break
            if option in short_optional_attached:
                occurrences.append((f"-{option}", remainder or None))
                break
            occurrences.append((f"-{option}", None))
            if option not in short_without_value:
                if any(candidate in remainder for candidate in sensitive_short):
                    return False, []
                if not remainder and index + 1 < len(args) and args[index + 1] == "--":
                    return False, []
            option_index += 1
        index += 2 if consumed_next else 1
    return True, occurrences


def _contains_inline_user_password(value: str) -> bool:
    _username, separator, password = value.partition(":")
    return bool(separator and password)


def _contains_inline_cookie(value: str) -> bool:
    return "=" in value


def _is_env_assignment(token: str) -> bool:
    _name, separator, _value = token.partition("=")
    return bool(separator and not token.startswith("-"))


def _is_sudo_assignment(token: str) -> bool:
    name, separator, _value = token.partition("=")
    return bool(separator and name and not name.startswith(("-", "/")))


def _contains_curl_certificate_password(value: str) -> bool:
    value = _fully_decoded(value)
    if value.casefold().startswith("pkcs11:"):
        attributes = value[len("pkcs11:") :].split("?", 1)[0]
        return any(
            name.casefold() == "pin-value" and bool(pin)
            for attribute in attributes.split(";")
            for name, separator, pin in [attribute.partition("=")]
            if separator
        )
    delimiters: list[int] = []
    for index, character in enumerate(value):
        if character != ":":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and value[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            delimiters.append(index)
    if delimiters and delimiters[0] == 1 and value[0].isalpha():
        delimiters = delimiters[1:]
    return bool(delimiters and value[delimiters[0] + 1 :])


def _curl_option_occurrences(
    args: list[str],
) -> tuple[bool, list[tuple[str, str, bool]]]:
    credential_options = (
        CURL_USER_PASSWORD_LONG_OPTIONS
        | CURL_HEADER_LONG_OPTIONS
        | CURL_CERTIFICATE_LONG_OPTIONS
        | CURL_COOKIE_LONG_OPTIONS
        | CURL_OPAQUE_CREDENTIAL_LONG_OPTIONS
    )
    occurrences: list[tuple[str, str, bool]] = []
    argument_index = 0
    while argument_index < len(args):
        token = args[argument_index]
        if token == "--":
            occurrences.extend(
                ("URL", value, False) for value in args[argument_index + 1 :]
            )
            break
        if token == "-" or not token.startswith("-"):
            occurrences.append(("URL", token, False))
            argument_index += 1
            continue
        if token.startswith("--"):
            name, separator, attached = token.partition("=")
            expanded = name.startswith("--expand-")
            core_name = f"--{name.removeprefix('--expand-')}" if expanded else name
            matches = []
            if not (
                core_name in CURL_EXACT_PREFIX_SAFE_LONG_OPTIONS
                and core_name not in credential_options
            ):
                matches = [
                    option
                    for option in credential_options
                    if option.startswith(core_name)
                ]
            if matches:
                value = attached if separator else (
                    args[argument_index + 1]
                    if argument_index + 1 < len(args)
                    else ""
                )
                occurrences.extend(
                    (option, value, expanded) for option in matches
                )
                argument_index += 2 if not separator and value else 1
                continue
            if separator:
                if core_name == "--url" or core_name in CURL_PROXY_URL_LONG_OPTIONS:
                    occurrences.append((core_name, attached, expanded))
                argument_index += 1
                continue
            if core_name in CURL_LONG_OPTIONS_WITH_VALUE:
                value = (
                    args[argument_index + 1]
                    if argument_index + 1 < len(args)
                    else ""
                )
                if core_name == "--url" or core_name in CURL_PROXY_URL_LONG_OPTIONS:
                    occurrences.append((core_name, value, expanded))
                argument_index += (
                    2 if argument_index + 1 < len(args) else 1
                )
                continue
            if core_name in CURL_LONG_OPTIONS_WITHOUT_VALUE:
                argument_index += 1
                continue
            if argument_index + 1 < len(args) and args[argument_index + 1] == "--":
                return False, []
            argument_index += 1
            continue

        options = token[1:]
        option_index = 0
        consumed_next = False
        while option_index < len(options):
            option = options[option_index]
            remainder = options[option_index + 1 :]
            if option in CURL_CREDENTIAL_SHORT_OPTIONS:
                value = remainder or (
                    args[argument_index + 1]
                    if argument_index + 1 < len(args)
                    else ""
                )
                occurrences.append((option, value, False))
                consumed_next = not remainder and bool(value)
                break
            if option in CURL_SHORT_OPTIONS_WITHOUT_VALUE:
                option_index += 1
                continue
            if option in CURL_SHORT_OPTIONS_WITH_VALUE:
                value = remainder or (
                    args[argument_index + 1]
                    if argument_index + 1 < len(args)
                    else ""
                )
                if option == "x":
                    occurrences.append(("--proxy", value, False))
                consumed_next = not remainder and argument_index + 1 < len(args)
                break
            if any(
                candidate in CURL_CREDENTIAL_SHORT_OPTIONS
                for candidate in remainder
            ):
                return False, []
            if not remainder and argument_index + 1 < len(args) and args[argument_index + 1] == "--":
                return False, []
            break
        argument_index += 2 if consumed_next else 1
    return True, occurrences


def _args_contain_curl_credential(args: list[str]) -> bool:
    valid, occurrences = _curl_option_occurrences(args)
    if not valid:
        return True
    for option, value, expanded in occurrences:
        if option in {"u", "U", "--user", "--proxy-user"} and (
            _contains_inline_user_password(value)
            or (expanded and "{{" in value)
        ):
            return True
        if option in {"H", "--header", "--proxy-header"} and (
            CURL_CREDENTIAL_HEADER.match(value)
        ):
            return True
        if option in {"E", "--cert", "--proxy-cert"} and (
            _contains_curl_certificate_password(value)
            or (expanded and "{{" in value)
        ):
            return True
        if option in {"b", "--cookie"} and (
            _contains_inline_cookie(value) or (expanded and "{{" in value)
        ):
            return True
        if option in CURL_OPAQUE_CREDENTIAL_LONG_OPTIONS and value:
            return True
        if option in {"URL", "--url", *CURL_PROXY_URL_LONG_OPTIONS} and (
            _contains_scheme_less_userinfo_password(value)
        ):
            return True
    return False


def _contextually_safe_carrier_indexes(
    command: str,
    args: list[str],
) -> set[int]:
    basename = _command_basename(command)
    safe: set[int] = set()
    if basename == "curl":
        for index, token in enumerate(args):
            name, separator, attached = token.partition("=")
            core_name = (
                f"--{name.removeprefix('--expand-')}"
                if name.startswith("--expand-")
                else name
            )
            if not any(
                option.startswith(core_name)
                for option in CURL_HEADER_LONG_OPTIONS
            ):
                continue
            value = attached if separator else (
                args[index + 1] if index + 1 < len(args) else ""
            )
            if (
                value
                and not CURL_CREDENTIAL_HEADER.match(value)
                and not _is_credential_carrier(value)
            ):
                safe.add(index)
    elif basename in MYSQL_PASSWORD_CLIENTS:
        for index, token in enumerate(args):
            if token in {
                "--connect-expired-password",
                "--password",
                "--password1",
                "--password2",
                "--password3",
                "--skip-password",
            }:
                safe.add(index)
            elif token.startswith("--default-auth="):
                value = token.removeprefix("--default-auth=")
                if not _is_credential_carrier(value):
                    safe.add(index)
    elif basename in {"mongo", "mongosh"}:
        if args and args[-1] == "--password":
            safe.add(len(args) - 1)
    elif basename == "env":
        for index, token in enumerate(args):
            if token in {"--ignore-environment"}:
                safe.add(index)
    elif basename == "sudo":
        for index, token in enumerate(args):
            if token == "--preserve-env":
                safe.add(index)
            elif token.startswith("--preserve-env="):
                value = token.removeprefix("--preserve-env=")
                if not _is_credential_carrier(value):
                    safe.add(index)
    elif basename == "docker":
        valid, subcommand = _container_subcommand_index(command, args)
        if (
            valid
            and subcommand is not None
            and args[subcommand].casefold() == "login"
        ):
            for index in range(subcommand + 1, len(args)):
                token = args[index]
                if token == "--password" and index + 1 < len(args):
                    if args[index + 1] == "-":
                        safe.add(index)
                elif token == "--password=-":
                    safe.add(index)
    return safe


def _args_contain_attached_mysql_password(
    command: str,
    args: list[str],
) -> bool:
    basename = _command_basename(command)
    short_required = {"P", "S", "h", "u"}
    long_required = {"--host", "--port", "--socket", "--user"}
    short_without_value = {"v"}
    if basename in {"mariadb", "mysql", "mysqlsh"}:
        short_required.update({"D", "e"})
        long_required.update({"--database", "--execute"})
    if basename in {
        "mariadb-check",
        "mariadb-dump",
        "mysqlcheck",
        "mysqldump",
    }:
        short_without_value.add("e")
    if basename in {"mariadb-binlog", "mysqlbinlog"}:
        short_without_value.add("D")
    valid, occurrences = _scan_cli_options(
        args,
        short_required=frozenset(short_required),
        short_optional_attached=frozenset({"p"}),
        short_without_value=frozenset(short_without_value),
        long_required=frozenset(long_required),
        long_optional_attached=frozenset(
            {"--password", "--password1", "--password2", "--password3"}
        ),
    )
    if not valid:
        return True
    return (
        basename == "mysqlsh"
        and any(_contains_scheme_less_userinfo_password(value) for value in args)
    ) or any(
        (option == "-p" and bool(value))
        or (
            option in {"--password", "--password1", "--password2", "--password3"}
            and bool(value)
        )
        or _is_mariadb_password_prefix(option, value)
        for option, value in occurrences
    )


def _is_mariadb_password_prefix(option: str, value: str | None) -> bool:
    for loose_prefix in ("--loose-", "--loose_"):
        if option.startswith(loose_prefix):
            option = f"--{option[len(loose_prefix):]}"
            break
    return bool(
        value
        and option != "--password"
        and len(option) > len("--")
        and "--password".startswith(option)
    )


def _contains_scheme_less_userinfo_password(value: str) -> bool:
    userinfo, separator, host = value.rpartition("@")
    _user, password_separator, password = userinfo.partition(":")
    return bool(
        separator
        and host
        and password_separator
        and password
    )


def _args_contain_redis_password(args: list[str]) -> bool:
    valid, occurrences = _scan_cli_options(
        args,
        short_required=frozenset({"h", "i", "n", "p", "r", "s", "u"}),
        short_required_separate=frozenset({"a"}),
        long_required=frozenset(
            {
                "--cacert",
                "--cacertdir",
                "--cert",
                "--key",
                "--sni",
                "--user",
            }
        ),
    )
    return not valid or any(
        option == "-a" and bool(value) for option, value in occurrences
    )


def _args_contain_mongo_password(args: list[str]) -> bool:
    valid, occurrences = _scan_cli_options(
        args,
        short_required=frozenset({"h", "p", "u"}),
        long_required=frozenset(
            {
                "--authenticationDatabase",
                "--host",
                "--password",
                "--port",
                "--username",
            }
        ),
    )
    return not valid or any(
        option in {"-p", "--password"} and bool(value)
        for option, value in occurrences
    )


def _args_contain_sql_password(command: str, args: list[str]) -> bool:
    sensitive_options = {"P"}
    if _command_basename(command) == "sqlcmd":
        sensitive_options.update({"Z", "z"})
    normalized_args = [
        f"-{token[1:]}"
        if re.match(r"^/[PUSZdz](?:.|$)", token)
        else token
        for token in args
    ]
    valid, occurrences = _scan_cli_options(
        normalized_args,
        short_required=frozenset({"P", "S", "U", "Z", "d", "z"}),
    )
    return not valid or any(
        option.removeprefix("-") in sensitive_options and bool(value)
        for option, value in occurrences
    )


def _args_contain_sshpass_password(args: list[str]) -> bool:
    valid, occurrences = _scan_cli_options(
        args,
        short_required=frozenset({"P", "d", "f", "p"}),
        short_without_value=frozenset({"e", "v"}),
        stop_at_positional=True,
    )
    return not valid or any(
        option == "-p" and bool(value) for option, value in occurrences
    )


def _args_contain_bcp_password(args: list[str]) -> bool:
    valid, occurrences = _scan_cli_options(
        args,
        short_required=frozenset({"P", "S", "U", "d"}),
        short_without_value=frozenset({"G"}),
    )
    if not valid:
        return True
    entra_auth = any(option == "-G" for option, _value in occurrences)
    sql_username = any(
        option == "-U" and bool(value) for option, value in occurrences
    )
    if entra_auth and not sql_username:
        return False
    return any(
        option == "-P" and bool(value) for option, value in occurrences
    )


def _container_subcommand_index(
    command: str,
    args: list[str],
) -> tuple[bool, int | None]:
    basename = _command_basename(command)
    if basename == "docker":
        options_with_value = DOCKER_GLOBAL_OPTIONS_WITH_VALUE
        options_without_value = DOCKER_GLOBAL_OPTIONS_WITHOUT_VALUE
        attached_short_options = ("-c", "-H", "-l")
    elif basename in {"podman", "podman-remote"}:
        options_with_value = PODMAN_GLOBAL_OPTIONS_WITH_VALUE
        options_without_value = PODMAN_GLOBAL_OPTIONS_WITHOUT_VALUE
        attached_short_options = ("-c",)
    else:
        return False, None

    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            subcommand = index + 1 if index + 1 < len(args) else None
            return True, subcommand
        if not token.startswith("-") or token == "-":
            return True, index
        flag, separator, _value = token.partition("=")
        if flag in options_with_value:
            if separator:
                index += 1
            elif index + 1 >= len(args):
                return False, None
            else:
                index += 2
            continue
        if token in options_without_value:
            index += 1
            continue
        if any(
            token.startswith(option) and len(token) > len(option)
            for option in attached_short_options
        ):
            index += 1
            continue
        return False, None
    return True, None


def _args_contain_container_login_password(
    command: str,
    args: list[str],
) -> bool:
    valid, subcommand = _container_subcommand_index(command, args)
    if not valid:
        return True
    if subcommand is None or args[subcommand].casefold() != "login":
        return False
    login_args = args[subcommand + 1 :]
    valid, occurrences = _scan_cli_options(
        login_args,
        short_required=frozenset({"p", "u"}),
        short_without_value=(
            frozenset({"v"})
            if _command_basename(command) in {"podman", "podman-remote"}
            else frozenset()
        ),
        long_required=frozenset(
            {
                "--authfile",
                "--cert-dir",
                "--compat-auth-file",
                "--password",
                "--username",
            }
        ),
    )
    if not valid:
        return True
    return any(
        option in {"-p", "--password"}
        and bool(value)
        and not (_command_basename(command) == "docker" and value == "-")
        for option, value in occurrences
    )


def _args_contain_command_specific_credential(
    command: str,
    args: list[str],
) -> bool:
    basename = _command_basename(command)
    if basename == "curl":
        return _args_contain_curl_credential(args)
    if basename in MYSQL_PASSWORD_CLIENTS:
        return _args_contain_attached_mysql_password(command, args)
    if basename in {"redis-cli", "valkey-cli"}:
        return _args_contain_redis_password(args)
    if basename == "sshpass":
        return _args_contain_sshpass_password(args)
    if basename in {"mongo", "mongosh"}:
        return _args_contain_mongo_password(args)
    if basename in SQLCMD_PASSWORD_CLIENTS:
        return _args_contain_sql_password(command, args)
    if basename == "bcp":
        return _args_contain_bcp_password(args)
    if basename in CONTAINER_COMMANDS:
        return _args_contain_container_login_password(command, args)
    return False


def _terminates_oracle_logon_scan(token: str) -> bool:
    for variant in _decoded_variants(token):
        candidate = variant.strip()
        if (
            candidate.startswith("@")
            or candidate.casefold() == "/nolog"
            or candidate == "/"
            or candidate.startswith("/ ")
        ):
            return True
    return False


def _args_contain_targetless_oracle_logon(command: str, args: list[str]) -> bool:
    oracle_client = _is_oracle_client_command(command)
    for index, value in enumerate(args):
        if _is_oracle_logon_assignment(value) or _contains_json_oracle_logon(value):
            return True
        if (
            _is_oracle_logon_flag(value)
            and index + 1 < len(args)
            and _is_targetless_oracle_logon(args[index + 1])
        ):
            return True
    if not oracle_client:
        return False
    for value in args:
        if _terminates_oracle_logon_scan(value):
            break
        if _is_targetless_oracle_logon(value):
            return True
    return False


def _is_shell_assignment_word(token: str) -> bool:
    return bool(
        SHELL_ASSIGNMENT.match(token) or SHELL_ARRAY_ASSIGNMENT.match(token)
    )


def _contains_unsupported_shell_expansion(script: str) -> bool:
    if re.search(r"\{[^{}\r\n]*(?:,|\.\.)[^{}\r\n]*\}", script):
        return True
    if re.search(r"(?:@|\+|!|\?|\*)\(", script):
        return True
    return any(
        any(character in token for character in "*?[")
        and not SHELL_ARRAY_ASSIGNMENT.match(token)
        for token in script.split()
    )


def _shell_command_vectors(
    script: str,
    *,
    shell: str,
) -> tuple[bool, list[list[str]]]:
    if (
        "\0" in script
        or "$" in script
        or "`" in script
        or "\\\n" in script
        or "\\\r\n" in script
        or _contains_unsupported_shell_expansion(script)
    ):
        return False, []
    lexer = shlex.shlex(
        script,
        posix=True,
        punctuation_chars=";&|(){}<>\n",
    )
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return False, []
    if any(
        ("<" in token or ">" in token or ("|" in token and token != "||"))
        for token in tokens
        if token
        and all(character in SHELL_CONTROL_CHARACTERS for character in token)
    ):
        return False, []

    segments: list[list[str]] = []
    segment: list[str] = []
    for token in tokens:
        if token and all(character in SHELL_CONTROL_CHARACTERS for character in token):
            if segment:
                segments.append(segment)
                segment = []
        else:
            segment.append(token)
    if segment:
        segments.append(segment)

    commands: list[list[str]] = []
    for values in segments:
        consumed_values: list[str] = []
        while values:
            if shell == "zsh" and values[0] == "repeat":
                if len(values) < 3 or not re.fullmatch(r"[0-9]+", values[1]):
                    return False, []
                values = values[2:]
                continue
            if _is_shell_assignment_word(values[0]):
                consumed_values.append(values[0])
                values = values[1:]
                continue
            if values[0] in {
                "!",
                "do",
                "elif",
                "else",
                "if",
                "nocorrect",
                "noglob",
                "then",
                "until",
                "while",
            } or (shell == "zsh" and values[0] == "-"):
                values = values[1:]
                continue
            if values[0] == "time":
                values = values[1:]
                if values and values[0] in {"-p", "--portability"}:
                    values = values[1:]
                elif values and values[0].startswith("-"):
                    return False, []
                continue
            break
        if values and values[0] == "coproc":
            return False, []

        dispatcher_depth = 0
        while values and values[0] in {"builtin", "command", "exec"}:
            dispatcher_depth += 1
            if dispatcher_depth >= MAX_COMMAND_CONTEXT_DEPTH:
                return False, []
            dispatcher = values[0]
            values = values[1:]
            if dispatcher == "builtin":
                if values and values[0] == "--":
                    values = values[1:]
                elif values and values[0].startswith("-"):
                    return False, []
            elif dispatcher == "command":
                while values:
                    if values[0] in {"-v", "-V"}:
                        values = []
                        break
                    if values[0] == "-p":
                        values = values[1:]
                        continue
                    if values[0] == "--":
                        values = values[1:]
                        break
                    if values[0].startswith("-"):
                        return False, []
                    break
            else:
                while values and values[0].startswith("-"):
                    option = values[0]
                    if option == "--":
                        values = values[1:]
                        break
                    if option == "-a":
                        if len(values) < 2:
                            return False, []
                        consumed_values.append(values[1])
                        values = values[2:]
                    elif option != "-" and set(option[1:]) <= {"c", "l"}:
                        values = values[1:]
                    else:
                        return False, []
        if values and values[0] == "eval":
            return False, []
        if values:
            if shell == "zsh" and re.fullmatch(
                r"=[A-Za-z_][A-Za-z0-9_.+-]*",
                values[0],
            ):
                values[0] = values[0][1:]
            commands.append([*values, *consumed_values])
        elif consumed_values:
            commands.append(["true", *consumed_values])
    return True, commands


def _shell_script(args: list[str]) -> tuple[bool, str | None]:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return True, None
        flag, separator, _value = token.partition("=")
        if flag in SHELL_OPTIONS_WITH_VALUE:
            if separator:
                index += 1
            elif index + 1 >= len(args):
                return False, None
            else:
                index += 2
            continue
        if any(
            token.startswith(option) and len(token) > len(option)
            for option in ("-O", "+O", "-o", "+o")
        ):
            index += 1
            continue
        if token.startswith("-") and not token.startswith("--") and "c" in token[1:]:
            if index + 1 >= len(args):
                return False, None
            return True, args[index + 1]
        if token.startswith("-") or token.startswith("+"):
            index += 1
            continue
        return True, None
    return True, None


def _env_child_command(
    args: list[str],
    *,
    split_depth: int = 0,
    observed: list[str] | None = None,
) -> tuple[bool, tuple[str, list[str]] | None, list[str]]:
    normalized = [*(observed or []), *args]
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if _is_env_assignment(token) or token in ENV_OPTIONS_WITHOUT_VALUE:
            index += 1
            continue
        if token in {"-S", "--split-string"}:
            if index + 1 >= len(args) or split_depth >= MAX_COMMAND_CONTEXT_DEPTH:
                return False, None, normalized
            split_value = args[index + 1]
            if any(marker in split_value for marker in ("\\", "$", "`", "\v", "\f")):
                return False, None, normalized
            try:
                expanded = shlex.split(split_value, posix=True)
            except ValueError:
                return False, None, normalized
            expanded.extend(args[index + 2 :])
            return _env_child_command(
                expanded,
                split_depth=split_depth + 1,
                observed=normalized,
            )
        for prefix in ("-S", "--split-string="):
            if token.startswith(prefix) and len(token) > len(prefix):
                if split_depth >= MAX_COMMAND_CONTEXT_DEPTH:
                    return False, None, normalized
                split_value = token[len(prefix) :]
                if any(
                    marker in split_value
                    for marker in ("\\", "$", "`", "\v", "\f")
                ):
                    return False, None, normalized
                try:
                    expanded = shlex.split(split_value, posix=True)
                except ValueError:
                    return False, None, normalized
                expanded.extend(args[index + 1 :])
                return _env_child_command(
                    expanded,
                    split_depth=split_depth + 1,
                    observed=normalized,
                )
        flag, separator, _value = token.partition("=")
        if flag in ENV_OPTIONS_WITH_VALUE:
            if not separator:
                if index + 1 >= len(args):
                    return False, None, normalized
                index += 2
            else:
                index += 1
            continue
        if token.startswith("-u") and len(token) > len("-u"):
            index += 1
            continue
        if token.startswith("-C") and len(token) > len("-C"):
            index += 1
            continue
        if token.startswith("-"):
            return False, None, normalized
        break
    if index >= len(args):
        return True, None, normalized
    return True, (args[index], args[index + 1 :]), normalized


def _sudo_child_command(
    args: list[str],
) -> tuple[bool, tuple[str, list[str]] | None]:
    index = 0
    shell_mode = False
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if _is_sudo_assignment(token):
            index += 1
            continue
        flag, separator, _value = token.partition("=")
        if flag in SUDO_OPTIONS_WITH_VALUE:
            if not separator:
                if index + 1 >= len(args):
                    return False, None
                index += 2
            else:
                index += 1
            continue
        if flag in SUDO_OPTIONS_WITHOUT_VALUE:
            if flag in {"-i", "--login", "-s", "--shell"}:
                shell_mode = True
            index += 1
            continue
        if any(
            token.startswith(option) and len(token) > len(option)
            for option in ("-C", "-D", "-g", "-h", "-p", "-R", "-T", "-u")
        ):
            index += 1
            continue
        if token.startswith("-"):
            return False, None
        break
    if index >= len(args):
        return True, None
    if shell_mode:
        return False, None
    return True, (args[index], args[index + 1 :])


def _wrapper_command_after_options(
    args: list[str],
    *,
    options_with_value: frozenset[str],
    options_without_value: frozenset[str],
    attached_short_options: tuple[str, ...],
    legacy_numeric_options: bool = False,
) -> tuple[bool, int | None]:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            child = index + 1 if index + 1 < len(args) else None
            return True, child
        if not token.startswith("-") or token == "-":
            return True, index
        if legacy_numeric_options and re.fullmatch(r"-[0-9]+", token):
            index += 1
            continue
        flag, separator, _value = token.partition("=")
        if flag in options_with_value:
            if separator:
                index += 1
            elif index + 1 >= len(args):
                return False, None
            else:
                index += 2
            continue
        if token in options_without_value:
            index += 1
            continue
        if any(
            token.startswith(option) and len(token) > len(option)
            for option in attached_short_options
        ):
            index += 1
            continue
        return False, None
    return True, None


def _transparent_child_command(
    command: str,
    args: list[str],
) -> tuple[bool, tuple[str, list[str]] | None]:
    basename = _command_basename(command)
    if basename == "nohup":
        valid, child = _wrapper_command_after_options(
            args,
            options_with_value=frozenset(),
            options_without_value=frozenset({"--help", "--version"}),
            attached_short_options=(),
        )
    elif basename == "timeout":
        valid, duration = _wrapper_command_after_options(
            args,
            options_with_value=frozenset(
                {"-k", "--kill-after", "-s", "--signal"}
            ),
            options_without_value=frozenset(
                {
                    "--foreground",
                    "--help",
                    "--preserve-status",
                    "--verbose",
                    "--version",
                }
            ),
            attached_short_options=("-k", "-s"),
        )
        if not valid or duration is None or duration + 1 >= len(args):
            return False, None
        child = duration + 1
    elif basename == "nice":
        valid, child = _wrapper_command_after_options(
            args,
            options_with_value=frozenset({"-n", "--adjustment"}),
            options_without_value=frozenset({"--help", "--version"}),
            attached_short_options=("-n",),
            legacy_numeric_options=True,
        )
    elif basename == "stdbuf":
        valid, child = _wrapper_command_after_options(
            args,
            options_with_value=frozenset(
                {"-e", "--error", "-i", "--input", "-o", "--output"}
            ),
            options_without_value=frozenset({"--help", "--version"}),
            attached_short_options=("-e", "-i", "-o"),
        )
    else:
        return False, None

    if not valid:
        return False, None
    if child is None:
        return True, None
    return True, (args[child], args[child + 1 :])


def _command_context_contains_credential(
    command: str,
    args: list[str],
    *,
    depth: int = 0,
    credential_values: list[str] | None = None,
) -> bool:
    if depth >= MAX_COMMAND_CONTEXT_DEPTH:
        return True
    if _contains_encoded_option_token(command) or any(
        _contains_encoded_option_token(value) for value in args
    ):
        return True
    semantic_command = _fully_decoded(command)
    semantic_args = [_fully_decoded(value) for value in args]
    ambient_values = credential_values or []
    if _contains_credential(semantic_command, ambient_values) or any(
        _contains_credential(value, ambient_values) for value in semantic_args
    ):
        return True
    if _is_credential_carrier(semantic_command):
        return True
    safe_indexes = _contextually_safe_carrier_indexes(
        semantic_command,
        semantic_args,
    )
    if any(
        index not in safe_indexes and _is_credential_carrier(value)
        for index, value in enumerate(semantic_args)
    ):
        return True
    if _args_contain_targetless_oracle_logon(semantic_command, semantic_args):
        return True
    if _args_contain_command_specific_credential(semantic_command, semantic_args):
        return True

    basename = _command_basename(semantic_command)
    if basename in SHELL_COMMANDS:
        valid, script = _shell_script(semantic_args)
        if not valid:
            return True
        if script is None:
            return False
        try:
            normalized_tokens = shlex.split(script, posix=True)
        except ValueError:
            return True
        if any(
            _contains_credential(value, ambient_values)
            for value in normalized_tokens
        ):
            return True
        valid, commands = _shell_command_vectors(script, shell=basename)
        if not valid:
            return True
        return any(
            _command_context_contains_credential(
                values[0],
                values[1:],
                depth=depth + 1,
                credential_values=ambient_values,
            )
            for values in commands
        )

    if basename == "env":
        valid, child, normalized_args = _env_child_command(semantic_args)
        normalized_safe_indexes = _contextually_safe_carrier_indexes(
            "env",
            normalized_args,
        )
        if any(
            (
                index not in normalized_safe_indexes
                and _is_credential_carrier(value)
            )
            or _contains_credential(value, ambient_values)
            for index, value in enumerate(normalized_args)
        ):
            return True
        if not valid:
            return True
        if child is None:
            return False
        return _command_context_contains_credential(
            child[0],
            child[1],
            depth=depth + 1,
            credential_values=ambient_values,
        )

    if basename == "sudo":
        valid, child = _sudo_child_command(semantic_args)
        if not valid:
            return True
        if child is None:
            return False
        return _command_context_contains_credential(
            child[0],
            child[1],
            depth=depth + 1,
            credential_values=ambient_values,
        )

    if basename in {"nice", "nohup", "stdbuf", "timeout"}:
        valid, child = _transparent_child_command(semantic_command, semantic_args)
        if not valid:
            return True
        if child is None:
            return False
        return _command_context_contains_credential(
            child[0],
            child[1],
            depth=depth + 1,
            credential_values=ambient_values,
        )

    return False


def _contains_placeholder_syntax(token: str) -> bool:
    return any(
        "${" in variant or _contains_json_placeholder(variant)
        for variant in _decoded_variants(token)
    )


def _config_path() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override) / ".claude.json"
    return Path.home() / ".claude.json"


def _directory_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


@contextmanager
def _config_directory(config_path: Path) -> Iterator[int]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(config_path.parent, flags)
    except OSError as error:
        raise BlockedPlan("user configuration directory is not secure") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise BlockedPlan("user configuration directory is not secure")
        yield descriptor
    finally:
        os.close(descriptor)


def _read_optional_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _read_object_snapshot(
    path: Path,
    *,
    missing_ok: bool = False,
) -> ConfigSnapshot:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return ConfigSnapshot(raw=None, data={})
        raise
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value is not an object")
    return ConfigSnapshot(raw=raw, data=data)


def _read_object(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    return _read_object_snapshot(path, missing_ok=missing_ok).data


def _read_user_config(path: Path, directory_fd: int) -> ConfigSnapshot:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return ConfigSnapshot(raw=None, data={})
    except OSError as error:
        if error.errno == errno.ENOENT:
            return ConfigSnapshot(raw=None, data={})
        raise BlockedPlan("user configuration is not a private regular file") from error

    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise BlockedPlan("user configuration is not a private regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value is not an object")
    return ConfigSnapshot(raw=raw, data=data)


def _normalized(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": entry.get("type"),
        "command": entry.get("command"),
        "args": entry.get("args", []),
        "env": entry.get("env", {}),
    }


def _desired_config(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": entry.get("command"),
        "args": entry.get("args", []),
        "env": entry.get("env", {}),
    }


def _changed_fields(actual: dict[str, Any], desired: dict[str, Any]) -> list[str]:
    changed = [field for field in DIFF_FIELDS if actual.get(field) != desired[field]]
    if set(actual) - set(desired):
        changed.append("extra")
    return changed


def _servers(container: dict[str, Any]) -> dict[str, Any]:
    servers = container.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers is not an object")
    return servers


def _shadow_scopes(
    config: dict[str, Any],
    project_config: dict[str, Any],
    name: str,
) -> list[str]:
    shadows: list[str] = []
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("projects is not an object")
    for local in projects.values():
        if not isinstance(local, dict):
            raise ValueError("local project config is not an object")
        if name in _servers(local):
            shadows.append("local")
            break

    if name in _servers(project_config):
        shadows.append("project")
    return shadows


def _validate_manifest_entry(name: str, entry: dict[str, Any]) -> None:
    env = entry.get("env", {})
    if not isinstance(env, dict):
        raise BlockedPlan(f"user/{name}: env must be an object")

    credential_values = [
        value
        for key, value in os.environ.items()
        if value and _is_credential_name(key)
    ]
    for key, value in env.items():
        placeholder = PLACEHOLDER.fullmatch(value) if isinstance(value, str) else None
        if not isinstance(key, str) or placeholder is None:
            safe_key = key if isinstance(key, str) and key else "<invalid>"
            raise BlockedPlan(f"user/{name}: env.{safe_key} must use a placeholder")
        variable = placeholder.group("name")
        credential_bearing = _is_credential_name(key) or _is_credential_name(variable)
        default = placeholder.group("default")
        if default is not None:
            raise BlockedPlan(
                f"user/{name}: env.{key} placeholder may not have a default"
            )
        inherited_value = os.environ.get(variable)
        if credential_bearing and inherited_value:
            credential_values.append(inherited_value)

    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise BlockedPlan(f"user/{name}: command must be a non-empty string")
    if _contains_placeholder_syntax(command):
        raise BlockedPlan(
            f"user/{name}: command may only reference approved path placeholders; "
            "command may not reference credential variables or other placeholders"
        )
    if (
        _is_credential_carrier(command)
        or _is_oracle_logon_assignment(command)
        or _contains_json_oracle_logon(command)
    ):
        raise BlockedPlan(f"user/{name}: command may not contain credential carriers")
    if _contains_credential(command, credential_values):
        raise BlockedPlan(f"user/{name}: command matches a credential environment value")

    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
        raise BlockedPlan(f"user/{name}: args must be a string array")
    if any(
        _contains_placeholder_syntax(value) and value not in SAFE_ARG_EXPANSIONS
        for value in args
    ):
        raise BlockedPlan(
            f"user/{name}: args may only reference approved path placeholders; "
            "args may not reference credential variables or other placeholders"
        )
    if _command_context_contains_credential(command, args):
        raise BlockedPlan(f"user/{name}: args may not contain credential flags")
    if any(_contains_credential(value, credential_values) for value in args):
        raise BlockedPlan(f"user/{name}: args match a credential environment value")
    if _command_context_contains_credential(
        command,
        args,
        credential_values=credential_values,
    ):
        raise BlockedPlan(f"user/{name}: args may not contain credential flags")


def _validate_private_lock(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise BlockedPlan("MCP synchronization lock is not a private regular file")


@contextmanager
def _config_lock(config_path: Path, directory_fd: int) -> Iterator[None]:
    lock_name = f"{config_path.name}.mcp-sync.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_name, flags, 0o600, dir_fd=directory_fd)
    except OSError as error:
        raise BlockedPlan("MCP synchronization lock could not be secured") from error
    try:
        _validate_private_lock(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _create_private_temp(directory_fd: int, basename: str) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _attempt in range(128):
        name = f"{basename}.mcp-sync.{secrets.token_hex(12)}"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        return descriptor, name
    raise FileExistsError("could not allocate private temporary file")


def _atomic_apply(
    config_path: Path,
    expected_raw: bytes | None,
    replacements: dict[str, dict[str, Any]],
    local_migrations: set[str],
    expected_directory: tuple[int, int],
    project_config_path: Path,
    expected_project_raw: bytes | None,
) -> str:
    temporary_name: str | None = None
    committed = False
    try:
        with _config_directory(config_path) as directory_fd:
            if _directory_identity(directory_fd) != expected_directory:
                return "changed"
            try:
                with _config_lock(config_path, directory_fd):
                    locked = _read_user_config(config_path, directory_fd)
                    if locked.raw != expected_raw:
                        return "changed"

                    updated = copy.deepcopy(locked.data)
                    user_servers = updated.setdefault("mcpServers", {})
                    if not isinstance(user_servers, dict):
                        return "failed"
                    for name, desired in replacements.items():
                        user_servers[name] = copy.deepcopy(desired)

                    projects = updated.get("projects", {})
                    if not isinstance(projects, dict):
                        return "failed"
                    for local in projects.values():
                        if not isinstance(local, dict):
                            return "failed"
                        local_servers = local.get("mcpServers", {})
                        if not isinstance(local_servers, dict):
                            return "failed"
                        for name in local_migrations:
                            local_servers.pop(name, None)

                    payload = (
                        json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
                    ).encode("utf-8")
                    descriptor, temporary_name = _create_private_temp(
                        directory_fd,
                        config_path.name,
                    )
                    try:
                        os.fchmod(descriptor, 0o600)
                        stream = os.fdopen(descriptor, "wb")
                        descriptor = -1
                        with stream:
                            stream.write(payload)
                            stream.flush()
                            os.fsync(stream.fileno())
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)

                    before_commit = _read_user_config(config_path, directory_fd)
                    if before_commit.raw != expected_raw:
                        return "changed"
                    try:
                        current_project_raw = _read_optional_bytes(project_config_path)
                    except OSError:
                        return "changed"
                    if current_project_raw != expected_project_raw:
                        return "changed"

                    os.replace(
                        temporary_name,
                        config_path.name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                    temporary_name = None
                    committed = True
                    os.fsync(directory_fd)
                    return "applied"
            finally:
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                    except OSError:
                        pass
    except BlockedPlan:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return "durability-uncertain" if committed else "failed"


def check(
    manifest_path: Path,
    *,
    with_internal: bool,
    apply: bool,
    migrate_local: bool = False,
) -> int:
    manifest = _read_object(manifest_path)
    config_path = _config_path()
    with _config_directory(config_path) as directory_fd:
        config_directory = _directory_identity(directory_fd)
        snapshot = _read_user_config(config_path, directory_fd)
    existing_servers = _servers(snapshot.data)
    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())).resolve()
    project_config_path = project_dir / ".mcp.json"
    project_snapshot = _read_object_snapshot(project_config_path, missing_ok=True)

    pending = False
    blocked = False
    replacements: dict[str, dict[str, Any]] = {}
    local_migrations: set[str] = set()

    for name, raw_entry in manifest.items():
        if not isinstance(name, str) or not isinstance(raw_entry, dict):
            raise ValueError("invalid manifest entry")
        if raw_entry.get("internal") and not with_internal:
            continue
        if raw_entry.get("scope") != "user":
            raise ValueError("manifest entry does not declare user scope")
        _validate_manifest_entry(name, raw_entry)

        shadows = _shadow_scopes(snapshot.data, project_snapshot.data, name)
        blocking_shadows = [
            scope for scope in shadows if scope != "local" or not migrate_local
        ]
        if blocking_shadows:
            print(
                f"[SHADOWED] user/{name}: {','.join(blocking_shadows)}",
                file=sys.stderr,
            )
            blocked = True
            continue
        if "local" in shadows:
            local_migrations.add(name)
            pending = True

        desired = _desired_config(raw_entry)
        existing = existing_servers.get(name)
        if not isinstance(existing, dict):
            print(f"[MISSING] user/{name}")
            pending = True
            replacements[name] = desired
            continue

        changed = _changed_fields(existing, desired)
        if changed:
            print(f"[DRIFT] user/{name}: {','.join(changed)}")
            pending = True
            replacements[name] = desired
        else:
            print(f"[IN_SYNC] user/{name}")
            if name in local_migrations:
                replacements[name] = desired

    if blocked:
        return 1
    if not apply:
        return PENDING_EXIT if pending else 0
    if not pending:
        return 0

    result = _atomic_apply(
        config_path,
        snapshot.raw,
        replacements,
        local_migrations,
        config_directory,
        project_config_path,
        project_snapshot.raw,
    )
    if result == "changed":
        print("[BLOCKED] MCP configuration changed after preview", file=sys.stderr)
        return 1
    if result == "durability-uncertain":
        print(
            "[APPLIED_UNCONFIRMED] MCP configuration was replaced but durability "
            "could not be confirmed",
            file=sys.stderr,
        )
        return 1
    if result != "applied":
        print("[FAILED] MCP configuration was not updated", file=sys.stderr)
        return 1

    for name in replacements:
        if name in local_migrations:
            print(f"[MIGRATED] local/{name} -> user/{name}")
        print(f"[APPLIED] user/{name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--with-internal", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--migrate-local", action="store_true")
    args = parser.parse_args()
    if args.migrate_local and not args.apply:
        print("--migrate-local requires --apply", file=sys.stderr)
        return 64
    try:
        return check(
            args.manifest,
            with_internal=args.with_internal,
            apply=args.apply,
            migrate_local=args.migrate_local,
        )
    except BlockedPlan as error:
        print(f"[BLOCKED] {error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        print("[BLOCKED] MCP configuration could not be validated", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
