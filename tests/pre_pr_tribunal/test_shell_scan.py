import pytest

from pre_pr_tribunal.shell_scan import (
    MAX_COMMAND_BYTES,
    MAX_RECURSION,
    MAX_TOKENS,
    ScanKind,
    ScanResult,
    scan_pr_create,
)


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create",
        "/usr/bin/gh pr create --base master",
        "./tools/gh pr create",
        "FOO=1 command gh pr create",
        "FOO='two words' gh pr create",
        "command -- gh pr create",
        "env FOO=1 gh --repo owner/repo pr create",
        "env -i -u OLD -C /tmp FOO=1 gh pr create",
        "env --ignore-environment --unset=OLD --chdir=/tmp gh pr create",
        "tests && gh pr create",
        "tests || gh pr create",
        "tests; gh pr create",
        "tests | gh pr create",
        "tests & gh pr create",
        "tests\ngh pr create",
        "(gh pr create)",
        "echo $(gh pr create)",
        'echo "$(gh pr create)"',
        "FOO=$(gh pr create) true",
        "echo $(printf x; command -- gh pr create)",
        "echo `gh pr create`",
        'echo "`gh pr create`"',
        "echo >$(gh pr create) output",
        "bash -c 'gh pr create --draft'",
        'dash -c "gh pr create --draft"',
        "/bin/sh -c 'gh pr create' shell-name",
        "g\\\nh pr create",
        "gh pr \\\ncreate",
        "<input gh pr create",
        "2>/tmp/pr-create.log gh pr create",
    ],
)
def test_detects_executable_pr_create(command):
    assert scan_pr_create(command).kind is ScanKind.PR_CREATE


@pytest.mark.parametrize(
    "command",
    [
        "echo 'gh pr create'",
        'printf "%s\\n" "gh pr create"',
        "true # gh pr create",
        "cat <<'EOF'\ngh pr create\nEOF\n",
        "cat <<-EOF\n\tgh pr create\n\tEOF\n",
        "cat <<A <<'B'\ngh pr create\nA\ngh pr create\nB\n",
        "rg 'gh pr create' docs",
        "gh pr view",
        "gh issue create",
        "printf gh pr create",
        "echo gh > 'pr create'",
        "echo > gh pr create",
        "echo '$(gh pr create)'",
        "printf /usr/bin/gh pr create",
        "gh\npr create",
        "X=gh pr create",
        '"FOO=1" gh pr create',
        "bash -c gh pr create",
        "bash -ec 'gh pr create'",
        'bash -c "gh pr create $DYNAMIC"',
        "echo './gh\npr create'",
        "echo 'gh\x01pr create'",
        "true;\rgh pr create",
        "touch 'file\ngh pr create'",
        "touch $'file\\ngh pr create'",
        "gh --repo owner/repo issue create",
        "env -u",
    ],
)
def test_ignores_data_and_non_create_subcommands(command):
    assert scan_pr_create(command).kind is ScanKind.NO_MATCH


@pytest.mark.parametrize(
    "command",
    [
        "gh --repo owner/repo pr create",
        "gh --repo=owner/repo pr create",
        "gh -R owner/repo pr create",
        "gh -Rowner/repo pr create",
        "gh --hostname github.example pr create",
        "gh --config /tmp/gh.yml pr create",
        "gh --help --version pr create",
    ],
)
def test_skips_only_supported_gh_global_options(command):
    assert scan_pr_create(command).kind is ScanKind.PR_CREATE


@pytest.mark.parametrize(
    "command",
    [
        "env --future gh pr create",
        "env -Z gh pr create",
        "command -v gh pr create",
        "gh --future pr create",
        "gh --repo",
    ],
)
def test_unknown_or_incomplete_wrapper_and_global_options_are_ambiguous(command):
    result = scan_pr_create(command)
    assert result.kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert result.reason is not None


def test_unclosed_candidate_is_ambiguous_but_unrelated_unclosed_quote_is_not():
    assert scan_pr_create("gh pr create '").kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert scan_pr_create("printf '").kind is ScanKind.NO_MATCH


