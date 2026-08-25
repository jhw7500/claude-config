import os
import subprocess
from pathlib import Path

import pytest


SOURCE_ROOT = Path(__file__).parents[1]
PLUG_SCRIPT = SOURCE_ROOT / "shell" / "plug.sh"


def run_plug(tmp_path: Path, action: str, key: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argv_log = tmp_path / "claude.argv"
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CLAUDE_ARGV_LOG"\n',
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    env = os.environ.copy()
    env["CLAUDE_ARGV_LOG"] = str(argv_log)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'export PATH="$4:$PATH"\n. "$1"\nplug "$2" "$3"',
            "plug-test",
            str(PLUG_SCRIPT),
            action,
            key,
            str(fake_bin),
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    return result, argv_log


@pytest.mark.parametrize(
    ("action", "expected_verb"),
    [
        ("on", "enable"),
        ("off", "disable"),
    ],
)
@pytest.mark.parametrize(
    ("key", "expected_name"),
    [
        ("bkit", "bkit@bkit-marketplace"),
        ("docs", "document-skills@anthropic-agent-skills"),
        ("pw", "playwright@claude-plugins-official"),
        ("pyright", "pyright-lsp@claude-plugins-official"),
        ("compound", "compound-engineering@every-marketplace"),
    ],
)
def test_plug_maps_each_alias_and_action_to_claude_cli_argv(
    tmp_path,
    action,
    expected_verb,
    key,
    expected_name,
):
    result, argv_log = run_plug(tmp_path, action, key)
    diagnostics = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    assert result.returncode == 0, diagnostics
    assert argv_log.read_text(encoding="utf-8").splitlines() == [
        "plugin",
        expected_verb,
        expected_name,
    ]
    assert "/reload-plugins" in result.stdout


def test_plug_rejects_unknown_action_without_invoking_claude(tmp_path):
    result, argv_log = run_plug(tmp_path, "status", "bkit")
    diagnostics = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    assert result.returncode == 1, diagnostics
    assert not argv_log.exists(), diagnostics
    assert "unknown action: status" in result.stdout + result.stderr
