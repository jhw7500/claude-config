#!/usr/bin/python3 -I
"""Secure-store-only supervisor for the Project Control CLI."""

from __future__ import annotations

import base64
import errno
import grp
import json
import hmac
import os
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import quote_from_bytes, quote_plus


MAX_CONFIG_BYTES = 16 * 1024
MAX_PROVIDER_OUTPUT_BYTES = 64 * 1024
MAX_CONTROL_OUTPUT_BYTES = 12 * 1024
MAX_SECRET_BYTES = 16 * 1024
PROVIDER_TIMEOUT_SECONDS = 15.0
UNLOCK_TIMEOUT_SECONDS = 120.0
CONTROL_TIMEOUT_SECONDS = 600.0
TRUSTED_PATH = "/usr/local/bin:/usr/bin:/bin"
SAFE_KEYRING_BACKEND = "keyring.backends.SecretService.Keyring"
TASK_ID_RE = re.compile(r"^tsk-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
CLAIM_ID_RE = re.compile(r"^clm-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
PROJECT_ID_RE = re.compile(r"^prj-[a-z0-9][a-z0-9-]{1,62}$")
REPO_ID_RE = re.compile(r"^repo-[a-z0-9][a-z0-9-]{1,62}$")
WORKTREE_REF_RE = re.compile(r"^wt-[a-z0-9][a-z0-9-]{1,120}$")
GITHUB_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
OFFSET_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")
COMMON_CONTROL_ERROR_CODES = frozenset(
    {
        "UNEXPECTED", "INVALID_CLI_ARGUMENT", "COMMAND_TIMEOUT", "COMMAND_FAILED",
        "COMMAND_ABORTED", "COMMAND_OUTPUT_TOO_LARGE", "CLI_OUTPUT_TOO_LARGE",
        "RAW_OUTPUT_TOO_LARGE", "LOCK_ACQUIRE_FAILED", "LOCK_ACQUIRE_TIMEOUT",
        "LOCK_SPAWN_FAILED", "LOCK_SETUP_FAILED", "LOCK_BUSY", "LOCK_CONTENDED",
        "REGISTRY_MOVED_DURING_READ", "REGISTRY_CORRUPT", "REGISTRY_DIRTY",
        "REGISTRY_INDEX_UNSAFE", "REMOTE_DIVERGED", "REMOTE_VERIFY_FAILED",
        "INVALID_CONFIG", "MISSING_CREDENTIAL", "SENSITIVE_DATA_REJECTED",
        "SENSITIVE_OUTPUT_REJECTED", "SENSITIVE_SCAN_TOO_LARGE",
        "AUTHORITY_UNAVAILABLE", "AUTHORITY_EPOCH_ROLLBACK",
        "AUTHORITY_MOVED", "AUTHORITY_POLICY_NOT_LEGACY", "TOOL_VERSION_TOO_OLD",
    }
)
TASK_FINISH_ERROR_REASONS = {
    "HANDOFF_RETRY_CONFLICT": frozenset(
        {
            "invalid_git_state_line", "duplicate_git_state_key",
            "unexpected_git_state_key", "missing_git_state_key",
            "invalid_git_state_count", "missing_git_identity",
            "invalid_dirty_digest", "legacy_dirty_evidence_ambiguous",
            "git_identity_changed", "dirty_delta_changed",
            "handoff_metadata_mismatch", "retry_fields_changed",
        }
    ),
    "INVALID_WORKTREE_INSPECTION": frozenset({"duplicate_dirty_files"}),
    "WORKTREE_DIRTY": frozenset({"handoff_copy_not_plain_file"}),
}
COMMAND_CONTROL_ERROR_CODES = {
    "preflight": COMMON_CONTROL_ERROR_CODES
    | {
        "PREFLIGHT_UNAVAILABLE", "INVALID_PREFLIGHT_RESULT", "PREFLIGHT_RESTORE_FAILED",
        "PREFLIGHT_PROJECT_INTEGRITY", "INVALID_PREFLIGHT_ISSUE", "INVALID_PREFLIGHT_ITEM",
        "NOTION_GUARD_INDETERMINATE", "NOTION_WRITES_DISABLED", "PORTFOLIO_UNAVAILABLE",
        "CREDENTIALS_NOT_SEPARATE", "PROJECT_SCOPE_UNVERIFIABLE",
        "PROJECT_TOKEN_HAS_REPO_SCOPE", "PROJECT_SCOPE_MISSING", "PROJECT_SCOPE_NOT_EXACT",
        "UNSUPPORTED_REGISTRY_OWNER", "REGISTRY_ROOT_MISMATCH", "AMBIGUOUS_REGISTRY_REMOTE",
        "REGISTRY_REMOTE_NOT_SSH", "REGISTRY_REMOTE_MISMATCH", "PROJECT_NOT_PRIVATE",
        "REPOSITORY_NOT_PRIVATE", "INVALID_PROJECT_FIELDS", "INVALID_PROJECT_RESPONSE",
        "INVALID_REPOSITORY_RESPONSE", "DUPLICATE_PROJECT_ITEM", "DUPLICATE_PROJECT_RECORD",
        "REPOSITORY_IDENTITY_MISMATCH", "PROJECT_NOT_FOUND", "PROJECT_CHANGED_DURING_READ",
        "INCOMPLETE_PROJECT_FIELD_READ", "INVALID_PROJECT_MUTATION", "UNSAFE_STATE_PATH",
    },
    "portfolio status": COMMON_CONTROL_ERROR_CODES
    | {
        "PORTFOLIO_UNAVAILABLE", "INVALID_PORTFOLIO_RESULT", "PORTFOLIO_PAYLOAD_TOO_LARGE",
        "PORTFOLIO_ITEM_TOO_LARGE", "PORTFOLIO_REPOSITORY_SECTION_TOO_LARGE",
        "INVALID_PAGE_ID", "PROJECT_NOT_FOUND", "INVALID_PREFLIGHT_ITEM",
        "INVALID_PROJECT_RECORD", "REPOSITORY_NOT_FOUND",
        "PROJECT_NOT_PRIVATE", "INVALID_PROJECT_SOURCE", "PROJECT_CHANGED_DURING_READ",
        "INCOMPLETE_PROJECT_FIELD_READ", "INCOMPLETE_PROJECT_READ", "INVALID_PROJECT_FIELDS",
        "INVALID_PROJECT_ITEM", "INVALID_PROJECT_RESPONSE",
        "DUPLICATE_PROJECT_ITEM", "DUPLICATE_PROJECT_RECORD",
    },
    "task start": COMMON_CONTROL_ERROR_CODES
    | {
        "TASK_ALREADY_CLAIMED", "CLAIM_MISMATCH", "CLAIM_NOT_FOUND",
        "CLAIM_HISTORY_NOT_FOUND", "HOST_MISMATCH", "TASK_COMPLETED", "TASK_START_FAILED",
        "TASK_CONTRACT_REQUIRED", "TASK_CONTRACT_MISMATCH", "TASK_CONTRACT_ACTIVE",
        "TASK_SCOPE_MISMATCH", "TASK_ALIAS_MISMATCH", "TASK_ALIAS_CONFLICT",
        "ISSUE_IDENTITY_MISMATCH", "ISSUE_REVISION_MISMATCH", "ISSUE_REPOSITORY_MISMATCH",
        "INVALID_ISSUE_URL", "INVALID_ISSUE_RESPONSE", "INVALID_REPOSITORY_ID",
        "INVALID_REPOSITORY_RESPONSE", "REPOSITORY_IDENTITY_MISMATCH", "REPOSITORY_NOT_FOUND",
        "REPOSITORY_NOT_PRIVATE", "INVALID_CHECKOUT_PATH", "CHECKOUT_ROOT_MISMATCH",
        "INVALID_CHECKOUT_ORIGIN", "AMBIGUOUS_CHECKOUT_ORIGIN", "CHECKOUT_REMOTE_MISMATCH",
        "INVALID_TASK_ID", "INVALID_CLAIM_ID", "INVALID_CLAIM", "INVALID_CLOCK",
        "INVALID_FORMAL_TASK", "INVALID_TEMPORARY_TASK", "FORMAL_TASK_SOURCE_MISMATCH",
        "STALE_SOURCE_REVISION", "TEMPORARY_ALIAS_CONFLICT", "PROJECT_RECORD_NOT_FOUND",
        "PROJECT_REPOSITORY_MISMATCH", "WORKTREE_CLEANUP_REQUIRED", "WORKTREE_PLAN_MISMATCH",
        "WORKTREE_MAPPING_MISMATCH", "WORKTREE_LIFECYCLE_MISMATCH", "WORKTREE_CREATE_PENDING",
        "WORKTREE_MAPPING_AMBIGUOUS", "WORKTREE_BRANCH_MISMATCH", "WORKTREE_REPOSITORY_MISMATCH",
        "INVALID_WORKTREE_STATE", "INVALID_GIT_STATE", "INVALID_REPOSITORY_PATH",
        "UNSAFE_WORKTREE_PATH", "UNSAFE_WORKTREE_ROOT", "INVALID_WORKTREE_REF",
        "UNSAFE_STATE_PATH", "WORKTREE_STATE_WRITE_FAILED", "HANDOFF_MISSING",
        "INVALID_HANDOFF_PATH", "INVALID_HANDOFF_EVIDENCE",
        "INVALID_MUTATION_PATH", "MUTATION_PATH_MISMATCH",
        "INVALID_PROJECT_RESPONSE", "PROJECT_NOT_FOUND", "PROJECT_CHANGED_DURING_READ",
        "INCOMPLETE_PROJECT_READ", "INVALID_PREFLIGHT_ITEM", "INVALID_PROJECT_RECORD",
        "DUPLICATE_PROJECT_ITEM", "DUPLICATE_PROJECT_RECORD", "REGISTRY_ROOT_MISMATCH",
        "AMBIGUOUS_REGISTRY_REMOTE", "REGISTRY_REMOTE_NOT_SSH", "REGISTRY_REMOTE_MISMATCH",
        "PROJECT_NOT_PRIVATE",
        "PROJECT_REPOSITORY_NOT_FOUND", "PROJECT_REPOSITORY_AMBIGUOUS",
    },
    "task finish": COMMON_CONTROL_ERROR_CODES
    | {
        "CLAIM_MISMATCH", "CLAIM_NOT_FOUND", "HOST_MISMATCH", "TASK_COMPLETED",
        "INVALID_CLOCK", "SOURCE_REVISION_MISMATCH", "INVALID_FINISH_OUTCOME",
        "HANDOFF_MISSING", "HANDOFF_RETRY_CONFLICT", "INVALID_HANDOFF_EVIDENCE",
        "UNSAFE_HANDOFF_PATH", "INVALID_WORKTREE_INSPECTION", "WORKTREE_DIRTY",
        "WORKTREE_NOT_MAPPED", "WORKTREE_REMOVE_PENDING", "WORKTREE_REMOVED",
        "WORKTREE_PLAN_MISMATCH", "WORKTREE_CLAIM_MISMATCH",
        "WORKTREE_MAPPING_MISMATCH", "WORKTREE_BRANCH_MISMATCH",
        "WORKTREE_REPOSITORY_MISMATCH", "WORKTREE_CREATE_PENDING",
        "INVALID_WORKTREE_STATE", "INVALID_GIT_STATE", "INVALID_REPOSITORY_PATH",
        "UNSAFE_WORKTREE_PATH", "UNSAFE_WORKTREE_ROOT", "UNSAFE_STATE_PATH",
        "MUTATION_PATH_MISMATCH",
    },
}
CONFLICT_EXIT_CODES = frozenset({"TASK_ALREADY_CLAIMED", "CLAIM_MISMATCH", "CLAIM_NOT_FOUND"})
RETRY_EXIT_CODES = frozenset(
    {"REMOTE_DIVERGED", "REMOTE_VERIFY_FAILED", "REGISTRY_DIRTY", "LOCK_BUSY", "LOCK_CONTENDED", "LOCK_ACQUIRE_TIMEOUT"}
)
POLICY_EXIT_CODES = frozenset(
    {
        "AUTHORITY_UNAVAILABLE", "AUTHORITY_EPOCH_ROLLBACK", "AUTHORITY_POLICY_NOT_LEGACY",
        "TOOL_VERSION_TOO_OLD", "NOTION_GUARD_INDETERMINATE", "MISSING_CREDENTIAL",
        "PORTFOLIO_UNAVAILABLE", "PREFLIGHT_UNAVAILABLE", "INVALID_CONFIG",
        "CREDENTIALS_NOT_SEPARATE", "PROJECT_SCOPE_UNVERIFIABLE",
        "PROJECT_TOKEN_HAS_REPO_SCOPE", "PROJECT_SCOPE_MISSING", "PROJECT_SCOPE_NOT_EXACT",
        "UNSUPPORTED_REGISTRY_OWNER", "REGISTRY_REMOTE_NOT_SSH", "REGISTRY_REMOTE_MISMATCH",
        "AMBIGUOUS_REGISTRY_REMOTE", "PROJECT_NOT_PRIVATE", "REPOSITORY_NOT_PRIVATE",
        "COMMAND_TIMEOUT",
    }
)
HANDOFF_SECTION_NAMES = frozenset(
    {
        "Progress Since Last Checkpoint",
        "Git State",
        "Validation Performed",
        "Failures and Uncertainty",
        "Session-Local Next Step",
        "Related ADR and Evidence",
    }
)
CONTROL_CONFIG_NAMES = frozenset(
    {
        "JHW_REGISTRY_DIR",
        "JHW_REGISTRY_REMOTE",
        "JHW_REGISTRY_BRANCH",
        "JHW_WORKTREE_ROOT",
        "JHW_CONTROL_STATE_DIR",
        "JHW_BUILD_HOST",
        "JHW_GITHUB_OWNER",
        "JHW_PROJECT_NUMBER",
        "JHW_REGISTRY_REPOSITORY",
        "JHW_PREFLIGHT_PROJECT_ITEM_ID",
        "JHW_PREFLIGHT_REGISTRY_ISSUE_NUMBER",
    }
)
ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$"
)
UNSAFE_VALUE_CHARACTERS = frozenset("$`'\"\\;&|<>()")


class LauncherError(RuntimeError):
    """A path-free stable launcher failure."""

    def __init__(self, code: str, *, action: str | None = None, returncode: int = 78) -> None:
        super().__init__(code)
        self.code = code
        self.action = action
        self.returncode = returncode


class CommandTimeout(RuntimeError):
    """A child exceeded its fixed deadline."""


class CommandOutputTooLarge(RuntimeError):
    """A child exceeded its fixed capture budget."""


class CommandStartFailed(RuntimeError):
    """A fixed child executable could not be started."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass(frozen=True)
class ProgramResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass(frozen=True)
class HostTools:
    python: str
    gh: str
    node: str
    control: str


@dataclass(frozen=True)
class Credentials:
    project: str
    repository: str
    notion: str


def _json_bytes(value: object) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return (encoded + "\n").encode()


def _error_result(error: LauncherError) -> ProgramResult:
    body: dict[str, object] = {"error": {"code": error.code}}
    if error.action is not None:
        body["error"]["action"] = error.action  # type: ignore[index]
    return ProgramResult(error.returncode, stderr=_json_bytes(body))


def parse_control_config(payload: bytes) -> dict[str, str]:
    """Parse an exact, data-only set of host coordinates."""

    if len(payload) > MAX_CONFIG_BYTES:
        raise LauncherError("CONFIG_UNSAFE")
    if b"\0" in payload:
        raise LauncherError("CONFIG_INVALID")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise LauncherError("CONFIG_INVALID") from None

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT_RE.fullmatch(raw_line)
        if match is None:
            raise LauncherError("CONFIG_INVALID")
        name, value = match.groups()
        if name not in CONTROL_CONFIG_NAMES or name in values:
            raise LauncherError("CONFIG_INVALID")
        if not value or value != value.strip():
            raise LauncherError("CONFIG_INVALID")
        if any(character in UNSAFE_VALUE_CHARACTERS for character in value):
            raise LauncherError("CONFIG_INVALID")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise LauncherError("CONFIG_INVALID")
        values[name] = value

    if values.keys() != CONTROL_CONFIG_NAMES:
        raise LauncherError("CONFIG_INVALID")
    return values


def read_control_config(path: str | os.PathLike[str], *, uid: int | None = None) -> dict[str, str]:
    """Open, validate, and read the config without reopening its directory entry."""

    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(os.fspath(path), flags)
    except (OSError, TypeError):
        raise LauncherError("CONFIG_UNSAFE") from None

    try:
        metadata = os.fstat(descriptor)
        expected_uid = os.getuid() if uid is None else uid
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_CONFIG_BYTES
        ):
            raise LauncherError("CONFIG_UNSAFE")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CONFIG_BYTES:
                raise LauncherError("CONFIG_UNSAFE")
        return parse_control_config(b"".join(chunks))
    except LauncherError:
        raise
    except OSError:
        raise LauncherError("CONFIG_UNSAFE") from None
    finally:
        os.close(descriptor)


TASK_SUBCOMMANDS = (
    "start",
    "child-start",
    "contract",
    "completion-ready",
    "promote",
    "status",
    "handoff",
    "finish",
    "recover",
    "assert-owner",
)
TASK_SUBCOMMAND_SET = frozenset(TASK_SUBCOMMANDS)
MUTATING_TASK_SUBCOMMANDS = frozenset({
    "start", "child-start", "contract", "completion-ready", "promote", "finish",
})

CONTRACT = {
    "commands": [
        "unlock",
        "preflight",
        "portfolio status",
        *(f"task {subcommand}" for subcommand in TASK_SUBCOMMANDS),
    ],
    "credential_policy": "secure-store-only",
    "name": "jhw-control-host",
    "version": 4,
}


KEYRING_HELPER = """\
import json
import sys

try:
    import keyring
    import secretstorage
    from keyring.backends.SecretService import Keyring as SecretServiceKeyring
    from keyring.errors import KeyringLocked
except ImportError:
    raise SystemExit(20)

try:
    backend = keyring.get_keyring()
    if type(backend) is not SecretServiceKeyring:
        raise SystemExit(23)
    project = backend.get_password("jhw-control", "GH_PROJECT_TOKEN")
    notion = backend.get_password("jhw-control", "NOTION_API_KEY")
except KeyringLocked:
    raise SystemExit(21)
except BaseException:
    raise SystemExit(23)

sys.stdout.write(json.dumps({"backend": "keyring.backends.SecretService.Keyring", "project": project, "notion": notion}))
"""


UNLOCK_HELPER = """\
import os
import sys
import termios
import xml.etree.ElementTree as ElementTree

try:
    from gi.repository import Gio, GLib
except ImportError:
    raise SystemExit(20)

SERVICE_NAME = "org.freedesktop.secrets"
SERVICE_PATH = "/org/freedesktop/secrets"
LOGIN_COLLECTION = "/org/freedesktop/secrets/collection/login"
PRIVATE_INTERFACE = "org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface"
NO_AUTO_START = Gio.DBusCallFlags.NO_AUTO_START
CALL_TIMEOUT_MS = 5000


class UnlockFailure(RuntimeError):
    def __init__(self, returncode):
        super().__init__(returncode)
        self.returncode = returncode


def call(connection, destination, path, interface, method, parameters, reply_type):
    return connection.call_sync(
        destination,
        path,
        interface,
        method,
        parameters,
        GLib.VariantType.new(reply_type),
        NO_AUTO_START,
        CALL_TIMEOUT_MS,
        None,
    )


def collection_locked(connection, owner, collection):
    reply = call(
        connection,
        owner,
        collection,
        "org.freedesktop.DBus.Properties",
        "Get",
        GLib.Variant("(ss)", ("org.freedesktop.Secret.Collection", "Locked")),
        "(v)",
    )
    locked = reply.unpack()[0]
    if type(locked) is not bool:
        raise UnlockFailure(22)
    return locked


def validate_private_contract(connection, owner):
    xml_text = call(
        connection,
        owner,
        SERVICE_PATH,
        "org.freedesktop.DBus.Introspectable",
        "Introspect",
        GLib.Variant("()", ()),
        "(s)",
    ).unpack()[0]
    if not isinstance(xml_text, str) or len(xml_text.encode("utf-8")) > 131072:
        raise UnlockFailure(23)
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        raise UnlockFailure(23) from None
    interfaces = [
        value for value in root.findall("interface")
        if value.get("name") == PRIVATE_INTERFACE
    ]
    if len(interfaces) != 1:
        raise UnlockFailure(23)
    methods = [
        value for value in interfaces[0].findall("method")
        if value.get("name") == "UnlockWithMasterPassword"
    ]
    if len(methods) != 1:
        raise UnlockFailure(23)
    arguments = methods[0].findall("arg")
    inputs = [value.get("type") for value in arguments if value.get("direction", "in") == "in"]
    outputs = [value for value in arguments if value.get("direction", "in") != "in"]
    if inputs != ["o", "(oayays)"] or outputs:
        raise UnlockFailure(23)


def read_password(fd=0):
    if not os.isatty(fd):
        raise UnlockFailure(21)
    try:
        original = termios.tcgetattr(fd)
    except (OSError, termios.error):
        raise UnlockFailure(21) from None
    hidden = list(original)
    hidden[3] &= ~termios.ECHO
    password = bytearray()
    try:
        termios.tcsetattr(fd, termios.TCSAFLUSH, hidden)
        os.write(fd, b"Login keyring password: ")
        while True:
            chunk = os.read(fd, 1)
            if not chunk:
                raise UnlockFailure(25)
            if chunk in {b"\\n", b"\\r"}:
                break
            if len(password) >= 1024:
                raise UnlockFailure(21)
            password.extend(chunk)
        os.write(fd, b"\\n")
        if not password:
            raise UnlockFailure(25)
        return password
    except KeyboardInterrupt:
        raise UnlockFailure(25) from None
    except BaseException:
        for index in range(len(password)):
            password[index] = 0
        raise
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, original)
        except (OSError, termios.error):
            pass


