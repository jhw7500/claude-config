"""Stage confirmed Markdown for a claude.ai Artifact and record publish results without duplicating an artifact."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "artifact-outbox.py"
URL = "https://claude.ai/code/artifact/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"


def run(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


def refused(result):
    # Exit 2 alone is also Python's "can't open file"; the script's own prefix proves the script refused.
    return result.returncode == 2 and result.stderr.startswith("artifact-outbox: ")


def export(tmp_path, *extra, text="# 회의 요약\n\n결정: A안\n", title="회의 요약 2026-10-20"):
    source = tmp_path / "source.md"
    source.write_text(text, encoding="utf-8")
    result = run("export", "--outbox", str(tmp_path / "outbox"), "--title", title, "--source", str(source), *extra)
    return source, result


def entry_of(result):
    assert result.returncode == 0, result.stderr
    return Path(json.loads(result.stdout)["entry"])


def test_export_stages_a_private_copy_with_its_digest(tmp_path):
    source, result = export(tmp_path)
    entry = entry_of(result)
    data = json.loads((entry / "entry.json").read_text(encoding="utf-8"))
    document = entry / data["document"]
    assert document.read_bytes() == source.read_bytes()
    assert data["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert (data["title"], data["mode"], data["target_url"], data["state"]) == ("회의 요약 2026-10-20", "new", None, "exported")
    assert document.suffix == ".md" and "회의" in document.name
    assert stat.S_IMODE(entry.stat().st_mode) == 0o700
    assert stat.S_IMODE((entry / "entry.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(document.stat().st_mode) == 0o600
    assert str(entry) in json.loads(result.stdout)["next_step"]


def test_default_outbox_is_outside_the_repository_under_home(tmp_path):
    source = tmp_path / "doc.md"
    source.write_text("body\n", encoding="utf-8")
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    entry = entry_of(run("export", "--title", "t", "--source", str(source), env=env))
    assert entry.parent == tmp_path / "home" / ".local" / "share" / "claude-config" / "artifact-outbox"


def test_update_records_the_target_url(tmp_path):
    _, result = export(tmp_path, "--update-url", URL)
    data = json.loads((entry_of(result) / "entry.json").read_text(encoding="utf-8"))
    assert (data["mode"], data["target_url"]) == ("update", URL)


@pytest.mark.parametrize(
    ("text", "title", "extra"),
    [
        (b"\xff\xfe not utf-8", "t", ()),
        ("", "t", ()),
        ("x" * (256 * 1024 + 1), "t", ()),
        ("body", "two\nlines", ()),
        ("body", " ", ()),
        ("body", "t", ("--update-url", "https://example.com/artifact/1")),
    ],
    ids=("not-utf8", "empty", "too-large", "multiline-title", "blank-title", "foreign-url"),
)
def test_export_rejects_invalid_input_and_stages_nothing(tmp_path, text, title, extra):
    source = tmp_path / "source.md"
    if isinstance(text, bytes):
        source.write_bytes(text)
    else:
        source.write_text(text, encoding="utf-8")
    result = run("export", "--outbox", str(tmp_path / "outbox"), "--title", title, "--source", str(source), *extra)
    assert refused(result), (result.returncode, result.stderr)
    assert not (tmp_path / "outbox").exists() or not any((tmp_path / "outbox").iterdir())


def test_show_detects_a_document_changed_after_export(tmp_path):
    _, result = export(tmp_path)
    entry = entry_of(result)
    shown = run("show", "--entry", str(entry))
    assert shown.returncode == 0 and json.loads(shown.stdout)["digest_ok"] is True
    data = json.loads((entry / "entry.json").read_text(encoding="utf-8"))
    (entry / data["document"]).write_text("changed\n", encoding="utf-8")
    shown = run("show", "--entry", str(entry))
    assert shown.returncode == 3 and json.loads(shown.stdout)["digest_ok"] is False


def test_record_moves_through_published_and_verified(tmp_path):
    _, result = export(tmp_path)
    entry = entry_of(result)
    assert run("record", "--entry", str(entry), "--state", "published", "--url", URL,
               "--account", "user@example.com").returncode == 0
    assert run("record", "--entry", str(entry), "--state", "verified", "--version", "v1").returncode == 0
    data = json.loads((entry / "entry.json").read_text(encoding="utf-8"))
    assert (data["state"], data["url"], data["account"], data["version"]) == ("verified", URL, "user@example.com", "v1")
    assert [item["state"] for item in data["history"]] == ["published", "verified"]


def test_a_second_new_artifact_url_is_refused_so_a_retry_cannot_duplicate(tmp_path):
    _, result = export(tmp_path)
    entry = entry_of(result)
    assert run("record", "--entry", str(entry), "--state", "published", "--url", URL).returncode == 0
    assert run("record", "--entry", str(entry), "--state", "failed", "--reason", "read-back mismatch").returncode == 0
    other = URL[:-1] + "1"
    assert refused(run("record", "--entry", str(entry), "--state", "published", "--url", other))
    assert run("record", "--entry", str(entry), "--state", "published", "--url", URL).returncode == 0


def test_update_mode_accepts_only_its_target_url(tmp_path):
    _, result = export(tmp_path, "--update-url", URL)
    entry = entry_of(result)
    assert refused(run("record", "--entry", str(entry), "--state", "published", "--url", URL[:-1] + "1"))
    assert run("record", "--entry", str(entry), "--state", "published", "--url", URL).returncode == 0


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("verified",), None),
        (("published", "--url", URL), ("verified",)),
    ],
    ids=("verify-before-publish", "publish-after-verified"),
)
def test_record_refuses_out_of_order_states(tmp_path, first, second):
    _, result = export(tmp_path)
    entry = entry_of(result)
    if second is None:
        assert refused(run("record", "--entry", str(entry), "--state", *first))
        return
    assert run("record", "--entry", str(entry), "--state", *first).returncode == 0
    assert run("record", "--entry", str(entry), "--state", *second).returncode == 0
    assert refused(run("record", "--entry", str(entry), "--state", "published", "--url", URL))
