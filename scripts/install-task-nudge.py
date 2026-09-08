#!/usr/bin/python3
"""Install the shared task-nudge runtime without partial target updates."""

from __future__ import annotations

from pathlib import Path
from typing import Callable
import argparse
import os
import stat
import sys


REPO = Path(__file__).resolve().parents[1]
LIB = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(LIB))

from runtime_hook_installer import (  # noqa: E402
    InstallError,
    PlannedWrite,
    SelectorPrecondition as _SelectorPrecondition,
    TargetSnapshot as _TargetSnapshot,
    apply_transaction as _apply_transaction,
    inspect_target as _inspect_target,
    merge_pre_tool_hook,
    read_regular_source as _read_regular_source,
    render_json_config,
    snapshot_matches as _snapshot_matches,
    strict_json_object as _parse_json_config,
    _retain_source_parent,
)


AGENTS_START = "<!-- claude-config:task-nudge:START -->"
AGENTS_END = "<!-- claude-config:task-nudge:END -->"
CLAUDE_MATCHER = "Edit|Write|NotebookEdit"
CLAUDE_COMMAND = "$HOME/.claude/hooks/task-nudge.sh"
CODEX_MATCHER = "apply_patch|Edit|Write"
CODEX_COMMAND = "/usr/bin/python3 $HOME/.local/share/claude-config/hooks/task-nudge-codex.py"


def load_json_config(path: Path) -> dict[str, object]:
    """Load one optional configuration object with duplicate-key rejection."""
    try:
        return _parse_json_config(path.read_bytes())
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise InstallError("cannot read JSON configuration") from error


def merge_hook_config(
    original: dict[str, object],
    *,
    matcher: str,
    command: str,
    legacy_commands: tuple[str, ...],
    home: Path,
) -> dict[str, object]:
    return merge_pre_tool_hook(
        original,
        matcher=matcher,
        command=command,
        legacy_commands=legacy_commands,
        home=home,
    )


def select_agents_path(home: Path) -> Path:
    """Select the active global Codex instruction file."""
    codex = home / ".codex"
    override = codex / "AGENTS.override.md"
    try:
        raw = override.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as error:
        raise InstallError("cannot read AGENTS override") from error
    try:
        nonempty = bool(raw.decode("utf-8").strip())
    except UnicodeDecodeError as error:
        raise InstallError("AGENTS override is not UTF-8") from error
    return override if nonempty else codex / "AGENTS.md"


def merge_agents_block(original: str, policy: str) -> str:
    """Append or replace one exact managed pair without changing outside bytes."""
    start_count = original.count(AGENTS_START)
    end_count = original.count(AGENTS_END)
    if start_count != end_count or start_count not in {0, 1}:
        raise InstallError("malformed AGENTS task-nudge markers")
    body = policy.rstrip("\n")
    block = AGENTS_START + "\n" + body + "\n" + AGENTS_END
    if start_count == 0:
        separator = "" if not original or original.endswith("\n") else "\n"
        return original + separator + block + "\n"
    start = original.index(AGENTS_START)
    end = original.index(AGENTS_END)
    if end < start:
        raise InstallError("reversed AGENTS task-nudge markers")
    return original[:start] + block + original[end + len(AGENTS_END) :]


def agents_policy_block() -> str:
    """Return the fallback policy kept semantically aligned with Task 3."""
    return (
        "첫 실질 변경 전에, 현재 세션에서 native [TASK-NUDGE]를 이미 받았거나 Task 선택을 이미 결정했다면 반복하지 않는다. "
        "그렇지 않으면 다음 stateless checker를 한 번 실행한다(설치/credential/Project Control mutation은 하지 않는다):\n"
        '/usr/bin/python3 $HOME/.local/share/claude-config/hooks/task-nudge-codex.py --manual-check --cwd "$PWD"\n'
        "반환된 repository_slug와 registration_status에 다음 우선순서를 적용해 하나만 추천한다: "
        "(1) 이미 결정됨·제외 작업(조회/Q&A·단순 문서/설정·subagent)은 Task 없이 진행; "
        "(2) backlog(unknown 포함)는 등록 상태와 무관하게 GitHub Issue만 제안하고 Task/Claim을 시작하지 않음; "
        "(3) 즉시 작업의 unknown이면 등록 여부를 가정하지 말고 현재 변경을 중단하고 bounded 오류의 복구 필요만 알림; "
        "(4) 등록 저장소의 즉시 작업은 기존 Issue 또는 반복 증거면 Formal Issue Task, 현재 세션의 제한 작업이면 Temporary Task, "
        "조정 비용보다 작으면 Task 없음; "
        "(5) 미등록 저장소의 즉시 작업은 반복 증거가 있을 때만 Project/Repository 등록만 먼저 제안하고, 아니면 Task 없음.\n"
        "반복·다중 세션 증거는 다음 셋뿐이다: 사용자의 장기·반복·여러 세션 명시, "
        "기존 GitHub Issue·승인된 계획·Handoff, 여러 구현 단계와 검증이 필요한 아키텍처 작업. "
        "파일 수나 저장소 안에 있다는 사실은 증거가 아니다.\n"
        "GitHub Issue 생성, Project/Repository 등록, Formal 또는 Temporary Task 시작은 각각 별도의 명시적 사용자 승인 후에만 한다. "
        "앞 단계 승인은 다음 단계를 승인하지 않는다.\n"
    )


