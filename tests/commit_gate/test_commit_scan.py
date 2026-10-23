import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from commit_gate.commit_scan import effective_message, scan_commits  # noqa: E402

CWD = Path("/work/repo")


def one(command, cwd=CWD):
    result = scan_commits(command, cwd=cwd)
    assert len(result.invocations) == 1, result
    return result.invocations[0]


def test_no_commit_returns_empty():
    assert scan_commits("git status && gh pr list", cwd=CWD).invocations == ()


def test_file_message():
    inv = one("git commit --cleanup=verbatim --file /tmp/m.md")
    assert inv.source == "file"
    assert inv.message_path == Path("/tmp/m.md")
    assert inv.directory == CWD


def test_file_message_variants():
    assert one("git commit -F /tmp/a.md").message_path == Path("/tmp/a.md")
    assert one("git commit --file=/tmp/b.md").message_path == Path("/tmp/b.md")
    assert one("git commit -F/tmp/c.md").message_path == Path("/tmp/c.md")
    assert one("git commit -F m.md").message_path == CWD / "m.md"
    assert one("git commit -F -").source == "unknown"


def test_literal_messages_joined():
    inv = one('git commit -m "a" -m "b"')
    assert inv.source == "literal"
    assert inv.message_text == "a\n\nb"
    assert one("git commit -am 'x y'").message_text == "x y"
    assert one("git commit --message=z").message_text == "z"


def test_claude_heredoc_message_is_literal():
    body = "feat: x\n\n ### Why\n$weird `text` <<\nline"
    for quote in ("'", '"'):
        cmd = (
            f'git commit -m "$(cat <<{quote}EOF{quote}\n{body}\nEOF\n)"'
        )
        inv = one(cmd)
        assert inv.source == "literal"
        assert inv.message_text == body
    cmd = f'git add a && git commit -m "$(cat <<\'EOF\'\n{body}\n\n\nEOF\n)" && git status'
    assert one(cmd).message_text == body


def test_unquoted_heredoc_is_unknown():
    assert one('git commit -m "$(cat <<EOF\nfeat: x\nEOF\n)"').source == "unknown"
    assert one('git commit -m "$MSG"').source == "unknown"


def test_editor_is_unknown():
    assert one("git commit").source == "unknown"
    assert one("git commit -e -F m.md").source == "unknown"
    assert one("git commit -t t.md").source == "unknown"
    assert one("git commit -C HEAD").source == "unknown"
    assert one("git commit --amend").source == "unknown"
    assert one("git --git-dir=/x/.git commit -m a").source == "unknown"


def test_cd_and_dash_c_resolve_commit_directory():
    assert one("cd /x && git commit -F m.md").directory == Path("/x")
    assert one("git -C /x -C y commit -F m.md").directory == Path("/x/y")
    assert one("cd sub && git -c a=b commit -F m.md").directory == CWD / "sub"


def test_multiple_commits_in_one_command():
    cmd = "git commit -F a.md && cd /y && git commit -m hi"
    first, second = scan_commits(cmd, cwd=CWD).invocations
    assert (first.directory, first.source) == (CWD, "file")
    assert (second.directory, second.source, second.message_text) == (
        Path("/y"), "literal", "hi")


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e.c",
               GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@e.c")
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                   capture_output=True)


def test_auto_sources(tmp_path):
    assert one("git commit --fixup HEAD").source == "auto"
    assert one("git commit --squash=abc").source == "auto"
    assert one("git commit --amend --no-edit").source == "auto"
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    assert one("git commit --no-edit", cwd=repo).source == "unknown"
    git_dir = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
        check=True, capture_output=True, text=True).stdout.strip()
    (Path(git_dir) / "MERGE_HEAD").write_text("0" * 40 + "\n")
    assert one("git commit --no-edit", cwd=repo).source == "auto"


def test_env_prefix_and_wrappers():
    assert one("GIT_AUTHOR_NAME=x env git commit -F m.md").source == "file"
    # env with options and the `command` wrapper are outside the allowlist
    assert one("env -i A=b command git commit -F m.md").source == "unknown"
    assert one("/usr/bin/git commit -F m.md").source == "file"


