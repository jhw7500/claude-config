# jhw-control-host Secret Service Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make headless and tmux launcher runs bind to one unchanged Secret Service owner on the canonical user bus without adding a file backend or automatic daemon lifecycle.

**Architecture:** Reuse the isolated GNOME unlock helper as a noninteractive, `NO_AUTO_START` state probe. Probe the canonical bus before and after both credential providers, require the same unique owner and an unlocked login collection, and stop before any `jhw-control` child when the state is missing, locked, unsupported, or replaced.

**Tech Stack:** Python 3 standard library, PyGObject/Gio, D-Bus Secret Service, Python keyring/SecretStorage, GitHub CLI, pytest

**Spec:** `docs/superpowers/specs/2026-08-29-jhw-control-host-secret-service-stability-design.md`

## Global Constraints

- Credential policy remains exactly `secure-store-only`; do not add plaintext, encrypted-file, or implicit fallback backends.
- Contract remains version `4` with the existing `13` command families.
- Canonical runtime and bus remain exactly `/run/user/<uid>` and `/run/user/<uid>/bus`; ambient D-Bus coordinates are ignored.
- A reboot may require exactly one operator-initiated `jhw-control-host unlock` from an interactive terminal.
- Probes must use `Gio.DBusCallFlags.NO_AUTO_START` and must not start, stop, replace, or kill a daemon.
- No credential value may enter argv, parent environment, files, logs, public JSON, migration artifacts, or documentation.
- Provider and control output limits remain `64 KiB` and `12 KiB`; unlock/probe helper output remains at most `1024` bytes.
- Any provider-generation owner change must stop before the first `jhw-control` child.

## File Structure

```text
scripts/jhw-control-host.py
    Embedded probe/unlock helper, bounded probe adapter, and owner-stability gate.

tests/test_jhw_control_host.py
    Embedded-helper, parent mapping, call-order, canonical-bus, endpoint, and leak regressions.

README.md
    Short operator policy, reboot recovery, headless verification, migration, rollback, and rollout link.

docs/security/jhw-control-host-secret-service-operations.md
    Detailed non-secret operator runbook and decision matrix.

docs/superpowers/specs/2026-08-29-jhw-control-host-secret-service-stability-design.md
    Approved architecture and threat model; implementation does not rewrite its decision.
```

---

### Task 1: Add a non-starting embedded Secret Service probe

**Files:**
- Modify: `scripts/jhw-control-host.py:274-519`
- Test: `tests/test_jhw_control_host.py:123-172`
- Test: `tests/test_jhw_control_host.py:800-1160`

**Interfaces:**
- Consumes: canonical bus connection from `open_connection()` and existing `call()`, `validate_private_contract()`, and `collection_locked()` helper functions.
- Produces: `inspect_credential_store(connection) -> tuple[str, str]`, where the second value is exactly `locked|unlocked`; embedded `main(..., mode=None)` accepts internal mode `probe` and emits exactly `{"owner":<unique-name>,"status":<state>}`.

- [ ] **Step 1: Write failing probe-state tests**

Extend the existing `unlock_helper_namespace` tests with an unlocked connection and require the probe to avoid password and mutation methods:

```python
def test_credential_store_probe_reports_owner_and_lock_state_without_prompt(
    unlock_helper_namespace: dict[str, object],
) -> None:
    namespace = unlock_helper_namespace
    inspect = namespace["inspect_credential_store"]
    GLib = namespace["GLib"]

    class Connection:
        def __init__(self) -> None:
            self.methods: list[str] = []

        def call_sync(self, _destination, _path, _interface, method,
                      _parameters, _reply_type, flags, _timeout, _cancellable):
            self.methods.append(method)
            assert flags == namespace["NO_AUTO_START"]
            return {
                "GetNameOwner": GLib.Variant("(s)", (":1.44",)),
                "Introspect": GLib.Variant("(s)", (UNLOCK_PRIVATE_XML,)),
                "ReadAlias": GLib.Variant(
                    "(o)", ("/org/freedesktop/secrets/collection/login",)
                ),
                "Get": GLib.Variant("(v)", (GLib.Variant("b", False),)),
            }[method]

    connection = Connection()
    assert inspect(connection) == (":1.44", "unlocked")
    assert connection.methods == ["GetNameOwner", "Introspect", "ReadAlias", "Get"]
```