def _validate_home(home: Path) -> None:
    """Require an existing real HOME after a descriptor-anchored path walk."""
    if not home.is_absolute():
        raise InstallError("HOME is unavailable")
    _inspect_target(PlannedWrite(home / ".task-nudge-home-probe", b"", 0o600, False))
    try:
        metadata = home.lstat()
    except OSError as error:
        raise InstallError("HOME is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise InstallError("HOME is unavailable")


def build_plan(
    repo: Path,
    home: Path,
    *,
    before_source_open: Callable[[Path], None] | None = None,
) -> list[PlannedWrite]:
    """Validate every input and return the complete task-nudge write set."""
    repo = Path(repo)
    home = Path(home)
    _validate_home(home)

    source_names = (
        "task_nudge.py",
        "task-nudge-claude.py",
        "task-nudge-codex.py",
        "task-nudge.sh",
    )
    with _retain_source_parent(repo / "hooks"):
        sources = {
            name: _read_regular_source(
                repo / "hooks" / name,
                before_open=before_source_open,
            )
            for name in source_names
        }

    claude_config = home / ".claude" / "settings.json"
    codex_config = home / ".codex" / "hooks.json"
    claude_snapshot = _inspect_target(PlannedWrite(claude_config, b"", 0o600, True))
    codex_snapshot = _inspect_target(PlannedWrite(codex_config, b"", 0o600, True))
    claude_raw = claude_snapshot.data if claude_snapshot.exists else None
    codex_raw = codex_snapshot.data if codex_snapshot.exists else None
    claude_value = merge_hook_config(
        {} if claude_raw is None else _parse_json_config(claude_raw),
        matcher=CLAUDE_MATCHER,
        command=CLAUDE_COMMAND,
        legacy_commands=(CLAUDE_COMMAND,),
        home=home,
    )
    codex_value = merge_hook_config(
        {} if codex_raw is None else _parse_json_config(codex_raw),
        matcher=CODEX_MATCHER,
        command=CODEX_COMMAND,
        legacy_commands=(),
        home=home,
    )

    override = home / ".codex" / "AGENTS.override.md"
    override_snapshot = _inspect_target(PlannedWrite(override, b"", 0o600, True))
    override_raw = override_snapshot.data if override_snapshot.exists else None
    try:
        override_nonempty = override_raw is not None and bool(
            override_raw.decode("utf-8").strip()
        )
    except UnicodeDecodeError as error:
        raise InstallError("AGENTS override is not UTF-8") from error
    agents_path = override if override_nonempty else home / ".codex" / "AGENTS.md"
    agents_snapshot = (
        override_snapshot
        if override_nonempty
        else _inspect_target(PlannedWrite(agents_path, b"", 0o600, True))
    )
    agents_raw = agents_snapshot.data if agents_snapshot.exists else None
    try:
        agents_original = "" if agents_raw is None else agents_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InstallError("active AGENTS file is not UTF-8") from error
    agents_data = merge_agents_block(agents_original, agents_policy_block()).encode(
        "utf-8"
    )

    installed = home / ".local" / "share" / "claude-config" / "hooks"
    plans = [
        PlannedWrite(installed / "task_nudge.py", sources["task_nudge.py"], 0o600, True),
        PlannedWrite(
            installed / "task-nudge-claude.py",
            sources["task-nudge-claude.py"],
            0o600,
            True,
        ),
        PlannedWrite(
            installed / "task-nudge-codex.py",
            sources["task-nudge-codex.py"],
            0o600,
            True,
        ),
        PlannedWrite(
            home / ".claude" / "hooks" / "task-nudge.sh",
            sources["task-nudge.sh"],
            0o700,
            True,
            allow_legacy_symlink=True,
        ),
        PlannedWrite(claude_config, render_json_config(claude_value), 0o600, True),
        PlannedWrite(codex_config, render_json_config(codex_value), 0o600, True),
        PlannedWrite(agents_path, agents_data, 0o600, True),
    ]
    build_snapshots = {
        claude_config: claude_snapshot,
        codex_config: codex_snapshot,
        agents_path: agents_snapshot,
    }
    seen: set[Path] = set()
    validated_plans: list[PlannedWrite] = []
    for plan in plans:
        if plan.path in seen:
            raise InstallError("duplicate planned target")
        seen.add(plan.path)
        snapshot = build_snapshots.get(plan.path)
        current = _inspect_target(plan)
        if snapshot is not None and not _snapshot_matches(current, snapshot):
            raise InstallError("planned target changed while building plan")
        if snapshot is None:
            snapshot = current
        validated_plans.append(
            PlannedWrite(
                plan.path,
                plan.data,
                plan.mode,
                plan.backup,
                allow_legacy_symlink=plan.allow_legacy_symlink,
                precondition=snapshot,
                selector_preconditions=(
                    (_SelectorPrecondition(override, override_snapshot),)
                    if plan.path == agents_path
                    else ()
                ),
            )
        )
    return validated_plans


def apply_transaction(writes, **kwargs):
    return _apply_transaction(writes, namespace="task-nudge", **kwargs)


def _cli_home(value: str | None) -> Path:
    raw = value if value is not None else os.environ.get("HOME")
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        raise InstallError("HOME must be an absolute path")
    return Path(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install transactional task-nudge runtime")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--home")
    arguments = parser.parse_args(argv)
    repo = Path(arguments.repo)
    if not repo.is_absolute():
        print("install-task-nudge: --repo must be absolute", file=sys.stderr)
        return 2
    try:
        plans = build_plan(repo, _cli_home(arguments.home))
        changed = apply_transaction(plans)
    except InstallError as error:
        print(f"install-task-nudge: {error}", file=sys.stderr)
        return 1
    print(
        f"task-nudge installed ({len(changed)} changed); "
        "review and trust the Codex hook with /hooks"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
