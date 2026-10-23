import hashlib
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from commit_gate import cli  # noqa: E402
from commit_gate import grant  # noqa: E402

HEAD = "a" * 40
MSG = hashlib.sha256(b"feat: x\n").hexdigest()
NOW = 1_000_000.0


def binding(**over):
    values = dict(repo_root=Path("/work/repo"), head=HEAD, message_sha256=MSG, runtime="claude")
    values.update(over)
    return grant.make_binding(**values)


def record(home, b=None, now=NOW, channel="relayed"):
    return grant.record_grant(
        home, binding=b or binding(), reason="user said commit", channel=channel, now=now
    )


def test_grant_allows_exactly_one_consume(tmp_path):
    record(tmp_path)
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW + 1) is True


def test_grant_is_consumed_once(tmp_path):
    record(tmp_path)
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW + 1) is True
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW + 2) is False


def test_grant_binds_message_hash(tmp_path):
    record(tmp_path)
    other = binding(message_sha256=hashlib.sha256(b"fix: y\n").hexdigest())
    assert grant.consume_grant(tmp_path, binding=other, now=NOW + 1) is False
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW + 1) is True


def test_grant_binds_head_and_runtime(tmp_path):
    record(tmp_path)
    assert grant.consume_grant(tmp_path, binding=binding(head="b" * 40), now=NOW) is False
    assert grant.consume_grant(tmp_path, binding=binding(runtime="codex"), now=NOW) is False
    assert grant.consume_grant(tmp_path, binding=binding(repo_root=Path("/work/other")), now=NOW) is False
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW) is True


def test_unborn_head_binding():
    assert binding(head="")["head"] == "UNBORN"
    assert binding(head=None)["head"] == "UNBORN"


def test_grant_expires(tmp_path):
    path = record(tmp_path)
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW + 601) is False
    assert not path.exists()


def test_grant_valid_at_ttl_boundary(tmp_path):
    record(tmp_path)
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW + 600) is True


def test_concurrent_consumers_one_winner(tmp_path):
    record(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _i: grant.consume_grant(tmp_path, binding=binding(), now=NOW + 1), range(8))
        )
    assert results.count(True) == 1
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW + 1) is False


def test_state_dir_and_file_modes(tmp_path):
    path = record(tmp_path)
    directory = grant.state_dir(tmp_path)
    assert directory == tmp_path / ".local/state/claude-config/commit-gate"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.parent == directory
    expected = hashlib.sha256(
        json.dumps(binding(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert path.name == expected


def test_reason_is_truncated_and_payload_bounded(tmp_path):
    path = grant.record_grant(
        tmp_path, binding=binding(), reason="가" * 2000, channel="tty", now=NOW
    )
    data = json.loads(path.read_text())
    assert len(data["reason"]) == 512
    assert data["channel"] == "tty" and data["created_at"] == NOW
    assert path.stat().st_size <= 16 * 1024


def test_record_rejects_bad_channel_and_binding(tmp_path):
    with pytest.raises(ValueError):
        grant.record_grant(tmp_path, binding=binding(), reason="r", channel="web", now=NOW)
    bad = binding()
    bad["runtime"] = "other"
    with pytest.raises(ValueError):
        grant.record_grant(tmp_path, binding=bad, reason="r", channel="tty", now=NOW)


def test_state_dir_symlink_is_refused(tmp_path):
    parent = tmp_path / ".local/state/claude-config"
    parent.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (parent / "commit-gate").symlink_to(target)
    with pytest.raises(OSError):
        record(tmp_path)
    assert list(target.iterdir()) == []
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW) is False


def test_wide_state_dir_is_refused(tmp_path):
    path = record(tmp_path)
    os.chmod(grant.state_dir(tmp_path), 0o755)
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW) is False
    assert path.exists()


def test_wide_grant_file_is_not_consumed(tmp_path):
    path = record(tmp_path)
    os.chmod(path, 0o644)
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW) is False