def unlock_credential_store(connection, password_reader):
    owner = call(
        connection,
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "GetNameOwner",
        GLib.Variant("(s)", (SERVICE_NAME,)),
        "(s)",
    ).unpack()[0]
    validate_private_contract(connection, owner)
    collection = call(
        connection,
        owner,
        SERVICE_PATH,
        "org.freedesktop.Secret.Service",
        "ReadAlias",
        GLib.Variant("(s)", ("default",)),
        "(o)",
    ).unpack()[0]
    if collection != LOGIN_COLLECTION:
        raise UnlockFailure(22)
    if not collection_locked(connection, owner, collection):
        return "already-unlocked"
    password = password_reader()
    if not isinstance(password, bytearray) or not (1 <= len(password) <= 1024):
        raise UnlockFailure(21)
    session = None
    try:
        session = call(
            connection,
            owner,
            SERVICE_PATH,
            "org.freedesktop.Secret.Service",
            "OpenSession",
            GLib.Variant("(sv)", ("plain", GLib.Variant("s", ""))),
            "(vo)",
        ).unpack()[1]
        secret = GLib.Variant("(oayays)", (session, b"", password, "text/plain"))
        call(
            connection,
            owner,
            SERVICE_PATH,
            PRIVATE_INTERFACE,
            "UnlockWithMasterPassword",
            GLib.Variant("(o(oayays))", (collection, secret)),
            "()",
        )
        if collection_locked(connection, owner, collection):
            raise UnlockFailure(24)
        return "unlocked"
    finally:
        for index in range(len(password)):
            password[index] = 0
        if session is not None:
            try:
                call(
                    connection,
                    owner,
                    session,
                    "org.freedesktop.Secret.Session",
                    "Close",
                    GLib.Variant("()", ()),
                    "()",
                )
            except BaseException:
                pass


