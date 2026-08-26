"""Secure-store-only host launcher contract tests for issue #28."""

from __future__ import annotations

import importlib.util
import base64
import io
import json
import os
import pty
import re
import select
import shutil
import socket
import subprocess
import sys
import termios
import threading
import time
from urllib.parse import quote_from_bytes
from pathlib import Path
from types import ModuleType

import pytest


REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "jhw-control-host.py"
CONFIG_VALUES = {
    "JHW_REGISTRY_DIR": "/srv/jhw/project-registry",
    "JHW_REGISTRY_REMOTE": "origin",
    "JHW_REGISTRY_BRANCH": "master",
    "JHW_WORKTREE_ROOT": "/srv/jhw/worktrees",
    "JHW_CONTROL_STATE_DIR": "/srv/jhw/state",
    "JHW_BUILD_HOST": "build-1",
    "JHW_GITHUB_OWNER": "jhw7500",
    "JHW_PROJECT_NUMBER": "7",
    "JHW_REGISTRY_REPOSITORY": "jhw7500/project-registry",
    "JHW_PREFLIGHT_PROJECT_ITEM_ID": "PVTI_fixture",
    "JHW_PREFLIGHT_REGISTRY_ISSUE_NUMBER": "1",
}


def load_launcher() -> ModuleType:
    assert SCRIPT.is_file(), "launcher implementation is missing"
    spec = importlib.util.spec_from_file_location("jhw_control_host", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def launcher() -> ModuleType:
    return load_launcher()


@pytest.fixture
def unlock_helper_namespace(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    class FakeVariant:
        def __init__(self, signature: str, value) -> None:
            self.signature = signature
            self.value = value

        def unpack(self):
            if self.signature == "(oayays)":
                session, parameters, value, content_type = self.value
                return session, list(parameters), list(value), content_type

            def unpack_value(value):
                return value.unpack() if isinstance(value, FakeVariant) else value

            if isinstance(self.value, tuple):
                return tuple(unpack_value(value) for value in self.value)
            return self.value

    class FakeVariantType:
        @staticmethod
        def new(signature: str) -> str:
            return signature

    class FakeDBusCallFlags:
        NO_AUTO_START = 1

    class FakeDBusConnectionFlags:
        AUTHENTICATION_CLIENT = 1
        MESSAGE_BUS_CONNECTION = 2

    class FakeGio:
        DBusCallFlags = FakeDBusCallFlags
        DBusConnectionFlags = FakeDBusConnectionFlags

    class FakeGLib:
        Variant = FakeVariant
        VariantType = FakeVariantType

    gi = ModuleType("gi")
    repository = ModuleType("gi.repository")
    repository.Gio = FakeGio
    repository.GLib = FakeGLib
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)

    namespace: dict[str, object] = {"__name__": "jhw_control_unlock_test"}
    exec(launcher.UNLOCK_HELPER, namespace)
    return namespace


def write_config(home: Path, values: dict[str, str] | None = None) -> Path:
    path = home / ".config" / "jhw-control" / "control.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    selected = CONFIG_VALUES if values is None else values
    path.write_text(
        "# non-secret host coordinates\n"
        + "".join(f"export {key}={value}\n" for key, value in selected.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_launcher_exists() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_bytes().splitlines()[0] == b"#!/usr/bin/python3 -I"


def test_executable_startup_ignores_path_and_python_poison(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    path_marker = tmp_path / "path-poison-ran"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        f"#!/bin/sh\n: > '{path_marker}'\nexit 91\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    poison_modules = tmp_path / "poison-modules"
    poison_modules.mkdir()
    module_marker = tmp_path / "module-poison-ran"
    (poison_modules / "json.py").write_text(
        f"from pathlib import Path\nPath({str(module_marker)!r}).touch()\n",
        encoding="utf-8",
    )

    path_result = subprocess.run(
        ["/usr/bin/env", "-i", f"PATH={fake_bin}", str(SCRIPT), "--contract"],
        text=True,
        capture_output=True,
        check=False,
    )
    module_result = subprocess.run(
        [
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            f"PYTHONPATH={poison_modules}",
            "PYTHONHOME=/definitely-not-a-python-home",
            str(SCRIPT),
            "--contract",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert path_result.returncode == 0, path_result.stderr
    assert module_result.returncode == 0, module_result.stderr
    assert json.loads(path_result.stdout)["version"] == 2
    assert json.loads(module_result.stdout)["version"] == 2
    assert not path_marker.exists()
    assert not module_marker.exists()


def test_contract_needs_no_config_or_provider_and_is_path_free(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    def unexpected_runner(*_args, **_kwargs):
        raise AssertionError("contract must not start a provider or child")

    result = launcher.run_program(
        ["--contract"],
        home=tmp_path / "missing-home",
        environment={"GH_TOKEN": "ambient-must-not-appear"},
        uid=os.getuid(),
        command_runner=unexpected_runner,
    )

    assert result.returncode == 0
    assert result.stderr == b""
    payload = json.loads(result.stdout)
    assert payload == {
        "commands": ["unlock", "preflight", "portfolio status", "task start"],
        "credential_policy": "secure-store-only",
        "name": "jhw-control-host",
        "version": 2,
    }
    assert str(tmp_path).encode() not in result.stdout
    assert b"ambient-must-not-appear" not in result.stdout


def test_reads_exact_literal_control_config(launcher: ModuleType, tmp_path: Path) -> None:
    path = write_config(tmp_path)

    assert launcher.read_control_config(path, uid=os.getuid()) == CONFIG_VALUES


@pytest.mark.parametrize("unsafe", ["missing", "mode", "symlink", "hardlink", "owner"])
def test_rejects_unsafe_control_config(
    launcher: ModuleType,
    tmp_path: Path,
    unsafe: str,
) -> None:
    path = tmp_path / ".config" / "jhw-control" / "control.env"
    expected_uid = os.getuid()
    if unsafe != "missing":
        real = write_config(tmp_path)
        if unsafe == "mode":
            real.chmod(0o640)
        elif unsafe == "symlink":
            target = tmp_path / "real-control.env"
            real.replace(target)
            real.symlink_to(target)
        elif unsafe == "hardlink":
            os.link(real, tmp_path / "control-copy.env")
        elif unsafe == "owner":
            expected_uid += 1

    with pytest.raises(launcher.LauncherError) as caught:
        launcher.read_control_config(path, uid=expected_uid)

    assert caught.value.code == "CONFIG_UNSAFE"
    assert str(path) not in str(caught.value)


def test_config_read_is_pinned_to_open_descriptor(
    launcher: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_config(tmp_path)
    original = path.read_bytes()
    replacement_values = dict(CONFIG_VALUES, JHW_BUILD_HOST="replacement")
    replacement = tmp_path / "replacement.env"
    replacement.write_text(
        "".join(f"export {key}={value}\n" for key, value in replacement_values.items()),
        encoding="utf-8",
    )
    replacement.chmod(0o600)
    real_read = launcher.os.read
    replaced = False

    def replace_then_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced:
            os.replace(replacement, path)
            replaced = True
        return real_read(descriptor, size)

    monkeypatch.setattr(launcher.os, "read", replace_then_read)

    assert launcher.read_control_config(path, uid=os.getuid()) == launcher.parse_control_config(original)


@pytest.mark.parametrize(
    "payload",
    [
        b"export JHW_REGISTRY_DIR=$(touch /tmp/marker)\n",
        b"export JHW_REGISTRY_DIR=${HOME}/registry\n",
        b"export JHW_REGISTRY_DIR='/srv/registry'\n",
        b"export GH_PROJECT_TOKEN=plaintext\n",
        b"export UNKNOWN_SETTING=value\n",
        b"export JHW_REGISTRY_DIR=/one\nexport JHW_REGISTRY_DIR=/two\n",
        b"export JHW_REGISTRY_DIR=/srv/registry\x00suffix\n",
        b"export JHW_REGISTRY_DIR=\xff\n",
    ],
)
def test_rejects_nonliteral_unknown_duplicate_or_secret_config(
    launcher: ModuleType,
    payload: bytes,
) -> None:
    with pytest.raises(launcher.LauncherError) as caught:
        launcher.parse_control_config(payload)

    assert caught.value.code == "CONFIG_INVALID"


def test_rejects_incomplete_or_oversized_config(launcher: ModuleType) -> None:
    with pytest.raises(launcher.LauncherError) as incomplete:
        launcher.parse_control_config(b"export JHW_REGISTRY_DIR=/srv/registry\n")
    with pytest.raises(launcher.LauncherError) as oversized:
        launcher.parse_control_config(b"#" * (launcher.MAX_CONFIG_BYTES + 1))

    assert incomplete.value.code == "CONFIG_INVALID"
    assert oversized.value.code == "CONFIG_UNSAFE"


PROJECT_TOKEN = "project-token-1234567890"
REPOSITORY_TOKEN = "repository-token-1234567890"
NOTION_TOKEN = "notion-token-1234567890"
TASK_ID = "tsk-0198aabb-ccdd-7eef-8abc-0123456789ab"
CLAIM_ID = "clm-0198aabb-ccdd-7eef-8abc-0123456789ab"
WORKTREE_REF = "wt-0123456789ab-issue-28"
TASK_BRANCH = "task/0123456789ab-issue-28"
OTHER_TASK_ID = "tsk-0198aabb-ccdd-7eef-8abc-fedcba987654"
OTHER_WORKTREE_REF = "wt-fedcba987654-issue-29"
OTHER_TASK_BRANCH = "task/fedcba987654-issue-29"
UNLOCK_PRIVATE_XML = """\
<node>
  <interface name="org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface">
    <method name="UnlockWithMasterPassword">
      <arg name="collection" type="o" direction="in"/>
      <arg name="secret" type="(oayays)" direction="in"/>
    </method>
  </interface>
</node>
"""
PREFLIGHT_OUTPUT = (
    b'{"command":"preflight","result":{"status":"ready","checks":'
    b'{"credentials":"ok","authority":"ok","notion_guard":"ok","project":"ok",'
    b'"registry_repository":"ok","registry_issue":"ok","registry_git":"ok"}}}\n'
)
PORTFOLIO_OUTPUT = (
    b'{"command":"portfolio status","result":{"page_id":"page-1","markdown":"ready",'
    b'"items":[],"repositories":[],"truncated":false,"total_items":0}}\n'
)
PORTFOLIO_PROJECTED = (
    b'{"command":"portfolio status","result":{"page_id":"page-1","items":[],'
    b'"repositories":[],"truncated":false,"total_items":0}}\n'
)


def portfolio_contract_payload(
    *,
    project_item_id: str = "PVTI_fixture",
    source_node_id: str = "I_fixture",
    slug: str | None = None,
) -> dict[str, object]:
    repositories = [] if slug is None else [
        {"repo_id": "repo-claude-config", "slug": slug, "allow_public": False}
    ]
    return {
        "command": "portfolio status",
        "result": {
            "page_id": "page-1",
            "markdown": "ready",
            "items": [
                {
                    "project_item_id": project_item_id,
                    "source_node_id": source_node_id,
                    "project_id": "prj-claude-config",
                    "title": "Project",
                    "objective": "Objective",
                    "repo_ids": ["repo-claude-config"],
                    "fields": {
                        "status": "active",
                        "priority": "P1",
                        "health": "on-track",
                        "next_action": f"task:{TASK_ID}",
                        "last_reviewed": "2026-08-26",
                    },
                    "stale": False,
                }
            ],
            "repositories": repositories,
            "truncated": False,
            "total_items": 1,
        },
    }


def fake_tools(launcher: ModuleType):
    return launcher.HostTools(
        python="/trusted/python3",
        gh="/trusted/gh",
        node="/trusted/node",
        control="/trusted/jhw-control",
    )


class FakeCommandRunner:
    def __init__(self, launcher: ModuleType) -> None:
        self.launcher = launcher
        self.tools = fake_tools(launcher)
        self.calls: list[dict[str, object]] = []
        self.keyring_result = launcher.CommandResult(
            0,
            json.dumps(
                {
                    "backend": "keyring.backends.SecretService.Keyring",
                    "notion": NOTION_TOKEN,
                    "project": PROJECT_TOKEN,
                }
            ).encode(),
            b"",
        )
        self.unlock_result = launcher.CommandResult(
            0,
            b'{"status":"unlocked"}\n',
            b"",
        )
        self.gh_result = launcher.CommandResult(
            0,
            json.dumps(
                {
                    "hosts": {
                        "github.com": [
                            {
                                "active": True,
                                "host": "github.com",
                                "login": "jhw7500",
                                "state": "success",
                                "token": REPOSITORY_TOKEN,
                                "tokenSource": "keyring",
                            }
                        ]
                    }
                }
            ).encode(),
            b"",
        )
        self.control_results: dict[tuple[str, ...], object] = {
            ("preflight",): launcher.CommandResult(
                0,
                PREFLIGHT_OUTPUT,
                b"",
            ),
            ("portfolio", "status", "--project", "prj-claude-config"): launcher.CommandResult(
                0,
                PORTFOLIO_OUTPUT,
                b"",
            ),
            ("task", "start", "--issue", "https://example.test/issues/28"): launcher.CommandResult(
                0,
                json.dumps(
                    {
                        "command": "task start",
                        "result": {
                            "task": {
                                "task_id": TASK_ID,
                                "kind": "formal",
                                "project_id": "prj-claude-config",
                                "repo_id": "repo-claude-config",
                            },
                            "claim": {
                                "task_id": TASK_ID,
                                "claim_id": CLAIM_ID,
                                "project_id": "prj-claude-config",
                                "repo_id": "repo-claude-config",
                                "host": "build-1",
                                "branch": TASK_BRANCH,
                                "worktree_ref": WORKTREE_REF,
                                "started_at": "2026-08-26T00:00:00.000Z",
                            },
                            "branch": TASK_BRANCH,
                            "worktree_ref": WORKTREE_REF,
                            "reused": False,
                        },
                    },
                    separators=(",", ":"),
                ).encode()
                + b"\n",
                b"",
            ),
        }

    def __call__(
        self,
        argv,
        *,
        env,
        timeout_seconds,
        max_output_bytes,
        tty_input=False,
    ):
        command = tuple(argv)
        self.calls.append(
            {
                "argv": command,
                "env": dict(env),
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
                "tty_input": tty_input,
            }
        )
        if (
            command[0] == self.tools.python
            and len(command) == 4
            and command[1:3] == ("-I", "-c")
            and command[3] == getattr(self.launcher, "UNLOCK_HELPER", None)
        ):
            result = self.unlock_result
        elif command[0] == self.tools.python:
            result = self.keyring_result
        elif command[0] == self.tools.gh:
            result = self.gh_result
        elif command[:2] == (self.tools.node, self.tools.control):
            result = self.control_results[command[2:]]
        else:
            raise AssertionError(f"unexpected command: {command!r}")
        if isinstance(result, BaseException):
            raise result
        return result


def run_secure(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
    runner: FakeCommandRunner,
    environment: dict[str, str] | None = None,
):
    write_config(tmp_path)
    poisoned = {
        "HOME": "/attacker/home",
        "PATH": "/attacker/bin",
        "PYTHONPATH": "/attacker/python",
        "PYTHONHOME": "/attacker/python-home",
        "NODE_OPTIONS": "--require=/attacker/preload.js",
        "NODE_PATH": "/attacker/node",
        "LD_PRELOAD": "/attacker/preload.so",
        "BASH_ENV": "/attacker/bash-env",
        "ENV": "/attacker/sh-env",
        "GIT_ASKPASS": "/attacker/askpass",
        "GH_CONFIG_DIR": "/attacker/gh",
        "GH_HOST": "attacker.test",
        "GH_TOKEN": "ambient-gh-token",
        "GITHUB_TOKEN": "ambient-github-token",
        "GH_PROJECT_TOKEN": "ambient-project-token",
        "GH_REPO_TOKEN": "ambient-repository-token",
        "NOTION_API_KEY": "ambient-notion-token",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "LANG": "C.UTF-8",
    }
    if environment:
        poisoned.update(environment)
    return launcher.run_program(
        argv,
        home=tmp_path,
        environment=poisoned,
        uid=os.getuid(),
        command_runner=runner,
        tools=runner.tools,
    )


def test_unlock_is_early_isolated_and_auto_discovers_the_user_bus(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    poisoned = {
        "GH_TOKEN": "ambient-gh-token",
        "GH_PROJECT_TOKEN": "ambient-project-token",
        "GH_REPO_TOKEN": "ambient-repository-token",
        "NOTION_API_KEY": "ambient-notion-token",
        "PYTHONPATH": "/attacker/python",
        "LANG": "C.UTF-8",
    }

    result = launcher.run_program(
        ["unlock"],
        home=tmp_path / "missing-home",
        environment=poisoned,
        uid=os.getuid(),
        command_runner=runner,
        tools=runner.tools,
    )

    assert result == launcher.ProgramResult(
        0,
        b'{"command":"unlock","result":{"status":"unlocked"}}\n',
        b"",
    )
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["argv"] == (runner.tools.python, "-I", "-c", launcher.UNLOCK_HELPER)
    assert call["tty_input"] is True
    assert call["env"]["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
    assert call["env"]["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path=/run/user/{os.getuid()}/bus"
    serialized = repr(call).encode() + result.stdout + result.stderr
    for secret in poisoned.values():
        if "token" in secret:
            assert secret.encode() not in serialized
    assert not (tmp_path / "missing-home" / ".config").exists()


def test_clean_preflight_auto_discovers_the_same_user_bus_for_both_providers(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    write_config(tmp_path)

    result = launcher.run_program(
        ["preflight"],
        home=tmp_path,
        environment={"LANG": "C.UTF-8"},
        uid=os.getuid(),
        command_runner=runner,
        tools=runner.tools,
    )

    assert result.returncode == 0
    for call in runner.calls[:2]:
        assert call["env"]["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
        assert call["env"]["DBUS_SESSION_BUS_ADDRESS"] == (
            f"unix:path=/run/user/{os.getuid()}/bus"
        )


def test_unlock_helper_skips_password_when_login_collection_is_already_unlocked(
    launcher: ModuleType,
    unlock_helper_namespace: dict[str, object],
) -> None:
    namespace = unlock_helper_namespace
    unlock = namespace.get("unlock_credential_store")
    assert callable(unlock)
    GLib = namespace["GLib"]

    class AlreadyUnlockedConnection:
        def __init__(self) -> None:
            self.methods: list[str] = []

        def call_sync(
            self,
            _destination,
            _path,
            _interface,
            method,
            _parameters,
            _reply_type,
            _flags,
            _timeout,
            _cancellable,
        ):
            self.methods.append(method)
            replies = {
                "GetNameOwner": GLib.Variant("(s)", (":1.44",)),
                "Introspect": GLib.Variant("(s)", (UNLOCK_PRIVATE_XML,)),
                "ReadAlias": GLib.Variant(
                    "(o)",
                    ("/org/freedesktop/secrets/collection/login",),
                ),
                "Get": GLib.Variant("(v)", (GLib.Variant("b", False),)),
            }
            return replies[method]

    connection = AlreadyUnlockedConnection()

    status = unlock(connection, lambda: pytest.fail("password must not be requested"))

    assert status == "already-unlocked"
    assert connection.methods == ["GetNameOwner", "Introspect", "ReadAlias", "Get"]


def test_unlock_helper_uses_the_existing_daemon_private_method_and_wipes_password(
    launcher: ModuleType,
    unlock_helper_namespace: dict[str, object],
) -> None:
    namespace = unlock_helper_namespace
    unlock = namespace["unlock_credential_store"]
    GLib = namespace["GLib"]

    class LockedConnection:
        def __init__(self) -> None:
            self.methods: list[str] = []
            self.locked = True
            self.secret_seen = b""

        def call_sync(
            self,
            destination,
            _path,
            interface,
            method,
            parameters,
            _reply_type,
            flags,
            _timeout,
            _cancellable,
        ):
            self.methods.append(method)
            assert flags == namespace["NO_AUTO_START"]
            if method == "GetNameOwner":
                return GLib.Variant("(s)", (":1.44",))
            assert destination == ":1.44"
            if method == "Introspect":
                return GLib.Variant("(s)", (UNLOCK_PRIVATE_XML,))
            if method == "ReadAlias":
                return GLib.Variant(
                    "(o)",
                    ("/org/freedesktop/secrets/collection/login",),
                )
            if method == "Get":
                return GLib.Variant("(v)", (GLib.Variant("b", self.locked),))
            if method == "OpenSession":
                return GLib.Variant(
                    "(vo)",
                    (
                        GLib.Variant("s", ""),
                        "/org/freedesktop/secrets/session/s1",
                    ),
                )
            if method == "UnlockWithMasterPassword":
                assert interface == namespace["PRIVATE_INTERFACE"]
                collection, secret = parameters.unpack()
                assert collection == "/org/freedesktop/secrets/collection/login"
                session, parameter, value, content_type = secret
                assert session == "/org/freedesktop/secrets/session/s1"
                assert parameter == []
                assert content_type == "text/plain"
                self.secret_seen = bytes(value)
                self.locked = False
                return GLib.Variant("()", ())
            if method == "Close":
                assert interface == "org.freedesktop.Secret.Session"
                return GLib.Variant("()", ())
            raise AssertionError(f"unexpected method: {method}")

    connection = LockedConnection()
    password = bytearray(b"dummy-login-keyring-password")

    status = unlock(connection, lambda: password)

    assert status == "unlocked"
    assert connection.methods == [
        "GetNameOwner",
        "Introspect",
        "ReadAlias",
        "Get",
        "OpenSession",
        "UnlockWithMasterPassword",
        "Get",
        "Close",
    ]
    assert connection.secret_seen == b"dummy-login-keyring-password"
    assert password == bytearray(len(password))
    assert not ({"Unlock", "CreateCollection", "ChangeLock"} & set(connection.methods))


def test_unlock_helper_fails_closed_when_collection_remains_locked_without_retry(
    launcher: ModuleType,
    unlock_helper_namespace: dict[str, object],
) -> None:
    namespace = unlock_helper_namespace
    unlock = namespace["unlock_credential_store"]
    GLib = namespace["GLib"]

    class StillLockedConnection:
        def __init__(self) -> None:
            self.methods: list[str] = []

        def call_sync(
            self,
            _destination,
            _path,
            _interface,
            method,
            _parameters,
            _reply_type,
            _flags,
            _timeout,
            _cancellable,
        ):
            self.methods.append(method)
            replies = {
                "GetNameOwner": GLib.Variant("(s)", (":1.44",)),
                "Introspect": GLib.Variant("(s)", (UNLOCK_PRIVATE_XML,)),
                "ReadAlias": GLib.Variant(
                    "(o)",
                    ("/org/freedesktop/secrets/collection/login",),
                ),
                "Get": GLib.Variant("(v)", (GLib.Variant("b", True),)),
                "OpenSession": GLib.Variant(
                    "(vo)",
                    (GLib.Variant("s", ""), "/org/freedesktop/secrets/session/s1"),
                ),
                "UnlockWithMasterPassword": GLib.Variant("()", ()),
                "Close": GLib.Variant("()", ()),
            }
            return replies[method]

    connection = StillLockedConnection()
    password = bytearray(b"wrong-password")

    with pytest.raises(namespace["UnlockFailure"]) as caught:
        unlock(connection, lambda: password)

    assert caught.value.returncode == 24
    assert connection.methods.count("UnlockWithMasterPassword") == 1
    assert connection.methods[-2:] == ["Get", "Close"]
    assert password == bytearray(len(password))


def test_unlock_helper_reads_one_hidden_tty_line_and_restores_echo(
    launcher: ModuleType,
    unlock_helper_namespace: dict[str, object],
) -> None:
    namespace = unlock_helper_namespace
    read_password = namespace.get("read_password")
    assert callable(read_password)

    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    observed = bytearray()
    expected = b"dummy-login-keyring-password"

    def enter_password() -> None:
        ready, _, _ = select.select([master], [], [], 2)
        assert ready
        observed.extend(os.read(master, 4096))
        os.write(master, expected + b"\n")

    worker = threading.Thread(target=enter_password)
    worker.start()
    restored = None
    try:
        password = read_password(slave)
        restored = termios.tcgetattr(slave)
        worker.join(timeout=2)
        assert not worker.is_alive()
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            observed.extend(os.read(master, 4096))
    finally:
        os.close(master)
        os.close(slave)

    assert password == bytearray(expected)
    assert expected not in observed
    assert b"Login keyring" in observed
    assert restored == original


def test_unlock_helper_discards_queued_input_after_overlength_password(
    launcher: ModuleType,
    unlock_helper_namespace: dict[str, object],
) -> None:
    namespace = unlock_helper_namespace
    read_password = namespace.get("read_password")
    assert callable(read_password)

    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    overlength = b"x" * 1025 + b"\n"

    def enter_password() -> None:
        ready, _, _ = select.select([master], [], [], 2)
        assert ready
        os.read(master, 4096)
        os.write(master, overlength)

    worker = threading.Thread(target=enter_password)
    worker.start()
    queued = None
    restored = None
    try:
        with pytest.raises(namespace["UnlockFailure"]) as caught:
            read_password(slave)
        assert caught.value.returncode == 21
        worker.join(timeout=2)
        assert not worker.is_alive()
        restored = termios.tcgetattr(slave)
        os.set_blocking(slave, False)
        try:
            queued = os.read(slave, 4096)
        except BlockingIOError:
            queued = b""
    finally:
        termios.tcsetattr(slave, termios.TCSANOW, original)
        os.close(master)
        os.close(slave)

    assert restored == original
    assert queued == b""


def test_unlock_helper_rejects_missing_private_contract_before_reading_password(
    launcher: ModuleType,
    unlock_helper_namespace: dict[str, object],
) -> None:
    namespace = unlock_helper_namespace
    unlock = namespace["unlock_credential_store"]
    GLib = namespace["GLib"]

    class UnsupportedConnection:
        def __init__(self) -> None:
            self.methods: list[str] = []

        def call_sync(
            self,
            _destination,
            _path,
            _interface,
            method,
            _parameters,
            _reply_type,
            _flags,
            _timeout,
            _cancellable,
        ):
            self.methods.append(method)
            replies = {
                "GetNameOwner": GLib.Variant("(s)", (":1.44",)),
                "Introspect": GLib.Variant("(s)", ("<node/>",)),
                "ReadAlias": GLib.Variant(
                    "(o)",
                    ("/org/freedesktop/secrets/collection/login",),
                ),
                "Get": GLib.Variant("(v)", (GLib.Variant("b", True),)),
            }
            return replies[method]

    connection = UnsupportedConnection()
    with pytest.raises(namespace["UnlockFailure"]) as caught:
        unlock(connection, lambda: pytest.fail("unsupported API must not request a password"))

    assert caught.value.returncode == 23
    assert connection.methods == ["GetNameOwner", "Introspect"]


def test_unlock_helper_main_emits_only_fixed_json_and_closes_its_connection(
    launcher: ModuleType,
    unlock_helper_namespace: dict[str, object],
) -> None:
    namespace = unlock_helper_namespace
    GLib = namespace["GLib"]

    class Connection:
        def __init__(self) -> None:
            self.closed = False

        def call_sync(
            self,
            _destination,
            _path,
            _interface,
            method,
            _parameters,
            _reply_type,
            _flags,
            _timeout,
            _cancellable,
        ):
            replies = {
                "GetNameOwner": GLib.Variant("(s)", (":1.44",)),
                "Introspect": GLib.Variant("(s)", (UNLOCK_PRIVATE_XML,)),
                "ReadAlias": GLib.Variant(
                    "(o)",
                    ("/org/freedesktop/secrets/collection/login",),
                ),
                "Get": GLib.Variant("(v)", (GLib.Variant("b", False),)),
            }
            return replies[method]

        def close_sync(self, _cancellable) -> None:
            self.closed = True

    connection = Connection()
    output = io.StringIO()

    returncode = namespace["main"](
        connection_factory=lambda: connection,
        password_reader=lambda: pytest.fail("already unlocked must not read password"),
        output=output,
    )

    assert returncode == 0
    assert output.getvalue() == '{"status":"already-unlocked"}\n'
    assert connection.closed is True


@pytest.mark.parametrize(
    ("helper_returncode", "expected_code", "expected_action"),
    [
        (20, "KEYRING_RUNTIME_UNAVAILABLE", "install keyring and SecretStorage for /usr/bin/python3"),
        (21, "INTERACTIVE_TTY_REQUIRED", "run jhw-control-host unlock from an interactive terminal"),
        (22, "OS_CREDENTIAL_STORE_UNAVAILABLE", None),
        (23, "OS_CREDENTIAL_STORE_UNLOCK_UNSUPPORTED", None),
        (24, "OS_CREDENTIAL_STORE_UNLOCK_FAILED", None),
        (25, "COMMAND_ABORTED", None),
        (26, "OS_CREDENTIAL_STORE_UNLOCK_FAILED", None),
    ],
)
def test_unlock_maps_helper_failures_without_exposing_helper_output(
    launcher: ModuleType,
    tmp_path: Path,
    helper_returncode: int,
    expected_code: str,
    expected_action: str | None,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.unlock_result = launcher.CommandResult(
        helper_returncode,
        b"password-canary-from-helper",
        b"private-diagnostic-from-helper",
    )

    result = launcher.run_program(
        ["unlock"],
        home=tmp_path,
        environment={},
        uid=os.getuid(),
        command_runner=runner,
        tools=runner.tools,
    )

    expected_error: dict[str, str] = {"code": expected_code}
    if expected_action is not None:
        expected_error["action"] = expected_action
    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": expected_error}
    assert b"password-canary" not in result.stdout + result.stderr
    assert b"private-diagnostic" not in result.stdout + result.stderr


def test_unlock_maps_parent_keyboard_interrupt_without_a_traceback(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.unlock_result = KeyboardInterrupt()

    try:
        result = launcher.run_program(
            ["unlock"],
            home=tmp_path,
            environment={},
            uid=os.getuid(),
            command_runner=runner,
            tools=runner.tools,
        )
    except KeyboardInterrupt:
        result = None

    assert result == launcher.ProgramResult(
        78,
        b"",
        b'{"error":{"code":"COMMAND_ABORTED"}}\n',
    )


def test_preflight_maps_parent_keyboard_interrupt_without_a_traceback(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.control_results[("preflight",)] = KeyboardInterrupt()

    try:
        result = run_secure(launcher, tmp_path, ["preflight"], runner)
    except KeyboardInterrupt:
        result = None

    assert result == launcher.ProgramResult(
        78,
        b"",
        b'{"error":{"code":"COMMAND_ABORTED"}}\n',
    )


@pytest.mark.parametrize(
    "helper_output",
    [
        b"not-json",
        b'{"status":"unlocked","status":"already-unlocked"}',
        b'{"status":"unlocked","extra":true}',
        b'{"status":"unknown"}',
    ],
)
def test_unlock_replaces_malformed_success_with_one_fixed_error(
    launcher: ModuleType,
    tmp_path: Path,
    helper_output: bytes,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.unlock_result = launcher.CommandResult(0, helper_output, b"")

    result = launcher.run_program(
        ["unlock"],
        home=tmp_path,
        environment={},
        uid=os.getuid(),
        command_runner=runner,
        tools=runner.tools,
    )

    assert result.returncode == 78
    assert json.loads(result.stderr) == {
        "error": {"code": "OS_CREDENTIAL_STORE_UNLOCK_FAILED"}
    }


def test_validated_session_bus_accepts_only_a_private_uid_owned_unix_socket(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    validator = getattr(launcher, "_validated_session_bus", None)
    assert callable(validator)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    bus = runtime / "bus"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(bus))
    try:
        assert validator(runtime, uid=os.getuid()) == bus
    finally:
        server.close()


@pytest.mark.parametrize("unsafe_kind", ["runtime-mode", "runtime-symlink", "bus-file", "bus-symlink"])
def test_validated_session_bus_rejects_unsafe_endpoints(
    launcher: ModuleType,
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    validator = getattr(launcher, "_validated_session_bus", None)
    assert callable(validator)
    real_runtime = tmp_path / "real-runtime"
    real_runtime.mkdir(mode=0o700)
    runtime = real_runtime
    server = None
    if unsafe_kind == "runtime-mode":
        real_runtime.chmod(0o750)
        (real_runtime / "bus").touch()
    elif unsafe_kind == "runtime-symlink":
        runtime = tmp_path / "runtime-link"
        runtime.symlink_to(real_runtime, target_is_directory=True)
        (real_runtime / "bus").touch()
    elif unsafe_kind == "bus-file":
        (real_runtime / "bus").touch()
    else:
        target = real_runtime / "real-bus"
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(target))
        (real_runtime / "bus").symlink_to(target)

    try:
        with pytest.raises(launcher.LauncherError) as caught:
            validator(runtime, uid=os.getuid())
    finally:
        if server is not None:
            server.close()

    assert caught.value.code == "OS_CREDENTIAL_STORE_UNAVAILABLE"


def test_interactive_runner_restores_terminal_state_after_timeout(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    real_open = open

    def controlled_open(path, mode="r", buffering=-1, *args, **kwargs):
        if path == "/dev/tty":
            return os.fdopen(os.dup(slave), mode, buffering=buffering)
        return real_open(path, mode, buffering, *args, **kwargs)

    monkeypatch.setattr("builtins.open", controlled_open)
    child = (
        "import os,termios,time; "
        "state=termios.tcgetattr(0); state[3]&=~termios.ECHO; "
        "termios.tcsetattr(0,termios.TCSANOW,state); "
        "os.write(0,b'ready'); time.sleep(30)"
    )
    timeout_input = b"timeout-password-canary\n"

    def enter_password() -> None:
        ready, _, _ = select.select([master], [], [], 2)
        assert ready
        assert b"ready" in os.read(master, 4096)
        os.write(master, timeout_input)

    worker = threading.Thread(target=enter_password)
    worker.start()
    restored = None
    queued = None
    try:
        with pytest.raises(launcher.CommandTimeout):
            launcher.run_bounded(
                ["/usr/bin/python3", "-I", "-c", child],
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                timeout_seconds=0.2,
                max_output_bytes=1024,
                tty_input=True,
            )
        worker.join(timeout=2)
        assert not worker.is_alive()
        restored = termios.tcgetattr(slave)
        os.set_blocking(slave, False)
        try:
            queued = os.read(slave, 4096)
        except BlockingIOError:
            queued = b""
    finally:
        termios.tcsetattr(slave, termios.TCSANOW, original)
        os.close(master)
        os.close(slave)

    assert restored == original
    assert queued == b""


@pytest.mark.parametrize(
    ("argv", "expected_control", "expected_stdout"),
    [
        (
            ["preflight"],
            [("preflight",)],
            PREFLIGHT_OUTPUT,
        ),
        (
            ["portfolio", "status", "--project", "prj-claude-config"],
            [("portfolio", "status", "--project", "prj-claude-config")],
            PORTFOLIO_PROJECTED,
        ),
        (
            ["task", "start", "--issue", "https://example.test/issues/28"],
            [("preflight",), ("task", "start", "--issue", "https://example.test/issues/28")],
            json.dumps(
                {
                    "command": "task start",
                    "result": {
                        "branch": TASK_BRANCH,
                        "claim_id": CLAIM_ID,
                        "task_id": TASK_ID,
                        "worktree_ref": WORKTREE_REF,
                    },
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            + b"\n",
        ),
    ],
)
def test_allowed_commands_use_stores_and_forward_safe_result_exactly(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
    expected_control: list[tuple[str, ...]],
    expected_stdout: bytes,
) -> None:
    runner = FakeCommandRunner(launcher)

    result = run_secure(launcher, tmp_path, argv, runner)

    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == json.loads(expected_stdout)
    assert runner.calls[0]["argv"][:3] == (runner.tools.python, "-I", "-c")
    assert runner.calls[1]["argv"] == (
        runner.tools.gh,
        "auth",
        "status",
        "--hostname",
        "github.com",
        "--active",
        "--show-token",
        "--json",
        "hosts",
    )
    assert [call["argv"][2:] for call in runner.calls[2:]] == expected_control

    provider_envs = [call["env"] for call in runner.calls[:2]]
    for index, env in enumerate(provider_envs):
        assert env["HOME"] == str(tmp_path)
        assert env["PATH"] == launcher.TRUSTED_PATH
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path=/run/user/{os.getuid()}/bus"
        if index == 0:
            assert env["PYTHON_KEYRING_BACKEND"] == launcher.SAFE_KEYRING_BACKEND
        else:
            assert "PYTHON_KEYRING_BACKEND" not in env
        for name in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_PROJECT_TOKEN",
            "GH_REPO_TOKEN",
            "NOTION_API_KEY",
            "GH_CONFIG_DIR",
            "GH_HOST",
            "PYTHONPATH",
            "PYTHONHOME",
            "NODE_OPTIONS",
            "LD_PRELOAD",
            "BASH_ENV",
            "GIT_ASKPASS",
        ):
            assert name not in env

    for call in runner.calls[2:]:
        env = call["env"]
        assert env["GH_PROJECT_TOKEN"] == PROJECT_TOKEN
        assert env["GH_REPO_TOKEN"] == REPOSITORY_TOKEN
        assert env["NOTION_API_KEY"] == NOTION_TOKEN
        assert env["PATH"] == launcher.TRUSTED_PATH
        assert env["HOME"] == str(tmp_path)
        assert "NODE_OPTIONS" not in env
        assert "PYTHONPATH" not in env
        assert "LD_PRELOAD" not in env


@pytest.mark.parametrize("title", ["한" * 100, "😀" * 128])
def test_portfolio_accepts_upstream_valid_unicode_title_lengths(
    launcher: ModuleType,
    tmp_path: Path,
    title: str,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("portfolio", "status")
    payload = {
        "command": "portfolio status",
        "result": {
            "page_id": "page-1",
            "markdown": "ready",
            "items": [
                {
                    "project_item_id": "PVTI_fixture",
                    "source_node_id": "I_fixture",
                    "project_id": "prj-claude-config",
                    "title": title,
                    "objective": "한" * 2000,
                    "repo_ids": ["repo-claude-config"],
                    "fields": {
                        "status": "active",
                        "priority": "P1",
                        "health": "on-track",
                        "next_action": f"task:{TASK_ID}",
                        "last_reviewed": "2026-08-26",
                    },
                    "stale": False,
                }
            ],
            "repositories": [],
            "truncated": False,
            "total_items": 1,
        },
    }
    runner.control_results[command] = launcher.CommandResult(
        0,
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode() + b"\n",
        b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 0
    assert json.loads(result.stdout)["result"]["items"][0]["title"] == title


@pytest.mark.parametrize("title", ["x" * 257, "😀" * 129])
def test_portfolio_rejects_titles_over_upstream_utf16_limit(
    launcher: ModuleType,
    tmp_path: Path,
    title: str,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("portfolio", "status")
    payload = {
        "command": "portfolio status",
        "result": {
            "page_id": "page-1",
            "markdown": "ready",
            "items": [
                {
                    "project_item_id": "PVTI_fixture",
                    "source_node_id": "I_fixture",
                    "project_id": "prj-claude-config",
                    "title": title,
                    "objective": "objective",
                    "repo_ids": ["repo-claude-config"],
                    "fields": {
                        "status": "active",
                        "priority": "P1",
                        "health": "on-track",
                        "next_action": f"task:{TASK_ID}",
                        "last_reviewed": "2026-08-26",
                    },
                    "stale": False,
                }
            ],
            "repositories": [],
            "truncated": False,
            "total_items": 1,
        },
    }
    runner.control_results[command] = launcher.CommandResult(
        0,
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode() + b"\n",
        b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


@pytest.mark.parametrize(
    ("field", "value", "expected_returncode"),
    [
        ("project_item_id", "A" * 256, 0),
        ("project_item_id", "A" * 257, 78),
        ("source_node_id", "N" * 128, 0),
        ("source_node_id", "N" * 129, 78),
    ],
)
def test_portfolio_github_ids_match_upstream_byte_bounds(
    launcher: ModuleType,
    tmp_path: Path,
    field: str,
    value: str,
    expected_returncode: int,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("portfolio", "status")
    payload = portfolio_contract_payload(**{field: value})
    runner.control_results[command] = launcher.CommandResult(
        0,
        json.dumps(payload, separators=(",", ":")).encode() + b"\n",
        b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == expected_returncode
    if expected_returncode:
        assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


@pytest.mark.parametrize(
    ("slug", "expected_returncode"),
    [
        (f"{'a' * 39}/{'r' * 100}", 0),
        ("bad_owner/repo", 78),
        (f"{'a' * 40}/repo", 78),
        (f"owner/{'r' * 101}", 78),
    ],
)
def test_portfolio_repository_slug_matches_upstream_schema(
    launcher: ModuleType,
    tmp_path: Path,
    slug: str,
    expected_returncode: int,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("portfolio", "status")
    runner.control_results[command] = launcher.CommandResult(
        0,
        json.dumps(
            portfolio_contract_payload(slug=slug),
            separators=(",", ":"),
        ).encode()
        + b"\n",
        b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == expected_returncode
    if expected_returncode:
        assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


def test_keyring_backend_selection_ignores_ambient_configuration(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    environment = {
        "PYTHON_KEYRING_BACKEND": "evil_backend.Keyring",
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
    }

    selected = launcher._keyring_environment(tmp_path, environment, uid=os.getuid())

    assert selected["PYTHON_KEYRING_BACKEND"] == launcher.SAFE_KEYRING_BACKEND
    assert "type(backend) is not SecretServiceKeyring" in launcher.KEYRING_HELPER
    assert "backend.get_password" in launcher.KEYRING_HELPER
    assert "keyring.get_password" not in launcher.KEYRING_HELPER


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"command": "task finish"}),
        lambda payload: payload["result"]["claim"].update({"task_id": TASK_ID[:-1] + "c"}),
        lambda payload: payload["result"].update({"branch": "task/different"}),
        lambda payload: payload["result"].update({"worktree_ref": "/tmp/untrusted"}),
        lambda payload: payload["result"]["claim"].update({"host": "other-build-host"}),
        lambda payload: (
            payload["result"].update(
                {"branch": "task/ffffffffffff-other", "worktree_ref": "wt-ffffffffffff-other"}
            ),
            payload["result"]["claim"].update(
                {"branch": "task/ffffffffffff-other", "worktree_ref": "wt-ffffffffffff-other"}
            ),
        ),
        lambda payload: payload["result"].update({"unexpected": "field"}),
    ],
)
def test_task_start_output_requires_exact_consistent_source_shape(
    launcher: ModuleType,
    tmp_path: Path,
    mutate,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "start", "--issue", "https://example.test/issues/28")
    payload = json.loads(runner.control_results[command].stdout)
    mutate(payload)
    runner.control_results[command] = launcher.CommandResult(
        0,
        json.dumps(payload, separators=(",", ":")).encode() + b"\n",
        b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


def test_task_start_output_projects_only_approved_fields(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "start", "--issue", "https://example.test/issues/28")
    payload = json.loads(runner.control_results[command].stdout)
    payload["journal_warning"] = {"code": "JOURNAL_WRITE_FAILED"}
    runner.control_results[command] = launcher.CommandResult(
        0,
        json.dumps(payload, separators=(",", ":")).encode() + b"\n",
        b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert json.loads(result.stdout) == {
        "command": "task start",
        "journal_warning": {"code": "JOURNAL_WRITE_FAILED"},
        "result": {
            "branch": TASK_BRANCH,
            "claim_id": CLAIM_ID,
            "task_id": TASK_ID,
            "worktree_ref": WORKTREE_REF,
        },
    }


def test_task_start_success_is_bound_to_an_explicit_task_request(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    source_command = ("task", "start", "--issue", "https://example.test/issues/28")
    command = ("task", "start", "--task", TASK_ID)
    runner.control_results[command] = runner.control_results[source_command]

    accepted = run_secure(launcher, tmp_path, list(command), runner)

    assert accepted.returncode == 0
    payload = json.loads(runner.control_results[source_command].stdout)
    payload["result"]["task"]["task_id"] = OTHER_TASK_ID
    payload["result"]["claim"].update(
        {
            "task_id": OTHER_TASK_ID,
            "branch": OTHER_TASK_BRANCH,
            "worktree_ref": OTHER_WORKTREE_REF,
        }
    )
    payload["result"].update(
        {"branch": OTHER_TASK_BRANCH, "worktree_ref": OTHER_WORKTREE_REF}
    )
    runner.control_results[command] = launcher.CommandResult(
        0,
        json.dumps(payload, separators=(",", ":")).encode() + b"\n",
        b"",
    )

    rejected = run_secure(launcher, tmp_path, list(command), runner)

    assert rejected.returncode == 78
    assert json.loads(rejected.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


@pytest.mark.parametrize(
    "command",
    [
        (
            "task",
            "start",
            "--project",
            "prj-another-project",
            "--repo-id",
            "repo-claude-config",
        ),
        (
            "task",
            "start",
            "--project",
            "prj-claude-config",
            "--repo-id",
            "repo-another-repository",
        ),
    ],
)
def test_task_registration_success_is_bound_to_explicit_project_and_repository(
    launcher: ModuleType,
    tmp_path: Path,
    command: tuple[str, ...],
) -> None:
    runner = FakeCommandRunner(launcher)
    source_command = ("task", "start", "--issue", "https://example.test/issues/28")
    runner.control_results[command] = runner.control_results[source_command]

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


@pytest.mark.parametrize("leaks_path", [False, True])
def test_task_start_latest_handoff_is_strict_and_path_scanned(
    launcher: ModuleType,
    tmp_path: Path,
    leaks_path: bool,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "start", "--issue", "https://example.test/issues/28")
    payload = json.loads(runner.control_results[command].stdout)
    sections = {
        "Progress Since Last Checkpoint": "implemented",
        "Git State": "clean",
        "Validation Performed": "tests pass",
        "Failures and Uncertainty": "none",
        "Session-Local Next Step": "continue",
        "Related ADR and Evidence": "design doc",
    }
    if leaks_path:
        sections["Git State"] = str(tmp_path / ".config" / "jhw-control" / "control.env")
    payload["result"]["latest_handoff"] = {
        "handoff_pointer": f"handoffs/{TASK_ID}/{CLAIM_ID}.md",
        "claim_id": CLAIM_ID,
        "generated_at": "2026-08-26T00:01:00.000Z",
        "sections": sections,
        "truncated": False,
    }
    runner.control_results[command] = launcher.CommandResult(
        0,
        json.dumps(payload, separators=(",", ":")).encode() + b"\n",
        b"",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    if leaks_path:
        assert result.returncode == 78
        assert json.loads(result.stderr) == {"error": {"code": "SENSITIVE_OUTPUT_REJECTED"}}
    else:
        assert result.returncode == 0
        assert json.loads(result.stdout)["result"]["latest_handoff"]["sections"] == sections


def test_task_start_success_never_echoes_source_repo_path(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    source_command = ("task", "start", "--issue", "https://example.test/issues/28")
    source_result = runner.control_results[source_command]
    repo_path = tmp_path / "source-checkout"
    command = ("task", "start", "--repo-path", str(repo_path), "--session", "codex-test")
    runner.control_results[command] = source_result

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 0
    assert str(repo_path).encode() not in result.stdout + result.stderr
    assert json.loads(result.stdout)["result"]["worktree_ref"] == WORKTREE_REF


def test_control_output_requires_json_stream_contract(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.control_results[("preflight",)] = launcher.CommandResult(75, b"safe stdout\n", b"safe stderr\n")

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


@pytest.mark.parametrize(
    "argv",
    [[], ["--help"], ["task"], ["task", "finish"], ["board", "status"], ["project", "register"]],
)
def test_non_allowlisted_command_stops_before_config_or_provider(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
) -> None:
    runner = FakeCommandRunner(launcher)

    result = launcher.run_program(
        argv,
        home=tmp_path / "missing",
        environment={},
        uid=os.getuid(),
        command_runner=runner,
        tools=runner.tools,
    )

    assert result.returncode == 2
    assert json.loads(result.stderr) == {"error": {"code": "INVALID_ARGUMENT"}}
    assert runner.calls == []


def test_hidden_preflight_failure_preserves_safe_result_and_skips_task(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    failure = launcher.CommandResult(
        78,
        b"",
        b'{"error":{"code":"PREFLIGHT_UNAVAILABLE"}}\n',
    )
    runner.control_results[("preflight",)] = failure

    result = run_secure(
        launcher,
        tmp_path,
        ["task", "start", "--issue", "https://example.test/issues/28"],
        runner,
    )

    assert result == launcher.ProgramResult(failure.returncode, failure.stdout, failure.stderr)
    assert [call["argv"][2:] for call in runner.calls[2:]] == [("preflight",)]


def test_child_stderr_and_exit_are_preserved(launcher: ModuleType, tmp_path: Path) -> None:
    runner = FakeCommandRunner(launcher)
    expected = launcher.CommandResult(75, b"", b'{"error":{"code":"LOCK_BUSY"}}\n')
    runner.control_results[("preflight",)] = expected

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    assert result == launcher.ProgramResult(75, expected.stdout, expected.stderr)


@pytest.mark.parametrize("canary_kind", ["project", "repository", "notion", "config", "gh-store"])
def test_sensitive_child_output_is_replaced_with_fixed_error(
    launcher: ModuleType,
    tmp_path: Path,
    canary_kind: str,
) -> None:
    runner = FakeCommandRunner(launcher)
    canaries = {
        "project": PROJECT_TOKEN,
        "repository": REPOSITORY_TOKEN,
        "notion": NOTION_TOKEN,
        "config": str(tmp_path / ".config" / "jhw-control" / "control.env"),
        "gh-store": str(tmp_path / ".config" / "gh"),
    }
    runner.control_results[("preflight",)] = launcher.CommandResult(
        0,
        f"unsafe={canaries[canary_kind]}\n".encode(),
        b"",
    )

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    combined = result.stdout + result.stderr
    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "SENSITIVE_OUTPUT_REJECTED"}}
    assert canaries[canary_kind].encode() not in combined


@pytest.mark.parametrize(
    "encoding",
    [
        "json-escaped",
        "base64",
        "unpadded-base64",
        "uppercase-hex",
        "mixed-case-hex",
        "lowercase-percent",
        "mixed-case-percent",
        "url-quoted-path",
        "lowercase-url-quoted-path",
        "mixed-case-url-quoted-path",
        "reverse-stream-split",
    ],
)
def test_encoded_or_cross_stream_secret_is_rejected(
    launcher: ModuleType,
    tmp_path: Path,
    encoding: str,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "start", "--issue", "https://example.test/issues/28")
    protected = PROJECT_TOKEN
    placeholder = b"ENCODED_CANARY_PLACEHOLDER"
    if encoding == "json-escaped":
        encoded = "".join(f"\\u{ord(character):04x}" for character in PROJECT_TOKEN).encode()
    elif encoding == "base64":
        encoded = base64.b64encode(PROJECT_TOKEN.encode())
    elif encoding == "unpadded-base64":
        encoded = base64.b64encode(NOTION_TOKEN.encode()).rstrip(b"=")
        protected = NOTION_TOKEN
    elif encoding == "uppercase-hex":
        encoded = PROJECT_TOKEN.encode().hex().upper().encode()
    elif encoding == "mixed-case-hex":
        index = 0

        def alternate_hex_case(match: re.Match[bytes]) -> bytes:
            nonlocal index
            index += 1
            return match.group(0).upper() if index % 2 else match.group(0)

        encoded = re.sub(rb"[a-f]", alternate_hex_case, PROJECT_TOKEN.encode().hex().encode())
    elif encoding == "lowercase-percent":
        encoded = b"".join(f"%{byte:02x}".encode() for byte in PROJECT_TOKEN.encode())
    elif encoding == "mixed-case-percent":
        chunks = [f"%{byte:02X}".encode() for byte in PROJECT_TOKEN.encode()]
        encoded = b"".join(chunk.lower() if index % 2 else chunk for index, chunk in enumerate(chunks))
    elif encoding in {"url-quoted-path", "lowercase-url-quoted-path", "mixed-case-url-quoted-path"}:
        protected = str(tmp_path / ".config" / "jhw-control" / "control.env")
        encoded = quote_from_bytes(protected.encode(), safe="").encode()
        if encoding == "lowercase-url-quoted-path":
            encoded = encoded.replace(b"%2F", b"%2f")
        elif encoding == "mixed-case-url-quoted-path":
            index = 0

            def alternate_escape_case(match: re.Match[bytes]) -> bytes:
                nonlocal index
                index += 1
                return match.group(0).lower() if index % 2 else match.group(0).upper()

            encoded = re.sub(rb"%[0-9A-F]{2}", alternate_escape_case, encoded)
    else:
        command = ("preflight",)
        midpoint = len(PROJECT_TOKEN) // 2
        runner.control_results[command] = launcher.CommandResult(
            0,
            PROJECT_TOKEN[midpoint:].encode(),
            PROJECT_TOKEN[:midpoint].encode(),
        )
        encoded = b""

    if command[0] == "task":
        payload = json.loads(runner.control_results[command].stdout)
        payload["result"]["latest_handoff"] = {
            "handoff_pointer": f"handoffs/{TASK_ID}/{CLAIM_ID}.md",
            "claim_id": CLAIM_ID,
            "generated_at": "2026-08-26T00:01:00Z",
            "sections": {
                "Progress Since Last Checkpoint": "implemented",
                "Git State": placeholder.decode(),
                "Validation Performed": "tests pass",
                "Failures and Uncertainty": "none",
                "Session-Local Next Step": "continue",
                "Related ADR and Evidence": "design doc",
            },
            "truncated": False,
        }
        result_bytes = json.dumps(payload, separators=(",", ":")).encode().replace(placeholder, encoded) + b"\n"
        runner.control_results[command] = launcher.CommandResult(0, result_bytes, b"")

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "SENSITIVE_OUTPUT_REJECTED"}}
    assert protected.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "argv",
    [
        [
            "task", "start", "--resolve-from-checkout", "true",
            "--repo-path", "/fixture/source",
            "--issue-url", "https://github.com/example/control/issues/28",
            "--issue-node-id", "I_kwDOControl28",
            "--issue-revision", "issue-revision-28",
            "--session", "codex-resolved-formal",
        ],
        [
            "task", "start", "--resolve-from-checkout", "true",
            "--repo-path", "/fixture/source",
            "--temp-alias", "control-resolver",
            "--goal", "resolve checkout coordinates",
            "--done", "unique Project selected",
            "--scope", "Task registration",
            "--session", "codex-resolved-temporary",
        ],
    ],
)
def test_resolver_start_forwards_complete_registration_argv(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
) -> None:
    runner = FakeCommandRunner(launcher)
    raw_upstream = runner.control_results[
        ("task", "start", "--issue", "https://example.test/issues/28")
    ]
    runner.control_results[tuple(argv)] = raw_upstream

    result = run_secure(launcher, tmp_path, argv, runner)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "command": "task start",
        "result": {
            "branch": TASK_BRANCH,
            "claim_id": CLAIM_ID,
            "task_id": TASK_ID,
            "worktree_ref": WORKTREE_REF,
        },
    }
    assert [call["argv"][2:] for call in runner.calls[2:]] == [
        ("preflight",), tuple(argv),
    ]


@pytest.mark.parametrize(
    ("argv", "code", "returncode"),
    [
        (["preflight"], "SENSITIVE_OUTPUT_REJECTED", 78),
        (["preflight"], "UNSAFE_STATE_PATH", 78),
        (["preflight"], "REPOSITORY_IDENTITY_MISMATCH", 78),
        (["preflight"], "PROJECT_NOT_FOUND", 78),
        (["preflight"], "PROJECT_CHANGED_DURING_READ", 78),
        (["preflight"], "INCOMPLETE_PROJECT_FIELD_READ", 78),
        (["preflight"], "INVALID_PROJECT_MUTATION", 78),
        (["portfolio", "status"], "SENSITIVE_OUTPUT_REJECTED", 1),
        (["portfolio", "status"], "INVALID_PREFLIGHT_ITEM", 1),
        (["portfolio", "status"], "INVALID_PROJECT_RECORD", 1),
        (["portfolio", "status"], "REPOSITORY_NOT_FOUND", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "SENSITIVE_OUTPUT_REJECTED", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "INVALID_ISSUE_RESPONSE", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "INVALID_REPOSITORY_RESPONSE", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "INVALID_CHECKOUT_ORIGIN", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "INVALID_CLOCK", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "INVALID_CLAIM", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "UNSAFE_WORKTREE_PATH", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "INVALID_PROJECT_RESPONSE", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "PROJECT_NOT_FOUND", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "PROJECT_NOT_PRIVATE", 78),
        (["task", "start", "--issue", "https://example.test/issues/28"], "PROJECT_CHANGED_DURING_READ", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "INCOMPLETE_PROJECT_READ", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "INVALID_PREFLIGHT_ITEM", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "INVALID_PROJECT_RECORD", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "DUPLICATE_PROJECT_ITEM", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "DUPLICATE_PROJECT_RECORD", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "REGISTRY_ROOT_MISMATCH", 1),
        (["task", "start", "--issue", "https://example.test/issues/28"], "AMBIGUOUS_REGISTRY_REMOTE", 78),
        (["task", "start", "--issue", "https://example.test/issues/28"], "REGISTRY_REMOTE_NOT_SSH", 78),
        (["task", "start", "--issue", "https://example.test/issues/28"], "REGISTRY_REMOTE_MISMATCH", 78),
        (["task", "start", "--resolve-from-checkout", "true"], "PROJECT_REPOSITORY_NOT_FOUND", 1),
        (["task", "start", "--resolve-from-checkout", "true"], "PROJECT_REPOSITORY_AMBIGUOUS", 1),
    ],
)
def test_reachable_command_errors_are_preserved(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
    code: str,
    returncode: int,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.control_results[tuple(argv)] = launcher.CommandResult(
        returncode,
        b"",
        json.dumps({"error": {"code": code}}, separators=(",", ":")).encode() + b"\n",
    )

    result = run_secure(launcher, tmp_path, argv, runner)

    assert result.returncode == returncode
    assert json.loads(result.stderr) == {"error": {"code": code}}


@pytest.mark.parametrize(
    ("argv", "error", "returncode"),
    [
        (["preflight"], {"code": "BOARD_BUSY", "reason": "exclusive_holder"}, 4),
        (["preflight"], {"code": "MADE_UP_ERROR"}, 4),
        (["preflight"], {"code": "MISSING_CREDENTIAL"}, 4),
        (["portfolio", "status"], {"code": "TASK_ALREADY_CLAIMED"}, 4),
        (["portfolio", "status"], {"code": "INVALID_CLOCK"}, 1),
        (
            ["task", "start", "--issue", "https://example.test/issues/28"],
            {"code": "HANDOFF_RETRY_CONFLICT", "reason": "git_identity_changed"},
            1,
        ),
        *[
            (
                ["task", "start", "--issue", "https://example.test/issues/28"],
                {"code": code},
                1,
            )
            for code in (
                "CLAIM_ALREADY_ACTIVE",
                "SOURCE_REVISION_MISMATCH",
                "WORKTREE_NOT_MAPPED",
                "WORKTREE_CLAIM_MISMATCH",
                "WORKTREE_REMOVE_PENDING",
                "WORKTREE_REMOVED",
                "WORKTREE_UNPUSHED",
                "WORKTREE_CLEANUP_FAILED",
                "INVALID_WORKTREE_INSPECTION",
                "STATE_PERSIST_FAILED",
                "UNSAFE_HANDOFF_PATH",
                "HANDOFF_NOT_FOUND",
                "INVALID_REPOSITORY",
                "REPOSITORY_ID_COLLISION",
                "SOURCE_ALREADY_MAPPED",
            )
        ],
        (["portfolio", "status"], {"code": "PROJECT_RECORD_NOT_FOUND"}, 1),
        (["portfolio", "status"], {"code": "INVALID_REPOSITORY_RESPONSE"}, 1),
    ],
)
def test_error_output_uses_a_closed_command_specific_schema(
    launcher: ModuleType,
    tmp_path: Path,
    argv: list[str],
    error: dict[str, object],
    returncode: int,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.control_results[tuple(argv)] = launcher.CommandResult(
        returncode,
        b"",
        json.dumps({"error": error}, separators=(",", ":")).encode() + b"\n",
    )

    result = run_secure(launcher, tmp_path, argv, runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


@pytest.mark.parametrize(
    "change",
    [
        {"branch": "task/different"},
        {"worktree_ref": "/tmp/not-a-coordinate"},
        {"branch": "task/ffffffffffff-other", "worktree_ref": "wt-ffffffffffff-other"},
        {"host": "other\nbuild-host"},
        {"host": "x" * 256},
        {"started_at": "2026-02-30T00:00:00Z"},
    ],
)
def test_task_conflict_error_requires_canonical_consistent_coordinates(
    launcher: ModuleType,
    tmp_path: Path,
    change: dict[str, str],
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "start", "--issue", "https://example.test/issues/28")
    conflict = {
        "task_id": TASK_ID,
        "claim_id": CLAIM_ID,
        "host": "build-1",
        "branch": TASK_BRANCH,
        "worktree_ref": WORKTREE_REF,
        "started_at": "2026-08-26T00:00:00Z",
        **change,
    }
    runner.control_results[command] = launcher.CommandResult(
        4,
        b"",
        json.dumps(
            {"error": {"code": "TASK_ALREADY_CLAIMED", "conflicting_claim": conflict}},
            separators=(",", ":"),
        ).encode() + b"\n",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


def test_task_conflict_accepts_another_host_but_does_not_disclose_it(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "start", "--issue", "https://example.test/issues/28")
    conflict = {
        "task_id": TASK_ID,
        "claim_id": CLAIM_ID,
        "host": "other-build-host",
        "branch": TASK_BRANCH,
        "worktree_ref": WORKTREE_REF,
        "started_at": "2026-08-26T00:00:00Z",
    }
    runner.control_results[command] = launcher.CommandResult(
        4,
        b"",
        json.dumps(
            {"error": {"code": "TASK_ALREADY_CLAIMED", "conflicting_claim": conflict}},
            separators=(",", ":"),
        ).encode()
        + b"\n",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 4
    assert json.loads(result.stderr) == {
        "error": {
            "code": "TASK_ALREADY_CLAIMED",
            "conflicting_claim": {
                "task_id": TASK_ID,
                "claim_id": CLAIM_ID,
                "branch": TASK_BRANCH,
                "worktree_ref": WORKTREE_REF,
                "started_at": "2026-08-26T00:00:00Z",
            },
        }
    }
    assert b"other-build-host" not in result.stderr


@pytest.mark.parametrize(
    ("code", "details", "returncode"),
    [
        (
            "TASK_ALREADY_CLAIMED",
            {
                "conflicting_claim": {
                    "task_id": OTHER_TASK_ID,
                    "claim_id": CLAIM_ID,
                    "host": "other-build-host",
                    "branch": OTHER_TASK_BRANCH,
                    "worktree_ref": OTHER_WORKTREE_REF,
                    "started_at": "2026-08-26T00:00:00Z",
                }
            },
            4,
        ),
        (
            "TASK_START_FAILED",
            {
                "retained_claim": {
                    "task_id": OTHER_TASK_ID,
                    "claim_id": CLAIM_ID,
                    "state": "active",
                }
            },
            1,
        ),
    ],
)
def test_task_error_claims_are_bound_to_an_explicit_task_request(
    launcher: ModuleType,
    tmp_path: Path,
    code: str,
    details: dict[str, object],
    returncode: int,
) -> None:
    runner = FakeCommandRunner(launcher)
    command = ("task", "start", "--task", TASK_ID)
    runner.control_results[command] = launcher.CommandResult(
        returncode,
        b"",
        json.dumps(
            {"error": {"code": code, **details}},
            separators=(",", ":"),
        ).encode()
        + b"\n",
    )

    result = run_secure(launcher, tmp_path, list(command), runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CONTROL_OUTPUT_INVALID"}}


def test_worktree_repo_and_ssh_paths_are_protected(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    repo_path = tmp_path / "checkout"
    socket_path = tmp_path / "agent.sock"
    with socket.socket(socket.AF_UNIX) as agent:
        agent.bind(str(socket_path))
        cases = [
            (CONFIG_VALUES["JHW_WORKTREE_ROOT"], {}, ["preflight"]),
            (
                str(repo_path),
                {},
                ["task", "start", "--repo-path", str(repo_path)],
            ),
            (str(socket_path), {"SSH_AUTH_SOCK": str(socket_path)}, ["preflight"]),
        ]
        for leaked, extra_environment, argv in cases:
            runner.control_results[("preflight",)] = launcher.CommandResult(
                0,
                f"unsafe={leaked}\n".encode(),
                b"",
            )
            result = run_secure(
                launcher,
                tmp_path,
                argv,
                runner,
                environment=extra_environment,
            )
            assert result.returncode == 78
            assert json.loads(result.stderr) == {
                "error": {"code": "SENSITIVE_OUTPUT_REJECTED"}
            }


def test_identical_project_and_repository_tokens_stop_before_child(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    gh_payload = json.loads(runner.gh_result.stdout)
    gh_payload["hosts"]["github.com"][0]["token"] = PROJECT_TOKEN
    runner.gh_result = launcher.CommandResult(0, json.dumps(gh_payload).encode(), b"")

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": "CREDENTIALS_NOT_SEPARATE"}}
    assert len(runner.calls) == 2
    assert PROJECT_TOKEN.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("keyring_patch", "expected_code"),
    [
        ({"backend": "keyring.backends.fail.Keyring"}, "OS_CREDENTIAL_STORE_UNAVAILABLE"),
        ({"project": ""}, "PROJECT_CREDENTIAL_UNAVAILABLE"),
        ({"notion": ""}, "NOTION_CREDENTIAL_UNAVAILABLE"),
    ],
)
def test_keyring_provenance_or_missing_secret_fails_closed(
    launcher: ModuleType,
    tmp_path: Path,
    keyring_patch: dict[str, str],
    expected_code: str,
) -> None:
    runner = FakeCommandRunner(launcher)
    payload = json.loads(runner.keyring_result.stdout)
    payload.update(keyring_patch)
    runner.keyring_result = launcher.CommandResult(0, json.dumps(payload).encode(), b"")

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    assert result.returncode == 78
    error = json.loads(result.stderr)["error"]
    assert error["code"] == expected_code
    if expected_code == "PROJECT_CREDENTIAL_UNAVAILABLE":
        assert error["action"] == (
            "/usr/bin/python3 -I -m keyring --keyring-backend "
            "keyring.backends.SecretService.Keyring set jhw-control GH_PROJECT_TOKEN"
        )
    elif expected_code == "NOTION_CREDENTIAL_UNAVAILABLE":
        assert error["action"] == (
            "/usr/bin/python3 -I -m keyring --keyring-backend "
            "keyring.backends.SecretService.Keyring set jhw-control NOTION_API_KEY"
        )
    assert len(runner.calls) == 1
    assert PROJECT_TOKEN.encode() not in result.stdout + result.stderr
    assert NOTION_TOKEN.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"hosts": {"enterprise.test": payload["hosts"]["github.com"]}}),
        lambda payload: payload["hosts"]["github.com"].append(dict(payload["hosts"]["github.com"][0])),
        lambda payload: payload["hosts"]["github.com"][0].update({"tokenSource": "oauth_token"}),
        lambda payload: payload["hosts"]["github.com"][0].update({"login": "someone-else"}),
        lambda payload: payload["hosts"]["github.com"][0].update({"state": "error"}),
        lambda payload: payload["hosts"]["github.com"][0].update({"active": False}),
    ],
)
def test_github_credential_requires_single_exact_keyring_entry(
    launcher: ModuleType,
    tmp_path: Path,
    mutate,
) -> None:
    runner = FakeCommandRunner(launcher)
    payload = json.loads(runner.gh_result.stdout)
    mutate(payload)
    runner.gh_result = launcher.CommandResult(0, json.dumps(payload).encode(), b"")

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    assert result.returncode == 78
    assert json.loads(result.stderr)["error"]["code"] in {
        "REPOSITORY_CREDENTIAL_NOT_SECURE",
        "REPOSITORY_CREDENTIAL_UNAVAILABLE",
    }
    assert len(runner.calls) == 2
    assert REPOSITORY_TOKEN.encode() not in result.stdout + result.stderr


def test_duplicate_provider_json_keys_fail_closed(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.gh_result = launcher.CommandResult(
        0,
        (
            '{"hosts":{"github.com":[{"active":true,"host":"github.com",'
            '"login":"jhw7500","state":"success","token":"repository-token-1234567890",'
            '"tokenSource":"plaintext","tokenSource":"keyring"}]}}'
        ).encode(),
        b"",
    )

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {
        "error": {
            "action": "gh auth login --hostname github.com --git-protocol ssh --web",
            "code": "REPOSITORY_CREDENTIAL_UNAVAILABLE",
        }
    }


def test_provider_diagnostic_is_never_forwarded(launcher: ModuleType, tmp_path: Path) -> None:
    runner = FakeCommandRunner(launcher)
    diagnostic = f"locked: {PROJECT_TOKEN} at {tmp_path / '.local/share/keyrings'}"
    runner.keyring_result = launcher.CommandResult(1, b"", diagnostic.encode())

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    assert result.returncode == 78
    assert json.loads(result.stderr)["error"]["code"] == "OS_CREDENTIAL_STORE_UNAVAILABLE"
    assert diagnostic.encode() not in result.stdout + result.stderr


def test_locked_store_points_to_the_single_launcher_unlock_command(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.keyring_result = launcher.CommandResult(21, b"", b"private diagnostic")

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {
        "error": {
            "action": "jhw-control-host unlock",
            "code": "OS_CREDENTIAL_STORE_LOCKED",
        }
    }


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("timeout", "CREDENTIAL_PROVIDER_TIMEOUT"),
        ("oversize", "CREDENTIAL_PROVIDER_OUTPUT_TOO_LARGE"),
    ],
)
def test_provider_runtime_failure_is_stable_and_nonleaking(
    launcher: ModuleType,
    tmp_path: Path,
    failure: str,
    expected_code: str,
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.keyring_result = (
        launcher.CommandTimeout()
        if failure == "timeout"
        else launcher.CommandOutputTooLarge()
    )

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {"error": {"code": expected_code}}


def test_bounded_runner_preserves_streams_exit_and_closes_stdin(launcher: ModuleType) -> None:
    result = launcher.run_bounded(
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            "import sys; data=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(b'out:'+data); sys.stderr.buffer.write(b'err'); raise SystemExit(75)",
        ],
        env={"PATH": launcher.TRUSTED_PATH},
        timeout_seconds=2,
        max_output_bytes=128,
    )

    assert result == launcher.CommandResult(75, b"out:", b"err")


def test_bounded_runner_stops_timeout_and_output_overflow(launcher: ModuleType) -> None:
    with pytest.raises(launcher.CommandTimeout):
        launcher.run_bounded(
            ["/usr/bin/python3", "-I", "-c", "import time; time.sleep(5)"],
            env={"PATH": launcher.TRUSTED_PATH},
            timeout_seconds=0.05,
            max_output_bytes=128,
        )

    with pytest.raises(launcher.CommandOutputTooLarge):
        launcher.run_bounded(
            ["/usr/bin/python3", "-I", "-c", "import os; os.write(1, b'x' * 1024)"],
            env={"PATH": launcher.TRUSTED_PATH},
            timeout_seconds=2,
            max_output_bytes=32,
        )


def test_bounded_runner_kills_descendant_after_parent_exits(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "escaped-descendant"
    descendant = (
        "import time; from pathlib import Path; time.sleep(0.4); "
        f"Path({str(marker)!r}).touch()"
    )
    parent = (
        "import subprocess, sys; "
        f"subprocess.Popen(['/usr/bin/python3', '-I', '-c', {descendant!r}]); "
        "raise SystemExit(0)"
    )

    with pytest.raises(launcher.CommandTimeout):
        launcher.run_bounded(
            ["/usr/bin/python3", "-I", "-c", parent],
            env={"PATH": launcher.TRUSTED_PATH},
            timeout_seconds=0.05,
            max_output_bytes=128,
        )
    time.sleep(0.5)

    assert not marker.exists()


def test_bounded_runner_kills_child_after_unexpected_capture_error(
    launcher: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "child-survived-capture-error"
    child = (
        "import time; from pathlib import Path; time.sleep(0.3); "
        f"Path({str(marker)!r}).touch()"
    )

    def fail_capture_setup(_descriptor: int, _blocking: bool) -> None:
        raise OSError("injected capture failure")

    monkeypatch.setattr(launcher.os, "set_blocking", fail_capture_setup)
    with pytest.raises(OSError):
        launcher.run_bounded(
            ["/usr/bin/python3", "-I", "-c", child],
            env={"PATH": launcher.TRUSTED_PATH},
            timeout_seconds=2,
            max_output_bytes=128,
        )
    time.sleep(0.5)

    assert not marker.exists()


def test_bounded_runner_kills_child_when_selector_construction_fails(
    launcher: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "child-survived-selector-error"
    child = (
        "import time; from pathlib import Path; time.sleep(0.3); "
        f"Path({str(marker)!r}).touch()"
    )

    def fail_selector():
        raise OSError("injected selector construction failure")

    monkeypatch.setattr(launcher.selectors, "DefaultSelector", fail_selector)
    with pytest.raises(OSError):
        launcher.run_bounded(
            ["/usr/bin/python3", "-I", "-c", child],
            env={"PATH": launcher.TRUSTED_PATH},
            timeout_seconds=2,
            max_output_bytes=128,
        )
    time.sleep(0.5)

    assert not marker.exists()


def test_bounded_runner_kills_child_on_async_exception_immediately_after_popen(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "child-survived-post-popen-exception"
    child = (
        "import time; from pathlib import Path; time.sleep(0.3); "
        f"Path({str(marker)!r}).touch()"
    )
    injected = False

    def interrupt_after_popen(frame, event, arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is launcher.run_bounded.__code__
            and frame.f_locals.get("process") is not None
        ):
            injected = True
            raise RuntimeError("injected post-Popen async exception")
        return interrupt_after_popen

    sys.settrace(interrupt_after_popen)
    try:
        with pytest.raises(RuntimeError, match="post-Popen"):
            launcher.run_bounded(
                ["/usr/bin/python3", "-I", "-c", child],
                env={"PATH": launcher.TRUSTED_PATH},
                timeout_seconds=2,
                max_output_bytes=128,
            )
    finally:
        sys.settrace(None)
    time.sleep(0.5)

    assert injected
    assert not marker.exists()


@pytest.mark.parametrize("unsafe", ["group-writable-file", "writable-parent"])
def test_rejects_executable_writable_by_other_principals(
    launcher: ModuleType,
    tmp_path: Path,
    unsafe: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "tools"
    parent.mkdir(mode=0o700)
    executable = parent / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o775 if unsafe == "group-writable-file" else 0o755)
    if unsafe == "group-writable-file":
        monkeypatch.setattr(launcher, "_group_is_private_to_uid", lambda _gid, _uid: False)
    if unsafe == "writable-parent":
        parent.chmod(0o777)

    with pytest.raises(launcher.LauncherError) as caught:
        launcher._validated_executable(executable, uid=os.getuid())

    assert caught.value.code == "CONTROL_UNAVAILABLE"


def test_rejects_writable_fixed_path_entry_even_when_tool_resolves_later(
    launcher: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writable = tmp_path / "writable-bin"
    trusted = tmp_path / "trusted-bin"
    writable.mkdir(mode=0o700)
    trusted.mkdir(mode=0o700)
    tool = trusted / "gh"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o700)
    writable.chmod(0o777)
    monkeypatch.setattr(launcher, "TRUSTED_PATH", f"{writable}:{trusted}")

    with pytest.raises(launcher.LauncherError) as caught:
        launcher._trusted_tool("gh", uid=os.getuid())

    assert caught.value.code == "CONTROL_UNAVAILABLE"


def test_allows_group_write_only_for_current_uid_private_group(
    launcher: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "private-group-tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o775)
    monkeypatch.setattr(launcher, "_group_is_private_to_uid", lambda _gid, _uid: True)

    assert launcher._validated_executable(executable, uid=os.getuid()) == str(executable.resolve())


@pytest.mark.skipif(shutil.which("setfacl") is None, reason="setfacl is unavailable")
@pytest.mark.parametrize("acl_target", ["file", "parent"])
def test_rejects_executable_or_ancestor_with_extended_acl(
    launcher: ModuleType,
    tmp_path: Path,
    acl_target: str,
) -> None:
    parent = tmp_path / "acl-tools"
    parent.mkdir(mode=0o700)
    executable = parent / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    target = executable if acl_target == "file" else parent
    applied = subprocess.run(
        ["setfacl", "-m", "u:nobody:rwx", str(target)],
        text=True,
        capture_output=True,
        check=False,
    )
    if applied.returncode != 0:
        pytest.skip(f"cannot create POSIX ACL fixture: {applied.stderr}")

    with pytest.raises(launcher.LauncherError) as caught:
        launcher._validated_executable(executable, uid=os.getuid())

    assert caught.value.code == "CONTROL_UNAVAILABLE"


def isolated_harness_source(home: Path, call_log: Path, *, preflight_fails: bool) -> str:
    task_output = json.dumps(
        {
            "command": "task start",
            "result": {
                "task": {
                    "task_id": TASK_ID,
                    "kind": "formal",
                    "project_id": "prj-claude-config",
                    "repo_id": "repo-claude-config",
                },
                "claim": {
                    "task_id": TASK_ID,
                    "claim_id": CLAIM_ID,
                    "project_id": "prj-claude-config",
                    "repo_id": "repo-claude-config",
                    "host": "build-1",
                    "branch": TASK_BRANCH,
                    "worktree_ref": WORKTREE_REF,
                    "started_at": "2026-08-26T00:00:00.000Z",
                },
                "branch": TASK_BRANCH,
                "worktree_ref": WORKTREE_REF,
                "reused": False,
            },
        },
        separators=(",", ":"),
    ).encode() + b"\n"
    return f"""
import importlib.util
import json
import os
import sys
from pathlib import Path

script = Path({str(SCRIPT)!r})
spec = importlib.util.spec_from_file_location("isolated_jhw_control_host", script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
tools = module.HostTools("/trusted/python3", "/trusted/gh", "/trusted/node", "/trusted/control")
calls = []

def runner(argv, *, env, timeout_seconds, max_output_bytes):
    command = tuple(argv)
    if command[0] == tools.python:
        result = module.CommandResult(0, json.dumps({{
            "backend": "keyring.backends.SecretService.Keyring",
            "project": "project-" + "p" * 24,
            "notion": "notion-" + "n" * 24,
        }}).encode(), b"")
    elif command[0] == tools.gh:
        result = module.CommandResult(0, json.dumps({{"hosts": {{"github.com": [{{
            "active": True,
            "host": "github.com",
            "login": "jhw7500",
            "state": "success",
            "token": "repository-" + "r" * 24,
            "tokenSource": "keyring",
        }}]}}}}).encode(), b"")
    else:
        calls.append(list(command[2:]))
        if command[2:] == ("preflight",) and {preflight_fails!r}:
            result = module.CommandResult(78, b"", b'{{"error":{{"code":"PREFLIGHT_UNAVAILABLE"}}}}\\n')
        elif command[2:] == ("preflight",):
            result = module.CommandResult(0, {PREFLIGHT_OUTPUT!r}, b"")
        else:
            result = module.CommandResult(0, {task_output!r}, b"")
    return result

result = module.run_program(
    ["task", "start", "--issue", "https://example.test/issues/28"],
    home=Path({str(home)!r}),
    environment={{"LANG": "C.UTF-8"}},
    uid=os.getuid(),
    command_runner=runner,
    tools=tools,
)
Path({str(call_log)!r}).write_text(json.dumps(calls), encoding="utf-8")
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
"""


@pytest.mark.parametrize("preflight_fails", [False, True])
def test_fresh_isolated_process_covers_provider_preflight_and_task(
    tmp_path: Path,
    preflight_fails: bool,
) -> None:
    write_config(tmp_path)
    call_log = tmp_path / "calls.json"
    result = subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            isolated_harness_source(tmp_path, call_log, preflight_fails=preflight_fails),
        ],
        env={
            "PATH": "/attacker/bin",
            "PYTHONPATH": "/attacker/python",
            "PYTHONHOME": "/attacker/python-home",
            "GH_TOKEN": "ambient-must-not-win",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert call_log.exists(), result.stderr
    calls = json.loads(call_log.read_text(encoding="utf-8"))
    if preflight_fails:
        assert result.returncode == 78
        assert result.stdout == ""
        assert json.loads(result.stderr) == {"error": {"code": "PREFLIGHT_UNAVAILABLE"}}
        assert calls == [["preflight"]]
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["result"] == {
            "task_id": TASK_ID,
            "claim_id": CLAIM_ID,
            "branch": TASK_BRANCH,
            "worktree_ref": WORKTREE_REF,
        }
        assert calls == [
            ["preflight"],
            ["task", "start", "--issue", "https://example.test/issues/28"],
        ]
    assert "ambient-must-not-win" not in result.stdout + result.stderr


def test_global_task_guidance_uses_only_installed_host_launcher() -> None:
    guidance = (REPO / "claude-md" / "global-guidance.md").read_text(encoding="utf-8")
    task_rule = next(line for line in guidance.splitlines() if line.startswith("9. **Task 등록 권유"))

    assert '"$HOME/.local/bin/jhw-control-host" preflight' in task_rule
    assert '"$HOME/.local/bin/jhw-control-host" portfolio status' in task_rule
    assert '"$HOME/.local/bin/jhw-control-host" task start' in task_rule
    assert "control.env" not in task_rule
    assert "source" not in task_rule
    assert "credential" not in task_rule.lower()


def test_readme_documents_secure_store_only_provision_and_no_migration() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    for required in (
        "jhw-control-host",
        "secure-store-only",
        "GH_PROJECT_TOKEN",
        "NOTION_API_KEY",
        "tokenSource=keyring",
        "/usr/bin/python3 -I -m keyring --keyring-backend keyring.backends.SecretService.Keyring",
        "set jhw-control GH_PROJECT_TOKEN",
        "set jhw-control NOTION_API_KEY",
            "gh auth login --hostname github.com --git-protocol ssh --web",
        "Linux Secret Service",
        "현재 UID",
        "0500",
        "--contract",
        "preflight",
        "portfolio status",
        "task start",
    ):
        assert required in readme
    assert "자동 migration하지" in readme
    assert "설치 중 credential" in readme