Add a `main(mode="probe")` test that asserts exact JSON, connection close, and no TTY read. Add a missing-owner test where `GetNameOwner` raises and assert helper return code `22` with empty output.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'credential_store_probe'
```

Expected: FAIL because `inspect_credential_store` and probe mode do not exist.

- [ ] **Step 3: Refactor the embedded helper around one inspection function**

Add `import json` inside `UNLOCK_HELPER`, then implement this shape:

```python
def inspect_credential_store(connection):
    owner = call(
        connection,
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "GetNameOwner",
        GLib.Variant("(s)", (SERVICE_NAME,)),
        "(s)",
    ).unpack()[0]
    if not isinstance(owner, str) or not owner.startswith(":"):
        raise UnlockFailure(22)
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
    state = "locked" if collection_locked(connection, owner, collection) else "unlocked"
    return owner, state
```

Make `unlock_credential_store()` call this function first. Return `already-unlocked` when state is `unlocked`; otherwise retain the existing one-shot password, private method, post-unlock verification, wipe, and session-close behavior.

Extend embedded `main()` without changing the public unlock output:

```python
def main(connection_factory=None, password_reader=None, output=None, mode=None):
    selected_mode = mode
    if selected_mode is None:
        if sys.argv[1:] == []:
            selected_mode = "unlock"
        elif sys.argv[1:] == ["probe"]:
            selected_mode = "probe"
        else:
            return 26
    if selected_mode not in {"probe", "unlock"}:
        return 26
    # Open one canonical connection and close it in the existing finally block.
    if selected_mode == "probe":
        owner, status = inspect_credential_store(connection)
        selected_output.write(json.dumps({"owner": owner, "status": status}) + "\\n")
    else:
        status = unlock_credential_store(connection, selected_reader)
        selected_output.write(json.dumps({"status": status}) + "\\n")
```

Keep the existing `UnlockFailure` mappings and ensure broad connection exceptions return `22` for probe availability failures rather than a traceback.

- [ ] **Step 4: Run all embedded unlock/probe tests**

Run:

```bash
rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'unlock_helper or credential_store_probe'
```

Expected: PASS; existing password wipe, TTY restore, unsupported-interface, and already-unlocked tests remain green.

- [ ] **Step 5: Commit the helper refactor**

```bash
rtk git add scripts/jhw-control-host.py tests/test_jhw_control_host.py
rtk git commit -m "feat: probe canonical Secret Service state"
```

---

### Task 2: Gate providers on one stable owner generation

**Files:**
- Modify: `scripts/jhw-control-host.py:34-40`
- Modify: `scripts/jhw-control-host.py:875-940`
- Modify: `scripts/jhw-control-host.py:1707-1788`
- Test: `tests/test_jhw_control_host.py:508-632`
- Test: `tests/test_jhw_control_host.py:692-790`
- Test: `tests/test_jhw_control_host.py:1150-1275`
- Test: `tests/test_jhw_control_host.py:1430-1570`

**Interfaces:**
- Consumes: embedded helper invocation `(python, "-I", "-c", UNLOCK_HELPER, "probe")` and provider environment with canonical bus coordinates.
- Produces: `_probe_credential_store(runner, python, env) -> str`, returning only a validated D-Bus unique owner; new stable public error `OS_CREDENTIAL_STORE_CHANGED` when the second owner differs.

- [ ] **Step 1: Teach the fake runner about two probe snapshots**

Add two default snapshots and route the exact five-argument helper invocation separately from public unlock and keyring:

```python
self.probe_results = [
    launcher.CommandResult(0, b'{"owner":":1.44","status":"unlocked"}\n', b""),
    launcher.CommandResult(0, b'{"owner":":1.44","status":"unlocked"}\n', b""),
]
self.probe_index = 0
```

```python
elif command == (
    self.tools.python, "-I", "-c", self.launcher.UNLOCK_HELPER, "probe"
):
    index = min(self.probe_index, len(self.probe_results) - 1)
    result = self.probe_results[index]
    self.probe_index += 1
