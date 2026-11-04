"""Bounded, read-only GitHub dependency collection and deterministic ranking.

``rank_issues`` accepts native-style open Issue records with ``number``,
``html_url``, and ``state`` fields. Dependencies are normalized records with
exactly ``blocker_url``, ``blocker_state``, and ``blocked_url``. The direction
is blocker to dependent. ``collect_queue`` accepts additional dependency edges
as exact ``(blocker_url, blocked_url)`` pairs. Uncollected blockers are fetched
by exact Issue URL so their state is known before ranking.
"""

from __future__ import annotations

import heapq
import json
import re
import subprocess
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

from .codec import CheckpointError


_MAX_ISSUES = 1_000
_MAX_RELATIONS = 10_000
_ISSUE_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)\Z"
)
_REPO = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\Z")
_NEXT_LINK = re.compile(r'<([^<>]+)>\s*;\s*rel="next"')


class _CollectionError(CheckpointError):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _incomplete(kind: str) -> dict[str, Any]:
    return {
        "status": "INCOMPLETE",
        "queue": [],
        "blocked": [],
        "reasons": [
            {"issue_url": None, "kind": kind, "blockers": []},
        ],
    }


def _parse_issue_url(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise CheckpointError("Issue URL must be a string")
    match = _ISSUE_URL.fullmatch(value)
    if match is None:
        raise CheckpointError("Issue URL must be exact")
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def _validate_issue(value: object) -> tuple[dict[str, Any], str, int, str]:
    if not isinstance(value, dict):
        raise CheckpointError("Issue record must be an object")
    number = value.get("number")
    state = value.get("state")
    url = value.get("html_url")
    if type(number) is not int or number < 1:
        raise CheckpointError("Issue number must be a positive integer")
    repo, url_number = _parse_issue_url(url)
    if url_number != number:
        raise CheckpointError("Issue URL and number do not match")
    if state not in {"open", "closed"}:
        raise CheckpointError("Issue state must be exact")
    return value, repo, number, state


def _number_key(url: str) -> tuple[str, int]:
    repo, number = _parse_issue_url(url)
    return repo, number


def _strong_components(
    nodes: set[str], adjacency: dict[str, set[str]]
) -> list[list[str]]:
    """Return SCCs using iterative Kosaraju traversal."""
    visited: set[str] = set()
    finishing: list[str] = []
    for root in sorted(nodes, key=_number_key):
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(sorted(adjacency[root], key=_number_key)))]
        while stack:
            node, children = stack[-1]
            try:
                child = next(children)
            except StopIteration:
                stack.pop()
                finishing.append(node)
                continue
            if child not in visited:
                visited.add(child)
                stack.append((child, iter(sorted(adjacency[child], key=_number_key))))

    reverse = {node: set() for node in nodes}
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].add(source)

    assigned: set[str] = set()
    components: list[list[str]] = []
    for root in reversed(finishing):
        if root in assigned:
            continue
        assigned.add(root)
        component: list[str] = []
        stack = [(root, False)]
        while stack:
            node, _ = stack.pop()
            component.append(node)
            for parent in sorted(reverse[node], key=_number_key, reverse=True):
                if parent not in assigned:
                    assigned.add(parent)
                    stack.append((parent, False))
        components.append(sorted(component, key=_number_key))
    return components


def _propagate(
    initial: dict[str, set[str]], adjacency: dict[str, set[str]]
) -> dict[str, set[str]]:
    reached = {node: set(blockers) for node, blockers in initial.items()}
    pending = list(sorted(reached, key=_number_key, reverse=True))
    while pending:
        node = pending.pop()
        for child in sorted(adjacency[node], key=_number_key):
            merged = reached.get(child, set()) | reached[node]
            if merged != reached.get(child, set()):
                reached[child] = merged
                pending.append(child)
    return reached


def _bounded_values(values: Iterable[object], limit: int) -> list[object]:
    iterator = iter(values)
    collected: list[object] = []
    for _ in range(limit + 1):
        try:
            collected.append(next(iterator))
        except StopIteration:
            return collected
    raise CheckpointError("rank input exceeds its bound")


