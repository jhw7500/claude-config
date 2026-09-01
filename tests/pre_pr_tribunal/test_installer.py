import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]
CLAUDE_COMMAND = (
    "/usr/bin/python3 $HOME/.local/share/claude-config/"
    "pre_pr_tribunal/claude_hook.py"
)
CODEX_COMMAND = (
    "/usr/bin/python3 $HOME/.local/share/claude-config/"
    "pre_pr_tribunal/codex_hook.py"
)
PACKAGE_NAMES = (
    "__init__.py",
    "claude_hook.py",
    "cli.py",
    "codex_hook.py",
    "gate.py",
    "git_state.py",
    "hook_common.py",
    "model.py",
    "shell_scan.py",
    "verdict_store.py",
)
SKILL_SOURCE = REPO / "skills/pre-pr-tribunal"
SKILL_TARGETS = (
    Path(".claude/skills/pre-pr-tribunal"),
    Path(".codex/skills/pre-pr-tribunal"),
)


def _targets(home: Path) -> list[Path]:
    package = home / ".local/share/claude-config/pre_pr_tribunal"
    return [
        *(package / name for name in PACKAGE_NAMES),
        home / ".claude/settings.json",
        home / ".codex/hooks.json",
        *(home / relative for relative in SKILL_TARGETS),
    ]


def _snapshot(paths: list[Path]) -> dict[Path, tuple[object, ...]]:
    result = {}
    for path in paths:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            result[path] = ("absent",)
        else:
            if stat.S_ISLNK(metadata.st_mode):
                result[path] = (
                    "symlink",
                    stat.S_IMODE(metadata.st_mode),
                    os.readlink(path),
                )
            elif stat.S_ISREG(metadata.st_mode):
                result[path] = (
                    "regular",
                    stat.S_IMODE(metadata.st_mode),
                    path.read_bytes(),
                )
            else:
                result[path] = (
                    "other",
                    stat.S_IFMT(metadata.st_mode),
                    stat.S_IMODE(metadata.st_mode),
                )
    return result


def _artifacts(home: Path) -> dict[Path, tuple[object, ...]]:
    result = {}
    for path in home.rglob("*"):
        if ".pre-pr-tribunal-" not in path.name and ".bak.pre-pr-tribunal." not in path.name:
            continue
        metadata = path.lstat()
        payload = path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None
        result[path] = (stat.S_IFMT(metadata.st_mode), stat.S_IMODE(metadata.st_mode), payload)
    return result


def _source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    package = source / "hooks" / "pre_pr_tribunal"
    package.parent.mkdir(parents=True)
    shutil.copytree(REPO / "hooks" / "pre_pr_tribunal", package)
    shutil.copytree(SKILL_SOURCE, source / "skills/pre-pr-tribunal")
    return source


def _managed(groups: list[dict[str, object]], command: str) -> list[dict[str, object]]:
    return [group for group in groups if group.get("hooks") == [{"type": "command", "command": command}]]


def test_build_plan_installs_one_shared_package_two_hooks_and_two_skill_links(installer, home):
    plans = installer.build_plan(installer.REPO, home)
    by_path = {entry.path: entry for entry in plans}
    package = home / ".local/share/claude-config/pre_pr_tribunal"
    assert set(by_path) == {
        *(package / name for name in PACKAGE_NAMES),
        home / ".claude/settings.json",
        home / ".codex/hooks.json",
        home / ".claude/skills/pre-pr-tribunal",
        home / ".codex/skills/pre-pr-tribunal",
    }
    assert all(by_path[package / name].mode == 0o600 for name in PACKAGE_NAMES)
    claude = json.loads(by_path[home / ".claude/settings.json"].data)
    codex = json.loads(by_path[home / ".codex/hooks.json"].data)
    assert _managed(claude["hooks"]["PreToolUse"], CLAUDE_COMMAND) == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": CLAUDE_COMMAND}]}
    ]
    assert _managed(codex["hooks"]["PreToolUse"], CODEX_COMMAND) == [
        {"hooks": [{"type": "command", "command": CODEX_COMMAND}]}
    ]
    for relative in SKILL_TARGETS:
        entry = by_path[home / relative]
        assert isinstance(entry, installer.PlannedSymlink)
        assert entry.target == SKILL_SOURCE


