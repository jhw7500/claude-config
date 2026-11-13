"""One leading ``cd <abs> &&`` before the canonical PR command.

The gate must evaluate the directory ``gh`` will run in, never the hook cwd.
Acceptance tests (``test_accepts_*``, ``test_target_*``, ``test_*_override_*``,
``test_adapters_*``, reverse-direction and worktree tests) fail on code without
the prefix split. ``test_refused_*`` and ``test_*_preserved`` are preservation
checks that pass before and after.
"""

from pathlib import Path
import re
import shutil

import pytest

from pre_pr_tribunal import pr_override_grant
from pre_pr_tribunal.gate import GateCode, evaluate_gate
from tests.pre_pr_tribunal.test_gate_adapters import (
    BOUND_COMMAND,
    _commit,
    _git,
    _override_digest,
    _passing_verdict,
    _round_three_failure,
    installed_package,
    payload,
    run_adapter,
)

_GRAMMAR_PATH = re.compile(r"(?:/[A-Za-z0-9._-]+)+")


def _bare(path: Path) -> str:
    return f"cd {path} && {BOUND_COMMAND}"


def _copy_repo(source: Path, name: str) -> Path:
    target = source.parent / name
    shutil.copytree(source, target, symlinks=True)
    return target


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    path = tmp_path / "elsewhere"
    path.mkdir()
    return path


def _assert_fixture_path_fits_grammar(path: Path) -> None:
    assert str(path.resolve()) == str(path)
    assert _GRAMMAR_PATH.fullmatch(str(path)) is not None


@pytest.mark.parametrize("quote", ("", "'", '"'))
def test_accepts_prefix_and_evaluates_target_not_hook_cwd(
    git_repo: Path, elsewhere: Path, quote: str
):
    _assert_fixture_path_fits_grammar(git_repo)
    _passing_verdict(git_repo)
    command = f"cd {quote}{git_repo}{quote} && {BOUND_COMMAND}"
    decision = evaluate_gate(elsewhere, command)
    assert decision.code is GateCode.PASS
    assert decision.block is False


def test_target_is_evaluated_even_when_hook_cwd_is_another_unreviewed_repo(
    git_repo: Path,
):
    other = _copy_repo(git_repo, "other")
    _passing_verdict(git_repo)
    assert evaluate_gate(other, BOUND_COMMAND).code is GateCode.TRIBUNAL_REQUIRED
    assert evaluate_gate(other, _bare(git_repo)).code is GateCode.PASS


def test_reverse_direction_hook_cwd_pass_does_not_cover_unreviewed_target(
    git_repo: Path,
):
    unreviewed = _copy_repo(git_repo, "unreviewed")
    _passing_verdict(git_repo)
    assert evaluate_gate(git_repo, BOUND_COMMAND).code is GateCode.PASS
    decision = evaluate_gate(git_repo, _bare(unreviewed))
    assert decision.code is GateCode.TRIBUNAL_REQUIRED
    assert decision.block is True


def test_linked_worktree_cannot_borrow_the_pass_of_its_sibling(git_repo: Path):
    linked = git_repo.parent / "linked"
    _git(git_repo, "worktree", "add", "-q", "-b", "linked-branch", str(linked))
    _passing_verdict(git_repo)
    assert evaluate_gate(git_repo, BOUND_COMMAND).code is GateCode.PASS
    decision = evaluate_gate(git_repo, _bare(linked))
    assert decision.block is True
    assert decision.code is GateCode.TRIBUNAL_REQUIRED
    # The same linked worktree is accepted only through its own PASS.
    assert evaluate_gate(linked, BOUND_COMMAND).code is GateCode.TRIBUNAL_REQUIRED


