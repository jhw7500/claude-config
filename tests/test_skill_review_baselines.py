"""Protect weekly review from staging bundles based on a stale live skill."""

import json
import os
from pathlib import Path
import shutil
import stat
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


def launcher_repository(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "master")
    copy_launcher(repo)
    live = repo / "skills/example"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text("stable live skill\n")
    anchor = observation(repo / "skill-observations/wlan")
    canonical = commit_all(repo, "launcher fixture")
    set_origin_master(repo, canonical)
    test_home = tmp_path / "home"
    links = test_home / ".claude/skills"
    links.mkdir(parents=True)
    (links / "example").symlink_to(live, target_is_directory=True)
    return repo, live, anchor, test_home


def prompt_from_capture(capture: Path) -> str:
    args = json.loads(capture.read_text())
    return args[args.index("-p") + 1]


def publication_api_fixture(tmp_path: Path):
    repo, live, _, test_home = launcher_repository(tmp_path)
    private_root = repo / "skill-observations/private"
    public_root = repo / "skill-observations/public"
    private_root.mkdir(parents=True)
    private_root.chmod(0o700)
    public_root.mkdir(parents=True)
    snapshot = private_root / "comparison.json"
    stage = private_root / "model"
    target = public_root / "run"
    namespace = runpy.run_path(str(SCRIPTS / "skill-review-baselines.py"))
    namespace["prepare"](
        snapshot,
        test_home / ".claude/skills",
        stage,
        target,
        ["example", str(live), "resolved"],
    )
    stage.mkdir()
    return namespace, live, snapshot, stage, target


def publication_audit_events(snapshot: Path) -> list[dict]:
    audit = snapshot.parent / "publication-audit"
    return [json.loads(path.read_text()) for path in sorted(audit.iterdir())]


def test_retain_evidence_creates_private_immutable_locator(tmp_path):
    api, _, snapshot, stage, target = publication_api_fixture(tmp_path)
    stage.mkdir(exist_ok=True)
    (stage / "draft").write_bytes(b"original model output")
    original_snapshot = snapshot.read_bytes()
    original_stage = (stage / "draft").read_bytes()
    previous_umask = os.umask(0)
    try:
        result = api["retain_evidence"](snapshot)
    finally:
        os.umask(previous_umask)

    locator = stage.parent / "private-evidence.json"
    info = locator.lstat()
    assert result == {"status": "retained", "evidence": str(locator)}
    assert stat.S_ISREG(info.st_mode)
    assert info.st_uid == os.getuid()
    assert info.st_nlink == 1
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert json.loads(locator.read_text("ascii")) == {
        "version": 1,
        "classification": "untrusted-data-only",
        "original_stage": str(stage),
        "snapshot": str(snapshot),
        "audit_directory": str(snapshot.parent / "publication-audit"),
        "public_target": str(target),
    }
    before = locator.stat()
    before_bytes = locator.read_bytes()
    with pytest.raises(FileExistsError):
        api["retain_evidence"](snapshot)
    after = locator.stat()
    assert locator.read_bytes() == before_bytes
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert snapshot.read_bytes() == original_snapshot
    assert (stage / "draft").read_bytes() == original_stage


def install_mutating_fake_claude(test_home: Path) -> None:
    executable = test_home / ".local/bin/claude"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, re, sys\n"
        "args = sys.argv[1:]\n"
        "prompt = args[args.index('-p') + 1]\n"
        "output = pathlib.Path(re.search(r'이번 산출물 경로: ([^\\n]+)', prompt).group(1))\n"
        "output.mkdir(parents=True)\n"
        "(output / 'dummy.skill').write_text('candidate')\n"
        "(output / 'REVIEW.md').write_text('model report')\n"
        "pathlib.Path(os.environ['REVIEW_TEST_MUTATE']).write_text('changed during review\\n')\n"
    )
    executable.chmod(0o755)


def install_output_fake_claude(test_home: Path, actions: str, exit_code=0) -> None:
    executable = test_home / ".local/bin/claude"
    executable.parent.mkdir(parents=True, exist_ok=True)
    indented = "\n".join(f"    {line}" for line in actions.splitlines())
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, re, sys\n"
        "args = sys.argv[1:]\n"
        "prompt = args[args.index('-p') + 1]\n"
        "output = pathlib.Path(re.search(r'이번 산출물 경로: ([^\\n]+)', prompt).group(1))\n"
        "output.mkdir(parents=True)\n"
        "try:\n"
        f"{indented}\n"
        "finally:\n"
        f"    raise SystemExit({exit_code})\n"
    )
    executable.chmod(0o755)