def open_connection():
    address = f"unix:path=/run/user/{os.getuid()}/bus"
    return Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )


def main(connection_factory=None, password_reader=None, output=None):
    selected_factory = open_connection if connection_factory is None else connection_factory
    selected_reader = read_password if password_reader is None else password_reader
    selected_output = sys.stdout if output is None else output
    connection = None
    try:
        connection = selected_factory()
        status = unlock_credential_store(connection, selected_reader)
    except UnlockFailure as error:
        return error.returncode
    except (KeyboardInterrupt, EOFError):
        return 25
    except BaseException:
        return 26
    finally:
        if connection is not None:
            try:
                connection.close_sync(None)
            except BaseException:
                pass
    if status == "already-unlocked":
        selected_output.write('{"status":"already-unlocked"}\\n')
    elif status == "unlocked":
        selected_output.write('{"status":"unlocked"}\\n')
    else:
        return 26
    selected_output.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


def run_bounded(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
    tty_input: bool = False,
) -> CommandResult:
    """Run a fixed child with bounded capture, deadline, and no interactive stdin."""

    selector: selectors.BaseSelector | None = None
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    process: subprocess.Popen[bytes] | None = None
    tty_stream = None
    tty_state = None
    try:
        try:
            if tty_input:
                tty_stream = open("/dev/tty", "r+b", buffering=0)
                if not os.isatty(tty_stream.fileno()):
                    raise OSError
                tty_state = termios.tcgetattr(tty_stream.fileno())
            process = subprocess.Popen(
                tuple(argv),
                env=dict(env),
                stdin=tty_stream if tty_stream is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError):
            raise CommandStartFailed from None
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + timeout_seconds
        total = 0
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandTimeout
            events = selector.select(remaining)
            if not events:
                raise CommandTimeout
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                total += len(chunk)
                if total > max_output_bytes:
                    raise CommandOutputTooLarge
                streams[key.data].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CommandTimeout
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise CommandTimeout from None
        return CommandResult(returncode, bytes(streams["stdout"]), bytes(streams["stderr"]))
    except BaseException:
        if process is not None:
            _kill_process_group(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        raise
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        if tty_stream is not None and tty_state is not None:
            try:
                termios.tcsetattr(tty_stream.fileno(), termios.TCSAFLUSH, tty_state)
            except (OSError, termios.error):
                pass
        if tty_stream is not None:
            tty_stream.close()


def _group_is_private_to_uid(gid: int, uid: int) -> bool:
    try:
        group = grp.getgrgid(gid)
        member_uids = {entry.pw_uid for entry in pwd.getpwall() if entry.pw_gid == gid}
        member_uids.update(pwd.getpwnam(name).pw_uid for name in group.gr_mem)
    except (KeyError, OSError):
        return False
    return member_uids <= {uid}


def _has_extended_posix_acl(path: Path, *, directory: bool) -> bool:
    names = ["system.posix_acl_access"]
    if directory:
        names.append("system.posix_acl_default")
    unsupported = {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}
    if hasattr(errno, "ENOATTR"):
        unsupported.add(errno.ENOATTR)
    for name in names:
        try:
            os.getxattr(path, name)
        except OSError as error:
            if error.errno in unsupported:
                continue
            return True
        else:
            return True
    return False


def _trusted_directory(path: Path, *, uid: int) -> bool:
    try:
        metadata = path.stat()
    except OSError:
        return False
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, uid}:
        return False
    if metadata.st_mode & stat.S_IWOTH:
        return (
            metadata.st_uid == 0
            and bool(metadata.st_mode & stat.S_ISVTX)
            and not _has_extended_posix_acl(path, directory=True)
        )
    return (
        not (metadata.st_mode & stat.S_IWGRP)
        or _group_is_private_to_uid(metadata.st_gid, uid)
    ) and not _has_extended_posix_acl(path, directory=True)


