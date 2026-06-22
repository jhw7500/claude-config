"""Run a single headless Claude Code turn against a session."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass

from config import ACTIVE_THRESHOLD_SECONDS, DENY_TOOLS


@dataclass(frozen=True)
class TurnResult:
    ok: bool
    text: str
    session_id: str
    cost_usd: float
    denials: list[str]
    raw: dict


def build_command(session_id: str, prompt: str, *, fork: bool = False) -> list[str]:
    cmd = [
        "claude", "-p", prompt,
        "--resume", session_id,
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
    ]
    for rule in DENY_TOOLS:
        cmd += ["--disallowedTools", rule]
    if fork:
        cmd.append("--fork-session")
    return cmd


def is_active(mtime: float, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return (now - mtime) < ACTIVE_THRESHOLD_SECONDS


def parse_result(stdout: str) -> TurnResult:
    data = json.loads(stdout)
    denials = []
    for d in data.get("permission_denials", []) or []:
        if isinstance(d, dict):
            denials.append(d.get("tool_name") or d.get("tool") or json.dumps(d))
        else:
            denials.append(str(d))
    return TurnResult(
        ok=not data.get("is_error", False),
        text=data.get("result", "") or "",
        session_id=data.get("session_id", "") or "",
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        denials=denials,
        raw=data,
    )


def run_turn(session_id: str, cwd: str, prompt: str, *, fork: bool = False,
             timeout: int = 1800) -> TurnResult:
    cmd = build_command(session_id, prompt, fork=fork)
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    if not proc.stdout.strip():
        raise RuntimeError(
            f"claude produced no output (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:500]}"
        )
    return parse_result(proc.stdout)
