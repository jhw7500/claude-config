from pathlib import Path
import importlib.util
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "scripts" / "lib" / "runtime_hook_installer.py"


@pytest.fixture(scope="session")
def common_installer():
    spec = importlib.util.spec_from_file_location("runtime_hook_installer", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
