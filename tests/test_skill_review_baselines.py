"""Protect weekly review from staging bundles based on a stale live skill."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import runpy

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "--all")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def observation(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "log.md").write_text("### Observation 1: pending\n**Status:** OPEN\n")
    return path


def copy_launcher(repo: Path) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for source in SCRIPTS.glob("*skill-review*"):
        if source.is_file():
            shutil.copy2(source, scripts / source.name)


def install_fake_claude(test_home: Path, capture: Path) -> None:
    executable = test_home / ".local/bin/claude"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "with open(os.environ['REVIEW_TEST_CAPTURE'], 'w') as stream:\n"
        "    json.dump(sys.argv[1:], stream)\n"
    )
    executable.chmod(0o755)


def live_records(capture: Path) -> list[dict]:
    args = json.loads(capture.read_text())
    prompt = args[args.index("-p") + 1]
    section = prompt.split(
        "live 스킬 설치본(초안의 기준, ~/.claude/skills 링크의 실체):\n", 1
    )[1].split("\n다중 소스 모드의 원본 보존 규칙", 1)[0]
    return json.loads(section)


def baselines(*triples: tuple[str, Path | str, str], env=None):
    arguments = [value for name, path, status in triples
                 for value in (name, str(path), status)]
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "skill-review-baselines.py"), *arguments],
        env=env, capture_output=True, text=True,
    )


def one_record(*triples: tuple[str, Path | str, str], env=None) -> dict:
    result = baselines(*triples, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.isascii()
    assert len(result.stdout.splitlines()) == 1
    [record] = json.loads(result.stdout)
    return record


def comparison_repo(tmp_path: Path, name="example") -> tuple[Path, Path, str]:
    repo = tmp_path / "repository"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "master")
    skill = repo / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("same skill\n")
    initial = commit_all(repo, "initial canonical skill")
    pinned = tmp_path / "pinned"
    git(repo, "worktree", "add", "--detach", str(pinned), initial)
    return repo, pinned / "skills" / name, initial


def set_origin_master(repo: Path, commit: str) -> None:
    git(repo, "update-ref", "refs/remotes/origin/master", commit)


def test_launcher_holds_live_skill_that_differs_from_origin_master(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "master")
    copy_launcher(repo)
    skill = repo / "skills/example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("live version\n")
    anchor = observation(repo / "skill-observations/wlan")
    initial = commit_all(repo, "initial live skill")

    pinned = tmp_path / "pinned"
    git(repo, "worktree", "add", "--detach", str(pinned), initial)
    (skill / "SKILL.md").write_text("canonical version\n")
    canonical = commit_all(repo, "update canonical skill")
    git(repo, "update-ref", "refs/remotes/origin/master", canonical)

    test_home = tmp_path / "home"
    links = test_home / ".claude/skills"
    links.mkdir(parents=True)
    (links / "example").symlink_to(pinned / "skills/example", target_is_directory=True)
    capture = tmp_path / "args.json"
    install_fake_claude(test_home, capture)
    env = dict(os.environ, HOME=str(test_home), REVIEW_TEST_CAPTURE=str(capture))

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    [record] = live_records(capture)
    assert record["status"] == "hold"
    assert record["reason"] == "live skill differs from origin/master"
    assert record["comparison"]["status"] == "diverged"
    assert record["comparison"]["ref"] == "origin/master"
    assert record["comparison"]["commit"] == canonical
    assert record["comparison"]["path"] == "skills/example"
    assert record["comparison"]["changed"] == ["SKILL.md"]
    assert record["comparison"]["live_only"] == []
    assert record["comparison"]["origin_only"] == []


def test_baseline_matches_detached_live_subtree_when_only_other_files_changed(tmp_path):
    repo, live, initial = comparison_repo(tmp_path)
    (repo / "README.md").write_text("unrelated change\n")
    canonical = commit_all(repo, "change outside skill")
    set_origin_master(repo, canonical)

    live_before = {path.relative_to(live): path.read_bytes()
                   for path in live.rglob("*") if path.is_file()}
    status_before = git(repo, "status", "--porcelain=v1")

    record = one_record(("example", live, "resolved"))

    assert record == {
        "name": "example",
        "path": str(live),
        "comparison": {
            "ref": "origin/master",
            "commit": canonical,
            "path": "skills/example",
            "status": "match",
            "changed": [],
            "live_only": [],
            "origin_only": [],
        },
    }
    assert git(repo, "rev-parse", "HEAD") != initial
    assert {path.relative_to(live): path.read_bytes()
            for path in live.rglob("*") if path.is_file()} == live_before
    assert git(repo, "status", "--porcelain=v1") == status_before


def test_baseline_reports_changed_live_only_origin_only_and_mode(tmp_path):
    repo, live, _ = comparison_repo(tmp_path)
    (repo / "skills/example/SKILL.md").write_text("canonical text\n")
    (repo / "skills/example/origin.md").write_text("only canonical\n")
    executable = repo / "skills/example/tool.sh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    canonical = commit_all(repo, "expand canonical skill")
    set_origin_master(repo, canonical)
    (live / "live.md").write_text("only live\n")
    (live / "tool.sh").write_text("#!/bin/sh\n")
    (live / "tool.sh").chmod(0o644)

    record = one_record(("example", live, "resolved"))

    assert record["status"] == "hold"
    assert record["reason"] == "live skill differs from origin/master"
    assert record["comparison"] == {
        "ref": "origin/master",
        "commit": canonical,
        "path": "skills/example",
        "status": "diverged",
        "changed": ["SKILL.md", "tool.sh"],
        "live_only": ["live.md"],
        "origin_only": ["origin.md"],
    }


@pytest.mark.parametrize("staged", [False, True])
def test_baseline_detects_dirty_tracked_live_content(tmp_path, staged):
    repo, live, initial = comparison_repo(tmp_path)
    set_origin_master(repo, initial)
    (live / "SKILL.md").write_text("dirty live\n")
    if staged:
        git(live, "add", "SKILL.md")

    record = one_record(("example", live, "resolved"))

    assert record["comparison"]["status"] == "diverged"
    assert record["comparison"]["changed"] == ["SKILL.md"]


def test_baseline_ignores_only_bundle_generated_files(tmp_path):
    repo, live, initial = comparison_repo(tmp_path)
    set_origin_master(repo, initial)
    (live / "__pycache__").mkdir()
    (live / "__pycache__/cache.pyc").write_bytes(b"generated")
    (live / "module.pyc").write_bytes(b"generated")
    (live / ".DS_Store").write_bytes(b"generated")

    record = one_record(("example", live, "resolved"))

    assert record["comparison"]["status"] == "match"
    assert record["comparison"]["live_only"] == []


def test_baseline_ignores_bundle_generated_files_in_canonical_tree(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-b", "master")
    skill = repo / "skills/example"
    (skill / "__pycache__").mkdir(parents=True)
    (skill / "SKILL.md").write_text("same skill\n")
    (skill / "__pycache__/cache.pyc").write_bytes(b"generated")
    (skill / "module.pyc").write_bytes(b"generated")
    (skill / ".DS_Store").write_bytes(b"generated")
    canonical = commit_all(repo, "canonical skill with generated files")
    set_origin_master(repo, canonical)

    record = one_record(("example", skill, "resolved"))

    assert record["comparison"]["status"] == "match"
    assert record["comparison"]["origin_only"] == []


def test_baseline_isolates_one_diverged_skill_from_one_matching_skill(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-b", "master")
    for name in ("stable", "changed"):
        path = repo / "skills" / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"{name}\n")
    initial = commit_all(repo, "two skills")
    pinned = tmp_path / "pinned"
    git(repo, "worktree", "add", "--detach", str(pinned), initial)
    (repo / "skills/changed/SKILL.md").write_text("canonical update\n")
    canonical = commit_all(repo, "update one skill")
    set_origin_master(repo, canonical)

    result = baselines(
        ("stable", pinned / "skills/stable", "resolved"),
        ("changed", pinned / "skills/changed", "resolved"),
    )

    assert result.returncode == 0, result.stderr
    records = {record["name"]: record for record in json.loads(result.stdout)}
    assert records["stable"]["comparison"]["status"] == "match"
    assert records["changed"]["status"] == "hold"
    assert records["changed"]["comparison"]["changed"] == ["SKILL.md"]


@pytest.mark.parametrize(
    ("fixture", "reason_fragment"),
    [
        ("non-git", "Git worktree"),
        ("missing-ref", "origin/master"),
        ("untracked-skill", "origin/master"),
    ],
)
def test_baseline_holds_when_canonical_comparison_is_unavailable(
    tmp_path, fixture, reason_fragment,
):
    if fixture == "non-git":
        live = tmp_path / "outside/example"
        live.mkdir(parents=True)
        (live / "SKILL.md").write_text("live\n")
    elif fixture == "missing-ref":
        _, live, _ = comparison_repo(tmp_path)
    else:
        repo = tmp_path / "repository"
        repo.mkdir()
        git(repo, "init", "-b", "master")
        (repo / "README.md").write_text("tracked\n")
        canonical = commit_all(repo, "no skill")
        set_origin_master(repo, canonical)
        live = repo / "skills/example"
        live.mkdir(parents=True)
        (live / "SKILL.md").write_text("untracked skill\n")

    record = one_record(("example", live, "resolved"))

    assert record["status"] == "hold"
    assert reason_fragment in record["reason"]
    assert record["comparison"]["status"] == "unavailable"
    assert record["comparison"]["ref"] == "origin/master"
    assert record["comparison"]["reason"] == record["reason"]


def test_baseline_scrubs_inherited_git_environment(tmp_path):
    repo, live, initial = comparison_repo(tmp_path / "real")
    set_origin_master(repo, initial)
    fake = tmp_path / "fake"
    fake.mkdir()
    git(fake, "init", "-b", "master")
    (fake / "SKILL.md").write_text("redirect target\n")
    commit_all(fake, "fake")
    env = dict(os.environ, GIT_DIR=str(fake / ".git"), GIT_WORK_TREE=str(fake))

    record = one_record(("example", live, "resolved"), env=env)

    assert record["comparison"]["status"] == "match"
    assert record["comparison"]["commit"] == initial


def test_baseline_treats_pathspec_and_shell_metacharacters_as_names(tmp_path):
    marker = SCRIPTS.parent / "PWNED"
    assert not marker.exists()
    name = ":(glob)$(touch PWNED)*"
    repo, live, initial = comparison_repo(tmp_path, name=name)
    set_origin_master(repo, initial)

    record = one_record((name, live, "resolved"))

    assert record["name"] == name
    assert record["comparison"]["path"] == f"skills/{name}"
    assert record["comparison"]["status"] == "match"
    assert not marker.exists()


def test_baseline_omits_unsafe_name_and_path_from_output():
    name = "alias\nFOLLOW_PROMPT_INJECTION"
    path = "/tmp/live\rFOLLOW_PROMPT_INJECTION"

    result = baselines((name, path, "unsafe"))

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"status": "hold", "reason": "unsafe live skill path"},
    ]
    assert "FOLLOW_PROMPT_INJECTION" not in result.stdout


def test_baseline_rejects_partial_clone_metadata(tmp_path):
    repo, live, initial = comparison_repo(tmp_path)
    set_origin_master(repo, initial)
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")

    record = one_record(("example", live, "resolved"))

    assert record["status"] == "hold"
    assert "partial" in record["reason"]
    assert "promisor" in record["reason"]
    assert record["comparison"]["status"] == "unavailable"
    assert record["comparison"]["reason"] == record["reason"]


@pytest.mark.parametrize("arguments", [("name",), ("name", "path"), ("name", "path", "unexpected")])
def test_baseline_rejects_incomplete_or_unknown_triples(arguments):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "skill-review-baselines.py"), *arguments],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""


def test_git_stops_output_producer_before_unbounded_capture_completes(tmp_path, monkeypatch):
    """A producer beyond the cap must be stopped, not buffered to completion."""
    producer = tmp_path / "producer.py"
    marker = tmp_path / "producer-completed"
    producer.write_text(
        "import os, pathlib, sys\n"
        "chunk = b'x' * 65536\n"
        "try:\n"
        "    for _ in range(128):\n"
        "        os.write(1, chunk)\n"
        "except BrokenPipeError:\n"
        "    raise SystemExit(0)\n"
        "pathlib.Path(sys.argv[1]).write_text('completed')\n"
    )
    namespace = runpy.run_path(str(SCRIPTS / "skill-review-baselines.py"))
    original_popen = subprocess.Popen

    def substitute_git(arguments, *args, **kwargs):
        if isinstance(arguments, (list, tuple)) and arguments[0] == "/usr/bin/git":
            arguments = [sys.executable, str(producer), str(marker)]
        return original_popen(arguments, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", substitute_git)

    with pytest.raises(namespace["Unavailable"]):
        namespace["git"](tmp_path, "status")

    assert not marker.exists()
