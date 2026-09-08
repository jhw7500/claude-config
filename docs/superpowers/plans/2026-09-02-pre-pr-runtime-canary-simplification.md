# Pre-PR Runtime Canary Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude와 Codex의 실제 pre-PR hook wiring을 raw runtime output 없이 `missing=D/0`, `pass=A/1` marker로 검증하는 단일-process schema-v2 canary를 만든다.

**Architecture:** `scripts/probe-pre-pr-tribunal.py` 하나가 secure credential preflight, immutable control root, bubblewrap 경계, phase별 marker file과 runtime process group을 소유한다. Runtime stdout/stderr는 `/dev/null`로 폐기하고, Codex credential과 hooks config는 sealed memfd에서 bubblewrap tmpfs `CODEX_HOME`으로만 주입한다. 별도 supervisor, socket/thread recorder, custom signal handler와 runtime-output parser는 두지 않는다.

**Tech Stack:** Python 3.11 표준 라이브러리, Linux `memfd_create`/file seals, `/usr/bin/bwrap`, pytest, 기존 pre-PR tribunal installer와 adapters.

**Spec:** `docs/superpowers/specs/2026-09-02-pre-pr-runtime-canary-simplification-design.md`

## Global Constraints

- Production subsystem을 새로 만들지 않는다. Production 변경은 `scripts/probe-pre-pr-tribunal.py` 한 파일 안에 둔다.
- Runtime stdin/stdout/stderr는 모두 `/dev/null`이고 runtime output byte, hash, version, auth marker 또는 sensitivity를 읽지 않는다.
- Report schema는 `2`이며 public field는 `schema`, overall/runtime `status`, phase별 `runtime_exit`, `hook`, `gh_calls`뿐이다.
- Overall `status`는 모든 선택 runtime PASS이면 `PASS`, runtime-level failure가 있으면 `BLOCKED`, runtime dispatch 전 controller failure이면 해당 stable status다.
- Missing phase는 hook marker `D` 정확히 1개와 `gh` marker 0개, pass phase는 `A` 정확히 1개와 `V` 정확히 1개여야 한다.
- Marker log는 owner-only regular file, 최대 64 bytes이며 unknown, duplicate, invalid 또는 oversized evidence는 `CANARY_MISMATCH`다.
- `/usr/bin/bwrap`, read-only root/control root, GitHub hostname sinkhole, 모든 발견된 fixed `gh` bind-over와 exact command/cwd/tool guard는 필수다. 준비할 수 없으면 `ISOLATION_UNAVAILABLE`이다.
- Claude OAuth validity margin은 정확히 330초다: runtime 2×120초 + begin/finalize 2×30초 + scheduling cushion 30초.
- Codex auth는 host filesystem에 복사하지 않는다. `MFD_CLOEXEC | MFD_ALLOW_SEALING`과 `F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE`를 모두 요구한다.
- Codex `CODEX_HOME`은 bubblewrap tmpfs다. Auth는 `--perms 0600 --file`, hooks config는 별도 sealed FD의 `--ro-bind-data`로 배치한다.
- Subscription mode는 API key로 fallback하지 않는다. Explicit environment mode는 Claude에 `ANTHROPIC_API_KEY`, Codex에 `OPENAI_API_KEY` 하나만 전달한다.
- Stable runtime failure status는 `CREDENTIAL_UNAVAILABLE`, `RUNTIME_UNAVAILABLE`, `ISOLATION_UNAVAILABLE`, `RUNTIME_FAILED`, `TIMEOUT`, `CANARY_MISMATCH`, `SETUP_FAILED`, `CLEANUP_FAILED`뿐이다.
- Synthetic tests와 독립 scoped review에 Critical/Important finding이 없어야 live credential 또는 실제 Claude/Codex를 사용한다.
- 실 canary는 Claude를 먼저 실행하며 Claude PASS 전에는 Codex를 실행하지 않는다. 실제 GitHub endpoint는 어느 단계에서도 사용하지 않는다.
- 모든 repository shell command는 `/home/jhw/.codex/RTK.md`에 따라 `rtk`로 시작한다.

## File Structure

- `scripts/probe-pre-pr-tribunal.py`: CLI, secure credential read, sealed memfd, control/evidence fixture 생성, bubblewrap argv, runtime lifecycle, marker 판정과 schema-v2 report를 소유한다.
- `tests/pre_pr_tribunal/test_probe_harness.py`: synthetic credential과 fake Claude/Codex를 사용해 성공 matrix, auth 전달, memfd/tmpfs, isolation, failure와 signal lifecycle을 검증한다.
- `README.md`: repository-level canary 보안/사용 계약을 짧게 설명한다.
- `hooks/README.md`: operator용 CLI, schema v2, failure와 복구 절차를 설명한다.
- `docs/validation/2026-09-01-pre-pr-tribunal-canary.md`: 과거 실패 이력을 보존하고 새 synthetic/review/live 증거를 append-only로 기록한다.
- `docs/superpowers/specs/2026-09-02-pre-pr-runtime-canary-simplification-design.md`: 구현 뒤 상태만 갱신하며 architecture source of truth로 유지한다.

---

### Task 1: Environment-auth marker pipeline과 schema v2

**Files:**
- Modify: `tests/pre_pr_tribunal/test_probe_harness.py`
- Modify: `scripts/probe-pre-pr-tribunal.py`

**Interfaces:**
- Consumes: 기존 installer, installed Claude/Codex adapters, fake runtime executables, explicit provider API-key environment mode.
- Produces: `ProcessResult(returncode: int | None, exit_class: str)`, `_read_marker_log(path: Path, allowed: frozenset[str]) -> tuple[str, ...]`, `_phase_report(...) -> tuple[dict[str, object], bool]`, `_probe_runtime(...) -> dict[str, object]`.
- Produces report shape: `{"schema": 2, "status": "PASS|BLOCKED", "claude|codex": {"status": ..., "missing": ..., "pass": ...}}`.

