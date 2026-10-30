"""Test the information a person sees before direct automatic-run consent."""

import io
import json
import sys

import pytest

from pre_pr_tribunal import auto_run, cli
from tests.pre_pr_tribunal.test_model_store import NOW


class TerminalAnswer(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("choice", ("100 자동", "100 auto", "100 automatic"))
def test_terminal_auto_consent_discloses_policy_scope_cost_and_stop_before_answer(
    git_repo, monkeypatch, capsys, choice,
):
    preview = auto_run.preview(git_repo, base="master", runtime="codex", now=NOW)
    disclosures = []

    class ObservedTerminalAnswer(TerminalAnswer):
        def readline(self, *args):
            # Observe what was visible at the actual consent boundary, before
            # supplying the person's answer. Final stdout is not pre-consent.
            disclosures.append(capsys.readouterr().err)
            return super().readline(*args)

    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(sys, "stdin", ObservedTerminalAnswer(f"{choice}\n"))
    assert cli.main([
        "auto-run-grant", "--base", "master", "--runtime", "codex",
        "--binding-sha256", preview["binding_sha256"],
        "--choice", choice, "--reason", "Explicit terminal choice",
    ], wall_clock=NOW) == 0
    visible = "".join(disclosures)
    assert "intensity 100" in visible
    assert "mode iterative" in visible
    assert "tracked.txt" in visible
    assert "A=inherit" in visible and "B=inherit" in visible and "C=inherit" in visible
    assert "fix" in visible and "test" in visible and "commit" in visible
    assert "elapsed time/cost: unknown" in visible
    assert "maximum rounds 3 (including initial review)" in visible
    assert "provider failure" in visible and "cancel" in visible
    assert "no PR creation or merge authority" in visible
    assert f"Type {json.dumps(choice, ensure_ascii=False)} to authorize" in visible
    assert json.loads(capsys.readouterr().out)["id"]


@pytest.mark.parametrize(
    ("choice", "answer"),
    (
        ("100 자동", "100 auto"),
        ("100 auto", "100 자동"),
        ("100 automatic", "100"),
        ("100 automatic", "진행"),
    ),
)
def test_terminal_auto_consent_rejects_answer_that_does_not_match_choice(
    git_repo, monkeypatch, capsys, choice, answer,
):
    preview = auto_run.preview(git_repo, base="master", runtime="codex", now=NOW)
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(sys, "stdin", TerminalAnswer(f"{answer}\n"))

    assert cli.main([
        "auto-run-grant", "--base", "master", "--runtime", "codex",
        "--binding-sha256", preview["binding_sha256"],
        "--choice", choice, "--reason", "Mismatched terminal answer",
    ], wall_clock=NOW) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith(
        "PRE_PR_TRIBUNAL:AUTO_RUN_CONFIRMATION_MISMATCH\n"
    )
    assert not (git_repo / ".review/auto-runs.json").exists()


def test_terminal_auto_consent_rejects_unsupported_choice_before_prompt(
    git_repo, monkeypatch, capsys,
):
    preview = auto_run.preview(git_repo, base="master", runtime="codex", now=NOW)
    answer = TerminalAnswer("100\n")
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(sys, "stdin", answer)

    assert cli.main([
        "auto-run-grant", "--base", "master", "--runtime", "codex",
        "--binding-sha256", preview["binding_sha256"],
        "--choice", "100", "--reason", "Unsupported terminal choice",
    ], wall_clock=NOW) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "PRE_PR_TRIBUNAL:AUTO_RUN_CONSENT_REQUIRED\n"
    assert answer.tell() == 0
    assert not (git_repo / ".review/auto-runs.json").exists()
