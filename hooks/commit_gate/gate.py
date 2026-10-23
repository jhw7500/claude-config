"""Commit-time gate: judge ``git commit`` messages in a shell command.

Only clear violations block.  Anything the gate cannot judge for certain (an
unknown message, an internal error, a vendored-rule digest mismatch) is allowed
with one stderr warning line (design section 12, low friction).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # package import (hooks on sys.path) and direct script run
    from . import commit_scan, grant, repo
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from commit_gate import commit_scan, grant, repo

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
INSTALLED_CLI = "$HOME/.local/share/claude-config/commit_gate/cli.py"
_MAX_FILE_BYTES = 64 * 1024
_TITLE_TYPES = "feat|fix|chore|refactor|docs|test|build|ci|perf|style"
_HEADINGS = "Contract version, Why, Changes, Validation, References"
_HINTS = {
    "heading-contract": f"필드 제목 5개를 순서대로(앞에 공백 한 칸): {_HEADINGS}",
    "title-type-enum": f"type은 {_TITLE_TYPES} 중 하나",
    "title-type-case": "type은 소문자",
    "title-type-empty": "제목을 `type(scope): 결과` 형식으로 시작",
    "title-subject-empty": "콜론 뒤에 결과를 말하는 제목을 쓴다",
    "title-subject-full-stop": "제목 끝의 마침표를 뺀다",
    "title-header-trim": "제목 앞뒤 공백을 뺀다",
    "commit-title-too-long": "제목을 72자 이하로 줄인다",
    "unexpected-preamble": "제목과 첫 필드 제목 사이에는 빈 줄만 둔다",
    "commit-title-separator": "제목 한 줄 뒤에 빈 줄을 두고 계약 필드 5개(Contract version, Why, Changes, Validation, References)를 쓴다",
    "empty-field": "비어 있는 필드에 내용을 채운다",
    "bullet-required": "해당 필드를 `- ` bullet로 쓴다",
    "version-mismatch": "Contract version 값을 v1로 쓴다",
    "reference-required": "References에 확인한 이슈·문서 URL을 bullet로 쓴다",
}

_FALLBACK_HINT = "커밋 템플릿(examples/change-evidence/commit-message.md) 또는 `/jhw:commit`으로 정본 계약(change-evidence-contract-v1)에 맞춘다"


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str


def _warn(text: str) -> None:
    print(f"[commit-gate] {text}", file=sys.stderr)


def check_message(message: str) -> dict:
    """Judge ``message`` with the vendored checker (loaded by path)."""
    path = VENDOR_DIR / "check_commit_message.py"
    name = f"_commit_gate_vendor_{hashlib.sha256(str(path).encode()).hexdigest()[:12]}"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return module.check_message(message)


def _vendor_source() -> Optional[dict]:
    """SOURCE.json when every vendored file matches its digest, else None."""
    try:
        source = json.loads((VENDOR_DIR / "SOURCE.json").read_text(encoding="utf-8"))
        for fname, want in source["files"].items():
            if hashlib.sha256((VENDOR_DIR / fname).read_bytes()).hexdigest() != want:
                return None
        return source
    except Exception:  # noqa: BLE001 - unreadable vendor copy is a mismatch
        return None


def _read_file(path: Path) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            data = handle.read(_MAX_FILE_BYTES + 1)
    except OSError:
        return None
    return data if len(data) <= _MAX_FILE_BYTES else None


def _effective_cleanup(inv: commit_scan.CommitInvocation) -> Optional[str]:
    """The cleanup mode git will use, or None when comment handling is unmodelled."""
    mode = inv.cleanup
    if mode == "default":
        configured = repo.git(inv.directory, "config", "--get", "commit.cleanup")
        if configured:
            mode = configured
    if mode != "verbatim":
        for key in ("core.commentChar", "core.commentString"):
            if repo.git(inv.directory, "config", "--get", key) is not None:
                return None
    return mode


def _raw_bytes(inv: commit_scan.CommitInvocation) -> Optional[bytes]:
    if inv.source == "literal":
        return inv.message_text.encode("utf-8") if inv.message_text is not None else None
    if inv.message_path is None:
        return None
    path = inv.message_path
    if not path.is_absolute():
        path = inv.directory / path
    return _read_file(path)


@dataclass(frozen=True)
class _Violation:
    binding: dict
    sha256: str
    codes: tuple[str, ...]
    directory: Path


def _judge(inv: commit_scan.CommitInvocation, runtime: str) -> Optional[_Violation]:
    root = repo.repo_root(inv.directory)
    if root is None or not repo.origin_is_jhw7500(root):
        return None
    if inv.source == "unknown":
        _warn(f"커밋 메시지를 알 수 없어 검사하지 않았다 ({inv.reason})")
        return None
    if inv.source == "auto":
        return None
    data = _raw_bytes(inv)
    if data is None:
        _warn("커밋 메시지를 읽지 못해 검사하지 않았다")
        return None
    mode = _effective_cleanup(inv)
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:
        raw = None
    message = (
        commit_scan.effective_message(raw, mode, from_literal=inv.source == "literal")
        if raw is not None and mode is not None
        else None
    )
    if message is None:
        _warn("커밋 메시지 정리 규칙을 알 수 없어 검사하지 않았다")
        return None
    report = check_message(message)
    if report["valid"]:
        return None
    digest = hashlib.sha256(data).hexdigest()
    binding = grant.make_binding(
        repo_root=root, head=repo.head(root), message_sha256=digest, runtime=runtime
    )
    codes = tuple(report["title_findings"]) + tuple(report["evidence_findings"])
    return _Violation(binding, digest, codes, root)


def _peek(home: Path, binding: dict, now: float) -> bool:
    """True when one exact, unexpired grant exists (read only)."""
    try:
        name = grant.binding_name(binding)
        with grant._locked_dir(home, create=False) as dir_fd:  # noqa: SLF001
            payload = grant._parse(grant._read_grant(dir_fd, name), name)  # noqa: SLF001
        if payload is None or payload["binding"] != binding:
            return False
        return 0 <= now - payload["created_at"] <= grant.GRANT_TTL_SECONDS
    except Exception:  # noqa: BLE001
        return False


def _cli_arg() -> str:
    # keep $HOME expandable while quoting the rest of the path
    prefix = "$HOME/"
    if INSTALLED_CLI.startswith(prefix):
        return '"$HOME"/' + shlex.quote(INSTALLED_CLI[len(prefix):])
    return shlex.quote(INSTALLED_CLI)


def _deny_reason(violations: list[_Violation], source: dict) -> str:
    lines = [
        "[COMMIT-GATE:RULE_VIOLATION] 커밋 메시지가 Change Evidence 규칙을 어겼다. "
        f"automation_commit={source['automation_commit']} rules_version={source['rules_version']}"
    ]
    for v in violations:
        lines.append(f"- 위반 커밋 (repo {v.binding['repo_root']}, message sha256 {v.sha256})")
        for code in dict.fromkeys(v.codes):
            lines.append(f"  - {code}: {_HINTS.get(code, _FALLBACK_HINT)}")
    first = violations[0]
    lines.append(
        "메시지를 고쳐 다시 커밋하는 것이 기본이다. 규칙을 어긴 채 커밋하려면 사용자에게 묻고, "
        "승인받은 경우에만 다음을 실행한 뒤 같은 커밋을 다시 실행한다 (승인은 1회·10분): "
        f"/usr/bin/python3 {_cli_arg()} grant --runtime {shlex.quote(str(first.binding['runtime']))} "
        f"--repo {shlex.quote(str(first.binding['repo_root']))} "
        f"--message-sha256 {shlex.quote(first.sha256)} "
        '--reason "<사용자의 답>" --relayed'
    )
    return "\n".join(lines)


def evaluate(command: str, *, cwd: Path, runtime: str, home: Path, now: float) -> Decision:
    allow = Decision(True, "")
    if not isinstance(command, str) or "commit" not in command:
        return allow
    try:
        source = _vendor_source()
        if source is None:
            _warn("vendor 규칙 사본의 digest가 SOURCE.json과 달라 검사하지 않았다")
            return allow
        violations: list[_Violation] = []
        for inv in commit_scan.scan_commits(command, cwd=Path(cwd)).invocations:
            found = _judge(inv, runtime)
            if found is not None:
                violations.append(found)
        if not violations:
            return allow
        seen: set[str] = set()
        ungranted: list[_Violation] = []
        for v in violations:
            name = grant.binding_name(v.binding)
            if name in seen or not _peek(home, v.binding, now):
                ungranted.append(v)
            seen.add(name)
        if ungranted:
            return Decision(False, _deny_reason(ungranted, source))
        for v in violations:
            if not grant.consume_grant(home, binding=v.binding, now=now):
                return Decision(False, _deny_reason([v], source))
        return allow
    except Exception as error:  # noqa: BLE001 - fail open (design section 12)
        _warn(f"게이트 내부 오류로 검사하지 않았다: {type(error).__name__}")
        return allow


def default_home() -> Path:
    return Path(os.path.expanduser("~"))