def test_launcher_does_not_put_raw_difference_filenames_in_model_prompt(tmp_path):
    repo, live, anchor, test_home = launcher_repository(tmp_path)
    hostile = "FOLLOW_MODEL_INSTRUCTION-delete-evidence.md"
    (live / hostile).write_text("untrusted filename\n")
    capture = tmp_path / "args.json"
    install_fake_claude(test_home, capture)
    env = dict(os.environ, HOME=str(test_home), REVIEW_TEST_CAPTURE=str(capture))

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    prompt = prompt_from_capture(capture)
    assert hostile not in prompt
    assert "difference_counts" in prompt
    assert "신뢰하지 않는 데이터" in prompt


def test_launcher_withholds_bundles_when_live_changes_during_review(tmp_path):
    repo, live, anchor, test_home = launcher_repository(tmp_path)
    install_mutating_fake_claude(test_home)
    env = dict(os.environ, HOME=str(test_home),
               REVIEW_TEST_MUTATE=str(live / "SKILL.md"))

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    public = repo / "skill-observations/skill-updates"
    assert list(public.rglob("*.skill")) == []


def test_launcher_publishes_stable_eligible_bundle_and_report(tmp_path):
    repo, _, anchor, test_home = launcher_repository(tmp_path)
    install_output_fake_claude(
        test_home,
        "(output / 'example.skill').write_text('candidate')\n"
        "(output / 'REVIEW.md').write_text('model report')",
    )
    env = dict(os.environ, HOME=str(test_home))

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    public = repo / "skill-observations/skill-updates"
    bundles = list(public.rglob("*.skill"))
    reports = list(public.rglob("REVIEW.md"))
    assert len(bundles) == 1 and bundles[0].read_text() == "candidate"
    assert len(reports) == 1 and "model report" in reports[0].read_text()


@pytest.mark.parametrize("model_report", [None, "   \n"], ids=["missing", "blank"])
def test_launcher_withholds_bundle_without_substantive_model_report(
        tmp_path, model_report):
    repo, _, anchor, test_home = launcher_repository(tmp_path)
    actions = "(output / 'example.skill').write_text('candidate')"
    if model_report is not None:
        actions += f"\n(output / 'REVIEW.md').write_text({model_report!r})"
    install_output_fake_claude(test_home, actions)

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=dict(os.environ, HOME=str(test_home)), capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    public = repo / "skill-observations/skill-updates"
    assert list(public.rglob("*.skill")) == []
    [report] = list(public.rglob("REVIEW.md"))
    assert "HOLD:" in report.read_text()


def test_launcher_detects_live_change_even_when_bytes_and_mtime_are_restored(tmp_path):
    repo, live, anchor, test_home = launcher_repository(tmp_path)
    install_output_fake_claude(
        test_home,
        "target = pathlib.Path(os.environ['REVIEW_TEST_MUTATE'])\n"
        "before = target.read_bytes()\n"
        "metadata = target.stat()\n"
        "target.write_text('transient change\\n')\n"
        "target.write_bytes(before)\n"
        "os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))\n"
        "(output / 'example.skill').write_text('candidate')\n"
        "(output / 'REVIEW.md').write_text('model report')",
    )
    env = dict(os.environ, HOME=str(test_home),
               REVIEW_TEST_MUTATE=str(live / "SKILL.md"))

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert list((repo / "skill-observations/skill-updates").rglob("*.skill")) == []


def test_launcher_detects_live_alias_retarget_during_review(tmp_path):
    repo, live, anchor, test_home = launcher_repository(tmp_path)
    replacement = tmp_path / "replacement"
    shutil.copytree(live, replacement)
    alias = test_home / ".claude/skills/example"
    install_output_fake_claude(
        test_home,
        "alias = pathlib.Path(os.environ['REVIEW_TEST_ALIAS'])\n"
        "alias.unlink()\n"
        "alias.symlink_to(os.environ['REVIEW_TEST_REPLACEMENT'], target_is_directory=True)\n"
        "(output / 'example.skill').write_text('candidate')\n"
        "(output / 'REVIEW.md').write_text('model report')",
    )
    env = dict(os.environ, HOME=str(test_home), REVIEW_TEST_ALIAS=str(alias),
               REVIEW_TEST_REPLACEMENT=str(replacement))

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert list((repo / "skill-observations/skill-updates").rglob("*.skill")) == []