def rank_issues(issues: Iterable[object], dependencies: Iterable[object]) -> dict:
    """Rank a complete Issue/dependency snapshot or return ``INCOMPLETE``."""
    try:
        issue_values = _bounded_values(issues, _MAX_ISSUES)
        dependency_values = _bounded_values(dependencies, _MAX_RELATIONS)

        by_url: dict[str, dict[str, Any]] = {}
        numbers: set[int] = set()
        repo: str | None = None
        for raw_issue in issue_values:
            record, issue_repo, number, state = _validate_issue(raw_issue)
            url = record["html_url"]
            if state != "open" or url in by_url or number in numbers:
                raise CheckpointError("queue Issues must be unique and open")
            if repo is None:
                repo = issue_repo
            elif issue_repo != repo:
                raise CheckpointError("queue Issues must belong to one repository")
            by_url[url] = record
            numbers.add(number)

        nodes = set(by_url)
        adjacency = {node: set() for node in nodes}
        external_initial: dict[str, set[str]] = {}
        closed_satisfied: dict[str, set[str]] = {}
        normalized_edges: set[tuple[str, str, str]] = set()
        for raw_dependency in dependency_values:
            if not isinstance(raw_dependency, dict) or set(raw_dependency) != {
                "blocker_url",
                "blocker_state",
                "blocked_url",
            }:
                raise CheckpointError("dependency record has missing or unknown keys")
            blocker_url = raw_dependency["blocker_url"]
            blocked_url = raw_dependency["blocked_url"]
            blocker_state = raw_dependency["blocker_state"]
            blocker_repo, _ = _parse_issue_url(blocker_url)
            _parse_issue_url(blocked_url)
            if blocker_state not in {"open", "closed"} or blocked_url not in nodes:
                raise CheckpointError("dependency endpoint or state is unknown")
            if blocker_url in by_url:
                if blocker_state != "open":
                    raise CheckpointError(
                        "collected Issue state conflicts with dependency"
                    )
            elif blocker_state == "open" and blocker_repo == repo:
                raise CheckpointError(
                    "open repository blocker is missing from collection"
                )
            normalized_edges.add((blocker_url, blocker_state, blocked_url))

        for blocker_url, blocker_state, blocked_url in normalized_edges:
            if blocker_state == "closed":
                closed_satisfied.setdefault(blocked_url, set()).add(blocker_url)
                continue
            if blocker_url in nodes:
                adjacency[blocker_url].add(blocked_url)
            else:
                external_initial.setdefault(blocked_url, set()).add(blocker_url)

        external = _propagate(external_initial, adjacency)
        cycle_initial: dict[str, set[str]] = {}
        for component in _strong_components(nodes, adjacency):
            if len(component) > 1 or component[0] in adjacency[component[0]]:
                members = set(component)
                for member in component:
                    cycle_initial[member] = members
        cycle_reached = _propagate(cycle_initial, adjacency)
        cycle_nodes = set(cycle_initial)
        blocked_nodes = set(external) | set(cycle_reached)

        available = nodes - blocked_nodes
        indegree = {node: 0 for node in available}
        for source in available:
            for target in adjacency[source] & available:
                indegree[target] += 1
        heap = [
            (_number_key(node)[1], node)
            for node, degree in indegree.items()
            if degree == 0
        ]
        heapq.heapify(heap)
        queue: list[str] = []
        while heap:
            _, node = heapq.heappop(heap)
            queue.append(node)
            for child in sorted(adjacency[node] & available, key=_number_key):
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(heap, (_number_key(child)[1], child))
        if len(queue) != len(available):
            raise CheckpointError("cycle classification was incomplete")

        reasons: list[dict[str, Any]] = []
        for node in sorted(blocked_nodes, key=_number_key):
            if node in external:
                kind = "EXTERNAL_OPEN"
                blockers = external[node]
            elif node in cycle_nodes:
                kind = "CYCLE"
                blockers = cycle_initial[node]
            else:
                kind = "DOWNSTREAM_OF_CYCLE"
                blockers = cycle_reached[node]
            reasons.append(
                {
                    "issue_url": node,
                    "kind": kind,
                    "blockers": sorted(blockers, key=_number_key),
                }
            )
        for node, blockers in closed_satisfied.items():
            reasons.append(
                {
                    "issue_url": node,
                    "kind": "CLOSED_SATISFIED",
                    "blockers": sorted(blockers, key=_number_key),
                }
            )
        reasons.sort(
            key=lambda reason: (_number_key(reason["issue_url"]), reason["kind"])
        )
        return {
            "status": "COMPLETE",
            "queue": queue,
            "blocked": sorted(blocked_nodes, key=_number_key),
            "reasons": reasons,
        }
    except (CheckpointError, TypeError, ValueError):
        return _incomplete("INVALID_INPUT")


def _split_gh_response(stdout: object) -> tuple[str, object]:
    if not isinstance(stdout, str):
        raise _CollectionError("COLLECTION_FAILED")
    normalized = stdout.replace("\r\n", "\n")
    try:
        headers, body = normalized.split("\n\n", 1)
        value = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise _CollectionError("COLLECTION_FAILED") from exc
    return headers, value


def _next_url(headers: str) -> str | None:
    links = []
    for line in headers.splitlines():
        if line.lower().startswith("link:"):
            links.extend(_NEXT_LINK.findall(line[5:]))
    if len(links) > 1:
        raise _CollectionError("UNSAFE_PAGINATION")
    return links[0] if links else None


