"""install.sh 회귀 테스트 — 임시 HOME 에서만 돈다. 실제 사용자 HOME 을 건드리지 않는다."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).parents[1]
INSTALL = REPO / "install.sh"
PRIVATE_FILE = REPO / "scripts" / "lib" / "private-file.sh"


def run_install(
    home: Path,
    umask: str = "022",
    *,
    path: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, HOME=str(home))
    if path is not None:
        env["PATH"] = path
    if extra_env is not None:
        env.update(extra_env)
    # permissive umask 에서도 결과 mode 가 결정적이어야 한다
    return subprocess.run(
        ["/bin/bash", "-c", f'umask {umask}; exec /bin/bash "{INSTALL}"'],
        env=env, text=True, capture_output=True, check=False,
    )


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak.*"))


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_installs_with_deterministic_modes(home: Path) -> None:
    result = run_install(home)

    assert result.returncode == 0, result.stderr
    claude = home / ".claude"
    assert mode_of(claude) == 0o700
    assert mode_of(claude / "CLAUDE.md") == 0o600
    guidance = claude / "global-guidance.md"
    assert mode_of(guidance) == 0o644
    assert not guidance.stat().st_mode & stat.S_IXUSR      # 실행 비트 없음


def test_fresh_install_creates_private_settings_with_all_non_notion_hooks(home: Path) -> None:
    settings = home / ".claude" / "settings.json"
    assert not settings.exists()

    result = run_install(home)

    assert result.returncode == 0, result.stderr
    assert settings.is_file(), result.stdout
    assert mode_of(settings) == 0o600
    # The generic Claude writer creates settings first; the dedicated task-nudge
    # transaction then records that just-created configuration before its merge.
    assert len(backups(settings)) == 1
    assert mode_of(backups(settings)[0]) == 0o600

    data = json.loads(settings.read_text(encoding="utf-8"))
    wired = [
        (event, hook["command"])
        for event, groups in data["hooks"].items()
        for group in groups
        for hook in group["hooks"]
    ]
    expected = {
        ("UserPromptSubmit", "python3 $HOME/.claude/scripts/timestamp-hook.py prompt"),
        ("Stop", "python3 $HOME/.claude/scripts/timestamp-hook.py stop"),
        ("Stop", "python3 $HOME/.claude/scripts/stop-text-required.py"),
        ("UserPromptSubmit", "python3 $HOME/.claude/hooks/general-continuation-hook.py"),
        ("PreToolUse", "python3 $HOME/.claude/hooks/bg-task-progress-hook.py"),
        ("PostToolUse", "python3 $HOME/.claude/hooks/bg-task-progress-hook.py"),
        ("SubagentStop", "python3 $HOME/.claude/hooks/bg-task-progress-hook.py"),
        ("UserPromptSubmit", "python3 $HOME/.claude/hooks/delegate-nudge-hook.py"),
        ("PostToolUse", "python3 $HOME/.claude/hooks/post-info-tool-continuation-hook.py"),
        ("PreToolUse", "python3 $HOME/.claude/hooks/agent-name-delivery-hook.py"),
        ("UserPromptSubmit", "python3 $HOME/.claude/hooks/handoff-checkpoint-hook.py"),
            ("PostToolUse", "python3 $HOME/.claude/hooks/control-char-guard-hook.py"),
            ("PreToolUse", "$HOME/.claude/hooks/task-nudge.sh"),
            ("PreCompact", "$HOME/.claude/hooks/precompact-handoff.sh"),
        }
    assert len(wired) == 14
    assert set(wired) == expected


def test_identical_rerun_adds_no_backup(home: Path) -> None:
    run_install(home)
    claude_md = home / ".claude" / "CLAUDE.md"
    before = len(backups(claude_md))

    run_install(home)

    assert len(backups(claude_md)) == before


def test_existing_settings_change_keeps_distinct_private_backups(home: Path) -> None:
    claude = home / ".claude"
    claude.mkdir()
    settings = claude / "settings.json"
    settings.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
    settings.chmod(0o744)                                   # 이슈 #50 이 보고한 실제 조건

    result = run_install(home)                              # 두 installer-owned merge 발생
    assert result.returncode == 0, result.stderr

    saved = backups(settings)
    assert len(saved) == 2
    assert all(mode_of(path) == 0o600 for path in saved)
    assert mode_of(settings) == 0o600

    result = run_install(home)                              # 이제 변경 없음
    assert result.returncode == 0, result.stderr
    assert len(backups(settings)) == 2


def test_hook_wiring_stays_idempotent(home: Path) -> None:
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    settings = home / ".claude" / "settings.json"

    def entry_count() -> int:
        data = json.loads(settings.read_text(encoding="utf-8"))
        return sum(len(g.get("hooks", [])) for gs in data["hooks"].values() for g in gs)

    first = entry_count()
    result = run_install(home)
    assert result.returncode == 0, result.stderr

    assert entry_count() == first
    assert first > 0


def test_links_are_still_created_except_task_nudge_shim(home: Path) -> None:
    run_install(home)
    claude = home / ".claude"
    launcher = home / ".local" / "bin" / "jhw-control-host"

    shim = claude / "hooks" / "task-nudge.sh"
    assert shim.is_file() and not shim.is_symlink()
    assert shim.read_bytes() == (REPO / "hooks" / "task-nudge.sh").read_bytes()
    assert mode_of(shim) == 0o700
    assert (claude / "scripts" / "hook-selfcheck.py").is_symlink()
    assert (claude / "skills" / "task-observer").is_symlink()
    assert launcher.is_symlink()
    installed = home / ".local" / "lib" / "jhw-control-host" / "jhw-control-host.py"
    assert launcher.resolve() == installed.resolve()
    assert installed.read_bytes() == (REPO / "scripts" / "jhw-control-host.py").read_bytes()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o500
    assert stat.S_IMODE(installed.parent.stat().st_mode) == 0o700
    assert mode_of(home / ".local") == 0o700
    assert mode_of(home / ".local" / "bin") == 0o700
    assert mode_of(home / ".local" / "lib") == 0o700
    assert os.access(launcher, os.X_OK)


def test_session_handoff_commands_are_linked_without_touching_unrelated_commands(
    home: Path,
) -> None:
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    unrelated = commands / "revive.md"
    unrelated.write_text("keep local command\n", encoding="utf-8")

    result = run_install(home)

    assert result.returncode == 0, result.stderr
    for name in ("handoff.md", "resume.md"):
        installed = commands / name
        assert installed.is_symlink()
        assert installed.resolve() == (REPO / "commands" / name).resolve()
    assert unrelated.read_text(encoding="utf-8") == "keep local command\n"
    assert not unrelated.is_symlink()


@pytest.mark.parametrize("name", ["handoff.md", "resume.md"])
def test_session_handoff_command_replacement_preserves_existing_file(
    home: Path, name: str,
) -> None:
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    installed = commands / name
    installed.write_text(f"legacy {name}\n", encoding="utf-8")

    result = run_install(home)

    assert result.returncode == 0, result.stderr
    assert installed.is_symlink()
    saved = sorted(commands.glob(f"{name}.replaced.*"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == f"legacy {name}\n"


def test_session_handoff_command_rerun_adds_no_replacement_backup(home: Path) -> None:
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    installed = commands / "resume.md"
    installed.write_text("legacy resume\n", encoding="utf-8")

    first = run_install(home)
    assert first.returncode == 0, first.stderr
    saved_after_first = sorted(commands.glob("resume.md.replaced.*"))
    second = run_install(home)

    assert second.returncode == 0, second.stderr
    assert sorted(commands.glob("resume.md.replaced.*")) == saved_after_first
    assert len(saved_after_first) == 1


def test_launcher_install_never_executes_leading_path_canaries(home: Path, tmp_path: Path) -> None:
    """Restoring ambient command lookup would execute one of these fakes."""
    poison = tmp_path / "poison-bin"
    poison.mkdir()
    canary_log = tmp_path / "path-canary.log"
    real_tools = {
        "basename": "/usr/bin/basename",
        "chmod": "/usr/bin/chmod",
        "cp": "/usr/bin/cp",
        "date": "/usr/bin/date",
        "dirname": "/usr/bin/dirname",
        "grep": "/usr/bin/grep",
        "install": "/usr/bin/install",
        "ln": "/usr/bin/ln",
        "mkdir": "/usr/bin/mkdir",
        "mktemp": "/usr/bin/mktemp",
        "mv": "/usr/bin/mv",
        "python3": "/usr/bin/python3",
        "rm": "/usr/bin/rm",
    }
    for name, real_tool in real_tools.items():
        fake = poison / name
        fake.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "${0##*/}" >> "$PATH_CANARY_LOG"\n'
            f'exec "{real_tool}" "$@"\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)

    result = run_install(
        home,
        path=f"{poison}{os.pathsep}{os.environ['PATH']}",
        extra_env={"PATH_CANARY_LOG": str(canary_log)},
    )

    assert result.returncode == 0, result.stderr
    assert not canary_log.exists(), canary_log.read_text(encoding="utf-8")
    installed = home / ".local" / "lib" / "jhw-control-host" / "jhw-control-host.py"
    assert installed.read_bytes() == (REPO / "scripts" / "jhw-control-host.py").read_bytes()


@pytest.mark.parametrize("entry_kind", ["executable", "symlink"])
def test_launcher_install_never_executes_user_local_path_canaries(
    home: Path, tmp_path: Path, entry_kind: str,
) -> None:
    """The user-local tool directory must remain data, not installer PATH."""
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True, mode=0o700)
    local_bin.chmod(0o700)
    canary_log = tmp_path / "path-canary.log"
    names = ("rtk", "install", "mv", "ln", "python3", "basename")

    if entry_kind == "executable":
        delegator_dir = local_bin
    else:
        delegator_dir = tmp_path / "delegators"
        delegator_dir.mkdir()

    for name in names:
        delegator = delegator_dir / name
        real_tool = Path("/usr/bin") / name
        body = (
            "#!/bin/sh\n"
            f'printf "%s\\n" "${{0##*/}}" >> "$PATH_CANARY_LOG"\n'
        )
        if name == "rtk":
            body += "exit 97\n"
        else:
            body += f'exec "{real_tool}" "$@"\n'
        delegator.write_text(body, encoding="utf-8")
        delegator.chmod(0o755)
        if entry_kind == "symlink":
            (local_bin / name).symlink_to(delegator)

    result = run_install(
        home,
        path="/usr/bin:/bin",
        extra_env={"PATH_CANARY_LOG": str(canary_log)},
    )

    assert result.returncode == 0, result.stderr
    assert "@RTK.md" in (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert not canary_log.exists()
    installed = home / ".local" / "lib" / "jhw-control-host" / "jhw-control-host.py"
    assert installed.read_bytes() == (REPO / "scripts" / "jhw-control-host.py").read_bytes()


def test_trusted_command_path_rejects_another_principals_writable_directory(tmp_path: Path) -> None:
    """Removing command-PATH validation would permit an attacker-owned tool."""
    safe = tmp_path / "safe-bin"
    safe.mkdir(mode=0o700)
    safe.chmod(0o700)
    unsafe = tmp_path / "unsafe-bin"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)

    def check(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/bin/bash",
                "-c",
                '. "$1"\nassert_trusted_command_path "$2"',
                "command-path-test",
                str(PRIVATE_FILE),
                str(path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    assert check(safe).returncode == 0
    assert check(unsafe).returncode != 0
    sticky = Path("/tmp")
    sticky_mode = sticky.stat().st_mode
    if sticky.stat().st_uid == 0 and sticky_mode & stat.S_IWOTH and sticky_mode & stat.S_ISVTX:
        assert check(sticky).returncode != 0


@pytest.mark.parametrize("unsafe", ["writable-local", "unsafe-symlink-target"])
def test_launcher_install_rejects_unsafe_path_ancestors(home: Path, unsafe: str) -> None:
    local = home / ".local"
    if unsafe == "writable-local":
        local.mkdir(mode=0o777)
        local.chmod(0o777)
    else:
        target = home.parent / "shared-local"
        target.mkdir(mode=0o777)
        target.chmod(0o777)
        local.symlink_to(target, target_is_directory=True)

    result = run_install(home)

    assert result.returncode != 0
    assert "launcher 설치 경로가 안전하지 않다" in result.stderr
    assert not (home / ".local" / "lib" / "jhw-control-host" / "jhw-control-host.py").exists()


def test_launcher_install_rejects_symlink_to_directory_target(home: Path) -> None:
    launcher_dir = home / ".local" / "lib" / "jhw-control-host"
    launcher_dir.mkdir(parents=True)
    directory_target = launcher_dir / "unexpected-directory"
    directory_target.mkdir()
    launcher = launcher_dir / "jhw-control-host.py"
    launcher.symlink_to(directory_target, target_is_directory=True)

    result = run_install(home)

    assert result.returncode != 0
    assert "launcher 설치 대상이 디렉터리다" in result.stderr
    assert launcher.is_symlink()
    assert list(directory_target.iterdir()) == []


def test_readme_requires_reinstall_to_update_launcher_copy() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    assert "launcher 갱신에는 `./install.sh` 재실행" in readme


def test_launcher_install_backs_up_existing_file_once_and_is_idempotent(home: Path) -> None:
    binary_dir = home / ".local" / "bin"
    binary_dir.mkdir(parents=True)
    launcher = binary_dir / "jhw-control-host"
    launcher.write_text("legacy launcher\n", encoding="utf-8")

    first = run_install(home)

    assert first.returncode == 0, first.stderr
    assert launcher.is_symlink()
    backups_after_first = sorted(binary_dir.glob("jhw-control-host.replaced.*"))
    assert len(backups_after_first) == 1
    assert backups_after_first[0].read_text(encoding="utf-8") == "legacy launcher\n"

    second = run_install(home)

    assert second.returncode == 0, second.stderr
    assert launcher.is_symlink()
    assert sorted(binary_dir.glob("jhw-control-host.replaced.*")) == backups_after_first
