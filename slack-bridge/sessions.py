"""Discover resumable Claude Code sessions under ~/.claude/projects."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
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
        if os.path.isdir(os.path.join(d, ".git")):
            return os.path.basename(d)
        d = os.path.dirname(d)
    return ""


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


def list_sessions(projects_dir: str, limit: int = 15) -> list[SessionInfo]:
    infos = []
    for p in glob(os.path.join(projects_dir, "*", "*.jsonl")):
        info = _extract(p)
        if info is not None:
            infos.append(info)
    infos.sort(key=lambda s: s.mtime, reverse=True)
    return infos[:limit]


def find_session(projects_dir: str, session_id: str) -> SessionInfo | None:
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    exact = glob(os.path.join(projects_dir, "*", session_id + ".jsonl"))
    if exact:
        return _extract(exact[0])
    matches = glob(os.path.join(projects_dir, "*", session_id + "*.jsonl"))
    if not matches:
        return None
    matches.sort(key=os.path.getmtime, reverse=True)
    return _extract(matches[0])