def test_launcher_preserves_claude_failure_and_publishes_nothing(tmp_path):
    repo, _, anchor, test_home = launcher_repository(tmp_path)
    install_output_fake_claude(
        test_home,
        "(output / 'example.skill').write_text('candidate')\n"
        "(output / 'REVIEW.md').write_text('model report')",
        exit_code=23,
    )
    env = dict(os.environ, HOME=str(test_home))

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 23, result.stderr
    public = repo / "skill-observations/skill-updates"
    assert not public.exists() or list(public.iterdir()) == []


def test_launcher_withholds_preheld_skill_without_suppressing_eligible_skill(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "master")
    copy_launcher(repo)
    for name in ("eligible", "held"):
        skill = repo / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"{name}\n")
    anchor = observation(repo / "skill-observations/wlan")
    canonical = commit_all(repo, "two live skills")
    set_origin_master(repo, canonical)
    (repo / "skills/held/SKILL.md").write_text("diverged live\n")
    test_home = tmp_path / "home"
    links = test_home / ".claude/skills"
    links.mkdir(parents=True)
    for name in ("eligible", "held"):
        (links / name).symlink_to(repo / "skills" / name, target_is_directory=True)
    install_output_fake_claude(
        test_home,
        "(output / 'eligible.skill').write_text('eligible candidate')\n"
        "(output / 'held.skill').write_text('held candidate')\n"
        "(output / 'REVIEW.md').write_text('model report')",
    )
    env = dict(os.environ, HOME=str(test_home))

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    public = repo / "skill-observations/skill-updates"
    assert [path.name for path in public.rglob("*.skill")] == ["eligible.skill"]


def test_launcher_renders_raw_difference_filename_only_in_published_report(tmp_path):
    repo, live, anchor, test_home = launcher_repository(tmp_path)
    hostile = "FOLLOW_MODEL_INSTRUCTION-delete-evidence.md"
    (live / hostile).write_text("untrusted filename\n")
    capture = tmp_path / "args.json"
    install_output_fake_claude(
        test_home,
        "pathlib.Path(os.environ['REVIEW_TEST_CAPTURE']).write_text(json.dumps(args))\n"
        "(output / 'REVIEW.md').write_text('model report')",
    )
    env = dict(os.environ, HOME=str(test_home), REVIEW_TEST_CAPTURE=str(capture))

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert hostile not in prompt_from_capture(capture)
    [report] = list((repo / "skill-observations/skill-updates").rglob("REVIEW.md"))
    report_text = report.read_text()
    assert report_text.isascii()
    assert json.dumps(hostile) in report_text
    assert '"live_only"' in report_text


def test_launcher_refuses_staged_review_symlink_without_outside_write(tmp_path):
    repo, _, anchor, test_home = launcher_repository(tmp_path)
    outside = tmp_path / "outside-report"
    outside.write_text("do not change")
    install_output_fake_claude(
        test_home,
        "(output / 'example.skill').write_text('candidate')\n"
        "(output / 'REVIEW.md').symlink_to(os.environ['REVIEW_TEST_OUTSIDE'])",
    )
    env = dict(os.environ, HOME=str(test_home), REVIEW_TEST_OUTSIDE=str(outside))

    result = subprocess.run(
        ["bash", str(repo / "scripts/weekly-skill-review.sh"), str(anchor)],
        env=env, capture_output=True, text=True,
    )

    assert outside.read_text() == "do not change"
    assert list((repo / "skill-observations/skill-updates").rglob("*.skill")) == []