- [ ] **Step 1: Fake runtime fixture를 marker-only 계약으로 축소한다**

`tests/pre_pr_tribunal/test_probe_harness.py`에서 version/leak/parser/round-6 mode와 helper를 제거한다. Fake runtime은 설치된 hook을 실행하고 deny가 아니면 fake `gh`를 호출하되 자신의 stdout/stderr는 판정에 필요 없는 noise로 취급한다.

```python
def _assert_phase(
    phase: dict[str, object],
    *,
    runtime_exit: str,
    hook: str,
    gh_calls: int,
) -> None:
    assert phase == {
        "runtime_exit": runtime_exit,
        "hook": hook,
        "gh_calls": gh_calls,
    }


def test_environment_canary_requires_missing_deny_then_pass_allow(
    fake_runtimes, tmp_path
):
    fake = fake_runtimes()
    result, work_dir = _run_probe(
        fake, tmp_path, runtime="all", auth_source="environment"
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert result.stderr == ""
    assert report["schema"] == 2
    assert report["status"] == "PASS"
    assert set(report) == {"schema", "status", "claude", "codex"}
    for runtime in ("claude", "codex"):
        assert report[runtime]["status"] == "PASS"
        _assert_phase(
            report[runtime]["missing"],
            runtime_exit="ZERO",
            hook="DENY",
            gh_calls=0,
        )
        _assert_phase(
            report[runtime]["pass"],
            runtime_exit="ZERO",
            hook="ALLOW",
            gh_calls=1,
        )
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("runtime", "expected_key", "forbidden_key"),
    [
        ("claude", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
        ("codex", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
    ],
)
def test_environment_mode_passes_only_the_selected_provider_key(
    fake_runtimes, tmp_path, runtime, expected_key, forbidden_key
):
    fake = fake_runtimes(**{runtime: "environment_isolation"})
    result, work_dir = _run_probe(
        fake,
        tmp_path,
        runtime=runtime,
        auth_source="environment",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)[runtime]["status"] == "PASS"
    assert expected_key in fake.env
    assert forbidden_key in fake.env
    assert not work_dir.exists()
```

- [ ] **Step 2: Runtime output이 증거와 report에서 완전히 제외되는 RED를 작성한다**

Fake runtime의 `noisy_output` mode는 stdout/stderr 각각 1 MiB와 `RUNTIME_OUTPUT_SENTINEL`을 쓰고 정상 hook/`gh` flow를 계속한다.

```python
@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_runtime_output_is_discarded_not_parsed_or_persisted(
    fake_runtimes, tmp_path, runtime
):
    fake = fake_runtimes(**{runtime: "noisy_output"})
    result, work_dir = _run_probe(
        fake,
        tmp_path,
        runtime=runtime,
        auth_source="environment",
    )

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report[runtime]["status"] == "PASS"
    assert "RUNTIME_OUTPUT_SENTINEL" not in result.stdout
    assert "version" not in result.stdout
    assert "capture" not in result.stdout
    assert "sha256" not in result.stdout
    assert "sensitivity" not in result.stdout
    assert not work_dir.exists()
```

- [ ] **Step 3: 새 contract가 현재 schema-v1 구현에서 RED인지 확인한다**

Run:

```bash
rtk env PYTHONPATH=/tmp/claude-config-issue32-test-deps.YXp69B python3 -m pytest -q \
  tests/pre_pr_tribunal/test_probe_harness.py \
  -k 'environment_canary or runtime_output_is_discarded'
```

Expected: schema가 `1`이고 기존 capture/version/parser path가 남아 있어 FAIL한다.

- [ ] **Step 4: Marker model과 bounded reader를 구현한다**

`scripts/probe-pre-pr-tribunal.py`의 `Capture`, `Sensitivity`, `Classification`, `_EvidenceRecorder`, output decoder/classifier와 version 함수들을 삭제하고 다음 최소 model을 둔다.

```python
SCHEMA_VERSION = 2
MARKER_LIMIT_BYTES = 64
HOOK_MARKERS = frozenset({"D", "A", "I"})
GH_MARKERS = frozenset({"V", "I"})


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    exit_class: str


def _read_marker_log(path: Path, allowed: frozenset[str]) -> tuple[str, ...]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > MARKER_LIMIT_BYTES
        ):
            raise ProbeFailure("CANARY_MISMATCH")
        raw = os.read(descriptor, MARKER_LIMIT_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(raw) > MARKER_LIMIT_BYTES or _credential_metadata(before) != _credential_metadata(after):
        raise ProbeFailure("CANARY_MISMATCH")
    try:
        markers = tuple(line for line in raw.decode("ascii").splitlines())
    except UnicodeDecodeError:
        raise ProbeFailure("CANARY_MISMATCH") from None
    if any(marker not in allowed for marker in markers):
        raise ProbeFailure("CANARY_MISMATCH")
    return markers


def _phase_report(
    result: ProcessResult,
    *,
    hook_log: Path,
    gh_log: Path,
    expected_hook: str,
    expected_gh_calls: int,
) -> tuple[dict[str, object], bool]:
    hooks = _read_marker_log(hook_log, HOOK_MARKERS)
    gh = _read_marker_log(gh_log, GH_MARKERS)
    hook = {("D",): "DENY", ("A",): "ALLOW"}.get(hooks, "INVALID")
    valid_calls = gh.count("V")
    phase = {
        "runtime_exit": result.exit_class,
        "hook": hook,
        "gh_calls": valid_calls,
    }
    matches = (
        result.exit_class == "ZERO"
        and hook == expected_hook
        and valid_calls == expected_gh_calls
        and "I" not in hooks
        and "I" not in gh
        and len(gh) == expected_gh_calls
    )
    return phase, matches
```

- [ ] **Step 5: Canary-only hook wrapper와 fake `gh`를 append-only marker writer로 바꾼다**

