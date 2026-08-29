from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Literal


def _require_exact_keys(data: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(data, dict) or set(data) != keys:
        raise ValueError("invalid persisted record keys")
    return data


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    return value


def _float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{field} must be a finite number")
    return converted


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return tuple(_string(item, field) for item in value)


@dataclass(frozen=True)
class ProcessIdentity:
    boot_id: str
    pid: int
    ppid: int
    pgid: int
    start_ticks: int
    exe_dev: int
    exe_ino: int
    exe_name: str

    def stable_key(self) -> str:
        canonical = json.dumps(
            {
                "boot_id": self.boot_id,
                "exe_dev": self.exe_dev,
                "exe_ino": self.exe_ino,
                "pid": self.pid,
                "start_ticks": self.start_ticks,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "boot_id": self.boot_id,
            "pid": self.pid,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "start_ticks": self.start_ticks,
            "exe_dev": self.exe_dev,
            "exe_ino": self.exe_ino,
            "exe_name": self.exe_name,
        }
        self.from_dict(data)
        return data

    @classmethod
    def from_dict(cls, data: object) -> ProcessIdentity:
        parsed = _require_exact_keys(
            data,
            {
                "boot_id",
                "pid",
                "ppid",
                "pgid",
                "start_ticks",
                "exe_dev",
                "exe_ino",
                "exe_name",
            },
        )
        return cls(
            boot_id=_string(parsed["boot_id"], "boot_id"),
            pid=_integer(parsed["pid"], "pid"),
            ppid=_integer(parsed["ppid"], "ppid"),
            pgid=_integer(parsed["pgid"], "pgid"),
            start_ticks=_integer(parsed["start_ticks"], "start_ticks"),
            exe_dev=_integer(parsed["exe_dev"], "exe_dev"),
            exe_ino=_integer(parsed["exe_ino"], "exe_ino"),
            exe_name=_string(parsed["exe_name"], "exe_name"),
        )


@dataclass(frozen=True)
class ObservedTime:
    wall_iso: str
    boot_id: str
    boottime: float

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "wall_iso": self.wall_iso,
            "boot_id": self.boot_id,
            "boottime": self.boottime,
        }
        self.from_dict(data)
        return data

    @classmethod
    def from_dict(cls, data: object) -> ObservedTime:
        parsed = _require_exact_keys(data, {"wall_iso", "boot_id", "boottime"})
        return cls(
            wall_iso=_string(parsed["wall_iso"], "wall_iso"),
            boot_id=_string(parsed["boot_id"], "boot_id"),
            boottime=_float(parsed["boottime"], "boottime"),
        )


@dataclass(frozen=True)
class SessionLease:
    schema_version: int
    session_id: str
    cwd: str
    source: str
    host_keys: tuple[str, ...]
    state: Literal["active", "ended"]
    observed: ObservedTime
    ended: ObservedTime | None = None

    def to_dict(self) -> dict[str, object]:
        if not isinstance(self.observed, ObservedTime):
            raise ValueError("observed must be an ObservedTime")
        if self.ended is not None and not isinstance(self.ended, ObservedTime):
            raise ValueError("ended must be an ObservedTime or None")
        if type(self.host_keys) is not tuple:
            raise ValueError("host_keys must be a tuple")
        data: dict[str, object] = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "source": self.source,
            "host_keys": list(self.host_keys),
            "state": self.state,
            "observed": self.observed.to_dict(),
            "ended": None if self.ended is None else self.ended.to_dict(),
        }
        self.from_dict(data)
        return data

    @classmethod
    def from_dict(cls, data: object) -> SessionLease:
        parsed = _require_exact_keys(
            data,
            {
                "schema_version",
                "session_id",
                "cwd",
                "source",
                "host_keys",
                "state",
                "observed",
                "ended",
            },
        )
        schema_version = _integer(parsed["schema_version"], "schema_version")
        if schema_version != 1:
            raise ValueError("unsupported schema_version")
        state = _string(parsed["state"], "state")
        if state not in ("active", "ended"):
            raise ValueError("invalid session state")
        ended_data = parsed["ended"]
        if ended_data is not None and not isinstance(ended_data, dict):
            raise ValueError("ended must be a record or null")
        return cls(
            schema_version=schema_version,
            session_id=_string(parsed["session_id"], "session_id"),
            cwd=_string(parsed["cwd"], "cwd"),
            source=_string(parsed["source"], "source"),
            host_keys=_strings(parsed["host_keys"], "host_keys"),
            state=state,
            observed=ObservedTime.from_dict(parsed["observed"]),
            ended=None if ended_data is None else ObservedTime.from_dict(ended_data),
        )


