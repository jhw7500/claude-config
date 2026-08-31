import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        ("https://github.com/jhw7500/claude-config.git", "jhw7500/claude-config"),
        ("git@github.com:jhw7500/claude-config.git", "jhw7500/claude-config"),
        ("ssh://git@github.com/jhw7500/claude-config", "jhw7500/claude-config"),
        ("HTTPS://GITHUB.COM/JHW7500/Claude-Config.git", "jhw7500/claude-config"),
    ],
)
def test_parse_github_slug_accepts_exact_origin_forms(core, remote, slug):
    assert core.parse_github_slug(remote) == slug


@pytest.mark.parametrize(
    "remote",
    [
        "https://token@github.com/jhw7500/claude-config.git",
        "https://gitlab.com/jhw7500/claude-config.git",
        "git@github.com:jhw7500/extra/claude-config.git",
        "https://github.com/jhw7500/claude-config/issues",
        "https://github.com/jhw7500/claude config.git",
    ],
)
def test_parse_github_slug_rejects_ambiguous_or_sensitive_forms(core, remote):
    assert core.parse_github_slug(remote) is None


def portfolio_payload(*, slugs=(), truncated=False):
    repositories = [
        {"repo_id": f"repo-{index}", "slug": slug, "allow_public": False}
        for index, slug in enumerate(slugs, start=1)
    ]
    items = []
    if repositories:
        items = [
            {
                "project_id": "project-1",
                "title": "Project",
                "repo_ids": [repository["repo_id"] for repository in repositories],
            }
        ]
    result = {
        "page_id": "page-1",
        "items": items,
        "repositories": repositories,
        "truncated": truncated,
        "total_items": len(items),
    }
    if truncated:
        result["next_page_id"] = "page-2"
    return {"command": "portfolio status", "result": result}


def portfolio_bytes(**kwargs):
    return json.dumps(portfolio_payload(**kwargs)).encode()


def test_portfolio_exact_hit_is_registered_even_when_truncated(core):
    result = core.parse_portfolio_output(
        portfolio_bytes(slugs=("JHW7500/CLAUDE-CONFIG",), truncated=True),
        "jhw7500/claude-config",
    )
    assert result.status is core.RegistrationStatus.REGISTERED


def test_portfolio_complete_miss_is_unregistered(core):
    result = core.parse_portfolio_output(portfolio_bytes(), "jhw7500/claude-config")
    assert result.status is core.RegistrationStatus.UNREGISTERED


def test_portfolio_truncated_miss_is_unknown(core):
    result = core.parse_portfolio_output(
        portfolio_bytes(slugs=("jhw7500/other",), truncated=True),
        "jhw7500/claude-config",
    )
    assert result == core.RegistrationResult(
        core.RegistrationStatus.UNKNOWN,
        "jhw7500/claude-config",
        "PORTFOLIO_RESULT_INCOMPLETE",
    )


def test_portfolio_rejects_duplicate_json_keys(core):
    raw = b'{"command":"portfolio status","command":"other","result":{}}'
    result = core.parse_portfolio_output(raw, "jhw7500/claude-config")
    assert result.reason == "PORTFOLIO_UNAVAILABLE"


@pytest.mark.parametrize("raw", [b"[]", b"null", b"true"])
def test_portfolio_rejects_non_object_json_roots(core, raw):
    result = core.parse_portfolio_output(raw, "jhw7500/claude-config")
    assert result == core.RegistrationResult(
        core.RegistrationStatus.UNKNOWN,
        "jhw7500/claude-config",
        "PORTFOLIO_UNAVAILABLE",
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(command="other"),
        lambda payload: payload.update(result=[]),
        lambda payload: payload["result"].pop("page_id"),
        lambda payload: payload.update(extra=True),
        lambda payload: payload["result"].update(total_items=True),
        lambda payload: payload["result"].update(truncated=True),
        lambda payload: payload["result"].update(next_page_id="bad"),
        lambda payload: payload["result"].update(repositories=[{}]),
        lambda payload: payload["result"].update(items=[{}], total_items=1),
        lambda payload: payload["result"].update(total_items=2),
        lambda payload: payload["result"].update(
            repositories=[{"repo_id": "repo-1", "slug": "jhw7500/good", "allow_public": False}],
            items=[{"project_id": "project-1", "title": "Project", "repo_ids": ["repo-missing"]}],
            total_items=1,
        ),
    ],
)
def test_portfolio_rejects_malformed_projected_envelopes(core, mutate):
    payload = portfolio_payload(slugs=("jhw7500/other",))
    mutate(payload)
    result = core.parse_portfolio_output(json.dumps(payload).encode(), "jhw7500/claude-config")
    assert result == core.RegistrationResult(
        core.RegistrationStatus.UNKNOWN,
        "jhw7500/claude-config",
        "PORTFOLIO_UNAVAILABLE",
    )


@pytest.mark.parametrize("raw", [b"\xff", b"x" * (12 * 1024 + 1)])
def test_portfolio_rejects_invalid_utf8_or_oversized_output(core, raw):
    result = core.parse_portfolio_output(raw, "jhw7500/claude-config")
    assert result.reason == "PORTFOLIO_UNAVAILABLE"


def test_resolve_repository_uses_only_origin_with_exact_git_argv(core):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        stdout = b"/workspace/project\n" if len(calls) == 1 else b"git@github.com:jhw7500/claude-config.git\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    identity = core.resolve_repository(Path("/workspace/project/subdir"), runner=runner)

    assert identity == core.RepositoryIdentity(Path("/workspace/project"), "jhw7500/claude-config")
    assert [call[0] for call in calls] == [
        ["/usr/bin/git", "-C", "/workspace/project/subdir", "rev-parse", "--show-toplevel"],
        ["/usr/bin/git", "-C", "/workspace/project", "config", "--get", "remote.origin.url"],
    ]


def test_query_registration_uses_exact_launcher_and_rejects_injected_oversized_output(core):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"x" * (12 * 1024 + 1), stderr=b"")

    result = core.query_registration(
        core.RepositoryIdentity(Path("/workspace/project"), "jhw7500/claude-config"),
        Path("/safe/home"),
        runner=runner,
    )

    assert result == core.RegistrationResult(
        core.RegistrationStatus.UNKNOWN,
        "jhw7500/claude-config",
        "PORTFOLIO_UNAVAILABLE",
    )
    assert calls[0][0] == ["/safe/home/.local/bin/jhw-control-host", "portfolio", "status"]
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["timeout"] == 15


def test_bounded_launcher_reaps_child_that_closes_pipes_before_timeout(core, monkeypatch):
    processes = []
    original_popen = core.subprocess.Popen

    def tracked_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(core.subprocess, "Popen", tracked_popen)
    monkeypatch.setattr(core, "LAUNCHER_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    result = core._bounded_launcher_run(
        [
            sys.executable,
            "-c",
            "import os, time; os.close(1); os.close(2); time.sleep(5)",
        ]
    )

    assert result is None
    assert time.monotonic() - started < 1
    assert len(processes) == 1
    assert processes[0].poll() is not None
