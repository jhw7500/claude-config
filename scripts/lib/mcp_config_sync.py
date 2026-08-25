#!/usr/bin/env python3
"""Plan and atomically synchronize Claude MCP user configuration."""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import json
import os
import re
import secrets
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


PENDING_EXIT = 2
DIFF_FIELDS = ("type", "command", "args", "env")
PLACEHOLDER = re.compile(
    r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<default>:-[^}]*)?\}$"
)
CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|TOKEN|PASSWORD|PASSWD|"
    r"SECRET|CREDENTIAL|AUTH|AUTHORIZATION|BEARER)(?:_|$)|"
    r"(?:PASSWORD|PASSWD)(?:_|$)",
    re.IGNORECASE,
)
CREDENTIAL_FLAG = re.compile(
    r"^--?(?:[a-z0-9]+[-_])*(?:api[-_]?key|access[-_]?key|"
    r"private[-_]?key|client[-_]?secret|token|password|passwd|secret|"
    r"credential|auth|authorization|bearer|headers?|env|environment|"
    r"env[-_]?(?:var|variable))$",
    re.IGNORECASE,
)
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?:^|[?&=,;\s])(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?=\s*[:=])"
)
CREDENTIAL_LABEL = re.compile(
    r"(?:^|[?&=,;{\s])[\"']?(?:x[-_])?(?:api[-_]?key|access[-_]?key|"
    r"private[-_]?key|client[-_]?secret|token|password|passwd|secret|"
    r"credential|auth|authorization|bearer)[\"']?\s*[:=]",
    re.IGNORECASE,
)
SHORT_ENV_CARRIER = re.compile(r"^-e(?:$|=|[A-Za-z_][A-Za-z0-9_]*=)")
PLACEHOLDER_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


class BlockedPlan(ValueError):
    """A validation failure whose value-free diagnostic is safe to display."""


@dataclass(frozen=True)
class ConfigSnapshot:
    raw: bytes | None
    data: dict[str, Any]


def _contains_credential(token: str, credential_values: list[str]) -> bool:
    return any(
        token == secret or (len(secret) >= 8 and secret in token)
        for secret in credential_values
    )


def _is_credential_carrier(token: str) -> bool:
    flag = token.partition("=")[0]
    if (
        token.startswith("-H")
        or SHORT_ENV_CARRIER.match(token)
        or CREDENTIAL_FLAG.fullmatch(flag)
        or CREDENTIAL_LABEL.search(token)
    ):
        return True
    return any(
        CREDENTIAL_NAME.search(match.group("name"))
        for match in CREDENTIAL_ASSIGNMENT.finditer(token)
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


def _read_object(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    if missing_ok and not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value is not an object")
    return data


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


def _shadow_scopes(config: dict[str, Any], project_dir: Path, name: str) -> list[str]:
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

    project_config = _read_object(project_dir / ".mcp.json", missing_ok=True)
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
        if value and CREDENTIAL_NAME.search(key)
    ]
    for key, value in env.items():
        placeholder = PLACEHOLDER.fullmatch(value) if isinstance(value, str) else None
        if not isinstance(key, str) or placeholder is None:
            safe_key = key if isinstance(key, str) and key else "<invalid>"
            raise BlockedPlan(f"user/{name}: env.{safe_key} must use a placeholder")
        variable = placeholder.group("name")
        credential_bearing = bool(
            CREDENTIAL_NAME.search(key) or CREDENTIAL_NAME.search(variable)
        )
        if placeholder.group("default") and credential_bearing:
            raise BlockedPlan(
                f"user/{name}: env.{key} credential placeholder may not have a default"
            )
        inherited_value = os.environ.get(variable)
        if credential_bearing and inherited_value:
            credential_values.append(inherited_value)

    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise BlockedPlan(f"user/{name}: command must be a non-empty string")
    if any(
        CREDENTIAL_NAME.search(variable)
        for variable in PLACEHOLDER_REFERENCE.findall(command)
    ):
        raise BlockedPlan(f"user/{name}: command may not reference credential variables")
    if _is_credential_carrier(command):
        raise BlockedPlan(f"user/{name}: command may not contain credential carriers")
    if _contains_credential(command, credential_values):
        raise BlockedPlan(f"user/{name}: command matches a credential environment value")

    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
        raise BlockedPlan(f"user/{name}: args must be a string array")
    if any(_is_credential_carrier(value) for value in args):
        raise BlockedPlan(f"user/{name}: args may not contain credential flags")
    if any(
        CREDENTIAL_NAME.search(variable)
        for value in args
        for variable in PLACEHOLDER_REFERENCE.findall(value)
    ):
        raise BlockedPlan(f"user/{name}: args may not reference credential variables")
    if any(_contains_credential(value, credential_values) for value in args):
        raise BlockedPlan(f"user/{name}: args match a credential environment value")


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

        shadows = _shadow_scopes(snapshot.data, project_dir, name)
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
