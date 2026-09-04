from __future__ import annotations

from dataclasses import replace
import json

import pytest

from codex_mcp_ownership import model, procfs
from helpers import write_proc_entry


def test_parse_stat_uses_last_closing_parenthesis():
    raw = (
        "321 (node worker) extra) S 77 88 88 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 424242 0 0"
    )
    parsed = procfs.parse_stat(raw)
    assert parsed.pid == 321
    assert parsed.ppid == 77
    assert parsed.pgid == 88
    assert parsed.start_ticks == 424242


def test_parse_stat_rejects_malformed_data():
    with pytest.raises(procfs.ProcfsFormatError):
        procfs.parse_stat("321 (node) S not-a-pid")


@pytest.mark.parametrize("leading_pid", ("not-a-pid", "+321"))
def test_parse_stat_rejects_invalid_leading_pid(leading_pid):
    raw = f"{leading_pid} (node) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 42 0 0"
    with pytest.raises(procfs.ProcfsFormatError):
        procfs.parse_stat(raw)


def test_identity_changes_when_pid_is_reused(fake_proc):
    first = fake_proc.identity(321)
    assert first is not None
    fake_proc.write_start_ticks(321, first.start_ticks + 1)
    second = fake_proc.identity(321)
    assert second is not None
    assert first.stable_key() != second.stable_key()


def test_rss_rejects_reused_identity(fake_proc):
    identity = fake_proc.identity(321)
    assert identity is not None
    fake_proc.write_start_ticks(321, identity.start_ticks + 1)
    assert fake_proc.rss_kib(identity) is None


def test_identity_returns_none_for_missing_process(fake_proc):
    assert fake_proc.identity(999) is None


def test_observe_identity_distinguishes_live_and_confirmed_missing(fake_proc):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    live = tree.observe_identity(321)
    missing = tree.observe_identity(999)
    assert live.kind == "live"
    assert live.identity == tree.identity(321)
    assert missing.kind == "missing"
    assert missing.identity is None


@pytest.mark.parametrize("unreadable", ["boot", "stat", "exe"])
def test_observe_identity_reports_concrete_procfs_unreadability_as_unavailable(
    fake_proc,
    monkeypatch,
    unreadable,
):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    original_read = tree._read_text
    original_readlink = procfs.os.readlink

    def read_text(path):
        if unreadable == "boot" and path == fake_proc.boot_id_path:
            raise PermissionError("unreadable boot id")
        if unreadable == "stat" and path == fake_proc.root / "321" / "stat":
            raise PermissionError("unreadable stat")
        return original_read(path)

    def readlink(path):
        if unreadable == "exe" and path == fake_proc.root / "321" / "exe":
            raise PermissionError("unreadable exe")
        return original_readlink(path)

    monkeypatch.setattr(tree, "_read_text", read_text)
    monkeypatch.setattr(procfs.os, "readlink", readlink)
    observation = tree.observe_identity(321)
    assert observation.kind == "unavailable"
    assert observation.identity is None


def test_observe_identity_reports_indeterminate_stat_race_as_unavailable(
    fake_proc,
    monkeypatch,
):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    original_read = tree._read_text
    stat_reads = 0

    def read_text(path):
        nonlocal stat_reads
        text = original_read(path)
        if path == fake_proc.root / "321" / "stat":
            stat_reads += 1
            if stat_reads == 1:
                fake_proc.write_start_ticks(321, 999999)
        return text

    monkeypatch.setattr(tree, "_read_text", read_text)
    assert tree.observe_identity(321).kind == "unavailable"


def test_identity_rejects_stat_with_a_different_leading_pid(fake_proc):
    (fake_proc.root / "321" / "stat").write_text(
        "999 (node) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 424242 0 0\n",
        encoding="utf-8",
    )
    assert fake_proc.identity(321) is None


def test_identity_rejects_start_ticks_changed_during_read(fake_proc, monkeypatch):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    original_read = tree._read_text
    stat_reads = 0

    def read_text(path):
        nonlocal stat_reads
        text = original_read(path)
        if path.name == "stat":
            stat_reads += 1
            if stat_reads == 1:
                fake_proc.write_start_ticks(321, 999999)
        return text

    monkeypatch.setattr(tree, "_read_text", read_text)
    assert tree.identity(321) is None