두 generated Python executable은 `os.open(..., O_APPEND | O_WRONLY | O_NOFOLLOW | O_CLOEXEC)`로 정확히 2-byte marker만 쓴다. Hook wrapper는 adapter stdout을 bounded capture한 뒤 그대로 runtime에 전달하고 다음 contract만 분류한다.

```python
def append_marker(path, marker):
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        if os.write(descriptor, marker.encode("ascii") + b"\n") != 2:
            raise OSError
    finally:
        os.close(descriptor)


def strict_native_deny(raw):
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
        output = value["hookSpecificOutput"]
        reason = output["permissionDecisionReason"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and isinstance(output, dict)
        and output.get("hookEventName") == "PreToolUse"
        and output.get("permissionDecision") == "deny"
        and isinstance(reason, str)
        and reason.startswith("[PRE-PR-TRIBUNAL:")
    )


adapter = subprocess.run(
    ["/usr/bin/python3", ADAPTER],
    input=sys.stdin.buffer.read(65537),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=30,
    check=False,
)
bounded = len(adapter.stdout) <= 65536 and len(adapter.stderr) <= 65536
if bounded and adapter.returncode == 0 and strict_native_deny(adapter.stdout):
    marker = "D"
elif bounded and adapter.returncode == 0 and adapter.stdout == b"":
    marker = "A"
else:
    marker = "I"
append_marker(os.environ["PRE_PR_PROBE_HOOK_LOG"], marker)
sys.stdout.buffer.write(adapter.stdout)
sys.stderr.buffer.write(adapter.stderr)
raise SystemExit(adapter.returncode)
```

Fake `gh`는 exact argv/cwd이면 `V`, 아니면 `I`를 기록하고 invalid call은 exit `64`로 끝낸다. Socket, thread, counter reset과 acknowledgement protocol은 만들지 않는다.

`_install_probe_guard()`는 installed adapter command를 immutable control root의 wrapper command로 정확히 한 번 교체하고 guard group을 앞에 넣는다. Wrapper argv의 adapter도 같은 immutable package의 absolute path다.

```python
wrapper_command = (
    f"/usr/bin/python3 {shlex.quote(str(wrapper))} "
    f"{runtime} {shlex.quote(str(package / adapter))}"
)
if replaced != 1:
    raise ProbeFailure("SETUP_FAILED")
```

- [ ] **Step 6: Runtime process는 output을 열지 않고 phase 종료 뒤 marker만 판정하게 한다**

```python
def _run_runtime(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    pass_fds: Sequence[int] = (),
) -> ProcessResult:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=tuple(pass_fds),
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_process_group(process)
            return ProcessResult(process.poll(), "TIMEOUT")
        return ProcessResult(returncode, "ZERO" if returncode == 0 else "NONZERO")
    finally:
        if process is not None and process.poll() is None:
            _stop_process_group(process)
```

각 phase 시작 전에 owner-private evidence directory와 mode `0600`의 빈 `hook.log`, `gh.log`를 만들고 해당 directory만 writable bind한다. `_probe_runtime()`은 missing/pass 순서로 실행하고 `RUNTIME_FAILED`와 `TIMEOUT`을 marker read보다 먼저 runtime status로 반환한다. 정상 exit에서는 `_phase_report()`가 돌려준 phase를 report에 먼저 넣고 `matches`가 false이면 `CANARY_MISMATCH`로 종료하므로 valid-but-unexpected `DENY/0` 같은 사실은 v2 field 안에서 보존된다.

- [ ] **Step 7: Obsolete runtime-output/signal subsystem이 제거됐는지 확인하고 focused GREEN을 만든다**

Run:

```bash
rtk rg -n 'Capture|Sensitivity|Classification|_EvidenceRecorder|_parse_runtime_output|_decoded_json_strings|_sensitivity|_version_capture|pthread_sigmask|signal\.signal|selectors|threading|AF_UNIX' \
  scripts/probe-pre-pr-tribunal.py tests/pre_pr_tribunal/test_probe_harness.py
rtk env PYTHONPATH=/tmp/claude-config-issue32-test-deps.YXp69B python3 -m pytest -q \
  tests/pre_pr_tribunal/test_probe_harness.py \
  -k 'environment_canary or runtime_output_is_discarded'
```

Expected: first command has no matches; focused tests PASS.

- [ ] **Step 8: Task 1을 커밋한다**

```bash
rtk git add scripts/probe-pre-pr-tribunal.py tests/pre_pr_tribunal/test_probe_harness.py
rtk git commit -m "refactor: replace runtime capture with marker evidence"
```

---

### Task 2: Secure subscription auth와 sealed Codex tmpfs

**Files:**
- Modify: `tests/pre_pr_tribunal/test_probe_harness.py`
- Modify: `scripts/probe-pre-pr-tribunal.py`

**Interfaces:**
- Consumes: descriptor-relative credential reader, marker pipeline, bubblewrap argv builder.
- Produces: `CredentialSnapshot`, `ClaudeSubscriptionAuth`, `CodexSubscriptionAuth`, `_sealed_memfd(name: str, data: bytes) -> Iterator[int]`.
- Produces sandbox inputs: `codex_auth_fd: int | None`, required `codex_hooks_fd: int`, and `pass_fds` containing only those live descriptors.
- Test helpers: `_start_probe(...) -> tuple[subprocess.Popen[str], Path, Path]`, `_wait_for(path: Path, process: subprocess.Popen[str]) -> None`, `_regular_files(root: Path) -> Iterator[Path]`.

- [ ] **Step 1: Subscription validation과 stable status RED를 작성한다**

Missing, symlink, group-readable, malformed/duplicate/oversized와 expired Claude source는 세부 원인을 report하지 않고 모두 `CREDENTIAL_UNAVAILABLE`이어야 한다. Expiry 경계는 330초로 고정한다.

