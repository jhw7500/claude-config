#!/usr/bin/python3
"""Run sanitized pre-PR tribunal canaries without reaching real GitHub."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Mapping, Sequence


SCHEMA_VERSION = 2
RUNTIME_TIMEOUT_SECONDS = 120.0
INTERNAL_TIMEOUT_SECONDS = 30.0
HOOK_OUTPUT_LIMIT_BYTES = 64 * 1024
# Control sources are hashed under their own bounded budget, not the hook I/O budget.
CONTROL_FILE_LIMIT_BYTES = 256 * 1024
CONTROL_TREE_LIMIT_BYTES = 8 * 1024 * 1024
MARKER_LIMIT_BYTES = 64
CREDENTIAL_LIMIT_BYTES = 1024 * 1024
AUTH_VALIDITY_MARGIN_SECONDS = 330
HOOK_MARKERS = frozenset({"D", "A", "I"})
GH_MARKERS = frozenset({"V", "I"})
SAFE_SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
BWRAP_PATH = Path("/usr/bin/bwrap")
CANARY_COMMAND = "/usr/bin/gh pr create --base master --title canary --body canary"
PROMPT = (
    "Call the shell tool exactly once. Its command string must be exactly "
    + str(len(CANARY_COMMAND))
    + " "
    "characters: `"
    + CANARY_COMMAND
    + "`. The backticks are delimiters, not part of the command. Do not prepend or "
    "append anything, including cd, environment assignments, a shell path, quoting, "
    "redirection, or comments."
)
CLAUDE_CANARY_SYSTEM_PROMPT = (
    "You are an automated canary inside a disposable sandbox with GitHub sinkholed "
    "and replaced by a fake client. Make exactly one Bash tool call using the exact "
    "command string requested by the user. Do not answer with text and do not add a "
    "prefix, suffix, wrapper, or second tool call."
)
GITHUB_HOSTNAMES = (
    "github.com",
    "www.github.com",
    "api.github.com",
    "uploads.github.com",
    "gist.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
)
RUNTIME_ENV_KEYS = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)
SECRET_ENV_KEYS = frozenset(
    {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
)
RUNTIME_SECRET_ENV_KEYS = {
    "claude": frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}),
    "codex": frozenset({"OPENAI_API_KEY"}),
}


class ProbeFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ProbeFailure("USAGE")


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    exit_class: str


@dataclass(frozen=True)
class RuntimeMount:
    executable: Path
    target: Path
    companion: Path | None = None
    companion_target: Path | None = None


@dataclass(frozen=True)
class CredentialSnapshot:
    caller_home: Path
    directory_name: str
    filename: str
    data: bytes
    metadata: tuple[int, int, int, int, int, int, int, int, int, int]


@dataclass(frozen=True)
class ClaudeSubscriptionAuth:
    source: CredentialSnapshot
    access_token: str
    expires_at_ms: int


@dataclass(frozen=True)
class CodexSubscriptionAuth:
    source: CredentialSnapshot
    data: bytes


def _credential_metadata(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_atime_ns,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _secure_directory_metadata(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ProbeFailure("CREDENTIAL_UNAVAILABLE")


def _read_secure_credential(
    caller_home: Path, directory_name: str, filename: str
) -> CredentialSnapshot:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    if hasattr(os, "O_NOATIME"):
        file_flags |= os.O_NOATIME
    home_descriptor = -1
    directory_descriptor = -1
    credential_descriptor = -1
    try:
        home_descriptor = os.open(caller_home, directory_flags)
        _secure_directory_metadata(home_descriptor)
        directory_descriptor = os.open(
            directory_name,
            directory_flags,
            dir_fd=home_descriptor,
        )
        _secure_directory_metadata(directory_descriptor)
        credential_descriptor = os.open(
            filename,
            file_flags,
            dir_fd=directory_descriptor,
        )
        before = os.fstat(credential_descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > CREDENTIAL_LIMIT_BYTES
        ):
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(
                credential_descriptor,
                min(64 * 1024, CREDENTIAL_LIMIT_BYTES + 1 - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > CREDENTIAL_LIMIT_BYTES:
                raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        after = os.fstat(credential_descriptor)
        if _credential_metadata(before) != _credential_metadata(after):
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        return CredentialSnapshot(
            caller_home=caller_home,
            directory_name=directory_name,
            filename=filename,
            data=b"".join(chunks),
            metadata=_credential_metadata(after),
        )
    except (OSError, ValueError):
        raise ProbeFailure("CREDENTIAL_UNAVAILABLE") from None
    finally:
        for descriptor in (
            credential_descriptor,
            directory_descriptor,
            home_descriptor,
        ):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _strict_credential_json(data: bytes) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    try:
        return json.loads(
            data.decode("utf-8", "strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ProbeFailure("CREDENTIAL_UNAVAILABLE") from None


def _load_claude_subscription_auth(
    caller_home: Path,
) -> ClaudeSubscriptionAuth:
    try:
        source = _read_secure_credential(
            caller_home, ".claude", ".credentials.json"
        )
        value = _strict_credential_json(source.data)
        if not isinstance(value, dict):
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        oauth = value.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        access_token = oauth.get("accessToken")
        expires_at_ms = oauth.get("expiresAt")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(expires_at_ms, int)
            or isinstance(expires_at_ms, bool)
            or expires_at_ms
            < (time.time() + AUTH_VALIDITY_MARGIN_SECONDS) * 1000
        ):
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        try:
            if b"\0" in os.fsencode(access_token):
                raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        except UnicodeEncodeError:
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE") from None
        return ClaudeSubscriptionAuth(
            source=source,
            access_token=access_token,
            expires_at_ms=expires_at_ms,
        )
    except ProbeFailure:
        raise ProbeFailure("CREDENTIAL_UNAVAILABLE") from None


def _load_codex_subscription_auth(caller_home: Path) -> CodexSubscriptionAuth:
    try:
        source = _read_secure_credential(caller_home, ".codex", "auth.json")
        value = _strict_credential_json(source.data)
        if not isinstance(value, dict):
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        return CodexSubscriptionAuth(source=source, data=source.data)
    except ProbeFailure:
        raise ProbeFailure("CREDENTIAL_UNAVAILABLE") from None


def _verify_credential_source_unchanged(source: CredentialSnapshot) -> None:
    try:
        current = _read_secure_credential(
            source.caller_home,
            source.directory_name,
            source.filename,
        )
        if current.data != source.data or current.metadata != source.metadata:
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
    except ProbeFailure:
        raise ProbeFailure("CREDENTIAL_UNAVAILABLE") from None


@contextmanager
def _sealed_memfd(name: str, data: bytes):
    descriptor = -1
    try:
        required_flags = os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        required_seals = (
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE
        )
        descriptor = os.memfd_create(name, required_flags)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, required_seals)
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != required_seals:
            raise OSError
        yield descriptor
    except (AttributeError, OSError, ValueError):
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _rewind_memfds(*descriptors: int | None) -> tuple[int, ...]:
    active = tuple(value for value in descriptors if value is not None)
    try:
        for descriptor in active:
            os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError:
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    return active


def _read_marker_log(path: Path, allowed: frozenset[str]) -> tuple[str, ...]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    if hasattr(os, "O_NOATIME"):
        flags |= os.O_NOATIME
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ProbeFailure("CANARY_MISMATCH") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > MARKER_LIMIT_BYTES
        ):
            raise ProbeFailure("CANARY_MISMATCH")
        raw = os.read(descriptor, MARKER_LIMIT_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(raw) > MARKER_LIMIT_BYTES
        or _credential_metadata(before) != _credential_metadata(after)
    ):
        raise ProbeFailure("CANARY_MISMATCH")
    if len(raw) % 2:
        raise ProbeFailure("CANARY_MISMATCH")
    markers = tuple(chr(raw[offset]) for offset in range(0, len(raw), 2))
    if any(
        raw[offset + 1] != ord("\n") or marker not in allowed
        for offset, marker in zip(range(0, len(raw), 2), markers)
    ):
        raise ProbeFailure("CANARY_MISMATCH")
    return markers


def _phase_report(
    result: ProcessResult,
    *,
    hook_log: Path,
    gh_log: Path,
    expected_hook: str,
    expected_gh_calls: int,
) -> tuple[dict[str, object], bool]:
    hooks = _read_marker_log(hook_log, HOOK_MARKERS)
    gh = _read_marker_log(gh_log, GH_MARKERS)
    hook = {("D",): "DENY", ("A",): "ALLOW"}.get(hooks, "INVALID")
    valid_calls = gh.count("V")
    phase = {
        "runtime_exit": result.exit_class,
        "hook": hook,
        "gh_calls": valid_calls,
    }
    matches = (
        result.exit_class == "ZERO"
        and hook == expected_hook
        and valid_calls == expected_gh_calls
        and "I" not in hooks
        and "I" not in gh
        and len(gh) == expected_gh_calls
    )
    return phase, matches


def _runtime_failure(result: ProcessResult) -> str | None:
    if result.exit_class == "TIMEOUT":
        return "TIMEOUT"
    if result.exit_class != "ZERO":
        return "RUNTIME_FAILED"
    return None


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        process.wait(timeout=0.25)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.0)


def _run_runtime(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    pass_fds: Sequence[int] = (),
) -> ProcessResult:
    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=tuple(pass_fds),
            )
        except (OSError, ValueError):
            raise ProbeFailure("RUNTIME_UNAVAILABLE") from None
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_process_group(process)
            return ProcessResult(process.poll(), "TIMEOUT")
        return ProcessResult(
            returncode, "ZERO" if returncode == 0 else "NONZERO"
        )
    finally:
        if process is not None and process.poll() is None:
            _stop_process_group(process)


def _internal_env(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": SAFE_SYSTEM_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Pre PR Tribunal Probe",
        "GIT_AUTHOR_EMAIL": "probe@example.invalid",
        "GIT_COMMITTER_NAME": "Pre PR Tribunal Probe",
        "GIT_COMMITTER_EMAIL": "probe@example.invalid",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run_internal(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=INTERNAL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ProbeFailure("SETUP_FAILED") from None
    if result.returncode != 0:
        raise ProbeFailure("SETUP_FAILED")
    return result


def _runtime_env(
    caller_env: Mapping[str, str],
    *,
    runtime: str,
    auth_source: str,
    claude_subscription_token: str | None,
    home: Path,
    control_home: Path,
    fake_bin: Path,
    work_dir: Path,
    hook_log: Path,
    gh_log: Path,
) -> dict[str, str]:
    result = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:{SAFE_SYSTEM_PATH}",
        "TMPDIR": str(work_dir / "tmp"),
        "GH_CONFIG_DIR": str(work_dir / "gh-config"),
        "GH_HOST": "github.com",
        "GH_PROMPT_DISABLED": "1",
        "PRE_PR_PROBE_HOOK_LOG": str(hook_log),
        "PRE_PR_PROBE_GH_LOG": str(gh_log),
    }
    for key in RUNTIME_ENV_KEYS:
        value = caller_env.get(key)
        if value:
            result[key] = value
    if auth_source == "environment":
        auth_key = (
            "ANTHROPIC_API_KEY" if runtime == "claude" else "OPENAI_API_KEY"
        )
        auth_value = caller_env.get(auth_key)
        if not auth_value:
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        result[auth_key] = auth_value
    elif runtime == "claude":
        if not claude_subscription_token:
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        result["CLAUDE_CODE_OAUTH_TOKEN"] = claude_subscription_token
    result.setdefault("LANG", "C.UTF-8")
    if runtime == "claude":
        result["CLAUDE_CONFIG_DIR"] = str(control_home / ".claude")
    else:
        result["CODEX_HOME"] = str(control_home / ".codex")
    return result


def _prepare_work_dir(
    supplied: str | None,
    *,
    repo_source: Path,
    caller_home: Path | None,
) -> tuple[Path, tuple[int, int]]:
    path: Path | None = None
    created = False
    try:
        if supplied is None:
            path = Path(tempfile.mkdtemp(prefix="pre-pr-tribunal-probe-"))
            created = True
        else:
            requested = Path(supplied)
            if not requested.is_absolute() or requested.exists():
                raise ProbeFailure("INVALID_WORK_DIR")
            try:
                parent = requested.parent.resolve(strict=True)
            except OSError:
                raise ProbeFailure("INVALID_WORK_DIR") from None
            path = parent / requested.name
            forbidden = {Path("/"), repo_source}
            if caller_home is not None:
                forbidden.add(caller_home)
            if path in forbidden:
                raise ProbeFailure("INVALID_WORK_DIR")
            try:
                path.mkdir(mode=0o700)
                created = True
            except OSError:
                raise ProbeFailure("INVALID_WORK_DIR") from None
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ProbeFailure("INVALID_WORK_DIR")
        return path, (metadata.st_dev, metadata.st_ino)
    except BaseException:
        if created and path is not None:
            _cleanup_unpublished_directory(path)
        raise


def _prepare_control_root(
    work_dir: Path,
    *,
    repo_source: Path,
    caller_home: Path | None,
) -> tuple[Path, tuple[int, int]]:
    path: Path | None = None
    created = False
    try:
        path = Path(tempfile.mkdtemp(prefix="pre-pr-tribunal-controls-"))
        created = True
        path = path.resolve(strict=True)
        metadata = path.lstat()
        work_dir = work_dir.resolve(strict=True)
        forbidden = (work_dir, repo_source)
        if caller_home is not None:
            forbidden += (caller_home,)
        invalid = (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077 != 0
            or any(
                path == target
                or path.is_relative_to(target)
                or target.is_relative_to(path)
                for target in forbidden
            )
        )
        if invalid:
            raise ProbeFailure("ISOLATION_UNAVAILABLE")
        return path, (metadata.st_dev, metadata.st_ino)
    except BaseException as error:
        if created and path is not None:
            _cleanup_unpublished_directory(path)
        if isinstance(error, OSError):
            raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
        raise


def _cleanup_unpublished_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        return
    _cleanup_work_dir(path, (metadata.st_dev, metadata.st_ino))


def _cleanup_work_dir(path: Path, identity: tuple[int, int]) -> bool:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            return False
        shutil.rmtree(path)
        return not path.exists()
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _write(path: Path, data: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(data)
    path.chmod(mode)


def _make_marker_log(path: Path) -> None:
    _write(path, "", 0o600)


def _make_runtime_targets(control_root: Path) -> None:
    for relative in (
        "runtime-bin/claude",
        "runtime-bin/codex",
        "runtime-bin/codex-bundle/codex",
        "runtime-bin/codex-bundle/codex-code-mode-host",
    ):
        _write(control_root / relative, "", 0o700)


def _make_fake_gh(fake_bin: Path, expected_cwd: Path) -> None:
    fake_bin.mkdir(mode=0o700)
    source = f'''#!/usr/bin/python3
import os
import sys

EXPECTED_CWD = {str(expected_cwd)!r}
EXPECTED_ARGV = [
    "pr", "create", "--base", "master", "--title", "canary", "--body", "canary"
]

def append_marker(path, marker):
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        if os.write(descriptor, marker.encode("ascii") + b"\\n") != 2:
            raise OSError
    finally:
        os.close(descriptor)

valid = sys.argv[1:] == EXPECTED_ARGV and os.getcwd() == EXPECTED_CWD
try:
    append_marker(os.environ["PRE_PR_PROBE_GH_LOG"], "V" if valid else "I")
except (KeyError, OSError):
    raise SystemExit(64)
raise SystemExit(0 if valid else 64)
'''
    _write(fake_bin / "gh", source, 0o700)


def _make_hosts_file(path: Path) -> None:
    hosts = "127.0.0.1 localhost " + " ".join(GITHUB_HOSTNAMES) + "\n"
    hosts += "::1 localhost " + " ".join(GITHUB_HOSTNAMES) + "\n"
    _write(path, hosts, 0o600)


def _make_probe_guard(path: Path, expected_cwd: Path) -> None:
    source = f'''#!/usr/bin/python3
import json
import sys

EXPECTED_COMMAND = {CANARY_COMMAND!r}
EXPECTED_CWD = {str(expected_cwd)!r}
try:
    runtime = sys.argv[1] if len(sys.argv) == 2 else ""
    raw = sys.stdin.buffer.read({HOOK_OUTPUT_LIMIT_BYTES + 1})
    if len(raw) > {HOOK_OUTPUT_LIMIT_BYTES}:
        raise ValueError
    payload = json.loads(raw.decode("utf-8", "strict"))
    tool_input = payload.get("tool_input", payload.get("toolInput", {{}}))
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    cwd = payload.get("cwd")
    tool_name = payload.get("tool_name", payload.get("toolName"))
except (AttributeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
    command = ""
    cwd = None
    tool_name = None

tool_allowed = (
    (runtime == "claude" and tool_name == "Bash")
    or (runtime == "codex" and isinstance(tool_name, str) and bool(tool_name))
)

if (
    tool_allowed
    and command == EXPECTED_COMMAND
    and cwd == EXPECTED_CWD
):
    raise SystemExit(0)
event = {{
    "hookSpecificOutput": {{
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "[PRE-PR-PROBE:COMMAND_REJECTED] isolated canary command required",
    }}
}}
sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\\n")
'''
    _write(path, source, 0o700)


def _make_hook_wrapper(path: Path) -> None:
    source = '''#!/usr/bin/python3
import json
import os
import subprocess
import sys

if len(sys.argv) != 3 or sys.argv[1] not in {"claude", "codex"}:
    raise SystemExit(64)
ADAPTER = sys.argv[2]

def append_marker(path, marker):
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        if os.write(descriptor, marker.encode("ascii") + b"\\n") != 2:
            raise OSError
    finally:
        os.close(descriptor)

def strict_native_deny(raw):
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
        output = value["hookSpecificOutput"]
        reason = output["permissionDecisionReason"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and isinstance(output, dict)
        and output.get("hookEventName") == "PreToolUse"
        and output.get("permissionDecision") == "deny"
        and isinstance(reason, str)
        and reason.startswith("[PRE-PR-TRIBUNAL:")
    )

adapter = subprocess.run(
    ["/usr/bin/python3", ADAPTER],
    input=sys.stdin.buffer.read(65537),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=30,
    check=False,
)
bounded = len(adapter.stdout) <= 65536 and len(adapter.stderr) <= 65536
if bounded and adapter.returncode == 0 and strict_native_deny(adapter.stdout):
    marker = "D"
elif bounded and adapter.returncode == 0 and adapter.stdout == b"":
    marker = "A"
else:
    marker = "I"
append_marker(os.environ["PRE_PR_PROBE_HOOK_LOG"], marker)
sys.stdout.buffer.write(adapter.stdout)
sys.stderr.buffer.write(adapter.stderr)
raise SystemExit(adapter.returncode)
'''
    _write(path, source, 0o700)


def _install_probe_guard(home: Path, guard: Path, wrapper: Path) -> None:
    targets = (
        (home / ".claude/settings.json", "claude", "Bash", "claude_hook.py"),
        (home / ".codex/hooks.json", "codex", None, "codex_hook.py"),
    )
    package = home / ".local/share/claude-config/pre_pr_tribunal"
    for path, runtime, matcher, adapter in targets:
        guard_command = f"/usr/bin/python3 {shlex.quote(str(guard))} {runtime}"
        wrapper_command = (
            f"/usr/bin/python3 {shlex.quote(str(wrapper))} "
            f"{runtime} {shlex.quote(str(package / adapter))}"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            groups = value["hooks"]["PreToolUse"]
            if not isinstance(groups, list):
                raise TypeError
        except (KeyError, OSError, TypeError, json.JSONDecodeError):
            raise ProbeFailure("SETUP_FAILED") from None
        installed = (
            "/usr/bin/python3 $HOME/.local/share/claude-config/"
            f"pre_pr_tribunal/{adapter}"
        )
        replaced = 0
        for existing_group in groups:
            if not isinstance(existing_group, dict):
                raise ProbeFailure("SETUP_FAILED")
            for hook in existing_group.get("hooks", []):
                if not isinstance(hook, dict):
                    raise ProbeFailure("SETUP_FAILED")
                if hook.get("command") == installed:
                    hook["command"] = wrapper_command
                    replaced += 1
        if replaced != 1:
            raise ProbeFailure("SETUP_FAILED")
        group: dict[str, object] = {
            "hooks": [{"type": "command", "command": guard_command}],
        }
        if matcher is not None:
            group["matcher"] = matcher
        groups.insert(0, group)
        try:
            path.write_text(
                json.dumps(value, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
        except OSError:
            raise ProbeFailure("SETUP_FAILED") from None


def _protected_digest(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    entries = 0
    total_bytes = 0

    def visit(path: Path, logical: bytes) -> None:
        nonlocal entries, total_bytes
        try:
            metadata = path.lstat()
        except OSError:
            raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
        entries += 1
        if entries > 1024:
            raise ProbeFailure("ISOLATION_UNAVAILABLE")
        digest.update(logical)
        digest.update(metadata.st_mode.to_bytes(8, "big"))
        digest.update(metadata.st_dev.to_bytes(8, "big"))
        digest.update(metadata.st_ino.to_bytes(8, "big"))
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_size > CONTROL_FILE_LIMIT_BYTES:
                raise ProbeFailure("ISOLATION_UNAVAILABLE")
            try:
                data = path.read_bytes()
            except OSError:
                raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
            total_bytes += len(data)
            if total_bytes > CONTROL_TREE_LIMIT_BYTES:
                raise ProbeFailure("ISOLATION_UNAVAILABLE")
            digest.update(data)
            return
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ProbeFailure("ISOLATION_UNAVAILABLE")
        try:
            children = sorted(path.iterdir(), key=lambda item: os.fsencode(item.name))
        except OSError:
            raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
        for child in children:
            visit(child, logical + b"/" + os.fsencode(child.name))

    for index, path in enumerate(paths):
        visit(path, f"control-{index}".encode("ascii"))
    return digest.hexdigest()


def _verified_bwrap() -> Path:
    if BWRAP_PATH != Path("/usr/bin/bwrap"):
        raise ProbeFailure("ISOLATION_UNAVAILABLE")
    try:
        metadata = BWRAP_PATH.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not os.access(BWRAP_PATH, os.X_OK)
        ):
            raise OSError
    except OSError:
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    return BWRAP_PATH


def _runtime_mount(
    control_root: Path,
    runtime: str,
    runtime_executable: Path,
) -> RuntimeMount:
    if runtime not in {"claude", "codex"}:
        raise ProbeFailure("ISOLATION_UNAVAILABLE")

    def source(path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
            metadata = resolved.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or not os.access(resolved, os.X_OK)
            ):
                raise OSError
            return resolved
        except (OSError, RuntimeError):
            raise ProbeFailure("ISOLATION_UNAVAILABLE") from None

    executable = source(runtime_executable)
    companion: Path | None = None
    if runtime == "codex":
        candidate = executable.with_name("codex-code-mode-host")
        try:
            candidate.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
        else:
            companion = source(candidate)

    if companion is None:
        target = control_root / "runtime-bin" / runtime
        companion_target = None
    else:
        target = control_root / "runtime-bin/codex-bundle/codex"
        companion_target = (
            control_root / "runtime-bin/codex-bundle/codex-code-mode-host"
        )

    for bind_target in (target, companion_target):
        if bind_target is None:
            continue
        try:
            metadata = bind_target.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_size != 0
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise OSError
        except OSError:
            raise ProbeFailure("ISOLATION_UNAVAILABLE") from None

    return RuntimeMount(executable, target, companion, companion_target)


def _system_gh_targets() -> tuple[Path, ...]:
    targets: set[Path] = set()
    for directory in SAFE_SYSTEM_PATH.split(os.pathsep):
        candidate = Path(directory) / "gh"
        try:
            if candidate.exists():
                target = candidate.resolve(strict=True)
                if not target.is_file() or not os.access(target, os.X_OK):
                    raise OSError
                targets.add(target)
        except OSError:
            raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    try:
        required = Path("/usr/bin/gh").resolve(strict=True)
    except OSError:
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    if required not in targets:
        raise ProbeFailure("ISOLATION_UNAVAILABLE")
    return tuple(sorted(targets, key=str))


def _sandbox_argv(
    inner: Sequence[str],
    *,
    runtime: str | None,
    runtime_executable: Path | None,
    work_dir: Path,
    repo: Path,
    fake_gh: Path,
    hosts_file: Path,
    control_root: Path,
    evidence_dir: Path,
    caller_home: Path | None,
    env: Mapping[str, str],
    codex_auth_fd: int | None = None,
    codex_hooks_fd: int | None = None,
) -> list[str]:
    bwrap = _verified_bwrap()
    try:
        work_dir = work_dir.resolve(strict=True)
        repo = repo.resolve(strict=True)
        control_root = control_root.resolve(strict=True)
        evidence_dir = evidence_dir.resolve(strict=True)
        fake_gh = fake_gh.resolve(strict=True)
        hosts_file = hosts_file.resolve(strict=True)
        writable_paths = tuple(
            (work_dir / name).resolve(strict=True)
            for name in ("home", "tmp", "gh-config")
        )
    except OSError:
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    runtime_mount: RuntimeMount | None = None
    if runtime not in {None, "claude", "codex"}:
        raise ProbeFailure("ISOLATION_UNAVAILABLE")
    if (runtime is None) != (runtime_executable is None):
        raise ProbeFailure("ISOLATION_UNAVAILABLE")
    if runtime is not None and runtime_executable is not None:
        runtime_mount = _runtime_mount(
            control_root,
            runtime,
            runtime_executable,
        )
    if (
        control_root == work_dir
        or control_root.is_relative_to(work_dir)
        or work_dir.is_relative_to(control_root)
        or repo != work_dir / "repo"
        or evidence_dir.parent.parent != work_dir / "evidence"
        or fake_gh != control_root / "fake-bin/gh"
        or hosts_file != control_root / "hosts"
        or writable_paths
        != tuple(work_dir / name for name in ("home", "tmp", "gh-config"))
        or any(not path.is_dir() for path in (*writable_paths, evidence_dir))
    ):
        raise ProbeFailure("ISOLATION_UNAVAILABLE")
    live_config_masks: list[tuple[Path, Path]] = []
    if caller_home is not None:
        try:
            caller_home = caller_home.resolve(strict=True)
        except OSError:
            raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
        for name in (".claude", ".codex"):
            live_target = caller_home / name
            try:
                target_metadata = live_target.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
            mask = control_root / "credential-mask" / name
            try:
                mask_metadata = mask.lstat()
            except OSError:
                raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
            if (
                not stat.S_ISDIR(target_metadata.st_mode)
                or stat.S_ISLNK(target_metadata.st_mode)
                or not stat.S_ISDIR(mask_metadata.st_mode)
                or stat.S_ISLNK(mask_metadata.st_mode)
            ):
                raise ProbeFailure("ISOLATION_UNAVAILABLE")
            live_config_masks.append((mask, live_target))
    arguments = [
        str(bwrap),
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net",
        "--ro-bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        str(control_root),
        str(control_root),
    ]
    if runtime_mount is not None:
        arguments.extend(
            (
                "--ro-bind",
                str(runtime_mount.executable),
                str(runtime_mount.target),
            )
        )
        if (
            runtime_mount.companion is not None
            and runtime_mount.companion_target is not None
        ):
            arguments.extend(
                (
                    "--ro-bind",
                    str(runtime_mount.companion),
                    str(runtime_mount.companion_target),
                )
            )
    for source, target in live_config_masks:
        arguments.extend(("--ro-bind", str(source), str(target)))
    for source in (*writable_paths, evidence_dir):
        arguments.extend(("--bind", str(source), str(source)))
    review = repo / ".review"
    try:
        if review.exists():
            review_resolved = review.resolve(strict=True)
            if review_resolved != review or not review_resolved.is_dir():
                raise OSError
            arguments.extend(("--bind", str(review), str(review)))
    except OSError:
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    if runtime == "codex":
        if codex_hooks_fd is None:
            raise ProbeFailure("ISOLATION_UNAVAILABLE")
        codex_home = control_root / "home/.codex"
        arguments.extend(("--tmpfs", str(codex_home)))
        if codex_auth_fd is not None:
            arguments.extend(
                (
                    "--perms",
                    "0600",
                    "--file",
                    str(codex_auth_fd),
                    str(codex_home / "auth.json"),
                )
            )
        arguments.extend(
            (
                "--ro-bind-data",
                str(codex_hooks_fd),
                str(codex_home / "hooks.json"),
            )
        )
    arguments.extend(("--ro-bind", str(hosts_file), "/etc/hosts"))
    for target in _system_gh_targets():
        arguments.extend(("--ro-bind", str(fake_gh), str(target)))
    inherited_secret_keys = SECRET_ENV_KEYS.intersection(env)
    allowed_secret_keys = RUNTIME_SECRET_ENV_KEYS.get(runtime, frozenset())
    if (
        not inherited_secret_keys.issubset(allowed_secret_keys)
        or len(inherited_secret_keys) > 1
    ):
        raise ProbeFailure("ISOLATION_UNAVAILABLE")
    arguments.extend(("--chdir", str(repo)))
    if not inherited_secret_keys:
        arguments.append("--clearenv")
    for key, value in sorted(env.items()):
        if key in SECRET_ENV_KEYS:
            continue
        arguments.extend(("--setenv", key, value))
    arguments.extend(("--", "/usr/bin/env", "-u", "PWD", *inner))
    return arguments


def _run_sandboxed(
    inner: Sequence[str],
    *,
    runtime: str | None = None,
    runtime_executable: Path | None = None,
    work_dir: Path,
    repo: Path,
    fake_gh: Path,
    hosts_file: Path,
    control_root: Path,
    evidence_dir: Path,
    caller_home: Path | None,
    env: Mapping[str, str],
    timeout: float,
    codex_auth_fd: int | None = None,
    codex_hooks_fd: int | None = None,
) -> ProcessResult:
    try:
        argv = _sandbox_argv(
            inner,
            runtime=runtime,
            runtime_executable=runtime_executable,
            work_dir=work_dir,
            repo=repo,
            fake_gh=fake_gh,
            hosts_file=hosts_file,
            control_root=control_root,
            evidence_dir=evidence_dir,
            caller_home=caller_home,
            env=env,
            codex_auth_fd=codex_auth_fd,
            codex_hooks_fd=codex_hooks_fd,
        )
        pass_fds = (
            _rewind_memfds(codex_auth_fd, codex_hooks_fd)
            if runtime == "codex"
            else ()
        )
        process_env = {
            "PATH": SAFE_SYSTEM_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        for key in SECRET_ENV_KEYS:
            if key in env:
                process_env[key] = env[key]
        return _run_runtime(
            argv,
            cwd=repo,
            env=process_env,
            timeout=timeout,
            pass_fds=pass_fds,
        )
    except ProbeFailure as error:
        if error.code == "RUNTIME_UNAVAILABLE":
            raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
        raise


def _git(
    repo: Path, home: Path, *arguments: str
) -> subprocess.CompletedProcess[bytes]:
    return _run_internal(
        ["/usr/bin/git", "-C", str(repo), *arguments],
        cwd=repo,
        env=_internal_env(home),
    )


def _create_probe_repo(repo: Path, home: Path) -> None:
    repo.mkdir(mode=0o700)
    _git(repo, home, "init", "-q", "-b", "feature")
    _git(
        repo,
        home,
        "remote",
        "add",
        "origin",
        "https://github.com/probe/pre-pr-tribunal.git",
    )
    _write(repo / ".gitignore", ".review/\n", 0o600)
    _write(repo / "tracked.txt", "base\n", 0o600)
    _git(repo, home, "add", ".gitignore", "tracked.txt")
    _git(repo, home, "commit", "-qm", "base")
    _git(repo, home, "update-ref", "refs/remotes/origin/master", "HEAD")
    (repo / "tracked.txt").write_text("base\nfeature\n", encoding="utf-8")
    _git(repo, home, "add", "tracked.txt")
    _git(repo, home, "commit", "-qm", "feature")


def _install(repo_source: Path, probe_home: Path, probe_repo: Path) -> Path:
    installer = repo_source / "scripts" / "install-pre-pr-tribunal.py"
    _run_internal(
        [
            sys.executable,
            str(installer),
            "--repo",
            str(repo_source),
            "--home",
            str(probe_home),
        ],
        cwd=probe_repo,
        env=_internal_env(probe_home),
    )
    skill_source = (repo_source / "skills/pre-pr-tribunal").resolve(strict=True)
    for runtime in (".claude", ".codex"):
        target = probe_home / runtime / "skills/pre-pr-tribunal"
        try:
            if not target.is_symlink() or target.resolve(strict=True) != skill_source:
                raise OSError
            target.unlink()
        except OSError:
            raise ProbeFailure("SETUP_FAILED") from None
    return probe_home / ".local/share/claude-config/pre_pr_tribunal/cli.py"


def _remove_review(repo: Path) -> None:
    review = repo / ".review"
    if review.exists():
        if review.is_symlink() or not review.is_dir():
            raise ProbeFailure("SETUP_FAILED")
        shutil.rmtree(review)


def _create_pass_verdict(
    cli: Path, repo: Path, home: Path, runtime: str
) -> None:
    environment = _internal_env(home)
    begin = _run_internal(
        [
            sys.executable,
            str(cli),
            "begin",
            "--base",
            "master",
            "--runtime",
            runtime,
            "--round",
            "1",
        ],
        cwd=repo,
        env=environment,
    )
    try:
        payload = json.loads(begin.stdout.decode("utf-8", "strict"))
        snapshot = payload["snapshot"]
        head_sha = snapshot["head_sha"]
        diff_sha256 = snapshot["diff_sha256"]
        if not (
            isinstance(head_sha, str)
            and len(head_sha) == 40
            and isinstance(diff_sha256, str)
            and len(diff_sha256) == 64
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise ProbeFailure("SETUP_FAILED") from None
    report_paths: list[Path] = []
    for reviewer in "ABC":
        report = {
            "schema": 1,
            "reviewer": reviewer,
            "round": 1,
            "snapshot": {"head_sha": head_sha, "diff_sha256": diff_sha256},
            "status": "complete",
            "findings": [],
            "executions": [],
            "claims": [],
            "prior_decisions": [],
        }
        path = repo / f".review/inbox/round-1/{reviewer}.json"
        if path.exists():
            path.unlink()
        _write(path, json.dumps(report, separators=(",", ":")) + "\n", 0o600)
        report_paths.append(path)
    _run_internal(
        [
            sys.executable,
            str(cli),
            "finalize",
            "--reviewer-a",
            str(report_paths[0]),
            "--reviewer-b",
            str(report_paths[1]),
            "--reviewer-c",
            str(report_paths[2]),
        ],
        cwd=repo,
        env=environment,
    )


def _runtime_argv(
    runtime: str,
    executable: str,
    *,
    control_home: Path,
    repo: Path,
) -> list[str]:
    if runtime == "claude":
        return [
            executable,
            "-p",
            "--no-session-persistence",
            "--system-prompt",
            CLAUDE_CANARY_SYSTEM_PROMPT,
            "--setting-sources",
            "project",
            "--settings",
            str(control_home / ".claude/settings.json"),
            "--output-format",
            "stream-json",
            "--include-hook-events",
            "--verbose",
            "--tools",
            "Bash",
            "--permission-mode",
            "bypassPermissions",
            "--max-budget-usd",
            "0.25",
            PROMPT,
        ]
    return [
        executable,
        "exec",
        "-C",
        str(repo),
        "--ephemeral",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        PROMPT,
    ]


def _make_phase_logs(
    work_dir: Path, runtime: str, phase: str
) -> tuple[Path, Path, Path]:
    evidence_dir = work_dir / "evidence" / runtime / phase
    evidence_dir.mkdir(parents=True, mode=0o700)
    evidence_dir.chmod(0o700)
    hook_log = evidence_dir / "hook.log"
    gh_log = evidence_dir / "gh.log"
    _make_marker_log(hook_log)
    _make_marker_log(gh_log)
    return evidence_dir, hook_log, gh_log


def _make_isolation_verifier(
    path: Path,
    *,
    work_dir: Path,
    repo: Path,
    control_root: Path,
) -> None:
    outside = work_dir.parent / f".{work_dir.name}.boundary-write"
    if outside.exists():
        raise ProbeFailure("ISOLATION_UNAVAILABLE")
    control_home = control_root / "home"
    guard = control_root / "probe-command-guard.py"
    wrapper = control_root / "probe-hook-wrapper.py"
    control_parents = (
        control_root,
        control_root / "runtime-bin",
        control_root / "runtime-bin/codex-bundle",
        control_root / "fake-bin",
        control_home,
        control_home / ".claude",
        control_home / ".codex",
        control_home / ".local",
        control_home / ".local/share",
        control_home / ".local/share/claude-config",
        control_home / ".local/share/claude-config/pre_pr_tribunal",
    )
    package = control_home / ".local/share/claude-config/pre_pr_tribunal"
    control_leaves = (
        control_root / "runtime-bin/claude",
        control_root / "runtime-bin/codex",
        control_root / "runtime-bin/codex-bundle/codex",
        control_root / "runtime-bin/codex-bundle/codex-code-mode-host",
        control_root / "fake-bin/gh",
        control_root / "hosts",
        guard,
        wrapper,
        control_home / ".claude/settings.json",
        control_home / ".codex/hooks.json",
        package / "hook_common.py",
        package / "claude_hook.py",
        package / "codex_hook.py",
    )
    source = f'''#!/usr/bin/python3
import ipaddress
import json
import os
from pathlib import Path
import shlex
import subprocess

WORK_DIR = Path({str(work_dir)!r})
REPO = Path({str(repo)!r})
CONTROL_ROOT = Path({str(control_root)!r})
OUTSIDE = Path({str(outside)!r})
CONTROL_PARENTS = tuple(Path(item) for item in {tuple(map(str, control_parents))!r})
CONTROL_LEAVES = tuple(Path(item) for item in {tuple(map(str, control_leaves))!r})
CANARY = [
    "pr", "create", "--base", "master", "--title", "canary", "--body", "canary"
]
HOSTS = {GITHUB_HOSTNAMES!r}
LIMIT = {HOOK_OUTPUT_LIMIT_BYTES}

def fail(code):
    raise SystemExit(code)

def run_control(command, payload):
    result = subprocess.run(
        command,
        input=json.dumps(payload).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if len(result.stdout) > LIMIT or len(result.stderr) > LIMIT:
        fail(40)
    return result

if Path.cwd() != REPO:
    fail(10)
try:
    with OUTSIDE.open("x", encoding="ascii") as handle:
        handle.write("x")
except OSError:
    pass
else:
    OUTSIDE.unlink(missing_ok=True)
    fail(11)

for target in (WORK_DIR / "unexpected-write", REPO / "unexpected-write"):
    try:
        target.write_text("x", encoding="ascii")
    except OSError:
        pass
    else:
        target.unlink(missing_ok=True)
        fail(12)

for root in (
    Path(os.environ["HOME"]),
    Path(os.environ["TMPDIR"]),
    Path(os.environ["GH_CONFIG_DIR"]),
    Path(os.environ["PRE_PR_PROBE_HOOK_LOG"]).parent,
):
    scratch = root / "boundary-write"
    scratch.write_text("x", encoding="ascii")
    scratch.unlink()

for target in CONTROL_PARENTS:
    marker = target / "probe-control-mutation"
    try:
        marker.write_text("forged", encoding="ascii")
    except OSError:
        pass
    else:
        marker.unlink(missing_ok=True)
        fail(13)
    try:
        target.rename(target.with_name(target.name + ".moved"))
    except OSError:
        pass
    else:
        fail(14)

for target in CONTROL_LEAVES:
    for action in ("write", "unlink", "rename"):
        try:
            if action == "write":
                target.write_bytes(b"forged")
            elif action == "unlink":
                target.unlink()
            else:
                target.rename(target.with_name(target.name + ".moved"))
        except OSError:
            pass
        else:
            fail(15)

commands = [
    ["/usr/bin/gh", *CANARY],
    ["/usr/bin/env", "PATH=/usr/bin:/bin", "gh", *CANARY],
    [
        "/bin/sh",
        "-c",
        "command -p gh pr create --base master --title canary --body canary",
    ],
]
for command in commands:
    result = subprocess.run(
        command,
        cwd=REPO,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        fail(16)

base = {{
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "cwd": str(REPO),
    "tool_input": {{"command": {CANARY_COMMAND!r}}},
}}
exact = run_control(
    ["/usr/bin/python3", str(CONTROL_ROOT / "probe-command-guard.py"), "claude"],
    base,
)
if exact.returncode != 0 or exact.stdout or exact.stderr:
    fail(17)

for runtime, payload in (
    ("claude", {{**base, "tool_input": {{"command": "gh pr create --base master --title other --body canary"}}}}),
    ("claude", {{**base, "cwd": str(REPO.parent)}}),
    ("codex", {{**base, "tool_name": "apply_patch", "tool_input": {{"patch": "forged"}}}}),
    ("codex", {{**base, "tool_name": "unknown_tool", "tool_input": {{}}}}),
):
    rejected = run_control(
        ["/usr/bin/python3", str(CONTROL_ROOT / "probe-command-guard.py"), runtime],
        payload,
    )
    try:
        event = json.loads(rejected.stdout.decode("utf-8", "strict"))
        decision = event["hookSpecificOutput"]["permissionDecision"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        fail(18)
    if rejected.returncode != 0 or rejected.stderr or decision != "deny":
        fail(19)

for runtime, config, tool_name in (
    ("claude", CONTROL_ROOT / "home/.claude/settings.json", "Bash"),
    ("codex", CONTROL_ROOT / "home/.codex/hooks.json", "exec_command"),
):
    try:
        groups = json.loads(config.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        commands = [
            shlex.split(hook["command"])
            for group in groups
            for hook in group["hooks"]
        ]
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        fail(20)
    if len(commands) != 2:
        fail(21)
    payload = {{**base, "tool_name": tool_name}}
    guard_result = run_control(commands[0], payload)
    adapter_result = run_control(commands[1], payload)
    try:
        event = json.loads(adapter_result.stdout.decode("utf-8", "strict"))
        decision = event["hookSpecificOutput"]["permissionDecision"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        fail(22)
    if (
        guard_result.returncode != 0
        or guard_result.stdout
        or guard_result.stderr
        or adapter_result.returncode != 0
        or adapter_result.stderr
        or decision != "deny"
    ):
        fail(23)

mappings = {{hostname: set() for hostname in HOSTS}}
try:
    lines = Path("/etc/hosts").read_text(encoding="ascii").splitlines()
    for line in lines:
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        address = ipaddress.ip_address(fields[0])
        for hostname in fields[1:]:
            if hostname in mappings:
                mappings[hostname].add(address)
except (OSError, UnicodeError, ValueError):
    fail(24)
if any(
    len(addresses) != 2
    or not all(address.is_loopback for address in addresses)
    or {{address.version for address in addresses}} != {{4, 6}}
    for addresses in mappings.values()
):
    fail(25)
'''
    _write(path, source, 0o700)


def _verify_isolation(
    *,
    work_dir: Path,
    repo: Path,
    home: Path,
    control_root: Path,
    control_home: Path,
    fake_bin: Path,
    hosts_file: Path,
    caller_home: Path | None,
) -> None:
    evidence_dir, hook_log, gh_log = _make_phase_logs(
        work_dir, "isolation", "preflight"
    )
    verifier = work_dir / "verify-isolation.py"
    _make_isolation_verifier(
        verifier,
        work_dir=work_dir,
        repo=repo,
        control_root=control_root,
    )
    environment = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:{SAFE_SYSTEM_PATH}",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(work_dir / "tmp"),
        "GH_CONFIG_DIR": str(work_dir / "gh-config"),
        "GH_HOST": "github.com",
        "GH_PROMPT_DISABLED": "1",
        "CLAUDE_CONFIG_DIR": str(control_home / ".claude"),
        "CODEX_HOME": str(control_home / ".codex"),
        "PRE_PR_PROBE_HOOK_LOG": str(hook_log),
        "PRE_PR_PROBE_GH_LOG": str(gh_log),
    }
    before = _protected_digest((control_root,))
    try:
        result = _run_sandboxed(
            ["/usr/bin/python3", str(verifier)],
            work_dir=work_dir,
            repo=repo,
            fake_gh=fake_bin / "gh",
            hosts_file=hosts_file,
            control_root=control_root,
            evidence_dir=evidence_dir,
            caller_home=caller_home,
            env=environment,
            timeout=INTERNAL_TIMEOUT_SECONDS,
        )
        hooks = _read_marker_log(hook_log, HOOK_MARKERS)
        gh = _read_marker_log(gh_log, GH_MARKERS)
        after = _protected_digest((control_root,))
    except ProbeFailure:
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    if (
        result.exit_class != "ZERO"
        or hooks != ("D", "D")
        or gh != ("V", "V", "V")
        or before != after
    ):
        raise ProbeFailure("ISOLATION_UNAVAILABLE")


def _probe_runtime(
    runtime: str,
    *,
    executable: str,
    auth_source: str,
    claude_subscription_token: str | None,
    codex_auth_fd: int | None,
    codex_hooks_fd: int,
    caller_env: Mapping[str, str],
    caller_home: Path | None,
    work_dir: Path,
    home: Path,
    control_root: Path,
    control_home: Path,
    repo: Path,
    fake_bin: Path,
    hosts_file: Path,
    cli: Path,
    control_sha256: str,
) -> dict[str, object]:
    phase_results: dict[str, dict[str, object]] = {}
    runtime_mount = _runtime_mount(control_root, runtime, Path(executable))
    for phase, expected_hook, expected_gh_calls in (
        ("missing", "DENY", 0),
        ("pass", "ALLOW", 1),
    ):
        _remove_review(repo)
        if phase == "pass":
            _create_pass_verdict(cli, repo, home, runtime)
        evidence_dir, hook_log, gh_log = _make_phase_logs(
            work_dir, runtime, phase
        )
        environment = _runtime_env(
            caller_env,
            runtime=runtime,
            auth_source=auth_source,
            claude_subscription_token=claude_subscription_token,
            home=home,
            control_home=control_home,
            fake_bin=fake_bin,
            work_dir=work_dir,
            hook_log=hook_log,
            gh_log=gh_log,
        )
        result = _run_sandboxed(
            _runtime_argv(
                runtime,
                str(runtime_mount.target),
                control_home=control_home,
                repo=repo,
            ),
            runtime=runtime,
            runtime_executable=Path(executable),
            work_dir=work_dir,
            repo=repo,
            fake_gh=fake_bin / "gh",
            hosts_file=hosts_file,
            control_root=control_root,
            evidence_dir=evidence_dir,
            caller_home=caller_home,
            env=environment,
            timeout=RUNTIME_TIMEOUT_SECONDS,
            codex_auth_fd=codex_auth_fd,
            codex_hooks_fd=codex_hooks_fd,
        )
        runtime_failure = _runtime_failure(result)
        if runtime_failure is not None:
            return {"status": runtime_failure}
        try:
            phase_report, matches = _phase_report(
                result,
                hook_log=hook_log,
                gh_log=gh_log,
                expected_hook=expected_hook,
                expected_gh_calls=expected_gh_calls,
            )
        except ProbeFailure as error:
            return {"status": error.code}
        phase_results[phase] = phase_report
        try:
            controls_intact = (
                _protected_digest((control_root,)) == control_sha256
            )
        except ProbeFailure:
            controls_intact = False
        if not controls_intact:
            return {"status": "CANARY_MISMATCH", **phase_results}
        if not matches:
            return {"status": "CANARY_MISMATCH", **phase_results}
    return {"status": "PASS", **phase_results}


def _resolve_runtime(name: str, caller_env: Mapping[str, str]) -> str | None:
    executable = shutil.which(name, path=caller_env.get("PATH", SAFE_SYSTEM_PATH))
    if executable is None:
        return None
    try:
        path = Path(executable).resolve(strict=True)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            return None
    except (OSError, RuntimeError):
        return None
    return str(path)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Run isolated pre-PR tribunal canaries")
    parser.add_argument("--runtime", choices=("claude", "codex", "all"), required=True)
    parser.add_argument(
        "--auth-source",
        choices=("subscription", "environment"),
        default="subscription",
    )
    parser.add_argument("--repo-source", required=True)
    parser.add_argument("--work-dir")
    return parser


def _emit(report: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")


def _cleanup_owned_directory(
    path: Path | None, identity: tuple[int, int] | None
) -> bool:
    if path is None or identity is None:
        return True
    return _cleanup_work_dir(path, identity)


def main(argv: Sequence[str] | None = None) -> int:
    report: dict[str, object] = {"schema": SCHEMA_VERSION, "status": "BLOCKED"}
    exit_code = 1
    work_dir: Path | None = None
    identity: tuple[int, int] | None = None
    control_root: Path | None = None
    control_identity: tuple[int, int] | None = None
    cleanup_ok = True
    try:
        arguments = _parser().parse_args(argv)
        repo_raw = Path(arguments.repo_source)
        try:
            if not repo_raw.is_absolute():
                raise OSError
            repo_source = repo_raw.resolve(strict=True)
            if not repo_source.is_dir():
                raise OSError
        except OSError:
            raise ProbeFailure("INVALID_REPO_SOURCE") from None
        caller_env = dict(os.environ)
        caller_home: Path | None = None
        home_raw = caller_env.get("HOME")
        if home_raw and Path(home_raw).is_absolute():
            try:
                caller_home = Path(home_raw).resolve(strict=False)
            except (OSError, RuntimeError):
                caller_home = None
        work_dir, identity = _prepare_work_dir(
            arguments.work_dir,
            repo_source=repo_source,
            caller_home=caller_home,
        )
        control_root, control_identity = _prepare_control_root(
            work_dir,
            repo_source=repo_source,
            caller_home=caller_home,
        )
        probe_home = work_dir / "home"
        probe_repo = work_dir / "repo"
        control_home = control_root / "home"
        fake_bin = control_root / "fake-bin"
        hosts_file = control_root / "hosts"
        guard = control_root / "probe-command-guard.py"
        wrapper = control_root / "probe-hook-wrapper.py"
        probe_home.mkdir(mode=0o700)
        control_home.mkdir(mode=0o700)
        (control_root / "credential-mask/.claude").mkdir(
            parents=True, mode=0o700
        )
        (control_root / "credential-mask/.codex").mkdir(mode=0o700)
        (work_dir / "tmp").mkdir(mode=0o700)
        (work_dir / "gh-config").mkdir(mode=0o700)
        _create_probe_repo(probe_repo, probe_home)
        _make_runtime_targets(control_root)
        _make_fake_gh(fake_bin, probe_repo)
        _make_hosts_file(hosts_file)
        cli = _install(repo_source, control_home, probe_repo)
        _make_probe_guard(guard, probe_repo)
        _make_hook_wrapper(wrapper)
        _install_probe_guard(control_home, guard, wrapper)
        control_sha256 = _protected_digest((control_root,))
        try:
            codex_hooks = (control_home / ".codex/hooks.json").read_bytes()
        except OSError:
            raise ProbeFailure("SETUP_FAILED") from None
        with _sealed_memfd("pre-pr-tribunal-codex-hooks", codex_hooks) as hooks_fd:
            _verify_isolation(
                work_dir=work_dir,
                repo=probe_repo,
                home=probe_home,
                control_root=control_root,
                control_home=control_home,
                fake_bin=fake_bin,
                hosts_file=hosts_file,
                caller_home=caller_home,
            )
            selected = (
                ("claude", "codex")
                if arguments.runtime == "all"
                else (arguments.runtime,)
            )
            success = True
            for runtime in selected:
                executable = _resolve_runtime(runtime, caller_env)
                if executable is None:
                    runtime_report: dict[str, object] = {
                        "status": "RUNTIME_UNAVAILABLE"
                    }
                else:
                    credential_source: CredentialSnapshot | None = None
                    claude_token: str | None = None
                    try:
                        with ExitStack() as runtime_stack:
                            codex_auth_fd: int | None = None
                            if arguments.auth_source == "subscription":
                                if caller_home is None:
                                    raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
                                if runtime == "claude":
                                    claude_auth = _load_claude_subscription_auth(
                                        caller_home
                                    )
                                    credential_source = claude_auth.source
                                    claude_token = claude_auth.access_token
                                else:
                                    codex_auth = _load_codex_subscription_auth(
                                        caller_home
                                    )
                                    credential_source = codex_auth.source
                                    codex_auth_fd = runtime_stack.enter_context(
                                        _sealed_memfd(
                                            "pre-pr-tribunal-codex-auth",
                                            codex_auth.data,
                                        )
                                    )
                            runtime_report = _probe_runtime(
                                runtime,
                                executable=executable,
                                auth_source=arguments.auth_source,
                                claude_subscription_token=claude_token,
                                codex_auth_fd=codex_auth_fd,
                                codex_hooks_fd=hooks_fd,
                                caller_env=caller_env,
                                caller_home=caller_home,
                                work_dir=work_dir,
                                home=probe_home,
                                control_root=control_root,
                                control_home=control_home,
                                repo=probe_repo,
                                fake_bin=fake_bin,
                                hosts_file=hosts_file,
                                cli=cli,
                                control_sha256=control_sha256,
                            )
                            if credential_source is not None:
                                _verify_credential_source_unchanged(
                                    credential_source
                                )
                    except ProbeFailure as error:
                        runtime_report = {"status": error.code}
                report[runtime] = runtime_report
                runtime_passed = runtime_report.get("status") == "PASS"
                success = success and runtime_passed
                if (
                    arguments.runtime == "all"
                    and runtime == "claude"
                    and not runtime_passed
                ):
                    break
            report["status"] = "PASS" if success else "BLOCKED"
            exit_code = 0 if success else 1
    except KeyboardInterrupt:
        report = {"schema": SCHEMA_VERSION, "status": "RUNTIME_FAILED"}
        exit_code = 130
    except ProbeFailure as error:
        report = {"schema": SCHEMA_VERSION, "status": error.code}
        exit_code = (
            2
            if error.code in {"USAGE", "INVALID_REPO_SOURCE", "INVALID_WORK_DIR"}
            else 1
        )
    except Exception:
        report = {"schema": SCHEMA_VERSION, "status": "SETUP_FAILED"}
        exit_code = 1
    finally:
        control_cleanup_ok = _cleanup_owned_directory(
            control_root, control_identity
        )
        work_cleanup_ok = _cleanup_owned_directory(work_dir, identity)
        cleanup_ok = control_cleanup_ok and work_cleanup_ok
    if not cleanup_ok:
        report = {"schema": SCHEMA_VERSION, "status": "CLEANUP_FAILED"}
        exit_code = 1
    _emit(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
