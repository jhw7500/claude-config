import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER_PATH = REPO / "scripts" / "install-commit-gate.py"
TRIBUNAL_PATH = REPO / "scripts" / "install-pre-pr-tribunal.py"
CLAUDE_COMMAND = "/usr/bin/python3 $HOME/.local/share/claude-config/commit_gate/claude_hook.py"
CODEX_COMMAND = "/usr/bin/python3 $HOME/.local/share/claude-config/commit_gate/codex_hook.py"
TRIBUNAL_CLAUDE = (
    "/usr/bin/python3 $HOME/.local/share/claude-config/pre_pr_tribunal/claude_hook.py"
)
TRIBUNAL_CODEX = (
    "/usr/bin/python3 $HOME/.local/share/claude-config/pre_pr_tribunal/codex_hook.py"
)
PACKAGE_NAMES = (
    "__init__.py",
    "claude_hook.py",
    "cli.py",
    "codex_hook.py",
    "commit_scan.py",
    "gate.py",
    "grant.py",
    "hook_io.py",
    "repo.py",
)
VENDOR_NAMES = (
    "check_commit_message.py",
    "commit_title_rules.py",
    "validate_change_evidence.py",
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def installer():
    return _load("install_commit_gate", INSTALLER_PATH)


@pytest.fixture(scope="module")
def tribunal():
    return _load("install_pre_pr_tribunal_for_gate", TRIBUNAL_PATH)


@pytest.fixture
def home(tmp_path):
    value = tmp_path / "home"
    value.mkdir(mode=0o700)
    return value


def _source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "hooks").mkdir(parents=True)
    (source / "scripts").mkdir()
    shutil.copytree(
        REPO / "hooks" / "commit_gate",
        source / "hooks" / "commit_gate",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(
        REPO / "scripts" / "sync-commit-gate-vendor.py",
        source / "scripts" / "sync-commit-gate-vendor.py",
    )
    return source


def _apply(installer, repo, home):
    return installer.apply_transaction(installer.build_plan(repo, home), namespace="commit-gate")


def _managed(groups, command):
    return [g for g in groups if g.get("hooks") == [{"type": "command", "command": command}]]


def test_install_copies_package_and_vendor(installer, home):
    _apply(installer, REPO, home)
    package = home / ".local/share/claude-config/commit_gate"
    for name in PACKAGE_NAMES:
        assert (package / name).read_bytes() == (REPO / "hooks/commit_gate" / name).read_bytes()
    for name in (*VENDOR_NAMES, "SOURCE.json"):
        assert (package / "vendor" / name).read_bytes() == (
            REPO / "hooks/commit_gate/vendor" / name
        ).read_bytes()
    installed = {p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file()}
    assert installed == {
        *PACKAGE_NAMES,
        *(f"vendor/{n}" for n in (*VENDOR_NAMES, "SOURCE.json")),
    }


def test_install_merges_claude_and_codex_hooks_idempotently(installer, home):
    _apply(installer, REPO, home)
    first = [
        (home / ".claude/settings.json").read_bytes(),
        (home / ".codex/hooks.json").read_bytes(),
    ]
    _apply(installer, REPO, home)
    claude = json.loads((home / ".claude/settings.json").read_bytes())
    codex = json.loads((home / ".codex/hooks.json").read_bytes())
    assert _managed(claude["hooks"]["PreToolUse"], CLAUDE_COMMAND) == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": CLAUDE_COMMAND}]}
    ]
    assert _managed(codex["hooks"]["PreToolUse"], CODEX_COMMAND) == [
        {"hooks": [{"type": "command", "command": CODEX_COMMAND}]}
    ]
    assert len(claude["hooks"]["PreToolUse"]) == 1
    assert len(codex["hooks"]["PreToolUse"]) == 1
    assert first == [
        (home / ".claude/settings.json").read_bytes(),
        (home / ".codex/hooks.json").read_bytes(),
    ]


def test_install_aborts_on_vendor_drift(installer, tmp_path, home):
    source = _source_repo(tmp_path)
    vendor = source / "hooks/commit_gate/vendor/commit_title_rules.py"
    vendor.write_bytes(vendor.read_bytes() + b"# drift\n")
    with pytest.raises(installer.InstallError, match="vendor"):
        installer.build_plan(source, home)
    assert installer.main(["--repo", str(source), "--home", str(home)]) == 1
    assert not (home / ".local").exists()
    assert not (home / ".claude").exists()
    assert not (home / ".codex").exists()


def test_tribunal_entries_untouched(installer, tribunal, home):
    tribunal_claude = tribunal.merge_runtime_configs(home, TRIBUNAL_CLAUDE, TRIBUNAL_CODEX)
    (home / ".claude").mkdir(mode=0o700)
    (home / ".codex").mkdir(mode=0o700)
    (home / ".claude/settings.json").write_bytes(tribunal_claude[0])
    (home / ".codex/hooks.json").write_bytes(tribunal_claude[1])
    before = [json.loads(b) for b in tribunal_claude]
    _apply(installer, REPO, home)
    after = [
        json.loads((home / ".claude/settings.json").read_bytes()),
        json.loads((home / ".codex/hooks.json").read_bytes()),
    ]
    for old, new, command in (
        (before[0], after[0], TRIBUNAL_CLAUDE),
        (before[1], after[1], TRIBUNAL_CODEX),
    ):
        assert _managed(new["hooks"]["PreToolUse"], command) == _managed(
            old["hooks"]["PreToolUse"], command
        )
        assert new["hooks"]["UserPromptSubmit"] == old["hooks"]["UserPromptSubmit"]
        assert len(new["hooks"]["PreToolUse"]) == len(old["hooks"]["PreToolUse"]) + 1