def test_ancestor_chain_stops_at_missing_parent(fake_proc):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    chain = tree.ancestor_chain(321)
    assert [identity.pid for identity in chain] == [321]


def test_ancestor_chain_stops_on_cycle(fake_proc):
    write_proc_entry(
        fake_proc.root,
        1,
        "1 (init) S 321 1 1 0 -1 0 0 0 0 0 0 0 0 20 0 1 0 1 0 0\n",
        fake_proc.exe,
    )
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    assert [identity.pid for identity in tree.ancestor_chain(321)] == [321, 1]


def test_identity_changes_when_executable_inode_changes(fake_proc):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    first = tree.identity(321)
    assert first is not None
    replacement = fake_proc.root / "replacement-node"
    replacement.write_text("replacement", encoding="utf-8")
    exe_link = fake_proc.root / "321" / "exe"
    exe_link.unlink()
    exe_link.symlink_to(replacement)
    second = tree.identity(321)
    assert second is not None
    assert first.stable_key() != second.stable_key()


def test_identity_includes_boot_id(fake_proc):
    first = fake_proc.identity(321)
    assert first is not None
    fake_proc.boot_id_path.write_text("next-boot\n", encoding="utf-8")
    second = fake_proc.identity(321)
    assert second is not None
    assert first.stable_key() != second.stable_key()


def test_current_boot_id_is_exposed_without_process_lookup(fake_proc):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    assert tree.boot_id() == "test-boot-id"


def test_group_members_ignores_non_numeric_entries_and_returns_live_members(fake_proc):
    write_proc_entry(
        fake_proc.root,
        322,
        "322 (node) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 20 0 1 0 22 0 0\n",
        fake_proc.exe,
    )
    (fake_proc.root / "self").mkdir()
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    assert [identity.pid for identity in tree.group_members(321)] == [321, 322]


def test_strict_group_observation_reports_unavailable_enumeration(
    fake_proc,
    monkeypatch,
):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    original_iterdir = procfs.Path.iterdir

    def unavailable_iterdir(path):
        if path == fake_proc.root:
            raise PermissionError("enumeration unavailable")
        return original_iterdir(path)

    monkeypatch.setattr(procfs.Path, "iterdir", unavailable_iterdir)

    observation = tree.observe_group_members(321)

    assert observation.kind == "unavailable"
    assert observation.members == ()
    assert observation.unavailable_pids == ()


def test_strict_group_observation_reports_partial_exact_member(
    fake_proc,
    monkeypatch,
):
    write_proc_entry(
        fake_proc.root,
        322,
        "322 (node) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 22 0 0\n",
        fake_proc.exe,
    )
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    original_observe = tree.observe_identity

    def unavailable_member(pid):
        if pid == 322:
            return procfs.IdentityObservation("unavailable", None)
        return original_observe(pid)

    monkeypatch.setattr(tree, "observe_identity", unavailable_member)

    observation = tree.observe_group_members(321)

    assert observation.kind == "partial"
    assert [identity.pid for identity in observation.members] == [321]
    assert observation.unavailable_pids == (322,)
    assert [identity.pid for identity in tree.group_members(321)] == [321, 322]


def test_strict_group_observation_keeps_same_identity_migrated_during_scan(
    fake_proc,
    monkeypatch,
):
    write_proc_entry(
        fake_proc.root,
        322,
        "322 (node) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 22 0 0\n",
        fake_proc.exe,
    )
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    original_observe = tree.observe_identity
    candidate = original_observe(322).identity
    assert candidate is not None

    def migrate_before_exact_observation(pid):
        if pid == 322:
            write_proc_entry(
                fake_proc.root,
                322,
                "322 (node) S 1 999 999 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 22 0 0\n",
                fake_proc.exe,
            )
        return original_observe(pid)

    monkeypatch.setattr(tree, "observe_identity", migrate_before_exact_observation)

    observation = tree.observe_group_members(321)

    migrated = next(member for member in observation.members if member.pid == 322)
    assert observation.kind == "complete"
    assert (migrated.pid, migrated.start_ticks) == (
        candidate.pid,
        candidate.start_ticks,
    )
    assert migrated.pgid == 999