@pytest.mark.parametrize(
    "relative",
    [
        Path("SKILL.md"),
        Path("references/reviewer-a.md"),
        Path("references/reviewer-b.md"),
        Path("references/reviewer-c.md"),
        Path("references/report-schema.md"),
    ],
)
@pytest.mark.parametrize("kind", ["missing", "empty", "symlink", "directory", "fifo"])
def test_every_required_skill_source_is_safely_preflighted(
    installer, tmp_path, home, relative, kind
):
    source = _source_repo(tmp_path)
    victim = source / "skills/pre-pr-tribunal" / relative
    victim.unlink()
    if kind == "empty":
        victim.write_bytes(b"")
    elif kind == "symlink":
        outside = tmp_path / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        victim.symlink_to(outside)
    elif kind == "directory":
        victim.mkdir()
    elif kind == "fifo":
        os.mkfifo(victim)
    before = _snapshot(_targets(home))
    with pytest.raises(installer.InstallError):
        installer.build_plan(source, home)
    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == {}


@pytest.mark.parametrize("relative", SKILL_TARGETS)
@pytest.mark.parametrize("kind", ["wrong-link", "directory"])
def test_conflicting_skill_target_aborts_before_any_transaction_change(
    installer, tmp_path, home, relative, kind
):
    package = home / ".local/share/claude-config/pre_pr_tribunal"
    package.mkdir(parents=True)
    (package / "model.py").write_bytes(b"keep-package")
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"keep": "claude"}\n', encoding="utf-8")
    hooks = home / ".codex/hooks.json"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text('{"keep": "codex"}\n', encoding="utf-8")
    target = home / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "wrong-link":
        target.symlink_to(tmp_path / "other-skill", target_is_directory=True)
    else:
        target.mkdir()
        (target / "sentinel").write_text("keep\n", encoding="utf-8")
    before = _snapshot(_targets(home))
    with pytest.raises(installer.InstallError):
        installer.build_plan(REPO, home)
    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == {}


def test_skill_source_change_after_plan_aborts_before_any_transaction_change(
    installer, tmp_path, home
):
    source = _source_repo(tmp_path)
    plans = installer.build_plan(source, home)
    before = _snapshot(_targets(home))
    reference = source / "skills/pre-pr-tribunal/references/reviewer-a.md"
    reference.write_bytes(reference.read_bytes() + b"changed\n")

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            plans,
            namespace="pre-pr-tribunal",
            stamp="20260901070707",
        )

    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == {}


def test_list_conversion_cannot_strip_skill_source_guards(installer, tmp_path, home):
    source = _source_repo(tmp_path)
    plans = list(installer.build_plan(source, home))
    links = [entry for entry in plans if isinstance(entry, installer.PlannedSymlink)]
    assert len(links) == 2
    assert all(entry.source_preconditions for entry in links)
    reference = source / "skills/pre-pr-tribunal/references/reviewer-a.md"
    reference.write_bytes(reference.read_bytes() + b"changed\n")
    before = _snapshot(_targets(home))

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            plans,
            namespace="pre-pr-tribunal",
            stamp="20260901073737",
        )

    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == {}


def test_tribunal_boundary_rejects_managed_skill_link_without_source_guards(
    installer, home
):
    target = home / ".codex/skills/pre-pr-tribunal"
    unguarded = installer.PlannedSymlink(target, installer.SKILL_SOURCE)

    with pytest.raises(installer.InstallError, match="source preconditions required"):
        installer.apply_transaction(
            [unguarded],
            namespace="pre-pr-tribunal",
            stamp="20260901074747",
        )

    assert not os.path.lexists(target)


def test_tribunal_boundary_rejects_incomplete_skill_source_guards(installer, home):
    target = home / ".codex/skills/pre-pr-tribunal"
    root_guard = installer.capture_source_directory(installer.SKILL_SOURCE)
    incomplete = installer.PlannedSymlink(
        target,
        installer.SKILL_SOURCE,
        source_preconditions=(root_guard,),
    )

    with pytest.raises(installer.InstallError, match="source metadata required"):
        installer.apply_transaction(
            [incomplete],
            namespace="pre-pr-tribunal",
            stamp="20260901075757",
        )

    assert not os.path.lexists(target)