@pytest.mark.parametrize(
    "command",
    [
        "echo $(gh pr create",
        "echo `gh pr create",
    ],
)
def test_malformed_executable_substitution_with_candidate_is_ambiguous(command):
    assert scan_pr_create(command).kind is ScanKind.AMBIGUOUS_CANDIDATE


@pytest.mark.parametrize(
    "command",
    [
        "echo $('gh pr create'",
        "echo `printf gh pr create",
        "cat <<EOF\ngh pr create",
    ],
)
def test_malformed_data_without_unquoted_command_candidate_is_no_match(command):
    assert scan_pr_create(command).kind is ScanKind.NO_MATCH


def test_command_byte_limit_is_exact_and_utf8_based():
    prefix = "gh pr create #"
    at_limit = prefix + "x" * (MAX_COMMAND_BYTES - len(prefix))
    over_limit = at_limit + "x"

    assert len(at_limit.encode("utf-8")) == MAX_COMMAND_BYTES
    assert scan_pr_create(at_limit).kind is ScanKind.PR_CREATE
    result = scan_pr_create(over_limit)
    assert result.kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert result.reason == "COMMAND_LIMIT"

    unicode_prefix = "printf #"
    unrelated = unicode_prefix + "é" * (
        (MAX_COMMAND_BYTES - len(unicode_prefix)) // 2 + 1
    )
    result = scan_pr_create(unrelated)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "COMMAND_LIMIT"

    quoted_data = "echo 'gh pr create " + "x" * MAX_COMMAND_BYTES + "'"
    result = scan_pr_create(quoted_data)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "COMMAND_LIMIT"


def test_token_limit_is_exact():
    at_limit = "gh pr create " + " ".join(["arg"] * (MAX_TOKENS - 3))
    over_limit = at_limit + " extra"
    unrelated = "printf " + " ".join(["arg"] * MAX_TOKENS)
    data_candidate = unrelated + " gh pr create"
    late_candidate = "printf " + " ".join(["arg"] * MAX_TOKENS) + "; gh pr create"

    assert scan_pr_create(at_limit).kind is ScanKind.PR_CREATE
    result = scan_pr_create(over_limit)
    assert result.kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert result.reason == "TOKEN_LIMIT"
    result = scan_pr_create(unrelated)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "TOKEN_LIMIT"
    result = scan_pr_create(data_candidate)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "TOKEN_LIMIT"
    result = scan_pr_create(late_candidate)
    assert result.kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert result.reason == "TOKEN_LIMIT"


def test_recursion_depth_17_is_bounded_not_executed():
    at_limit = "$(" * MAX_RECURSION + "gh pr create" + ")" * MAX_RECURSION
    too_deep = "$(" + at_limit + ")"
    unrelated = "$(" * (MAX_RECURSION + 1) + "printf x" + ")" * (
        MAX_RECURSION + 1
    )
    quoted_data = "$(" * (MAX_RECURSION + 1) + "printf 'gh pr create'" + ")" * (
        MAX_RECURSION + 1
    )

    assert scan_pr_create(at_limit).kind is ScanKind.PR_CREATE
    result = scan_pr_create(too_deep)
    assert result.kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert result.reason == "RECURSION_LIMIT"
    result = scan_pr_create(unrelated)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "RECURSION_LIMIT"
    result = scan_pr_create(quoted_data)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "RECURSION_LIMIT"


def test_candidate_hint_is_bounded_for_extreme_depth_and_token_volume():
    extreme_depth = 5_000
    deep_candidate = "$(" * extreme_depth + "gh pr create" + ")" * extreme_depth
    deep_data = "$(" * extreme_depth + "printf x" + ")" * extreme_depth
    token_heavy_candidate = "x;" * 20_000 + "gh pr create"
    token_heavy_data = "x;" * 20_000 + "printf gh pr create"

    result = scan_pr_create(deep_candidate)
    assert result.kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert result.reason == "RECURSION_LIMIT"
    result = scan_pr_create(deep_data)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "RECURSION_LIMIT"
    result = scan_pr_create(token_heavy_candidate)
    assert result.kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert result.reason == "TOKEN_LIMIT"
    result = scan_pr_create(token_heavy_data)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "TOKEN_LIMIT"


