# Cross-Runtime Task Nudge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude와 Codex가 같은 read-only 포트폴리오 판정과 Task 선택 정책을 사용하고, 첫 실질 변경 후보에서 세션당 최대 한 번 안전하게 안내하도록 만든다.

**Architecture:** `hooks/task_nudge.py`가 Git identity, portfolio projection, 정책, 원자적 세션 상태를 소유하고 두 Python adapter가 runtime JSON 입출력만 변환한다. 별도 Python installer가 neutral hook 사본, Claude/Codex hook JSON, 활성 전역 AGENTS marker를 하나의 검증·rollback 단위로 병합하며 기존 `install.sh`는 그 helper만 호출한다.

**Tech Stack:** Python 3.10 standard library, pytest, Bash compatibility shim, JSON hook configuration, shellcheck.

**Spec:** `docs/superpowers/specs/2026-08-31-cross-runtime-task-nudge-design.md`

## Global Constraints

- 모든 개발 shell 명령은 `rtk`로 시작한다.
- Project Control 자동 호출은 정확히 `$HOME/.local/bin/jhw-control-host portfolio status` 하나이며 shell을 통하지 않는다.
- Launcher stdin은 비우고 timeout은 15초, stdout/stderr capture 상한은 각각 12 KiB다.
- Runtime hook stdin은 최대 1 MiB이며 초과·invalid JSON은 `HOOK_INPUT_INVALID`로 fail closed한다.
- 완전한 portfolio 결과에서 slug가 없을 때만 `unregistered`; truncated, malformed, timeout, nonzero exit는 `unknown`이다.
- `unknown`과 deterministic skip은 완료 marker를 만들지 않는다. 정상 결과만 runtime/session당 최대 한 번 출력한다.
- Codex native matcher는 `apply_patch|Edit|Write`, Claude matcher는 `Edit|Write|NotebookEdit`다. Bash mutation parser는 만들지 않는다.
- Hook은 permission denial을 반환하지 않지만 `unknown`은 guidance에서 후속 실질 변경을 중단시키는 fail-closed 상태다.
- Issue 생성, Project/Repository 등록, Task start/finish를 hook이나 installer가 자동 실행하지 않는다.
- Portfolio pagination, raw `jhw-control`, credential file/env 조립과 trust bypass를 추가하지 않는다.
- Installer test는 temporary HOME만 사용하며 실제 사용자 HOME, credential store, live portfolio 또는 Codex trust를 건드리지 않는다.
- 각 Task는 RED 확인 후 최소 구현, focused PASS, commit 순서를 지킨다.

Before Task 1, record a clean baseline with:

```bash
rtk python3 -m pytest -q tests/test_hook_payload_guard.py tests/test_installer_private_config.py tests/test_jhw_control_host.py
rtk bash -n install.sh hooks/task-nudge.sh
rtk git status --short --branch
```

Expected: existing tests and Bash syntax pass; only the already committed design/plan history is present and the worktree is clean.

## Planned File Structure

```text
hooks/
  task_nudge.py                 # 공통 model, identity/portfolio 판정, 정책, 상태와 orchestration
  task-nudge-claude.py          # Claude PreToolUse stdin/stdout adapter
  task-nudge-codex.py           # Codex PreToolUse + stateless manual-check adapter
  task-nudge.sh                 # 기존 Claude command 경로를 보존하는 exec shim
scripts/
  install-task-nudge.py         # task-nudge 전용 preflight/stage/merge/rollback installer
install.sh                      # generic hook loop 예외와 전용 installer 호출
tests/task_nudge/
  conftest.py                   # 공통 module loader, temporary repo/launcher fixtures
  test_identity_portfolio.py    # remote slug, launcher envelope와 등록 상태
  test_events_state.py          # runtime payload, skip, state security/concurrency
  test_policy_adapters.py       # 정책 행렬, Claude/Codex/manual output
  test_installer.py             # JSON/AGENTS merge와 transaction rollback
  test_install_integration.py   # temporary HOME의 install.sh end-to-end
  test_guidance_contract.py     # 세 runtime guidance의 의미 동등성
tests/test_installer_private_config.py  # 기존 installer 기대값 migration
tests/test_jhw_control_host.py          # 전역 Task rule의 launcher-only 계약 갱신
claude-md/global-guidance.md             # 확장된 Task nudge 결정표
hooks/README.md                          # 두 runtime 동작과 manual fallback 운영 문서
README.md                                # 설치 결과와 /hooks trust 절차
```

---

### Task 1: Repository identity와 portfolio 등록 판정

**Files:**

- Create: `hooks/task_nudge.py`
- Create: `tests/task_nudge/conftest.py`
- Create: `tests/task_nudge/test_identity_portfolio.py`

**Interfaces:**

- Consumes: `/usr/bin/git`, `$HOME/.local/bin/jhw-control-host portfolio status`, launcher v4 projected envelope.
- Produces: `RegistrationStatus`, `RegistrationResult`, `RepositoryIdentity`, `parse_github_slug()`, `resolve_repository()`, `parse_portfolio_output()`, `query_registration()`.

- [ ] **Step 1: 공통 module loader와 remote slug RED 테스트를 작성한다**

`tests/task_nudge/conftest.py`에서 hyphen 없는 core module을 repo source에서 직접 import한다.

```python
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
```

`tests/task_nudge/test_identity_portfolio.py`에 exact accepted/rejected URL을 고정한다.

```python
import json
import pytest


@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        ("https://github.com/jhw7500/claude-config.git", "jhw7500/claude-config"),
        ("git@github.com:jhw7500/claude-config.git", "jhw7500/claude-config"),
        ("ssh://git@github.com/jhw7500/claude-config", "jhw7500/claude-config"),
        ("HTTPS://GITHUB.COM/JHW7500/Claude-Config.git", "jhw7500/claude-config"),
    ],
)
def test_parse_github_slug_accepts_exact_origin_forms(core, remote, slug):
    assert core.parse_github_slug(remote) == slug


@pytest.mark.parametrize(
    "remote",
    [
        "https://token@github.com/jhw7500/claude-config.git",
        "https://gitlab.com/jhw7500/claude-config.git",
        "git@github.com:jhw7500/extra/claude-config.git",
        "https://github.com/jhw7500/claude-config/issues",
        "https://github.com/jhw7500/claude config.git",
    ],
)
def test_parse_github_slug_rejects_ambiguous_or_sensitive_forms(core, remote):
    assert core.parse_github_slug(remote) is None
```

