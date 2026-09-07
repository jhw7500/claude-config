"""Deterministic, network-free Git snapshot capture for tribunal reviews."""

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import unicodedata

from .model import (
    ChangedPath,
    MAX_COMMAND_TEXT_BYTES,
    MAX_INITIAL_PATHS,
    SCHEMA_VERSION,
    Snapshot,
    TribunalError,
)


GIT = "/usr/bin/git"
MAX_GIT_STDOUT_BYTES = 32 * 1024 * 1024
MAX_GIT_STDERR_BYTES = 64 * 1024
GIT_TIMEOUT_SECONDS = 30
GIT_READ_CHUNK_BYTES = 64 * 1024
GIT_TERMINATION_GRACE_SECONDS = 0.2

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
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false",
        }
    )
    return environment


def _gh_config_environment() -> dict[str, str]:
    environment = _git_environment()
    environment.pop("GIT_CONFIG_NOSYSTEM", None)
    environment.pop("GIT_CONFIG_GLOBAL", None)
    return environment


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        time.sleep(GIT_TERMINATION_GRACE_SECONDS)
    except Exception:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=GIT_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait()
        except Exception:
            pass
    except Exception:
        pass
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass


def _run_git_with_environment(
    cwd: Path, arguments: Sequence[str], environment: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    argv = [GIT, "-C", str(cwd), *arguments]
    try:
        selector = selectors.DefaultSelector()
    except Exception:
        raise GitStateError("GIT_COMMAND_FAILED") from None

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("missing Git capture pipe")
        stdout = bytearray()
        stderr = bytearray()
        selector.register(
            process.stdout,
            selectors.EVENT_READ,
            (stdout, MAX_GIT_STDOUT_BYTES),
        )
        selector.register(
            process.stderr,
            selectors.EVENT_READ,
            (stderr, MAX_GIT_STDERR_BYTES),
        )
        deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitStateError("GIT_COMMAND_FAILED")
            events = selector.select(remaining)
            if not events:
                raise GitStateError("GIT_COMMAND_FAILED")
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), GIT_READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffer, limit = key.data
                if len(buffer) + len(chunk) > limit:
                    raise GitStateError("GIT_OUTPUT_LIMIT")
                buffer.extend(chunk)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitStateError("GIT_COMMAND_FAILED")
        returncode = process.wait(timeout=remaining)
        result = subprocess.CompletedProcess(
            argv, returncode, bytes(stdout), bytes(stderr)
        )
    except BaseException as error:
        if process is not None and process.returncode is None:
            _terminate_process_group(process)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, GitStateError):
            raise
        raise GitStateError("GIT_COMMAND_FAILED") from None
    finally:
        try:
            selector.close()
        except Exception:
            pass

    return result


def _run_git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return _run_git_with_environment(cwd, arguments, _git_environment())


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


def _valid_schema_text(value: object, maximum: int) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return (
        len(encoded) <= maximum
        and unicodedata.normalize("NFC", value) == value
        and not any(
            unicodedata.category(character) in {"Cc", "Cs"}
            for character in value
        )
    )


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


def _validated_cwd(cwd: object) -> Path:
    try:
        raw = os.fspath(cwd)
    except (OSError, TypeError, ValueError):
        raise GitStateError("NOT_GIT_REPOSITORY") from None
    if not isinstance(raw, str):
        raise GitStateError("PATH_INVALID")
    try:
        raw.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise GitStateError("PATH_INVALID") from None
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in raw):
        raise GitStateError("PATH_INVALID")
    try:
        return Path(raw)
    except (OSError, TypeError, ValueError):
        raise GitStateError("NOT_GIT_REPOSITORY") from None


def _validate_base(root: Path, base: str) -> None:
    if (
        not _valid_schema_text(base, 256)
        or base.startswith("-")
    ):
        raise GitStateError("BASE_INVALID")
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
    if (
        not _valid_schema_text(value, MAX_COMMAND_TEXT_BYTES)
        or value.startswith(("/", "\\"))
        or "\\" in value
    ):
        return False
    return all(component not in {"", ".", ".."} for component in value.split("/"))


def _validate_gh_default_repository(root: Path) -> None:
    result = _run_git_with_environment(
        root,
        ("config", "--null", "--get-regexp", r"^remote\..*\.gh-resolved$"),
        _gh_config_environment(),
    )
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return
    if result.returncode != 0 or result.stderr:
        raise GitStateError("REPOSITORY_UNSUPPORTED")
    if result.stdout != b"remote.origin.gh-resolved\nbase\x00":
        raise GitStateError("REPOSITORY_UNSUPPORTED")


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