def test_target_state_flows_through(git_repo: Path, elsewhere: Path, tmp_path: Path):
    unreviewed = _copy_repo(git_repo, "unreviewed")
    assert evaluate_gate(elsewhere, _bare(unreviewed)).code is (
        GateCode.TRIBUNAL_REQUIRED
    )
    _passing_verdict(git_repo)
    child = git_repo / "child"
    child.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    for path in (child, plain, tmp_path / "missing"):
        decision = evaluate_gate(elsewhere, _bare(path))
        assert decision.code is GateCode.REPOSITORY_UNSUPPORTED
        assert decision.block is True
    (git_repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    assert evaluate_gate(elsewhere, _bare(git_repo)).code is GateCode.WORKTREE_DIRTY
    (git_repo / "dirty.txt").unlink()
    _commit(git_repo, "after the verdict\n")
    assert evaluate_gate(elsewhere, _bare(git_repo)).code is GateCode.VERDICT_STALE


def test_symlink_alias_of_the_root_lands_on_the_same_checkout(
    git_repo: Path, elsewhere: Path
):
    _passing_verdict(git_repo)
    alias = git_repo.parent / "alias"
    alias.symlink_to(git_repo)
    assert evaluate_gate(elsewhere, _bare(alias)).code is GateCode.PASS


@pytest.mark.parametrize(
    ("name", "runtime"),
    (("claude_hook.py", "claude"), ("codex_hook.py", "codex")),
)
def test_adapters_allow_prefixed_command(
    installed_package, git_repo, elsewhere, name, runtime
):
    _passing_verdict(git_repo)
    result = run_adapter(
        installed_package, name, payload(elsewhere, _bare(git_repo)), elsewhere
    )
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    denied = run_adapter(
        installed_package,
        name,
        payload(elsewhere, f"cd {git_repo} && {BOUND_COMMAND}; true"),
        elsewhere,
    )
    assert "COMMAND_AMBIGUOUS" in denied.stdout


def test_terminal_override_is_consumed_from_the_target_not_the_hook_cwd(
    git_repo: Path, elsewhere: Path
):
    session = _copy_repo(git_repo, "session")
    _round_three_failure(git_repo)
    command = _bare(git_repo)
    assert evaluate_gate(session, command).code is GateCode.ROUND_LIMIT_EXHAUSTED
    pr_override_grant.record_grant(
        git_repo,
        runtime="codex",
        expected_verdict_sha256=_override_digest(git_repo),
        reason="user approved the terminal result",
        channel="relayed",
        now=lambda: "2026-11-13T00:00:00Z",
    )
    grant = git_repo / ".review/pr-override-grant.json"
    first = evaluate_gate(session, command, runtime="codex")
    assert first.code is GateCode.USER_APPROVED and first.block is False
    assert not grant.exists()
    second = evaluate_gate(session, command, runtime="codex")
    assert second.code is GateCode.ROUND_LIMIT_EXHAUSTED


def test_grant_recorded_in_the_session_repository_is_not_consumed_by_the_target(
    git_repo: Path,
):
    session = _copy_repo(git_repo, "session")
    _round_three_failure(git_repo)
    _round_three_failure(session)
    pr_override_grant.record_grant(
        session,
        runtime="codex",
        expected_verdict_sha256=_override_digest(session),
        reason="user approved the session repository",
        channel="relayed",
        now=lambda: "2026-11-13T00:00:00Z",
    )
    grant = session / ".review/pr-override-grant.json"
    decision = evaluate_gate(session, _bare(git_repo), runtime="codex")
    assert decision.code is GateCode.ROUND_LIMIT_EXHAUSTED
    assert decision.block is True
    assert grant.exists()


def test_bash_func_cd_in_the_environment_refuses_the_prefix(
    git_repo: Path, elsewhere: Path, monkeypatch
):
    _passing_verdict(git_repo)
    command = _bare(git_repo)
    assert evaluate_gate(elsewhere, command).code is GateCode.PASS
    monkeypatch.setenv("BASH_FUNC_cd%%", "() { return 0; }")
    decision = evaluate_gate(elsewhere, command)
    assert decision.block is True
    assert decision.code is GateCode.COMMAND_AMBIGUOUS
    # The bare canonical command has no cd and is unaffected.
    assert evaluate_gate(git_repo, BOUND_COMMAND).code is GateCode.PASS


@pytest.mark.parametrize("name", ("BASH_ENV", "ENV"))
def test_startup_file_variables_refuse_the_prefix(
    git_repo: Path, elsewhere: Path, monkeypatch, name: str
):
    _passing_verdict(git_repo)
    command = _bare(git_repo)
    monkeypatch.delenv("BASH_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    assert evaluate_gate(elsewhere, command).code is GateCode.PASS
    monkeypatch.setenv(name, "")
    assert evaluate_gate(elsewhere, command).code is GateCode.PASS
    monkeypatch.setenv(name, "/tmp/startup.sh")
    decision = evaluate_gate(elsewhere, command)
    assert decision.block is True
    assert decision.code is GateCode.COMMAND_AMBIGUOUS
    assert decision.reason == "TARGET_ENVIRONMENT"
    assert evaluate_gate(git_repo, BOUND_COMMAND).code is GateCode.PASS


def _refused_forms(target: Path) -> dict[str, str]:
    t = str(target)
    c = BOUND_COMMAND
    return {
        "relative": f"cd ../{target.name} && {c}",
        "bare-name": f"cd {target.name} && {c}",
        "dash": f"cd - && {c}",
        "no-arg": f"cd && {c}",
        "tilde": f"cd ~/x && {c}",
        "empty-single": f"cd '' && {c}",
        "empty-double": f'cd "" && {c}',
        "expansion": f"cd $HOME/x && {c}",
        "subshell": f"cd $(pwd) && {c}",
        "escape-space": f"cd {t}\\ x && {c}",
        "option": f"cd -P {t} && {c}",
        "double-dash": f"cd -- {t} && {c}",
        "semicolon": f"cd {t}; {c}",
        "or": f"cd {t} || {c}",
        "pipe": f"cd {t} | {c}",
        "background-cd": f"cd {t} & {c}",
        "newline": f"cd {t}\n{c}",
        "newline-after-and": f"cd {t} &&\n{c}",
        "double-cd": f"cd {t} && cd {t} && {c}",
        "assignment-before": f"FOO=1 cd {t} && {c}",
        "cdpath-before": f"CDPATH=x cd {t} && {c}",
        "leading-space": f" cd {t} && {c}",
        "double-space": f"cd  {t} && {c}",
        "tab": f"cd\t{t} && {c}",
        "builtin": f"builtin cd {t} && {c}",
        "command": f"command cd {t} && {c}",
        "backslash": f"\\cd {t} && {c}",
        "pushd": f"pushd {t} && {c}",
        "trailing-slash": f"cd {t}/ && {c}",
        "double-slash": f"cd /{t} && {c}",
        "dot-component": f"cd {t}/. && {c}",
        "dotdot-component": f"cd {t}/../{target.name} && {c}",
        "grouped": f"cd {t} && ({c})",
        "bare-gh": f"cd {t} && gh pr create --base master",
        "env-wrapper": f"cd {t} && env {c}",
        "trailing-semicolon": f"cd {t} && {c}; true",
        "trailing-pipe": f"cd {t} && {c} | cat",
        "redirection": f"cd {t} && {c} >log",
        "trailing-background": f"cd {t} && {c} &",
        "trailing-background-spaced": f"cd {t} && {c}  & ",
        "trailing-background-comment": f"cd {t} && {c} & # x",
        "head-override": f"cd {t} && {c} --head x",
        "newline-background-comment": f"cd {t} && {c} & #a\n#b",
        "newline-background-continuation": f"cd {t} && {c} &\\\n#b",
        "cr-in-path": f"cd {t}\r && {c}",
        "nbsp-after-cd": f"cd\u00a0{t} && {c}",
        "fullwidth-slash-in-path": f"cd {t}\uff0fx && {c}",
        "nbsp-before-and": f"cd {t}\u00a0&& {c}",
    }


# Forms the prefix regex or the remainder rules reject; the scanner's own
# reason for them stays UNSAFE_PR_CONTEXT.
_PREFIX_RULE_REFUSALS = frozenset(
    {
        "relative", "bare-name", "tilde", "empty-single", "empty-double",
        "expansion", "subshell", "escape-space", "option", "double-dash",
        "trailing-slash", "double-slash", "dot-component", "dotdot-component",
        "leading-space", "double-space", "tab", "grouped", "bare-gh",
        "env-wrapper", "trailing-semicolon", "trailing-pipe", "redirection",
        "trailing-background", "trailing-background-spaced",
        "trailing-background-comment",
        "newline-background-comment", "newline-background-continuation",
        "cr-in-path", "nbsp-after-cd", "fullwidth-slash-in-path",
        "nbsp-before-and",
    }
)


def test_refused_forms_keep_todays_denial(git_repo: Path, elsewhere: Path):
    _passing_verdict(git_repo)
    forms = _refused_forms(git_repo)
    for name, command in forms.items():
        decision = evaluate_gate(elsewhere, command)
        assert decision.block is True, name
        assert decision.code is GateCode.COMMAND_AMBIGUOUS, name
        if name in _PREFIX_RULE_REFUSALS:
            assert decision.reason == "UNSAFE_PR_CONTEXT", name


def test_wrong_base_behind_a_valid_prefix_is_denied(git_repo: Path, elsewhere: Path):
    _passing_verdict(git_repo)
    command = f"cd {git_repo} && PATH=/usr/bin:/bin /usr/bin/gh pr create --base main"
    decision = evaluate_gate(elsewhere, command)
    assert decision.block is True
    assert decision.code is GateCode.COMMAND_AMBIGUOUS
    assert decision.reason == "TARGET_BINDING"


def test_non_create_gh_with_prefix_and_plain_command_preserved(
    git_repo: Path, elsewhere: Path
):
    _passing_verdict(git_repo)
    view = evaluate_gate(elsewhere, f"cd {git_repo} && gh pr view")
    assert view.code is GateCode.NOT_PR_CREATE and view.block is False
    assert evaluate_gate(git_repo, BOUND_COMMAND).code is GateCode.PASS
    assert evaluate_gate(elsewhere, BOUND_COMMAND).code is (
        GateCode.REPOSITORY_UNSUPPORTED
    )


def test_prefixed_override_cli_commands_are_not_pr_create(
    git_repo: Path, elsewhere: Path
):
    cli = '/usr/bin/python3 "$HOME/.local/share/claude-config/pre_pr_tribunal/cli.py"'
    for tail in (
        "pr-override-preview --runtime claude",
        "pr-override-grant --runtime claude --revoke",
    ):
        decision = evaluate_gate(elsewhere, f"cd {git_repo} && {cli} {tail}")
        assert decision.code is GateCode.NOT_PR_CREATE
        assert decision.block is False