def test_nested_shell_commit_is_unknown():
    inv = one('bash -c "git commit -F m.md"')
    assert inv.source == "unknown"
    assert inv.directory == CWD
    assert one("eval 'git -C /x commit -F m.md'").source == "unknown"


def test_git_log_grep_commit_is_not_a_commit():
    for cmd in ("git log --grep=commit", "git show HEAD -- commit.md",
                'bash -c "git log --grep=commit"'):
        assert scan_commits(cmd, cwd=CWD).invocations == (), cmd


def test_dynamic_constructs_make_commits_unknown():
    assert one("git commit -F $(mktemp)").source == "unknown"
    assert one("git commit -F `x`").source == "unknown"
    assert one("cd $D && git commit -F m.md").source == "unknown"


def test_never_raises_on_malformed_or_huge_input():
    assert scan_commits('git commit -m "unterminated', cwd=CWD).invocations[0].source == "unknown"
    assert scan_commits("echo 'oops", cwd=CWD).invocations == ()
    assert scan_commits("", cwd=CWD).invocations == ()
    huge = "git status; " * 100000
    assert scan_commits(huge, cwd=CWD).invocations == ()
    assert scan_commits(huge + "git commit -F m.md", cwd=CWD).invocations[0].source == "unknown"


# ---- Run 2 fix wave: allowlist grammar regression tests ----------------------

import time  # noqa: E402

import pytest  # noqa: E402


def _only_unknown(command, cwd=CWD):
    inv = one(command, cwd=cwd)
    assert inv.source == "unknown", (command, inv)
    return inv


def test_line_continuation_between_message_options():
    inv = one('git commit -m "feat: t" \\\n  -m "body"')
    assert (inv.source, inv.message_text) == ("literal", "feat: t\n\nbody")
    cmd = ('git commit -m "feat: t" \\\n  -m "$(cat <<\'EOF\'\nbody line\nEOF\n)"')
    inv = one(cmd)
    assert (inv.source, inv.message_text) == ("literal", "feat: t\n\nbody line")
    inv = one("git commit \\\n --cleanup=verbatim \\\n --file /tmp/m.md")
    assert (inv.source, inv.message_path) == ("file", Path("/tmp/m.md"))


def test_line_continuation_quote_semantics():
    assert one("git commit -m 'a \\\nb'").message_text == "a \\\nb"
    assert one('git commit -m "a \\\nb"').message_text == "a b"


@pytest.mark.parametrize("body_and_tail", [
    "feat: x\nEOF\necho injected\nEOF\n)\"",   # delimiter line inside the body
    "feat: x\nEOF)\"",                          # closing paren glued to delimiter
    "feat: x\nEOF  \n)\"",                      # delimiter with trailing blanks
    "feat: x\nEOF\n)\"suffix",                  # text glued after the closing quote
])
def test_noncanonical_quoted_heredoc_is_unknown(body_and_tail):
    inv = _only_unknown(f"git commit -m \"$(cat <<'EOF'\n{body_and_tail}")
    assert inv.message_text is None


def test_heredoc_dash_and_unquoted_delimiter_are_unknown():
    _only_unknown("git commit -m \"$(cat <<-'EOF'\n\tfeat: x\n\tEOF\n)\"")
    _only_unknown("git commit -m \"$(cat <<EOF\nfeat: x\nEOF\n)\"")


def test_canonical_heredoc_followed_by_separator_stays_literal():
    cmd = "git commit -m \"$(cat <<'EOF'\nfeat: x\nEOF\n)\"\ngit status"
    assert one(cmd).message_text == "feat: x"


@pytest.mark.parametrize("command", [
    "{ cd /x; git commit -F m.md; }",
    "if true; then cd /x; fi; git commit -F m.md",
    "pushd /x && git commit -F m.md",
    "env -C /x git commit -F m.md",
    "env --chdir=/x git commit -F m.md",
    "GIT_DIR=/x/.git git commit -F m.md",
    "GIT_WORK_TREE=/x git commit -F m.md",
    "GIT_INDEX_FILE=/x/i git commit -F m.md",
    "env GIT_DIR=/x/.git git commit -F m.md",
    "export GIT_DIR=/x/.git && git commit -F m.md",
    "git status || git commit -F m.md",
    "git add . & git commit -F m.md",
    "cat m.md | git commit -F m.md",
    "(cd /x && git commit -F m.md)",
    "git commit -F m.md > out.txt",
    "git commit -F m.md 2>&1",
    "echo hi # note\ngit commit -m wip",
    "git commit -m wip # trailing",
    "tee m.md && git commit -F m.md",
    "cd ~ && git commit -F m.md",
    "cd /x/* && git commit -F m.md",
    "git checkout b && git commit -F m.md",
])
def test_constructs_outside_the_grammar_make_commits_unknown(command):
    result = scan_commits(command, cwd=CWD)
    assert result.invocations, command
    assert all(i.source == "unknown" for i in result.invocations), (command, result)
    assert all(i.directory == CWD for i in result.invocations), (command, result)