@pytest.mark.parametrize(
    ("field", "replacement_value"),
    (("pid", 999), ("start_ticks", 23)),
)
def test_strict_group_observation_retries_unbound_exact_identity(
    fake_proc,
    monkeypatch,
    field,
    replacement_value,
):
    write_proc_entry(
        fake_proc.root,
        322,
        "322 (node) S 1 321 321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 22 0 0\n",
        fake_proc.exe,
    )
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    original_observe = tree.observe_identity
    exact = original_observe(322).identity
    assert exact is not None

    def replaced_before_exact_observation(pid):
        if pid == 322:
            return procfs.IdentityObservation(
                "live",
                replace(exact, **{field: replacement_value}),
            )
        return original_observe(pid)

    monkeypatch.setattr(tree, "observe_identity", replaced_before_exact_observation)

    observation = tree.observe_group_members(321)

    assert observation.kind == "partial"
    assert [identity.pid for identity in observation.members] == [321]
    assert observation.unavailable_pids == (322,)


def test_rss_kib_reads_only_vmrss_and_revalidates_identity(fake_proc):
    identity = fake_proc.identity(321)
    assert identity is not None
    status = fake_proc.root / "321" / "status"
    status.write_text("VmSize:\t999 kB\nVmRSS:\t128 kB\n", encoding="utf-8")
    assert fake_proc.rss_kib(identity) == 128