def _revalidate_snapshot_state(
    root: Path,
    *,
    symbolic_head: str,
    head_sha: str,
    remote_ref: str,
    base_sha: str,
    repository: str,
) -> None:
    final_symbolic_result = _run_git(root, "symbolic-ref", "-q", "HEAD")
    if final_symbolic_result.returncode != 0:
        raise GitStateError("SNAPSHOT_CHANGED")
    try:
        final_symbolic_head = _one_line_utf8(
            final_symbolic_result.stdout, "SNAPSHOT_CHANGED"
        )
        final_head_sha = _sha(
            _command_output(
                root,
                ("rev-parse", "--verify", "HEAD^{commit}"),
                failure="SNAPSHOT_CHANGED",
            ),
            "SNAPSHOT_CHANGED",
        )
        final_base_sha = _sha(
            _command_output(
                root,
                ("rev-parse", "--verify", remote_ref),
                failure="SNAPSHOT_CHANGED",
            ),
            "SNAPSHOT_CHANGED",
        )
        final_origin = _command_output(
            root,
            ("remote", "get-url", "origin"),
            failure="SNAPSHOT_CHANGED",
        )
        final_repository = _repository_from_origin(final_origin)
        _validate_gh_default_repository(root)
    except GitStateError:
        raise GitStateError("SNAPSHOT_CHANGED") from None
    if (
        final_symbolic_head != symbolic_head
        or final_head_sha != head_sha
        or final_base_sha != base_sha
        or final_repository != repository
    ):
        raise GitStateError("SNAPSHOT_CHANGED")

    final_status = _command_output(
        root, ("status", "--porcelain=v2", "-z", "--untracked-files=all")
    )
    if _worktree_is_dirty(final_status):
        raise GitStateError("WORKTREE_DIRTY")


def capture_snapshot(
    cwd: Path,
    base: str,
    *,
    now: Callable[[], str] | None = None,
) -> Snapshot:
    requested_cwd = _validated_cwd(cwd)
    root = _physical_root(requested_cwd)

    symbolic_head = _run_git(root, "symbolic-ref", "-q", "HEAD")
    if symbolic_head.returncode != 0:
        raise GitStateError("DETACHED_HEAD")
    symbolic_head_name = _one_line_utf8(symbolic_head.stdout, "GIT_STATE_INVALID")
    if not _valid_schema_text(symbolic_head_name, 1024):
        raise GitStateError("GIT_STATE_INVALID")

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
    origin = _command_output(
        root,
        ("remote", "get-url", "origin"),
        failure="REPOSITORY_UNSUPPORTED",
    )
    repository = _repository_from_origin(origin)
    _validate_gh_default_repository(root)

    status = _command_output(
        root, ("status", "--porcelain=v2", "-z", "--untracked-files=all")
    )
    if _worktree_is_dirty(status):
        raise GitStateError("WORKTREE_DIRTY")

    revision_range = f"{merge_base_sha}..{head_sha}"
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

    initial_paths = {
        path
        for item in paths
        for path in (item.path, item.old_path)
        if path is not None
    }
    if len(initial_paths) > MAX_INITIAL_PATHS:
        raise GitStateError("GIT_STATE_INVALID")
    ordered_initial_paths = tuple(
        sorted(initial_paths, key=lambda value: value.encode("utf-8"))
    )
    created_at = _created_at(now)
    _revalidate_snapshot_state(
        root,
        symbolic_head=symbolic_head_name,
        head_sha=head_sha,
        remote_ref=remote_ref,
        base_sha=base_sha,
        repository=repository,
    )
    return Snapshot(
        schema=SCHEMA_VERSION,
        repository=repository,
        base_ref=base,
        base_sha=base_sha,
        head_ref=symbolic_head_name,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        diff_sha256=diff_sha256,
        paths=paths,
        initial_paths=ordered_initial_paths,
        created_at=created_at,
    )


def snapshot_matches(left: Snapshot, right: Snapshot) -> bool:
    if not isinstance(left, Snapshot) or not isinstance(right, Snapshot):
        return False
    return (
        left.schema,
        left.repository,
        left.base_ref,
        left.base_sha,
        left.head_ref,
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
        right.head_ref,
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