```python
def test_claude_subscription_expiry_margin_is_exactly_330_seconds(
    fake_runtimes, monkeypatch
):
    module = _load_probe_module()
    fake = fake_runtimes()
    now = 2_000_000_000.0
    credential = fake.caller_home / ".claude/.credentials.json"
    credential.write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": fake.claude_token,
                "expiresAt": int((now + 329) * 1000),
            }
        }),
        encoding="utf-8",
    )
    credential.chmod(0o600)
    monkeypatch.setattr(module.time, "time", lambda: now)

    with pytest.raises(module.ProbeFailure) as raised:
        module._load_claude_subscription_auth(fake.caller_home)

    assert raised.value.code == "CREDENTIAL_UNAVAILABLE"
    assert module.AUTH_VALIDITY_MARGIN_SECONDS == 330
```

- [ ] **Step 2: Host-disk absence, Codex atomic refresh와 immutable hooks RED를 작성한다**

Fake Codex의 `pause_after_auth` mode는 namespace 안에서 `auth.json`을 읽고 temp-file + `os.replace()`로 갱신한 뒤 `TMPDIR/auth-ready`를 만든다. Fake executable source에는 auth document나 sentinel을 embed하지 않는다. Parent는 runtime이 대기하는 동안 host work/control tree의 모든 regular file을 읽어 unique credential sentinel이 없음을 확인하고 `hooks.json` write/replace가 모두 실패했음을 확인한다.

```python
def _start_probe(fake, tmp_path: Path, *, runtime: str):
    controller_tmp = tmp_path / "controller-tmp"
    controller_tmp.mkdir(mode=0o700)
    work_dir = tmp_path / "probe"
    release = work_dir / "tmp/auth-release"
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--runtime", runtime,
            "--repo-source", str(REPO),
            "--work-dir", str(work_dir),
        ],
        env=dict(fake.env, TMPDIR=str(controller_tmp)),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return process, work_dir, release


def _wait_for(path: Path, process, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            process.communicate(timeout=5)
            pytest.fail(f"probe did not create {path.name}")
        time.sleep(0.02)
    assert path.exists()


def _regular_files(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            yield path


def test_codex_auth_uses_memfd_tmpfs_without_host_disk_copy(
    fake_runtimes, tmp_path
):
    sentinel = "MEMFD_ONLY_CODEX_SENTINEL_32"
    auth = json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"access_token": sentinel},
    }).encode() + b"\n"
    fake = fake_runtimes(codex="pause_after_auth", codex_auth_document=auth)
    process, work_dir, release = _start_probe(fake, tmp_path, runtime="codex")
    _wait_for(work_dir / "tmp/auth-ready", process)

    roots = [
        work_dir,
        *tmp_path.glob("controller-tmp/pre-pr-tribunal-controls-*"),
    ]
    for root in roots:
        for path in _regular_files(root):
            assert sentinel.encode() not in path.read_bytes()

    release.write_text("continue", encoding="ascii")
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0
    assert stderr == ""
    assert json.loads(stdout)["codex"]["status"] == "PASS"
    assert not work_dir.exists()
```

- [ ] **Step 3: 새 auth tests가 disk staging과 450초 margin 때문에 RED인지 확인한다**

Run:

```bash
rtk env PYTHONPATH=/tmp/claude-config-issue32-test-deps.YXp69B python3 -m pytest -q \
  tests/pre_pr_tribunal/test_probe_harness.py \
  -k 'subscription or expiry_margin or memfd_tmpfs or source_bytes'
```

Expected: old stage path, old status 또는 450초 margin 때문에 FAIL한다.

- [ ] **Step 4: Secure source read는 유지하되 public credential 오류를 하나로 접는다**

`_read_secure_credential()`은 descriptor-relative/no-follow/current-UID/owner-only/bounded stable-read 계약을 유지한다. `_load_*_subscription_auth()`의 외부 경계에서는 내부 validation 오류를 다음과 같이 collapse한다.

```python
AUTH_VALIDITY_MARGIN_SECONDS = 330


@dataclass(frozen=True)
class ClaudeSubscriptionAuth:
    source: CredentialSnapshot
    access_token: str
    expires_at_ms: int


@dataclass(frozen=True)
class CodexSubscriptionAuth:
    source: CredentialSnapshot
    data: bytes


def _load_codex_subscription_auth(caller_home: Path) -> CodexSubscriptionAuth:
    try:
        source = _read_secure_credential(caller_home, ".codex", "auth.json")
        value = _strict_credential_json(source.data)
        if not isinstance(value, dict):
            raise ProbeFailure("CREDENTIAL_UNAVAILABLE")
        return CodexSubscriptionAuth(source=source, data=source.data)
    except ProbeFailure:
        raise ProbeFailure("CREDENTIAL_UNAVAILABLE") from None
```

Claude loader도 같은 boundary에서 internal credential 오류를 `CREDENTIAL_UNAVAILABLE`로 collapse한다. Token inventory, prefix extraction, refreshed-stage read와 leak matcher는 만들지 않는다. Source bytes/metadata는 runtime 종료 뒤 `_verify_credential_source_unchanged()`로 다시 확인하고 mismatch도 `CREDENTIAL_UNAVAILABLE`로 report한다.

- [ ] **Step 5: Exact-seal memfd context manager를 구현한다**

```python
@contextmanager
def _sealed_memfd(name: str, data: bytes):
    descriptor = -1
    try:
        required_flags = os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        required_seals = (
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE
        )
        descriptor = os.memfd_create(name, required_flags)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, required_seals)
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != required_seals:
            raise OSError
        yield descriptor
    except (AttributeError, OSError, ValueError):
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
```

Fallback file이나 unsealed FD는 허용하지 않는다.

- [ ] **Step 6: Codex bubblewrap overlay를 tmpfs + two-FD contract로 바꾼다**