def test_main_cli_path_applies_both_managed_links_with_source_guards(
    installer, home, monkeypatch
):
    real_apply = installer._apply_transaction
    observed = []

    def inspect_and_apply(entries, **kwargs):
        links = [entry for entry in entries if isinstance(entry, installer.PlannedSymlink)]
        assert len(links) == 2
        assert all(entry.source_preconditions for entry in links)
        observed.extend(links)
        return real_apply(entries, **kwargs)

    monkeypatch.setattr(installer, "_apply_transaction", inspect_and_apply)

    assert installer.main(["--repo", str(REPO), "--home", str(home)]) == 0
    assert {entry.path for entry in observed} == {
        home / ".claude/skills/pre-pr-tribunal",
        home / ".codex/skills/pre-pr-tribunal",
    }
    assert all(entry.path.is_symlink() for entry in observed)


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_dynamic_extra_markdown_reference_is_also_safely_preflighted(
    installer, tmp_path, home, kind
):
    source = _source_repo(tmp_path)
    extra = source / "skills/pre-pr-tribunal/references/extra.md"
    if kind == "symlink":
        outside = tmp_path / "outside-extra.md"
        outside.write_text("outside\n", encoding="utf-8")
        extra.symlink_to(outside)
    elif kind == "directory":
        extra.mkdir()
    else:
        os.mkfifo(extra)

    before = _snapshot(_targets(home))
    with pytest.raises(installer.InstallError):
        installer.build_plan(source, home)

    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == {}


def test_symlinked_skills_parent_cannot_redirect_preflight_outside_repo(
    installer, tmp_path, home
):
    source = _source_repo(tmp_path)
    skills = source / "skills"
    outside = tmp_path / "outside-skills"
    skills.rename(outside)
    skills.symlink_to(outside, target_is_directory=True)
    before = _snapshot(_targets(home))

    with pytest.raises(installer.InstallError):
        installer.build_plan(source, home)

    assert skills.is_symlink() and skills.resolve() == outside
    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == {}


def test_skills_parent_drift_after_plan_aborts_before_transaction_apply(
    installer, tmp_path, home
):
    source = _source_repo(tmp_path)
    plans = installer.build_plan(source, home)
    skills = source / "skills"
    outside = tmp_path / "outside-skills"
    skills.rename(outside)
    skills.symlink_to(outside, target_is_directory=True)
    before = _snapshot(_targets(home))

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            plans,
            namespace="pre-pr-tribunal",
            stamp="20260901071717",
        )

    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == {}


def test_skill_target_drift_after_plan_aborts_every_package_and_config_write(
    installer, home
):
    plans = installer.build_plan(REPO, home)
    target = home / ".codex/skills/pre-pr-tribunal"
    target.mkdir(parents=True)
    (target / "sentinel").write_text("keep\n", encoding="utf-8")
    before = _snapshot(_targets(home))

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            plans,
            namespace="pre-pr-tribunal",
            stamp="20260901072727",
        )

    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == {}