def test_message_file_written_in_same_command_is_unknown():
    _only_unknown("printf 'feat: x\\n' > m.md && git commit -F m.md")
    _only_unknown("printf 'feat: x\\n' | tee m.md && git commit -F m.md")
    inv = one("git add . && git commit -F m.md")
    assert (inv.source, inv.message_path) == ("file", CWD / "m.md")
    assert one("git status && git diff --stat && git commit -F m.md").source == "file"
    assert one("true && git commit -F m.md").source == "file"


@pytest.mark.parametrize("flag", [
    "-F ~/m.md", "-F /tmp/m*.md", "-F <(printf x)", "-F /dev/stdin", "-F /proc/self/fd/0",
    "-F /tmp/{a,b}.md", "-F ''", "--file=", "-F",
])
def test_unsafe_message_file_values_are_unknown(flag):
    _only_unknown(f"git commit {flag}")
    if "<" not in flag:
        _only_unknown(f"cat m.md | git commit {flag}")


def test_empty_attached_long_option_does_not_consume_next_argument():
    _only_unknown("git commit --file= -m x")
    _only_unknown("git commit --message= -F m.md")
    inv = one("git commit --message=")
    assert (inv.source, inv.message_text) == ("literal", "")


def test_hash_inside_word_or_quotes_is_not_a_comment():
    assert one('git commit -m "a # b"').message_text == "a # b"
    assert one("git commit -m a#b").message_text == "a#b"


def test_assignment_and_env_wrapper_allowlist():
    assert one("GIT_AUTHOR_NAME=x GIT_COMMITTER_DATE=y git commit -F m.md").source == "file"
    assert one("env GIT_AUTHOR_EMAIL=a@b git commit -F m.md").source == "file"
    _only_unknown("env -i git commit -F m.md")
    _only_unknown("FOO=bar git commit -F m.md")
    _only_unknown("env FOO=bar git commit -F m.md")


def test_fixup_and_squash_consume_their_argument():
    assert one("git commit --fixup HEAD").source == "auto"
    assert one("git commit --fixup=HEAD~1").source == "auto"
    assert one("git commit --squash HEAD").source == "auto"
    _only_unknown("git commit --fixup amend:HEAD")
    _only_unknown("git commit --fixup=reword:HEAD")
    # `HEAD` is the fixup target, not a message file or message
    _only_unknown("git commit --fixup HEAD -F m.md")  # autosquash line is prepended


def test_unrecognised_wrapper_with_commit_is_reported_unknown():
    for cmd in ("timeout 30 git commit -F m.md", "sudo git commit -F m.md",
                "nice -n 5 git commit -F m.md", "command git commit -F m.md",
                "env -i A=b command git commit -F m.md"):
        inv = _only_unknown(cmd)
        assert inv.directory == CWD


def test_adversarial_input_is_fast_and_never_raises():
    cases = [
        "git " + "-C " * 17000 + "x",
        "git commit -m \"$(cat <<'EOF'\n" * 2000,
        "git " + "-c a=b " * 7000 + "commit -F m.md",
        "(" * 5000 + "git commit -F m.md" + ")" * 5000,
        "'" * 50000,
        "git commit " + "-m x " * 12000,
        "\\\n" * 25000 + "git commit -F m.md",
        "x" * 190000 + "\ngit commit -F m.md",
        "cd a && " * 8000 + "git commit -F m.md",
    ]
    for case in cases:
        start = time.monotonic()
        result = scan_commits(case, cwd=CWD)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, (case[:40], elapsed)
        assert isinstance(result.invocations, tuple)
    deep = scan_commits("git " + "-C x " * 10000 + "commit -F m.md", cwd=CWD)
    assert [i.source for i in deep.invocations] == ["unknown"]
    chain = scan_commits("cd a && " * 8000 + "git commit -F m.md", cwd=CWD)
    assert [i.source for i in chain.invocations] == ["unknown"]