- [ ] **Step 2: Identity 테스트가 core 부재로 실패하는지 확인한다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_identity_portfolio.py -k parse_github_slug`

Expected: FAIL because `hooks/task_nudge.py` or `parse_github_slug` does not exist.

- [ ] **Step 3: 최소 identity model과 origin resolver를 구현한다**

`hooks/task_nudge.py`에 다음 public surface를 만들고 URL 전체 match 뒤 lower-case slug만 반환한다.

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Sequence
import json
import os
import re
import subprocess

MAX_CAPTURE_BYTES = 12 * 1024
LAUNCHER_TIMEOUT_SECONDS = 15
GIT = "/usr/bin/git"


class RegistrationStatus(str, Enum):
    REGISTERED = "registered"
    UNREGISTERED = "unregistered"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RepositoryIdentity:
    root: Path
    slug: str


@dataclass(frozen=True)
class RegistrationResult:
    status: RegistrationStatus
    repository_slug: str | None
    reason: str | None = None


HTTPS_REMOTE = re.compile(
    r"(?i)https://github\.com/"
    r"(?P<owner>[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?)/"
    r"(?P<repo>[a-z0-9_.-]{1,100}?)(?:\.git)?/?\Z"
)
SSH_REMOTE = re.compile(
    r"(?i)(?:git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?)/"
    r"(?P<repo>[a-z0-9_.-]{1,100}?)(?:\.git)?/?\Z"
)


def parse_github_slug(remote: str) -> str | None:
    if not isinstance(remote, str) or any(ord(char) < 32 or ord(char) == 127 for char in remote):
        return None
    match = HTTPS_REMOTE.fullmatch(remote) or SSH_REMOTE.fullmatch(remote)
    if match is None:
        return None
    return f"{match.group('owner')}/{match.group('repo')}".lower()
```

`resolve_repository(cwd, runner=subprocess.run)`는 `git -C cwd rev-parse --show-toplevel`과 `git -C root config --get remote.origin.url`만 argv list로 실행한다. Nonzero, stderr, empty/oversized output, non-absolute root 또는 invalid origin은 `RegistrationResult(UNKNOWN, None, "REPOSITORY_IDENTITY_UNKNOWN")`로 변환할 수 있는 내부 `NudgeError`를 발생시킨다. 다른 remote를 탐색하지 않는다.

- [ ] **Step 4: Remote identity focused 테스트를 통과시킨다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_identity_portfolio.py -k parse_github_slug`

Expected: PASS for all accepted and rejected forms.

- [ ] **Step 5: Portfolio hit/miss/truncation과 strict envelope RED 테스트를 작성한다**

같은 test file에 duplicate-free launcher envelope helper와 결정표를 추가한다.

```python


def portfolio_bytes(*, slugs=(), truncated=False):
    result = {
        "page_id": "page-1",
        "items": [],
        "repositories": [
            {"repo_id": f"repo-{index}", "slug": slug, "allow_public": False}
            for index, slug in enumerate(slugs, start=1)
        ],
        "truncated": truncated,
        "total_items": len(slugs),
    }
    if truncated:
        result["next_page_id"] = "page-2"
    return json.dumps({"command": "portfolio status", "result": result}).encode()


def test_portfolio_exact_hit_is_registered_even_when_truncated(core):
    result = core.parse_portfolio_output(
        portfolio_bytes(slugs=("JHW7500/CLAUDE-CONFIG",), truncated=True),
        "jhw7500/claude-config",
    )
    assert result.status is core.RegistrationStatus.REGISTERED


def test_portfolio_complete_miss_is_unregistered(core):
    result = core.parse_portfolio_output(portfolio_bytes(), "jhw7500/claude-config")
    assert result.status is core.RegistrationStatus.UNREGISTERED


def test_portfolio_truncated_miss_is_unknown(core):
    result = core.parse_portfolio_output(
        portfolio_bytes(slugs=("jhw7500/other",), truncated=True),
        "jhw7500/claude-config",
    )
    assert result == core.RegistrationResult(
        core.RegistrationStatus.UNKNOWN,
        "jhw7500/claude-config",
        "PORTFOLIO_RESULT_INCOMPLETE",
    )


def test_portfolio_rejects_duplicate_json_keys(core):
    raw = b'{"command":"portfolio status","command":"other","result":{}}'
    result = core.parse_portfolio_output(raw, "jhw7500/claude-config")
    assert result.reason == "PORTFOLIO_UNAVAILABLE"
```

Parameterized invalid cases must also cover command mismatch, non-object root/result, missing required key, unknown key, boolean `total_items`, inconsistent `truncated`/`next_page_id`, malformed repository object, oversized bytes and invalid UTF-8.

- [ ] **Step 6: Portfolio 테스트가 판정 함수 부재로 실패하는지 확인한다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_identity_portfolio.py -k portfolio`

Expected: FAIL because `parse_portfolio_output` is not defined.

- [ ] **Step 7: Strict portfolio parser와 bounded launcher query를 구현한다**

고유 JSON key loader와 결과 함수를 추가한다.

```python
def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _unknown(slug: str | None, reason: str) -> RegistrationResult:
    return RegistrationResult(RegistrationStatus.UNKNOWN, slug, reason)


def parse_portfolio_output(raw: bytes, slug: str) -> RegistrationResult:
    try:
        if len(raw) > MAX_CAPTURE_BYTES:
            raise ValueError("oversized output")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(payload, dict):
            raise ValueError("root is not an object")
        if set(payload) - {"command", "result", "journal_warning", "registration_record_warning"}:
            raise ValueError("unknown envelope key")
        if payload.get("command") != "portfolio status":
            raise ValueError("command mismatch")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("result is not an object")
        required = {"page_id", "items", "repositories", "truncated", "total_items"}
        if set(result) - (required | {"next_page_id"}) or not required.issubset(result):
            raise ValueError("result shape mismatch")
        truncated = result["truncated"]
        total_items = result["total_items"]
        if not isinstance(truncated, bool):
            raise ValueError("truncated is not boolean")
        if not isinstance(total_items, int) or isinstance(total_items, bool) or total_items < 0:
            raise ValueError("total_items is invalid")
        if ("next_page_id" in result) != truncated:
            raise ValueError("pagination evidence is inconsistent")
        repositories = result["repositories"]
        if not isinstance(repositories, list):
            raise ValueError("repositories is not a list")
        normalized = []
        for repository in repositories:
            if not isinstance(repository, dict) or set(repository) != {"repo_id", "slug", "allow_public"}:
                raise ValueError("repository shape mismatch")
            if not isinstance(repository["slug"], str) or not isinstance(repository["allow_public"], bool):
                raise ValueError("repository value mismatch")
            parsed = parse_github_slug(f"https://github.com/{repository['slug']}")
            if parsed is None:
                raise ValueError("repository slug is invalid")
            normalized.append(parsed)
        if slug.lower() in normalized:
            return RegistrationResult(RegistrationStatus.REGISTERED, slug.lower())
        if truncated:
            return _unknown(slug.lower(), "PORTFOLIO_RESULT_INCOMPLETE")
        return RegistrationResult(RegistrationStatus.UNREGISTERED, slug.lower())
    except (UnicodeDecodeError, ValueError, TypeError):
        return _unknown(slug, "PORTFOLIO_UNAVAILABLE")
```

`query_registration(identity, home, runner=subprocess.run)`는 exact launcher argv, `stdin=subprocess.DEVNULL`, `capture_output=True`, `timeout=15`, bytes mode를 사용한다. Timeout/nonzero/stderr/empty output은 raw data를 포함하지 않은 `PORTFOLIO_UNAVAILABLE`로 반환한다. `resolve_repository()`와 `query_registration()` 테스트에는 fake runner의 exact argv 및 timeout assertion을 넣는다.

