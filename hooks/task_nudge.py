from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable
import json
import os
import re
import selectors
import subprocess
import time


MAX_CAPTURE_BYTES = 12 * 1024
LAUNCHER_TIMEOUT_SECONDS = 15
GIT = "/usr/bin/git"


class RegistrationStatus(str, Enum):
    REGISTERED = "registered"
    UNREGISTERED = "unregistered"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RepositoryIdentity:
    root: Path
    slug: str


@dataclass(frozen=True)
class RegistrationResult:
    status: RegistrationStatus
    repository_slug: str | None
    reason: str | None = None


class NudgeError(Exception):
    """A bounded error that may be surfaced by a runtime adapter."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


HTTPS_REMOTE = re.compile(
    r"(?i)https://github\.com/"
    r"(?P<owner>[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?)/"
    r"(?P<repo>[a-z0-9_.-]{1,100})/?\Z"
)
SSH_REMOTE = re.compile(
    r"(?i)(?:git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?)/"
    r"(?P<repo>[a-z0-9_.-]{1,100})/?\Z"
)


def parse_github_slug(remote: str) -> str | None:
    if not isinstance(remote, str) or any(ord(char) < 32 or ord(char) == 127 for char in remote):
        return None
    match = HTTPS_REMOTE.fullmatch(remote) or SSH_REMOTE.fullmatch(remote)
    if match is None:
        return None
    repo = match.group("repo")
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not repo:
        return None
    return f"{match.group('owner')}/{repo}".lower()


def _git_output(args: list[str], runner: Callable[..., object]) -> bytes:
    try:
        completed = runner(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise NudgeError("REPOSITORY_IDENTITY_UNKNOWN") from error
    stdout = getattr(completed, "stdout", b"")
    stderr = getattr(completed, "stderr", b"")
    returncode = getattr(completed, "returncode", 1)
    if (
        returncode != 0
        or not isinstance(stdout, bytes)
        or not isinstance(stderr, bytes)
        or stderr
        or not stdout
        or len(stdout) > MAX_CAPTURE_BYTES
    ):
        raise NudgeError("REPOSITORY_IDENTITY_UNKNOWN")
    return stdout


def resolve_repository(
    cwd: Path | str, runner: Callable[..., object] = subprocess.run
) -> RepositoryIdentity:
    root_raw = _git_output(
        [GIT, "-C", os.fspath(cwd), "rev-parse", "--show-toplevel"], runner
    )
    try:
        root = Path(root_raw.decode("utf-8").strip())
    except UnicodeDecodeError as error:
        raise NudgeError("REPOSITORY_IDENTITY_UNKNOWN") from error
    if not root.is_absolute() or not str(root):
        raise NudgeError("REPOSITORY_IDENTITY_UNKNOWN")
    remote_raw = _git_output(
        [GIT, "-C", os.fspath(root), "config", "--get", "remote.origin.url"], runner
    )
    try:
        remote = remote_raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise NudgeError("REPOSITORY_IDENTITY_UNKNOWN") from error
    slug = parse_github_slug(remote)
    if slug is None:
        raise NudgeError("REPOSITORY_IDENTITY_UNKNOWN")
    return RepositoryIdentity(root=root, slug=slug)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _unknown(slug: str | None, reason: str) -> RegistrationResult:
    return RegistrationResult(RegistrationStatus.UNKNOWN, slug, reason)


def _bounded_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError("invalid bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid bounded text")
    return value


def _validate_warning(value: object, allowed_codes: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != {"code"}:
        raise ValueError("warning shape mismatch")
    if value["code"] not in allowed_codes:
        raise ValueError("warning value mismatch")


def _normalized_slug(slug: str) -> str:
    parsed = parse_github_slug(f"https://github.com/{slug}")
    if parsed is None:
        raise ValueError("invalid requested slug")
    return parsed


def parse_portfolio_output(raw: bytes, slug: str) -> RegistrationResult:
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_CAPTURE_BYTES:
            raise ValueError("oversized output")
        normalized_slug = _normalized_slug(slug)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(payload, dict):
            raise ValueError("root is not an object")
        allowed_envelope = {"command", "result", "journal_warning", "registration_record_warning"}
        if set(payload) - allowed_envelope or not {"command", "result"}.issubset(payload):
            raise ValueError("envelope shape mismatch")
        if payload["command"] != "portfolio status":
            raise ValueError("command mismatch")
        if "journal_warning" in payload:
            _validate_warning(payload["journal_warning"], {"JOURNAL_WRITE_FAILED"})
        if "registration_record_warning" in payload:
            _validate_warning(
                payload["registration_record_warning"],
                {
                    "REGISTRATION_RECORD_UNREADABLE",
                    "REGISTRATION_RECORD_UNWRITABLE",
                    "REGISTRATION_RECORD_AT_CAPACITY",
                },
            )
        result = payload["result"]
        if not isinstance(result, dict):
            raise ValueError("result is not an object")
        required = {"page_id", "items", "repositories", "truncated", "total_items"}
        if set(result) - (required | {"next_page_id"}) or not required.issubset(result):
            raise ValueError("result shape mismatch")
        _bounded_text(result["page_id"], 32)
        if re.fullmatch(r"page-[1-9][0-9]*", result["page_id"]) is None:
            raise ValueError("invalid page id")
        truncated = result["truncated"]
        total_items = result["total_items"]
        if not isinstance(truncated, bool):
            raise ValueError("truncated is not boolean")
        if not isinstance(total_items, int) or isinstance(total_items, bool) or total_items < 0:
            raise ValueError("total_items is invalid")
        if ("next_page_id" in result) != truncated:
            raise ValueError("pagination evidence is inconsistent")
        if truncated:
            next_page_id = _bounded_text(result["next_page_id"], 32)
            if re.fullmatch(r"page-[1-9][0-9]*", next_page_id) is None:
                raise ValueError("invalid next page id")
        repositories = result["repositories"]
        if not isinstance(repositories, list):
            raise ValueError("repositories is not a list")
        repository_ids: set[str] = set()
        normalized_repositories: set[str] = set()
        for repository in repositories:
            if not isinstance(repository, dict) or set(repository) != {"repo_id", "slug", "allow_public"}:
                raise ValueError("repository shape mismatch")
            repo_id = _bounded_text(repository["repo_id"], 256)
            if repo_id in repository_ids or not isinstance(repository["allow_public"], bool):
                raise ValueError("repository value mismatch")
            repository_slug = _normalized_slug(repository["slug"])
            repository_ids.add(repo_id)
            normalized_repositories.add(repository_slug)
        items = result["items"]
        if not isinstance(items, list) or len(items) != total_items:
            raise ValueError("items are inconsistent")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"project_id", "title", "repo_ids"}:
                raise ValueError("item shape mismatch")
            _bounded_text(item["project_id"], 256)
            _bounded_text(item["title"], 256)
            repo_ids = item["repo_ids"]
            if not isinstance(repo_ids, list) or not repo_ids:
                raise ValueError("item repo ids are invalid")
            for repo_id in repo_ids:
                if not isinstance(repo_id, str) or repo_id not in repository_ids:
                    raise ValueError("item repository reference is invalid")
        if normalized_slug in normalized_repositories:
            return RegistrationResult(RegistrationStatus.REGISTERED, normalized_slug)
        if truncated:
            return _unknown(normalized_slug, "PORTFOLIO_RESULT_INCOMPLETE")
        return RegistrationResult(RegistrationStatus.UNREGISTERED, normalized_slug)
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        return _unknown(slug if isinstance(slug, str) else None, "PORTFOLIO_UNAVAILABLE")


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _bounded_launcher_run(argv: list[str]) -> tuple[int, bytes, bytes] | None:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
    except OSError:
        return None
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    try:
        for stream in streams:
            if stream is not None:
                selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + LAUNCHER_TIMEOUT_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_and_reap(process)
                return None
            for key, _ in selector.select(remaining):
                stream = key.fileobj
                captured = streams[stream]
                if len(captured) == MAX_CAPTURE_BYTES:
                    if os.read(key.fd, 1):
                        _terminate_and_reap(process)
                        return None
                    selector.unregister(stream)
                    continue
                chunk = os.read(key.fd, min(4096, MAX_CAPTURE_BYTES - len(captured)))
                if not chunk:
                    selector.unregister(stream)
                    continue
                captured.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_and_reap(process)
            return None
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _terminate_and_reap(process)
            return None
        stdout = bytes(streams[process.stdout])
        stderr = bytes(streams[process.stderr])
        return returncode, stdout, stderr
    except (OSError, subprocess.SubprocessError):
        _terminate_and_reap(process)
        return None
    finally:
        selector.close()


def _injected_launcher_run(
    runner: Callable[..., object], argv: list[str]
) -> tuple[int, bytes, bytes] | None:
    try:
        completed = runner(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=False,
            timeout=LAUNCHER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stdout = getattr(completed, "stdout", None)
    stderr = getattr(completed, "stderr", None)
    returncode = getattr(completed, "returncode", None)
    if not isinstance(returncode, int) or not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        return None
    if len(stdout) > MAX_CAPTURE_BYTES or len(stderr) > MAX_CAPTURE_BYTES:
        return None
    return returncode, stdout, stderr


def query_registration(
    identity: RepositoryIdentity,
    home: Path | str,
    runner: Callable[..., object] = subprocess.run,
) -> RegistrationResult:
    argv = [os.fspath(Path(home) / ".local/bin/jhw-control-host"), "portfolio", "status"]
    completed = (
        _bounded_launcher_run(argv)
        if runner is subprocess.run
        else _injected_launcher_run(runner, argv)
    )
    if completed is None:
        return _unknown(identity.slug, "PORTFOLIO_UNAVAILABLE")
    returncode, stdout, stderr = completed
    if returncode != 0 or stderr or not stdout:
        return _unknown(identity.slug, "PORTFOLIO_UNAVAILABLE")
    return parse_portfolio_output(stdout, identity.slug)