def test_process_identity_serialization_is_strict_and_uses_canonical_key(fake_proc):
    identity = fake_proc.identity(321)
    assert identity is not None
    expected = json.dumps(
        {
            "boot_id": identity.boot_id,
            "exe_dev": identity.exe_dev,
            "exe_ino": identity.exe_ino,
            "pid": identity.pid,
            "start_ticks": identity.start_ticks,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert identity.stable_key() != expected
    assert model.ProcessIdentity.from_dict(identity.to_dict()) == identity
    with pytest.raises(ValueError):
        model.ProcessIdentity.from_dict({**identity.to_dict(), "extra": "no"})


def test_models_reject_boolean_integers_and_non_finite_floats(fake_proc):
    identity = fake_proc.identity(321)
    assert identity is not None
    bad_identity = {**identity.to_dict(), "pid": True}
    with pytest.raises(ValueError):
        model.ProcessIdentity.from_dict(bad_identity)
    observed = model.ObservedTime(
        "2026-08-29T00:00:00+00:00", "test-boot-id", float("inf")
    )
    with pytest.raises(ValueError):
        model.ObservedTime.from_dict(observed.to_dict())


def test_session_and_managed_process_round_trip_with_schema_v1(fake_proc):
    identity = fake_proc.identity(321)
    assert identity is not None
    observed = model.ObservedTime("2026-08-29T00:00:00+00:00", identity.boot_id, 12.5)
    lease = model.SessionLease(
        1,
        "session",
        "/workspace",
        "SessionStart",
        (identity.stable_key(),),
        "active",
        observed,
    )
    assert model.SessionLease.from_dict(lease.to_dict()) == lease
    managed = model.ManagedProcess(
        1,
        "record",
        "user",
        "server",
        "/workspace",
        identity,
        None,
        (identity,),
        identity.pgid,
        frozenset({identity.stable_key()}),
        observed,
    )
    assert model.ManagedProcess.from_dict(managed.to_dict()) == managed
    with pytest.raises(ValueError):
        model.SessionLease.from_dict({**lease.to_dict(), "schema_version": 2})


def test_managed_process_term_sent_keys_are_a_strict_immutable_stable_key_set(
    fake_proc,
):
    identity = fake_proc.identity(321)
    assert identity is not None
    observed = model.ObservedTime("2026-08-29T00:00:00+00:00", identity.boot_id, 12.5)
    managed = model.ManagedProcess(
        schema_version=1,
        record_id="record",
        scope="user",
        server="server",
        cwd="/workspace",
        wrapper=identity,
        child=None,
        members=(identity,),
        pgid=identity.pgid,
        host_keys=frozenset(),
        spawned=observed,
        term_sent_boot=20.0,
        term_sent_keys=frozenset({identity.stable_key()}),
    )

    payload = managed.to_dict()
    assert payload["term_sent_keys"] == [identity.stable_key()]
    assert model.ManagedProcess.from_dict(payload) == managed
    missing = dict(payload)
    missing.pop("term_sent_keys")
    with pytest.raises(ValueError):
        model.ManagedProcess.from_dict(missing)

    invalid_values = (
        (identity.stable_key(),),
        [identity.stable_key(), identity.stable_key()],
        ["not-a-stable-key"],
        ["f" * 64, "0" * 64],
    )
    for invalid in invalid_values:
        with pytest.raises(ValueError):
            model.ManagedProcess.from_dict({**payload, "term_sent_keys": invalid})


def test_open_pidfd_returns_revalidated_descriptor(fake_proc, monkeypatch):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    identity = tree.identity(321)
    assert identity is not None
    calls = []

    def pidfd_open(pid, flags):
        calls.append((pid, flags))
        return 91

    monkeypatch.setattr(procfs.os, "pidfd_open", pidfd_open)
    assert tree.open_pidfd(identity) == 91
    assert calls == [(321, 0)]


def test_open_pidfd_closes_on_post_open_identity_mismatch(fake_proc, monkeypatch):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    identity = tree.identity(321)
    assert identity is not None
    observations = iter((identity, None))
    closed = []
    monkeypatch.setattr(tree, "identity", lambda pid: next(observations))
    monkeypatch.setattr(procfs.os, "pidfd_open", lambda pid, flags: 91)
    monkeypatch.setattr(procfs.os, "close", closed.append)

    with pytest.raises(ProcessLookupError) as error:
        tree.open_pidfd(identity)

    assert error.value.errno == procfs.errno.ESRCH
    assert closed == [91]


def test_open_pidfd_reports_enosys_when_unavailable(fake_proc, monkeypatch):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    identity = tree.identity(321)
    assert identity is not None
    monkeypatch.delattr(procfs.os, "pidfd_open", raising=False)

    with pytest.raises(OSError) as error:
        tree.open_pidfd(identity)

    assert error.value.errno == procfs.errno.ENOSYS


def test_open_pidfd_closes_once_when_post_open_parse_fails(fake_proc, monkeypatch):
    tree = procfs.LinuxProcfs(fake_proc.root, fake_proc.boot_id_path)
    identity = tree.identity(321)
    assert identity is not None
    observations = iter((identity, procfs.ProcfsFormatError("bad stat")))
    closed = []

    def read_identity(pid):
        result = next(observations)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tree, "identity", read_identity)
    monkeypatch.setattr(procfs.os, "pidfd_open", lambda pid, flags: 91)
    monkeypatch.setattr(procfs.os, "close", closed.append)

    with pytest.raises(procfs.ProcfsFormatError):
        tree.open_pidfd(identity)

    assert closed == [91]


def test_open_pidfd_survives_the_parent_exiting() -> None:
    """A reparented process is still the same process.

    When a leader exits the kernel reparents its children to init, changing
    only ``ppid``. Comparing whole identities called that live process gone —
    and orphaned descendants are exactly what this supervisor exists to reap,
    so the check failed precisely when it was needed.
    """
    import os
    import subprocess
    import sys
    import time

    descendant = "import sys,time;sys.stdout.write('x');sys.stdout.flush();time.sleep(30)"
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys,time;"
            f"c=subprocess.Popen([sys.executable,'-c',{descendant!r}],stdout=subprocess.PIPE);"
            "sys.stdout.write(str(c.pid)+'\\n');sys.stdout.flush();time.sleep(0.2)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert leader.stdout is not None
        descendant_pid = int(leader.stdout.readline().strip())
        live = procfs.LinuxProcfs()
        captured = live.identity(descendant_pid)
        assert captured is not None

        leader.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = live.identity(descendant_pid)
            if current is not None and current.ppid != captured.ppid:
                break
            time.sleep(0.01)

        reparented = live.identity(descendant_pid)
        assert reparented is not None
        assert reparented != captured, "parent never exited; test proves nothing"
        assert reparented.start_ticks == captured.start_ticks

        pidfd = live.open_pidfd(captured)
        os.close(pidfd)
    finally:
        try:
            os.kill(descendant_pid, 15)
        except (OSError, NameError, UnboundLocalError):
            pass
        if leader.stdout is not None:
            leader.stdout.close()


def test_same_process_still_rejects_a_reused_pid(fake_proc) -> None:
    """Loosening the comparison must not loosen pid-reuse protection."""
    identity = fake_proc.identity(321)
    assert identity is not None
    recycled = replace(identity, start_ticks=identity.start_ticks + 1)
    assert not identity.same_process(recycled)
    assert not identity.same_process(None)
    assert identity.same_process(replace(identity, ppid=1))