Parser는 snippet의 공통 검증에 더해 `page_id`/`next_page_id`의 `page-[1-9][0-9]*` 형식, `items`의 list 및 projected item exact keys(`project_id`, `title`, `repo_ids`), 각 `repo_id`/`repository.repo_id`의 bounded string, `allow_public` boolean을 모두 확인한 뒤 slug 존재 여부를 본다. 즉 malformed item을 무시하고 registered로 진행하는 경로가 없어야 한다.

- [ ] **Step 8: Task 1 focused suite와 compile gate를 실행한다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_identity_portfolio.py`

Run: `rtk python3 -m py_compile hooks/task_nudge.py`

Expected: all tests PASS; compile exits 0.

- [ ] **Step 9: Identity와 portfolio classifier를 commit한다**

```bash
rtk git add hooks/task_nudge.py tests/task_nudge/conftest.py tests/task_nudge/test_identity_portfolio.py
rtk git commit -m "feat: classify task nudge repositories"
```

---

### Task 2: Runtime event, deterministic skip와 원자적 once state

**Files:**

- Modify: `hooks/task_nudge.py`
- Create: `tests/task_nudge/test_events_state.py`

**Interfaces:**

- Consumes: Task 1의 `RepositoryIdentity`, `RegistrationResult`, `query_registration()`.
- Produces: `Runtime`, `HookEvent`, `parse_claude_event()`, `parse_codex_event()`, `should_skip_event()`, `MarkerClaim`, `claim_session_marker()`, `evaluate_event()`.

- [ ] **Step 1: Claude/Codex payload와 patch target 추출 RED 테스트를 작성한다**

```python
from pathlib import Path


def test_claude_event_uses_file_path(core, tmp_path):
    payload = {
        "session_id": "session/opaque:value",
        "cwd": str(tmp_path),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "src" / "app.py")},
    }
    event = core.parse_claude_event(payload)
    assert event.runtime is core.Runtime.CLAUDE
    assert event.session_id == "session/opaque:value"
    assert event.target_paths == (tmp_path / "src" / "app.py",)


def test_codex_apply_patch_extracts_every_target(core, tmp_path):
    payload = {
        "session_id": "01a-test",
        "cwd": str(tmp_path),
        "tool_name": "apply_patch",
        "tool_input": {
            "command": "*** Begin Patch\n*** Update File: src/a.py\n*** Add File: src/b.py\n*** End Patch"
        },
    }
    event = core.parse_codex_event(payload)
    assert event.target_paths == (tmp_path / "src" / "a.py", tmp_path / "src" / "b.py")


def test_unparseable_patch_is_conservatively_a_candidate(core, tmp_path):
    payload = {
        "session_id": "01a-test",
        "cwd": str(tmp_path),
        "tool_name": "apply_patch",
        "tool_input": {"command": "not a patch header"},
    }
    assert core.parse_codex_event(payload).target_paths == ()
```

Add invalid tests for non-object payload/tool_input, missing session/cwd, unsupported tool name, relative cwd and NUL/control characters. `parse_claude_event()` and `parse_codex_event()` return `HookEvent` on success and raise typed `NudgeError("HOOK_INPUT_INVALID")` on failure; adapters convert that error to `RegistrationResult(UNKNOWN, None, "HOOK_INPUT_INVALID")` without creating state.

- [ ] **Step 2: Payload tests가 새 interfaces 부재로 실패하는지 확인한다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_events_state.py -k 'event or patch or payload'`

Expected: FAIL because event interfaces are undefined.

- [ ] **Step 3: Runtime event normalization과 deterministic skip을 구현한다**

```python
class Runtime(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"


@dataclass(frozen=True)
class HookEvent:
    runtime: Runtime
    session_id: str
    cwd: Path
    tool_name: str
    target_paths: tuple[Path, ...]


PATCH_PATH = re.compile(
    r"^\*\*\* (?:(?:Add|Update|Delete) File:|Move to:) "
    r"(?P<path>[^\x00-\x1f\x7f]+)$"
)


def extract_apply_patch_paths(command: str, cwd: Path) -> tuple[Path, ...]:
    paths = []
    for line in command.splitlines():
        match = PATCH_PATH.fullmatch(line)
        if match is not None:
            candidate = Path(match.group("path"))
            paths.append(candidate if candidate.is_absolute() else cwd / candidate)
    return tuple(paths)
```

`should_skip_event(event, home, env)`는 모든 known target이 `env["TMPDIR"]` 또는 기본 `/tmp`의 scratch root, `home/.claude`, `home/.codex`, `.omc`, `memory/*.md`, `HANDOFF*.md` 중 하나일 때만 true다. Target이 없거나 하나라도 project candidate면 false다. `Path.resolve()`로 symlink를 따라 privacy-sensitive path를 출력하지 말고 lexical absolute normalization과 `os.path.commonpath()`만 사용한다. Test adapter는 repository와 다른 `TMPDIR`을 넘겨 temporary Git repository가 scratch로 오인되지 않게 한다.

- [ ] **Step 4: Skip이 marker를 소비하지 않는 RED 테스트를 작성한다**

```python
import pytest


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/settings.json",
        ".codex/config.toml",
        "repo/.omc/state.json",
        "repo/memory/context.md",
        "repo/HANDOFF.session.md",
    ],
)
def test_support_paths_are_skipped(core, tmp_path, relative):
    home = tmp_path / "home"
    home.mkdir()
    event = core.HookEvent(
        core.Runtime.CODEX,
        "session-a",
        home / "repo",
        "Write",
        (home / relative,),
    )
    assert core.should_skip_event(event, home, {"TMPDIR": str(home / "scratch")})


def test_project_markdown_is_not_blanket_skipped(core, tmp_path):
    home = tmp_path / "home"
    target = home / "repo" / "docs" / "architecture.md"
    event = core.HookEvent(core.Runtime.CODEX, "session-a", target.parent, "Write", (target,))
    assert not core.should_skip_event(event, home, {"TMPDIR": str(home / "scratch")})
```

- [ ] **Step 5: Secure marker와 concurrent at-most-once RED 테스트를 작성한다**

```python
from concurrent.futures import ThreadPoolExecutor


def test_marker_hashes_opaque_session_and_only_one_caller_wins(core, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    env = {"XDG_RUNTIME_DIR": str(runtime)}
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda _: core.claim_session_marker(core.Runtime.CODEX, "../opaque/session", env=env),
                range(32),
            )
        )
    assert results.count(core.MarkerClaim.CLAIMED) == 1
    assert results.count(core.MarkerClaim.ALREADY_DONE) == 31
    marker_names = [path.name for path in runtime.rglob("*") if path.is_file()]
    assert marker_names and all("opaque" not in name and "/" not in name for name in marker_names)


def test_unsafe_state_roots_fail_closed(core, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o777)
    runtime.chmod(0o777)
    fallback = tmp_path / "fallback"
    fallback.mkdir(mode=0o777)
    fallback.chmod(0o777)
    result = core.claim_session_marker(
        core.Runtime.CODEX,
        "session-a",
        env={"XDG_RUNTIME_DIR": str(runtime), "TMPDIR": str(fallback)},
    )
    assert result is core.MarkerClaim.UNAVAILABLE
```

