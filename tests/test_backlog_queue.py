import json
import os
from pathlib import Path

import pytest

from scripts.backlog_checkpoint.queue import collect_queue, rank_issues


REPO = "octo/example"
WEB = "https://github.com/octo/example/issues"
API = "https://api.github.com/repos/octo/example"


def issue(number: int, *, state: str = "open", repo: str = REPO) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "state": state,
        "title": f"Issue {number}",
    }


def dependency(blocker: dict, blocked: dict) -> dict:
    return {
        "blocker_url": blocker["html_url"],
        "blocker_state": blocker["state"],
        "blocked_url": blocked["html_url"],
    }


def incomplete_kind(result: dict) -> str:
    assert result["status"] == "INCOMPLETE"
    assert result["queue"] == []
    assert result["blocked"] == []
    assert len(result["reasons"]) == 1
    return result["reasons"][0]["kind"]


def write_fake_gh(tmp_path: Path, responses: dict[str, list[dict]]) -> Path:
    executable = tmp_path / "gh"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

if sys.argv[1:4] != ["api", "--method", "GET"]:
    print("read-only GET required", file=sys.stderr)
    raise SystemExit(64)
if sys.argv[4] != "--include" or len(sys.argv) != 6:
    print("unexpected argv", file=sys.stderr)
    raise SystemExit(65)
responses = json.loads(os.environ["FAKE_GH_RESPONSES"])
url = sys.argv[5]
items = responses.get(url)
if not items:
    print(f"no response for {url}", file=sys.stderr)
    raise SystemExit(66)
response = items.pop(0)
responses[url] = items
os.environ["FAKE_GH_RESPONSES"] = json.dumps(responses)
if response.get("exit", 0):
    print(response.get("stderr", "failed"), file=sys.stderr)
    raise SystemExit(response["exit"])
print(response["headers"])
print()
print(json.dumps(response["body"], separators=(",", ":")))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def response(body: list[dict], *, link: str | None = None) -> dict:
    headers = "HTTP/2.0 200 OK\ncontent-type: application/json"
    if link is not None:
        headers += f'\nlink: <{link}>; rel="next"'
    return {"headers": headers, "body": body}


def install_fake_gh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, responses: dict[str, list[dict]]
) -> None:
    write_fake_gh(tmp_path, responses)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_RESPONSES", json.dumps(responses))


def test_rank_uses_kahn_number_ties_and_separates_cycles_from_downstream() -> None:
    issues = [issue(number) for number in [8, 7, 6, 5, 4, 3, 2, 1]]
    edges = [
        dependency(issue(1), issue(3)),
        dependency(issue(2), issue(3)),
        dependency(issue(3), issue(4)),
        dependency(issue(5), issue(6)),
        dependency(issue(6), issue(5)),
        dependency(issue(6), issue(7)),
        dependency(issue(99, state="closed"), issue(8)),
    ]

    result = rank_issues(issues, edges)

    assert result == {
        "status": "COMPLETE",
        "queue": [f"{WEB}/{number}" for number in [1, 2, 3, 4, 8]],
        "blocked": [f"{WEB}/{number}" for number in [5, 6, 7]],
        "reasons": [
            {
                "issue_url": f"{WEB}/5",
                "kind": "CYCLE",
                "blockers": [f"{WEB}/5", f"{WEB}/6"],
            },
            {
                "issue_url": f"{WEB}/6",
                "kind": "CYCLE",
                "blockers": [f"{WEB}/5", f"{WEB}/6"],
            },
            {
                "issue_url": f"{WEB}/7",
                "kind": "DOWNSTREAM_OF_CYCLE",
                "blockers": [f"{WEB}/5", f"{WEB}/6"],
            },
            {
                "issue_url": f"{WEB}/8",
                "kind": "CLOSED_SATISFIED",
                "blockers": [f"{WEB}/99"],
            },
        ],
    }


def test_rank_treats_closed_blockers_as_satisfied_and_propagates_external_open() -> (
    None
):
    issues = [issue(1), issue(2), issue(3)]
    external_open = issue(90, repo="octo/other")
    external_closed_low = issue(89, state="closed", repo="octo/other")
    external_closed_high = issue(91, state="closed", repo="octo/other")
    edges = [
        dependency(external_closed_high, issue(2)),
        dependency(external_closed_low, issue(2)),
        dependency(external_closed_high, issue(2)),
        dependency(external_open, issue(2)),
        dependency(issue(2), issue(3)),
    ]

    result = rank_issues(issues, edges)

    assert result == {
        "status": "COMPLETE",
        "queue": [f"{WEB}/1"],
        "blocked": [f"{WEB}/2", f"{WEB}/3"],
        "reasons": [
            {
                "issue_url": f"{WEB}/2",
                "kind": "CLOSED_SATISFIED",
                "blockers": [
                    external_closed_low["html_url"],
                    external_closed_high["html_url"],
                ],
            },
            {
                "issue_url": f"{WEB}/2",
                "kind": "EXTERNAL_OPEN",
                "blockers": [external_open["html_url"]],
            },
            {
                "issue_url": f"{WEB}/3",
                "kind": "EXTERNAL_OPEN",
                "blockers": [external_open["html_url"]],
            },
        ],
    }


