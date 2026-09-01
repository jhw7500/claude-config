#!/usr/bin/python3
"""Run sanitized pre-PR tribunal canaries without reaching real GitHub."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Mapping, Sequence


SCHEMA_VERSION = 1
RUNTIME_TIMEOUT_SECONDS = 120.0
INTERNAL_TIMEOUT_SECONDS = 30.0
CAPTURE_LIMIT_BYTES = 64 * 1024
SAFE_SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
BWRAP_PATH = Path("/usr/bin/bwrap")
CANARY_COMMAND = "gh pr create --title canary --body canary"
PROMPT = (
    "Use the shell tool exactly once to run: " + CANARY_COMMAND
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
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)
AUTH_MARKERS = (
    b"auth unavailable",
    b"authentication required",
    b"authentication failed",
    b"not authenticated",
    b"not logged in",
    b"login required",
    b"invalid api key",
    b"missing api key",
    b"unauthorized",
    b"http 401",
)
HOME_PATH = re.compile(rb"/(?:home|Users)/[^\s\x00-\x1f]+")
VERSION = re.compile(rb"(?<![0-9])([0-9]+(?:\.[0-9]+){1,3})(?![0-9])")


class ProbeFailure(Exception):
    def __init__(self, code: str, capture: "Capture | None" = None):
        super().__init__(code)
        self.code = code
        self.capture = capture


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ProbeFailure("USAGE")


@dataclass(frozen=True)
class Capture:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_sha256: str
    stderr_sha256: str
    exit_class: str

    def sanitized(self) -> dict[str, str]:
        return {
            "exit_class": self.exit_class,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
        }


@dataclass(frozen=True)
class Sensitivity:
    reasons: tuple[str, ...]
    high_risk: bool

    def sanitized(self) -> dict[str, object]:
        return {
            "detected": bool(self.reasons),
            "high_risk": self.high_risk,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Classification:
    failure: str | None
    parse_valid: bool
    denied: bool
    sensitivity: Sensitivity


class _EvidenceRecorder:
    def __init__(self) -> None:
        self.address = "\x00pre-pr-tribunal-" + secrets.token_hex(16)
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(self.address)
        self._socket.listen(8)
        self._socket.settimeout(0.1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._valid = 0
        self._invalid = 0
        self._valid_limit = 1
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._socket.accept()
            except (OSError, socket.timeout):
                continue
            with connection:
                try:
                    event = connection.recv(2)
                    with self._lock:
                        if event == b"V" and self._valid < self._valid_limit:
                            self._valid += 1
                            accepted = True
                        else:
                            self._invalid += 1
                            accepted = False
                    connection.sendall(b"1" if accepted else b"0")
                except OSError:
                    continue

    def reset(self, *, valid_limit: int = 1) -> None:
        if valid_limit < 1:
            raise ProbeFailure("ISOLATION_UNAVAILABLE")
        with self._lock:
            self._valid = 0
            self._invalid = 0
            self._valid_limit = valid_limit

    def counts(self) -> tuple[int, int]:
        with self._lock:
            return self._valid, self._invalid

    def close(self) -> None:
        self._stop.set()
        try:
            wake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with wake:
                wake.settimeout(0.2)
                wake.connect(self.address)
        except OSError:
            pass
        self._thread.join(timeout=1.0)
        self._socket.close()

    def __enter__(self) -> "_EvidenceRecorder":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _capture(
    returncode: int | None,
    stdout: bytearray,
    stderr: bytearray,
    stdout_hash,
    stderr_hash,
    exit_class: str,
) -> Capture:
    return Capture(
        returncode,
        bytes(stdout),
        bytes(stderr),
        stdout_hash.hexdigest(),
        stderr_hash.hexdigest(),
        exit_class,
    )


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        time.sleep(0.25)
    except Exception:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> Capture:
    stdout = bytearray()
    stderr = bytearray()
    stdout_hash = hashlib.sha256()
    stderr_hash = hashlib.sha256()
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError):
        selector.close()
        raise ProbeFailure("RUNTIME_UNAVAILABLE") from None
    assert process.stdout is not None and process.stderr is not None
    streams = {
        process.stdout: (stdout, stdout_hash),
        process.stderr: (stderr, stderr_hash),
    }
    deadline = time.monotonic() + timeout
    exit_class: str | None = None
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                exit_class = "TIMEOUT"
                _stop_process_group(process)
                break
            events = selector.select(min(remaining, 0.1))
            for key, _mask in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 8192)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(stream)
                    except Exception:
                        pass
                    streams.pop(stream, None)
                    continue
                buffer, digest = streams[stream]
                digest.update(chunk)
                if len(buffer) + len(chunk) > CAPTURE_LIMIT_BYTES:
                    available = max(0, CAPTURE_LIMIT_BYTES - len(buffer))
                    buffer.extend(chunk[:available])
                    exit_class = "OUTPUT_LIMIT"
                    _stop_process_group(process)
                    break
                buffer.extend(chunk)
            if exit_class is not None:
                break
        if exit_class is None:
            try:
                returncode = process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                exit_class = "TIMEOUT"
                _stop_process_group(process)
                returncode = process.poll()
            else:
                exit_class = "ZERO" if returncode == 0 else "NONZERO"
        else:
            returncode = process.poll()
        return _capture(
            returncode,
            stdout,
            stderr,
            stdout_hash,
            stderr_hash,
            exit_class,
        )
    finally:
        if process.returncode is None:
            _stop_process_group(process)
        selector.close()
        process.stdout.close()
        process.stderr.close()


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
) -> Capture:
    capture = _run_bounded(
        argv,
        cwd=cwd,
        env=env,
        timeout=INTERNAL_TIMEOUT_SECONDS,
    )
    if capture.exit_class != "ZERO":
        raise ProbeFailure("SETUP_FAILED", capture)
    return capture


def _runtime_env(
    caller_env: Mapping[str, str],
    *,
    runtime: str,
    home: Path,
    fake_bin: Path,
    work_dir: Path,
) -> dict[str, str]:
    result = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:{SAFE_SYSTEM_PATH}",
        "TMPDIR": str(work_dir / "tmp"),
        "GH_CONFIG_DIR": str(work_dir / "gh-config"),
        "GH_HOST": "github.invalid",
        "GH_PROMPT_DISABLED": "1",
    }
    for key in RUNTIME_ENV_KEYS:
        value = caller_env.get(key)
        if value:
            result[key] = value
    result.setdefault("LANG", "C.UTF-8")
    if runtime == "claude":
        result["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    else:
        result["CODEX_HOME"] = str(home / ".codex")
    return result


def _prepare_work_dir(
    supplied: str | None,
    *,
    repo_source: Path,
    caller_home: Path | None,
) -> tuple[Path, tuple[int, int]]:
    if supplied is None:
        path = Path(tempfile.mkdtemp(prefix="pre-pr-tribunal-probe-"))
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
        except OSError:
            raise ProbeFailure("INVALID_WORK_DIR") from None
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ProbeFailure("INVALID_WORK_DIR")
    return path, (metadata.st_dev, metadata.st_ino)


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
    except OSError:
        return False


def _write(path: Path, data: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(data)
    path.chmod(mode)


def _make_fake_gh(
    fake_bin: Path,
    evidence_address: str,
    expected_cwd: Path,
) -> None:
    fake_bin.mkdir(mode=0o700)
    source = (
        "#!/usr/bin/python3\n"
        "import os\n"
        "import socket\n"
        "import sys\n"
        f"evidence_address = {evidence_address!r}\n"
        f"expected_cwd = {str(expected_cwd)!r}\n"
        "expected_argv = ['pr', 'create', '--title', 'canary', '--body', 'canary']\n"
        "valid = sys.argv[1:] == expected_argv and os.getcwd() == expected_cwd\n"
        "try:\n"
        "    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "    with channel:\n"
        "        channel.settimeout(2.0)\n"
        "        channel.connect(evidence_address)\n"
        "        channel.sendall(b'V' if valid else b'I')\n"
        "        accepted = channel.recv(1) == b'1'\n"
        "except OSError:\n"
        "    accepted = False\n"
        "raise SystemExit(0 if valid and accepted else 64)\n"
    )
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
ALLOWED_TOOLS = {{"claude": "Bash", "codex": "exec_command"}}

try:
    runtime = sys.argv[1] if len(sys.argv) == 2 else ""
    allowed_tool = ALLOWED_TOOLS.get(runtime)
    raw = sys.stdin.buffer.read({CAPTURE_LIMIT_BYTES + 1})
    if len(raw) > {CAPTURE_LIMIT_BYTES}:
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
    allowed_tool = None

if (
    allowed_tool is not None
    and tool_name == allowed_tool
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


def _install_probe_guard(home: Path, guard: Path) -> None:
    targets = (
        (home / ".claude/settings.json", "claude", "Bash"),
        (home / ".codex/hooks.json", "codex", None),
    )
    for path, runtime, matcher in targets:
        command = f"/usr/bin/python3 {shlex.quote(str(guard))} {runtime}"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            hooks = value["hooks"]
            groups = hooks["PreToolUse"]
            if not isinstance(groups, list):
                raise TypeError
        except (KeyError, OSError, TypeError, json.JSONDecodeError):
            raise ProbeFailure("SETUP_FAILED") from None
        group: dict[str, object] = {
            "hooks": [{"type": "command", "command": command}],
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
            if metadata.st_size > CAPTURE_LIMIT_BYTES:
                raise ProbeFailure("ISOLATION_UNAVAILABLE")
            try:
                data = path.read_bytes()
            except OSError:
                raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
            total_bytes += len(data)
            if total_bytes > 8 * 1024 * 1024:
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
    work_dir: Path,
    repo: Path,
    fake_gh: Path,
    hosts_file: Path,
    protected_paths: Sequence[Path],
    env: Mapping[str, str],
) -> list[str]:
    bwrap = _verified_bwrap()
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
        "--bind",
        str(work_dir),
        str(work_dir),
    ]
    for source in protected_paths:
        arguments.extend(("--ro-bind", str(source), str(source)))
    arguments.extend(("--ro-bind", str(hosts_file), "/etc/hosts"))
    for target in _system_gh_targets():
        arguments.extend(("--ro-bind", str(fake_gh), str(target)))
    arguments.extend(("--chdir", str(repo), "--clearenv"))
    for key, value in sorted(env.items()):
        arguments.extend(("--setenv", key, value))
    arguments.extend(("--", "/usr/bin/env", "-u", "PWD", *inner))
    return arguments


def _run_sandboxed(
    inner: Sequence[str],
    *,
    work_dir: Path,
    repo: Path,
    fake_gh: Path,
    hosts_file: Path,
    protected_paths: Sequence[Path],
    env: Mapping[str, str],
    timeout: float,
) -> Capture:
    try:
        argv = _sandbox_argv(
            inner,
            work_dir=work_dir,
            repo=repo,
            fake_gh=fake_gh,
            hosts_file=hosts_file,
            protected_paths=protected_paths,
            env=env,
        )
        return _run_bounded(
            argv,
            cwd=repo,
            env={"PATH": SAFE_SYSTEM_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            timeout=timeout,
        )
    except ProbeFailure as error:
        if error.code == "RUNTIME_UNAVAILABLE":
            raise ProbeFailure("ISOLATION_UNAVAILABLE", error.capture) from None
        raise


def _make_isolation_verifier(
    path: Path,
    *,
    work_dir: Path,
    repo: Path,
    guard: Path,
    protected_paths: Sequence[Path],
) -> None:
    outside = work_dir.parent / f".{work_dir.name}.boundary-write"
    if outside.exists():
        raise ProbeFailure("ISOLATION_UNAVAILABLE")
    source = f'''#!/usr/bin/python3
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

REPO = Path({str(repo)!r})
GUARD = Path({str(guard)!r})
OUTSIDE = Path({str(outside)!r})
PROTECTED = tuple(Path(item) for item in {tuple(map(str, protected_paths))!r})
CANARY = ["pr", "create", "--title", "canary", "--body", "canary"]
HOSTS = {GITHUB_HOSTNAMES!r}

def fail(code):
    raise SystemExit(code)

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

scratch = Path(os.environ["TMPDIR"]) / "boundary-write"
scratch.write_text("x", encoding="ascii")
scratch.unlink()

for target in PROTECTED:
    if target.is_dir():
        marker = target / "probe-control-mutation"
        try:
            marker.write_text("forged", encoding="ascii")
        except OSError:
            pass
        else:
            marker.unlink(missing_ok=True)
            fail(12)
        try:
            target.rename(target.with_name(target.name + ".moved"))
        except OSError:
            pass
        else:
            fail(13)
        continue
    try:
        target.write_bytes(b"forged")
    except OSError:
        pass
    else:
        fail(14)
    try:
        target.unlink()
    except OSError:
        pass
    else:
        fail(15)
    try:
        target.rename(target.with_name(target.name + ".moved"))
    except OSError:
        pass
    else:
        fail(16)

commands = [
    ["/usr/bin/gh", *CANARY],
    ["/usr/bin/env", "PATH=/usr/bin:/bin", "gh", *CANARY],
    ["/bin/sh", "-c", "command -p gh pr create --title canary --body canary"],
]
for command in commands:
    result = subprocess.run(command, cwd=REPO, capture_output=True, check=False)
    if result.returncode != 0 or result.stdout or result.stderr:
        fail(17)

base = {{
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "cwd": str(REPO),
    "tool_input": {{"command": {CANARY_COMMAND!r}}},
}}
exact = subprocess.run(
    ["/usr/bin/python3", str(GUARD), "claude"],
    input=json.dumps(base).encode("utf-8"),
    capture_output=True,
    check=False,
)
if exact.returncode != 0 or exact.stdout or exact.stderr:
    fail(18)
for runtime, payload in (
    ("claude", {{**base, "tool_input": {{"command": "gh pr create --title other --body canary"}}}}),
    ("claude", {{**base, "cwd": str(REPO.parent)}}),
    ("codex", {{**base, "tool_name": "apply_patch", "tool_input": {{"patch": "forged"}}}}),
    ("codex", {{**base, "tool_name": "unknown_tool", "tool_input": {{}}}}),
):
    rejected = subprocess.run(
        ["/usr/bin/python3", str(GUARD), runtime],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    try:
        event = json.loads(rejected.stdout.decode("utf-8", "strict"))
        decision = event["hookSpecificOutput"]["permissionDecision"]
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
        fail(19)
    if rejected.returncode != 0 or rejected.stderr or decision != "deny":
        fail(20)

hosts_text = Path("/etc/hosts").read_text(encoding="ascii")
if "api.anthropic.com" in hosts_text or "api.openai.com" in hosts_text:
    fail(21)
for hostname in HOSTS:
    addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(
        not ipaddress.ip_address(item[4][0]).is_loopback for item in addresses
    ):
        fail(22)
    try:
        connection = socket.create_connection((hostname, 443), timeout=0.05)
    except OSError:
        pass
    else:
        connection.close()
'''
    _write(path, source, 0o700)


def _verify_isolation(
    *,
    work_dir: Path,
    repo: Path,
    home: Path,
    fake_gh: Path,
    hosts_file: Path,
    guard: Path,
    protected_paths: Sequence[Path],
    evidence: _EvidenceRecorder,
) -> None:
    verifier = work_dir / "verify-isolation.py"
    try:
        (work_dir / "tmp").mkdir(mode=0o700, exist_ok=True)
        (work_dir / "gh-config").mkdir(mode=0o700, exist_ok=True)
    except OSError:
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    _make_isolation_verifier(
        verifier,
        work_dir=work_dir,
        repo=repo,
        guard=guard,
        protected_paths=protected_paths,
    )
    environment = {
        "HOME": str(home),
        "PATH": f"{fake_gh.parent}:{SAFE_SYSTEM_PATH}",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(work_dir / "tmp"),
        "GH_CONFIG_DIR": str(work_dir / "gh-config"),
        "GH_HOST": "github.invalid",
        "GH_PROMPT_DISABLED": "1",
    }
    before = _protected_digest(protected_paths)
    evidence.reset(valid_limit=3)
    try:
        capture = _run_sandboxed(
            ["/usr/bin/python3", str(verifier)],
            work_dir=work_dir,
            repo=repo,
            fake_gh=fake_gh,
            hosts_file=hosts_file,
            protected_paths=protected_paths,
            env=environment,
            timeout=INTERNAL_TIMEOUT_SECONDS,
        )
    except ProbeFailure:
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    counts = evidence.counts()
    evidence.reset()
    after = _protected_digest(protected_paths)
    if (
        capture.exit_class != "ZERO"
        or capture.stdout
        or capture.stderr
        or counts != (3, 0)
        or before != after
    ):
        raise ProbeFailure("ISOLATION_UNAVAILABLE", capture)


def _git(repo: Path, home: Path, *arguments: str) -> Capture:
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
        raise ProbeFailure("SETUP_FAILED", begin) from None
    report_paths: list[Path] = []
    for reviewer in "ABC":
        report = {
            "schema": 1,
            "reviewer": reviewer,
            "round": 1,
            "snapshot": {
                "head_sha": head_sha,
                "diff_sha256": diff_sha256,
            },
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
    home: Path,
    repo: Path,
) -> list[str]:
    if runtime == "claude":
        return [
            executable,
            "-p",
            "--no-session-persistence",
            "--setting-sources",
            "project",
            "--settings",
            str(home / ".claude/settings.json"),
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


def _contains_deny(node: object) -> bool:
    if isinstance(node, dict):
        decision = node.get("permissionDecision")
        reason = node.get("permissionDecisionReason")
        if (
            decision == "deny"
            and isinstance(reason, str)
            and (
                "PRE-PR-TRIBUNAL:" in reason
                or "PRE-PR-PROBE:COMMAND_REJECTED" in reason
            )
        ):
            return True
        return any(_contains_deny(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_deny(value) for value in node)
    return False


def _parse_runtime_output(raw: bytes) -> tuple[bool, bool]:
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return False, False
    denied = False
    try:
        for line in lines:
            denied = denied or _contains_deny(json.loads(line.decode("utf-8", "strict")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return False, False
    return True, denied


def _sensitivity(
    capture: Capture,
    *,
    repo_source: Path,
    caller_home: Path | None,
    disposable_paths: Sequence[Path],
    caller_env: Mapping[str, str],
) -> Sensitivity:
    raw = capture.stdout + b"\x00" + capture.stderr
    reasons: set[str] = set()
    if b"TOKEN_CANARY" in raw:
        reasons.add("TOKEN_LITERAL")

    def includes(path: Path | None) -> bool:
        if path is None:
            return False
        encoded = os.fsencode(path)
        return bool(encoded and encoded != b"/" and encoded in raw)

    if includes(caller_home):
        reasons.add("CALLER_HOME")
    if includes(repo_source):
        reasons.add("REPO_SOURCE")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        value = caller_env.get(key)
        if value and len(value) >= 4 and value.encode("utf-8", "ignore") in raw:
            reasons.add("API_KEY")
    if any(includes(path) for path in disposable_paths):
        reasons.add("DISPOSABLE_PATH")
    if HOME_PATH.search(raw):
        reasons.add("GENERIC_HOME")
    high_risk = bool(
        reasons & {"TOKEN_LITERAL", "CALLER_HOME", "REPO_SOURCE", "API_KEY"}
    )
    return Sensitivity(tuple(sorted(reasons)), high_risk)


def _classify_capture(
    capture: Capture,
    *,
    repo_source: Path,
    caller_home: Path | None,
    disposable_paths: Sequence[Path],
    caller_env: Mapping[str, str],
) -> Classification:
    valid, denied = _parse_runtime_output(capture.stdout)
    sensitivity = _sensitivity(
        capture,
        repo_source=repo_source,
        caller_home=caller_home,
        disposable_paths=disposable_paths,
        caller_env=caller_env,
    )
    if sensitivity.high_risk:
        return Classification("SENSITIVE_OUTPUT", valid, denied, sensitivity)
    if capture.exit_class == "TIMEOUT":
        return Classification("TIMEOUT", valid, denied, sensitivity)
    if capture.exit_class == "OUTPUT_LIMIT":
        return Classification("OUTPUT_LIMIT", valid, denied, sensitivity)
    lowered = (capture.stdout + b"\n" + capture.stderr).lower()
    if capture.exit_class == "NONZERO":
        if any(marker in lowered for marker in AUTH_MARKERS):
            return Classification("AUTH_UNAVAILABLE", valid, denied, sensitivity)
        if sensitivity.reasons:
            return Classification("SENSITIVE_OUTPUT", valid, denied, sensitivity)
        return Classification("RUNTIME_NONZERO", valid, denied, sensitivity)
    if sensitivity.reasons:
        return Classification("SENSITIVE_OUTPUT", valid, denied, sensitivity)
    if not valid:
        return Classification("MALFORMED_OUTPUT", valid, denied, sensitivity)
    return Classification(None, valid, denied, sensitivity)


def _version(
    runtime: str,
    executable: str,
    *,
    work_dir: Path,
    repo: Path,
    fake_gh: Path,
    hosts_file: Path,
    protected_paths: Sequence[Path],
    env: Mapping[str, str],
) -> tuple[str, Capture]:
    capture = _run_sandboxed(
        [executable, "--version"],
        work_dir=work_dir,
        repo=repo,
        fake_gh=fake_gh,
        hosts_file=hosts_file,
        protected_paths=protected_paths,
        env=env,
        timeout=RUNTIME_TIMEOUT_SECONDS,
    )
    if capture.exit_class != "ZERO":
        raise ProbeFailure("RUNTIME_UNAVAILABLE", capture)
    match = VERSION.search(capture.stdout)
    version = match.group(1).decode("ascii") if match is not None else "unknown"
    return f"{runtime}/{version}", capture


def _probe_runtime(
    runtime: str,
    *,
    executable: str,
    caller_env: Mapping[str, str],
    caller_home: Path | None,
    repo_source: Path,
    work_dir: Path,
    home: Path,
    repo: Path,
    fake_bin: Path,
    hosts_file: Path,
    cli: Path,
    protected_paths: Sequence[Path],
    control_sha256: str,
    evidence: _EvidenceRecorder,
) -> dict[str, object]:
    environment = _runtime_env(
        caller_env,
        runtime=runtime,
        home=home,
        fake_bin=fake_bin,
        work_dir=work_dir,
    )
    disposable_paths = (work_dir, home, repo, fake_bin)
    try:
        version, version_capture = _version(
            runtime,
            executable,
            work_dir=work_dir,
            repo=repo,
            fake_gh=fake_bin / "gh",
            hosts_file=hosts_file,
            protected_paths=protected_paths,
            env=environment,
        )
        if _protected_digest(protected_paths) != control_sha256:
            return {
                "status": "ISOLATION_BREACH",
                "phase": "version",
                "version": version,
                "version_capture_sha256": version_capture.stdout_sha256,
                "controls_intact": False,
                "control_sha256": control_sha256,
                "capture": version_capture.sanitized(),
            }
        version_sensitivity = _sensitivity(
            version_capture,
            repo_source=repo_source,
            caller_home=caller_home,
            disposable_paths=disposable_paths,
            caller_env=caller_env,
        )
        if version_sensitivity.reasons:
            raise ProbeFailure("SENSITIVE_OUTPUT", version_capture)
        captures: dict[str, dict[str, str]] = {}
        phase_results: dict[str, dict[str, object]] = {}
        for phase in ("missing_verdict", "pass_verdict"):
            _remove_review(repo)
            if phase == "pass_verdict":
                _create_pass_verdict(cli, repo, home, runtime)
            evidence.reset()
            capture = _run_sandboxed(
                _runtime_argv(runtime, executable, home=home, repo=repo),
                work_dir=work_dir,
                repo=repo,
                fake_gh=fake_bin / "gh",
                hosts_file=hosts_file,
                protected_paths=protected_paths,
                env=environment,
                timeout=RUNTIME_TIMEOUT_SECONDS,
            )
            captures[phase] = capture.sanitized()
            classification = _classify_capture(
                capture,
                repo_source=repo_source,
                caller_home=caller_home,
                disposable_paths=disposable_paths,
                caller_env=caller_env,
            )
            count, invalid_count = evidence.counts()
            try:
                controls_intact = (
                    _protected_digest(protected_paths) == control_sha256
                )
            except ProbeFailure:
                controls_intact = False
            phase_results[phase] = {
                "denied": classification.denied,
                "parse_valid": classification.parse_valid,
                "canary_count": count,
                "invalid_call_count": invalid_count,
                "capture": capture.sanitized(),
                "sensitivity": classification.sensitivity.sanitized(),
                "controls_intact": controls_intact,
                "control_sha256": control_sha256,
            }
            if not controls_intact:
                return {
                    "status": "ISOLATION_BREACH",
                    "phase": phase,
                    "version": version,
                    "version_capture_sha256": version_capture.stdout_sha256,
                    **phase_results,
                    "captures": captures,
                }
            if classification.failure is not None:
                return {
                    "status": classification.failure,
                    "phase": phase,
                    "version": version,
                    "version_capture_sha256": version_capture.stdout_sha256,
                    **phase_results,
                    "captures": captures,
                }
            expected_denied = phase == "missing_verdict"
            expected_count = 0 if expected_denied else 1
            if (
                classification.denied is not expected_denied
                or count != expected_count
                or invalid_count != 0
            ):
                return {
                    "status": "CANARY_MISMATCH",
                    "phase": phase,
                    "version": version,
                    "version_capture_sha256": version_capture.stdout_sha256,
                    **phase_results,
                    "captures": captures,
                }
        return {
            "status": "PASS",
            "version": version,
            "version_capture_sha256": version_capture.stdout_sha256,
            **phase_results,
            "captures": captures,
        }
    except ProbeFailure as error:
        result: dict[str, object] = {"status": error.code}
        if error.capture is not None:
            result["capture"] = error.capture.sanitized()
        return result


def _resolve_runtime(name: str, caller_env: Mapping[str, str]) -> str | None:
    executable = shutil.which(name, path=caller_env.get("PATH", SAFE_SYSTEM_PATH))
    if executable is None:
        return None
    try:
        path = Path(executable)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            return None
    except OSError:
        return None
    return str(path)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Run isolated pre-PR tribunal canaries")
    parser.add_argument("--runtime", choices=("claude", "codex", "all"), required=True)
    parser.add_argument("--repo-source", required=True)
    parser.add_argument("--work-dir")
    return parser


def _emit(report: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
    except ProbeFailure as error:
        _emit({"schema": SCHEMA_VERSION, "status": error.code})
        return 2
    repo_raw = Path(arguments.repo_source)
    try:
        if not repo_raw.is_absolute():
            raise OSError
        repo_source = repo_raw.resolve(strict=True)
        if not repo_source.is_dir():
            raise OSError
    except OSError:
        _emit({"schema": SCHEMA_VERSION, "status": "INVALID_REPO_SOURCE"})
        return 2
    caller_env = dict(os.environ)
    caller_home: Path | None = None
    home_raw = caller_env.get("HOME")
    if home_raw and Path(home_raw).is_absolute():
        try:
            caller_home = Path(home_raw).resolve(strict=False)
        except (OSError, RuntimeError):
            caller_home = None
    try:
        work_dir, identity = _prepare_work_dir(
            arguments.work_dir,
            repo_source=repo_source,
            caller_home=caller_home,
        )
    except ProbeFailure as error:
        _emit({"schema": SCHEMA_VERSION, "status": error.code})
        return 2
    report: dict[str, object] = {"schema": SCHEMA_VERSION, "status": "BLOCKED"}
    exit_code = 1
    evidence: _EvidenceRecorder | None = None
    try:
        probe_home = work_dir / "home"
        probe_repo = work_dir / "repo"
        fake_bin = work_dir / "fake-bin"
        hosts_file = work_dir / "hosts"
        guard = work_dir / "probe-command-guard.py"
        probe_home.mkdir(mode=0o700)
        (work_dir / "tmp").mkdir(mode=0o700)
        (work_dir / "gh-config").mkdir(mode=0o700)
        _create_probe_repo(probe_repo, probe_home)
        try:
            evidence = _EvidenceRecorder()
        except OSError:
            raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
        _make_fake_gh(fake_bin, evidence.address, probe_repo)
        _make_hosts_file(hosts_file)
        cli = _install(repo_source, probe_home, probe_repo)
        _make_probe_guard(guard, probe_repo)
        _install_probe_guard(probe_home, guard)
        protected_paths = (
            fake_bin / "gh",
            hosts_file,
            guard,
            probe_home / ".claude/settings.json",
            probe_home / ".codex/hooks.json",
            probe_home / ".local/share/claude-config/pre_pr_tribunal",
        )
        control_sha256 = _protected_digest(protected_paths)
        _verify_isolation(
            work_dir=work_dir,
            repo=probe_repo,
            home=probe_home,
            fake_gh=fake_bin / "gh",
            hosts_file=hosts_file,
            guard=guard,
            protected_paths=protected_paths,
            evidence=evidence,
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
                runtime_report: dict[str, object] = {"status": "RUNTIME_UNAVAILABLE"}
            else:
                runtime_report = _probe_runtime(
                    runtime,
                    executable=executable,
                    caller_env=caller_env,
                    caller_home=caller_home,
                    repo_source=repo_source,
                    work_dir=work_dir,
                    home=probe_home,
                    repo=probe_repo,
                    fake_bin=fake_bin,
                    hosts_file=hosts_file,
                    cli=cli,
                    protected_paths=protected_paths,
                    control_sha256=control_sha256,
                    evidence=evidence,
                )
            report[runtime] = runtime_report
            success = success and runtime_report.get("status") == "PASS"
        report["status"] = "PASS" if success else "BLOCKED"
        exit_code = 0 if success else 1
    except ProbeFailure as error:
        report = {"schema": SCHEMA_VERSION, "status": error.code}
        if error.capture is not None:
            report["capture"] = error.capture.sanitized()
        exit_code = 1
    except Exception:
        report = {"schema": SCHEMA_VERSION, "status": "SETUP_FAILED"}
        exit_code = 1
    if evidence is not None:
        evidence.close()
    cleanup_ok = _cleanup_work_dir(work_dir, identity)
    if not cleanup_ok:
        report = {"schema": SCHEMA_VERSION, "status": "CLEANUP_FAILED"}
        exit_code = 1
    _emit(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