Also test current-UID ownership, symlink rejection, exact `0700` directory/`0600` marker modes, runtime namespace separation and existing marker behavior.

- [ ] **Step 6: Atomic marker와 event orchestration을 구현한다**

```python
class MarkerClaim(str, Enum):
    CLAIMED = "claimed"
    ALREADY_DONE = "already_done"
    UNAVAILABLE = "unavailable"


def marker_name(runtime: Runtime, session_id: str) -> str:
    import hashlib
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return f"{runtime.value}-{digest}"
```

Private directory helper는 `lstat()`으로 symlink를 거부하고 `st_uid == os.getuid()` 및 `(st_mode & 0o077) == 0`을 확인한다. 새 directory는 `0700`; marker는 `os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o600)`으로 생성한다. Safe XDG root가 아니면 UID suffix를 가진 TMPDIR child를 시도하고 둘 다 불가하면 `UNAVAILABLE`다.

`evaluate_event(event, home, env, runner)` 순서는 skip → identity → portfolio → marker다. Registered/unregistered에서 `CLAIMED`면 결과, `ALREADY_DONE`이면 `None`; unknown은 marker 없이 그대로 반환; marker unavailable은 slug를 보존한 `UNKNOWN/NUDGE_STATE_UNAVAILABLE`을 반환한다.

- [ ] **Step 7: Event/state focused suite를 통과시킨다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_events_state.py`

Run: `rtk python3 -m pytest -q tests/task_nudge/test_identity_portfolio.py`

Expected: both files PASS.

- [ ] **Step 8: Runtime event와 state를 commit한다**

```bash
rtk git add hooks/task_nudge.py tests/task_nudge/test_events_state.py
rtk git commit -m "feat: gate task nudge once per runtime session"
```

---

### Task 3: 정책 행렬과 Claude/Codex adapters

**Files:**

- Modify: `hooks/task_nudge.py`
- Create: `hooks/task-nudge-claude.py`
- Create: `hooks/task-nudge-codex.py`
- Modify: `hooks/task-nudge.sh`
- Modify: `tests/task_nudge/conftest.py`
- Create: `tests/task_nudge/test_policy_adapters.py`

**Interfaces:**

- Consumes: Task 2의 `evaluate_event()`와 `RegistrationResult`.
- Produces: `WorkKind`, `PolicyContext`, `SuggestedAction`, `suggest_action()`, `render_nudge_message()`, Claude plaintext adapter, Codex `systemMessage`/manual JSON adapter.

- [ ] **Step 1: 정책 precedence RED 테스트를 작성한다**

```python
import json
import pytest


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"status": "registered", "work": "excluded"}, "no_task"),
        ({"status": "unregistered", "work": "backlog"}, "github_issue_only"),
        ({"status": "unknown", "work": "backlog"}, "github_issue_only"),
        ({"status": "unknown", "work": "immediate"}, "stop_for_classification"),
        ({"status": "registered", "work": "immediate", "existing_issue": True}, "formal_issue_task"),
        ({"status": "registered", "work": "immediate", "recurring": True}, "formal_issue_task"),
        ({"status": "registered", "work": "immediate", "bounded": True}, "temporary_task"),
        ({"status": "registered", "work": "immediate"}, "no_task"),
        ({"status": "unregistered", "work": "immediate", "recurring": True}, "register_repository"),
        ({"status": "unregistered", "work": "immediate"}, "no_task"),
    ],
)
def test_policy_matrix(core, context, expected):
    policy = core.PolicyContext.from_strings(**context)
    assert core.suggest_action(policy).value == expected
```

Add assertions that recurring evidence is only explicit long-running language, existing Issue/plan/Handoff, or architectural multi-stage work; file count and repository presence are not evidence.

- [ ] **Step 2: 정책 테스트가 interfaces 부재로 실패하는지 확인한다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_policy_adapters.py -k policy`

Expected: FAIL because policy types are undefined.

- [ ] **Step 3: Pure policy types와 shared message를 구현한다**

```python
class WorkKind(str, Enum):
    EXCLUDED = "excluded"
    BACKLOG = "backlog"
    IMMEDIATE = "immediate"


class SuggestedAction(str, Enum):
    NO_TASK = "no_task"
    GITHUB_ISSUE_ONLY = "github_issue_only"
    FORMAL_ISSUE_TASK = "formal_issue_task"
    TEMPORARY_TASK = "temporary_task"
    REGISTER_REPOSITORY = "register_repository"
    STOP_FOR_CLASSIFICATION = "stop_for_classification"


@dataclass(frozen=True)
class PolicyContext:
    status: RegistrationStatus
    work: WorkKind
    recurring: bool = False
    existing_issue: bool = False
    bounded: bool = False

    @classmethod
    def from_strings(cls, *, status: str, work: str, recurring: bool = False,
                     existing_issue: bool = False, bounded: bool = False) -> "PolicyContext":
        return cls(RegistrationStatus(status), WorkKind(work), recurring, existing_issue, bounded)


def suggest_action(context: PolicyContext) -> SuggestedAction:
    if context.work is WorkKind.EXCLUDED:
        return SuggestedAction.NO_TASK
    if context.work is WorkKind.BACKLOG:
        return SuggestedAction.GITHUB_ISSUE_ONLY
    if context.status is RegistrationStatus.UNKNOWN:
        return SuggestedAction.STOP_FOR_CLASSIFICATION
    if context.status is RegistrationStatus.REGISTERED:
        if context.existing_issue or context.recurring:
            return SuggestedAction.FORMAL_ISSUE_TASK
        if context.bounded:
            return SuggestedAction.TEMPORARY_TASK
        return SuggestedAction.NO_TASK
    if context.recurring:
        return SuggestedAction.REGISTER_REPOSITORY
    return SuggestedAction.NO_TASK
```

`render_nudge_message(result)`는 `[TASK-NUDGE]`, safe slug/status 또는 bounded reason, 위 precedence, recurring evidence 3개, separate approvals 3개, subagent/already-decided skip을 한 canonical Korean text로 만든다. Path/session/internal IDs/raw errors는 함수 인자나 output에 넣지 않는다.

- [ ] **Step 4: Adapter subprocess RED 테스트와 fake repo/launcher fixtures를 작성한다**

`conftest.py`에 temporary HOME/Git repository, executable fake launcher와 adapter subprocess runners를 만든다.