def _validated_executable(path: Path, *, uid: int) -> str:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        raise LauncherError("CONTROL_UNAVAILABLE") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, uid}
        or metadata.st_mode & stat.S_IWOTH
        or (metadata.st_mode & stat.S_IWGRP and not _group_is_private_to_uid(metadata.st_gid, uid))
        or _has_extended_posix_acl(resolved, directory=False)
        or not os.access(resolved, os.X_OK)
    ):
        raise LauncherError("CONTROL_UNAVAILABLE")
    original_parent = path.absolute().parent
    directory_chains = {original_parent, *original_parent.parents, resolved.parent, *resolved.parents}
    if any(not _trusted_directory(directory, uid=uid) for directory in directory_chains):
        raise LauncherError("CONTROL_UNAVAILABLE")
    return str(resolved)


def _validate_search_path(*, uid: int) -> None:
    """Validate every fixed PATH entry before any credential-bearing child starts."""

    for value in TRUSTED_PATH.split(":"):
        if not value or not value.startswith("/"):
            raise LauncherError("CONTROL_UNAVAILABLE")
        original = Path(value).absolute()
        try:
            resolved = original.resolve(strict=True)
            original_metadata = original.stat()
            resolved_metadata = resolved.stat()
        except OSError:
            raise LauncherError("CONTROL_UNAVAILABLE") from None
        if (
            not stat.S_ISDIR(original_metadata.st_mode)
            or not stat.S_ISDIR(resolved_metadata.st_mode)
            or original_metadata.st_mode & stat.S_IWOTH
            or resolved_metadata.st_mode & stat.S_IWOTH
        ):
            raise LauncherError("CONTROL_UNAVAILABLE")
        directory_chains = {original, *original.parents, resolved, *resolved.parents}
        if any(not _trusted_directory(directory, uid=uid) for directory in directory_chains):
            raise LauncherError("CONTROL_UNAVAILABLE")


def _trusted_tool(name: str, *, uid: int) -> str:
    _validate_search_path(uid=uid)
    for directory in TRUSTED_PATH.split(":"):
        candidate = Path(directory) / name
        if candidate.exists():
            return _validated_executable(candidate, uid=uid)
    raise LauncherError("CONTROL_UNAVAILABLE")


def resolve_host_tools(home: Path, *, uid: int) -> HostTools:
    """Resolve fixed production tools without consulting ambient PATH."""

    node_candidates = (home / ".local/bin/node", Path("/usr/local/bin/node"), Path("/usr/bin/node"))
    node: str | None = None
    for candidate in node_candidates:
        if candidate.exists():
            node = _validated_executable(candidate, uid=uid)
            break
    if node is None:
        raise LauncherError("CONTROL_UNAVAILABLE")
    return HostTools(
        python=_validated_executable(Path("/usr/bin/python3"), uid=uid),
        gh=_trusted_tool("gh", uid=uid),
        node=node,
        control=_validated_executable(home / ".local/bin/jhw-control", uid=uid),
    )


def _base_identity_environment(home: Path, source: Mapping[str, str], *, uid: int) -> dict[str, str]:
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = str(uid)
    locale = source.get("LANG", "C.UTF-8")
    if re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", locale) is None:
        locale = "C.UTF-8"
    return {
        "HOME": str(home),
        "LANG": locale,
        "LOGNAME": username,
        "NO_COLOR": "1",
        "PATH": TRUSTED_PATH,
        "USER": username,
    }


def _validated_session_bus(runtime: Path, *, uid: int) -> Path:
    bus = runtime / "bus"
    try:
        runtime_metadata = runtime.lstat()
        bus_metadata = bus.lstat()
    except OSError:
        raise LauncherError("OS_CREDENTIAL_STORE_UNAVAILABLE") from None
    if (
        not stat.S_ISDIR(runtime_metadata.st_mode)
        or runtime_metadata.st_uid != uid
        or stat.S_IMODE(runtime_metadata.st_mode) & 0o077
        or _has_extended_posix_acl(runtime, directory=True)
        or not stat.S_ISSOCK(bus_metadata.st_mode)
        or bus_metadata.st_uid != uid
        or _has_extended_posix_acl(bus, directory=False)
        or any(not _trusted_directory(parent, uid=uid) for parent in runtime.parents)
    ):
        raise LauncherError("OS_CREDENTIAL_STORE_UNAVAILABLE")
    return bus


def _session_bus_environment(*, uid: int) -> dict[str, str]:
    runtime = Path(f"/run/user/{uid}")
    bus = _validated_session_bus(runtime, uid=uid)
    return {
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}",
    }


def _provider_environment(home: Path, source: Mapping[str, str], *, uid: int) -> dict[str, str]:
    env = _base_identity_environment(home, source, uid=uid)
    env.update(_session_bus_environment(uid=uid))
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def _keyring_environment(home: Path, source: Mapping[str, str], *, uid: int) -> dict[str, str]:
    env = _provider_environment(home, source, uid=uid)
    env["PYTHON_KEYRING_BACKEND"] = SAFE_KEYRING_BACKEND
    return env


def _child_environment(
    home: Path,
    source: Mapping[str, str],
    config: Mapping[str, str],
    credentials: Credentials,
    *,
    uid: int,
) -> dict[str, str]:
    env = _base_identity_environment(home, source, uid=uid)
    env.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_TERMINAL_PROMPT": "0",
            **config,
            "GH_PROJECT_TOKEN": credentials.project,
            "GH_REPO_TOKEN": credentials.repository,
            "NOTION_API_KEY": credentials.notion,
        }
    )
    socket_path = source.get("SSH_AUTH_SOCK")
    if socket_path:
        try:
            metadata = os.stat(socket_path, follow_symlinks=False)
        except OSError:
            metadata = None
        if metadata is not None and stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == uid:
            env["SSH_AUTH_SOCK"] = socket_path
    return env


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LauncherError("CREDENTIAL_PROVIDER_INVALID")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON numeric constant")


