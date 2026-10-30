"""Test the information a person sees before direct automatic-run consent."""

import io
import json
import sys

from pre_pr_tribunal import auto_run, cli
from tests.pre_pr_tribunal.test_model_store import NOW


class TerminalAnswer(io.StringIO):
    def isatty(self):
        return True


def test_terminal_auto_consent_discloses_policy_scope_cost_and_stop_before_answer(
    git_repo, monkeypatch, capsys,
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
    monkeypatch.setattr(sys, "stdin", ObservedTerminalAnswer("100 자동\n"))
    assert cli.main([
        "auto-run-grant", "--base", "master", "--runtime", "codex",
        "--binding-sha256", preview["binding_sha256"],
        "--choice", "100 자동", "--reason", "Explicit terminal choice",
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
    assert json.loads(capsys.readouterr().out)["id"]