```

Keep public unlock matched only by the four-argument command. Keep keyring matched by exact `KEYRING_HELPER`, not by any Python command.

- [ ] **Step 2: Write failing parent-gate tests**

Add tests for locked, owner replacement, malformed output, and helper diagnostics:

```python
def test_owner_change_discards_credentials_before_control(
    launcher: ModuleType, tmp_path: Path
) -> None:
    runner = FakeCommandRunner(launcher)
    runner.probe_results[1] = launcher.CommandResult(
        0, b'{"owner":":1.99","status":"unlocked"}\n', b""
    )

    result = run_secure(launcher, tmp_path, ["preflight"], runner)

    assert result.returncode == 78
    assert json.loads(result.stderr) == {
        "error": {
            "action": "restore one user Secret Service session and rerun jhw-control-host preflight",
            "code": "OS_CREDENTIAL_STORE_CHANGED",
        }
    }
    assert not any(call["argv"][:2] == (runner.tools.node, runner.tools.control)
                   for call in runner.calls)
    assert PROJECT_TOKEN.encode() not in result.stdout + result.stderr
    assert REPOSITORY_TOKEN.encode() not in result.stdout + result.stderr
    assert NOTION_TOKEN.encode() not in result.stdout + result.stderr
```

For the locked case, use `{"owner":":1.44","status":"locked"}` and assert only the first probe runs, result code is `OS_CREDENTIAL_STORE_LOCKED`, action is `jhw-control-host unlock`, and neither keyring, GitHub CLI, nor control runs. Feed duplicate JSON keys, an invalid owner, extra fields, nonempty stderr, and a helper secret canary; assert one fixed path-free error and no copied helper output.

- [ ] **Step 3: Run the new parent tests and verify they fail**

Run:

```bash
rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'owner_change or probe_output or probe_locked'
```

Expected: FAIL because the parent has no probe adapter or generation gate.

- [ ] **Step 4: Implement bounded probe parsing and error mapping**

Add a strict unique-name pattern near the other constants:

```python
DBUS_UNIQUE_NAME_RE = re.compile(r"^:[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$")
```

Implement `_probe_credential_store()` next to `_unlock_credential_store()` using
`PROVIDER_TIMEOUT_SECONDS`, `max_output_bytes=1024`, and `tty_input=False`. Parse with
`_parse_json()`, require exact keys `owner` and `status`, reject nonempty stderr, validate owner with
`DBUS_UNIQUE_NAME_RE`, and map status exactly:

```python
if status == "locked":
    raise LauncherError("OS_CREDENTIAL_STORE_LOCKED", action="jhw-control-host unlock")
if status != "unlocked":
    raise LauncherError("OS_CREDENTIAL_STORE_UNAVAILABLE")
return owner
```

Map helper return code `20` to the existing `KEYRING_RUNTIME_UNAVAILABLE`, `22` to
`OS_CREDENTIAL_STORE_UNAVAILABLE`, `23` to `OS_CREDENTIAL_STORE_UNLOCK_UNSUPPORTED`, and all
timeouts/start/output/malformed failures to a fixed credential-store error without copying child output.

- [ ] **Step 5: Put the two probes around both providers**

In `run_program()` keep public `unlock` as the early branch. For every other allowed command:

```python
provider_env = _provider_environment(selected_home, source_environment, uid=selected_uid)
keyring_env = _keyring_environment(selected_home, source_environment, uid=selected_uid)
initial_owner = _probe_credential_store(
    runner, selected_tools.python, provider_env
)
project, notion = _load_keyring_credentials(runner, selected_tools, keyring_env)
repository = _load_repository_credential(
    runner, selected_tools, provider_env, owner=config["JHW_GITHUB_OWNER"]
)
final_owner = _probe_credential_store(
    runner, selected_tools.python, provider_env
)
if initial_owner != final_owner:
    raise LauncherError(
        "OS_CREDENTIAL_STORE_CHANGED",
        action="restore one user Secret Service session and rerun jhw-control-host preflight",
    )