`_sandbox_argv()`는 Codex일 때 다음 순서의 arguments를 만든다. `codex_hooks_fd`는 installer가 만든 최종 `hooks.json` bytes의 sealed FD이고 subscription mode에서만 `codex_auth_fd`가 추가된다.

```python
codex_home = control_root / "home/.codex"
arguments.extend(("--tmpfs", str(codex_home)))
if codex_auth_fd is not None:
    arguments.extend((
        "--perms", "0600",
        "--file", str(codex_auth_fd), str(codex_home / "auth.json"),
    ))
arguments.extend((
    "--ro-bind-data", str(codex_hooks_fd), str(codex_home / "hooks.json"),
))
```

각 bwrap child를 만들기 직전에 사용 중인 FD를 `os.lseek(fd, 0, os.SEEK_SET)`으로 rewind한다. 두 phase가 같은 open-file description을 재사용해도 두 번째 `--file`/`--ro-bind-data`가 EOF를 보지 않게 하는 필수 동작이다. `_run_sandboxed()`는 두 FD를 `_run_runtime(..., pass_fds=...)`까지 전달한다. Claude token은 argv나 file에 넣지 않고 Claude child env의 `CLAUDE_CODE_OAUTH_TOKEN`에만 둔다. Environment mode에서는 subscription FD/token을 만들지 않지만 Codex hooks FD와 tmpfs `CODEX_HOME`은 그대로 사용한다.

```python
def _rewind_memfds(*descriptors: int | None) -> tuple[int, ...]:
    active = tuple(value for value in descriptors if value is not None)
    try:
        for descriptor in active:
            os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError:
        raise ProbeFailure("ISOLATION_UNAVAILABLE") from None
    return active
```

- [ ] **Step 7: Subscription/memfd focused tests와 direct adapters를 GREEN으로 만든다**

Run:

```bash
rtk env PYTHONPATH=/tmp/claude-config-issue32-test-deps.YXp69B python3 -m pytest -q \
  tests/pre_pr_tribunal/test_probe_harness.py \
  tests/pre_pr_tribunal/test_gate_adapters.py
```

Expected: synthetic credentials/fake runtimes만 사용해 PASS한다. `auth-stage`, `CREDENTIAL_STAGING_FAILED`, token inventory와 capture classifier를 참조하는 test/code는 남지 않는다.

- [ ] **Step 8: Task 2를 커밋한다**

```bash
rtk git add scripts/probe-pre-pr-tribunal.py tests/pre_pr_tribunal/test_probe_harness.py
rtk git commit -m "fix: inject codex subscription auth through sealed memory"
```

---

### Task 3: Isolation, failure와 process lifecycle 회귀

**Files:**
- Modify: `tests/pre_pr_tribunal/test_probe_harness.py`
- Modify: `scripts/probe-pre-pr-tribunal.py`

**Interfaces:**
- Consumes: `_run_runtime`, `_sandbox_argv`, sealed auth/hooks FDs, marker logs, existing guard/hosts/fixed-`gh` boundary.
- Produces: exact stable failure mapping and normal/timeout/SIGINT/SIGTERM child-death evidence without custom signal state.
- Test helpers: `_wait_until_process_absent(pid: int, timeout: float) -> None`, `_tree_contains(root: Path, needle: bytes) -> bool`.

- [ ] **Step 1: Marker와 stable failure adversarial RED를 작성한다**

```python
def _inject_fault(module, monkeypatch, fault: str, tmp_path: Path) -> None:
    if fault == "missing_runtime":
        monkeypatch.setattr(module, "_resolve_runtime", lambda *_args: None)
    elif fault == "missing_bwrap":
        monkeypatch.setattr(module, "BWRAP_PATH", tmp_path / "absent-bwrap")
    elif fault == "installer_failure":
        def fail_install(*_args, **_kwargs):
            raise module.ProbeFailure("SETUP_FAILED")
        monkeypatch.setattr(module, "_install", fail_install)
    elif fault == "cleanup_failure":
        monkeypatch.setattr(module, "_cleanup_work_dir", lambda *_args: False)
    else:
        raise AssertionError(fault)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("nonzero", "RUNTIME_FAILED"),
        ("timeout", "TIMEOUT"),
        ("missing_hook", "CANARY_MISMATCH"),
        ("duplicate_hook", "CANARY_MISMATCH"),
        ("invalid_hook", "CANARY_MISMATCH"),
        ("oversized_hook", "CANARY_MISMATCH"),
        ("pass_absent", "CANARY_MISMATCH"),
        ("pass_duplicate", "CANARY_MISMATCH"),
        ("invalid_gh", "CANARY_MISMATCH"),
    ],
)
def test_runtime_failures_use_only_stable_v2_statuses(
    fake_runtimes, tmp_path, mode, expected
):
    fake = fake_runtimes(claude=mode)
    result, work_dir = _run_probe(fake, tmp_path, runtime="claude")
    report = json.loads(result.stdout)

    assert result.returncode != 0
    assert report["status"] == "BLOCKED"
    assert report["claude"]["status"] == expected
    assert set(report["claude"]) <= {"status", "missing", "pass"}
    assert not work_dir.exists()
```

Direct unit tests는 marker file의 wrong owner simulation, unsafe mode, symlink, unknown line, duplicate line와 65-byte input을 모두 `CANARY_MISMATCH`로 확인한다.

Top-level failure fixtures도 stable list 전체를 고정한다.

```python
@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("missing_runtime", "RUNTIME_UNAVAILABLE"),
        ("missing_bwrap", "ISOLATION_UNAVAILABLE"),
        ("installer_failure", "SETUP_FAILED"),
        ("cleanup_failure", "CLEANUP_FAILED"),
    ],
)
def test_top_level_failures_use_stable_status(
    fake_runtimes, tmp_path, monkeypatch, fault, expected
):
    module = _load_probe_module()
    fake = fake_runtimes()
    _inject_fault(module, monkeypatch, fault, tmp_path)
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = module.main([
            "--runtime", "claude",
            "--repo-source", str(REPO),
            "--work-dir", str(tmp_path / "probe"),
        ])

    assert exit_code != 0
    report = json.loads(stdout.getvalue())
    observed = report.get("claude", report)["status"]
    assert observed == expected
```

