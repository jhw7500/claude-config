"""Deterministic, network-free Git snapshot capture for tribunal reviews."""

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import subprocess
import unicodedata

from .model import ChangedPath, SCHEMA_VERSION, Snapshot, TribunalError


GIT = "/usr/bin/git"
MAX_GIT_STDOUT_BYTES = 32 * 1024 * 1024
MAX_GIT_STDERR_BYTES = 64 * 1024
GIT_TIMEOUT_SECONDS = 30

_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_DIFF_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_STATUS = re.compile(r"(?:[ACDMRTUXB]|[RC][0-9]{1,3})\Z")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_REMOTE_PATTERNS = (
    re.compile(r"https://github\.com/([^/]+)/([^/]+)\Z"),
    re.compile(r"git@github\.com:([^/]+)/([^/]+)\Z"),
    re.compile(r"ssh://git@github\.com/([^/]+)/([^/]+)\Z"),
)


class GitStateError(TribunalError):
    pass


def _git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _run_git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    argv = [GIT, "-C", str(cwd), *arguments]
    try:
        result = subprocess.run(
            argv,
            shell=False,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GitStateError("GIT_COMMAND_FAILED") from None
    if (
        len(result.stdout) > MAX_GIT_STDOUT_BYTES
        or len(result.stderr) > MAX_GIT_STDERR_BYTES
    ):
        raise GitStateError("GIT_OUTPUT_LIMIT")
    return result


def _command_output(
    cwd: Path,
    arguments: Sequence[str],
    *,
    failure: str = "GIT_COMMAND_FAILED",
) -> bytes:
    result = _run_git(cwd, *arguments)
    if result.returncode != 0:
        raise GitStateError(failure)
    return result.stdout


def _one_line_utf8(raw: bytes, code: str) -> str:
    if not raw.endswith(b"\n") or b"\n" in raw[:-1] or b"\r" in raw:
        raise GitStateError(code)
    try:
        value = raw[:-1].decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise GitStateError(code) from None
    if not value or "\x00" in value:
        raise GitStateError(code)
    return value


def _sha(raw: bytes, code: str = "GIT_STATE_INVALID") -> str:
    try:
        value = raw.decode("ascii", "strict").strip()
    except UnicodeDecodeError:
        raise GitStateError(code) from None
    if _SHA1.fullmatch(value) is None:
        raise GitStateError(code)
    return value


def _physical_root(cwd: Path) -> Path:
    output = _command_output(
        cwd,
        ("rev-parse", "--show-toplevel"),
        failure="NOT_GIT_REPOSITORY",
    )
    root_text = _one_line_utf8(output, "PATH_INVALID")
    try:
        root = Path(root_text).resolve(strict=True)
        physical_cwd = cwd.resolve(strict=True)
    except (OSError, RuntimeError):
        raise GitStateError("NOT_GIT_REPOSITORY") from None
    if not root.is_dir() or not physical_cwd.is_relative_to(root):
        raise GitStateError("NOT_GIT_REPOSITORY")
    return root


def _validate_base(root: Path, base: str) -> None:
    if (
        not isinstance(base, str)
        or not base
        or base.startswith("-")
        or any(unicodedata.category(character) == "Cc" for character in base)
    ):
        raise GitStateError("BASE_INVALID")
    try:
        base.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise GitStateError("BASE_INVALID") from None
    result = _run_git(root, "check-ref-format", "--branch", base)
    if result.returncode != 0:
        raise GitStateError("BASE_INVALID")


def _repository_from_origin(raw: bytes) -> str:
    try:
        url = _one_line_utf8(raw, "REPOSITORY_UNSUPPORTED")
        url.encode("ascii", "strict")
    except (GitStateError, UnicodeEncodeError):
        raise GitStateError("REPOSITORY_UNSUPPORTED") from None

    match = None
    for pattern in _REMOTE_PATTERNS:
        match = pattern.fullmatch(url)
        if match is not None:
            break
    if match is None:
        raise GitStateError("REPOSITORY_UNSUPPORTED")
    owner, repository = match.groups()
    if repository.endswith(".git"):
        repository = repository[:-4]
    if (
        _OWNER.fullmatch(owner) is None
        or _REPOSITORY.fullmatch(repository) is None
        or repository in {"", ".", ".."}
    ):
        raise GitStateError("REPOSITORY_UNSUPPORTED")
    return f"{owner}/{repository}"


def _valid_path_text(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    if any(
        unicodedata.category(character) == "Cc" and character not in "\n\t"
        for character in value
    ):
        return False
    return all(component not in {"", ".", ".."} for component in value.split("/"))


def _path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise GitStateError("PATH_INVALID") from None
    if not _valid_path_text(value):
        raise GitStateError("PATH_INVALID")
    return value


def _worktree_is_dirty(raw: bytes) -> bool:
    if not raw:
        return False
    if not raw.endswith(b"\x00"):
        raise GitStateError("GIT_STATE_INVALID")
    records = raw[:-1].split(b"\x00")
    index = 0
    while index < len(records):
        record = records[index]
        if record.startswith(b"2 "):
            index += 1
            if index >= len(records) or not records[index]:
                raise GitStateError("GIT_STATE_INVALID")
        elif not record.startswith((b"1 ", b"u ", b"? ", b"! ")):
            raise GitStateError("GIT_STATE_INVALID")
        index += 1
    return True


def _changed_paths(raw: bytes) -> tuple[ChangedPath, ...]:
    if not raw:
        return ()
    if not raw.endswith(b"\x00"):
        raise GitStateError("GIT_STATE_INVALID")
    fields = raw[:-1].split(b"\x00")
    changed: list[ChangedPath] = []
    index = 0
    while index < len(fields):
        try:
            status = fields[index].decode("ascii", "strict")
        except UnicodeDecodeError:
            raise GitStateError("GIT_STATE_INVALID") from None
        index += 1
        if _STATUS.fullmatch(status) is None:
            raise GitStateError("GIT_STATE_INVALID")
        if status.startswith(("R", "C")):
            if index + 1 >= len(fields):
                raise GitStateError("GIT_STATE_INVALID")
            old_path = _path(fields[index])
            new_path = _path(fields[index + 1])
            index += 2
            changed.append(ChangedPath(status, new_path, old_path))
        else:
            if index >= len(fields):
                raise GitStateError("GIT_STATE_INVALID")
            changed.append(ChangedPath(status, _path(fields[index])))
            index += 1
    return tuple(changed)


def _created_at(now: Callable[[], str] | None) -> str:
    if now is None:
        value = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        try:
            value = now()
        except Exception:
            raise GitStateError("TIMESTAMP_INVALID") from None
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise GitStateError("TIMESTAMP_INVALID")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise GitStateError("TIMESTAMP_INVALID") from None
    return value


def capture_snapshot(
    cwd: Path,
    base: str,
    *,
    now: Callable[[], str] | None = None,
) -> Snapshot:
    try:
        requested_cwd = Path(cwd)
    except TypeError:
        raise GitStateError("NOT_GIT_REPOSITORY") from None
    root = _physical_root(requested_cwd)

    symbolic_head = _run_git(root, "symbolic-ref", "-q", "HEAD")
    if symbolic_head.returncode != 0:
        raise GitStateError("DETACHED_HEAD")
    _one_line_utf8(symbolic_head.stdout, "GIT_STATE_INVALID")

    head_sha = _sha(_command_output(root, ("rev-parse", "--verify", "HEAD^{commit}")))
    _validate_base(root, base)
    remote_ref = f"refs/remotes/origin/{base}^{{commit}}"
    base_sha = _sha(
        _command_output(
            root,
            ("rev-parse", "--verify", remote_ref),
            failure="BASE_INVALID",
        ),
        "BASE_INVALID",
    )
    merge_base_sha = _sha(
        _command_output(
            root,
            ("merge-base", base_sha, head_sha),
            failure="BASE_INVALID",
        ),
        "BASE_INVALID",
    )

    status = _command_output(
        root, ("status", "--porcelain=v2", "-z", "--untracked-files=all")
    )
    if _worktree_is_dirty(status):
        raise GitStateError("WORKTREE_DIRTY")

    revision_range = f"{merge_base_sha}..HEAD"
    name_status = _command_output(
        root,
        ("diff", "--name-status", "-z", "--find-renames", revision_range),
    )
    paths = _changed_paths(name_status)
    if not paths:
        raise GitStateError("EMPTY_DIFF")

    diff = _command_output(
        root,
        (
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--full-index",
            revision_range,
        ),
    )
    diff_sha256 = hashlib.sha256(diff).hexdigest()
    if _DIFF_SHA256.fullmatch(diff_sha256) is None:
        raise GitStateError("GIT_STATE_INVALID")

    origin = _command_output(
        root,
        ("remote", "get-url", "origin"),
        failure="REPOSITORY_UNSUPPORTED",
    )
    repository = _repository_from_origin(origin)

    initial_paths = {
        path
        for item in paths
        for path in (item.path, item.old_path)
        if path is not None
    }
    ordered_initial_paths = tuple(
        sorted(initial_paths, key=lambda value: value.encode("utf-8"))
    )
    return Snapshot(
        schema=SCHEMA_VERSION,
        repository=repository,
        base_ref=base,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        diff_sha256=diff_sha256,
        paths=paths,
        initial_paths=ordered_initial_paths,
        created_at=_created_at(now),
    )


def snapshot_matches(left: Snapshot, right: Snapshot) -> bool:
    if not isinstance(left, Snapshot) or not isinstance(right, Snapshot):
        return False
    return (
        left.schema,
        left.repository,
        left.base_ref,
        left.base_sha,
        left.head_sha,
        left.merge_base_sha,
        left.diff_sha256,
        tuple(left.paths),
        tuple(left.initial_paths),
    ) == (
        right.schema,
        right.repository,
        right.base_ref,
        right.base_sha,
        right.head_sha,
        right.merge_base_sha,
        right.diff_sha256,
        tuple(right.paths),
        tuple(right.initial_paths),
    )


def assert_auto_fix_scope(
    initial_paths: Iterable[str], current_paths: Iterable[str]
) -> None:
    try:
        allowed = tuple(initial_paths)
        current = tuple(current_paths)
    except (TypeError, ValueError):
        raise GitStateError("AUTO_FIX_SCOPE_EXPANDED") from None
    if (
        any(not _valid_path_text(path) for path in allowed)
        or any(not _valid_path_text(path) for path in current)
        or not set(current).issubset(set(allowed))
    ):
        raise GitStateError("AUTO_FIX_SCOPE_EXPANDED")
