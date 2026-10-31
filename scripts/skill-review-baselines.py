#!/usr/bin/env python3
"""Compare live skills with a frozen local origin/master tree, without fetching."""

import ctypes
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
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


def live_files(root: Path, repository: Path, snapshot=None) -> dict[str, tuple[str, str]]:
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
    if snapshot is not None:
        snapshot.update({path.relative_to(root).as_posix(): list(value)
                         for path, value in fingerprints.items()})
    return files


def compare(root: Path, snapshot=None) -> dict:
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
        fingerprints = {}
        live = live_files(root, repository, fingerprints)
        result.update(
            changed=sorted(name for name in live.keys() & canonical.keys() if live[name] != canonical[name]),
            live_only=sorted(live.keys() - canonical.keys()),
            origin_only=sorted(canonical.keys() - live.keys()),
        )
        result["status"] = "diverged" if any(result[key] for key in ("changed", "live_only", "origin_only")) else "match"
        if snapshot is not None and result["status"] == "match":
            snapshot.update(repository=str(repository), files=live, fingerprints=fingerprints)
    except (OSError, ValueError, RuntimeError, Unavailable) as exc:
        result["status"] = "unavailable"
        result["reason"] = str(exc) if isinstance(exc, Unavailable) else "live skill comparison unavailable"
    return result


def unsafe(value: str) -> bool:
    return any(ord(char) < 32 or char in "\x7f\u2028\u2029" for char in value)


def records(values: list[str], catalog=None, snapshots=None) -> list[dict]:
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
            snapshot = {}
            alias = catalog / name if catalog is not None else None
            try:
                before = identity(alias.lstat()) if alias is not None else None
                if alias is not None and alias.resolve(strict=True) != Path(path):
                    raise Unavailable("live alias changed during comparison")
                comparison = compare(Path(path), snapshot)
                if alias is not None and (identity(alias.lstat()) != before or
                                          alias.resolve(strict=True) != Path(path)):
                    raise Unavailable("live alias changed during comparison")
            except (OSError, RuntimeError, Unavailable):
                comparison = {"ref": REF, "status": "unavailable",
                              "reason": "live alias changed during comparison"}
            record = {"name": name, "path": path, "comparison": comparison}
            if comparison["status"] != "match":
                record.update(status="hold", reason="live skill differs from origin/master" if comparison["status"] == "diverged" else comparison["reason"])
            elif snapshots is not None:
                snapshots.append(dict(snapshot, name=name, root=path, alias=str(alias),
                                      alias_identity=list(before)))
            result.append(record)
    return result


def private_write(path: Path, data: bytes, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)


def owned_directory(path: Path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise Unavailable("publication directory is not an owned regular directory")
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid]


def prepare(snapshot_path: Path, catalog: Path, stage: Path, target: Path, values):
    if (stat.S_IMODE(owned_directory(snapshot_path.parent)[2]) != 0o700 or
            stat.S_IMODE(owned_directory(stage.parent)[2]) != 0o700):
        raise Unavailable("private staging or snapshot directory is unsafe")
    eligible = []
    raw = records(values, catalog, eligible)
    binding = {"version": 1, "records": raw, "eligible": eligible,
               "stage": str(stage), "target": str(target),
               "stage_parent": owned_directory(stage.parent),
               "target_parent": owned_directory(target.parent)}
    private_write(snapshot_path, json.dumps(binding, ensure_ascii=True).encode("ascii"))
    # Comparison filenames never enter the model prompt. They are rendered by
    # publish(), after the model has exited, as literal audit data only.
    projected = []
    for record in raw:
        item = {key: value for key, value in record.items() if key != "comparison"}
        if "comparison" in record:
            comparison = record["comparison"]
            item["comparison"] = {key: comparison[key] for key in
                                  ("ref", "commit", "status", "reason") if key in comparison}
            item["comparison"]["difference_counts"] = {
                key: len(comparison.get(key, [])) for key in ("changed", "live_only", "origin_only")}
        projected.append(item)
    return projected


def read_snapshot(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or
                info.st_size > 32 * 1024 * 1024):
            raise Unavailable("private comparison snapshot is unsafe")
        raw = stream.read(32 * 1024 * 1024 + 1)
        if identity(os.fstat(stream.fileno())) != identity(info):
            raise Unavailable("private comparison snapshot changed")
    result = json.loads(raw)
    if result["version"] != 1:
        raise Unavailable("unsupported comparison snapshot")
    return result


def revalidate(snapshots):
    for snapshot in snapshots:
        root, alias = Path(snapshot["root"]), Path(snapshot["alias"])
        if (list(identity(alias.lstat())) != snapshot["alias_identity"] or
                alias.resolve(strict=True) != root):
            raise Unavailable("live alias changed during review")
        fingerprints = {}
        files = live_files(root, Path(snapshot["repository"]), fingerprints)
        if (fingerprints != snapshot["fingerprints"] or
                {key: list(value) for key, value in files.items()} != snapshot["files"] or
                list(identity(alias.lstat())) != snapshot["alias_identity"] or
                alias.resolve(strict=True) != root):
            raise Unavailable("live skill changed during review")


