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
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Mapping, Sequence


SCHEMA_VERSION = 1
RUNTIME_TIMEOUT_SECONDS = 120.0
INTERNAL_TIMEOUT_SECONDS = 30.0
CAPTURE_LIMIT_BYTES = 64 * 1024
SAFE_SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
PROMPT = (
    "Use the shell tool exactly once to run: "
    "gh pr create --title canary --body canary"
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
) -> dict[str, str]:
    result = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:{SAFE_SYSTEM_PATH}",
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


def _make_fake_gh(fake_bin: Path, count_file: Path) -> None:
    fake_bin.mkdir(mode=0o700)
    source = (
        "#!/usr/bin/python3\n"
        "from pathlib import Path\n"
        f"target = Path({str(count_file)!r})\n"
        "with target.open('a', encoding='ascii') as handle:\n"
        "    handle.write('1\\n')\n"
    )
    _write(fake_bin / "gh", source, 0o700)


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
        if decision == "deny" and isinstance(reason, str) and "PRE-PR-TRIBUNAL:" in reason:
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


def _has_sensitive_output(
    capture: Capture,
    *,
    protected_paths: Sequence[Path],
    caller_env: Mapping[str, str],
) -> bool:
    raw = capture.stdout + b"\x00" + capture.stderr
    if b"TOKEN_CANARY" in raw or HOME_PATH.search(raw):
        return True
    for path in protected_paths:
        encoded = os.fsencode(path)
        if encoded and encoded != b"/" and encoded in raw:
            return True
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        value = caller_env.get(key)
        if value and len(value) >= 4 and value.encode("utf-8", "ignore") in raw:
            return True
    return False


def _classify_capture(
    capture: Capture,
    *,
    protected_paths: Sequence[Path],
    caller_env: Mapping[str, str],
) -> tuple[str | None, bool]:
    if capture.exit_class == "TIMEOUT":
        return "TIMEOUT", False
    if capture.exit_class == "OUTPUT_LIMIT":
        return "OUTPUT_LIMIT", False
    if _has_sensitive_output(
        capture, protected_paths=protected_paths, caller_env=caller_env
    ):
        return "SENSITIVE_OUTPUT", False
    lowered = (capture.stdout + b"\n" + capture.stderr).lower()
    if capture.exit_class == "NONZERO":
        if any(marker in lowered for marker in AUTH_MARKERS):
            return "AUTH_UNAVAILABLE", False
        return "RUNTIME_NONZERO", False
    valid, denied = _parse_runtime_output(capture.stdout)
    if not valid:
        return "MALFORMED_OUTPUT", False
    return None, denied


def _canary_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > CAPTURE_LIMIT_BYTES:
            raise ProbeFailure("CANARY_MISMATCH")
        values = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        raise ProbeFailure("CANARY_MISMATCH") from None
    if any(value != "1" for value in values):
        raise ProbeFailure("CANARY_MISMATCH")
    return len(values)


def _reset_canary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        raise ProbeFailure("CANARY_MISMATCH") from None


def _version(
    runtime: str,
    executable: str,
    *,
    repo: Path,
    env: Mapping[str, str],
) -> tuple[str, Capture]:
    capture = _run_bounded(
        [executable, "--version"],
        cwd=repo,
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
    repo_source: Path,
    home: Path,
    repo: Path,
    fake_bin: Path,
    count_file: Path,
    cli: Path,
) -> dict[str, object]:
    environment = _runtime_env(
        caller_env, runtime=runtime, home=home, fake_bin=fake_bin
    )
    protected_paths = (repo_source, home, repo, fake_bin, count_file.parent)
    try:
        version, version_capture = _version(
            runtime, executable, repo=repo, env=environment
        )
        if _has_sensitive_output(
            version_capture,
            protected_paths=protected_paths,
            caller_env=caller_env,
        ):
            raise ProbeFailure("SENSITIVE_OUTPUT", version_capture)
        captures: dict[str, dict[str, str]] = {}
        phase_results: dict[str, dict[str, object]] = {}
        for phase in ("missing_verdict", "pass_verdict"):
            _remove_review(repo)
            if phase == "pass_verdict":
                _create_pass_verdict(cli, repo, home, runtime)
            _reset_canary(count_file)
            capture = _run_bounded(
                _runtime_argv(runtime, executable, home=home, repo=repo),
                cwd=repo,
                env=environment,
                timeout=RUNTIME_TIMEOUT_SECONDS,
            )
            captures[phase] = capture.sanitized()
            failure, denied = _classify_capture(
                capture,
                protected_paths=protected_paths,
                caller_env=caller_env,
            )
            count = _canary_count(count_file)
            if failure is not None:
                return {
                    "status": failure,
                    "version": version,
                    "version_capture_sha256": version_capture.stdout_sha256,
                    "captures": captures,
                }
            expected_denied = phase == "missing_verdict"
            expected_count = 0 if expected_denied else 1
            phase_results[phase] = {"denied": denied, "canary_count": count}
            if denied is not expected_denied or count != expected_count:
                return {
                    "status": "CANARY_MISMATCH",
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
    try:
        probe_home = work_dir / "home"
        probe_repo = work_dir / "repo"
        fake_bin = work_dir / "fake-bin"
        count_file = work_dir / "canary-count"
        probe_home.mkdir(mode=0o700)
        _create_probe_repo(probe_repo, probe_home)
        _make_fake_gh(fake_bin, count_file)
        cli = _install(repo_source, probe_home, probe_repo)
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
                    repo_source=repo_source,
                    home=probe_home,
                    repo=probe_repo,
                    fake_bin=fake_bin,
                    count_file=count_file,
                    cli=cli,
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
    cleanup_ok = _cleanup_work_dir(work_dir, identity)
    if not cleanup_ok:
        report = {"schema": SCHEMA_VERSION, "status": "CLEANUP_FAILED"}
        exit_code = 1
    _emit(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
