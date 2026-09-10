"""Run the skill's Bash example; textual presence is not a behavioral verdict."""

import os
from pathlib import Path
import re
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
    return result, log.read_text(), marker


def test_missing_directory_never_runs_a_different_checkouts_script(tmp_path, capture_example):
    # Break: a semicolon lets the runner execute after a failed directory change.
    result, log, marker = run_example(
        tmp_path, capture_example, "", missing_cwd=True,
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert log == ""


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
    assert log.startswith("first-diagnostic\n")
    assert "detail-45\n" in log
    assert not marker.exists()


def test_zero_exit_with_all_skips_remains_visible_not_rewritten_as_pass(tmp_path, capture_example):
    # A capture example is not a runner-independent semantic PASS classifier.
    runner = "printf 'collected=12 passed=0 failed=0 skipped=12\\n'\n"
    result, log, marker = run_example(tmp_path, capture_example, runner)
    assert result.returncode == 0
    assert "collected=12 passed=0 failed=0 skipped=12" in log
    assert not marker.exists()
