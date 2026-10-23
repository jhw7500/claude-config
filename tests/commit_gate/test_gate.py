import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from commit_gate import gate, grant, repo as repo_mod

VALID = """feat(x): 결과를 말하는 제목

 ### Contract version

v1

 ### Why

이유다.

 ### Changes

- 변경한다.

 ### Validation

- 실행한 명령과 결과.

 ### References

- https://github.com/jhw7500/automation/issues/237
"""
BAD = "feat(x): 필드가 없는 메시지\n\n본문만 있다.\n"


def ev(command, repo, home, runtime="claude", cwd=None):
    return gate.evaluate(command, cwd=cwd or repo, runtime=runtime, home=home, now=time.time())


def msgfile(tmp_path, text, name="msg.txt"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def sha_of_reason(reason):
    return re.search(r"--message-sha256 ([0-9a-f]{64})", reason).group(1)


def approve(home, repo, sha, runtime="claude"):
    b = grant.make_binding(repo_root=repo_mod.repo_root(repo), head=repo_mod.head(repo),
                           message_sha256=sha, runtime=runtime)
    grant.record_grant(home, binding=b, reason="ok", channel="relayed", now=time.time())


def test_valid_file_commit_passes(repo, home, tmp_path):
    f = msgfile(tmp_path, VALID)
    assert ev(f"git commit --cleanup=verbatim -F {f}", repo, home).allow


def test_missing_fields_denied_with_codes_and_source(repo, home, tmp_path):
    f = msgfile(tmp_path, BAD)
    d = ev(f"git commit -F {f}", repo, home)
    source = json.loads((gate.VENDOR_DIR / "SOURCE.json").read_text())
    assert not d.allow
    assert "heading-contract" in d.reason and source["automation_commit"] in d.reason
    assert "cli.py grant" in d.reason and "--relayed" in d.reason


def test_unknown_message_passes_with_warning(repo, home, capsys):
    assert ev("git commit", repo, home).allow
    assert "[commit-gate]" in capsys.readouterr().err
    assert ev('MSG=x; git commit -m "$MSG"', repo, home).allow
    assert "[commit-gate]" in capsys.readouterr().err


def test_claude_heredoc_violation_denied_valid_passes(repo, home):
    cmd = "git commit -m \"$(cat <<'EOF'\n%s\nEOF\n)\""
    assert not ev(cmd % BAD.strip(), repo, home).allow
    assert ev(cmd % VALID.strip(), repo, home).allow


def test_auto_message_passes(repo, home):
    assert ev('git commit -m "Merge branch \'x\'"', repo, home).allow


@pytest.mark.parametrize("origin", [
    "https://github.com/someone/demo.git", "https://gitlab.com/jhw7500/demo.git", None])
def test_non_jhw7500_origin_passes(tmp_path, home, origin, repo_factory):
    r = repo_factory(tmp_path / "other", origin)
    assert ev('git commit -m "bad"', r, home).allow


def test_gate_uses_commit_directory_origin(tmp_path, repo, home, repo_factory):
    outside = repo_factory(tmp_path / "outside", None)
    assert not ev(f'git -C {repo} commit -m "bad"', repo, home, cwd=outside).allow
    assert ev(f'git commit -m "bad"', repo, home, cwd=outside).allow


def test_grant_allows_one_then_denies(repo, home, tmp_path):
    f = msgfile(tmp_path, BAD)
    cmd = f"git commit -F {f}"
    d = ev(cmd, repo, home)
    approve(home, repo, sha_of_reason(d.reason))
    assert ev(cmd, repo, home).allow
    assert not ev(cmd, repo, home).allow


def test_one_violation_blocks_compound_command(repo, home, tmp_path):
    granted, bad = msgfile(tmp_path, BAD, "a"), msgfile(tmp_path, BAD + "x\n", "b")
    approve(home, repo, hashlib.sha256(granted.read_bytes()).hexdigest())
    assert not ev(f"git commit -F {granted} && git commit -F {bad}", repo, home).allow
    # the grant for the first commit survived the denied compound command
    assert ev(f"git commit -F {granted}", repo, home).allow


def test_literal_violation_can_be_granted(repo, home):
    cmd = 'git commit -m "bad title"'
    d = ev(cmd, repo, home)
    assert not d.allow
    approve(home, repo, sha_of_reason(d.reason))
    assert ev(cmd, repo, home).allow
    assert not ev(cmd, repo, home).allow


def test_file_grant_uses_raw_file_bytes(repo, home, tmp_path):
    f = msgfile(tmp_path, BAD + "\n\n   \n")  # cleanup changes bytes; hash stays raw
    d = ev(f"git commit -F {f}", repo, home)
    assert sha_of_reason(d.reason) == hashlib.sha256(f.read_bytes()).hexdigest()


def test_deny_reason_has_fix_hint(repo, home):
    r = ev('git commit -m "Feat: x"', repo, home).reason
    assert "title-type-case: " in r and "heading-contract: " in r


def test_internal_error_fails_open(repo, home, monkeypatch, capsys):
    def boom(_):
        raise RuntimeError("x")
    monkeypatch.setattr(gate, "check_message", boom)
    assert ev('git commit -m "bad"', repo, home).allow
    assert "[commit-gate]" in capsys.readouterr().err


def test_config_cleanup_verbatim_is_honoured(repo, home):
    # default cleanup would drop the leading blank-trimmed text; verbatim keeps it
    msg = "\n" + VALID
    subprocess.run(["git", "-C", str(repo), "config", "commit.cleanup", "verbatim"], check=True)
    assert not ev(f"git commit -m '{msg}'", repo, home).allow
    subprocess.run(["git", "-C", str(repo), "config", "--unset", "commit.cleanup"], check=True)
    assert ev(f"git commit -m '{msg}'", repo, home).allow


def test_comment_char_config_is_unknown(repo, home, capsys):
    subprocess.run(["git", "-C", str(repo), "config", "core.commentChar", ";"], check=True)
    assert ev('git commit -m "bad"', repo, home).allow
    assert "[commit-gate]" in capsys.readouterr().err


def test_fast_path_skips_scanning(repo, home, monkeypatch):
    monkeypatch.setattr(gate.commit_scan, "scan_commits", lambda *a, **k: 1 / 0)
    assert ev("ls -la", repo, home).allow


def test_vendor_mismatch_allows_with_warning(repo, home, tmp_path, monkeypatch, capsys):
    copy = tmp_path / "vendor"
    shutil.copytree(gate.VENDOR_DIR, copy)
    target = copy / "commit_title_rules.py"
    target.write_bytes(target.read_bytes() + b"#")
    monkeypatch.setattr(gate, "VENDOR_DIR", copy)
    assert ev('git commit -m "bad"', repo, home).allow
    assert "digest" in capsys.readouterr().err
