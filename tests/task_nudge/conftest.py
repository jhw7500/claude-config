from pathlib import Path
import importlib.util
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
CORE_PATH = REPO / "hooks" / "task_nudge.py"


@pytest.fixture(scope="session")
def core():
    spec = importlib.util.spec_from_file_location("task_nudge", CORE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