# ---- PR #255 fix wave ---------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git commit -s -F m.md", "git commit --signoff -F m.md",
    "git commit --trailer 'A: b' -F m.md", "git commit --allow-empty-message -F m.md",
    "git commit --dry-run -F m.md", "git commit --short -F m.md",
    "git commit --branch -F m.md", "git commit --porcelain -F m.md",
    "git commit --long -F m.md", "git commit -z -F m.md", "git commit --null -F m.md",
    "git commit -uno -F m.md", "git commit --untracked-files=no -F m.md",
    "git commit --pathspec-from-file=p -F m.md", "git commit --no-message -F m.md",
    "git commit --no-file -F m.md", "git commit --no-verify=1 -F m.md",
    "git commit --mes=x", "git commit --edi -F m.md", "git commit --fil=m.md",
    "git commit --no-signoff -F m.md", "git commit -ax -F m.md", "git commit -sF m.md",
    "git commit -",
])
def test_unlisted_commit_options_are_unknown(command):
    _only_unknown(command)


@pytest.mark.parametrize("command", [
    "git commit -a -q -v -n -F m.md", "git commit --all --quiet --verbose -F m.md",
    "git commit --verify --allow-empty --no-post-rewrite -F m.md",
    "git commit -S -F m.md", "git commit -SABCD -F m.md", "git commit --gpg-sign=K -F m.md",
    "git commit --gpg-sign -F m.md", "git commit --no-gpg-sign -F m.md",
    "git commit --author 'A <a@b>' -F m.md", "git commit --author='A <a@b>' -F m.md",
    "git commit --date 2020-01-01 -F m.md", "git commit --date=now -F m.md",
    "git commit --reset-author -F m.md", "git commit -o -F m.md f", "git commit -i -F m.md f",
    "git commit --only --include --status --no-status -F m.md", "git commit -F m.md -- f g",
    "git commit -aF m.md", "git commit -qaF m.md", "git commit -F m.md a b",
])
def test_listed_commit_options_stay_determinate(command):
    assert one(command).source == "file", command


def test_common_legitimate_forms_stay_determinate():
    assert one("git commit -F m.md").source == "file"
    assert one("git add . && git commit -F m.md").source == "file"
    cmd = "git add -A && git commit -m \"$(cat <<'EOF'\nfeat: x\n\nbody\nEOF\n)\""
    assert one(cmd).message_text == "feat: x\n\nbody"
    assert one("cd /repo && git commit -m x").directory == Path("/repo")
    inv = one("git -C /repo commit -m a -m b")
    assert (inv.directory, inv.message_text) == (Path("/repo"), "a\n\nb")
    assert one('git commit -am "x"').message_text == "x"


def test_cleanup_field_and_effective_message():
    assert one("git commit -m x").cleanup == "default"
    assert one("git commit --cleanup=verbatim -m x").cleanup == "verbatim"
    assert one("git commit --cleanup whitespace -F m.md").cleanup == "whitespace"
    assert one("git commit --cleanup=strip -m x").cleanup == "strip"
    _only_unknown("git commit --cleanup=bogus -m x")
    _only_unknown("git commit --cleanup -m")
    inv = one("git commit -m '' -m 'feat(x): t'")
    assert effective_message(inv.message_text, inv.cleanup) == "feat(x): t\n"
    inv = one("git commit -m 'feat: x  '")
    assert effective_message(inv.message_text, inv.cleanup) == "feat: x\n"
    inv = one("git commit --cleanup=verbatim -m 'a  '")
    assert effective_message(inv.message_text, inv.cleanup) == "a  "
    inv = one("git commit --cleanup=strip -m 'a'")
    assert effective_message(inv.message_text, inv.cleanup) is None
    assert effective_message("a", "scissors") is None
    _only_unknown("git commit --cleanup=verbatim -m '' -m 'feat: t'")


