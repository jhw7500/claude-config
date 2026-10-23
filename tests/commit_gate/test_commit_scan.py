import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from commit_gate.commit_scan import scan_commits  # noqa: E402

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
    assert one("cd sub; git -c a=b commit -F m.md").directory == CWD / "sub"


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
    assert one("env -i A=b command git commit -F m.md").source == "file"
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