def test_merge_preserves_unrelated_top_level_keys_groups_and_order(installer, home):
    claude = home / ".claude"
    codex = home / ".codex"
    claude.mkdir()
    codex.mkdir()
    unrelated = {"matcher": "Write", "hooks": [{"type": "command", "command": "keep-claude"}]}
    claude_value = {"alpha": 1, "hooks": {"PreToolUse": [unrelated], "Stop": [{"hooks": [{"type": "command", "command": "keep-stop"}]}]}, "omega": 2}
    codex_value = {"first": True, "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "keep-codex"}]}]}, "last": False}
    (claude / "settings.json").write_text(json.dumps(claude_value), encoding="utf-8")
    (codex / "hooks.json").write_text(json.dumps(codex_value), encoding="utf-8")

    plans = {entry.path: entry for entry in installer.build_plan(REPO, home)}
    merged_claude = json.loads(plans[claude / "settings.json"].data)
    merged_codex = json.loads(plans[codex / "hooks.json"].data)

    assert list(merged_claude) == ["alpha", "hooks", "omega"]
    assert merged_claude["hooks"]["Stop"] == claude_value["hooks"]["Stop"]
    assert merged_claude["hooks"]["PreToolUse"][0] == unrelated
    assert list(merged_codex) == ["first", "hooks", "last"]
    assert merged_codex["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "keep-codex"


@pytest.mark.parametrize(
    ("runtime", "command", "expected_matcher"),
    [("claude", CLAUDE_COMMAND, "Bash"), ("codex", CODEX_COMMAND, None)],
)
def test_one_legacy_managed_group_is_replaced_in_place(
    installer, home, runtime, command, expected_matcher
):
    config = home / f".{runtime}" / ("settings.json" if runtime == "claude" else "hooks.json")
    config.parent.mkdir()
    absolute = command.replace("$HOME", str(home))
    original = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "legacy", "hooks": [{"type": "command", "command": "before"}]},
                {"matcher": "legacy", "hooks": [{"type": "command", "command": absolute}]},
                {"hooks": [{"type": "command", "command": "after"}]},
            ]
        }
    }
    config.write_text(json.dumps(original), encoding="utf-8")
    plan = next(entry for entry in installer.build_plan(REPO, home) if entry.path == config)
    groups = json.loads(plan.data)["hooks"]["PreToolUse"]
    assert [group["hooks"][0]["command"] for group in groups] == ["before", command, "after"]
    assert groups[1].get("matcher") == expected_matcher
    assert ("matcher" in groups[1]) is (expected_matcher is not None)


@pytest.mark.parametrize(
    "groups",
    [
        [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": CLAUDE_COMMAND}]},
            {"matcher": "legacy", "hooks": [{"type": "command", "command": CLAUDE_COMMAND}]},
        ],
        [{"matcher": "Bash", "hooks": [{"type": "command", "command": CLAUDE_COMMAND}, {"type": "command", "command": "keep"}]}],
        [{"matcher": "Bash", "hooks": [{"type": "command", "command": CLAUDE_COMMAND}], "timeout": 10}],
    ],
    ids=["duplicate-managed-groups", "managed-command-in-multi-hook-group", "unknown-managed-field"],
)
def test_ambiguous_managed_claude_groups_fail_without_changes(installer, home, groups):
    config = home / ".claude/settings.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"hooks": {"PreToolUse": groups}}), encoding="utf-8")
    before = _snapshot(_targets(home))
    artifacts = _artifacts(home)
    with pytest.raises(installer.InstallError):
        installer.build_plan(REPO, home)
    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == artifacts


@pytest.mark.parametrize(
    "payload",
    [b"{broken", b'{"hooks": {}, "hooks": {}}', b'[]'],
    ids=["malformed", "duplicate-key", "non-object"],
)
@pytest.mark.parametrize("relative", [Path(".claude/settings.json"), Path(".codex/hooks.json")])
def test_invalid_runtime_json_fails_without_changes(installer, home, payload, relative):
    config = home / relative
    config.parent.mkdir()
    config.write_bytes(payload)
    before = _snapshot(_targets(home))
    artifacts = _artifacts(home)
    with pytest.raises(installer.InstallError):
        installer.build_plan(REPO, home)
    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == artifacts


@pytest.mark.parametrize("kind", ["missing", "empty", "symlink", "directory", "fifo"])
def test_invalid_required_python_source_fails_closed(installer, tmp_path, home, kind):
    source = _source_repo(tmp_path)
    victim = source / "hooks/pre_pr_tribunal/model.py"
    victim.unlink()
    if kind == "empty":
        victim.write_bytes(b"")
    elif kind == "symlink":
        outside = tmp_path / "outside.py"
        outside.write_text("outside\n", encoding="utf-8")
        victim.symlink_to(outside)
    elif kind == "directory":
        victim.mkdir()
    elif kind == "fifo":
        os.mkfifo(victim)
    before = _snapshot(_targets(home))
    with pytest.raises(installer.InstallError):
        installer.build_plan(source, home)
    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == {}


