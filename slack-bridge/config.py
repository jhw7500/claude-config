"""Configuration + safety constants for the Claude Code Slack bridge."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Destructive command patterns blocked even under acceptEdits.
# Claude Code permission-rule syntax: "Bash(<prefix>:*)".
# Verified live 2026-06-22: `--disallowedTools "Bash(rm:*)"` blocks rm under
# --permission-mode acceptEdits (permission_denials=['Bash'], target file survived).
DENY_TOOLS = [
    "Bash(rm:*)",
    "Bash(rmdir:*)",
    "Bash(git push --force:*)",
    "Bash(git push -f:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(mkfs:*)",
    "Bash(dd:*)",
    "Bash(shutdown:*)",
    "Bash(reboot:*)",
]

# A session whose transcript changed within this window is treated as
# "active" (possibly open in a TUI) -> require fork/confirm before resuming.
ACTIVE_THRESHOLD_SECONDS = 90


@dataclass(frozen=True)
class Config:
    bot_token: str
    app_token: str
    channel_id: str
    allowed_user_id: str
    projects_dir: str


def load_config() -> Config:
    def req(name: str) -> str:
        v = os.environ.get(name, "").strip()
        if not v:
            raise SystemExit(f"Missing required env var: {name}")
        return v

    return Config(
        bot_token=req("SLACK_BOT_TOKEN"),
        app_token=req("SLACK_APP_TOKEN"),
        channel_id=req("SLACK_CHANNEL_ID"),
        allowed_user_id=req("SLACK_ALLOWED_USER_ID"),
        projects_dir=os.path.expanduser(
            os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
        ),
    )