def test_effective_message_stripspace_rules():
    f = lambda raw: effective_message(raw, "whitespace")
    assert f("\n\n a \t\n\n\n\nb\n\n\n") == " a\n\nb\n"
    assert f("y \x0b\n") == "y \x0b\n"   # git keeps \v and \f (measured)
    assert f("") == "" and f("\n \n") == ""
    assert effective_message("x\n\n", "default") == "x\n"
    assert effective_message("x\n\n", "verbatim") == "x\n\n"
    assert f("a\u00a0") == "a\u00a0\n"   # only space, tab, CR, LF are stripped


@pytest.mark.parametrize("command", [
    "cd /x; git commit -F m.md", "cd /x\ngit commit -F m.md",
    "cd /x && git status; git commit -F m.md",
    "git add . && cd /x\ngit commit -m y",
])
def test_cd_before_unconditional_separator_makes_later_commits_unknown(command):
    result = scan_commits(command, cwd=CWD)
    assert result.invocations and all(i.source == "unknown" for i in result.invocations)
    assert all(i.directory == CWD for i in result.invocations)
    assert one("cd /x && git commit -F m.md").source == "file"


@pytest.mark.parametrize("command", [
    "cd .. && git commit -F m.md", "cd a/../b && git commit -F m.md",
    "git -C ../x commit -F m.md", "git -C a/.. commit -F m.md",
    "git commit -F ../m.md", "git commit -F a/../m.md", "git commit --file=../m.md",
    "cd -P /x && git commit -F m.md", "cd -L /x && git commit -F m.md",
    "git -c core.worktree=/x commit -F m.md", "git -c core.bare=true commit -F m.md",
    "git -c include.path=x commit -F m.md", "git -c includeIf.gitdir:/a.path=x commit -F m.md",
    "git -c CORE.WorkTree=/x commit -F m.md",
])
def test_dotdot_physical_paths_and_config_redirects_are_unknown(command):
    _only_unknown(command)


def test_dotdot_lookalikes_stay_determinate():
    assert one("git commit -F ..m.md").source == "file"
    assert one("cd /x/... && git commit -F m.md").directory == Path("/x/...")


@pytest.mark.parametrize("command", [
    "git -c alias.ci=commit ci -F m.md", "git -c alias.commit='commit -s' commit -F m.md",
    "git -c ALIAS.x=y status && git commit -F m.md",
    "git -c alias.st=status st", "git --config-env=alias.ci=V ci -m x",
])
def test_command_line_aliases_yield_unknown(command):
    result = scan_commits(command, cwd=CWD)
    assert result.invocations, command
    assert all(i.source == "unknown" for i in result.invocations), command


@pytest.mark.parametrize("command", [
    "git diff --output=m.md && git commit -F m.md",
    "git diff --output m.md && git commit -F m.md",
    "git diff --stat --outp=m.md && git commit -F m.md",
    "git add --pathspec-from-file=p && git commit -F m.md",
    "git rm --pathspec-from-file p && git commit -F m.md",
])
def test_writers_before_commit_make_it_unknown(command):
    _only_unknown(command)
    assert one("git diff --stat && git add -A && git rm f && git commit -F m.md").source == "file"


def test_heredoc_search_is_linear_on_adversarial_shape():
    opener = "git commit -m \"$(cat <<'EOF'\n"
    cmd = opener * 3000 + "EOFX\n" * 3000 + "EOF\n"
    assert len(cmd) < 200_000
    start = time.monotonic()
    result = scan_commits(cmd, cwd=CWD)
    assert time.monotonic() - start < 1.0
    assert isinstance(result.invocations, tuple)


@pytest.mark.parametrize("command", [
    'git -C "my dir" commit -m x | cat',
    'git -c user.name="A B" commit -m x | cat',
    'git -C "a;b" commit -m x | cat',
])
def test_loose_fallback_counts_commit_after_quoted_option_values(command):
    _only_unknown(command)


@pytest.mark.parametrize("command", [
    "git -c commit.cleanup=verbatim commit -m 'a  '",
    "git -c core.commentChar=';' commit -m x",
    "git -c CORE.COMMENTSTRING=// commit -m x",
    "git -c commit.template=t.txt commit -m x",
    "git --config-env=commit.cleanup=CLEAN commit -m x",
    "git --config-env core.commentChar=CC commit -m x",
])
def test_message_changing_config_overrides_are_unknown(command):
    _only_unknown(command)