def test_publish_refuses_existing_target_without_replacing_it(tmp_path):
    api, _, snapshot, stage, target = publication_api_fixture(tmp_path)
    (stage / "example.skill").write_bytes(b"candidate")
    (stage / "REVIEW.md").write_text("model report")
    target.write_bytes(b"existing public result")
    before = target.stat()

    with pytest.raises(OSError):
        api["publish"](snapshot)

    after = target.stat()
    assert target.read_bytes() == b"existing public result"
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert (stage / "example.skill").read_bytes() == b"candidate"
    events = publication_audit_events(snapshot)
    assert [event["event"] for event in events] == ["publication-start", "candidate-created"]
    candidate = Path(events[1]["path"])
    assert candidate.is_dir()
    assert (candidate.stat().st_dev, candidate.stat().st_ino) == (
        events[1]["device"], events[1]["inode"])


def test_publish_revalidates_live_when_copying_a_bundle(tmp_path, monkeypatch):
    api, live, snapshot, stage, target = publication_api_fixture(tmp_path)
    staged_bundle = stage / "example.skill"
    staged_bundle.write_bytes(b"candidate")
    (stage / "REVIEW.md").write_text("model report")
    function_globals = api["publish"].__globals__
    original_write = function_globals["private_write"]
    changed = False

    def change_live_at_bundle_copy(path, data, mode=0o600):
        nonlocal changed
        if not changed and path.suffix == ".skill":
            changed = True
            (live / "SKILL.md").write_text("changed during publication\n")
        return original_write(path, data, mode)

    monkeypatch.setitem(function_globals, "private_write", change_live_at_bundle_copy)

    result = api["publish"](snapshot)

    assert changed
    assert result["status"] == "hold"
    assert list(target.rglob("*.skill")) == []
    assert "HOLD:" in (target / "REVIEW.md").read_text()
    assert staged_bundle.read_bytes() == b"candidate"
    events = publication_audit_events(snapshot)
    candidates = [event for event in events if event["event"] == "candidate-created"]
    assert len(candidates) == 2
    retained = Path(candidates[0]["path"])
    assert retained.is_dir()
    assert (retained / "example.skill").read_bytes() == b"candidate"
    assert (retained.stat().st_dev, retained.stat().st_ino) == (
        candidates[0]["device"], candidates[0]["inode"])
    assert (target.stat().st_dev, target.stat().st_ino) == (
        candidates[1]["device"], candidates[1]["inode"])
    assert result["audit_directory"] == str(snapshot.parent / "publication-audit")
    assert result["retained_candidates"] == [str(retained)]


def test_publish_records_partial_candidate_before_copy_failure(tmp_path, monkeypatch):
    api, _, snapshot, stage, target = publication_api_fixture(tmp_path)
    (stage / "example.skill").write_bytes(b"candidate")
    (stage / "REVIEW.md").write_text("model report")
    original_snapshot = snapshot.read_bytes()
    function_globals = api["publish"].__globals__
    original_write = function_globals["private_write"]

    def fail_bundle_copy(path, data, mode=0o600):
        if path.suffix == ".skill":
            original_write(path, b"partial", mode)
            raise OSError("injected copy failure")
        return original_write(path, data, mode)

    monkeypatch.setitem(function_globals, "private_write", fail_bundle_copy)

    with pytest.raises(OSError, match="injected copy failure"):
        api["publish"](snapshot)

    assert not target.exists()
    events = publication_audit_events(snapshot)
    candidate = next(event for event in events if event["event"] == "candidate-created")
    retained = Path(candidate["path"])
    assert retained.is_dir()
    assert (retained / "example.skill").read_bytes() == b"partial"
    assert (retained.stat().st_dev, retained.stat().st_ino) == (
        candidate["device"], candidate["inode"])
    assert (stage / "example.skill").read_bytes() == b"candidate"
    assert snapshot.read_bytes() == original_snapshot


def test_publish_audit_permissions_ignore_umask(tmp_path):
    api, _, snapshot, stage, target = publication_api_fixture(tmp_path)
    (stage / "example.skill").write_bytes(b"candidate")
    (stage / "REVIEW.md").write_text("model report")
    previous_umask = os.umask(0)
    try:
        result = api["publish"](snapshot)
    finally:
        os.umask(previous_umask)

    audit = Path(result["audit_directory"])
    assert stat.S_IMODE(audit.stat().st_mode) == 0o700
    assert audit.stat().st_uid == os.getuid()
    for path in audit.iterdir():
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode)
        assert info.st_uid == os.getuid()
        assert info.st_nlink == 1
        assert stat.S_IMODE(info.st_mode) == 0o600
    assert target.is_dir()


