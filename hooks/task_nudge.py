from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import time


MAX_CAPTURE_BYTES = 12 * 1024
LAUNCHER_TIMEOUT_SECONDS = 15
GIT = "/usr/bin/git"


class RegistrationStatus(str, Enum):
    REGISTERED = "registered"
    UNREGISTERED = "unregistered"
    UNKNOWN = "unknown"


class WorkKind(str, Enum):
    EXCLUDED = "excluded"
    BACKLOG = "backlog"
    IMMEDIATE = "immediate"


class SuggestedAction(str, Enum):
    NO_TASK = "no_task"
    GITHUB_ISSUE_ONLY = "github_issue_only"
    FORMAL_ISSUE_TASK = "formal_issue_task"
    TEMPORARY_TASK = "temporary_task"
    REGISTER_REPOSITORY = "register_repository"
    STOP_FOR_CLASSIFICATION = "stop_for_classification"


class Runtime(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"


class MarkerClaim(str, Enum):
    CLAIMED = "claimed"
    ALREADY_DONE = "already_done"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RepositoryIdentity:
    root: Path
    slug: str


@dataclass(frozen=True)
class RegistrationResult:
    status: RegistrationStatus
    repository_slug: str | None
    reason: str | None = None


@dataclass(frozen=True)
class PolicyContext:
    status: RegistrationStatus
    work: WorkKind
    recurring: bool = False
    existing_issue: bool = False
    bounded: bool = False

    @classmethod
    def from_strings(
        cls,
        *,
        status: str,
        work: str,
        recurring: bool = False,
        existing_issue: bool = False,
        bounded: bool = False,
    ) -> "PolicyContext":
        return cls(
            RegistrationStatus(status),
            WorkKind(work),
            recurring,
            existing_issue,
            bounded,
        )


@dataclass(frozen=True)
class HookEvent:
    runtime: Runtime
    session_id: str
    cwd: Path
    tool_name: str
    target_paths: tuple[Path, ...]


class NudgeError(Exception):
    """A bounded error that may be surfaced by a runtime adapter."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def suggest_action(context: PolicyContext) -> SuggestedAction:
    """Apply the policy table without attempting to infer conversation context."""
    if context.work is WorkKind.EXCLUDED:
        return SuggestedAction.NO_TASK
    if context.work is WorkKind.BACKLOG:
        return SuggestedAction.GITHUB_ISSUE_ONLY
    if context.status is RegistrationStatus.UNKNOWN:
        return SuggestedAction.STOP_FOR_CLASSIFICATION
    if context.status is RegistrationStatus.REGISTERED:
        if context.existing_issue or context.recurring:
            return SuggestedAction.FORMAL_ISSUE_TASK
        if context.bounded:
            return SuggestedAction.TEMPORARY_TASK
        return SuggestedAction.NO_TASK
    if context.recurring:
        return SuggestedAction.REGISTER_REPOSITORY
    return SuggestedAction.NO_TASK


def has_recurring_evidence(
    *,
    explicit_long_running: bool = False,
    existing_issue_plan_or_handoff: bool = False,
    architectural_multistage: bool = False,
    file_count: int | None = None,
    repository_present: bool = False,
) -> bool:
    """Accept only the three approved positive recurring-work evidence classes."""
    del file_count, repository_present
    return explicit_long_running or existing_issue_plan_or_handoff or architectural_multistage


_BOUNDED_REASONS = frozenset(
    {
        "HOOK_INPUT_INVALID",
        "REPOSITORY_IDENTITY_UNKNOWN",
        "PORTFOLIO_UNAVAILABLE",
        "PORTFOLIO_RESULT_INCOMPLETE",
        "NUDGE_STATE_UNAVAILABLE",
    }
)


def _safe_message_identity(result: RegistrationResult) -> str:
    if result.status in {RegistrationStatus.REGISTERED, RegistrationStatus.UNREGISTERED}:
        slug = result.repository_slug
        if isinstance(slug, str) and parse_github_slug(f"https://github.com/{slug}") == slug.lower():
            return f"저장소: {slug.lower()} / 상태: {result.status.value}"
        return f"상태: {result.status.value}"
    reason = result.reason if result.reason in _BOUNDED_REASONS else "PORTFOLIO_UNAVAILABLE"
    return f"상태: unknown / 사유: {reason}"


def render_nudge_message(result: RegistrationResult) -> str:
    """Return the shared, scrubbed runtime guidance for a registration result."""
    identity = _safe_message_identity(result)
    return (
        "[TASK-NUDGE] " + identity + "\n"
        "정책 우선순서: (1) 이미 결정됨·제외 작업(조회/Q&A·단순 문서/설정·subagent)은 Task 없이 진행; "
        "(2) backlog(unknown 포함)는 등록 상태와 무관하게 GitHub Issue만 제안하고 Task/Claim을 선점하거나 시작하지 않음; "
        "(3) 즉시 작업의 unknown이면 등록 여부를 가정하지 말고 현재 변경을 중단하고 복구가 필요하다고 알린다; "
        "(4) 등록 저장소의 즉시 작업은 기존 Issue 또는 반복 증거면 Formal Issue Task, "
        "현재 세션에서 끝낼 제한 작업이면 Temporary Task, 조정 비용보다 작으면 Task 없음; "
        "(5) 미등록 저장소의 즉시 작업은 반복 증거가 있을 때만 Project/Repository 등록만 먼저 제안, 아니면 Task 없음.\n"
        "반복·다중 세션 증거는 다음 셋뿐이다: 사용자의 장기·반복·여러 세션 명시, "
        "기존 GitHub Issue·승인된 계획·Handoff, 여러 구현 단계와 검증이 필요한 아키텍처 작업. "
        "파일 수나 저장소 안에 있다는 사실은 증거가 아니다.\n"
        "GitHub Issue 생성, Project/Repository 등록, Formal 또는 Temporary Task 시작은 각각 별도의 명시적 사용자 승인 후에만 한다. "
        "앞 단계 승인은 다음 단계를 승인하지 않는다. 이미 결정했거나 subagent이면 이 안내를 적용하지 않는다."
    )


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
PATCH_PATH = re.compile(
    r"^\*\*\* (?:(?:Add|Update|Delete) File:|Move to:) "
    r"(?P<path>[^\x00-\x1f\x7f]+)$"
)


def _has_control_characters(value: object, *, allow_newlines: bool = False) -> bool:
    if not isinstance(value, str):
        return True
    return any(
        ord(character) == 127
        or (ord(character) < 32 and (not allow_newlines or character not in "\n\r"))
        for character in value
    )


def _lexical_absolute(path: str, cwd: Path | None = None) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        if cwd is None:
            raise NudgeError("HOOK_INPUT_INVALID")
        candidate = cwd / candidate
    return Path(os.path.normpath(os.fspath(candidate)))


def _absolute_environment_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value or _has_control_characters(value):
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        return None
    return Path(os.path.normpath(os.fspath(candidate)))


def _event_fields(payload: object, runtime: Runtime, permitted_tools: set[str]) -> tuple[str, Path, str, dict[str, object]]:
    if not isinstance(payload, dict):
        raise NudgeError("HOOK_INPUT_INVALID")
    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if (
        not isinstance(session_id, str)
        or not session_id
        or _has_control_characters(session_id)
        or not isinstance(cwd, str)
        or not cwd
        or _has_control_characters(cwd)
        or not isinstance(tool_name, str)
        or _has_control_characters(tool_name)
        or tool_name not in permitted_tools
        or not isinstance(tool_input, dict)
    ):
        raise NudgeError("HOOK_INPUT_INVALID")
    normalized_cwd = _lexical_absolute(cwd)
    if not normalized_cwd.is_absolute():
        raise NudgeError("HOOK_INPUT_INVALID")
    return session_id, normalized_cwd, tool_name, tool_input


def _input_path(tool_input: dict[str, object], field: str, cwd: Path) -> Path:
    value = tool_input.get(field)
    if not isinstance(value, str) or not value or _has_control_characters(value):
        raise NudgeError("HOOK_INPUT_INVALID")
    return _lexical_absolute(value, cwd)


def extract_apply_patch_paths(command: str, cwd: Path) -> tuple[Path, ...]:
    if not isinstance(command, str) or _has_control_characters(command, allow_newlines=True):
        raise NudgeError("HOOK_INPUT_INVALID")
    paths: list[Path] = []
    for line in command.splitlines():
        match = PATCH_PATH.fullmatch(line)
        if match is not None:
            paths.append(_lexical_absolute(match.group("path"), cwd))
    return tuple(paths)


def parse_claude_event(payload: object) -> HookEvent:
    session_id, cwd, tool_name, tool_input = _event_fields(
        payload, Runtime.CLAUDE, {"Edit", "Write", "NotebookEdit"}
    )
    path_field = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
    return HookEvent(Runtime.CLAUDE, session_id, cwd, tool_name, (_input_path(tool_input, path_field, cwd),))


def parse_codex_event(payload: object) -> HookEvent:
    session_id, cwd, tool_name, tool_input = _event_fields(
        payload, Runtime.CODEX, {"apply_patch", "Edit", "Write"}
    )
    if tool_name == "apply_patch":
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise NudgeError("HOOK_INPUT_INVALID")
        paths = extract_apply_patch_paths(command, cwd)
    else:
        paths = (_input_path(tool_input, "file_path", cwd),)
    return HookEvent(Runtime.CODEX, session_id, cwd, tool_name, paths)


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


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.fspath(path), os.fspath(root))) == os.fspath(root)
    except ValueError:
        return False


def should_skip_event(event: HookEvent, home: Path | str, env: Mapping[str, str]) -> bool:
    if not event.target_paths:
        return False
    normalized_home = _lexical_absolute(os.fspath(home))
    scratch_root = _absolute_environment_path(env.get("TMPDIR", "/tmp"))
    if scratch_root is None:
        return False
    claude_root = normalized_home / ".claude"
    codex_root = normalized_home / ".codex"
    for target in event.target_paths:
        target_path = _lexical_absolute(os.fspath(target), event.cwd)
        if _is_within(target_path, scratch_root):
            continue
        if _is_within(target_path, claude_root) or _is_within(target_path, codex_root):
            continue
        if ".omc" in target_path.parts:
            continue
        if target_path.parent.name == "memory" and target_path.suffix == ".md":
            continue
        if target_path.name.startswith("HANDOFF") and target_path.suffix == ".md":
            continue
        return False
    return True


def marker_name(runtime: Runtime, session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return f"{runtime.value}-{digest}"


def _safe_directory(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and (info.st_mode & 0o077) == 0
    )


def _safe_shared_tmp_directory(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid in {0, os.getuid()}
        and bool(info.st_mode & stat.S_ISVTX)
    )


def _private_child(parent: Path, name: str, *, allow_shared_parent: bool = False) -> Path | None:
    if not _safe_directory(parent) and not (allow_shared_parent and _safe_shared_tmp_directory(parent)):
        return None
    child = parent / name
    try:
        os.mkdir(child, 0o700)
        os.chmod(child, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return None
    return child if _safe_directory(child) else None


def _state_directory(env: Mapping[str, str]) -> Path | None:
    xdg_root = _absolute_environment_path(env.get("XDG_RUNTIME_DIR"))
    if xdg_root is not None:
        state_dir = _private_child(xdg_root, "task-nudge")
        if state_dir is not None:
            return state_dir
    tmpdir = _absolute_environment_path(env["TMPDIR"]) if "TMPDIR" in env else Path("/tmp")
    if tmpdir is None:
        return None
    return _private_child(tmpdir, f"task-nudge-{os.getuid()}", allow_shared_parent=True)


def _marker_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _existing_marker_claim(directory_descriptor: int, name: str) -> MarkerClaim | None:
    try:
        existing = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        return MarkerClaim.UNAVAILABLE
    if (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.getuid()
        or (existing.st_mode & 0o077) != 0
    ):
        return MarkerClaim.UNAVAILABLE
    return MarkerClaim.ALREADY_DONE


def _close_after_error(descriptor: int) -> None:
    try:
        os.fstat(descriptor)
    except OSError:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _remove_created_marker(
    directory_descriptor: int,
    name: str,
    created_identity: tuple[int, int, int],
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError:
        return
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_uid != os.getuid()
        or _marker_identity(current) != created_identity
    ):
        return
    try:
        os.unlink(name, dir_fd=directory_descriptor)
    except OSError:
        return


def claim_session_marker(
    runtime: Runtime, session_id: str, env: Mapping[str, str] | None = None
) -> MarkerClaim:
    if not isinstance(runtime, Runtime) or not isinstance(session_id, str) or not session_id or _has_control_characters(session_id):
        return MarkerClaim.UNAVAILABLE
    state_dir = _state_directory(os.environ if env is None else env)
    if state_dir is None:
        return MarkerClaim.UNAVAILABLE
    name = marker_name(runtime, session_id)
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_descriptor = os.open(state_dir, directory_flags)
    except OSError:
        return MarkerClaim.UNAVAILABLE
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.getuid()
            or (directory_metadata.st_mode & 0o077) != 0
        ):
            return MarkerClaim.UNAVAILABLE
        existing_claim = _existing_marker_claim(directory_descriptor, name)
        if existing_claim is not None:
            return existing_claim
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
        except FileExistsError:
            return _existing_marker_claim(directory_descriptor, name) or MarkerClaim.UNAVAILABLE
        except OSError:
            return MarkerClaim.UNAVAILABLE
        created_identity = _marker_identity(os.fstat(descriptor))
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            _close_after_error(descriptor)
            _remove_created_marker(directory_descriptor, name, created_identity)
            return MarkerClaim.UNAVAILABLE
        try:
            os.close(descriptor)
        except OSError:
            _close_after_error(descriptor)
            _remove_created_marker(directory_descriptor, name, created_identity)
            return MarkerClaim.UNAVAILABLE
        return MarkerClaim.CLAIMED
    except OSError:
        return MarkerClaim.UNAVAILABLE
    finally:
        try:
            os.close(directory_descriptor)
        except OSError:
            pass


def evaluate_event(
    event: HookEvent,
    home: Path | str,
    env: Mapping[str, str],
    runner: Callable[..., object] = subprocess.run,
) -> RegistrationResult | None:
    if should_skip_event(event, home, env):
        return None
    try:
        identity = resolve_repository(event.cwd, runner=runner)
    except NudgeError as error:
        return _unknown(None, error.reason)
    result = query_registration(identity, home, runner=runner)
    if result.status is RegistrationStatus.UNKNOWN:
        return result
    marker = claim_session_marker(event.runtime, event.session_id, env=env)
    if marker is MarkerClaim.CLAIMED:
        return result
    if marker is MarkerClaim.ALREADY_DONE:
        return None
    return _unknown(result.repository_slug, "NUDGE_STATE_UNAVAILABLE")
