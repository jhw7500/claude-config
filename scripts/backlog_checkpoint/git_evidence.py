"""Read-only Git evidence collection for backlog checkpoint binding."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import unicodedata
from typing import Any

from .codec import CheckpointError, canonical_bytes, decode_snapshot, sha256_bytes


_OID = re.compile(r"[0-9a-f]{40}\Z")
_CHECKPOINT_NAME = re.compile(r"HANDOFF\.backlog-([0-9a-f]{32})\.md\Z")
_SOURCE_KEYS = {"change", "mode", "path", "sha256"}
_MANIFEST_KEYS = {
    "paths",
    "count",
    "path_set_digest",
    "change_digest",
    "sources",
    "parent_head",
}


def _git_environment() -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git(root: str, *arguments: str) -> bytes:
    command = [
        "git",
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.fileMode=true",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "diff.external=",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (OSError, ValueError) as exc:
        raise CheckpointError("Git evidence probe could not run") from exc
    if result.returncode != 0:
        raise CheckpointError(f"Git evidence probe failed: {arguments[0]}")
    return result.stdout


def _decode_line(data: bytes, label: str) -> str:
    try:
        value = data.decode("utf-8")
    except UnicodeError as exc:
        raise CheckpointError(f"{label} is not valid UTF-8") from exc
    if not value.endswith("\n") or value.endswith("\n\n"):
        raise CheckpointError(f"{label} is malformed")
    value = value[:-1]
    if not value or "\n" in value or "\r" in value:
        raise CheckpointError(f"{label} is malformed")
    return value


def _oid(data: bytes, label: str) -> str:
    value = _decode_line(data, label)
    if _OID.fullmatch(value) is None:
        raise CheckpointError(f"{label} is not a 40-character object ID")
    return value


def _canonical_root(root: object) -> str:
    if not isinstance(root, str) or not root:
        raise CheckpointError("Git root must be a non-empty string")
    try:
        root.encode("utf-8")
    except UnicodeError as exc:
        raise CheckpointError("Git root must be valid UTF-8") from exc
    if any(unicodedata.category(character) == "Cc" for character in root):
        raise CheckpointError("Git root contains a control character")
    if not os.path.isabs(root) or os.path.normpath(root) != root:
        raise CheckpointError("Git root must be an exact absolute path")
    physical = os.path.realpath(root)
    if physical != root:
        raise CheckpointError("Git root must not be a lexical or symlink alias")
    try:
        metadata = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise CheckpointError("Git root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise CheckpointError("Git root must be a directory")
    top = _decode_line(_git(root, "rev-parse", "--show-toplevel"), "Git root")
    if top != root:
        raise CheckpointError("Git root is not the exact checkout root")
    return root


def _parse_worktrees(data: bytes) -> list[dict[bytes, bytes | bool]]:
    records: list[dict[bytes, bytes | bool]] = []
    current: dict[bytes, bytes | bool] = {}
    for field in data.split(b"\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        key, separator, value = field.partition(b" ")
        if key in current:
            raise CheckpointError("Git worktree record has a duplicate field")
        current[key] = value if separator else True
    if current:
        records.append(current)
    if not records:
        raise CheckpointError("Git worktree list is empty")
    return records


def inspect_worktree(
    root: str, expected_branch: str, expected_worktree_ref: str
) -> dict:
    """Verify an exact physical root, branch, and unique worktree record."""
    checked_root = _canonical_root(root)
    if (
        not isinstance(expected_worktree_ref, str)
        or expected_worktree_ref != checked_root
    ):
        raise CheckpointError(
            "expected worktree reference does not match the exact root"
        )
    if not isinstance(expected_branch, str) or not expected_branch:
        raise CheckpointError("expected branch is invalid")
    try:
        branch_bytes = expected_branch.encode("utf-8")
    except UnicodeError as exc:
        raise CheckpointError("expected branch is not valid UTF-8") from exc
    if len(branch_bytes) > 256 or any(
        unicodedata.category(character) == "Cc" for character in expected_branch
    ):
        raise CheckpointError("expected branch is invalid")
    branch_ref = b"refs/heads/" + branch_bytes
    records = _parse_worktrees(
        _git(checked_root, "worktree", "list", "--porcelain", "-z")
    )
    branch_records = [
        record for record in records if record.get(b"branch") == branch_ref
    ]
    if len(branch_records) != 1:
        raise CheckpointError(
            "expected branch does not have one unique worktree record"
        )
    record = branch_records[0]
    raw_path = record.get(b"worktree")
    raw_head = record.get(b"HEAD")
    if not isinstance(raw_path, bytes) or not isinstance(raw_head, bytes):
        raise CheckpointError("Git worktree record is incomplete")
    try:
        worktree_path = raw_path.decode("utf-8")
        head = raw_head.decode("ascii")
    except UnicodeError as exc:
        raise CheckpointError("Git worktree record encoding is invalid") from exc
    if (
        worktree_path != checked_root
        or os.path.realpath(worktree_path) != checked_root
        or os.path.normpath(worktree_path) != worktree_path
    ):
        raise CheckpointError("Git worktree record is not the canonical root")
    if _OID.fullmatch(head) is None:
        raise CheckpointError("Git worktree HEAD is invalid")
    current_ref = _decode_line(
        _git(checked_root, "symbolic-ref", "--quiet", "HEAD"), "Git branch"
    )
    if current_ref.encode("utf-8") != branch_ref:
        raise CheckpointError("current branch does not match the expected branch")
    current_head = _oid(
        _git(checked_root, "rev-parse", "--verify", "HEAD^{commit}"), "HEAD"
    )
    if current_head != head:
        raise CheckpointError("worktree record HEAD does not match current HEAD")
    return {
        "root": checked_root,
        "realpath": checked_root,
        "branch": expected_branch,
        "worktree_ref": current_ref,
        "head": current_head,
    }


def _validate_paths(paths: object) -> list[tuple[bytes, str]]:
    if not isinstance(paths, (list, tuple)) or len(paths) > 10_000:
        raise CheckpointError("source allowlist must contain at most 10000 paths")
    checked: list[tuple[bytes, str]] = []
    seen: set[bytes] = set()
    for path in paths:
        if not isinstance(path, str) or not path:
            raise CheckpointError("source path must be a non-empty string")
        if os.path.isabs(path):
            raise CheckpointError("source path must be relative")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise CheckpointError("source path is not lexically safe")
        if any(unicodedata.category(character) == "Cc" for character in path):
            raise CheckpointError("source path contains a control character")
        try:
            raw = path.encode("utf-8")
        except UnicodeError as exc:
            raise CheckpointError("source path must be valid UTF-8") from exc
        if raw in seen:
            raise CheckpointError("source allowlist contains a duplicate path")
        seen.add(raw)
        checked.append((raw, path))
    checked.sort(key=lambda item: item[0])
    return checked


def _status_entries(data: bytes) -> dict[bytes, bytes]:
    entries: dict[bytes, bytes] = {}
    for record in data.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise CheckpointError("Git status porcelain record is malformed")
        path = record[3:]
        if not path:
            raise CheckpointError("Git status contains an empty path")
        try:
            path.decode("utf-8")
        except UnicodeError as exc:
            raise CheckpointError("dirty Git path is not valid UTF-8") from exc
        if path in entries:
            raise CheckpointError("Git status contains a duplicate path")
        entries[path] = record[:2]
    return entries


def _status(root: str) -> bytes:
    return _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
    )


def _checkpoint_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_checkpoint_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise CheckpointError(
            "checkpoint artifact must be current-UID mode 0600 regular file"
        )
    return _checkpoint_metadata(metadata)


def _checkpoint_artifact(
    root: str, path: str | None
) -> tuple[bytes, tuple[tuple[int, ...], str]] | None:
    if path is None:
        return None
    if (
        not isinstance(path, str)
        or not os.path.isabs(path)
        or os.path.normpath(path) != path
    ):
        raise CheckpointError("checkpoint path must be an exact absolute path")
    parent, name = os.path.split(path)
    match = _CHECKPOINT_NAME.fullmatch(name)
    if parent != root or match is None:
        raise CheckpointError("checkpoint must be the selected root-level run artifact")
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise CheckpointError("checkpoint artifact is unavailable") from exc
    try:
        opened = _validate_checkpoint_metadata(os.fstat(fd))
        data = bytearray()
        while len(data) <= 12 * 1024:
            chunk = os.read(fd, 12 * 1024 + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > 12 * 1024:
            raise CheckpointError("checkpoint artifact exceeds its size bound")
        after = _validate_checkpoint_metadata(os.fstat(fd))
        if opened != after:
            raise CheckpointError("checkpoint artifact changed during validation")
    finally:
        os.close(fd)
    try:
        current = _validate_checkpoint_metadata(os.stat(path, follow_symlinks=False))
    except OSError as exc:
        raise CheckpointError("checkpoint artifact changed during validation") from exc
    if current != after:
        raise CheckpointError("checkpoint artifact changed during validation")
    snapshot = decode_snapshot(bytes(data))
    if (
        snapshot["run_id"] != match.group(1)
        or snapshot["binding"]["worktree_root"] != root
    ):
        raise CheckpointError("checkpoint artifact does not match its run and worktree")
    raw_name = name.encode("utf-8")
    if _git(root, "ls-files", "--stage", "--", name):
        raise CheckpointError("checkpoint artifact must never be tracked")
    if _git(root, "ls-tree", "-z", "--full-tree", "HEAD", "--", name):
        raise CheckpointError("checkpoint artifact must never exist in HEAD")
    return raw_name, (after, sha256_bytes(bytes(data)))


def _tree(root: str, object_id: str) -> dict[bytes, tuple[str, str, str]]:
    data = _git(root, "ls-tree", "-r", "-z", "--full-tree", object_id)
    entries: dict[bytes, tuple[str, str, str]] = {}
    for record in data.split(b"\0"):
        if not record:
            continue
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3 or not path or path in entries:
            raise CheckpointError("Git tree record is malformed")
        try:
            mode = fields[0].decode("ascii")
            kind = fields[1].decode("ascii")
            oid = fields[2].decode("ascii")
        except UnicodeError as exc:
            raise CheckpointError("Git tree metadata encoding is invalid") from exc
        if _OID.fullmatch(oid) is None:
            raise CheckpointError("Git tree object ID is invalid")
        entries[path] = (mode, kind, oid)
    return entries


def _read_regular(
    parent_descriptor: int, name: bytes, metadata: os.stat_result
) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise CheckpointError("source regular file could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise CheckpointError("source regular file changed during collection")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise CheckpointError("source regular file changed during collection")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _worktree_entry(root: str, raw_path: bytes) -> tuple[str, bytes] | None:
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or os.readlink not in os.supports_dir_fd
    ):
        raise CheckpointError("secure descriptor-relative source reads are unsupported")
    parts = raw_path.split(b"/")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        try:
            descriptors.append(os.open(os.fsencode(root), directory_flags))
        except OSError as exc:
            raise CheckpointError("source root could not be opened safely") from exc
        for part in parts[:-1]:
            try:
                descriptors.append(
                    os.open(part, directory_flags, dir_fd=descriptors[-1])
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise CheckpointError(
                    "source path has an unsupported parent component"
                ) from exc
        try:
            metadata = os.stat(parts[-1], dir_fd=descriptors[-1], follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CheckpointError("source metadata could not be inspected") from exc
        if stat.S_ISREG(metadata.st_mode):
            mode = "100755" if metadata.st_mode & 0o111 else "100644"
            return mode, _read_regular(descriptors[-1], parts[-1], metadata)
        if stat.S_ISLNK(metadata.st_mode):
            try:
                value = os.readlink(parts[-1], dir_fd=descriptors[-1])
                after = os.stat(
                    parts[-1], dir_fd=descriptors[-1], follow_symlinks=False
                )
            except OSError as exc:
                raise CheckpointError(
                    "source symlink could not be read safely"
                ) from exc
            if (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_mtime_ns,
            ):
                raise CheckpointError("source symlink changed during collection")
            if not isinstance(value, bytes):
                value = os.fsencode(value)
            return "120000", value
        raise CheckpointError("source path has an unsupported file type")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _blob(root: str, object_id: str) -> bytes:
    return _git(root, "cat-file", "blob", object_id)


def _source_records(
    root: str,
    checked_paths: list[tuple[bytes, str]],
    parent_tree: dict[bytes, tuple[str, str, str]],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for raw_path, path in checked_paths:
        base = parent_tree.get(raw_path)
        current = _worktree_entry(root, raw_path)
        if base is None and current is None:
            continue
        if base is not None and base[1] != "blob":
            raise CheckpointError("source path has an unsupported Git object type")
        if current is None:
            sources.append(
                {"change": "delete", "mode": None, "path": path, "sha256": None}
            )
            continue
        mode, data = current
        if base is None:
            change = "add"
        else:
            base_data = _blob(root, base[2])
            if base[0] == mode and base_data == data:
                continue
            change = "modify"
        sources.append(
            {
                "change": change,
                "mode": mode,
                "path": path,
                "sha256": sha256_bytes(data),
            }
        )
    return sources


def _manifest(parent_head: str, sources: list[dict[str, Any]]) -> dict:
    paths = [source["path"] for source in sources]
    path_bytes = b"".join(path.encode("utf-8") + b"\0" for path in paths)
    return {
        "paths": paths,
        "count": len(paths),
        "path_set_digest": sha256_bytes(path_bytes),
        "change_digest": sha256_bytes(canonical_bytes(sources)),
        "sources": sources,
        "parent_head": parent_head,
    }


def _visible_index(root: str) -> bytes:
    entries = _git(root, "ls-files", "-v", "-z")
    if any(
        entry[:1].islower() or entry[:1] == b"S"
        for entry in entries.split(b"\0")
        if entry
    ):
        raise CheckpointError("Git index flags can hide tracked worktree changes")
    return entries


def collect_sources(
    root: str, allowed_paths: list[str], checkpoint_path: str | None = None
) -> dict:
    """Collect an exact source manifest without staging or mutating Git state."""
    checked_root = _canonical_root(root)
    checked_paths = _validate_paths(allowed_paths)
    first_index = _visible_index(checked_root)
    checkpoint = _checkpoint_artifact(checked_root, checkpoint_path)
    allowed = {raw for raw, _ in checked_paths}
    parent_head = _oid(
        _git(checked_root, "rev-parse", "--verify", "HEAD^{commit}"), "parent HEAD"
    )
    first_status = _status(checked_root)
    status_entries = _status_entries(first_status)
    dirty = set(status_entries)
    runtime = set() if checkpoint is None else {checkpoint[0]}
    unknown = dirty - allowed - runtime
    if unknown:
        raise CheckpointError("dirty Git paths exist outside the source allowlist")
    if any(
        status != b"??" and status[:1] != b" " and status[1:] != b" "
        for status in status_entries.values()
    ):
        raise CheckpointError("source path has mixed staged and worktree changes")
    parent_tree = _tree(checked_root, parent_head)
    sources = _source_records(checked_root, checked_paths, parent_tree)
    second_sources = _source_records(checked_root, checked_paths, parent_tree)
    second_status = _status(checked_root)
    second_index = _visible_index(checked_root)
    if checkpoint_path is not None:
        if _checkpoint_artifact(checked_root, checkpoint_path) != checkpoint:
            raise CheckpointError("checkpoint artifact changed during collection")
    current_head = _oid(
        _git(checked_root, "rev-parse", "--verify", "HEAD^{commit}"), "current HEAD"
    )
    if (
        sources != second_sources
        or first_status != second_status
        or first_index != second_index
        or parent_head != current_head
    ):
        raise CheckpointError("Git source state changed during collection")
    changed = {source["path"].encode("utf-8") for source in sources}
    if dirty - changed - runtime:
        raise CheckpointError("Git index state does not match the source worktree")
    return _manifest(parent_head, sources)


def _validate_manifest(manifest: object, parent_head: str) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise CheckpointError("source manifest has missing or unknown keys")
    if manifest["parent_head"] != parent_head:
        raise CheckpointError("source manifest parent does not match the frozen parent")
    if type(manifest["count"]) is not int or not 0 <= manifest["count"] <= 10_000:
        raise CheckpointError("source manifest count is invalid")
    if not isinstance(manifest["paths"], list):
        raise CheckpointError("source manifest paths are invalid")
    for field in ("path_set_digest", "change_digest"):
        digest = manifest[field]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise CheckpointError(f"source manifest {field} is invalid")
    sources = manifest["sources"]
    if not isinstance(sources, list) or len(sources) > 10_000:
        raise CheckpointError("source manifest records are invalid")
    normalized: list[dict[str, Any]] = []
    previous: bytes | None = None
    for source in sources:
        if not isinstance(source, dict) or set(source) != _SOURCE_KEYS:
            raise CheckpointError("source manifest record has missing or unknown keys")
        raw_path, _ = _validate_paths([source["path"]])[0]
        if previous is not None and raw_path <= previous:
            raise CheckpointError("source manifest paths are not uniquely byte-sorted")
        previous = raw_path
        change = source["change"]
        mode = source["mode"]
        digest = source["sha256"]
        if not isinstance(change, str):
            raise CheckpointError("source manifest change is invalid")
        if mode is not None and not isinstance(mode, str):
            raise CheckpointError("source manifest mode is invalid")
        if change == "delete":
            if mode is not None or digest is not None:
                raise CheckpointError("deleted source manifest record is invalid")
        elif change in {"add", "modify"}:
            if mode not in {"100644", "100755", "120000"}:
                raise CheckpointError("source manifest mode is invalid")
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise CheckpointError("source manifest digest is invalid")
        else:
            raise CheckpointError("source manifest change is invalid")
        normalized.append(dict(source))
    expected = _manifest(parent_head, normalized)
    if manifest != expected:
        raise CheckpointError(
            "source manifest digests or summary do not match its records"
        )
    return expected


def _tree_changed_paths(root: str, parent_head: str, commit_oid: str) -> list[str]:
    parent_tree = _tree(root, parent_head)
    commit_tree = _tree(root, commit_oid)
    changed_paths = sorted(
        path
        for path in parent_tree.keys() | commit_tree.keys()
        if parent_tree.get(path) != commit_tree.get(path)
    )
    paths: list[str] = []
    for raw_path in changed_paths:
        try:
            path = raw_path.decode("utf-8")
        except UnicodeError as exc:
            raise CheckpointError("committed source path is not valid UTF-8") from exc
        _validate_paths([path])
        paths.append(path)
    return paths


def _tree_manifest(root: str, parent_head: str, commit_oid: str) -> dict:
    parent_tree = _tree(root, parent_head)
    commit_tree = _tree(root, commit_oid)
    checked_paths = [
        (path.encode("utf-8"), path)
        for path in _tree_changed_paths(root, parent_head, commit_oid)
    ]
    sources: list[dict[str, Any]] = []
    for raw_path, path in checked_paths:
        base = parent_tree.get(raw_path)
        current = commit_tree.get(raw_path)
        if current is None:
            sources.append(
                {"change": "delete", "mode": None, "path": path, "sha256": None}
            )
            continue
        if current[1] != "blob" or current[0] not in {"100644", "100755", "120000"}:
            raise CheckpointError("committed source has an unsupported Git object type")
        sources.append(
            {
                "change": "add" if base is None else "modify",
                "mode": current[0],
                "path": path,
                "sha256": sha256_bytes(_blob(root, current[2])),
            }
        )
    return _manifest(parent_head, sources)


def _commit_parents(root: str, commit_oid: str) -> list[str]:
    data = _git(root, "cat-file", "commit", commit_oid)
    parents: list[str] = []
    for line in data.splitlines():
        if not line:
            break
        if line.startswith(b"parent "):
            try:
                parent = line[7:].decode("ascii")
            except UnicodeError as exc:
                raise CheckpointError("commit parent encoding is invalid") from exc
            if _OID.fullmatch(parent) is None:
                raise CheckpointError("commit parent is invalid")
            parents.append(parent)
    return parents


def _matching_direct_children(root: str, parent_head: str, manifest: dict) -> list[str]:
    data = _git(
        root,
        "rev-list",
        "--parents",
        "--exclude=refs/replace/*",
        "--all",
    )
    candidates: set[str] = set()
    for line in data.splitlines():
        fields = line.decode("ascii").split()
        if len(fields) == 2 and fields[1] == parent_head and _OID.fullmatch(fields[0]):
            candidates.add(fields[0])
    matching: list[str] = []
    for candidate in sorted(candidates):
        if _tree_changed_paths(root, parent_head, candidate) != manifest["paths"]:
            continue
        if _tree_manifest(root, parent_head, candidate) == manifest:
            matching.append(candidate)
    return matching


def bind_commit(
    root: str,
    parent_head: str,
    manifest: dict,
    commit_oid: str,
    checkpoint_path: str | None = None,
) -> dict:
    """Bind reviewed source evidence to one exact current direct child commit."""
    checked_root = _canonical_root(root)
    if not isinstance(parent_head, str) or _OID.fullmatch(parent_head) is None:
        raise CheckpointError("frozen parent is not a 40-character object ID")
    if not isinstance(commit_oid, str) or _OID.fullmatch(commit_oid) is None:
        raise CheckpointError("result commit is not a 40-character object ID")
    expected = _validate_manifest(manifest, parent_head)
    current_head = _oid(
        _git(checked_root, "rev-parse", "--verify", "HEAD^{commit}"), "current HEAD"
    )
    if current_head != commit_oid:
        raise CheckpointError("result commit is not the current HEAD")
    parents = _commit_parents(checked_root, commit_oid)
    if len(parents) != 1:
        raise CheckpointError("result commit must have a single parent")
    if parents[0] != parent_head:
        raise CheckpointError("result commit parent does not match the frozen parent")
    if _tree_changed_paths(checked_root, parent_head, commit_oid) != expected["paths"]:
        raise CheckpointError(
            "result commit changed paths do not match the source manifest"
        )
    committed = _tree_manifest(checked_root, parent_head, commit_oid)
    if committed != expected:
        raise CheckpointError("result commit does not match the source manifest")
    if _matching_direct_children(checked_root, parent_head, expected) != [commit_oid]:
        raise CheckpointError("result commit is not the unique matching direct child")
    remaining = collect_sources(checked_root, expected["paths"], checkpoint_path)
    if remaining["parent_head"] != commit_oid:
        raise CheckpointError("result commit is no longer the current HEAD")
    if remaining["count"] != 0:
        raise CheckpointError("worktree has additional source changes after commit")
    final_head = _oid(
        _git(checked_root, "rev-parse", "--verify", "HEAD^{commit}"), "final HEAD"
    )
    if final_head != commit_oid:
        raise CheckpointError("result commit is no longer the current HEAD")
    return {
        "commit_parent": parent_head,
        "result_commit": commit_oid,
        "source_count": expected["count"],
        "path_set_digest": expected["path_set_digest"],
        "change_digest": expected["change_digest"],
    }
