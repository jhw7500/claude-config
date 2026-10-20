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


def test_a_long_multibyte_title_still_fits_a_filesystem_name(tmp_path):
    # Sixty four-byte characters exceed a 255-byte name component once the timestamp is prepended.
    _, result = export(tmp_path, title="\U00020000" * 60)
    entry = entry_of(result)
    data = json.loads((entry / "entry.json").read_text(encoding="utf-8"))
    assert len(entry.name.encode("utf-8")) <= 255
    assert len(data["document"].encode("utf-8")) <= 255
    assert data["title"] == "\U00020000" * 60


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


def record(entry, *args):
    return run("record", "--entry", str(entry), "--state", *args)


def test_record_moves_through_publishing_published_and_verified(tmp_path):
    _, result = export(tmp_path)
    entry = entry_of(result)
    assert record(entry, "publishing").returncode == 0
    assert record(entry, "published", "--url", URL, "--account", "user@example.com").returncode == 0
    assert record(entry, "verified", "--version", "v1").returncode == 0
    data = json.loads((entry / "entry.json").read_text(encoding="utf-8"))
    assert (data["state"], data["url"], data["account"], data["version"]) == ("verified", URL, "user@example.com", "v1")
    assert [item["state"] for item in data["history"]] == ["publishing", "published", "verified"]


def test_an_unrecorded_publish_attempt_blocks_a_second_publish(tmp_path):
    # The session published and died before recording the URL: the entry is still in publishing.
    _, result = export(tmp_path)
    entry = entry_of(result)
    assert record(entry, "publishing").returncode == 0
    blocked = record(entry, "publishing")
    assert refused(blocked) and "earlier publish attempt was not recorded" in blocked.stderr
    assert record(entry, "published", "--url", URL).returncode == 0


def test_a_second_new_artifact_url_is_refused_so_a_retry_cannot_duplicate(tmp_path):
    _, result = export(tmp_path)
    entry = entry_of(result)
    assert record(entry, "publishing").returncode == 0
    assert record(entry, "published", "--url", URL).returncode == 0
    assert record(entry, "failed", "--reason", "read-back mismatch").returncode == 0
    assert record(entry, "publishing").returncode == 0
    assert refused(record(entry, "published", "--url", URL[:-1] + "1"))
    assert record(entry, "published", "--url", URL).returncode == 0


def test_update_mode_accepts_only_its_target_url(tmp_path):
    _, result = export(tmp_path, "--update-url", URL)
    entry = entry_of(result)
    assert record(entry, "publishing").returncode == 0
    assert refused(record(entry, "published", "--url", URL[:-1] + "1"))
    assert record(entry, "published", "--url", URL).returncode == 0


def test_repeated_failures_keep_every_reason(tmp_path):
    _, result = export(tmp_path)
    entry = entry_of(result)
    assert record(entry, "failed", "--reason", "publish tool refused").returncode == 0
    assert record(entry, "failed", "--reason", "retry also refused").returncode == 0
    data = json.loads((entry / "entry.json").read_text(encoding="utf-8"))
    assert [item.get("reason") for item in data["history"]] == ["publish tool refused", "retry also refused"]


@pytest.mark.parametrize(
    ("steps", "refused_step"),
    [
        ((), ("verified",)),
        ((), ("published", "--url", URL)),
        ((("publishing",), ("published", "--url", URL), ("verified",)), ("publishing",)),
    ],
    ids=("verify-before-publish", "published-without-publishing", "publish-after-verified"),
)
def test_record_refuses_out_of_order_states(tmp_path, steps, refused_step):
    _, result = export(tmp_path)
    entry = entry_of(result)
    for step in steps:
        assert record(entry, *step).returncode == 0
    assert refused(record(entry, *refused_step))


def test_filesystem_and_damaged_entry_errors_use_the_script_contract(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    source = tmp_path / "doc.md"
    source.write_text("body\n", encoding="utf-8")
    assert refused(run("export", "--outbox", str(blocker / "outbox"), "--title", "t", "--source", str(source)))
    _, result = export(tmp_path)
    entry = entry_of(result)
    data = json.loads((entry / "entry.json").read_text(encoding="utf-8"))
    del data["document"]
    (entry / "entry.json").write_text(json.dumps(data), encoding="utf-8")
    assert refused(run("show", "--entry", str(entry)))
    assert refused(record(entry, "failed", "--reason", "x"))