@pytest.mark.parametrize("kind", ["symlink-target", "directory-target", "symlink-parent"])
def test_unsafe_package_target_or_parent_fails_closed(installer, tmp_path, home, kind):
    package = home / ".local/share/claude-config/pre_pr_tribunal"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    if kind == "symlink-parent":
        package.parent.mkdir(parents=True)
        package.symlink_to(tmp_path, target_is_directory=True)
    else:
        package.mkdir(parents=True)
        target = package / "model.py"
        if kind == "symlink-target":
            target.symlink_to(outside)
        else:
            target.mkdir()
    before = _snapshot(_targets(home))
    artifacts = _artifacts(home)
    with pytest.raises(installer.InstallError):
        installer.build_plan(REPO, home)
    assert _snapshot(_targets(home)) == before
    assert outside.read_bytes() == b"outside"
    assert _artifacts(home) == artifacts


def test_source_identity_change_during_preflight_fails_without_writes(installer, tmp_path, home):
    source = _source_repo(tmp_path)
    victim = source / "hooks/pre_pr_tribunal/model.py"
    replacement = tmp_path / "replacement.py"
    replacement.write_text("replacement\n", encoding="utf-8")

    def swap(path: Path) -> None:
        if path == victim:
            victim.unlink()
            victim.symlink_to(replacement)

    before = _snapshot(_targets(home))
    with pytest.raises(installer.InstallError):
        installer.build_plan(source, home, before_source_open=swap)
    assert _snapshot(_targets(home)) == before
    assert replacement.read_text(encoding="utf-8") == "replacement\n"
    assert _artifacts(home) == {}


def test_extra_top_level_python_module_is_planned_and_installed(
    installer, tmp_path, home
):
    source = _source_repo(tmp_path)
    extra = source / "hooks/pre_pr_tribunal/extra_module.py"
    extra.write_bytes(b"EXTRA = True\n")
    target = home / ".local/share/claude-config/pre_pr_tribunal/extra_module.py"

    plans = installer.build_plan(source, home)
    by_path = {plan.path: plan for plan in plans}
    assert by_path[target].data == b"EXTRA = True\n"

    installer.apply_transaction(
        plans,
        namespace="pre-pr-tribunal",
        stamp="20260901080808",
    )
    assert target.read_bytes() == b"EXTRA = True\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_tribunal_parent_drift_fails_without_false_install_or_outside_change(
    installer, tmp_path, home
):
    package = home / ".local/share/claude-config/pre_pr_tribunal"
    package.mkdir(parents=True)
    moved_package = tmp_path / "validated-package"
    outside_package = tmp_path / "outside-package"
    outside_package.mkdir()
    outside_target = outside_package / "__init__.py"
    outside_target.write_bytes(b"outside-substitution\n")
    plans = installer.build_plan(REPO, home)

    def swap(phase, path):
        if phase == "before_replace_revalidate" and path == package / "__init__.py":
            package.rename(moved_package)
            package.symlink_to(outside_package, target_is_directory=True)

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            plans,
            namespace="pre-pr-tribunal",
            stamp="20260901090909",
            phase_hook=swap,
        )

    assert package.is_symlink() and package.resolve() == outside_package
    assert outside_target.read_bytes() == b"outside-substitution\n"
    assert list(moved_package.iterdir()) == []
    assert not (home / ".claude/settings.json").exists()
    assert not (home / ".codex/hooks.json").exists()


@pytest.mark.parametrize("kind", ["missing", "regular", "symlink"])
def test_build_plan_rejects_invalid_home_without_writes(installer, tmp_path, kind):
    candidate = tmp_path / "invalid-home"
    outside = tmp_path / "outside-home"
    outside.mkdir(mode=0o700)
    if kind == "regular":
        candidate.write_text("not a directory\n", encoding="utf-8")
    elif kind == "symlink":
        candidate.symlink_to(outside, target_is_directory=True)

    with pytest.raises(installer.InstallError):
        installer.build_plan(REPO, candidate)

    assert not (outside / ".local").exists()
    assert not (outside / ".claude").exists()
    assert not (outside / ".codex").exists()