def test_deep_hint_preserves_parent_state_after_quoted_substitution():
    nesting = MAX_RECURSION + 3
    prefix = "$(" * nesting
    suffix = ")" * nesting
    candidate = prefix + 'FOO="$(printf x)" gh pr create' + suffix
    quoted_data = prefix + 'echo "$(printf x) gh pr create"' + suffix
    argument_data = prefix + 'FOO="$(printf x)" printf gh pr create' + suffix

    result = scan_pr_create(candidate)
    assert result.kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert result.reason == "RECURSION_LIMIT"
    result = scan_pr_create(quoted_data)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "RECURSION_LIMIT"
    result = scan_pr_create(argument_data)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "RECURSION_LIMIT"


@pytest.mark.parametrize("redirection", ["2>/tmp/x", "3>>/tmp/x"])
def test_token_limited_hint_skips_numeric_fd_redirection(redirection):
    exhausted = "x;" * MAX_TOKENS
    candidate = f"{exhausted}{redirection} gh pr create"
    argument_data = f"{exhausted}{redirection} printf gh pr create"

    result = scan_pr_create(candidate)
    assert result.kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert result.reason == "TOKEN_LIMIT"
    result = scan_pr_create(argument_data)
    assert result.kind is ScanKind.NO_MATCH
    assert result.reason == "TOKEN_LIMIT"


def test_deep_backtick_child_preserves_outer_argument_context():
    command = (
        "echo "
        + "$(" * (MAX_RECURSION + 1)
        + "`$(printf x)`"
        + ")" * (MAX_RECURSION + 1)
        + " gh pr create"
    )

    assert scan_pr_create(command) == ScanResult(
        ScanKind.NO_MATCH, "RECURSION_LIMIT"
    )


def test_escaped_digit_is_not_a_numeric_fd_hint():
    command = ("x;" * MAX_TOKENS) + r"\2>/tmp/x gh pr create"

    assert scan_pr_create(command) == ScanResult(ScanKind.NO_MATCH, "TOKEN_LIMIT")


@pytest.mark.parametrize(
    "command",
    [
        "echo &>out gh pr create",
        "echo &>>out gh pr create",
        "echo &>gh pr create",
        "echo &>>'gh pr create'",
    ],
)
def test_bash_compound_redirection_operand_and_following_words_are_data(command):
    assert scan_pr_create(command).kind is ScanKind.NO_MATCH


def test_command_boundary_after_bash_compound_redirection_is_still_executable():
    assert scan_pr_create("echo &>out; gh pr create").kind is ScanKind.PR_CREATE


@pytest.mark.parametrize(
    "command",
    [
        "echo $((gh pr create))",
        "((gh pr create))",
    ],
)
def test_arithmetic_text_is_not_an_executable_command_context(command):
    assert scan_pr_create(command).kind is ScanKind.NO_MATCH


@pytest.mark.parametrize(
    "command",
    [
        "echo $(( $(gh pr create) + 1 ))",
        "(( $(gh pr create) ))",
    ],
)
def test_command_substitution_inside_arithmetic_is_executable(command):
    assert scan_pr_create(command).kind is ScanKind.PR_CREATE


def test_comment_cannot_supply_a_redirection_operand():
    candidate = scan_pr_create("gh pr create > # missing operand")
    unrelated = scan_pr_create("printf > # missing operand")

    assert candidate.kind is ScanKind.AMBIGUOUS_CANDIDATE
    assert candidate.reason == "PARSE_ERROR"
    assert unrelated.kind is ScanKind.NO_MATCH
    assert unrelated.reason == "PARSE_ERROR"


@pytest.mark.parametrize("command", [None, b"gh pr create", "gh\x00pr create"])
def test_non_strings_and_nul_are_deterministic_no_match(command):
    assert scan_pr_create(command).kind is ScanKind.NO_MATCH