@dataclass(frozen=True)
class ManagedProcess:
    schema_version: int
    record_id: str
    scope: str
    server: str
    cwd: str
    wrapper: ProcessIdentity
    child: ProcessIdentity | None
    members: tuple[ProcessIdentity, ...]
    pgid: int
    host_keys: frozenset[str]
    spawned: ObservedTime
    owner_session_id: str | None = None
    shared_owner: str | None = None
    first_owner_gone_boot: float | None = None
    term_sent_boot: float | None = None
    exit_code: int | None = None

    def to_dict(self) -> dict[str, object]:
        if not isinstance(self.wrapper, ProcessIdentity):
            raise ValueError("wrapper must be a ProcessIdentity")
        if self.child is not None and not isinstance(self.child, ProcessIdentity):
            raise ValueError("child must be a ProcessIdentity or None")
        if not isinstance(self.spawned, ObservedTime):
            raise ValueError("spawned must be an ObservedTime")
        if type(self.members) is not tuple or not all(
            isinstance(member, ProcessIdentity) for member in self.members
        ):
            raise ValueError("members must be a tuple of ProcessIdentity values")
        if type(self.host_keys) is not frozenset or not all(
            isinstance(key, str) for key in self.host_keys
        ):
            raise ValueError("host_keys must be a frozenset of strings")
        data: dict[str, object] = {
            "schema_version": self.schema_version,
            "record_id": self.record_id,
            "scope": self.scope,
            "server": self.server,
            "cwd": self.cwd,
            "wrapper": self.wrapper.to_dict(),
            "child": None if self.child is None else self.child.to_dict(),
            "members": [member.to_dict() for member in self.members],
            "pgid": self.pgid,
            "host_keys": sorted(self.host_keys),
            "spawned": self.spawned.to_dict(),
            "owner_session_id": self.owner_session_id,
            "shared_owner": self.shared_owner,
            "first_owner_gone_boot": self.first_owner_gone_boot,
            "term_sent_boot": self.term_sent_boot,
            "exit_code": self.exit_code,
        }
        self.from_dict(data)
        return data

    @classmethod
    def from_dict(cls, data: object) -> ManagedProcess:
        parsed = _require_exact_keys(
            data,
            {
                "schema_version",
                "record_id",
                "scope",
                "server",
                "cwd",
                "wrapper",
                "child",
                "members",
                "pgid",
                "host_keys",
                "spawned",
                "owner_session_id",
                "shared_owner",
                "first_owner_gone_boot",
                "term_sent_boot",
                "exit_code",
            },
        )
        schema_version = _integer(parsed["schema_version"], "schema_version")
        if schema_version != 1:
            raise ValueError("unsupported schema_version")
        child_data = parsed["child"]
        if child_data is not None and not isinstance(child_data, dict):
            raise ValueError("child must be a record or null")
        members_data = parsed["members"]
        if not isinstance(members_data, list):
            raise ValueError("members must be a list")
        owner_session_id = parsed["owner_session_id"]
        shared_owner = parsed["shared_owner"]
        if owner_session_id is not None:
            owner_session_id = _string(owner_session_id, "owner_session_id")
        if shared_owner is not None:
            shared_owner = _string(shared_owner, "shared_owner")
        first_owner_gone_boot = parsed["first_owner_gone_boot"]
        term_sent_boot = parsed["term_sent_boot"]
        exit_code = parsed["exit_code"]
        return cls(
            schema_version=schema_version,
            record_id=_string(parsed["record_id"], "record_id"),
            scope=_string(parsed["scope"], "scope"),
            server=_string(parsed["server"], "server"),
            cwd=_string(parsed["cwd"], "cwd"),
            wrapper=ProcessIdentity.from_dict(parsed["wrapper"]),
            child=None if child_data is None else ProcessIdentity.from_dict(child_data),
            members=tuple(ProcessIdentity.from_dict(member) for member in members_data),
            pgid=_integer(parsed["pgid"], "pgid"),
            host_keys=frozenset(_strings(parsed["host_keys"], "host_keys")),
            spawned=ObservedTime.from_dict(parsed["spawned"]),
            owner_session_id=owner_session_id,
            shared_owner=shared_owner,
            first_owner_gone_boot=(
                None
                if first_owner_gone_boot is None
                else _float(first_owner_gone_boot, "first_owner_gone_boot")
            ),
            term_sent_boot=(
                None if term_sent_boot is None else _float(term_sent_boot, "term_sent_boot")
            ),
            exit_code=None if exit_code is None else _integer(exit_code, "exit_code"),
        )