def test_target_change_before_replace_aborts_whole_transaction(installer, home):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text('{"keep": "original"}\n', encoding="utf-8")
    plans = installer.build_plan(REPO, home)
    before = _snapshot(_targets(home))
    artifacts = _artifacts(home)

    def race(phase: str, path: Path) -> None:
        if phase == "before_replace_revalidate" and path == settings:
            settings.write_text('{"keep": "raced"}\n', encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            plans,
            namespace="pre-pr-tribunal",
            stamp="20260901010101",
            phase_hook=race,
        )
    expected = dict(before)
    expected[settings] = ("regular", stat.S_IMODE(settings.stat().st_mode), b'{"keep": "raced"}\n')
    assert _snapshot(_targets(home)) == expected
    assert _artifacts(home) == artifacts


def test_injected_staging_failure_rolls_back_every_target(
    installer, home, monkeypatch
):
    plans = installer.build_plan(REPO, home)
    before = _snapshot(_targets(home))
    artifacts = _artifacts(home)
    common = sys.modules[installer._apply_transaction.__module__]
    real_stage = common._stage_regular_at
    stages = 0

    def fail_third(*args, **kwargs):
        nonlocal stages
        stages += 1
        if stages == 3:
            raise OSError("injected stage failure")
        return real_stage(*args, **kwargs)

    monkeypatch.setattr(common, "_stage_regular_at", fail_third)
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            plans,
            namespace="pre-pr-tribunal",
            stamp="20260901015151",
        )
    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == artifacts


def test_injected_mid_transaction_replace_failure_rolls_back_every_target(installer, home):
    plans = installer.build_plan(REPO, home)
    before = _snapshot(_targets(home))
    artifacts = _artifacts(home)
    replacements = 0

    def fail_third(source: Path, destination: Path) -> None:
        nonlocal replacements
        replacements += 1
        if replacements == 3:
            raise OSError("injected replace failure")
        os.replace(source, destination)

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            plans,
            namespace="pre-pr-tribunal",
            replace=fail_third,
            stamp="20260901020202",
        )
    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == artifacts


def test_backup_collision_preflight_has_zero_partial_changes(installer, home):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text('{"keep": true}\n', encoding="utf-8")
    collision = settings.with_name("settings.json.bak.pre-pr-tribunal.20260901030303")
    collision.write_bytes(b"collision-owner")
    plans = installer.build_plan(REPO, home)
    before = _snapshot(_targets(home))
    artifacts = _artifacts(home)
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            plans,
            namespace="pre-pr-tribunal",
            stamp="20260901030303",
        )
    assert _snapshot(_targets(home)) == before
    assert collision.read_bytes() == b"collision-owner"
    assert _artifacts(home) == artifacts


def test_duplicate_planned_target_is_rejected_before_apply(installer, home):
    plans = installer.build_plan(REPO, home)
    before = _snapshot(_targets(home))
    with pytest.raises(installer.InstallError):
        installer.apply_transaction(
            [*plans, plans[0]],
            namespace="pre-pr-tribunal",
            stamp="20260901040404",
        )
    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == {}


def test_identical_reinstall_is_byte_stable_and_creates_no_backup(installer, home):
    first = installer.build_plan(REPO, home)
    installer.apply_transaction(first, namespace="pre-pr-tribunal", stamp="20260901050505")
    before = _snapshot(_targets(home))
    artifacts = _artifacts(home)
    second = installer.build_plan(REPO, home)
    assert installer.apply_transaction(
        second, namespace="pre-pr-tribunal", stamp="20260901060606"
    ) == []
    assert _snapshot(_targets(home)) == before
    assert _artifacts(home) == artifacts


def test_cli_error_is_bounded_and_does_not_leak_config_or_paths(installer, home):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    canary = "PRIVATE_CONFIG_CANARY"
    settings.write_text("{" + canary, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts/install-pre-pr-tribunal.py"), "--repo", str(REPO), "--home", str(home)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert canary not in result.stderr
    assert str(home) not in result.stderr
    assert str(REPO) not in result.stderr
    assert _artifacts(home) == {}