```python
import json
import os
import subprocess
import sys


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["/usr/bin/git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "remote", "add", "origin",
         "https://github.com/jhw7500/claude-config.git"],
        check=True,
    )
    return repo


def install_fake_launcher(home, payload, exit_code=0):
    launcher = home / ".local" / "bin" / "jhw-control-host"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        "#!/usr/bin/python3\n"
        "import json, sys\n"
        f"payload = {payload!r}\n"
        "sys.stdout.write(json.dumps(payload))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    launcher.chmod(0o500)
    return launcher


@pytest.fixture
def registered_home(home):
    payload = {
        "command": "portfolio status",
        "result": {
            "page_id": "page-1",
            "items": [],
            "repositories": [
                {"repo_id": "repo-1", "slug": "jhw7500/claude-config", "allow_public": False}
            ],
            "truncated": False,
            "total_items": 1,
        },
    }
    install_fake_launcher(home, payload)
    runtime = home / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    (home / "scratch").mkdir(mode=0o700)
    return home


@pytest.fixture
def run_adapter():
    def invoke(name, payload, home):
        env = dict(
            os.environ,
            HOME=str(home),
            XDG_RUNTIME_DIR=str(home / "runtime"),
            TMPDIR=str(home / "scratch"),
        )
        return subprocess.run(
            [sys.executable, str(REPO / "hooks" / name)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
    return invoke


@pytest.fixture
def run_manual():
    def invoke(repo, home):
        env = dict(os.environ, HOME=str(home), TMPDIR=str(home / "scratch"))
        return subprocess.run(
            [
                sys.executable,
                str(REPO / "hooks" / "task-nudge-codex.py"),
                "--manual-check",
                "--cwd",
                str(repo),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
    return invoke
```

Adapter tests invoke source scripts with JSON stdin and isolated `HOME`, `XDG_RUNTIME_DIR`.

```python
def test_codex_adapter_emits_system_message_once(core, repo, registered_home, run_adapter):
    payload = {
        "session_id": "codex-session",
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": "*** Begin Patch\n*** Update File: src.py\n*** End Patch"},
    }
    first = run_adapter("task-nudge-codex.py", payload, registered_home)
    second = run_adapter("task-nudge-codex.py", payload, registered_home)
    assert first.returncode == 0 and second.returncode == 0
    assert json.loads(first.stdout)["systemMessage"].startswith("[TASK-NUDGE]")
    assert second.stdout == ""


def test_claude_adapter_preserves_plaintext_contract(repo, registered_home, run_adapter):
    payload = {
        "session_id": "claude-session",
        "cwd": str(repo),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / "src.py")},
    }
    result = run_adapter("task-nudge-claude.py", payload, registered_home)
    assert result.returncode == 0
    assert result.stdout.startswith("[TASK-NUDGE]")
    assert not result.stdout.lstrip().startswith("{")


def test_manual_check_is_stateless_bounded_json(repo, registered_home, run_manual):
    first = run_manual(repo, registered_home)
    second = run_manual(repo, registered_home)
    expected = {"repository_slug": "jhw7500/claude-config", "registration_status": "registered"}
    assert json.loads(first.stdout) == expected
    assert json.loads(second.stdout) == expected
```

Add tests for registered/unregistered messages, unknown repeat without marker, malformed input as `HOOK_INPUT_INVALID`, skip output silence, Codex JSON having only `systemMessage`, and secret/path/session canaries absent from both streams.

- [ ] **Step 5: Adapter tests가 entrypoint 부재로 실패하는지 확인한다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_policy_adapters.py -k 'adapter or manual'`

Expected: FAIL because adapter scripts are absent.

- [ ] **Step 6: Thin adapters와 compatibility shim을 구현한다**

Claude adapter는 `sys.stdin.buffer.read(1024 * 1024 + 1)`로 bounded stdin JSON object를 parse하고 `parse_claude_event()` → `evaluate_event()` → plaintext render 순서만 수행한다. Codex adapter의 hook mode도 같은 1 MiB limit와 순서 뒤 다음 exact shape를 출력한다.

```python
sys.stdout.write(json.dumps({"systemMessage": message}, ensure_ascii=False) + "\n")
```

Codex `--manual-check --cwd PATH` mode는 state와 event skip을 거치지 않고 `resolve_repository()`와 `query_registration()`만 호출한 뒤 다음 shape를 출력한다.

```python
projection = {
    "repository_slug": result.repository_slug,
    "registration_status": result.status.value,
}
if result.reason is not None:
    projection["reason"] = result.reason
sys.stdout.write(json.dumps(projection, ensure_ascii=False, sort_keys=True) + "\n")
```

모든 adapter exception은 raw exception 없이 bounded unknown output으로 바꾸고 exit 0을 유지한다. `hooks/task-nudge.sh`는 stdin을 읽지 않고 neutral installed adapter로 `exec`한다.

```bash
#!/usr/bin/env bash
exec /usr/bin/python3 "$HOME/.local/share/claude-config/hooks/task-nudge-claude.py"
```

- [ ] **Step 7: Policy와 adapter focused suite를 통과시킨다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_policy_adapters.py`

Run: `rtk bash -n hooks/task-nudge.sh`

Run: `rtk python3 -m py_compile hooks/task_nudge.py hooks/task-nudge-claude.py hooks/task-nudge-codex.py`

Expected: tests PASS; syntax/compile gates exit 0.

- [ ] **Step 8: Shared policy와 adapters를 commit한다**

```bash
rtk git add hooks/task_nudge.py hooks/task-nudge-claude.py hooks/task-nudge-codex.py hooks/task-nudge.sh tests/task_nudge/conftest.py tests/task_nudge/test_policy_adapters.py
rtk git commit -m "feat: add Claude and Codex task nudge adapters"
```

---

### Task 4: Task-nudge transactional installer

**Files:**

- Create: `scripts/install-task-nudge.py`
- Modify: `tests/task_nudge/conftest.py`
- Create: `tests/task_nudge/test_installer.py`

**Interfaces:**

- Consumes: repository root, target HOME, Task 3 source files and `render_nudge_message()` policy clauses.
- Produces: `merge_hook_config()`, `select_agents_path()`, `merge_agents_block()`, `agents_policy_block()`, `build_plan()`, `apply_transaction()`, installer CLI.

- [ ] **Step 1: Installer module fixture와 Claude/Codex JSON additive merge RED 테스트를 작성한다**

Add a second dynamic loader to `conftest.py` and register it in `sys.modules` before execution so dataclasses resolve their module.

```python
INSTALLER_PATH = REPO / "scripts" / "install-task-nudge.py"


@pytest.fixture(scope="session")
def installer():
    spec = importlib.util.spec_from_file_location("install_task_nudge", INSTALLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
```