def test_publish_refuses_preexisting_audit_symlink_without_outside_write(tmp_path):
    api, _, snapshot, stage, target = publication_api_fixture(tmp_path)
    (stage / "REVIEW.md").write_text("model report")
    outside = tmp_path / "outside"
    outside.mkdir()
    (snapshot.parent / "publication-audit").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        api["publish"](snapshot)

    assert list(outside.iterdir()) == []
    assert not target.exists()


def test_publish_rechecks_stage_when_copying_a_bundle(tmp_path, monkeypatch):
    api, _, snapshot, stage, target = publication_api_fixture(tmp_path)
    staged_bundle = stage / "example.skill"
    staged_bundle.write_bytes(b"candidate")
    (stage / "REVIEW.md").write_text("model report")
    function_globals = api["publish"].__globals__
    original_write = function_globals["private_write"]
    changed = False

    def change_stage_at_bundle_copy(path, data, mode=0o600):
        nonlocal changed
        if not changed and path.suffix == ".skill":
            changed = True
            staged_bundle.write_bytes(b"changed after validation")
        return original_write(path, data, mode)

    monkeypatch.setitem(function_globals, "private_write", change_stage_at_bundle_copy)

    result = api["publish"](snapshot)

    assert changed
    assert result["status"] == "hold"
    assert list(target.rglob("*.skill")) == []
    assert "HOLD:" in (target / "REVIEW.md").read_text()
    assert staged_bundle.read_bytes() == b"changed after validation"


def test_published_report_discloses_final_live_boundary_before_install(
        tmp_path, monkeypatch):
    api, live, snapshot, stage, target = publication_api_fixture(tmp_path)
    (stage / "example.skill").write_bytes(b"point-in-time candidate")
    (stage / "REVIEW.md").write_text("model report")
    function_globals = api["publish"].__globals__
    original_rename = function_globals["rename_noreplace"]
    concurrent_live = b"changed at final publication boundary\n"
    boundary_reached = False

    def change_live_at_final_rename(source, destination, source_parent, target_parent):
        nonlocal boundary_reached
        boundary_reached = True
        (live / "SKILL.md").write_bytes(concurrent_live)
        return original_rename(source, destination, source_parent, target_parent)

    monkeypatch.setitem(
        function_globals, "rename_noreplace", change_live_at_final_rename)

    result = api["publish"](snapshot)

    assert boundary_reached
    assert result["status"] == "published"
    assert (target / "example.skill").read_bytes() == b"point-in-time candidate"
    assert (live / "SKILL.md").read_bytes() == concurrent_live
    report = (target / "REVIEW.md").read_text().lower()
    assert "installed: 0" in report
    assert any(term in report for term in ("point-in-time", "snapshot-bound", "specific-time"))
    assert "live" in report
    assert any(term in report for term in ("recheck", "re-check", "verify again"))
    assert any(term in report for term in ("before install", "before installation"))
    assert "approval" in report


def test_publish_holds_stage_with_too_many_empty_directories(tmp_path):
    api, _, snapshot, stage, target = publication_api_fixture(tmp_path)
    (stage / "REVIEW.md").write_text("model report")
    for index in range(4096):
        (stage / f"empty-{index:04d}").mkdir()

    result = api["publish"](snapshot)

    assert result["status"] == "hold"
    assert list(target.rglob("*.skill")) == []
    assert "HOLD:" in (target / "REVIEW.md").read_text()


def test_publish_holds_directory_tree_deeper_than_limit(tmp_path):
    api, _, snapshot, stage, target = publication_api_fixture(tmp_path)
    (stage / "REVIEW.md").write_text("model report")
    nested = stage
    for index in range(65):
        nested = nested / f"level-{index:02d}"
        nested.mkdir()

    result = api["publish"](snapshot)

    assert result["status"] == "hold"
    assert list(target.rglob("*.skill")) == []
    assert "HOLD:" in (target / "REVIEW.md").read_text()


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
    assert record["comparison"]["difference_counts"] == {
        "changed": 1,
        "live_only": 0,
        "origin_only": 0,
    }
    assert "path" not in record["comparison"]
    assert "changed" not in record["comparison"]
    assert "live_only" not in record["comparison"]
    assert "origin_only" not in record["comparison"]


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
