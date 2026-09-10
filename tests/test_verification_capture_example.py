"""Run the skill's Bash example; textual presence is not a behavioral verdict."""

import os
from pathlib import Path
import re
import shlex
import subprocess

import pytest


SKILL = Path(__file__).parents[1] / "skills/superpowers-extras/SKILL.md"


@pytest.fixture
def capture_example():
    source = SKILL.read_text()
    section = source.split("### Bash capture example", 1)[1]
    return re.search(r"```bash\n(.*?)\n```", section, re.S).group(1)


def run_example(tmp_path, example, runner, *, missing_cwd=False):
    intended = tmp_path / "intended checkout"
    wrong = tmp_path / "wrong checkout"
    (wrong / "tests").mkdir(parents=True)
    marker = wrong / "wrong-checkout-ran"
    (wrong / "tests/check.sh").write_text("touch wrong-checkout-ran\n")
    if not missing_cwd:
        (intended / "tests").mkdir(parents=True)
        (intended / "tests/check.sh").write_text(runner)
    log = tmp_path / "private log"
    log.touch(mode=0o600)
    result = subprocess.run(
        ["bash", "-c", example], cwd=wrong, text=True, capture_output=True,
        env={**os.environ, "verification_cwd": str(intended),
             "verification_log": str(log)}, timeout=5,
    )
    return result, log, marker


def test_missing_directory_never_runs_a_different_checkouts_script(tmp_path, capture_example):
    # Break: a semicolon lets the runner execute after a failed directory change.
    result, log, marker = run_example(
        tmp_path, capture_example, "", missing_cwd=True,
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert log.read_text() == ""


@pytest.mark.parametrize("verifier_exit", [0, 23])
def test_target_exit_and_full_diagnostics_survive_formatting(
    tmp_path, capture_example, verifier_exit,
):
    # Break: take $? after tail/printf, or discard the full stream.
    runner = "printf 'first-diagnostic\\n' >&2\n"
    runner += "for n in {1..45}; do printf 'detail-%s\\n' \"$n\"; done\n"
    runner += f"exit {verifier_exit}\n"
    result, log, marker = run_example(tmp_path, capture_example, runner)
    assert result.returncode == verifier_exit
    assert log.read_text().startswith("first-diagnostic\n")
    assert "detail-45\n" in log.read_text()
    assert not marker.exists()


def test_zero_exit_with_all_skips_remains_visible_not_rewritten_as_pass(tmp_path, capture_example):
    # A capture example is not a runner-independent semantic PASS classifier.
    runner = "printf 'collected=12 passed=0 failed=0 skipped=12\\n'\n"
    result, log, marker = run_example(tmp_path, capture_example, runner)
    assert result.returncode == 0
    assert "collected=12 passed=0 failed=0 skipped=12" in log.read_text()
    assert not marker.exists()


@pytest.mark.parametrize("verifier_exit", [0, 23])
def test_missing_raw_log_fails_capture_but_reports_target_exit(
    tmp_path, capture_example, verifier_exit,
):
    # Break: unconditional target exit overwrites a lost-log/readback failure.
    log_arg = shlex.quote(str(tmp_path / "private log"))
    runner = f"printf 'diagnostic\\n'\nunlink -- {log_arg}\nexit {verifier_exit}\n"
    result, log, marker = run_example(tmp_path, capture_example, runner)
    assert not log.exists()
    assert result.returncode == 125
    assert f"verifier_exit={verifier_exit}" in result.stdout
    assert "capture_error=" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_nonregular_or_symlink_log_is_not_accepted_as_retained_evidence(
    tmp_path, capture_example, replacement,
):
    # Break: accepting a readable symlink or ignoring a directory readback error.
    log_arg = shlex.quote(str(tmp_path / "private log"))
    decoy = tmp_path / "decoy"
    decoy.write_text("unrelated output\n")
    replace = (f"ln -s -- {shlex.quote(str(decoy))} {log_arg}"
               if replacement == "symlink" else f"mkdir -- {log_arg}")
    runner = f"printf 'diagnostic\\n'\nunlink -- {log_arg}\n{replace}\nexit 0\n"
    result, log, marker = run_example(tmp_path, capture_example, runner)
    assert log.is_symlink() if replacement == "symlink" else log.is_dir()
    assert result.returncode == 125
    assert "verifier_exit=0" in result.stdout
    assert "capture_error=" in result.stderr
    assert "unrelated output" not in result.stdout
    assert not marker.exists()


def test_log_location_is_not_exported_to_the_verifier(tmp_path, capture_example):
    # Break: inherited exported capture metadata leaks to the child runner.
    runner = 'if [[ ${verification_log+x} ]]; then exit 31; fi\nprintf "diagnostic\\n"\n'
    result, log, marker = run_example(tmp_path, capture_example, runner)
    assert result.returncode == 0
    assert log.read_text() == "diagnostic\n"
    assert not marker.exists()


def test_readback_failure_does_not_report_capture_success(tmp_path, capture_example):
    # Fault-inject readback I/O failure; the verifier and log remain real.
    example = "tail() { return 74; }\n" + capture_example
    result, log, marker = run_example(tmp_path, example, "printf 'diagnostic\\n'\n")
    assert log.read_text() == "diagnostic\n"
    assert result.returncode == 125
    assert "verifier_exit=0" in result.stdout
    assert "capture_error=" in result.stderr
    assert not marker.exists()