이 matrix는 credential validation의 `CREDENTIAL_UNAVAILABLE`와 phase tests의 `RUNTIME_FAILED`, `TIMEOUT`, `CANARY_MISMATCH`를 합쳐 stable status 8개를 모두 고정한다.

- [ ] **Step 2: Existing confinement attacks를 v2 marker assertion으로 보존한다**

```python
@pytest.mark.parametrize(
    "mode",
    ["absolute_gh", "path_reset", "command_p", "nonexact_hook", "github_client"],
)
def test_runtime_guard_keeps_existing_github_confinement(
    fake_runtimes, tmp_path, mode
):
    fake = fake_runtimes(claude=mode)
    result, work_dir = _run_probe(fake, tmp_path, runtime="claude")
    runtime = json.loads(result.stdout)["claude"]

    assert result.returncode != 0
    assert runtime["status"] == "CANARY_MISMATCH"
    assert runtime["pass"]["gh_calls"] == 0
    assert not work_dir.exists()
```

Isolation verifier는 `/usr/bin/gh`, PATH reset, `command -p`, GitHub hostname sinkhole, exact/nonexact guard, writable runtime directories, Codex auth atomic replace와 hooks write/rename failure를 실제 bubblewrap 안에서 실행한다.

- [ ] **Step 3: Timeout과 default signals의 child-death RED를 작성한다**

Fake runtime은 자신의 PID와 spawned descendant PID를 `TMPDIR/runtime-pids`에 기록한 뒤 대기한다. Test는 timeout, SIGINT, SIGTERM 각각 probe가 nonzero로 끝난 뒤 두 PID 모두 사라지는지 bounded poll한다.

```python
def _wait_until_process_absent(pid: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    pytest.fail(f"process {pid} survived")


def _tree_contains(root: Path, needle: bytes) -> bool:
    for path in _regular_files(root):
        try:
            if needle in path.read_bytes():
                return True
        except OSError:
            continue
    return False


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_default_signal_semantics_leave_no_runtime_or_credential_copy(
    fake_runtimes, tmp_path, signum
):
    sentinel = b"MEMFD_ONLY_CODEX_SENTINEL_32"
    auth = (
        b'{"auth_mode":"chatgpt","tokens":{"access_token":"'
        + sentinel
        + b'"}}\n'
    )
    fake = fake_runtimes(
        codex="wait_with_descendant",
        codex_auth_document=auth,
    )
    process, work_dir, _release = _start_probe(fake, tmp_path, runtime="codex")
    pid_file = work_dir / "tmp/runtime-pids"
    _wait_for(pid_file, process)
    pids = [int(value) for value in pid_file.read_text().splitlines()]

    os.kill(process.pid, signum)
    process.communicate(timeout=15)

    assert process.returncode != 0
    for pid in pids:
        _wait_until_process_absent(pid, timeout=5.0)
    assert not _tree_contains(work_dir, sentinel)
    shutil.rmtree(work_dir, ignore_errors=True)
```

SIGTERM/SIGKILL 뒤 non-secret work/control residue는 허용하므로 test가 identity-confined path만 정리한다. Success, ordinary failure, timeout과 SIGINT에서는 normal `finally` cleanup으로 work/control roots가 없어야 한다.

- [ ] **Step 4: 새 adversarial selectors가 RED인지 확인한다**

Run:

```bash
rtk env PYTHONPATH=/tmp/claude-config-issue32-test-deps.YXp69B python3 -m pytest -q \
  tests/pre_pr_tribunal/test_probe_harness.py \
  -k 'stable_v2 or confinement or default_signal or timeout or marker_log'
```

Expected: 아직 정리되지 않은 failure mapping 또는 lifecycle edge가 있으면 FAIL하며 모든 failure는 synthetic fixture에서 재현된다.

- [ ] **Step 5: Process lifecycle과 failure precedence를 최소 코드로 완성한다**

```python
def _runtime_failure(result: ProcessResult) -> str | None:
    if result.exit_class == "TIMEOUT":
        return "TIMEOUT"
    if result.exit_class != "ZERO":
        return "RUNTIME_FAILED"
    return None


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        process.wait(timeout=0.25)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.0)
```

`main()`은 ordinary `try/except/finally`만 사용한다. `KeyboardInterrupt`는 가능하면 sanitized `RUNTIME_FAILED`/nonzero를 emit하고 cleanup한다. SIGTERM에는 handler를 설치하지 않으며 bubblewrap `--die-with-parent`가 child를 종료한다. `_TerminationSignal`, `_TerminationState`, `_blocked_termination_signals`, signal mask/handler install/restore 코드는 다시 도입하지 않는다.

- [ ] **Step 6: Full harness와 관련 tribunal regression을 실행한다**

Run:

```bash
rtk env PYTHONPATH=/tmp/claude-config-issue32-test-deps.YXp69B python3 -m pytest -q \
  tests/pre_pr_tribunal/test_probe_harness.py \
  tests/pre_pr_tribunal/test_gate_adapters.py \
  tests/pre_pr_tribunal/test_installer.py \
  tests/pre_pr_tribunal/test_install_integration.py \
  tests/pre_pr_tribunal/test_skill_contract.py
rtk python3 -m py_compile scripts/probe-pre-pr-tribunal.py tests/pre_pr_tribunal/test_probe_harness.py
rtk git diff --check
```

Expected: 모두 PASS하며 runtime-output parser, capture hash, version extraction, socket/thread evidence와 custom signal state가 없다.

- [ ] **Step 7: Task 3을 커밋한다**

```bash
rtk git add scripts/probe-pre-pr-tribunal.py tests/pre_pr_tribunal/test_probe_harness.py
rtk git commit -m "test: cover simplified canary isolation and lifecycle"
```