```

Perform the existing project/repository separation check and build the credential child environment only
after owner equality succeeds.

- [ ] **Step 6: Update call-order assertions and run launcher tests**

Update tests to expect `probe → keyring → gh → probe → control`. Provider environment assertions apply to
the first four calls; only keyring carries `PYTHON_KEYRING_BACKEND`; no provider carries ambient credential or
preload variables. Control call assertions start at index `4`.

Run:

```bash
rtk python3 -m pytest -q tests/test_jhw_control_host.py
```

Expected: PASS.

- [ ] **Step 7: Commit the generation gate**

```bash
rtk git add scripts/jhw-control-host.py tests/test_jhw_control_host.py
rtk git commit -m "feat: pin credential reads to one Secret Service owner"
```

---

### Task 3: Complete canonical endpoint and headless regressions

**Files:**
- Modify: `tests/test_jhw_control_host.py:692-800`
- Modify: `tests/test_jhw_control_host.py:1279-1335`
- Modify only if a regression exposes a gap: `scripts/jhw-control-host.py:775-813`

**Interfaces:**
- Consumes: `_validated_session_bus(runtime: Path, *, uid: int) -> Path` and `_session_bus_environment(*, uid: int) -> dict[str, str]`.
- Produces: regression coverage for owner, mode, ACL, symlink, socket type, alternate ambient bus, clean environment, and provider/probe canonicalization.

- [ ] **Step 1: Write alternate-bus and clean-environment tests**

Run preflight with tmux-equivalent poisoned coordinates:

```python
result = launcher.run_program(
    ["preflight"],
    home=tmp_path,
    environment={
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": "/tmp/tmux-runtime",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/tmux-runtime/bus",
    },
    uid=os.getuid(),
    command_runner=runner,
    tools=runner.tools,
)
assert result.returncode == 0
for call in runner.calls[:4]:
    assert call["env"]["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
    assert call["env"]["DBUS_SESSION_BUS_ADDRESS"] == (
        f"unix:path=/run/user/{os.getuid()}/bus"
    )
```

Retain the fake-runner isolation test proving unit tests do not inspect the real host bus.

- [ ] **Step 2: Write owner and ACL endpoint tests**

Use `monkeypatch` around `Path.lstat` to return a copied stat object with `st_uid = os.getuid() + 1` for the
runtime or bus target, and assert `OS_CREDENTIAL_STORE_UNAVAILABLE`. Separately monkeypatch
`_has_extended_posix_acl` to return true only for the runtime directory and only for the bus socket. Keep the
existing mode, runtime symlink, bus symlink, regular-file, and valid private socket cases.

- [ ] **Step 3: Run endpoint tests and make the smallest necessary implementation fix**

Run:

```bash
rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'session_bus or canonical_bus or alternate_bus'
```

Expected: PASS with the current validator. If one owner/ACL assertion fails, change only
`_validated_session_bus()` so both runtime and socket require current UID, runtime mode has no group/world bits,
both reject extended ACLs, the runtime is a real directory, and bus is a real UNIX socket.

- [ ] **Step 4: Commit the endpoint regressions**

```bash
rtk git add scripts/jhw-control-host.py tests/test_jhw_control_host.py
rtk git commit -m "test: cover headless Secret Service endpoints"
```

---

### Task 4: Publish the operator decision and recovery runbook

**Files:**
- Create: `docs/security/jhw-control-host-secret-service-operations.md`
- Modify: `README.md:55-151`

**Interfaces:**
- Consumes: the approved design decision and stable runtime errors from Tasks 1-3.
- Produces: one operator path for reboot, clean shell, tmux, recovery, rollback, and future-backend admission; README links to the detailed runbook.

- [ ] **Step 1: Preserve the executable README contract baseline**

Do not add tests that grep new human prose. The loaded TDD guidance requires behavior tests for executable
artifacts and says human prose earns no source-text assertion. Run the existing contract-boundary tests before
and after the documentation change instead:

```bash
rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'readme or operator_contract'
```

Expected: PASS; command inventory, projection, and security-boundary assertions remain unchanged.

- [ ] **Step 2: Create the detailed operations document**

Write the following bounded sections with no credential values:

1. Decision: Secret Service remains the only Project/Notion backend and GitHub CLI keyring remains the repo backend.
2. Bootstrap: optional operator-run `loginctl enable-linger "$(id -un)"`, systemd user manager/service inspection, one interactive `jhw-control-host unlock` after reboot.
3. Clean verification using exactly:

```bash
env -i HOME="$HOME" LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin \
  "$HOME/.local/bin/jhw-control-host" --contract
env -i HOME="$HOME" LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin \
  "$HOME/.local/bin/jhw-control-host" preflight
```

4. tmux verification: start a new tmux session and run the same `env -i ... preflight` command; success must not depend on pane-exported D-Bus variables.
5. Error table for locked, unavailable, unsupported, and changed owner.
6. Recovery: repair one user-session owner, unlock once, repeat both preflights; never run per-pane `dbus-run-session`, spawn an extra daemon, kill by PID, or edit Registry state.
7. Migration and rollback: reinstall launcher only; do not export/import credentials; prior launcher rollback uses the same store.
8. Future provider gate: separate Issue must define bootstrap secret, least privilege, rotation, revoke, non-leaking migration, rollback, and outage behavior.

- [ ] **Step 3: Update the README operator contract**

Add a concise paragraph after the existing unlock description: probes use `NO_AUTO_START`; all non-unlock commands require the same unlocked owner before and after provider reads; process count is not authority; changed owner stops before control. Add the clean-shell commands, tmux note, reboot unlock policy, no file fallback, rollout order, and link to the operations document. Do not change contract version or inventory.

- [ ] **Step 4: Run documentation and contract tests**

Run:

```bash
rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'contract or readme or operator'
```

Expected: PASS.

- [ ] **Step 5: Commit the operator contract**

```bash
rtk git add README.md docs/security/jhw-control-host-secret-service-operations.md
rtk git commit -m "docs: define Secret Service recovery contract"
```

---

### Task 5: Validate the complete stability change

**Files:**
- Verify: `scripts/jhw-control-host.py`
- Verify: `tests/test_jhw_control_host.py`
- Verify: `tests/test_installer_private_config.py`
- Verify: `README.md`
- Verify: `docs/security/jhw-control-host-secret-service-operations.md`
- Verify: `docs/superpowers/specs/2026-08-29-jhw-control-host-secret-service-stability-design.md`

**Interfaces:**
- Consumes: all prior task deliverables.
- Produces: compile, focused security, installer, full-suite, and whitespace evidence suitable for Project Control completion readiness.

- [ ] **Step 1: Compile the launcher**

```bash
rtk python3 -m py_compile scripts/jhw-control-host.py
```

Expected: exit `0` with no output.

- [ ] **Step 2: Run focused credential-store security tests**

```bash
rtk python3 -m pytest -q tests/test_jhw_control_host.py -k 'credential_store or keyring or unlock or session_bus or provider'
```

Expected: all selected tests pass.

- [ ] **Step 3: Run the launcher and installer suites**

```bash
rtk python3 -m pytest -q tests/test_jhw_control_host.py tests/test_installer_private_config.py
```

Expected: all tests pass.

- [ ] **Step 4: Run the repository test suite**

```bash
rtk python3 -m pytest -q
```

Expected: all tests pass with no unexpected skips or warnings introduced by this change.

- [ ] **Step 5: Check the final diff**

```bash
rtk git diff --check
rtk git status --short
```

Expected: `git diff --check` exits `0`; status lists only the intended issue #68 files before the final commit.

- [ ] **Step 6: Commit the approved design and plan if still uncommitted**

```bash
rtk git add docs/superpowers/specs/2026-08-29-jhw-control-host-secret-service-stability-design.md docs/superpowers/plans/2026-08-29-jhw-control-host-secret-service-stability.md
rtk git commit -m "docs: record Secret Service stability design"
```

- [ ] **Step 7: Record exact validation evidence**

Capture only command names, exit status, and pass counts for the Task completion-ready call. Do not include credential output, D-Bus unique owner, runtime path, process ID, or private configured paths.

## Validation Evidence (2026-08-29)

- `python3 -m py_compile scripts/jhw-control-host.py`: exit `0`.
- focused credential-store pytest selection: exit `0`, `75 passed`, `252 deselected`.
- launcher and installer pytest suites: exit `0`, `342 passed`.
- full repository pytest suite with the declared Slack bridge requirement loaded from a disposable target: exit `0`, `1289 passed`.
- `git diff --check`: exit `0`.
- TDD probe cycle: `3 failed` before helper implementation, then `3 passed`; combined unlock/probe regression: `10 passed`.
- TDD owner-gate cycle: `11 failed` before parent integration, then `11 passed`; launcher suite after integration: `323 passed`.
- canonical endpoint/headless regression selection: `12 passed`.
- The disposable dependency target was removed after the full-suite run; no host package or repository dependency state was changed.