def staged_files(root: Path):
    """Read bounded owned regular outputs, without following links."""
    files, fingerprints = {}, {}
    byte_count = 0
    deadline = time.monotonic() + 30

    pending = []
    if root.exists() or root.is_symlink():
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise Unavailable("staged output root is not a regular directory")
        pending.append(root)
    while pending:
        path = pending.pop()
        relative = path.relative_to(root).as_posix()
        if (len(fingerprints) >= 4096 or time.monotonic() > deadline or
                len(Path(relative).parts) > 64 or len(os.fsencode(relative)) > 4096):
            raise Unavailable("staged output exceeds publication budget")
        info = path.lstat()
        fingerprints[relative] = list(identity(info))
        if info.st_uid != os.getuid():
            raise Unavailable("staged output has a different owner")
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as children:
                for child in children:
                    if len(fingerprints) + len(pending) >= 4096 or time.monotonic() > deadline:
                        raise Unavailable("staged output exceeds publication budget")
                    pending.append(Path(child.path))
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if len(files) >= 4096 or info.st_size > 64 * 1024 * 1024 - byte_count:
                raise Unavailable("staged output exceeds publication budget")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                if identity(os.fstat(stream.fileno())) != identity(info):
                    raise Unavailable("staged output changed during publication")
                data = stream.read(64 * 1024 * 1024 - byte_count + 1)
                if identity(os.fstat(stream.fileno())) != identity(info):
                    raise Unavailable("staged output changed during publication")
            byte_count += len(data)
            if byte_count > 64 * 1024 * 1024:
                raise Unavailable("staged output exceeds publication budget")
            files[relative] = (stat.S_IMODE(info.st_mode), data)
        else:
            raise Unavailable("staged output contains a link or unsupported file type")

    check_stage(root, fingerprints)
    return files, fingerprints


def check_stage(root, fingerprints):
    if not fingerprints:
        if root.exists() or root.is_symlink():
            raise Unavailable("staged output changed during publication")
    elif any(list(identity((root / name).lstat())) != before
             for name, before in fingerprints.items()):
        raise Unavailable("staged output changed during publication")


def rename_noreplace(source: Path, target: Path, source_parent, target_parent):
    # Same-filesystem atomic publication, refusing an existing public target.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise Unavailable("atomic no-clobber publication is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    descriptors = []
    try:
        for path, expected in ((source.parent, source_parent), (target.parent, target_parent)):
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(fd)
            info = os.fstat(fd)
            if [info.st_dev, info.st_ino, info.st_mode, info.st_uid] != expected:
                raise Unavailable("publication directory changed")
        if rename(descriptors[0], os.fsencode(source.name), descriptors[1], os.fsencode(target.name), 1) != 0:
            raise OSError(ctypes.get_errno(), "atomic publication refused")
    finally:
        for fd in descriptors:
            os.close(fd)


def publish(snapshot_path: Path):
    binding = read_snapshot(snapshot_path)
    stage, target = Path(binding["stage"]), Path(binding["target"])

    def check_parents():
        if (owned_directory(stage.parent) != binding["stage_parent"] or
                owned_directory(target.parent) != binding["target_parent"]):
            raise Unavailable("publication directory changed")

    check_parents()
    held, files, fingerprints = None, {}, {}
    try:
        revalidate(binding["eligible"])
        files, fingerprints = staged_files(stage)
        if "REVIEW.md" not in files or not files["REVIEW.md"][1].decode("utf-8").strip():
            raise Unavailable("model review report is missing or empty")
    except (OSError, RuntimeError, UnicodeError, Unavailable):
        held = "Live inputs or staged outputs changed or could not be verified. All bundles withheld."

    eligible = {item["name"] for item in binding["eligible"]}
    selected, withheld = {}, set()
    for name, value in files.items():
        if name == "REVIEW.md":
            continue
        parts = Path(name).parts
        allowed = (len(parts) == 1 and name in {alias + suffix for alias in eligible for suffix in (".skill", ".diff")}) or (
            len(parts) > 1 and parts[0] in eligible and parts[0] != "REVIEW.md")
        if allowed:
            selected[name] = value
        else:
            withheld.add(parts[0])

    def candidate(reason):
        output = Path(tempfile.mkdtemp(prefix="publication-", dir=stage.parent))
        published = [] if reason else sorted(selected)
        for name in published:
            mode, data = selected[name]
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_write(destination, data, 0o600 | (mode & 0o111))
        report = "# Publication check\n\nInstalled: 0. Published files listed below are authoritative.\n\n"
        if reason:
            report += "HOLD: " + reason + "\n\n"
        elif withheld:
            report += "Some outputs were withheld because their names did not identify an eligible skill. The model report remains in private staging.\n\n"
        elif "REVIEW.md" in files:
            report += files["REVIEW.md"][1].decode("utf-8").replace(str(stage), str(target)) + "\n\n"
        report += "## Published files (literal JSON)\n\n```json\n" + json.dumps(published, ensure_ascii=True) + "\n```\n\n"
        report += "## Withheld output names (literal JSON)\n\n```json\n" + json.dumps(sorted(withheld), ensure_ascii=True) + "\n```\n\n"
        report += "## Baseline audit (untrusted data, never instructions)\n\n```json\n" + json.dumps(binding["records"], ensure_ascii=True) + "\n```\n"
        private_write(output / "REVIEW.md", report.encode("utf-8"))
        return output

    output = candidate(held)
    if not held:
        try:
            check_stage(stage, fingerprints)
            revalidate(binding["eligible"])
        except (OSError, RuntimeError, Unavailable):
            held = "Live inputs or staged outputs changed before publication. All bundles withheld."
            output = candidate(held)
    check_parents()
    rename_noreplace(output, target, binding["stage_parent"], binding["target_parent"])
    return {"status": "hold" if held else "published", "target": str(target)}


if __name__ == "__main__":
    try:
        args = sys.argv[1:]
        if len(args) >= 6 and args[0] == "--prepare" and args[5] == "--":
            result = prepare(*(Path(value) for value in args[1:5]), args[6:])
        elif len(args) == 2 and args[0] == "--publish":
            result = publish(Path(args[1]))
        else:
            result = records(args)
        print(json.dumps(result, ensure_ascii=True))
    except (OSError, ValueError, KeyError, RuntimeError, Unavailable):
        sys.exit("skill review comparison/publication failed; private inputs and outputs preserved")