---

### Task 4: Operator docs와 synthetic completion gate

**Files:**
- Modify: `README.md`
- Modify: `hooks/README.md`
- Modify: `docs/validation/2026-09-01-pre-pr-tribunal-canary.md`
- Modify: `docs/superpowers/specs/2026-09-02-pre-pr-runtime-canary-simplification-design.md`

**Interfaces:**
- Consumes: final schema-v2 implementation and synthetic test output.
- Produces: concise operator contract, append-only validation checkpoint and implementation-complete spec status.

- [ ] **Step 1: README의 obsolete capture/version/signal 설명을 schema-v2 계약으로 교체한다**

README의 pre-PR tribunal canary subsection은 다음 사실만 남긴다.

```markdown
실제 runtime canary는 `/usr/bin/bwrap` 안에서 GitHub hostname을 sinkhole하고 발견된
모든 fixed `gh` 경로를 exact fake executable로 덮는다. Runtime stdout/stderr는
`/dev/null`로 폐기하며 missing phase의 `D/0`, pass phase의 `A/1` marker만 schema-v2
report로 판정한다. Claude OAuth는 child environment에만 전달하고 Codex auth는 sealed
memfd에서 tmpfs `CODEX_HOME/auth.json`으로만 초기화하므로 host filesystem에 credential
copy를 만들지 않는다. 이 경계를 준비할 수 없으면 fail closed한다.
```

450초, version, hash, sensitivity, capture, staging leaf, socket/thread recorder와 custom signal mask 설명을 제거한다.

- [ ] **Step 2: hooks 운영 문서에 CLI, status와 report v2를 정확히 기록한다**

```json
{
  "schema": 2,
  "status": "PASS",
  "claude": {
    "status": "PASS",
    "missing": {"runtime_exit": "ZERO", "hook": "DENY", "gh_calls": 0},
    "pass": {"runtime_exit": "ZERO", "hook": "ALLOW", "gh_calls": 1}
  }
}
```

문서는 stable failure 8개, subscription-default/no-API-key-fallback, environment opt-in, 330초 Claude margin, Codex memfd/tmpfs, non-secret SIGTERM/SIGKILL residue와 provider-network shared residual을 명시한다. CLI는 그대로 유지한다.

```bash
rtk python3 scripts/probe-pre-pr-tribunal.py --runtime claude --repo-source "$PWD"
rtk python3 scripts/probe-pre-pr-tribunal.py --runtime codex --repo-source "$PWD"
rtk python3 scripts/probe-pre-pr-tribunal.py --runtime claude --auth-source environment --repo-source "$PWD"
```

- [ ] **Step 3: Validation 문서에 synthetic simplification checkpoint를 append한다**

다음 heading 아래 실제 실행 명령, test count, elapsed time과 commit SHA를 채운다. Credential/path/output byte는 기록하지 않는다.

```markdown
## Schema-v2 simplification synthetic checkpoint — 2026-09-02

- Status: synthetic PASS; live runtime not yet authorized at this checkpoint.
- Probe contract: missing `D/0`, pass `A/1`; runtime stdout/stderr discarded.
- Codex auth: sealed memfd → tmpfs auth, writable atomic refresh, read-only hooks overlay.
- Focused harness: copy the complete pytest summary from the focused command; exit 0.
- Related tribunal regression: copy the complete pytest summary from the related command; exit 0.
- Full repository regression: copy the complete pytest summary from the full command; exit 0.
- Implementation commit range: record the first implementation commit and reviewed HEAD from `git rev-parse`.
- No live credential, real Claude/Codex, GitHub/provider endpoint, push, merge or PR was used.
```

위 네 evidence line은 설명 문장을 그대로 commit하지 않고 Step 4의 실제 pytest summary와 full SHA로 바꾼다.

- [ ] **Step 4: Obsolete contract scan과 full repository regression을 실행한다**

Run:

```bash
rtk rg -n 'schema.?1|450초|version.capture|capture_sha256|SENSITIVE_OUTPUT|AUTH_UNAVAILABLE|OUTPUT_LIMIT|CREDENTIAL_STAGING_FAILED|pthread_sigmask|_EvidenceRecorder' \
  README.md hooks/README.md scripts/probe-pre-pr-tribunal.py tests/pre_pr_tribunal/test_probe_harness.py
rtk env PYTHONPATH=/tmp/claude-config-issue32-test-deps.YXp69B python3 -m pytest -q tests/pre_pr_tribunal
rtk env PYTHONPATH=/tmp/claude-config-issue32-test-deps.YXp69B python3 -m pytest -q
rtk python3 -m py_compile scripts/probe-pre-pr-tribunal.py tests/pre_pr_tribunal/test_probe_harness.py
rtk git diff --check
```

Expected: obsolete scan has no matches in the rewritten contract surfaces; all tests and static checks PASS. Validation 문서의 역사 구간은 과거 schema-v1 증거를 보존하므로 scan 대상에서 제외한다.

- [ ] **Step 5: Spec 상태와 validation의 실제 증거 값을 갱신한다**

Spec status를 `synthetic 구현 완료, 독립 scoped review 대기`로 바꾸고 Step 3의 실제 count/SHA를 기록한다. Test 결과를 재실행하지 않고 터미널 출력 그대로 옮긴다.

- [ ] **Step 6: Task 4를 커밋한다**

```bash
rtk git add README.md hooks/README.md \
  docs/validation/2026-09-01-pre-pr-tribunal-canary.md \
  docs/superpowers/specs/2026-09-02-pre-pr-runtime-canary-simplification-design.md
rtk git commit -m "docs: document schema v2 runtime canary"
```

---

### Task 5: Independent review, ordered live canary와 readiness

