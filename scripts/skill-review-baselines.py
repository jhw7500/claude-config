#!/usr/bin/env python3
"""Compare live skills with a frozen local origin/master tree, without fetching."""

import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import time


REF = "origin/master"
MAX_FILES = 512
MAX_BYTES = 16 * 1024 * 1024
MAX_GIT_OUTPUT = 1024 * 1024


class Unavailable(Exception):
    pass


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    process = None
    try:
        process = subprocess.Popen(
            ["/usr/bin/git", "--no-optional-locks", "--literal-pathspecs",
             "-c", "core.fsmonitor=false", "-C", str(root), *args],
            stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, bufsize=0,
        )
        deadline = time.monotonic() + 5
        output = bytearray()
        with selectors.DefaultSelector() as pending:
            os.set_blocking(process.stdout.fileno(), False)
            pending.register(process.stdout, selectors.EVENT_READ, "stdout")
            if data:
                os.set_blocking(process.stdin.fileno(), False)
                pending.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            elif process.stdin is not None:
                process.stdin.close()
            offset = 0
            while pending.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Unavailable("Git comparison timed out")
                for key, _event in pending.select(remaining):
                    if key.data == "stdin":
                        offset += os.write(key.fd, memoryview(data)[offset:offset + 65536])
                        if offset == len(data):
                            pending.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, min(65536, MAX_GIT_OUTPUT + 1 - len(output)))
                        if not chunk:
                            pending.unregister(key.fileobj)
                            key.fileobj.close()
                        else:
                            output.extend(chunk)
                            if len(output) > MAX_GIT_OUTPUT:
                                raise Unavailable("comparison inventory exceeds budget")
            process.wait(timeout=max(0, deadline - time.monotonic()))
        if process.returncode:
            raise Unavailable("Git comparison command failed")
        return bytes(output)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unavailable("Git comparison command unavailable") from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def excluded(path: Path) -> bool:
    return any(part in {".git", "__pycache__", ".DS_Store"} or part.endswith(".pyc")
               for part in path.parts)


def live_files(root: Path, repository: Path) -> dict[str, tuple[str, str]]:
    files = {}
    fingerprints = {}
    byte_count = 0
    deadline = time.monotonic() + 30

    def visit(path):
        nonlocal byte_count
        if time.monotonic() > deadline:
            raise Unavailable("comparison time budget exceeded")
        info = path.lstat()
        fingerprints[path] = identity(info)
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                if excluded(child.relative_to(root)):
                    continue
                visit(child)
            return
        if len(files) >= MAX_FILES:
            raise Unavailable("comparison file budget exceeded")
        if stat.S_ISLNK(info.st_mode):
            mode, raw = "120000", os.fsencode(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            if info.st_size > MAX_BYTES - byte_count:
                raise Unavailable("comparison byte budget exceeded")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                if identity(os.fstat(stream.fileno())) != identity(info):
                    raise Unavailable("live skill changed during comparison")
                raw = stream.read(MAX_BYTES - byte_count + 1)
                if identity(os.fstat(stream.fileno())) != identity(info):
                    raise Unavailable("live skill changed during comparison")
            mode = "100755" if info.st_mode & 0o111 else "100644"
        else:
            raise Unavailable("live skill contains an unsupported file type")
        byte_count += len(raw)
        if byte_count > MAX_BYTES:
            raise Unavailable("comparison byte budget exceeded")
        oid = git(repository, "hash-object", "--stdin", "--no-filters", data=raw).decode("ascii").strip()
        files[path.relative_to(root).as_posix()] = (mode, oid)

    visit(root)
    if any(identity(path.lstat()) != before for path, before in fingerprints.items()):
        raise Unavailable("live skill changed during comparison")
    return files


def compare(root: Path) -> dict:
    result = {"ref": REF, "status": "unavailable"}
    try:
        root = root.resolve(strict=True)
        if not root.is_dir() or not (root / "SKILL.md").is_file():
            raise Unavailable("live SKILL.md unavailable")
        try:
            repository = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).rstrip("\n"))
        except Unavailable as exc:
            raise Unavailable("live skill is not in a readable Git worktree") from exc
        relative = root.relative_to(repository)
        result["path"] = relative.as_posix()
        # Refuse lazy object fetching before resolving or traversing any objects.
        keys = git(repository, "config", "--includes", "--name-only", "--list").lower().splitlines()
        if any(key == b"extensions.partialclone" or key.endswith(b".promisor") for key in keys):
            raise Unavailable("partial or promisor repositories require explicit comparison")
        try:
            commit = git(repository, "rev-parse", "--verify", "refs/remotes/origin/master^{commit}").decode("ascii").strip()
        except Unavailable as exc:
            raise Unavailable("local origin/master commit unavailable") from exc
        result["commit"] = commit
        prefix = "" if relative == Path(".") else relative.as_posix() + "/"
        tree = git(repository, "ls-tree", "-r", "-z", "--full-tree", commit, "--", relative.as_posix())
        canonical = {}
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            path = os.fsdecode(name)
            if path.startswith(prefix) and excluded(Path(path[len(prefix):])):
                continue
            if not path.startswith(prefix) or mode not in {"100644", "100755", "120000"} or kind != "blob":
                raise Unavailable("unsupported canonical skill entry")
            canonical[path[len(prefix):]] = (mode, oid)
            if len(canonical) > MAX_FILES:
                raise Unavailable("comparison file budget exceeded")
        if canonical.get("SKILL.md", (None,))[0] not in {"100644", "100755"}:
            raise Unavailable("regular SKILL.md absent from origin/master")
        live = live_files(root, repository)
        result.update(
            changed=sorted(name for name in live.keys() & canonical.keys() if live[name] != canonical[name]),
            live_only=sorted(live.keys() - canonical.keys()),
            origin_only=sorted(canonical.keys() - live.keys()),
        )
        result["status"] = "diverged" if any(result[key] for key in ("changed", "live_only", "origin_only")) else "match"
    except (OSError, ValueError, RuntimeError, Unavailable) as exc:
        result["status"] = "unavailable"
        result["reason"] = str(exc) if isinstance(exc, Unavailable) else "live skill comparison unavailable"
    return result


def unsafe(value: str) -> bool:
    return any(ord(char) < 32 or char in "\x7f\u2028\u2029" for char in value)


def records(values: list[str]) -> list[dict]:
    if len(values) % 3 or any(values[i] not in {"resolved", "unresolved", "unsafe"} for i in range(2, len(values), 3)):
        raise ValueError("expected name/path/resolution triples")
    result = []
    for offset in range(0, len(values), 3):
        name, path, status = values[offset:offset + 3]
        if status == "unsafe" or unsafe(name) or unsafe(path):
            result.append({"status": "hold", "reason": "unsafe live skill path"})
        elif status == "unresolved":
            result.append({"name": name, "status": "hold", "reason": "live target resolution failed"})
        else:
            comparison = compare(Path(path))
            record = {"name": name, "path": path, "comparison": comparison}
            if comparison["status"] != "match":
                record.update(status="hold", reason="live skill differs from origin/master" if comparison["status"] == "diverged" else comparison["reason"])
            result.append(record)
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(records(sys.argv[1:]), ensure_ascii=True))
    except ValueError as exc:
        sys.exit(str(exc))