def test_rank_deduplicates_edges_and_handles_a_1000_issue_chain_iteratively() -> None:
    issues = [issue(number) for number in range(1, 1001)]
    edges = [dependency(issue(number), issue(number + 1)) for number in range(1, 1000)]
    edges.append(dependency(issue(500), issue(501)))

    result = rank_issues(iter(issues), iter(edges))

    assert result["status"] == "COMPLETE"
    assert result["queue"] == [f"{WEB}/{number}" for number in range(1, 1001)]
    assert result["blocked"] == []
    assert result["reasons"] == []


@pytest.mark.parametrize("target", ["issues", "dependencies"])
def test_rank_stops_consuming_an_oversized_iterator_at_limit_plus_one(
    target: str,
) -> None:
    limit = 1_000 if target == "issues" else 10_000
    calls = 0

    def oversized():
        nonlocal calls
        for index in range(limit + 1):
            calls += 1
            if target == "issues":
                yield issue(index + 1)
            else:
                yield dependency(issue(1), issue(1))
        raise RuntimeError("consumed past cap")

    issues = oversized() if target == "issues" else [issue(1)]
    dependencies = oversized() if target == "dependencies" else []

    result = rank_issues(issues, dependencies)

    assert incomplete_kind(result) == "INVALID_INPUT"
    assert calls == limit + 1


def test_rank_does_not_misclassify_an_acyclic_cross_edge_as_a_cycle() -> None:
    issues = [issue(number) for number in [1, 2, 3, 4]]
    edges = [
        dependency(issue(1), issue(3)),
        dependency(issue(1), issue(4)),
        dependency(issue(3), issue(4)),
    ]

    result = rank_issues(issues, edges)

    assert result == {
        "status": "COMPLETE",
        "queue": [f"{WEB}/{number}" for number in [1, 2, 3, 4]],
        "blocked": [],
        "reasons": [],
    }


@pytest.mark.parametrize(
    ("issues", "edges"),
    [
        ([issue(1) | {"state": "OPEN"}], []),
        ([issue(1), issue(1)], []),
        ([issue(1) | {"html_url": f"{WEB}/2"}], []),
        ([issue(1)], [{"blocker_url": f"{WEB}/1", "blocker_state": "open"}]),
        (
            [issue(1)],
            [
                {
                    "blocker_url": f"{WEB}/1",
                    "blocker_state": "open",
                    "blocked_url": f"{WEB}/2",
                }
            ],
        ),
    ],
)
def test_rank_invalid_or_incomplete_input_never_publishes_a_queue(
    issues: list[dict], edges: list[dict]
) -> None:
    assert incomplete_kind(rank_issues(issues, edges)) == "INVALID_INPUT"


def test_collect_pages_issues_and_blocked_by_with_a_real_fake_gh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    issues_url = f"{API}/issues?state=open&per_page=100"
    issues_page_2 = f"{API}/issues?page=2&per_page=100&state=open"
    blocked_1 = f"{API}/issues/1/dependencies/blocked_by?per_page=100"
    blocked_2 = f"{API}/issues/2/dependencies/blocked_by?per_page=100"
    blocked_2_page_2 = f"{API}/issues/2/dependencies/blocked_by?page=2&per_page=100"
    responses = {
        issues_url: [response([issue(1)], link=issues_page_2)],
        issues_page_2: [
            response(
                [
                    issue(2),
                    issue(99) | {"pull_request": {"url": f"{API}/pulls/99"}},
                ]
            )
        ],
        blocked_1: [response([])],
        blocked_2: [response([], link=blocked_2_page_2)],
        blocked_2_page_2: [response([issue(1)])],
    }
    install_fake_gh(monkeypatch, tmp_path, responses)

    result = collect_queue(REPO)

    assert result == {
        "status": "COMPLETE",
        "queue": [f"{WEB}/1", f"{WEB}/2"],
        "blocked": [],
        "reasons": [],
    }


