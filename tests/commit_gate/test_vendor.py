import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "hooks" / "commit_gate" / "vendor"
SYNC = ROOT / "scripts" / "sync-commit-gate-vendor.py"
NAMES = ("validate_change_evidence.py", "commit_title_rules.py", "check_commit_message.py")

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


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_vendor_digests_match_source():
    source = json.loads((VENDOR / "SOURCE.json").read_text(encoding="utf-8"))
    assert len(source["automation_commit"]) == 40
    assert set(source["files"]) == set(NAMES)
    for name in NAMES:
        assert source["files"][name] == _sha(VENDOR / name)


def test_vendor_runs_under_this_interpreter():
    spec = importlib.util.spec_from_file_location(
        "vendored_check_commit_message", VENDOR / "check_commit_message.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.check_message(VALID)["valid"] is True
    assert module.check_message("feat(x): 필드가 없는 메시지\n\n본문만 있다.\n")["valid"] is False


def test_sync_check_detects_drift(tmp_path):
    copy = tmp_path / "vendor"
    shutil.copytree(VENDOR, copy)
    cmd = [sys.executable, str(SYNC), "--check", "--vendor-dir", str(copy)]
    assert subprocess.run(cmd, capture_output=True).returncode == 0
    target = copy / NAMES[1]
    target.write_bytes(target.read_bytes() + b"#")
    assert subprocess.run(cmd, capture_output=True).returncode == 1
