"""Discover resumable Claude Code sessions under ~/.claude/projects."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from glob import glob


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    cwd: str
    mtime: float
    title: str
    last_user: str = ""
    last_assistant: str = ""
    branch: str = ""
    repo: str = ""
    turns: int = 0
    # "open"   = a running claude process names this exact session id
    # "maybe"  = an interactive TUI is running in this session's cwd (id unknown)
    # "closed" = no claude process in this session's cwd
    live: str = "closed"

    @property
    def folder(self) -> str:
        return os.path.basename(self.cwd.rstrip("/")) or self.cwd


def _msg_text(content) -> str | None:
    """Join text blocks of a message (skips tool_use/tool_result blocks)."""
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = " ".join(p for p in parts if p).strip()
        return joined or None
    if isinstance(content, str):
        return content
    return None


def _clip(text: str, n: int) -> str:
    return " ".join(text.split())[:n]


def _find_repo(cwd: str) -> str:
    """Nearest ancestor dir containing .git (the work repo root name)."""
    d = cwd
    while d and d != os.path.dirname(d):
        # .git is a dir in normal repos but a FILE in worktrees/submodules.
        if os.path.exists(os.path.join(d, ".git")):
            return os.path.basename(d)
        d = os.path.dirname(d)
    return ""


# claude subcommands that never host a resumable session transcript.
_SKIP_SUBCOMMANDS = {
    "daemon", "bg-spare", "update", "doctor", "mcp", "auth", "agents",
    "install", "setup-token", "migrate-installer", "plugin",
}

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _parse_session_ref(args: list[str]) -> str | None:
    """Exact session id named on a claude cmdline.

    --session-id wins over --resume/-r: a bg-pty-host forked from a
    transcript carries both, and only the --session-id one is the live
    session. Accepts both "--flag value" and "--flag=value" forms.
    """
    for flag in ("--session-id", "--resume", "-r"):
        for i, a in enumerate(args):
            v = None
            if a == flag:
                if i + 1 < len(args):
                    v = args[i + 1]
            elif a.startswith(flag + "="):
                v = a[len(flag) + 1:]
            if v is None:
                continue
            if v.endswith(".jsonl"):
                v = os.path.splitext(os.path.basename(v))[0]
            if _UUID_RE.fullmatch(v):
                return v
    return None


def scan_live(proc_root: str = "/proc") -> tuple[set[str], dict[str, int]]:
    """Best-effort scan of running claude processes.

    Returns (open_ids, live_cwd_counts): session ids named exactly on a
    claude cmdline, and cwd -> count of interactive TUIs whose session id
    is unknowable (a fresh `claude` has no id on its cmdline or environ,
    and holds no fd to its transcript while idle — verified live 2026-07-17).
    """
    open_ids: set[str] = set()
    live_cwds: dict[str, int] = {}
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return open_ids, live_cwds
    for pid in pids:
        base = os.path.join(proc_root, pid)
        try:
            with open(os.path.join(base, "cmdline"), "rb") as fh:
                args = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if not args or os.path.basename(args[0]) != "claude":
            continue
        # one-off print-mode runs (the bridge itself, scripts) are transient
        if "-p" in args or "--print" in args:
            continue
        # first non-flag token is the subcommand, wherever the flags sit
        sub = next((a for a in args[1:] if not a.startswith("-")), "")
        if sub in _SKIP_SUBCOMMANDS:
            continue
        sid = _parse_session_ref(args)
        if sid:
            open_ids.add(sid)
            continue
        try:
            cwd = os.readlink(os.path.join(base, "cwd"))
        except OSError:
            continue
        live_cwds[cwd] = live_cwds.get(cwd, 0) + 1
    return open_ids, live_cwds


def _annotate_live(
    info: SessionInfo, open_ids: set[str], live_cwd_counts: dict[str, int],
) -> SessionInfo:
    """Single-session annotation (conservative: any TUI in the cwd -> maybe)."""
    if info.session_id in open_ids:
        return replace(info, live="open")
    if live_cwd_counts.get(info.cwd, 0) > 0:
        return replace(info, live="maybe")
    return info


def _extract(path: str) -> SessionInfo | None:
    session_id = os.path.splitext(os.path.basename(path))[0]
    cwd = ""
    title = ""
    branch = ""
    turns = 0
    last_user = ""       # the latest user prompt
    last_assistant = ""  # assistant reply AFTER that prompt ("" = no reply yet)
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                if d.get("gitBranch"):
                    branch = d["gitBranch"]
                typ = d.get("type")
                if typ == "user":
                    text = _msg_text((d.get("message") or {}).get("content"))
                    if text and not text.startswith("<"):
                        if not title:
                            title = _clip(text, 80)
                        last_user = _clip(text, 500)
                        last_assistant = ""  # new prompt -> its reply not seen yet
                        turns += 1
                elif typ == "assistant":
                    text = _msg_text((d.get("message") or {}).get("content"))
                    if text:
                        last_assistant = _clip(text, 1500)
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if not cwd:
        return None
    return SessionInfo(
        session_id=session_id,
        cwd=cwd,
        mtime=mtime,
        title=title or "(no title)",
        last_user=last_user,
        last_assistant=last_assistant,
        branch=branch,
        repo=_find_repo(cwd),
        turns=turns,
    )


def list_sessions(
    projects_dir: str, limit: int = 15,
    live: tuple[set[str], dict[str, int]] | None = None,
) -> list[SessionInfo]:
    # Sort by file mtime first (cheap stat), then fully parse only the newest `limit`.
    paths = []
    for p in glob(os.path.join(projects_dir, "*", "*.jsonl")):
        try:
            paths.append((os.path.getmtime(p), p))
        except OSError:
            continue
    paths.sort(reverse=True)
    open_ids, cwd_counts = scan_live() if live is None else live
    # Budgeted "maybe": with K unidentified TUIs in a cwd, only the K most
    # recent sessions there are open candidates — older siblings are closed.
    budget = dict(cwd_counts)
    infos = []
    for _, p in paths[:limit]:
        info = _extract(p)
        if info is None:
            continue
        if info.session_id in open_ids:
            info = replace(info, live="open")
        elif budget.get(info.cwd, 0) > 0:
            budget[info.cwd] -= 1
            info = replace(info, live="maybe")
        infos.append(info)
    return infos


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:  # deleted between glob() and sort()
        return 0.0


def find_session(
    projects_dir: str, session_id: str,
    live: tuple[set[str], dict[str, int]] | None = None,
) -> SessionInfo | None:
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    exact = glob(os.path.join(projects_dir, "*", session_id + ".jsonl"))
    matches = exact or glob(os.path.join(projects_dir, "*", session_id + "*.jsonl"))
    if not matches:
        return None
    matches.sort(key=_safe_mtime, reverse=True)
    info = _extract(matches[0])
    if info is None:
        return None
    open_ids, cwd_counts = scan_live() if live is None else live
    return _annotate_live(info, open_ids, cwd_counts)