@pytest.mark.parametrize("failure", ["issues", "relations-later-page"])
def test_collect_never_publishes_partial_pagination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    issues_url = f"{API}/issues?state=open&per_page=100"
    issues_page_2 = f"{API}/issues?page=2&per_page=100&state=open"
    blocked_1 = f"{API}/issues/1/dependencies/blocked_by?per_page=100"
    blocked_page_2 = f"{API}/issues/1/dependencies/blocked_by?page=2&per_page=100"
    if failure == "issues":
        responses = {
            issues_url: [response([issue(1)], link=issues_page_2)],
            issues_page_2: [{"exit": 1, "stderr": "page failed"}],
        }
    else:
        responses = {
            issues_url: [response([issue(1)])],
            blocked_1: [response([], link=blocked_page_2)],
            blocked_page_2: [{"exit": 1, "stderr": "page failed"}],
        }
    install_fake_gh(monkeypatch, tmp_path, responses)

    assert incomplete_kind(collect_queue(REPO)) == "COLLECTION_FAILED"


@pytest.mark.parametrize(
    ("next_url", "kind"),
    [
        ("https://evil.example/repos/octo/example/issues?page=2", "UNSAFE_PAGINATION"),
        (
            f"{API}/issues/1/dependencies/blocked_by?page=2",
            "UNSAFE_PAGINATION",
        ),
        (f"{API}/issues?state=open&per_page=100", "REPEATED_PAGINATION"),
    ],
)
def test_collect_rejects_cross_origin_wrong_resource_and_repeated_next_links(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, next_url: str, kind: str
) -> None:
    issues_url = f"{API}/issues?state=open&per_page=100"
    install_fake_gh(
        monkeypatch,
        tmp_path,
        {issues_url: [response([issue(1)], link=next_url)]},
    )

    assert incomplete_kind(collect_queue(REPO)) == kind


def test_collect_enforces_open_issue_cap_before_dependency_queries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    issues_url = f"{API}/issues?state=open&per_page=100"
    install_fake_gh(
        monkeypatch,
        tmp_path,
        {issues_url: [response([issue(number) for number in range(1, 1002)])]},
    )

    assert incomplete_kind(collect_queue(REPO)) == "ISSUE_CAP_EXCEEDED"


def test_collect_enforces_relation_cap_and_deduplicates_before_ranking() -> None:
    issue_records = [issue(1), issue(2)]
    duplicate = issue(1)

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, body: list[dict]) -> None:
            self.stdout = "HTTP/2.0 200 OK\n\n" + json.dumps(body)

    calls = 0

    def runner(argv: list[str], **kwargs: object) -> Result:
        nonlocal calls
        calls += 1
        assert kwargs["shell"] is False
        if calls == 1:
            return Result(issue_records)
        if calls == 2:
            return Result([])
        return Result([duplicate] * 10_001)

    result = collect_queue(REPO, runner=runner)

    assert incomplete_kind(result) == "RELATION_CAP_EXCEEDED"


def test_collect_explicit_edges_look_up_closed_blockers_before_ranking() -> None:
    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, body: object) -> None:
            self.stdout = "HTTP/2.0 200 OK\n\n" + json.dumps(body)

    responses = iter(
        [
            Result([issue(1), issue(8)]),
            Result([]),
            Result([]),
            Result(issue(99, state="closed")),
        ]
    )

    result = collect_queue(
        REPO,
        explicit_edges=[(f"{WEB}/99", f"{WEB}/8")],
        runner=lambda argv, **kwargs: next(responses),
    )

    assert result == {
        "status": "COMPLETE",
        "queue": [f"{WEB}/1", f"{WEB}/8"],
        "blocked": [],
        "reasons": [
            {
                "issue_url": f"{WEB}/8",
                "kind": "CLOSED_SATISFIED",
                "blockers": [f"{WEB}/99"],
            }
        ],
    }


def test_collect_failed_explicit_blocker_lookup_never_publishes_a_queue() -> None:
    class Result:
        stderr = ""

        def __init__(self, body: object, returncode: int = 0) -> None:
            self.returncode = returncode
            self.stdout = "HTTP/2.0 200 OK\n\n" + json.dumps(body)

    responses = iter(
        [
            Result([issue(8)]),
            Result([]),
            Result({}, returncode=1),
        ]
    )

    result = collect_queue(
        REPO,
        explicit_edges=[(f"{WEB}/99", f"{WEB}/8")],
        runner=lambda argv, **kwargs: next(responses),
    )

    assert incomplete_kind(result) == "COLLECTION_FAILED"
