import re
from pathlib import Path


SKILL = Path(__file__).parents[1] / "skills" / "task-observer" / "SKILL.md"
CANONICAL_PRINCIPLES_PATH = "skill-observations/cross-cutting-principles.md"


def test_session_start_uses_canonical_principles_path():
    """Catch a startup rule that sends principles outside the review workspace."""
    skill = SKILL.read_text()
    session_start = skill.split("## Session Start Protocol", 1)[1].split(
        "## When to Observe", 1
    )[0]
    mentioned_paths = [
        path.removeprefix("[workspace folder]/")
        for path in re.findall(r"`([^`\n]*cross-cutting-principles\.md)`", session_start)
    ]

    assert mentioned_paths == [CANONICAL_PRINCIPLES_PATH]
