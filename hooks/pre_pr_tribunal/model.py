from dataclasses import dataclass
from typing import Sequence


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ChangedPath:
    status: str
    path: str
    old_path: str | None = None

    def to_json(self) -> dict[str, object]:
        value: dict[str, object] = {"status": self.status, "path": self.path}
        if self.old_path is not None:
            value["old_path"] = self.old_path
        return value


@dataclass(frozen=True)
class Snapshot:
    schema: int
    repository: str
    base_ref: str
    base_sha: str
    head_sha: str
    merge_base_sha: str
    diff_sha256: str
    paths: Sequence[ChangedPath]
    initial_paths: Sequence[str]
    created_at: str

    def to_json(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "repository": self.repository,
            "base": {"ref": self.base_ref, "sha": self.base_sha},
            "head_sha": self.head_sha,
            "merge_base_sha": self.merge_base_sha,
            "diff_sha256": self.diff_sha256,
            "paths": [item.to_json() for item in self.paths],
            "initial_paths": list(self.initial_paths),
            "created_at": self.created_at,
        }


class TribunalError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