def test_symlink_grant_file_is_not_consumed(tmp_path):
    path = record(tmp_path)
    real = tmp_path / "real.json"
    path.rename(real)
    path.symlink_to(real)
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW) is False
    assert real.exists()


def test_oversized_grant_file_is_not_consumed(tmp_path):
    path = record(tmp_path)
    with open(path, "ab") as handle:
        handle.write(b" " * (17 * 1024))
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW) is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(created_at="1000000"),
        lambda d: d.update(created_at=True),
        lambda d: d.update(channel="web"),
        lambda d: d.update(reason=5),
        lambda d: d.update(schema=2),
        lambda d: d["binding"].update(runtime="other"),
        lambda d: d["binding"].update(extra="x"),
        lambda d: d.__setitem__("binding", []),
        lambda d: d.update(created_at=NOW + 5000),
    ],
)
def test_malformed_content_is_never_consumed(tmp_path, mutate):
    path = record(tmp_path)
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW + 1) is False


def test_garbage_content_is_never_consumed(tmp_path):
    path = record(tmp_path)
    path.write_bytes(b"\xff\x00not json")
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW) is False


def test_content_binding_must_match_file_name(tmp_path):
    path = record(tmp_path)
    other = binding(head="c" * 40)
    forged = grant.state_dir(tmp_path) / path.name
    data = json.loads(forged.read_text())
    data["binding"] = other
    forged.write_text(json.dumps(data))
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW) is False


def test_consume_without_state_dir_is_false(tmp_path):
    assert grant.consume_grant(tmp_path, binding=binding(), now=NOW) is False
    assert not (tmp_path / ".local").exists()


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_cli_relayed_records_and_revoke(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    home = tmp_path / "home"
    home.mkdir()
    argv = [
        "--home", str(home), "grant", "--runtime", "claude", "--repo", str(repo),
        "--message-sha256", MSG, "--reason", "사용자 승인", "--relayed",
    ]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert MSG in out and "UNBORN" in out
    files = list(grant.state_dir(home).iterdir())
    assert len(files) == 1
    root = Path(os.path.realpath(repo))
    b = grant.make_binding(repo_root=root, head="", message_sha256=MSG, runtime="claude")
    assert files[0].name == grant.binding_name(b)
    assert cli.main(["--home", str(home), "revoke", "--repo", str(repo)]) == 0
    assert list(grant.state_dir(home).iterdir()) == []
    assert grant.consume_grant(home, binding=b, now=grant_now()) is False


def grant_now():
    import time
    return time.time()


def test_cli_message_file_hash(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    msg = tmp_path / "msg.txt"
    msg.write_bytes(b"feat: x\n")
    home = tmp_path / "home"
    home.mkdir()
    assert cli.main([
        "--home", str(home), "grant", "--runtime", "codex", "--repo", str(repo),
        "--message-file", str(msg), "--reason", "ok", "--relayed",
    ]) == 0
    (path,) = list(grant.state_dir(home).iterdir())
    assert json.loads(path.read_text())["binding"]["message_sha256"] == MSG


def test_cli_without_relayed_requires_tty_confirmation(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    home = tmp_path / "home"
    home.mkdir()
    argv = [
        "--home", str(home), "grant", "--runtime", "claude", "--repo", str(repo),
        "--message-sha256", MSG, "--reason", "ok",
    ]
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(argv) != 0
    assert not grant.state_dir(home).exists()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "no")
    assert cli.main(argv) != 0
    monkeypatch.setattr("builtins.input", lambda _prompt="": "commit")
    assert cli.main(argv) == 0


def test_cli_rejects_bad_hash_and_non_repo(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert cli.main([
        "--home", str(home), "grant", "--runtime", "claude", "--repo", str(tmp_path),
        "--message-sha256", "xyz", "--reason", "ok", "--relayed",
    ]) != 0
