import json
import os
import runpy
import shlex
import subprocess
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "hooks"
    / "verification-command-hygiene-hook.py"
)


def run_payload(
    payload: object,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_raw(json.dumps(payload, ensure_ascii=False), env_extra)


def run_raw(
    raw: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("CLAUDE_SKIP_VERIFICATION_COMMAND_HYGIENE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=raw,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def run_hook(
    command: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_payload(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
        env_extra,
    )


def test_warns_when_fallback_injects_arrow_conclusion() -> None:
    result = run_hook('rg "CONFIG_BT" . || echo "없음 → CONFIG_X 뿐"')

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_warns_when_fallback_claims_only() -> None:
    result = run_hook('rg "CONFIG_BT" . || echo "CONFIG_X의 select뿐"')

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_arrow_outside_fallback_does_not_warn() -> None:
    result = run_hook(
        'echo "before → after"; rg "CONFIG_BT" . || echo "(no match)"'
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_simple_absence_fallback_stays_silent() -> None:
    result = run_hook('test -f report.txt || echo "파일 없음"')

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_neutral_no_match_fallback_stays_silent() -> None:
    result = run_hook('rg "CONFIG_BT" . || echo "(no match)"')

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_quoted_search_pattern_is_not_treated_as_shell_fallback() -> None:
    result = run_hook(
        "rg -F '|| echo \"없음 → X 뿐\"' README.md || echo \"(no match)\""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_quoted_or_token_is_not_treated_as_shell_operator() -> None:
    result = run_hook("printf '%s\\n' '||' echo 'X뿐'")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_warns_for_multiline_fallback_with_echo_option() -> None:
    result = run_hook("rg CONFIG_BT . ||\n  echo -e '없음 → CONFIG_X 뿐'")

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_camel_case_payload_is_supported() -> None:
    result = run_payload(
        {
            "hookEventName": "PreToolUse",
            "toolName": "Bash",
            "toolInput": {"command": "rg CONFIG_BT . || echo 'X뿐'"},
        }
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_kill_switch_stays_silent() -> None:
    result = run_hook(
        'rg "CONFIG_BT" . || echo "없음 → CONFIG_X 뿐"',
        {"CLAUDE_SKIP_VERIFICATION_COMMAND_HYGIENE": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_non_bash_and_non_object_payloads_stay_silent() -> None:
    non_bash = run_payload(
        {"tool_name": "Write", "tool_input": {"command": "x || echo 'X뿐'"}}
    )
    non_object = run_payload([1, 2, 3])

    assert non_bash.returncode == 0, non_bash.stderr
    assert non_bash.stdout == ""
    assert non_object.returncode == 0, non_object.stderr
    assert non_object.stdout == ""


def test_comment_text_is_not_treated_as_shell_fallback() -> None:
    result = run_hook("true # example || echo 'X뿐'\n")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_heredoc_body_is_not_treated_as_shell_fallback() -> None:
    result = run_hook("cat <<'EOF'\nexample || echo 'X뿐'\nEOF\n")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_quoted_heredoc_backslash_does_not_hide_later_fallback() -> None:
    command = (
        "cat <<'EOF'\n"
        + "literal "
        + "\\"
        + "\n"
        + "EOF\n"
        + "rg CONFIG_BT . || echo '없음 → X뿐'\n"
    )
    result = run_hook(command)

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_arithmetic_left_shift_does_not_hide_later_fallback() -> None:
    result = run_hook(
        "(( shifted = 1 << 2 ))\n"
        "rg CONFIG_BT . || echo '없음 → X뿐'\n"
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_multiline_arithmetic_left_shift_does_not_hide_fallback() -> None:
    result = run_hook(
        "((\n"
        "  shifted = 1 << 2\n"
        "))\n"
        "rg CONFIG_BT . || echo '없음 → X뿐'\n"
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_continued_heredoc_declaration_does_not_hide_later_fallback() -> None:
    command = (
        "cat <<EO"
        + "\\"
        + "\n"
        + "F\nliteral\nEOF\n"
        + "rg CONFIG_BT . || echo '없음 → X뿐'\n"
    )
    result = run_hook(command)

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_double_quoted_heredoc_delimiter_keeps_literal_backslash() -> None:
    result = run_hook(
        'cat <<"E\\OF" >/dev/null\n'
        "text\n"
        "E\\OF\n"
        "rg CONFIG_BT . || echo '없음 → X뿐'\n"
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_multiline_single_quoted_heredoc_text_does_not_hide_fallback() -> None:
    result = run_hook(
        "printf '%s\\n' 'documentation\n"
        "<<EOF\n"
        "literal text'\n"
        "rg CONFIG_BT . || echo '없음 → X뿐'\n"
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_multiline_double_quoted_heredoc_text_does_not_hide_fallback() -> None:
    result = run_hook(
        "printf '%s\\n' \"documentation\n"
        "<<EOF\n"
        "literal text\"\n"
        "rg CONFIG_BT . || echo '없음 → X뿐'\n"
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_warns_inside_double_quoted_command_substitution() -> None:
    result = run_hook("echo \"$(rg CONFIG_BT . || echo '없음 → X뿐')\"")

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_case_pattern_parenthesis_does_not_close_command_substitution() -> None:
    result = run_hook(
        'echo "$(case x in x) true;; esac; '
        "rg CONFIG_BT . || echo '없음 → X뿐')\""
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_nested_substitution_in_case_pattern_keeps_outer_boundary() -> None:
    result = run_hook(
        'echo "$(case x in $(printf x)) true;; esac; '
        "rg CONFIG_BT . || echo '없음 → X뿐')\""
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_quoted_parentheses_do_not_consume_boundary_budget() -> None:
    quoted_parentheses = ")" * 64
    result = run_hook(
        'echo "$(printf %s \''
        + quoted_parentheses
        + "'; rg CONFIG_BT . || echo '없음 → X뿐')\""
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_heredoc_parenthesis_does_not_close_command_substitution() -> None:
    result = run_hook(
        'echo "$(cat <<\'EOF\'\n'
        ")\n"
        "EOF\n"
        "rg CONFIG_BT . || echo '없음 → X뿐'\n"
        ')"'
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_boundary_parser_never_executes_substitution(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    result = run_hook(
        'echo "$(touch '
        + shlex.quote(str(marker))
        + "; rg CONFIG_BT . || echo '없음 → X뿐')\""
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout
    assert not marker.exists()


def test_single_quoted_command_substitution_is_literal() -> None:
    result = run_hook("echo '$(rg CONFIG_BT . || echo \"없음 → X뿐\")'")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_warns_inside_double_quoted_backtick_substitution() -> None:
    result = run_hook("echo \"`rg CONFIG_BT . || echo '없음 → X뿐'`\"")

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_parenthesis_in_substitution_comment_does_not_close_scope() -> None:
    result = run_hook(
        "echo \"$(printf x # ignored )\n"
        "rg CONFIG_BT . || echo '없음 → X뿐'\n"
        ")\""
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_warns_after_backslash_newline_continuation() -> None:
    result = run_hook("rg CONFIG_BT . || \\\n  echo '없음 → X뿐'")

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_marker_in_trailing_comment_stays_silent() -> None:
    result = run_hook(
        'rg CONFIG_BT . || echo "(no match)" # → explanation, not echo output'
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_marker_in_following_newline_command_stays_silent() -> None:
    result = run_hook(
        'rg CONFIG_BT . || echo "(no match)"\necho "→ later summary"'
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_marker_in_redirection_target_stays_silent() -> None:
    result = run_hook('rg CONFIG_BT . || echo "(no match)" > "report→.txt"')

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_marker_in_process_substitution_target_stays_silent() -> None:
    result = run_hook(
        'rg CONFIG_BT . || echo "(no match)" < <(printf X뿐)'
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_real_fallback_inside_process_substitution_warns() -> None:
    result = run_hook(
        'rg CONFIG_BT . || echo "(no match)" '
        "< <(rg INNER . || echo '없음 → X뿐')"
    )

    assert result.returncode == 0, result.stderr
    assert "[VERIFICATION-COMMAND-HYGIENE]" in result.stdout


def test_process_substitution_boundaries_are_computed_once() -> None:
    namespace = runpy.run_path(str(SCRIPT))
    detector = namespace["has_conclusion_fallback"]
    hook_subprocess = namespace["subprocess"]
    real_run = hook_subprocess.run
    calls = 0

    def counted_run(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return real_run(*args, **kwargs)

    hook_subprocess.run = counted_run
    try:
        detected = detector("false || echo no < <(true); " * 40)
    finally:
        hook_subprocess.run = real_run

    assert detected is False
    assert calls <= 40


def test_exhausted_shared_boundary_budget_fails_open() -> None:
    namespace = runpy.run_path(str(SCRIPT))
    detector = namespace["has_conclusion_fallback"]
    hook_subprocess = namespace["subprocess"]
    real_run = hook_subprocess.run
    calls = 0

    def counted_run(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return real_run(*args, **kwargs)

    command = 'false || echo "(no match)" ' + "< <(printf X뿐) " * 65
    hook_subprocess.run = counted_run
    try:
        detected = detector(command)
    finally:
        hook_subprocess.run = real_run

    assert detected is False
    assert calls <= 64


def test_empty_and_malformed_json_stay_silent() -> None:
    empty = run_raw("")
    malformed = run_raw("{not-json")

    assert empty.returncode == 0, empty.stderr
    assert empty.stdout == ""
    assert malformed.returncode == 0, malformed.stderr
    assert malformed.stdout == ""