def test_unrelated_config_override_stays_literal():
    assert one("git -c user.name=x commit -m 'feat: t'").source == "literal"


@pytest.mark.parametrize("command", [
    "git commit --squash HEAD -F msg",
    "git commit --fixup HEAD -m x",
    "git commit --fixup=HEAD --message=x",
    "git commit --squash=HEAD -m x",
    "git commit -m x --fixup HEAD",
])
def test_autosquash_with_message_source_is_unknown(command):
    _only_unknown(command)


def test_autosquash_alone_stays_auto():
    assert one("git commit --fixup HEAD").source == "auto"
    assert one("git commit --squash=HEAD").source == "auto"


def test_heredoc_search_is_linear_for_many_distinct_delimiters():
    cmd = "".join(f"git commit -m \"$(cat <<'D{i}'\n" for i in range(5000))
    assert len(cmd) < 200_000
    start = time.monotonic()
    result = scan_commits(cmd, cwd=CWD)
    assert time.monotonic() - start < 1.0
    assert isinstance(result.invocations, tuple)


def test_heredoc_with_many_distinct_delimiters_still_extracts():
    cmd = "".join(f"git commit -m \"$(cat <<'D{i}'\nm{i}\nD{i}\n)\"\n" for i in range(50))
    result = scan_commits(cmd, cwd=CWD)
    assert [i.message_text for i in result.invocations] == [f"m{i}" for i in range(50)]


_GIT = "/usr/bin/git"


def _recorded(args, cwd, env):
    subprocess.run([_GIT, "commit", "-q", "--allow-empty", *args], cwd=cwd, env=env, check=True)
    out = subprocess.run([_GIT, "cat-file", "commit", "HEAD"], cwd=cwd, env=env,
                         check=True, capture_output=True).stdout.decode()
    return out.split("\n\n", 1)[1]


@pytest.mark.skipif(not os.path.exists(_GIT), reason="needs /usr/bin/git")
@pytest.mark.parametrize("command,flags,expect", [
    ("git commit --cleanup=verbatim -m a", ["--cleanup=verbatim", "-m", "a"], "a\n"),
    ("git commit --cleanup=verbatim -m a -m b",
     ["--cleanup=verbatim", "-m", "a", "-m", "b"], "a\n\nb\n"),
    ("git commit -m a", ["-m", "a"], "a\n"),
    ("git commit --cleanup=verbatim -m 'a\n' -m b",
     ["--cleanup=verbatim", "-m", "a\n", "-m", "b"], "a\n\nb\n"),
])
def test_effective_message_literal_matches_real_git(tmp_path, command, flags, expect):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run([_GIT, "init", "-q", str(tmp_path)], check=True, env=env)
    inv = one(command)
    got = effective_message(inv.message_text, inv.cleanup, from_literal=True)
    assert got == expect
    assert got == _recorded(flags, tmp_path, env)


def test_effective_message_literal_appends_newline_only_when_needed():
    assert effective_message("a", "verbatim", from_literal=True) == "a\n"
    assert effective_message("a\n", "verbatim", from_literal=True) == "a\n"
    assert effective_message("", "verbatim", from_literal=True) == ""
    assert effective_message("a", "default", from_literal=True) == "a\n"
    # file bytes are kept as git reads them (measured: verbatim file without
    # trailing newline is recorded unchanged)
    assert effective_message("nofinal", "verbatim") == "nofinal"


def test_control_character_command_is_unknown():
    inv = one('git commit --cleanup=verbatim -m "title"\r')
    assert inv.source == "unknown"
    assert "control character" in inv.reason
    inv = one('git commit -m "a\x01b"')
    assert inv.source == "unknown"
    assert "control character" in inv.reason
    inv = one('git commit -m "a\x7fb"')
    assert inv.source == "unknown"


def test_control_character_without_commit_has_no_invocation():
    assert scan_commits("git status\r", cwd=CWD).invocations == ()


def test_newline_and_tab_stay_determinate():
    heredoc = (
        "git commit --cleanup=verbatim -m \"$(cat <<'EOF'\n"
        "feat: x\n\nbody\tTabbed\nEOF\n)\""
    )
    assert one(heredoc).source == "literal"
    assert one('git commit --cleanup=verbatim -m "a\tb"').source == "literal"