**Files:**
- Modify: `docs/validation/2026-09-01-pre-pr-tribunal-canary.md`
- Modify: `docs/superpowers/specs/2026-09-02-pre-pr-runtime-canary-simplification-design.md`
- Modify only if review requires a fix: `scripts/probe-pre-pr-tribunal.py`
- Modify only if review requires a fix: `tests/pre_pr_tribunal/test_probe_harness.py`

**Interfaces:**
- Consumes: clean synthetic implementation commits, full regression evidence, live subscription sources only after review approval.
- Produces: independent no-Critical/no-Important review verdict, ordered Claude/Codex sanitized schema-v2 PASS evidence, Project Control readiness. PR/push/merge는 생산하지 않는다.

- [ ] **Step 1: Review scope와 clean diff를 고정한다**

Run:

```bash
rtk git status --short --branch
rtk git diff --check a9f839516b0189c4d9fd2ec88bc4f96f61181fc4..HEAD
rtk git diff --stat a9f839516b0189c4d9fd2ec88bc4f96f61181fc4..HEAD
```

Expected: worktree clean, whitespace error 없음, diff는 계획에 명시된 production/test/docs file에 한정된다.

- [ ] **Step 2: Independent scoped implementation review를 실행한다**

`superpowers:requesting-code-review`로 `a9f839516b0189c4d9fd2ec88bc4f96f61181fc4..HEAD`를 검토한다. Reviewer에게 다음 acceptance matrix를 그대로 제공한다.

```text
1. Runtime stdout/stderr is DEVNULL and no output-derived field remains.
2. Missing is exactly D/0; pass is exactly A/1; marker errors fail closed.
3. Codex auth never reaches host disk; exact seals and tmpfs/file/ro-bind-data are required.
4. Existing bwrap, fixed-gh, GitHub sinkhole and exact guard boundaries remain.
5. No custom signal handler/mask, supervisor, socket/thread evidence or output classifier remains.
6. Public report fields and runtime failures match schema v2.
```

Stop condition: Critical 또는 Important finding이 하나라도 있으면 live canary를 실행하지 않는다. Finding은 RED test → minimal fix → focused/full regression → 새 독립 review 순서로 처리한다.

- [ ] **Step 3: Reviewer approval 뒤 Claude subscription canary를 먼저 실행한다**

Run:

```bash
rtk python3 scripts/probe-pre-pr-tribunal.py --runtime claude --repo-source "$PWD"
```

Expected: exit `0`, top/runtime `PASS`, missing `{runtime_exit: ZERO, hook: DENY, gh_calls: 0}`, pass `{runtime_exit: ZERO, hook: ALLOW, gh_calls: 1}`. 다른 status면 즉시 중단하고 Codex를 실행하지 않는다.

- [ ] **Step 4: Claude PASS일 때만 Codex subscription canary를 실행한다**

Run:

```bash
rtk python3 scripts/probe-pre-pr-tribunal.py --runtime codex --repo-source "$PWD"
```

Expected: exit `0`과 동일한 `D/0`, `A/1` matrix. Runtime output, credential, path, prompt, version 또는 hash는 report/validation에 없어야 한다.

- [ ] **Step 5: Sanitized live evidence와 최종 spec 상태를 기록한다**

Validation 문서에는 timestamp, commit SHA, 각 runtime의 exact schema-v2 object와 review verdict만 append한다. Spec status는 두 runtime PASS일 때 `완료`로 바꾼다. 실패 시 actual stable status만 기록하고 완료로 바꾸지 않는다.

```markdown
## Schema-v2 live runtime checkpoint — 2026-09-02

- Independent scoped review: PASS — Critical 0, Important 0.
- Commit: record the exact reviewed HEAD printed by `rtk git rev-parse HEAD`.
- Claude: `{"status":"PASS","missing":{"runtime_exit":"ZERO","hook":"DENY","gh_calls":0},"pass":{"runtime_exit":"ZERO","hook":"ALLOW","gh_calls":1}}`
- Codex: `{"status":"PASS","missing":{"runtime_exit":"ZERO","hook":"DENY","gh_calls":0},"pass":{"runtime_exit":"ZERO","hook":"ALLOW","gh_calls":1}}`
- No real GitHub endpoint, push, merge or PR action was used.
```

Commit line의 설명 문장은 실제 reviewed full SHA로 바꾼 뒤 commit한다.

- [ ] **Step 6: Evidence doc를 커밋하고 final regression을 확인한다**

```bash
rtk git add docs/validation/2026-09-01-pre-pr-tribunal-canary.md \
  docs/superpowers/specs/2026-09-02-pre-pr-runtime-canary-simplification-design.md
rtk git commit -m "test: record schema v2 runtime canary evidence"
rtk env PYTHONPATH=/tmp/claude-config-issue32-test-deps.YXp69B python3 -m pytest -q
rtk git diff --check
rtk git status --short --branch
```

Expected: full regression PASS와 clean worktree다.

- [ ] **Step 7: Final clean HEAD에 repository tribunal과 Project Control readiness를 확인한다**

Repository의 `skills/pre-pr-tribunal/SKILL.md` workflow를 base `master`, current clean HEAD, Reviewer A/B/C로 실행해 `.review/verdict.json`이 current snapshot PASS인지 확인한다. Reviewer blocker가 있으면 PR을 만들지 않고 해당 finding만 RED/fix/review cycle로 되돌린다.

Run after the current verdict is PASS:

```bash
rtk /home/jhw/.local/bin/jhw-control-host task ready \
  --task-id tsk-01a05b0d-9a53-77c3-8ede-da281cdf8e81 \
  --claim-id clm-01a05b0d-ddef-7752-96ec-f9dd699b8537 \
  --worktree-ref wt-da281cdf8e81-jhw7500-claude-config-32 \
  --json
```

Expected: current reviewed HEAD에 대한 Project Control readiness가 준비된다. PR 생성, push와 merge는 후속 `/jhw:ship` 범위이며 이 계획에서는 실행하지 않는다.