def _parse_json(payload: bytes) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise LauncherError("CREDENTIAL_PROVIDER_INVALID") from None


def _unlock_credential_store(
    runner: Callable[..., CommandResult],
    python: str,
    env: Mapping[str, str],
) -> ProgramResult:
    try:
        result = runner(
            (python, "-I", "-c", UNLOCK_HELPER),
            env=env,
            timeout_seconds=UNLOCK_TIMEOUT_SECONDS,
            max_output_bytes=1024,
            tty_input=True,
        )
    except CommandTimeout:
        raise LauncherError("OS_CREDENTIAL_STORE_UNLOCK_TIMEOUT") from None
    except CommandOutputTooLarge:
        raise LauncherError("OS_CREDENTIAL_STORE_UNLOCK_FAILED") from None
    except KeyboardInterrupt:
        raise LauncherError("COMMAND_ABORTED") from None
    except (CommandStartFailed, OSError, ValueError):
        raise LauncherError(
            "INTERACTIVE_TTY_REQUIRED",
            action="run jhw-control-host unlock from an interactive terminal",
        ) from None
    if result.returncode != 0:
        if result.returncode == 20:
            raise LauncherError(
                "KEYRING_RUNTIME_UNAVAILABLE",
                action="install keyring and SecretStorage for /usr/bin/python3",
            )
        if result.returncode == 21:
            raise LauncherError(
                "INTERACTIVE_TTY_REQUIRED",
                action="run jhw-control-host unlock from an interactive terminal",
            )
        if result.returncode == 22:
            raise LauncherError("OS_CREDENTIAL_STORE_UNAVAILABLE")
        if result.returncode == 23:
            raise LauncherError("OS_CREDENTIAL_STORE_UNLOCK_UNSUPPORTED")
        if result.returncode == 25:
            raise LauncherError("COMMAND_ABORTED")
        raise LauncherError("OS_CREDENTIAL_STORE_UNLOCK_FAILED")
    if result.stderr:
        raise LauncherError("OS_CREDENTIAL_STORE_UNLOCK_FAILED")
    try:
        payload = _parse_json(result.stdout)
    except LauncherError:
        raise LauncherError("OS_CREDENTIAL_STORE_UNLOCK_FAILED") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"status"}
        or payload.get("status") not in {"unlocked", "already-unlocked"}
    ):
        raise LauncherError("OS_CREDENTIAL_STORE_UNLOCK_FAILED")
    return ProgramResult(
        0,
        stdout=_json_bytes({"command": "unlock", "result": {"status": payload["status"]}}),
    )


def _secret(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise LauncherError(code)
    encoded = value.encode("utf-8")
    if not (8 <= len(encoded) <= MAX_SECRET_BYTES):
        raise LauncherError(code)
    if value != value.strip() or any(not 0x21 <= ord(character) <= 0x7E for character in value):
        raise LauncherError(code)
    return value


def _provider_call(
    runner: Callable[..., CommandResult],
    argv: Sequence[str],
    env: Mapping[str, str],
) -> CommandResult:
    try:
        return runner(
            tuple(argv),
            env=env,
            timeout_seconds=PROVIDER_TIMEOUT_SECONDS,
            max_output_bytes=MAX_PROVIDER_OUTPUT_BYTES,
        )
    except CommandTimeout:
        raise LauncherError("CREDENTIAL_PROVIDER_TIMEOUT") from None
    except CommandOutputTooLarge:
        raise LauncherError("CREDENTIAL_PROVIDER_OUTPUT_TOO_LARGE") from None
    except (CommandStartFailed, OSError, ValueError):
        raise LauncherError("CREDENTIAL_PROVIDER_UNAVAILABLE") from None


def _load_keyring_credentials(
    runner: Callable[..., CommandResult],
    tools: HostTools,
    env: Mapping[str, str],
) -> tuple[str, str]:
    result = _provider_call(runner, (tools.python, "-I", "-c", KEYRING_HELPER), env)
    if result.returncode != 0:
        if result.returncode == 20:
            raise LauncherError(
                "KEYRING_RUNTIME_UNAVAILABLE",
                action="install keyring and SecretStorage for /usr/bin/python3",
            )
        if result.returncode == 21:
            raise LauncherError(
                "OS_CREDENTIAL_STORE_LOCKED",
                action="jhw-control-host unlock",
            )
        raise LauncherError(
            "OS_CREDENTIAL_STORE_UNAVAILABLE",
            action="ensure the OS Secret Service session is running",
        )
    payload = _parse_json(result.stdout)
    if not isinstance(payload, dict) or payload.get("backend") != SAFE_KEYRING_BACKEND:
        raise LauncherError("OS_CREDENTIAL_STORE_UNAVAILABLE")
    try:
        project = _secret(payload.get("project"), "PROJECT_CREDENTIAL_UNAVAILABLE")
    except LauncherError:
        raise LauncherError(
            "PROJECT_CREDENTIAL_UNAVAILABLE",
            action="/usr/bin/python3 -I -m keyring --keyring-backend keyring.backends.SecretService.Keyring set jhw-control GH_PROJECT_TOKEN",
        ) from None
    try:
        notion = _secret(payload.get("notion"), "NOTION_CREDENTIAL_UNAVAILABLE")
    except LauncherError:
        raise LauncherError(
            "NOTION_CREDENTIAL_UNAVAILABLE",
            action="/usr/bin/python3 -I -m keyring --keyring-backend keyring.backends.SecretService.Keyring set jhw-control NOTION_API_KEY",
        ) from None
    return project, notion


def _load_repository_credential(
    runner: Callable[..., CommandResult],
    tools: HostTools,
    env: Mapping[str, str],
    *,
    owner: str,
) -> str:
    login_action = "gh auth login --hostname github.com --git-protocol ssh --web"
    unavailable = LauncherError(
        "REPOSITORY_CREDENTIAL_UNAVAILABLE",
        action=login_action,
    )
    result = _provider_call(
        runner,
        (
            tools.gh,
            "auth",
            "status",
            "--hostname",
            "github.com",
            "--active",
            "--show-token",
            "--json",
            "hosts",
        ),
        env,
    )
    if result.returncode != 0:
        raise unavailable
    try:
        payload = _parse_json(result.stdout)
    except LauncherError:
        raise LauncherError(
            "REPOSITORY_CREDENTIAL_UNAVAILABLE",
            action=login_action,
        ) from None
    if not isinstance(payload, dict) or set(payload) != {"hosts"}:
        raise LauncherError(
            "REPOSITORY_CREDENTIAL_UNAVAILABLE",
            action=login_action,
        )
    hosts = payload.get("hosts")
    if not isinstance(hosts, dict) or set(hosts) != {"github.com"}:
        raise unavailable
    entries = hosts.get("github.com")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise unavailable
    entry = entries[0]
    if entry.get("tokenSource") != "keyring":
        raise LauncherError(
            "REPOSITORY_CREDENTIAL_NOT_SECURE",
            action=login_action,
        )
    if (
        entry.get("active") is not True
        or entry.get("host") != "github.com"
        or entry.get("login") != owner
        or entry.get("state") != "success"
    ):
        raise unavailable
    try:
        return _secret(entry.get("token"), "REPOSITORY_CREDENTIAL_UNAVAILABLE")
    except LauncherError:
        raise unavailable from None


def _allowed_invocation(argv: Sequence[str]) -> bool:
    values = tuple(argv)
    return (
        values == ("unlock",)
        or values == ("preflight",)
        or values[:2] == ("portfolio", "status")
        or (
            len(values) >= 2
            and values[0] == "task"
            and values[1] in TASK_SUBCOMMAND_SET
        )
    )


def _task_requires_preflight(argv: Sequence[str]) -> bool:
    values = tuple(argv)
    if len(values) < 2 or values[0] != "task":
        return False
    if values[1] in MUTATING_TASK_SUBCOMMANDS:
        return True
    if values[1] != "recover":
        return False
    action_positions = [
        index for index, value in enumerate(values)
        if value == "--action"
    ]
    return not (
        len(action_positions) == 1
        and action_positions[0] + 1 < len(values)
        and values[action_positions[0] + 1] == "status"
    )


def _control_call(
    runner: Callable[..., CommandResult],
    tools: HostTools,
    argv: Sequence[str],
    env: Mapping[str, str],
) -> CommandResult:
    try:
        return runner(
            (tools.node, tools.control, *argv),
            env=env,
            timeout_seconds=CONTROL_TIMEOUT_SECONDS,
            max_output_bytes=MAX_CONTROL_OUTPUT_BYTES,
        )
    except CommandTimeout:
        raise LauncherError("CONTROL_TIMEOUT") from None
    except CommandOutputTooLarge:
        raise LauncherError("CONTROL_OUTPUT_TOO_LARGE") from None
    except (CommandStartFailed, OSError, ValueError):
        raise LauncherError("CONTROL_UNAVAILABLE") from None


def _exact_object(value: object, required: set[str], optional: set[str] | None = None) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    allowed = required | (set() if optional is None else optional)
    if set(value) - allowed or not required.issubset(value):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return value


def _required_object(value: object, required: set[str]) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or not all(isinstance(key, str) for key in value)
        or not required.issubset(value)
    ):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return value