def _get_pages(
    initial_url: str,
    expected_path: str,
    runner: Any,
) -> Iterable[list[dict[str, Any]]]:
    url: str | None = initial_url
    visited: set[str] = set()
    while url is not None:
        if url in visited:
            raise _CollectionError("REPEATED_PAGINATION")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.github.com"
            or parsed.path != expected_path
            or parsed.fragment
        ):
            raise _CollectionError("UNSAFE_PAGINATION")
        visited.add(url)
        try:
            completed = runner(
                ["gh", "api", "--method", "GET", "--include", url],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _CollectionError("COLLECTION_FAILED") from exc
        if getattr(completed, "returncode", None) != 0:
            raise _CollectionError("COLLECTION_FAILED")
        headers, page = _split_gh_response(getattr(completed, "stdout", None))
        if not isinstance(page, list):
            raise _CollectionError("COLLECTION_FAILED")
        yield page
        url = _next_url(headers)


def _get_issue(url: str, runner: Any) -> dict[str, Any]:
    repo, number = _parse_issue_url(url)
    owner, name = repo.split("/", 1)
    api_url = f"https://api.github.com/repos/{owner}/{name}/issues/{number}"
    try:
        completed = runner(
            ["gh", "api", "--method", "GET", "--include", api_url],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _CollectionError("COLLECTION_FAILED") from exc
    if getattr(completed, "returncode", None) != 0:
        raise _CollectionError("COLLECTION_FAILED")
    headers, record = _split_gh_response(getattr(completed, "stdout", None))
    if _next_url(headers) is not None or not isinstance(record, dict):
        raise _CollectionError("COLLECTION_FAILED")
    validated, _, _, _ = _validate_issue(record)
    if validated["html_url"] != url or "pull_request" in validated:
        raise _CollectionError("COLLECTION_FAILED")
    return validated


def collect_queue(
    repo: str,
    explicit_edges: Sequence[tuple[str, str]] = (),
    runner: Any = subprocess.run,
) -> dict:
    """Collect all open Issues and official blocked-by relations via GET only."""
    try:
        match = _REPO.fullmatch(repo) if isinstance(repo, str) else None
        if match is None:
            raise _CollectionError("INVALID_REPOSITORY")
        owner, name = match.groups()
        api_root = f"https://api.github.com/repos/{owner}/{name}"
        issue_path = f"/repos/{owner}/{name}/issues"
        issue_url = f"{api_root}/issues?state=open&per_page=100"
        candidates: list[dict[str, Any]] = []
        for page in _get_pages(issue_url, issue_path, runner):
            for raw_issue in page:
                if not isinstance(raw_issue, dict):
                    raise _CollectionError("COLLECTION_FAILED")
                if "pull_request" in raw_issue:
                    continue
                record, issue_repo, _, state = _validate_issue(raw_issue)
                if issue_repo != repo or state != "open":
                    raise _CollectionError("COLLECTION_FAILED")
                candidates.append(record)
                if len(candidates) > _MAX_ISSUES:
                    raise _CollectionError("ISSUE_CAP_EXCEEDED")

        dependencies: list[dict[str, str]] = []
        relation_count = 0
        for candidate in candidates:
            number = candidate["number"]
            relation_path = f"{issue_path}/{number}/dependencies/blocked_by"
            relation_url = (
                f"{api_root}/issues/{number}/dependencies/blocked_by?per_page=100"
            )
            for page in _get_pages(relation_url, relation_path, runner):
                relation_count += len(page)
                if relation_count > _MAX_RELATIONS:
                    raise _CollectionError("RELATION_CAP_EXCEEDED")
                for raw_blocker in page:
                    if (
                        not isinstance(raw_blocker, dict)
                        or "pull_request" in raw_blocker
                    ):
                        raise _CollectionError("COLLECTION_FAILED")
                    blocker, _, _, blocker_state = _validate_issue(raw_blocker)
                    dependencies.append(
                        {
                            "blocker_url": blocker["html_url"],
                            "blocker_state": blocker_state,
                            "blocked_url": candidate["html_url"],
                        }
                    )

        known = {candidate["html_url"]: candidate for candidate in candidates}
        for edge in explicit_edges:
            if (
                not isinstance(edge, (list, tuple))
                or len(edge) != 2
                or edge[1] not in known
            ):
                raise _CollectionError("INVALID_EXPLICIT_EDGE")
            _parse_issue_url(edge[0])
            if edge[0] not in known:
                known[edge[0]] = _get_issue(edge[0], runner)
            dependencies.append(
                {
                    "blocker_url": edge[0],
                    "blocker_state": known[edge[0]]["state"],
                    "blocked_url": edge[1],
                }
            )
            relation_count += 1
            if relation_count > _MAX_RELATIONS:
                raise _CollectionError("RELATION_CAP_EXCEEDED")

        result = rank_issues(candidates, dependencies)
        if result["status"] != "COMPLETE":
            raise _CollectionError("INVALID_COLLECTED_DATA")
        return result
    except _CollectionError as exc:
        return _incomplete(exc.kind)
    except (CheckpointError, TypeError, ValueError):
        return _incomplete("COLLECTION_FAILED")
