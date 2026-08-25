import re
import shlex
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "pytest.yml"
DIRECT_REQUIREMENTS_PATH = ROOT / "requirements-test.in"
LOCK_PATH = ROOT / "requirements-test.lock"
RUNTIME_REQUIREMENTS_PATH = ROOT / "slack-bridge" / "requirements.txt"
SHA256_HASH_OPTION = re.compile(r"\s+--hash=sha256:([0-9a-f]{64})")


def _load_requirements(path):
    requirements = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        requirements[canonicalize_name(requirement.name)] = requirement
    return requirements


def _logical_requirement_lines(path):
    parts = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        parts.append(line[:-1].strip() if continued else line)
        if not continued:
            yield " ".join(parts)
            parts = []
    assert not parts, f"unterminated requirement in {path.name}"


def _load_hashed_lock(path):
    requirements = {}
    for line in _logical_requirement_lines(path):
        hashes = SHA256_HASH_OPTION.findall(line)
        assert hashes, f"unhashed requirement in {path.name}: {line}"
        assert len(hashes) == len(set(hashes))
        requirement_text = SHA256_HASH_OPTION.sub("", line).strip()
        assert "--hash" not in requirement_text
        requirement = Requirement(requirement_text)
        name = canonicalize_name(requirement.name)
        assert name not in requirements
        requirements[name] = requirement
    return requirements


def _exact_pin(requirement):
    specifiers = list(requirement.specifier)
    assert len(specifiers) == 1
    assert specifiers[0].operator == "=="
    assert not specifiers[0].version.endswith(".*")
    return specifiers[0].version


def _commands(step):
    return [
        shlex.split(line)
        for line in step.get("run", "").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _assert_unconditional_required_gate(element, label):
    assert "if" not in element, f"{label} must not have an if condition"
    continue_on_error = str(element.get("continue-on-error", "false")).lower()
    assert continue_on_error != "true", f"{label} must not continue on error"


def test_pytest_workflow_is_an_unfiltered_reproducible_required_gate():
    assert WORKFLOW_PATH.is_file(), "the repository has no pytest workflow"
    assert LOCK_PATH.is_file(), "the repository has no hashed pytest lock"
    assert DIRECT_REQUIREMENTS_PATH.is_file(), "the direct pytest inputs are missing"
    workflow = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert workflow["name"] == "Pytest"
    triggers = workflow["on"]
    assert triggers["push"]["branches"] == ["master"]
    assert "pull_request" in triggers
    pull_request = triggers["pull_request"]
    assert pull_request == "" or pull_request == {}
    assert "paths" not in triggers["push"]
    assert "paths-ignore" not in triggers["push"]

    pytest_job = workflow["jobs"]["pytest"]
    assert pytest_job["name"] == "Pytest"
    _assert_unconditional_required_gate(pytest_job, "pytest job")
    steps = pytest_job["steps"]

    setup_python_steps = [
        step for step in steps if step.get("uses", "").startswith("actions/setup-python@")
    ]
    assert len(setup_python_steps) == 1
    assert setup_python_steps[0]["with"]["python-version"] == "3.10"

    step_commands = [
        (step, command)
        for step in steps
        for command in _commands(step)
    ]
    install_steps = [
        (step, command)
        for step, command in step_commands
        if command[:4] == ["python", "-m", "pip", "install"]
    ]
    assert len(install_steps) == 1
    install_step, install_command = install_steps[0]
    _assert_unconditional_required_gate(install_step, "dependency install step")
    assert "--require-hashes" in install_command
    requirement_files = [
        install_command[index + 1]
        for index, token in enumerate(install_command[:-1])
        if token in {"-r", "--requirement"}
    ]
    assert requirement_files == ["requirements-test.lock"]

    test_steps = [
        (step, command)
        for step, command in step_commands
        if command[:3] == ["python", "-m", "pytest"]
    ]
    assert len(test_steps) == 1
    test_step, test_command = test_steps[0]
    _assert_unconditional_required_gate(test_step, "pytest step")
    assert test_command == ["python", "-m", "pytest", "-q"]

    direct_requirements = _load_requirements(DIRECT_REQUIREMENTS_PATH)
    runtime_requirements = _load_requirements(RUNTIME_REQUIREMENTS_PATH)
    required_names = {"packaging", "pytest", "pyyaml", *runtime_requirements}
    assert direct_requirements.keys() == required_names
    for requirement in direct_requirements.values():
        _exact_pin(requirement)
    for name, runtime_requirement in runtime_requirements.items():
        pinned_version = _exact_pin(direct_requirements[name])
        assert runtime_requirement.specifier.contains(pinned_version)

    locked_requirements = _load_hashed_lock(LOCK_PATH)
    expected_lock_names = {
        "exceptiongroup",
        "iniconfig",
        "packaging",
        "pluggy",
        "pygments",
        "pytest",
        "pyyaml",
        "slack-bolt",
        "slack-sdk",
        "tomli",
        "typing-extensions",
    }
    assert locked_requirements.keys() == expected_lock_names
    for name, requirement in locked_requirements.items():
        locked_version = _exact_pin(requirement)
        if name in direct_requirements:
            assert locked_version == _exact_pin(direct_requirements[name])