```python
from pathlib import Path

import pytest


CLAUDE_COMMAND = "$HOME/.claude/hooks/task-nudge.sh"
CODEX_COMMAND = "/usr/bin/python3 $HOME/.local/share/claude-config/hooks/task-nudge-codex.py"


def test_merge_hook_config_preserves_unrelated_entries(installer):
    original = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-me"}]}
            ]
        },
        "theme": "dark",
    }
    merged = installer.merge_hook_config(
        original,
        matcher="apply_patch|Edit|Write",
        command=CODEX_COMMAND,
        legacy_commands=(),
        home=Path("/home/test"),
    )
    assert merged["theme"] == "dark"
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "keep-me"
    managed = [
        group for group in merged["hooks"]["PreToolUse"]
        if group.get("hooks") == [{"type": "command", "command": CODEX_COMMAND}]
    ]
    assert len(managed) == 1
    assert managed[0]["matcher"] == "apply_patch|Edit|Write"


def test_merge_hook_config_migrates_one_legacy_command_without_duplicate(installer):
    original = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": CLAUDE_COMMAND}]}
            ]
        }
    }
    once = installer.merge_hook_config(
        original,
        matcher="Edit|Write|NotebookEdit",
        command=CLAUDE_COMMAND,
        legacy_commands=(CLAUDE_COMMAND,),
        home=Path("/home/test"),
    )
    twice = installer.merge_hook_config(
        once,
        matcher="Edit|Write|NotebookEdit",
        command=CLAUDE_COMMAND,
        legacy_commands=(CLAUDE_COMMAND,),
        home=Path("/home/test"),
    )
    assert once == twice
```

Add RED cases for duplicate JSON keys, non-object hook groups, multi-hook managed group, contradictory duplicate managed commands and unknown hook record shape. These must raise `InstallError` before any write.

- [ ] **Step 2: JSON merge tests가 installer 부재로 실패하는지 확인한다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_installer.py -k hook_config`

Expected: FAIL because `scripts/install-task-nudge.py` is absent.

- [ ] **Step 3: Strict JSON loader와 managed hook merge를 구현한다**

`scripts/install-task-nudge.py`는 duplicate-key rejecting JSON loader를 사용하고 dict insertion order를 유지한다. Managed group은 exact one-command group만 갱신한다. Command normalization은 target HOME absolute prefix와 literal `$HOME`만 동등하게 취급하며 basename 부분 일치로 unrelated hook을 잡지 않는다.

Module constant `REPO = Path(__file__).resolve().parents[1]`은 source/test 기본값으로만 사용하고 CLI의 validated `--repo` 값이 실제 install source를 결정한다.

```python
def managed_group(matcher: str, command: str) -> dict[str, object]:
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    }


def normalize_command(command: object, home: Path) -> str | None:
    if not isinstance(command, str):
        return None
    return command.replace(str(home), "$HOME")
```

파일이 없으면 `{}`에서 시작하고, 최초 변경 시 `json.dumps(value, ensure_ascii=False, indent=2) + "\n"`으로 안정화한다. 두 번째 merge는 byte-identical이다.

- [ ] **Step 4: Active AGENTS와 marker corruption RED 테스트를 작성한다**

```python
def test_nonempty_override_is_active_agents_target(installer, home):
    codex = home / ".codex"
    codex.mkdir()
    (codex / "AGENTS.md").write_text("base\n", encoding="utf-8")
    override = codex / "AGENTS.override.md"
    override.write_text("override\n", encoding="utf-8")
    assert installer.select_agents_path(home) == override


def test_empty_override_falls_back_to_agents_md(installer, home):
    codex = home / ".codex"
    codex.mkdir()
    (codex / "AGENTS.override.md").write_text("\n", encoding="utf-8")
    assert installer.select_agents_path(home) == codex / "AGENTS.md"


def test_agents_marker_merge_preserves_outside_bytes(installer):
    original = "prefix\n<!-- local block -->\nsuffix\n"
    merged = installer.merge_agents_block(original, "managed policy\n")
    assert merged.startswith(original)
    assert merged.count(installer.AGENTS_START) == 1
    assert merged.count(installer.AGENTS_END) == 1


@pytest.mark.parametrize(
    "text",
    [
        "<!-- claude-config:task-nudge:START -->\n",
        "<!-- claude-config:task-nudge:END -->\n",
        "<!-- claude-config:task-nudge:END -->\n<!-- claude-config:task-nudge:START -->\n",
        "<!-- claude-config:task-nudge:START -->\na\n<!-- claude-config:task-nudge:START -->\nb\n<!-- claude-config:task-nudge:END -->\n",
    ],
)
def test_malformed_agents_markers_are_rejected(installer, text):
    with pytest.raises(installer.InstallError):
        installer.merge_agents_block(text, "managed policy\n")
```

- [ ] **Step 5: Active file selection과 exact marker merge를 구현한다**

Managed block에는 stateless checker command, native nudge를 이미 받았거나 선택을 끝낸 세션의 skip, excluded → backlog → registered immediate → unregistered immediate → unknown precedence, recurring evidence 3개와 별도 승인 3개를 모두 넣는다. Existing text가 newline으로 끝나지 않으면 정확히 한 newline을 보충한 뒤 block을 append한다. Exactly one valid pair만 replace하고 marker 밖 byte는 유지한다.

- [ ] **Step 6: Cross-file transaction과 rollback RED 테스트를 작성한다**

```python
def test_transaction_rolls_back_updated_and_created_targets(installer, tmp_path):
    existing = tmp_path / "settings.json"
    created = tmp_path / "hooks.json"
    existing.write_bytes(b"before\n")
    existing.chmod(0o600)
    writes = [
        installer.PlannedWrite(existing, b"after\n", 0o600, True),
        installer.PlannedWrite(created, b"created\n", 0o600, True),
    ]
    calls = 0
    real_replace = installer.os.replace

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        real_replace(source, target)

    with pytest.raises(installer.InstallError):
        installer.apply_transaction(writes, replace=fail_second, stamp="20260831010101")
    assert existing.read_bytes() == b"before\n"
    assert not created.exists()


def test_identical_plan_creates_no_backup(installer, tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"same\n")
    target.chmod(0o600)
    installer.apply_transaction(
        [installer.PlannedWrite(target, b"same\n", 0o600, True)],
        stamp="20260831010101",
    )
    assert list(tmp_path.glob("AGENTS.md.bak.*")) == []
```

Also test timestamped `0600` backup on real change, owner-only staged file, source validation before first target write, rollback restoring prior mode, and backup-name collision fail-closed behavior.

- [ ] **Step 7: Staged atomic apply와 rollback을 구현한다**

```python
@dataclass(frozen=True)
class PlannedWrite:
    path: Path
    data: bytes
    mode: int
    backup: bool
```

`build_plan(repo, home)` must read and validate all four source files, both JSON configs and the active AGENTS markers before returning writes. `apply_transaction()` filters byte-identical targets, stages every changed body in the destination directory, fsyncs and chmods each temp, creates backups for existing regular targets, then replaces in deterministic path order. A failure restores replaced targets in reverse order and removes targets that were absent before the phase. Raw file content must not appear in `InstallError`.

“Unchanged” requires a non-symlink regular target with identical bytes and exact mode. The legacy `~/.claude/hooks/task-nudge.sh` symlink therefore migrates once to the owner-only regular shim even when its target bytes match; the backup captures the previous content and later identical installs remain unchanged.

- [ ] **Step 8: Installer unit suite와 compile gate를 통과시킨다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_installer.py`

