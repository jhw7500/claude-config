from __future__ import annotations

import json

import pytest

from codex_mcp_ownership import model, procfs
from helpers import write_proc_entry


def test_parse_stat_uses_last_closing_parenthesis():
    raw = (
        "321 (node worker) extra) S 77 88 88 0 -1 0 0 0 0 0 "
        "0 0 0 0 20 0 1 0 424242 0 0"
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
    observed = model.ObservedTime("2026-08-29T00:00:00+00:00", "test-boot-id", float("inf"))
    with pytest.raises(ValueError):
        model.ObservedTime.from_dict(observed.to_dict())


def test_session_and_managed_process_round_trip_with_schema_v1(fake_proc):
    identity = fake_proc.identity(321)
    assert identity is not None
    observed = model.ObservedTime("2026-08-29T00:00:00+00:00", identity.boot_id, 12.5)
    lease = model.SessionLease(
        1, "session", "/workspace", "SessionStart", (identity.stable_key(),), "active", observed
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
