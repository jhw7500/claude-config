from pathlib import Path
import sys

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "codex-mcp-ownership"
sys.path.insert(0, str(PACKAGE_ROOT))


@pytest.fixture
def fake_proc(tmp_path):
    from helpers import FakeProcTree

    return FakeProcTree(tmp_path / "proc")