Run: `rtk python3 -m py_compile scripts/install-task-nudge.py`

Expected: all tests PASS; compile exits 0.

- [ ] **Step 9: Transactional installer를 commit한다**

```bash
rtk git add scripts/install-task-nudge.py tests/task_nudge/conftest.py tests/task_nudge/test_installer.py
rtk git commit -m "feat: add transactional task nudge installer"
```

---

### Task 5: `install.sh` integration과 temporary-HOME migration

**Files:**

- Modify: `install.sh`
- Modify: `tests/task_nudge/conftest.py`
- Modify: `tests/test_installer_private_config.py`
- Create: `tests/task_nudge/test_install_integration.py`

**Interfaces:**

- Consumes: Task 4 installer CLI `--repo PATH --home PATH`.
- Produces: neutral owner-only hook files, regular Claude shim, merged Claude/Codex hook JSON, active AGENTS block and explicit `/hooks` trust notice.

- [ ] **Step 1: Fresh install와 existing-config preservation RED 테스트를 작성한다**

Add this exact temporary-HOME installer fixture to `conftest.py`; the existing flat installer tests keep their current helper.

```python
INSTALL = REPO / "install.sh"


@pytest.fixture
def run_install():
    def invoke(home):
        env = dict(os.environ, HOME=str(home))
        return subprocess.run(
            ["/bin/bash", "-c", f'umask 022; exec /bin/bash "{INSTALL}"'],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    return invoke
```

The new integration test module imports `json`; its assertions are exact.

```python
import json


def test_fresh_install_adds_neutral_hooks_codex_config_and_agents(home, run_install):
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    neutral = home / ".local" / "share" / "claude-config" / "hooks"
    assert (neutral / "task_nudge.py").is_file()
    assert (neutral / "task-nudge-claude.py").is_file()
    assert (neutral / "task-nudge-codex.py").is_file()
    assert all((path.stat().st_mode & 0o077) == 0 for path in neutral.iterdir())
    codex = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    groups = codex["hooks"]["PreToolUse"]
    assert any(group["matcher"] == "apply_patch|Edit|Write" for group in groups)
    agents = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.count("<!-- claude-config:task-nudge:START -->") == 1
    assert "/hooks" in result.stdout


def test_install_preserves_unrelated_codex_and_agents_content(home, run_install):
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "keep"}]}]}, "ui": "keep"}),
        encoding="utf-8",
    )
    (codex / "AGENTS.md").write_text("keep-before\n", encoding="utf-8")
    result = run_install(home)
    assert result.returncode == 0, result.stderr
    assert json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["ui"] == "keep"
    assert "keep-before" in (codex / "AGENTS.md").read_text(encoding="utf-8")
```

Update the old link test to require `~/.claude/hooks/task-nudge.sh` to be a regular owner-executable shim whose bytes equal the repository shim; other hook links remain symlinks.