def _bounded_text(value: object, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return value


def _bounded_zod_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    utf16_units = len(value.encode("utf-16-le")) // 2
    if utf16_units > maximum:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return value


def _bounded_zod_utf8_text(value: object, *, maximum: int) -> str:
    text = _bounded_zod_text(value, maximum=maximum)
    if len(text.encode("utf-8")) > maximum:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return text


def _canonical_id(value: object, pattern: re.Pattern[str]) -> str:
    text = _bounded_text(value, maximum=128)
    if pattern.fullmatch(text) is None:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return text


def _requested_id(
    command: Sequence[str],
    flag: str,
    pattern: re.Pattern[str],
) -> str | None:
    positions = [index for index, value in enumerate(command) if value == flag]
    if not positions:
        return None
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return _canonical_id(command[positions[0] + 1], pattern)


def _requested_literal(request: Sequence[str], flag: str, allowed: set[str]) -> str:
    positions = [index for index, value in enumerate(request) if value == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(request):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    selected = _bounded_text(request[positions[0] + 1], maximum=32)
    if selected not in allowed:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return selected


def _timestamp(value: object) -> str:
    text = _bounded_text(value, maximum=64)
    if OFFSET_DATETIME_RE.fullmatch(text) is None:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise LauncherError("CONTROL_OUTPUT_INVALID") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return text


def _worktree_coordinates(task_id: str, worktree_value: object, branch_value: object) -> tuple[str, str]:
    worktree_ref = _canonical_id(worktree_value, WORKTREE_REF_RE)
    branch = _bounded_text(branch_value, maximum=255)
    if (
        not worktree_ref.startswith(f"wt-{task_id[-12:]}-")
        or branch != f"task/{worktree_ref[3:]}"
    ):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return worktree_ref, branch


def _warning(value: object, *, registration: bool) -> dict[str, str]:
    warning = _exact_object(value, {"code"})
    code = warning.get("code")
    allowed = (
        {"REGISTRATION_RECORD_UNREADABLE", "REGISTRATION_RECORD_UNWRITABLE", "REGISTRATION_RECORD_AT_CAPACITY"}
        if registration
        else {"JOURNAL_WRITE_FAILED"}
    )
    if code not in allowed:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return {"code": str(code)}


def _output_warnings(payload: Mapping[str, object]) -> dict[str, object]:
    warnings: dict[str, object] = {}
    if "journal_warning" in payload:
        warnings["journal_warning"] = _warning(payload["journal_warning"], registration=False)
    if "registration_record_warning" in payload:
        warnings["registration_record_warning"] = _warning(
            payload["registration_record_warning"],
            registration=True,
        )
    return warnings


def _control_json(payload: bytes) -> dict[str, object]:
    try:
        parsed = _parse_json(payload)
    except LauncherError:
        raise LauncherError("CONTROL_OUTPUT_INVALID") from None
    if not isinstance(parsed, dict):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return parsed


def _validate_preflight_result(value: object) -> dict[str, object]:
    result = _exact_object(value, {"status", "checks"})
    if result.get("status") != "ready":
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    checks = _exact_object(
        result.get("checks"),
        {
            "credentials", "authority", "notion_guard", "project",
            "registry_repository", "registry_issue", "registry_git",
        },
    )
    if any(checks[name] != "ok" for name in checks):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return {"status": "ready", "checks": {name: "ok" for name in sorted(checks)}}


def _validate_portfolio_result(value: object) -> dict[str, object]:
    result = _exact_object(
        value,
        {"page_id", "markdown", "items", "repositories", "truncated", "total_items"},
        {"next_page_id"},
    )
    page_id = _bounded_text(result.get("page_id"), maximum=32)
    if re.fullmatch(r"page-[1-9][0-9]*", page_id) is None:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    if not isinstance(result.get("markdown"), str) or len(result["markdown"].encode("utf-8")) > 12 * 1024:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    truncated = result.get("truncated")
    total_items = result.get("total_items")
    if not isinstance(truncated, bool) or not isinstance(total_items, int) or isinstance(total_items, bool) or total_items < 0:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    next_page = result.get("next_page_id")
    if (next_page is not None) != truncated:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    if next_page is not None and re.fullmatch(r"page-[1-9][0-9]*", _bounded_text(next_page, maximum=32)) is None:
        raise LauncherError("CONTROL_OUTPUT_INVALID")

    raw_items = result.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > 20:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    items: list[dict[str, object]] = []
    for raw in raw_items:
        item = _exact_object(
            raw,
            {"project_item_id", "source_node_id", "project_id", "title", "objective", "repo_ids", "fields", "stale"},
        )
        _bounded_zod_utf8_text(item.get("project_item_id"), maximum=256)
        _bounded_zod_utf8_text(item.get("source_node_id"), maximum=128)
        project_id = _canonical_id(item.get("project_id"), PROJECT_ID_RE)
        title = _bounded_zod_text(item.get("title"), maximum=256)
        _bounded_zod_text(item.get("objective"), maximum=4096)
        repo_ids = item.get("repo_ids")
        if not isinstance(repo_ids, list) or not (1 <= len(repo_ids) <= 64):
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        selected_repo_ids = [_canonical_id(repo_id, REPO_ID_RE) for repo_id in repo_ids]
        fields = _exact_object(item.get("fields"), {"status", "priority", "health", "next_action", "last_reviewed"})
        if (
            fields.get("status") not in {"proposed", "active", "paused", "completed", "cancelled"}
            or fields.get("priority") not in {"P0", "P1", "P2", "P3"}
            or fields.get("health") not in {"on-track", "at-risk", "blocked", "unknown"}
        ):
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        next_action = _bounded_zod_text(fields.get("next_action"), maximum=165)
        if (
            re.fullmatch(rf"task:{TASK_ID_RE.pattern[1:-1]}", next_action) is None
            and re.fullmatch(r"wait:[^\x00-\x1f\x7f]{1,160}", next_action) is None
        ):
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        last_reviewed = _bounded_text(fields.get("last_reviewed"), maximum=10)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", last_reviewed) is None:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        try:
            datetime.strptime(last_reviewed, "%Y-%m-%d")
        except ValueError:
            raise LauncherError("CONTROL_OUTPUT_INVALID") from None
        if not isinstance(item.get("stale"), bool):
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        items.append({"project_id": project_id, "title": title, "repo_ids": selected_repo_ids})

    raw_repositories = result.get("repositories")
    if not isinstance(raw_repositories, list):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    repositories: list[dict[str, object]] = []
    for raw in raw_repositories:
        repository = _exact_object(raw, {"repo_id", "slug", "allow_public"})
        repo_id = _canonical_id(repository.get("repo_id"), REPO_ID_RE)
        slug = _bounded_text(repository.get("slug"), maximum=140)
        if GITHUB_SLUG_RE.fullmatch(slug) is None or not isinstance(repository.get("allow_public"), bool):
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        repositories.append({"repo_id": repo_id, "slug": slug, "allow_public": repository["allow_public"]})
    return {
        "page_id": page_id,
        "items": items,
        "repositories": repositories,
        "truncated": truncated,
        "total_items": total_items,
        **({"next_page_id": next_page} if next_page is not None else {}),
    }


def _validate_latest_handoff(value: object, *, task_id: str) -> dict[str, object]:
    handoff = _exact_object(value, {"handoff_pointer", "claim_id", "generated_at", "sections", "truncated"})
    claim_id = _canonical_id(handoff.get("claim_id"), CLAIM_ID_RE)
    pointer = _bounded_text(handoff.get("handoff_pointer"), maximum=512)
    if pointer != f"handoffs/{task_id}/{claim_id}.md":
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    sections = _exact_object(handoff.get("sections"), set(HANDOFF_SECTION_NAMES))
    if any(not isinstance(text, str) for text in sections.values()) or not isinstance(handoff.get("truncated"), bool):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    return {
        "handoff_pointer": pointer,
        "claim_id": claim_id,
        "generated_at": _timestamp(handoff.get("generated_at")),
        "sections": sections,
        "truncated": handoff["truncated"],
    }


def _validate_task_start_result(
    value: object,
    *,
    build_host: str,
    request: Sequence[str],
) -> dict[str, object]:
    result = _required_object(
        value,
        {"task", "claim", "branch", "worktree_ref"},
    )
    task = _required_object(
        result.get("task"),
        {"task_id", "project_id", "repo_id"},
    )
    claim = _required_object(
        result.get("claim"),
        {"task_id", "claim_id", "project_id", "repo_id", "host", "branch", "worktree_ref", "started_at"},
    )
    task_id = _canonical_id(task.get("task_id"), TASK_ID_RE)
    claim_id = _canonical_id(claim.get("claim_id"), CLAIM_ID_RE)
    project_id = _canonical_id(task.get("project_id"), PROJECT_ID_RE)
    repo_id = _canonical_id(task.get("repo_id"), REPO_ID_RE)
    requested_task_id = _requested_id(request, "--task", TASK_ID_RE)
    requested_project_id = _requested_id(request, "--project", PROJECT_ID_RE)
    requested_repo_id = _requested_id(request, "--repo-id", REPO_ID_RE)
    worktree_ref, branch = _worktree_coordinates(
        task_id,
        result.get("worktree_ref"),
        result.get("branch"),
    )
    if (
        claim.get("task_id") != task_id
        or claim.get("project_id") != project_id
        or claim.get("repo_id") != repo_id
        or claim.get("branch") != branch
        or claim.get("worktree_ref") != worktree_ref
        or claim.get("host") != build_host
        or (requested_task_id is not None and task_id != requested_task_id)
        or (requested_project_id is not None and project_id != requested_project_id)
        or (requested_repo_id is not None and repo_id != requested_repo_id)
    ):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    _timestamp(claim.get("started_at"))
    projected: dict[str, object] = {
        "task_id": task_id,
        "claim_id": claim_id,
        "branch": branch,
        "worktree_ref": worktree_ref,
    }
    if "latest_handoff" in result:
        projected["latest_handoff"] = _validate_latest_handoff(result["latest_handoff"], task_id=task_id)
    return projected


def _validate_task_finish_result(value: object, *, request: Sequence[str]) -> dict[str, object]:
    result = _required_object(
        value,
        {"task_id", "claim_id", "status", "released_at", "worktree_removed"},
    )
    task_id = _canonical_id(result["task_id"], TASK_ID_RE)
    claim_id = _canonical_id(result["claim_id"], CLAIM_ID_RE)
    requested_task = _requested_id(request, "--task", TASK_ID_RE)
    requested_claim = _requested_id(request, "--claim", CLAIM_ID_RE)
    requested_status = _requested_literal(request, "--status", {"completed", "handoff", "abandoned"})
    if (task_id, claim_id, result["status"]) != (requested_task, requested_claim, requested_status):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    released_at = _timestamp(result["released_at"])
    removed = result["worktree_removed"]
    if not isinstance(removed, bool):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    cleanup = result.get("cleanup_error")
    pointer = result.get("handoff_pointer")
    projected: dict[str, object] = {
        "task_id": task_id,
        "claim_id": claim_id,
        "status": requested_status,
        "released_at": released_at,
        "worktree_removed": removed,
    }
    if requested_status == "handoff":
        expected_pointer = f"handoffs/{task_id}/{claim_id}.md"
        if removed or cleanup is not None or pointer != expected_pointer:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        projected["handoff_pointer"] = expected_pointer
    else:
        if pointer is not None:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        if cleanup is None:
            if not removed:
                raise LauncherError("CONTROL_OUTPUT_INVALID")
        else:
            if cleanup != "WORKTREE_CLEANUP_FAILED" or removed:
                raise LauncherError("CONTROL_OUTPUT_INVALID")
            projected["cleanup_error"] = cleanup
    return projected


def _validate_generic_task_result(value: object) -> dict[str, object]:
    return dict(_required_object(value, set()))


def _validate_error_result(
    value: object,
    *,
    command: str,
    request: Sequence[str],
) -> dict[str, object]:
    error = _exact_object(value, {"code"}, {"reason", "conflicting_claim", "retained_claim"})
    code = error.get("code")
    if not isinstance(code, str) or code not in COMMAND_CONTROL_ERROR_CODES.get(command, frozenset()):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    projected: dict[str, object] = {"code": code}
    if "reason" in error:
        reason = error["reason"]
        allowed_reasons = TASK_FINISH_ERROR_REASONS.get(str(code), frozenset())
        if command != "task finish" or not isinstance(reason, str) or reason not in allowed_reasons:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        projected["reason"] = reason
    if "conflicting_claim" in error:
        if code != "TASK_ALREADY_CLAIMED":
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        conflict = _exact_object(
            error["conflicting_claim"],
            {"task_id", "claim_id", "host", "branch", "worktree_ref", "started_at"},
        )
        task_id = _canonical_id(conflict.get("task_id"), TASK_ID_RE)
        requested_task_id = _requested_id(request, "--task", TASK_ID_RE)
        worktree_ref, branch = _worktree_coordinates(
            task_id,
            conflict.get("worktree_ref"),
            conflict.get("branch"),
        )
        _bounded_text(conflict.get("host"), maximum=255)
        if requested_task_id is not None and task_id != requested_task_id:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        projected["conflicting_claim"] = {
            "task_id": task_id,
            "claim_id": _canonical_id(conflict.get("claim_id"), CLAIM_ID_RE),
            "branch": branch,
            "worktree_ref": worktree_ref,
            "started_at": _timestamp(conflict.get("started_at")),
        }
    if "retained_claim" in error:
        if command != "task start" or code == "TASK_ALREADY_CLAIMED":
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        retained = _exact_object(error["retained_claim"], {"task_id", "claim_id", "state"})
        if retained.get("state") not in {"active", "released"}:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        retained_task_id = _canonical_id(retained.get("task_id"), TASK_ID_RE)
        requested_task_id = _requested_id(request, "--task", TASK_ID_RE)
        if requested_task_id is not None and retained_task_id != requested_task_id:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        projected["retained_claim"] = {
            "task_id": retained_task_id,
            "claim_id": _canonical_id(retained.get("claim_id"), CLAIM_ID_RE),
            "state": retained["state"],
        }
    return projected


def _expected_error_returncode(code: str, *, command: str) -> int:
    if code in CONFLICT_EXIT_CODES:
        return 4
    if code in RETRY_EXIT_CODES:
        return 75
    if code == "INVALID_CLI_ARGUMENT":
        return 2
    if command == "preflight" or code in POLICY_EXIT_CODES:
        return 78
    return 1


def _validated_control_result(result: CommandResult, command: Sequence[str], *, build_host: str) -> ProgramResult:
    if result.returncode not in {0, 1, 2, 4, 75, 78}:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    if result.returncode == 0:
        if result.stderr or not result.stdout:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        payload = _exact_object(
            _control_json(result.stdout),
            {"command", "result"},
            {"journal_warning", "registration_record_warning"},
        )
        expected = " ".join(command[:2]) if command and command[0] in {"portfolio", "task"} else " ".join(command)
        if payload.get("command") != expected:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        if expected == "preflight":
            projected_result = _validate_preflight_result(payload.get("result"))
        elif expected == "portfolio status":
            projected_result = _validate_portfolio_result(payload.get("result"))
        elif expected in {"task start", "task child-start"}:
            projected_result = _validate_task_start_result(
                payload.get("result"),
                build_host=build_host,
                request=command,
            )
            if expected == "task child-start":
                projected_result.pop("latest_handoff", None)
        elif expected == "task finish":
            projected_result = _validate_task_finish_result(payload.get("result"), request=command)
        elif expected.startswith("task ") and expected.split(" ", 1)[1] in TASK_SUBCOMMAND_SET:
            projected_result = _validate_generic_task_result(payload.get("result"))
        else:
            raise LauncherError("CONTROL_OUTPUT_INVALID")
        output = {"command": expected, "result": projected_result, **_output_warnings(payload)}
        return ProgramResult(0, stdout=_json_bytes(output))
    if result.stdout or not result.stderr:
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    payload = _exact_object(
        _control_json(result.stderr),
        {"error"},
        {"journal_warning", "registration_record_warning"},
    )
    expected = " ".join(command[:2]) if command and command[0] in {"portfolio", "task"} else " ".join(command)
    validated_error = _validate_error_result(payload.get("error"), command=expected, request=command)
    if result.returncode != _expected_error_returncode(str(validated_error["code"]), command=expected):
        raise LauncherError("CONTROL_OUTPUT_INVALID")
    output = {
        "error": validated_error,
        **_output_warnings(payload),
    }
    return ProgramResult(result.returncode, stderr=_json_bytes(output))


def _program_result(
    result: CommandResult,
    *,
    command: Sequence[str],
    credentials: Credentials,
    protected_paths: Sequence[Path | str],
    build_host: str,
) -> ProgramResult:
    canaries = [credentials.project, credentials.repository, credentials.notion]
    canaries.extend(os.fspath(path) for path in protected_paths)
    byte_canaries: set[bytes] = set()
    hex_canaries: set[bytes] = set()
    percent_canaries: set[bytes] = set()

    def uppercase_percent_escapes(value: bytes) -> bytes:
        return re.sub(rb"%[0-9A-Fa-f]{2}", lambda match: match.group(0).upper(), value)

    for canary in canaries:
        if not canary:
            continue
        raw = canary.encode("utf-8")
        standard_base64 = base64.b64encode(raw)
        urlsafe_base64 = base64.urlsafe_b64encode(raw)
        full_percent_upper = b"".join(f"%{byte:02X}".encode("ascii") for byte in raw)
        quoted = quote_from_bytes(raw, safe="").encode("ascii")
        quoted_plus = quote_plus(raw, safe="").encode("ascii")

        def lowercase_percent_escapes(value: bytes) -> bytes:
            return re.sub(rb"%[0-9A-Fa-f]{2}", lambda match: match.group(0).lower(), value)

        byte_canaries.update(
            {
                raw,
                standard_base64,
                standard_base64.rstrip(b"="),
                urlsafe_base64,
                urlsafe_base64.rstrip(b"="),
                raw.hex().encode("ascii"),
                raw.hex().upper().encode("ascii"),
                full_percent_upper,
                full_percent_upper.lower(),
                quoted,
                lowercase_percent_escapes(quoted),
                quoted_plus,
                lowercase_percent_escapes(quoted_plus),
            }
        )
        hex_canaries.add(raw.hex().encode("ascii"))
        percent_canaries.update(
            {
                full_percent_upper,
                uppercase_percent_escapes(quoted),
                uppercase_percent_escapes(quoted_plus),
            }
        )
    byte_canaries.discard(b"")
    hex_canaries.discard(b"")
    percent_canaries.discard(b"")
    surfaces = (result.stdout, result.stderr, result.stdout + result.stderr, result.stderr + result.stdout)
    decoded_strings: list[bytes] = []

    def collect_strings(value: object) -> None:
        if isinstance(value, str):
            decoded_strings.append(value.encode("utf-8"))
        elif isinstance(value, list):
            for item in value:
                collect_strings(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect_strings(key)
                collect_strings(item)

    for stream in (result.stdout, result.stderr):
        try:
            collect_strings(json.loads(stream.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    scanned_surfaces = (*surfaces, *decoded_strings)
    compact_surfaces = tuple(b"".join(surface.split()) for surface in scanned_surfaces)
    all_surfaces = (*scanned_surfaces, *compact_surfaces)
    percent_surfaces = tuple(uppercase_percent_escapes(surface) for surface in all_surfaces)
    lowercase_surfaces = tuple(surface.lower() for surface in all_surfaces)
    if (
        any(canary in surface for canary in byte_canaries for surface in all_surfaces)
        or any(canary in surface for canary in hex_canaries for surface in lowercase_surfaces)
        or any(canary in surface for canary in percent_canaries for surface in percent_surfaces)
    ):
        return _error_result(LauncherError("SENSITIVE_OUTPUT_REJECTED"))
    try:
        return _validated_control_result(result, command, build_host=build_host)
    except LauncherError:
        return _error_result(LauncherError("CONTROL_OUTPUT_INVALID"))


def run_program(
    argv: Sequence[str],
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    uid: int | None = None,
    command_runner: Callable[..., CommandResult] | None = None,
    tools: HostTools | None = None,
) -> ProgramResult:
    """Run one launcher invocation; injectable parameters are for tests."""

    if list(argv) == ["--contract"]:
        return ProgramResult(0, stdout=_json_bytes(CONTRACT))
    if not _allowed_invocation(argv):
        return _error_result(LauncherError("INVALID_ARGUMENT", returncode=2))

    selected_uid = os.getuid() if uid is None else uid
    source_environment = os.environ if environment is None else environment
    if home is None:
        try:
            selected_home = Path(pwd.getpwuid(selected_uid).pw_dir)
        except KeyError:
            return _error_result(LauncherError("CONFIG_UNSAFE"))
    else:
        selected_home = Path(home)
    runner = run_bounded if command_runner is None else command_runner

    try:
        if tuple(argv) == ("unlock",):
            python = (
                _validated_executable(Path("/usr/bin/python3"), uid=selected_uid)
                if tools is None
                else tools.python
            )
            return _unlock_credential_store(
                runner,
                python,
                _provider_environment(selected_home, source_environment, uid=selected_uid),
            )
        config_path = selected_home / ".config" / "jhw-control" / "control.env"
        config = read_control_config(config_path, uid=selected_uid)
        selected_tools = resolve_host_tools(selected_home, uid=selected_uid) if tools is None else tools
        provider_env = _provider_environment(selected_home, source_environment, uid=selected_uid)
        keyring_env = _keyring_environment(selected_home, source_environment, uid=selected_uid)
        project, notion = _load_keyring_credentials(runner, selected_tools, keyring_env)
        repository = _load_repository_credential(
            runner,
            selected_tools,
            provider_env,
            owner=config["JHW_GITHUB_OWNER"],
        )
        if hmac.compare_digest(project, repository):
            raise LauncherError("CREDENTIALS_NOT_SEPARATE")
        credentials = Credentials(project=project, repository=repository, notion=notion)
        child_env = _child_environment(
            selected_home,
            source_environment,
            config,
            credentials,
            uid=selected_uid,
        )
        protected_paths: tuple[Path | str, ...] = (
            config_path,
            selected_home / ".config" / "gh",
            selected_home / ".local" / "share" / "keyrings",
            config["JHW_REGISTRY_DIR"],
            config["JHW_WORKTREE_ROOT"],
            config["JHW_CONTROL_STATE_DIR"],
            *(value for index, value in enumerate(argv) if index and argv[index - 1] == "--repo-path"),
            *(value for value in (child_env.get("SSH_AUTH_SOCK"),) if value),
        )
        if _task_requires_preflight(argv):
            preflight = _program_result(
                _control_call(runner, selected_tools, ("preflight",), child_env),
                command=("preflight",),
                credentials=credentials,
                protected_paths=protected_paths,
                build_host=config["JHW_BUILD_HOST"],
            )
            if preflight.returncode != 0:
                return preflight
        return _program_result(
            _control_call(runner, selected_tools, tuple(argv), child_env),
            command=tuple(argv),
            credentials=credentials,
            protected_paths=protected_paths,
            build_host=config["JHW_BUILD_HOST"],
        )
    except KeyboardInterrupt:
        return _error_result(LauncherError("COMMAND_ABORTED"))
    except LauncherError as error:
        return _error_result(error)
    except Exception:
        return _error_result(LauncherError("UNEXPECTED", returncode=1))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run_program(sys.argv[1:] if argv is None else argv)
    except KeyboardInterrupt:
        result = _error_result(LauncherError("COMMAND_ABORTED"))
    except LauncherError as error:
        result = _error_result(error)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
