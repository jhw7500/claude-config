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

    @property
    def folder(self) -> str:
        return os.path.basename(self.cwd.rstrip("/")) or self.cwd


def _first_user_text(content) -> str | None:
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text")
        return None
    if isinstance(content, str):
        return content
    return None


def _extract(path: str) -> SessionInfo | None:
    session_id = os.path.splitext(os.path.basename(path))[0]
    cwd = ""
    title = ""
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
                if not title and d.get("type") == "user":
                    text = _first_user_text((d.get("message") or {}).get("content"))
                    if text and not text.startswith("<"):
                        title = " ".join(text.split())[:80]
                if cwd and title:
                    break
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if not cwd:
        return None
    return SessionInfo(session_id, cwd, mtime, title or "(no title)")


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