- [ ] **Step 2: Integration tests가 neutral install 부재로 실패하는지 확인한다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_install_integration.py tests/test_installer_private_config.py -k 'task_nudge or neutral or codex or links_are_still_created'`

Expected: FAIL because `install.sh` does not call the dedicated installer and still symlinks the old shim.

- [ ] **Step 3: Generic hook loop와 Claude inline wiring의 ownership을 분리한다**

In the `hooks/*.py hooks/*.sh` loop, skip exactly these names so they are owned only by `install-task-nudge.py`:

```bash
case "$(basename "$f")" in
  task_nudge.py|task-nudge-claude.py|task-nudge-codex.py|task-nudge.sh) continue ;;
esac
```

Remove only `tn`, `TN_MATCH` and `ensure("PreToolUse", tn, TN_MATCH)` from the existing inline settings Python. All unrelated Claude hooks remain byte-for-byte under the existing writer.

- [ ] **Step 4: Dedicated installer 호출을 `install.sh` 마지막 설정 phase에 추가한다**

After the existing Claude settings merge and before the completion banner, call the helper with the sanitized system Python.

```bash
/usr/bin/python3 "$REPO_DIR/scripts/install-task-nudge.py" \
  --repo "$REPO_DIR" \
  --home "$HOME"
echo "[install] Task nudge: Claude/Codex hook + active AGENTS block"
echo "[주의] Codex에서 /hooks를 열어 새 hook 또는 변경된 hash를 직접 검토·신뢰하세요."
```

Do not invoke the installed hook, launcher, `/hooks`, Codex CLI or credential provider during installation.

- [ ] **Step 5: Idempotence, override, corruption과 no-provider RED tests를 추가한다**

```python
def test_identical_reinstall_is_byte_stable_and_adds_no_backups(home, run_install):
    first = run_install(home)
    assert first.returncode == 0, first.stderr
    targets = [
        home / ".claude" / "settings.json",
        home / ".codex" / "hooks.json",
        home / ".codex" / "AGENTS.md",
    ]
    before = {path: path.read_bytes() for path in targets}
    backup_counts = {path: len(list(path.parent.glob(f"{path.name}.bak.*"))) for path in targets}
    second = run_install(home)
    assert second.returncode == 0, second.stderr
    assert {path: path.read_bytes() for path in targets} == before
    assert {path: len(list(path.parent.glob(f"{path.name}.bak.*"))) for path in targets} == backup_counts


def test_malformed_agents_marker_leaves_task_nudge_targets_unchanged(home, run_install):
    codex = home / ".codex"
    codex.mkdir(parents=True)
    agents = codex / "AGENTS.md"
    agents.write_text("<!-- claude-config:task-nudge:START -->\n", encoding="utf-8")
    before = agents.read_bytes()
    result = run_install(home)
    assert result.returncode != 0
    assert agents.read_bytes() == before
    assert not (codex / "hooks.json").exists()
```

Add tests for non-empty override selection, invalid/duplicate-key Codex JSON, contradictory managed hook, exactly one backup on change, launcher canary never executed, no trust-bypass literal in installed configs or output, and AGENTS marker policy containing all five precedence branches.

- [ ] **Step 6: Full installer integration suite를 통과시킨다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_install_integration.py tests/test_installer_private_config.py`

Run: `rtk bash -n install.sh hooks/task-nudge.sh`

Run: `rtk shellcheck -x -s bash -S error install.sh hooks/task-nudge.sh scripts/lib/private-file.sh scripts/lib/link-safely.sh`

Expected: tests PASS; bash and shellcheck exit 0.

- [ ] **Step 7: Installer integration을 commit한다**

```bash
rtk git add install.sh tests/task_nudge/conftest.py tests/test_installer_private_config.py tests/task_nudge/test_install_integration.py
rtk git commit -m "feat: install cross-runtime task nudge safely"
```

---

### Task 6: Runtime guidance, operator docs와 전체 회귀 검증

**Files:**

- Modify: `claude-md/global-guidance.md`
- Modify: `hooks/README.md`
- Modify: `README.md`
- Modify: `tests/test_jhw_control_host.py`
- Create: `tests/task_nudge/test_guidance_contract.py`

**Interfaces:**

- Consumes: Task 3 canonical policy text, Task 4 AGENTS managed block, Task 5 installed paths.
- Produces: Claude/Codex/fallback 의미 동등성 contract와 사용자 설치/trust 안내.

- [ ] **Step 1: 세 guidance surface의 정책 동등성 RED 테스트를 작성한다**

`test_guidance_contract.py`는 source hook message, installer AGENTS block, Claude global rule에서 다음 exact concepts를 모두 요구한다. 먼저 세 surface를 live HOME 없이 구성한다.

```python
import pytest


@pytest.fixture
def guidance_surfaces(core, installer):
    guidance = (installer.REPO / "claude-md" / "global-guidance.md").read_text(encoding="utf-8")
    start = guidance.index("9. **Task 등록 권유")
    end = guidance.index("\n---", start)
    result = core.RegistrationResult(
        core.RegistrationStatus.REGISTERED,
        "jhw7500/claude-config",
    )
    return {
        "claude-global": guidance[start:end],
        "native-hook": core.render_nudge_message(result),
        "codex-agents": installer.agents_policy_block(),
    }


@pytest.mark.parametrize(
    "required",
    [
        "Formal Issue Task",
        "Temporary Task",
        "Task 없이",
        "GitHub Issue",
        "Project/Repository 등록",
        "unknown",
        "기존 Issue",
        "계획",
        "Handoff",
        "아키텍처",
        "별도 승인",
        "서브에이전트",
    ],
)
def test_all_guidance_surfaces_share_task_policy(guidance_surfaces, required):
    for name, text in guidance_surfaces.items():
        assert required in text, f"{name} misses {required}"


def test_backlog_policy_never_preclaims(guidance_surfaces):
    for text in guidance_surfaces.values():
        assert "backlog" in text
        assert "Issue만" in text
        assert "Task/Claim" in text


def test_guidance_never_assembles_credentials_or_raw_control(guidance_surfaces):
    forbidden = ("source control.env", "jhw-control task", "GH_PROJECT_TOKEN", "NOTION_API_KEY")
    for text in guidance_surfaces.values():
        assert all(value not in text for value in forbidden)
```

The fixture must not inspect a live HOME. `scripts/install-task-nudge.py` exposes its repository root as `REPO = Path(__file__).resolve().parents[1]`, so the test reads only checked-in source.

- [ ] **Step 2: Guidance contract가 기존 한 줄 규칙에서 실패하는지 확인한다**

Run: `rtk python3 -m pytest -q tests/task_nudge/test_guidance_contract.py tests/test_jhw_control_host.py -k 'guidance or task_policy'`

Expected: FAIL because current Claude rule lacks unregistered/backlog/unknown behavior and Codex fallback text.

- [ ] **Step 3: `global-guidance.md` rule 9를 approved precedence로 교체한다**

Use a short leading rule plus nested bullets, preserving the approved lifecycle commands. The rule must say:

```text
[TASK-NUDGE] 또는 Codex AGENTS fallback을 받으면 이미 선택한 세션과 제외 작업을 먼저 건너뛴다.
backlog는 등록 여부와 무관하게 GitHub Issue만 별도 승인 후 제안하고 Task/Claim을 선점하지 않는다.
registered immediate는 Formal Issue Task / Temporary Task / Task 없이 중 하나를 추천한다.
unregistered immediate는 반복·다중 세션 증거가 있을 때 Project/Repository 등록만 별도 승인으로 제안한다.
unknown은 등록 여부를 가정하지 않고 분류가 복구될 때까지 후속 실질 변경을 멈춘다.
```

Keep exact secure execution strings for approved actions: `"$HOME/.local/bin/jhw-control-host" preflight`, `task start --resolve-from-checkout true`, existing-task start and `task finish`. Keep raw CLI, credential, pagination and arbitrary Project fallback prohibitions.

- [ ] **Step 4: README와 hook 운영 문서를 갱신한다**

`README.md` installation section must document:

- task-nudge files are owner-only neutral copies and require `./install.sh` after source update,
- Claude settings and Codex `~/.codex/hooks.json` are additive merges,
- active global AGENTS marker preserves outside content,
- Codex `/hooks` review/trust is manual and changed hashes require review,
- installer never invokes portfolio or credentials.

`hooks/README.md` file table must list the core and both adapters, native matcher scopes, stateless manual-check command, at-most-once state and unknown retry. Revise the blanket “all hooks silently fail” wording so task-nudge bounded unknown output is an explicit exception while all hooks still exit 0 and never permission-deny.

- [ ] **Step 5: 기존 launcher guidance contract test를 새 multi-line rule에 맞춘다**

In `tests/test_jhw_control_host.py`, extract the entire rule 9 section rather than only its first line. Preserve these assertions:

```python
assert '"$HOME/.local/bin/jhw-control-host" preflight' in task_rule
assert '"$HOME/.local/bin/jhw-control-host" task start' in task_rule
assert '--resolve-from-checkout true' in task_rule
assert '"$HOME/.local/bin/jhw-control-host" task finish' in task_rule
assert 'jhw-control-host" portfolio status' not in task_rule
assert 'jhw-control task start' not in task_rule
assert 'jhw-control task finish' not in task_rule
assert "control.env" not in task_rule
assert "credential" not in task_rule.lower()
```

Add assertions for `registered`, `unregistered`, `unknown`, backlog Issue-only, recurring evidence and three separate approvals. Portfolio lookup remains the hook engine's job, so the direct command stays absent from guidance.

- [ ] **Step 6: Focused policy, adapter, installer와 launcher guidance tests를 실행한다**

Run: `rtk python3 -m pytest -q tests/task_nudge tests/test_installer_private_config.py tests/test_jhw_control_host.py -k 'task_nudge or guidance or global_task or installer or portfolio'`

Expected: all selected tests PASS.

- [ ] **Step 7: 전체 static와 repository regression gates를 실행한다**

Run: `rtk python3 -m py_compile hooks/task_nudge.py hooks/task-nudge-claude.py hooks/task-nudge-codex.py scripts/install-task-nudge.py`

Run: `rtk bash -n install.sh hooks/task-nudge.sh`

Run: `rtk shellcheck -x -s bash -S error install.sh hooks/task-nudge.sh scripts/lib/private-file.sh scripts/lib/link-safely.sh`

Run: `rtk python3 -m pytest -q`

Run: `rtk git diff --check`

Expected: every command exits 0; the full pytest count has no failures, errors or skips newly introduced by this change.

- [ ] **Step 8: Guidance와 final regression 결과를 commit한다**

```bash
rtk git add claude-md/global-guidance.md hooks/README.md README.md tests/test_jhw_control_host.py tests/task_nudge/test_guidance_contract.py
rtk git commit -m "docs: align task nudge policy across runtimes"
```

- [ ] **Step 9: 구현 branch가 review-ready인지 확인한다**

Run: `rtk git status --short --branch`

Run: `rtk git log --oneline --decorate -7`

Expected: clean task branch with the design commit, plan commit and six independently reviewable implementation commits; no user HOME or live Project Control state was changed.
