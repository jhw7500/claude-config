import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest


SOURCE_ROOT = Path(__file__).parents[1]
SYNC_HELPER = SOURCE_ROOT / "scripts" / "lib" / "mcp_config_sync.py"
CANARY = "CANARY_EXISTING_CREDENTIAL_MUST_NOT_APPEAR"
SEMANTIC_SECRET = "ZXCVBNM987654321"
PERCENT_ENCODED_SEMANTIC_SECRET = "".join(
    f"%{ord(character):02X}" for character in SEMANTIC_SECRET
)
UNICODE_ESCAPED_SEMANTIC_SECRET = "".join(
    rf"\u{ord(character):04x}" for character in SEMANTIC_SECRET
)
SAFE_TEST_ENV = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "TMPDIR",
    "TZ",
    "USER",
)


def make_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    repo = tmp_path / "repo"
    shutil.copytree(SOURCE_ROOT / "scripts", repo / "scripts")
    (repo / "manifest").mkdir()
    (repo / "manifest" / "mcp.json").write_text(
        json.dumps(
            {
                "probe": {
                    "scope": "user",
                    "command": "new-command",
                    "args": ["--safe"],
                    "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
                }
            }
        ),
        encoding="utf-8",
    )

    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    config_dir.chmod(0o700)
    config_path = config_dir / ".claude.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "probe": {
                        "type": "stdio",
                        "command": "old-command",
                        "args": ["--token", CANARY],
                        "env": {"PROBE_TOKEN": CANARY},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "claude-calls.log"
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['CLAUDE_CALLS_FILE']).write_text('called\\n', encoding='utf-8')\n"
        "raise SystemExit(99)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    env = {key: os.environ[key] for key in SAFE_TEST_ENV if key in os.environ}
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CLAUDE_PROJECT_DIR"] = str(repo)
    env["CLAUDE_CALLS_FILE"] = str(call_log)
    env["PROBE_TOKEN"] = CANARY
    return repo, env, config_path, call_log


def run_setup(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "scripts" / "setup-mcp.sh"), *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=5,
    )


def load_sync_helper():
    spec = importlib.util.spec_from_file_location("test_mcp_config_sync", SYNC_HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_preview_reports_field_level_drift_without_mutation(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 2, result.stdout + result.stderr
    assert "[DRIFT] user/probe: command,args,env" in result.stdout
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


def test_preview_blocks_literal_credential_before_any_cli_call(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"]["PROBE_TOKEN"] = CANARY
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user/probe: env.PROBE_TOKEN must use a placeholder" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


def test_apply_replaces_drift_atomically_without_claude_child(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["unmanagedTopLevel"] = {"keep": True}
    config["mcpServers"]["unmanaged"] = {"type": "stdio", "command": "keep-me"}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "[DRIFT] user/probe: command,args,env" in result.stdout
    assert "[APPLIED] user/probe" in result.stdout
    assert CANARY not in combined
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["mcpServers"]["probe"] == {
        "type": "stdio",
        "command": "new-command",
        "args": ["--safe"],
        "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
    }
    assert updated["mcpServers"]["unmanaged"] == {
        "type": "stdio",
        "command": "keep-me",
    }
    assert updated["unmanagedTopLevel"] == {"keep": True}
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("case", "expected_field"),
    [
        ("type", "type"),
        ("command", "command"),
        ("args-order", "args"),
        ("env-add", "env"),
        ("env-delete", "env"),
        ("extra", "extra"),
    ],
)
def test_preview_reports_each_drift_field_independently(
    tmp_path,
    case,
    expected_field,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["args"] = ["first", "second"]
    manifest["probe"]["env"]["SAFE_SETTING"] = "${SAFE_SETTING}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    actual = {
        "type": "stdio",
        "command": "new-command",
        "args": ["first", "second"],
        "env": {
            "PROBE_TOKEN": "${PROBE_TOKEN}",
            "SAFE_SETTING": "${SAFE_SETTING}",
        },
    }
    if case == "type":
        actual["type"] = "sse"
    elif case == "command":
        actual["command"] = "other-command"
    elif case == "args-order":
        actual["args"] = ["second", "first"]
    elif case == "env-add":
        actual["env"]["EXTRA_SETTING"] = "${EXTRA_SETTING}"
    elif case == "env-delete":
        del actual["env"]["SAFE_SETTING"]
    elif case == "extra":
        actual["unexpected"] = True
    config = {"mcpServers": {"probe": actual}}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env)

    assert result.returncode == 2, result.stdout + result.stderr
    assert f"[DRIFT] user/probe: {expected_field}" in result.stdout
    assert CANARY not in result.stdout + result.stderr
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        (
            "command",
            "${PROBE_TOKEN}",
            "command may only reference approved path placeholders",
        ),
        ("args", ["--api-key", CANARY], "args may not contain credential flags"),
        ("command", CANARY, "command matches a credential environment value"),
        ("args", [CANARY], "args match a credential environment value"),
        ("command", f"prefix-{CANARY}", "command matches a credential environment value"),
        ("args", [f"--header=Bearer {CANARY}"], "args may not contain credential flags"),
    ],
)
def test_preview_blocks_credentials_in_command_or_args(
    tmp_path,
    field,
    value,
    diagnostic,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"[BLOCKED] user/probe: {diagnostic}" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


@pytest.mark.parametrize(
    "args",
    [
        [f"--auth={CANARY}"],
        ["--authorization", CANARY],
        [f"--bearer={CANARY}"],
        [f"--access-key={CANARY}"],
        [f"--accessToken={CANARY}"],
        [f"--refreshToken={CANARY}"],
        [f"--oauthAccessToken={CANARY}"],
        ["--private-key", CANARY],
        ["--header", f"Bearer {CANARY}"],
        ["--header", "Accept: application/json"],
        ["-H", f"Authorization: Bearer {CANARY}"],
        [f"--headers=Authorization: Bearer {CANARY}"],
        [f"-HAuthorization: Bearer {CANARY}"],
        [f"-H=Authorization: Bearer {CANARY}"],
        ["-e", f"PROBE_TOKEN={CANARY}"],
        [f"-ePROBE_TOKEN={CANARY}"],
        [f"--env=PROBE_TOKEN={CANARY}"],
        ["--wrapper-option", f"PROBE_TOKEN={CANARY}"],
        ["--config", f'{{"token":"{CANARY}"}}'],
        ["--config", f"{{'api_key': '{CANARY}'}}"],
        ["--config", f'{{"accessToken":"{CANARY}"}}'],
        ["--config", f'{{"refreshToken":"{CANARY}"}}'],
        ["--config", f'{{"oauthAccessToken":"{CANARY}"}}'],
        ["--config", f'{{"pass\\u0077ord":"{CANARY}"}}'],
        ["--config", f'{{"access\\u0054oken":"{CANARY}"}}'],
        [f'--config={{"client\\u0053ecret":"{CANARY}"}}'],
        [
            json.dumps(
                {"config": f'{{"pass\\u0077ord":"{CANARY}"}}'},
            )
        ],
        [
            "--config",
            fr'{{"url":"https:\/\/hooks.slack.com\/services\/T\/B\/{CANARY}"}}',
        ],
        [json.dumps(["--password", CANARY])],
        [json.dumps({"args": ["--token", CANARY]})],
        [json.dumps(f"--password={CANARY}")],
        [
            json.dumps(
                f"https://hooks.slack.com/services/T/B/{CANARY}"
            ).replace("/", r"\/")
        ],
        [json.dumps(json.dumps({"password": CANARY}))],
        [f"postgresql://app:{CANARY}@db.example/prod"],
        [f"--dsn=postgresql://app:{CANARY}@db.example/prod"],
        ["--dsn", CANARY],
        [f"https://{CANARY}@api.example/v1"],
        [f"Driver=PostgreSQL;Pwd={CANARY};Server=db.example"],
        [f"jdbc:postgresql://db.example/prod?pwd={CANARY}"],
        [f"app/{CANARY}@db.example/prod"],
        [f"app/{CANARY}@//db.example:1521/prod"],
        [f"app/{CANARY}@db.example:1521:PROD"],
        [f"app/{CANARY}@ORCL"],
        [f"--connect=app/{CANARY}"],
        [f"--logon=proxy[client]/{CANARY}"],
        [f"--connect=사용자/{CANARY}"],
        ["--connect", f"app/{CANARY}"],
        [json.dumps(["--connect", f"app/{CANARY}"])],
        [json.dumps({"--connect": f"app/{CANARY}"})],
        [
            json.dumps(
                {"nested": {"--logon": f"proxy[client]/{CANARY}"}},
            )
        ],
        [json.dumps(json.dumps({"--connect": f"app/{CANARY}"}))],
        [json.dumps({f"--connect=app/{CANARY}": "ignored"})],
        [json.dumps({"command": "sqlplus", "args": [f"app/{CANARY}"]})],
        [
            json.dumps(
                {
                    "nested": {
                        "command": "/opt/oracle/bin/sqlplus",
                        "args": [f"proxy[client]/{CANARY}"],
                    }
                },
            )
        ],
        [json.dumps(["sqlplus", f"app/{CANARY}"])],
        [
            quote(
                json.dumps(
                    {"command": "sqlcl", "args": [f"app/{CANARY}"]},
                ),
                safe="",
            )
        ],
        [
            quote(
                json.dumps(["--connect", f"app/{CANARY}"]),
                safe="",
            )
        ],
        [
            quote(
                json.dumps({"--connect": f"app/{CANARY}"}),
                safe="",
            )
        ],
        [quote(f"--connect=app/{CANARY}", safe="")],
        [f"--connect=app/{CANARY}@ORCL"],
        [f'--connect=app/"p {CANARY}"@ORCL'],
        [f"--connect=app/'p {CANARY}'@db.example:1521/prod"],
        [f'--connect="app user"/{CANARY}@ORCL'],
        [f"--connect=APP$USER/{CANARY}@ORCL"],
        [f"--connect=APP#USER/{CANARY}@ORCL"],
        [f"proxy[client]/{CANARY}@ORCL"],
        [f"--connect=proxy[client]/{CANARY}@db.example:1521/prod"],
        [f"jdbc:oracle:thin:proxy[client]/{CANARY}@ORCL"],
        [f"jdbc:oracle:oci:app/{CANARY}@ORCL"],
        [f"jdbc:oracle:oci8:proxy[client]/{CANARY}@ORCL"],
        [f'proxy["client user"]/"p {CANARY}"@tcps://db.example/prod'],
        [f"--connect=app/{CANARY}@(DESCRIPTION=(ADDRESS=synthetic))"],
        [f"app/{CANARY}@tcps://db.example:1521/prod"],
        [f"--connect=app/{CANARY}@tcp://db.example:1521/prod"],
        [f'"app user"/"p {CANARY}"@tcps://db.example:1521/prod'],
        [f"app/{CANARY}@db.example:1521/prod:dedicated/inst1"],
        [f"app/{CANARY}@db.example/prod//inst1"],
        [
            quote(
                f"app/{CANARY}@tcps://db.example:1521/prod",
                safe="",
            )
        ],
        [f"app/{CANARY}@db.example:1521/prod?connect_timeout=10"],
        [f"jdbc:oracle:thin:app/{CANARY}@db.example:1521/prod"],
        [f"jdbc:oracle:thin:app/{CANARY}@ORCL"],
        [f'jdbc:oracle:thin:app/"p@{CANARY}"@db.example:1521/prod'],
        [f"jdbc:oracle:thin:app/{CANARY}@(DESCRIPTION=(ADDRESS=synthetic))"],
        [f"https://hooks.slack.com/services/T000/B000/{CANARY}"],
        [f"https://hooks.slack.com/triggers/T000/B000/{CANARY}"],
        [f"https://hooks.slack.com/actions/T000/B000/{CANARY}"],
        [f"https://hooks.slack.com/app/T000/B000/{CANARY}"],
        [f"https://hooks.slack.com:443/services/T000/B000/{CANARY}"],
        [f"https://hooks.slack.com./services/T000/B000/{CANARY}"],
        [f"//hooks.slack.com/services/T000/B000/{CANARY}"],
        [f"https://hooks.slack.com/x/../services/T000/B000/{CANARY}"],
        [f"https://hooks.slack.com//services/T000/B000/{CANARY}"],
        [f"https://hooks.slack.com\\services\\T000\\B000\\{CANARY}"],
        [
            "https://example.test/redirect?next="
            f"https://hooks.slack.com/triggers/T/B/{CANARY}"
        ],
        [
            "https://example.test/redirect?next="
            + quote(
                f"https://hooks.slack.com/triggers/T/B/{CANARY}",
                safe="",
            )
        ],
        [
            "https://example.test/redirect?next="
            f"postgresql://app:{CANARY}@db.example/prod"
        ],
        [
            "https://example.test/redirect?next="
            + quote(
                f"postgresql://app:{CANARY}@db.example/prod",
                safe="",
            )
        ],
        [f"https://discord.com/api/v10/webhooks/123/{CANARY}"],
        [f"https://discord.com:443/api/webhooks/123/{CANARY}"],
        [
            "https://bucket.s3.example/object?X-Amz-Credential=AKIA"
            f"&X-Amz-Signature={CANARY}"
        ],
        [f"https://account.blob.example/object?sig={CANARY}"],
        [f"X-Amz-Signature={CANARY}"],
        [f"--x-amz-signature={CANARY}"],
        [f"X-Goog-Signature={CANARY}"],
        [f"--data=X-Amz-Signature={CANARY}"],
        [f"prefix X-Goog-Signature={CANARY}"],
        [f";X-Amz-Signature={CANARY}"],
        [f"--x-goog-signature={CANARY}"],
        [f"https://storage.googleapis.com/object?X-Goog-Signature={CANARY}"],
        [quote(f"X-Goog-Signature={CANARY}", safe="")],
        [json.dumps({"X-Amz-Signature": CANARY})],
        [json.dumps({"X-Goog-Signature": CANARY})],
        [f"https://example.test/?password[]={CANARY}"],
        [f"https://example.test/?auth.token={CANARY}"],
        [f"https://example.test/callback#access_token={CANARY}"],
        [f"https://example.test/callback#token={CANARY}"],
        [f"?sv=2024-11-04&sp=r&sig={CANARY}"],
        [f"sv=2024-11-04&se=2099-01-01&sp=r&sig={CANARY}"],
        [f"--data=sv=2024-11-04&sp=r&sig={CANARY}"],
        [f"(PASSWORD={CANARY})"],
        [f"[password[]={CANARY}]"],
        [
            "postgresql%253A%252F%252Fapp%253A"
            f"{CANARY}%2540db.example%252Fprod"
        ],
        [
            quote(
                quote(
                    quote(
                        f"https://hooks.slack.com/services/T/B/{CANARY}",
                        safe="",
                    ),
                    safe="",
                ),
                safe="",
            )
        ],
    ],
)
def test_preview_blocks_extended_credential_flags_without_inherited_secret(
    tmp_path,
    args,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["args"] = args
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user/probe: args may not contain credential flags" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("sqlplus", [f"app/{CANARY}"]),
        ("/opt/oracle/bin/sqlplus", [f"app/{CANARY}", "as", "sysdba"]),
        (r"C:\oracle\bin\sqlplus.exe", [f"proxy[client]/{CANARY}"]),
        ("sql", [f'"app user"/"p {CANARY}"']),
        ("sqlcl", [f"proxy[client]/{CANARY} as sysdba"]),
        ("sqlplus", [f"app/{CANARY} edition=release_v2"]),
        ("sqlplus", [f"app/{CANARY} AS SYSDBA edition=release_v2"]),
        ("sqlcl", [f"proxy[client]/{CANARY} edition=release_v2"]),
        ("sqlplus", [f"사용자/{CANARY}"]),
        ("sqlplus", [f"équipe/{CANARY}"]),
        ("sqlplus", [f"프록시[사용자]/{CANARY}"]),
        ("sqlplus", [f"app/{CANARY} edition=릴리스"]),
    ],
)
def test_preview_blocks_targetless_oracle_logon_for_oracle_clients(
    tmp_path,
    command,
    args,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["command"] = command
    manifest["probe"]["args"] = args
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user/probe: args may not contain credential flags" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("curl", ["-u", f"app:{SEMANTIC_SECRET}"]),
        ("curl", [f"-uapp:{SEMANTIC_SECRET}"]),
        ("curl", ["--user", f"app:{SEMANTIC_SECRET}"]),
        ("curl", [f"--user=app:{SEMANTIC_SECRET}"]),
        ("curl", ["-U", f"proxy:{SEMANTIC_SECRET}"]),
        ("curl", ["--proxy-user", f"proxy:{SEMANTIC_SECRET}"]),
        ("curl", ["-H", f"Cookie: session={SEMANTIC_SECRET}"]),
        ("curl", [f"-HCookie: session={SEMANTIC_SECRET}"]),
        ("curl", [f"--header=Cookie: session={SEMANTIC_SECRET}"]),
        ("curl", [f"--header=X-API-Key: {SEMANTIC_SECRET}"]),
        ("curl", [f"--proxy-header=X-API-Key: {SEMANTIC_SECRET}"]),
        ("curl", ["--cookie", f"session={SEMANTIC_SECRET}"]),
        ("curl", [f"--cookie=session={SEMANTIC_SECRET}"]),
        ("curl", ["-b", f"session={SEMANTIC_SECRET}"]),
        ("curl", [f"-bsession={SEMANTIC_SECRET}"]),
        ("curl", [f"-suapp:{SEMANTIC_SECRET}"]),
        ("curl", [f"-sUproxy:{SEMANTIC_SECRET}"]),
        ("curl", [f"-sbcookie={SEMANTIC_SECRET}"]),
        ("curl", [f"-sEclient.p12:{SEMANTIC_SECRET}"]),
        ("curl", [f"-sHCookie: session={SEMANTIC_SECRET}"]),
        ("curl", ["--cert", f"client.p12:{SEMANTIC_SECRET}"]),
        ("curl", ["-E", f"client.p12:{SEMANTIC_SECRET}"]),
        ("curl", [f"-Eclient.p12:{SEMANTIC_SECRET}"]),
        ("curl", ["--proxy-cert", f"client.p12:{SEMANTIC_SECRET}"]),
        ("curl", [f"--proxy-cert=client.p12:{SEMANTIC_SECRET}"]),
        ("curl", ["--proxy-us", f"proxy:{SEMANTIC_SECRET}"]),
        ("curl", [f"--proxy-us=proxy:{SEMANTIC_SECRET}"]),
        ("curl", ["--heade", f"Cookie: session={SEMANTIC_SECRET}"]),
        ("curl", [f"--proxy-hea=X-API-Key: {SEMANTIC_SECRET}"]),
        ("curl", ["--oauth2-b", SEMANTIC_SECRET]),
        ("curl", [f"--oauth2-b={SEMANTIC_SECRET}"]),
        ("curl", ["-u", "--", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("curl", ["-o", "--", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("curl", ["--output", "--", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("curl", ["--url", "--", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("curl", [f"app:{SEMANTIC_SECRET}@example.invalid"]),
        ("curl", ["--url", f"app:{SEMANTIC_SECRET}@example.invalid"]),
        ("curl", [f"--url=app:{SEMANTIC_SECRET}@example.invalid"]),
        ("curl", [f":{SEMANTIC_SECRET}@example.invalid"]),
        ("curl", [f"app:p%20{SEMANTIC_SECRET}@example.invalid"]),
        ("curl", ["--url", f":{SEMANTIC_SECRET}@example.invalid"]),
        ("curl", ["-x", f"app:{SEMANTIC_SECRET}@proxy.example.test"]),
        ("curl", ["-x", f":{SEMANTIC_SECRET}@proxy.example.test"]),
        ("curl", ["-x", f"app:p%09{SEMANTIC_SECRET}@proxy.example.test"]),
        ("curl", ["--proxy", f"app:{SEMANTIC_SECRET}@proxy.example.test"]),
        ("curl", [f"--proxy=app:{SEMANTIC_SECRET}@proxy.example.test"]),
        ("curl", ["--preproxy", f"app:{SEMANTIC_SECRET}@proxy.example.test"]),
        ("curl", ["%2D%2D", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("curl", ["%252D%252Do", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("curl", ["--out%70ut", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("curl", ["-%6f", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("curl", ["--expand-user", f"app:{SEMANTIC_SECRET}"]),
        (
            "curl",
            ["--variable", f"login=app:{SEMANTIC_SECRET}", "--expand-user", "{{login}}"],
        ),
        ("curl", ["--expand-cert", f"client.p12:{SEMANTIC_SECRET}"]),
        ("curl", ["--expand-cookie", f"sid={SEMANTIC_SECRET}"]),
        ("curl", ["--cert", rf"C:\safe\client.p12:{SEMANTIC_SECRET}"]),
        (
            "curl",
            [
                "--cert",
                f"pkcs11:token=synthetic;object=client;pin-value={SEMANTIC_SECRET}",
            ],
        ),
        (
            "curl",
            [
                f"--cert=pkcs11:token=synthetic;object=client;"
                f"pin-value={SEMANTIC_SECRET}"
            ],
        ),
        (
            "curl",
            [
                "-E",
                f"pkcs11:object=client;pin-value={SEMANTIC_SECRET}",
            ],
        ),
        (
            "curl",
            [f"-Epkcs11:object=client;pin-value={SEMANTIC_SECRET}"],
        ),
        (
            "curl",
            [
                "--proxy-cert",
                f"pkcs11:object=client;pin-value={SEMANTIC_SECRET}",
            ],
        ),
        (
            "curl",
            [
                f"--proxy-cert=pkcs11:object=client;"
                f"%70in%2Dvalue={SEMANTIC_SECRET}"
            ],
        ),
        ("curl", ["%2Du", f"app:{SEMANTIC_SECRET}"]),
        ("mysql", [f"-p{SEMANTIC_SECRET}"]),
        ("mariadb", [f"-p{SEMANTIC_SECRET}"]),
        ("mysqlsh", [f"-p{SEMANTIC_SECRET}"]),
        ("mysqlsh", ["--uri", f"app:{SEMANTIC_SECRET}@db.example.test"]),
        ("mysqlsh", [f"--uri=app:{SEMANTIC_SECRET}@db.example.test"]),
        ("mysqlsh", [f"app:{SEMANTIC_SECRET}@db.example.test"]),
        ("mysqlsh", [f":{SEMANTIC_SECRET}@db.example.test"]),
        ("mysqlsh", ["--uri", f"app:p%20{SEMANTIC_SECRET}@db.example.test"]),
        ("mysqlpump", [f"-p{SEMANTIC_SECRET}"]),
        ("mysqlslap", [f"-p{SEMANTIC_SECRET}"]),
        ("mysqlbinlog", [f"-p{SEMANTIC_SECRET}"]),
        ("mariadb-binlog", [f"-p{SEMANTIC_SECRET}"]),
        ("mariadb-slap", [f"-p{SEMANTIC_SECRET}"]),
        ("mysql", [f"--password1={SEMANTIC_SECRET}"]),
        ("mysql", [f"--password2={SEMANTIC_SECRET}"]),
        ("mysql", [f"--password3={SEMANTIC_SECRET}"]),
        ("mysql", [f"-vp{SEMANTIC_SECRET}"]),
        ("mariadb", [f"-vp{SEMANTIC_SECRET}"]),
        ("mysqldump", [f"-ep{SEMANTIC_SECRET}"]),
        ("mariadb-dump", [f"-ep{SEMANTIC_SECRET}"]),
        ("mysqlcheck", [f"-ep{SEMANTIC_SECRET}"]),
        ("mariadb-check", [f"-ep{SEMANTIC_SECRET}"]),
        ("mysqlbinlog", [f"-Dp{SEMANTIC_SECRET}"]),
        ("mariadb-binlog", [f"-Dp{SEMANTIC_SECRET}"]),
        ("mariadb", [f"--passw={SEMANTIC_SECRET}"]),
        ("mariadb-admin", [f"--passwo={SEMANTIC_SECRET}"]),
        ("mariadb-dump", [f"--passwor={SEMANTIC_SECRET}"]),
        ("mariadb", [f"--loose-passw={SEMANTIC_SECRET}"]),
        ("mariadb-dump", [f"--loose_passwo={SEMANTIC_SECRET}"]),
        ("mysql", [f"--default-auth=PROBE_TOKEN={SEMANTIC_SECRET}"]),
        ("mysql", ["-h", "--", f"-p{SEMANTIC_SECRET}"]),
        ("mysql", ["%2D%2D", f"-p{SEMANTIC_SECRET}"]),
        ("mysql", ["--h%6fst", f"-p{SEMANTIC_SECRET}"]),
        ("mariadb", ["-e", "--", f"-p{SEMANTIC_SECRET}"]),
        ("redis-cli", ["-a", SEMANTIC_SECRET]),
        ("redis-cli", ["-h", "--", "-a", SEMANTIC_SECRET]),
        ("redis-cli", ["%2D%2D", "-a", SEMANTIC_SECRET]),
        ("redis-cli", ["-%68", "-a", SEMANTIC_SECRET]),
        ("valkey-cli", ["-p", "--", "-a", SEMANTIC_SECRET]),
        ("sshpass", ["-p", SEMANTIC_SECRET, "ssh", "host"]),
        ("sshpass", [f"-p{SEMANTIC_SECRET}", "ssh", "host"]),
        ("sshpass", [f"-vp{SEMANTIC_SECRET}", "ssh", "host"]),
        ("sshpass", ["-P", "--", "-p", SEMANTIC_SECRET, "ssh", "host"]),
        ("sshpass", ["-f", "--", "-p", SEMANTIC_SECRET, "ssh", "host"]),
        ("sshpass", ["%2D%2D", "-p", SEMANTIC_SECRET, "ssh", "host"]),
        ("sshpass", ["-%50", "-p", SEMANTIC_SECRET, "ssh", "host"]),
        ("mongosh", ["-p", SEMANTIC_SECRET]),
        ("mongosh", ["--password", SEMANTIC_SECRET]),
        ("mongosh", [f"--password={SEMANTIC_SECRET}"]),
        ("mongosh", ["-u", "--", "-p", SEMANTIC_SECRET]),
        ("mongosh", ["%2D%2D", "-p", SEMANTIC_SECRET]),
        ("mongosh", ["--user%6eame", "-p", SEMANTIC_SECRET]),
        ("mongo", ["--username", "--", "-p", SEMANTIC_SECRET]),
        ("mongo", [f"-p{SEMANTIC_SECRET}"]),
        ("sqlcmd", ["-P", SEMANTIC_SECRET]),
        ("sqlcmd", [f"-P{SEMANTIC_SECRET}"]),
        ("sqlcmd", ["-U", "--", "-P", SEMANTIC_SECRET]),
        ("sqlcmd", ["%2D%2D", "-P", SEMANTIC_SECRET]),
        ("sqlcmd", ["-%55", "-P", SEMANTIC_SECRET]),
        ("sqlcmd.exe", ["/P", SEMANTIC_SECRET]),
        ("sqlcmd.exe", [f"/P{SEMANTIC_SECRET}"]),
        ("sqlcmd.exe", ["/U", "app", "/P", SEMANTIC_SECRET]),
        ("sqlcmd.exe", ["%2FU", "/P", SEMANTIC_SECRET]),
        ("sqlcmd.exe", ["%2F%55", "/P", SEMANTIC_SECRET]),
        ("sqlcmd.exe", ["%252F%2555", "/P", SEMANTIC_SECRET]),
        ("sqlcmd.exe", ["%252FU", "/Z", SEMANTIC_SECRET]),
        ("sqlcmd.exe", ["/%55", "/P", SEMANTIC_SECRET]),
        ("sqlcmd.exe", ["/%53", "/P", SEMANTIC_SECRET]),
        ("sqlcmd.exe", ["/%64", "/P", SEMANTIC_SECRET]),
        ("sqlcmd", ["-z", SEMANTIC_SECRET]),
        ("sqlcmd", [f"-Z{SEMANTIC_SECRET}"]),
        ("sqlcmd.exe", ["/z", SEMANTIC_SECRET]),
        ("sqlcmd.exe", [f"/Z{SEMANTIC_SECRET}"]),
        ("osql", ["-P", SEMANTIC_SECRET]),
        ("osql", ["-U", "--", "-P", SEMANTIC_SECRET]),
        ("osql.exe", ["/P", SEMANTIC_SECRET]),
        ("osql.exe", ["%2FS", "/P", SEMANTIC_SECRET]),
        ("osql.exe", ["%2fU", "/P", SEMANTIC_SECRET]),
        ("osql.exe", ["/%55", "/P", SEMANTIC_SECRET]),
        ("bcp", ["table", "out", "file", "-U", "app", "-P", SEMANTIC_SECRET]),
        ("bcp", ["table", "out", "file", f"-P{SEMANTIC_SECRET}"]),
        (
            "bcp",
            ["table", "out", "file", "-U", "--", "-P", SEMANTIC_SECRET],
        ),
        ("docker", ["login", "-p", SEMANTIC_SECRET]),
        ("docker", ["login", f"-p{SEMANTIC_SECRET}"]),
        ("docker", ["login", "-u", "--", "-p", SEMANTIC_SECRET]),
        ("docker", ["login", "%2D%2D", "-p", SEMANTIC_SECRET]),
        ("docker", ["login", "--user%6eame", "-p", SEMANTIC_SECRET]),
        (
            "docker",
            ["login", "--username", "--", "-p", SEMANTIC_SECRET],
        ),
        ("docker", ["--tlskey", "/safe/key.pem", "login", "-p", SEMANTIC_SECRET]),
        ("docker", ["-l", "debug", "login", "-p", SEMANTIC_SECRET]),
        ("podman", ["login", "-p", SEMANTIC_SECRET]),
        ("podman", ["login", f"-vp{SEMANTIC_SECRET}"]),
        ("podman", ["login", "-vp", "registry.example.test"]),
        ("podman", ["login", "-u", "--", "-p", SEMANTIC_SECRET]),
        ("podman", ["login", "%2D%2D", "-p", SEMANTIC_SECRET]),
        ("podman-remote", ["login", "-p", SEMANTIC_SECRET]),
        ("podman-remote.exe", ["login", f"-p{SEMANTIC_SECRET}"]),
        (
            "podman",
            ["login", "--authfile", "--", "-p", SEMANTIC_SECRET],
        ),
        (
            "podman",
            [
                "--url",
                "unix:///run/user/1000/podman.sock",
                "login",
                f"-p{SEMANTIC_SECRET}",
            ],
        ),
        ("podman", ["--runtime", "crun", "login", "-p", SEMANTIC_SECRET]),
        (
            "sudo",
            [
                f"--preserve-env=PROBE_TOKEN={SEMANTIC_SECRET}",
                "git",
                "status",
            ],
        ),
        (
            "podman",
            ["--cgroup-manager", "systemd", "login", "-p", SEMANTIC_SECRET],
        ),
    ],
)
def test_preview_blocks_command_specific_credential_carriers(
    tmp_path,
    command,
    args,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["command"] = command
    manifest["probe"]["args"] = args
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user/probe: args may not contain credential flags" in result.stderr
    assert SEMANTIC_SECRET not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("bash", ["-lc", f"sqlplus app/{SEMANTIC_SECRET}"]),
        ("bash", ["-o", "posix", "-c", f"sqlplus app/{SEMANTIC_SECRET}"]),
        ("bash", ["+%4F", "-c", f"curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["+%6f", "-c", f"curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["%2B%4F", "-c", f"curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["%252B%254F", "-c", f"curl -u app:{SEMANTIC_SECRET}"]),
        (
            "bash",
            ["--rcfile", "/safe/rc", "-c", f"curl -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "bash",
            [
                "--init-file",
                "/safe/rc",
                "-c",
                f"curl -u app:{SEMANTIC_SECRET}",
            ],
        ),
        ("sh", ["-c", f"exec sqlcl proxy[client]/{SEMANTIC_SECRET}"]),
        (
            "bash",
            ["-c", f"if sqlplus app/{SEMANTIC_SECRET}; then :; fi"],
        ),
        (
            "bash",
            ["-c", f"while sqlplus app/{SEMANTIC_SECRET}; do :; done"],
        ),
        ("bash", ["-c", f"{{ sqlplus app/{SEMANTIC_SECRET}; }}"]),
        ("bash", ["-c", f"x=curl; $x -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"eval 'sqlplus app/{SEMANTIC_SECRET}'"]),
        (
            "bash",
            ["-c", f"builtin eval 'curl -u app:{SEMANTIC_SECRET}'"],
        ),
        (
            "bash",
            ["-c", f"builtin builtin eval 'curl -u app:{SEMANTIC_SECRET}'"],
        ),
        (
            "bash",
            ["-c", f"command command eval 'curl -u app:{SEMANTIC_SECRET}'"],
        ),
        (
            "bash",
            ["-c", f"builtin command curl -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "bash",
            ["-c", f"builtin exec sqlplus app/{SEMANTIC_SECRET}"],
        ),
        ("bash", ["-c", f"command -p curl -u app:{SEMANTIC_SECRET}"]),
        (
            "bash",
            ["-c", f"command -p -- curl -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "bash",
            [
                "-c",
                f"builtin -- command -p -- curl -u app:{SEMANTIC_SECRET}",
            ],
        ),
        ("bash", ["-c", f"exec -a alias curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"exec -c curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"time -p curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"time ! curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"time -p ! curl -u app:{SEMANTIC_SECRET}"]),
        (
            "bash",
            ["-c", f"time SAFE_MODE=1 curl -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "bash",
            ["-c", f"time -p SAFE_MODE=1 curl -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "zsh",
            ["-c", f"noglob curl -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "zsh",
            ["-c", f"nocorrect SAFE_MODE=1 curl -u app:{SEMANTIC_SECRET}"],
        ),
        ("zsh", ["-c", f"=curl -u app:{SEMANTIC_SECRET}"]),
        ("zsh", ["-c", f"=sqlplus app/{SEMANTIC_SECRET}"]),
        ("zsh", ["-c", f"repeat 1 curl -u app:{SEMANTIC_SECRET}"]),
        ("zsh", ["-c", f"- curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"cu\\\nrl -u app:{SEMANTIC_SECRET}"]),
        ("dash", ["-c", f"curl -\\\nu app:{SEMANTIC_SECRET}"]),
        ("sh", ["-c", f"curl -u app:\\\n{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"A[0]=x curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"A[key]=x curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"A[0]+=x curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"/usr/bin/cu?l -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"/usr/bin/cu*l -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"/usr/bin/cu[r]l -u app:{SEMANTIC_SECRET}"]),
        (
            "bash",
            ["-c", f"/usr/bin/cu{{rl,zz}} -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "bash",
            ["-c", f"/usr/bin/cur{{l..l}} -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "bash",
            [
                "-O",
                "extglob",
                "-c",
                f"/usr/bin/@(curl) -u app:{SEMANTIC_SECRET}",
            ],
        ),
        ("bash", ["-c", f"coproc curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"TOKEN+={SEMANTIC_SECRET} true"]),
        ("bash", ["-c", f'TO""KEN={SEMANTIC_SECRET} true']),
        ("env", ["-S", f'TO""KEN={SEMANTIC_SECRET} true']),
        (
            "env",
            ["-S", f'TO""KEN={SEMANTIC_SECRET} -S "true"'],
        ),
        ("bash", ["-c", f"API_KEY+={SEMANTIC_SECRET} true"]),
        (
            "bash",
            ["-c", quote(f"TOKEN+={SEMANTIC_SECRET} true", safe="")],
        ),
        (
            "node",
            [json.dumps(["bash", "-c", f"TOKEN+={SEMANTIC_SECRET} true"])],
        ),
        (
            "bash",
            [
                "-c",
                f"printf '%s' '{SEMANTIC_SECRET}' | "
                "docker login --password-stdin",
            ],
        ),
        ("bash", ["-c", f"< /dev/null sqlplus app/{SEMANTIC_SECRET}"]),
        ("env", ["sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("env", ["SAFE_MODE=1", "sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("env", ["A.B=x", "curl", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("env", ["A-B=x", "sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("env", ["1A=x", "sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("env", ["=x", "sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("env", ["/A=x", "curl", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("env", ["-S", f"-i curl -u app:{SEMANTIC_SECRET}"]),
        ("env", ["-S", rf"-i\_curl\_-u\_app:{SEMANTIC_SECRET}"]),
        ("env", ["-S", f"curl\v-u\vapp:{SEMANTIC_SECRET}"]),
        ("env", ["-S", f"curl\f-u\fapp:{SEMANTIC_SECRET}"]),
        ("sudo", ["sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("sudo", ["-u", "oracle", "sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("sudo", ["A.B=x", "curl", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("sudo", ["A-B=x", "sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("sudo", ["-s", "curl", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("sudo", ["-i", "sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("timeout", ["30", "sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("nohup", ["curl", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("nice", ["-n", "5", "mysql", f"-p{SEMANTIC_SECRET}"]),
        ("nice", ["-5", "--", "mysql", f"-p{SEMANTIC_SECRET}"]),
        (
            "nice",
            ["-5", "-n", "2", "mysql", f"-p{SEMANTIC_SECRET}"],
        ),
        ("stdbuf", ["-oL", "sqlplus", f"app/{SEMANTIC_SECRET}"]),
        ("env", ["curl", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("bash", ["-c", f"curl --cookie session={SEMANTIC_SECRET}"]),
        (
            "bash",
            ["-c", f"curl %2D%2D -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "bash",
            ["-c", f"curl \\%2D%2D -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "bash",
            ["-c", f"curl --out%70ut -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "zsh",
            ["-c", f"curl \\%252D%252D -u app:{SEMANTIC_SECRET}"],
        ),
        (
            "bash",
            ["-c", f"sqlcmd.exe \\%2FU /P {SEMANTIC_SECRET}"],
        ),
        (
            "node",
            [
                json.dumps(
                    {
                        "command": "curl",
                        "args": ["-u", f"app:{SEMANTIC_SECRET}"],
                    }
                )
            ],
        ),
        (
            "node",
            [json.dumps(["curl", "%2Du", f"app:{SEMANTIC_SECRET}"])],
        ),
        (
            "node",
            [
                quote(
                    json.dumps(["curl", "-u", f"app:{SEMANTIC_SECRET}"]),
                    safe="",
                )
            ],
        ),
        (
            "node",
            [json.dumps(["bash", "-lc", f"sqlplus app/{SEMANTIC_SECRET}"])],
        ),
    ],
)
def test_preview_blocks_wrapped_command_credential_carriers(
    tmp_path,
    command,
    args,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["command"] = command
    manifest["probe"]["args"] = args
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user/probe: args may not contain credential flags" in result.stderr
    assert SEMANTIC_SECRET not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


def test_preview_blocks_credential_assignment_in_command_without_inherited_secret(
    tmp_path,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["command"] = f"PROBE_TOKEN={CANARY}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "command may not contain credential carriers" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    "command",
    [
        f"--connect=app/{CANARY}",
        quote(f"--logon=proxy[client]/{CANARY}", safe=""),
        json.dumps(["--connect", f"app/{CANARY}"]),
        json.dumps({"--connect": f"app/{CANARY}"}),
    ],
)
def test_preview_blocks_targetless_oracle_logon_in_command(tmp_path, command):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["command"] = command
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "command may not contain credential carriers" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


@pytest.mark.parametrize(
    "command",
    [
        "owner/repository",
        "--connect=owner/repository/path",
    ],
)
def test_preview_allows_noncredential_command_paths(tmp_path, command):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["command"] = command
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "[BLOCKED]" not in result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        (
            "command",
            "${DATABASE_URL}",
            "command may only reference approved path placeholders",
        ),
        (
            "args",
            ["${APP_DSN}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["${MONGODB_URI}"],
            "args may only reference approved path placeholders",
        ),
    ],
)
def test_preview_blocks_connection_container_placeholder_references(
    tmp_path,
    field,
    value,
    diagnostic,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    assert result.returncode == 1
    assert f"[BLOCKED] user/probe: {diagnostic}" in result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        (
            "args",
            ["${MYSQL_PWD}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["${CLOUDAMQP_URL}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["${CLOUDINARY_URL}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["${REDISCLOUD_URL}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["${JAWSDB_URL}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["${PUBLIC_URL}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["${UNCLASSIFIED_VALUE}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["${FILESYSTEM_MCP_ROOT:-synthetic-value}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["${FILESYSTEM_MCP_ROOT:?synthetic-value}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["prefix-${FILESYSTEM_MCP_ROOT}"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            ["${JHW_NOTION_DIST}/../synthetic"],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            [quote(quote("${DATABASE_URL}", safe=""), safe="")],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            [r'"\u0024{DATABASE_URL}"'],
            "args may only reference approved path placeholders",
        ),
        (
            "args",
            [r'{"value":"\u0024{DATABASE_URL}"}'],
            "args may only reference approved path placeholders",
        ),
    ],
)
def test_preview_blocks_provider_credential_placeholder_references(
    tmp_path,
    field,
    value,
    diagnostic,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    assert result.returncode == 1
    assert f"[BLOCKED] user/probe: {diagnostic}" in result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("command", CANARY, "command matches a credential environment value"),
        ("args", [f"prefix-{CANARY}"], "args match a credential environment value"),
    ],
)
def test_preview_blocks_literal_from_ambient_credential_not_declared_by_entry(
    tmp_path,
    field,
    value,
    diagnostic,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    env["OTHER_API_KEY"] = CANARY
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {}
    manifest["probe"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"[BLOCKED] user/probe: {diagnostic}" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("command", "args"),
    [
        (
            "npx",
            [f"https://example.test/?value={PERCENT_ENCODED_SEMANTIC_SECRET}"],
        ),
        (
            "npx",
            [
                "https://example.test/?value="
                + quote(PERCENT_ENCODED_SEMANTIC_SECRET, safe="")
            ],
        ),
        ("npx", [f'"{UNICODE_ESCAPED_SEMANTIC_SECRET}"']),
        (
            "npx",
            [f'{{"safe":"{UNICODE_ESCAPED_SEMANTIC_SECRET}"}}'],
        ),
        (
            "npx",
            [json.dumps(f'"{UNICODE_ESCAPED_SEMANTIC_SECRET}"')],
        ),
        (
            "bash",
            ["-c", f'printf %s {SEMANTIC_SECRET[:2]}""{SEMANTIC_SECRET[2:]}'],
        ),
        (
            "env",
            ["-S", f'printf %s {SEMANTIC_SECRET[:2]}""{SEMANTIC_SECRET[2:]}'],
        ),
        (
            "bash",
            ["-c", f'SAFE_MODE={SEMANTIC_SECRET[:2]}""{SEMANTIC_SECRET[2:]} true'],
        ),
        (
            "bash",
            ["-c", f'exec -a {SEMANTIC_SECRET[:2]}""{SEMANTIC_SECRET[2:]} true'],
        ),
        (
            "env",
            ["-S", f'SAFE_MODE={SEMANTIC_SECRET[:2]}""{SEMANTIC_SECRET[2:]} true'],
        ),
        (
            "env",
            [
                "-S",
                f'SAFE_MODE={SEMANTIC_SECRET[:2]}""{SEMANTIC_SECRET[2:]} '
                '-S "true"',
            ],
        ),
    ],
)
def test_preview_blocks_encoded_or_parser_normalized_ambient_credential(
    tmp_path,
    command,
    args,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    env["OTHER_API_KEY"] = SEMANTIC_SECRET
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {}
    manifest["probe"]["command"] = command
    manifest["probe"]["args"] = args
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user/probe: args" in result.stderr
    assert SEMANTIC_SECRET not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


def test_preview_fails_closed_at_ambient_json_normalization_depth(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    env["OTHER_API_KEY"] = SEMANTIC_SECRET
    nested: object = f'"{UNICODE_ESCAPED_SEMANTIC_SECRET}"'
    for _level in range(8):
        nested = {"safe": nested}
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {}
    manifest["probe"]["args"] = [json.dumps(nested)]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    assert result.returncode == 1
    assert "[BLOCKED] user/probe: args" in result.stderr
    assert SEMANTIC_SECRET not in result.stdout + result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    "variable",
    [
        "GITHUB_PAT",
        "GITHUBPAT",
        "GITLABPAT",
        "CLIENTSECRET",
        "MYSQL_PWD",
        "DB_PASS",
        "SSH_PASSPHRASE",
        "SSHPASS",
    ],
)
def test_preview_blocks_literal_from_ambient_concatenated_credential_name(
    tmp_path,
    variable,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    env[variable] = CANARY
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {}
    manifest["probe"]["args"] = [f"prefix-{CANARY}"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "args match a credential environment value" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    "name",
    ["COMPAT", "PATH", "PATTERN", "PWD", "OLDPWD", "SSH_ASKPASS", "PGPASSFILE"],
)
def test_credential_name_classification_preserves_safe_names(name):
    helper = load_sync_helper()

    assert not helper._is_credential_name(name)


@pytest.mark.parametrize(
    "variable",
    [
        "DATABASE_URL",
        "CLOUDAMQP_URL",
        "CLOUDINARY_URL",
        "REDISCLOUD_URL",
        "JAWSDB_URL",
    ],
)
def test_preview_blocks_literal_from_ambient_connection_container(
    tmp_path,
    variable,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    env.pop("PROBE_TOKEN")
    env[variable] = CANARY
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {}
    manifest["probe"]["args"] = [f"prefix-{CANARY}"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "args match a credential environment value" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    "arg",
    [
        "git@github.com:owner/repository",
        "user@example.com",
        "sftp://deploy@files.example/path",
        "ftp://anonymous@files.example/pub",
        "${FILESYSTEM_MCP_ROOT}",
        "${JHW_NOTION_DIST}/index.js",
        "sig=public-checksum",
        "--signature-version=4",
        "signatureVersion=4",
        "owner/repository",
        "owner/repository/path",
        "사용자/저장소",
        "사용자/저장소/경로",
        "--connect=owner/repository/path",
        "--connect=https://example.test",
        json.dumps({"--connect": "owner/repository/path"}),
        json.dumps({"--connect": "https://example.test"}),
        json.dumps({"label": "owner/repository"}),
        json.dumps(
            {"command": "sqlplus", "args": ["owner/repository/path"]},
        ),
        json.dumps(
            {"command": "curl", "args": ["--header", "Accept: application/json"]},
        ),
        json.dumps(["curl", "--header=Accept: application/json"]),
        json.dumps(["sqlplus", "owner/repository/path"]),
        json.dumps(
            ["sqlplus", "/nolog", "@script.sql", "owner/repository"],
        ),
        json.dumps(
            {
                "command": "sqlplus",
                "args": ["@script.sql", "owner/repository"],
            },
        ),
        "/nolog",
        "/",
        "/ as sysdba",
        "@owner/repository/path.sql",
        "git:owner/repository@host/service",
        "jdbc:oracle:thin:@db.example:1521/prod",
        "jdbc:oracle:oci:@ORCL",
    ],
)
def test_preview_allows_noncredential_carrier_args(tmp_path, arg):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["args"] = [arg]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "[BLOCKED]" not in result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("sqlplus", ["owner/repository/path"]),
        ("sqlplus", ["사용자/저장소/경로"]),
        ("/opt/oracle/bin/sqlplus", ["/nolog"]),
        ("sql", ["/"]),
        ("sqlcl", ["/", "as", "sysdba"]),
        ("sqlplus", ["/nolog", "@script.sql", "owner/repository"]),
        ("sqlplus", ["@script.sql", "owner/repository"]),
        ("sqlplus", ["/", "@script.sql", "owner/repository"]),
        ("sqlplus", ["app", "@script.sql", "owner/repository"]),
        ("sqlplus-helper", ["owner/repository"]),
        ("npx", ["--connect", "https://example.test"]),
    ],
)
def test_preview_allows_noncredential_oracle_client_args(tmp_path, command, args):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["command"] = command
    manifest["probe"]["args"] = args
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "[BLOCKED]" not in result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("node", ["-e", "console.log(1)"]),
        ("ruby", ["-e", "puts 1"]),
        ("ssh", ["-H"]),
        ("cc", ["-H"]),
        ("docker", ["run", "-e", "SAFE_MODE=1", "image"]),
        ("npx", ["-p", "package-name"]),
        ("ssh", ["-p", "22"]),
        ("generic-command", ["-u", "owner"]),
        ("curl", ["-u", "owner"]),
        ("curl", ["-H", "Accept: application/json"]),
        ("curl", ["--header", "Accept: application/json"]),
        ("curl", ["--header=Accept: application/json"]),
        ("curl", ["--proxy-header", "Accept: application/json"]),
        ("curl", ["--cookie", "cookies.txt"]),
        ("curl", ["--cookie-jar", "cookies.txt"]),
        ("curl", ["--cert", "/safe/client.p12"]),
        ("curl", ["--cert", r"C:\safe\client.p12"]),
        ("curl", ["--cert", r"/safe/client\:name.pem"]),
        ("curl", ["--cert", "pkcs11:object=client-cert;type=cert"]),
        ("curl", ["--head", "https://example.test"]),
        ("curl", ["--proxy", "https://proxy.example.test"]),
        ("curl", ["--user-agent", "public-client/1.0"]),
        ("curl", ["--cert-status", "https://example.test"]),
        ("curl", ["--url", "app@example.invalid"]),
        ("curl", ["app:@example.invalid"]),
        ("curl", ["app@example.invalid"]),
        ("curl", ["@example.invalid"]),
        ("curl", [":@example.invalid"]),
        ("curl", ["--proxy", "proxy.example.test:8080"]),
        ("curl", ["--proxy=http://proxy.example.test:8080"]),
        ("curl", ["https://example.test/public%2Dpath"]),
        ("curl", ["https://example.test/public%2Fpath"]),
        ("curl", ["--expand-user", "app"]),
        ("curl", ["--expand-cert", "/safe/client.p12"]),
        (
            "curl",
            [
                "--variable",
                "color=blue",
                "--expand-header",
                "X-Color: {{color}}",
            ],
        ),
        ("curl", ["-sS"]),
        ("curl", ["-OLv"]),
        ("curl", [f"-ouapp:{SEMANTIC_SECRET}"]),
        ("curl", ["--", "-u", f"app:{SEMANTIC_SECRET}"]),
        ("mysql", ["-p", "database_name"]),
        ("mysql", [f"-hp{SEMANTIC_SECRET}"]),
        ("mysql", [f"-ep{SEMANTIC_SECRET}"]),
        ("mysql", ["-v", "-p", "database_name"]),
        ("mysql", ["--", f"-p{SEMANTIC_SECRET}"]),
        ("mysql", ["--password", "database_name"]),
        ("mysql", ["--password2"]),
        ("mysql", ["--skip-password"]),
        ("mysql", ["--connect-expired-password"]),
        ("mysql", ["--default-auth=caching_sha2_password"]),
        ("mysqlsh", ["--uri", "app@db.example.test"]),
        ("mysqlsh", ["app:@db.example.test"]),
        ("mysqlsh", ["app@example.test"]),
        ("mariadb", ["--passw", "database_name"]),
        ("sshpass", ["-e", "ssh", "host"]),
        ("sshpass", ["-v", "ssh", "host"]),
        ("sshpass", ["-f", "/safe/password-file", "ssh", "host"]),
        ("sshpass", ["-e", "ssh", "-p", "22", "host"]),
        ("redis-cli", [f"-a{SEMANTIC_SECRET}"]),
        ("mongosh", ["-p"]),
        ("mongosh", ["--username", "app", "--password"]),
        ("sqlcmd", ["-P"]),
        ("sqlcmd.exe", ["/safe/path"]),
        ("osql", ["-P"]),
        ("bcp", ["table", "out", "file", "-G", "-P", "/safe/token-file"]),
        ("docker", ["run", "-p", "8080:80", "image"]),
        ("docker", ["run", "image", "login", "-p", "8080:80"]),
        ("docker", ["--debug", "run", "-p", "8080:80", "image"]),
        (
            "podman",
            ["--url", "unix:///run/user/1000/podman.sock", "info"],
        ),
        ("docker", ["login", "--password-stdin"]),
        ("docker", ["login", "-p", "-"]),
        ("docker", ["login", "-p-"]),
        ("docker", ["login", "--password=-"]),
        ("docker", ["login", "--", "-p", SEMANTIC_SECRET]),
        ("podman", ["login", "-upublic-user", "registry.example.test"]),
        (
            "podman-remote",
            ["login", "-upublic-user", "registry.example.test"],
        ),
        ("podman-remote", ["run", "-p", "8080:80", "image"]),
        ("bash", ["-lc", "git clone owner/repository"]),
        ("bash", ["-o", "posix", "script.sh"]),
        ("bash", ["+O", "-c", f"curl -u app:{SEMANTIC_SECRET}"]),
        ("bash", ["+public", "script.sh"]),
        ("bash", ["-c", f"echo 'sqlplus app/{SEMANTIC_SECRET}'"]),
        ("bash", ["-c", f"printf '%s' 'sqlplus app/{SEMANTIC_SECRET}'"]),
        ("bash", ["-c", "SAFE_MODE+=public true"]),
        ("bash", ["-c", "time SAFE_MODE=1 git status"]),
        ("bash", ["-c", "time -p ! SAFE_MODE=1 git status"]),
        ("zsh", ["-c", "noglob SAFE_MODE=1 git status"]),
        ("zsh", ["-c", "repeat 1 git status"]),
        ("zsh", ["-c", "- git status"]),
        ("zsh", ["-c", "git =not-a-command"]),
        ("bash", ["-c", r"printf '%s' '\r'"]),
        ("bash", ["-c", "A[0]=x git status"]),
        ("bash", ["-c", "command -p -- git status"]),
        ("env", ["npx", "-p", "owner/repository"]),
        ("env", ["A.B=x", "git", "status"]),
        ("env", ["=x", "git", "status"]),
        ("env", ["-S", "-i git clone owner/repository"]),
        ("env", ["--ignore-environment", "git", "status"]),
        ("sudo", ["git", "clone", "owner/repository"]),
        ("sudo", ["A-B=x", "git", "status"]),
        ("sudo", ["--preserve-env=SAFE_MODE", "git", "status"]),
        ("sudo", ["-i"]),
        ("timeout", ["30", "git", "clone", "owner/repository"]),
        ("nohup", ["git", "clone", "owner/repository"]),
        ("nice", ["-n", "5", "git", "status"]),
        ("nice", ["-5", "--", "git", "status"]),
        ("nice", ["-5", "-n", "2", "git", "status"]),
        ("stdbuf", ["-oL", "git", "status"]),
    ],
)
def test_preview_allows_noncredential_short_flags(tmp_path, command, args):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["command"] = command
    manifest["probe"]["args"] = args
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "[BLOCKED]" not in result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize("shadow_scope", ["local", "project"])
def test_preview_blocks_higher_precedence_scope_shadow(tmp_path, shadow_scope):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    desired = {
        "type": "stdio",
        "command": "new-command",
        "args": ["--safe"],
        "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
    }
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mcpServers"]["probe"] = desired
    config_path.write_text(json.dumps(config), encoding="utf-8")
    if shadow_scope == "local":
        config["projects"] = {
            str(repo): {
                "mcpServers": {
                    "probe": {
                        "type": "stdio",
                        "command": "shadow-command",
                        "args": [CANARY],
                    }
                }
            }
        }
    else:
        (repo / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "probe": {
                            "type": "stdio",
                            "command": "shadow-command",
                            "args": [CANARY],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"[SHADOWED] user/probe: {shadow_scope}" in result.stderr
    assert CANARY not in combined
    assert not call_log.exists()


def test_preview_detects_local_shadow_under_an_opaque_claude_project_key(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    desired = {
        "type": "stdio",
        "command": "new-command",
        "args": ["--safe"],
        "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
    }
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mcpServers"]["probe"] = desired
    config["projects"] = {
        "/opaque/claude-project-key": {
            "mcpServers": {
                "probe": {
                    "type": "stdio",
                    "command": "shadow-command",
                    "args": [CANARY],
                }
            }
        }
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[SHADOWED] user/probe: local" in result.stderr
    assert "/opaque/claude-project-key" not in combined
    assert CANARY not in combined
    assert not call_log.exists()


def test_explicit_apply_migrates_legacy_local_entry_to_user_scope(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config = {
        "projects": {
            "/opaque/claude-project-key": {
                "mcpServers": {
                    "probe": {
                        "type": "stdio",
                        "command": "legacy-command",
                        "args": [CANARY],
                        "env": {"PROBE_TOKEN": CANARY},
                    },
                    "unmanaged": {"type": "stdio", "command": "keep-me"},
                }
            }
        },
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env, "--apply", "--migrate-local")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "[MIGRATED] local/probe -> user/probe" in result.stdout
    assert "[APPLIED] user/probe" in result.stdout
    assert "/opaque/claude-project-key" not in combined
    assert CANARY not in combined
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["mcpServers"]["probe"] == {
        "type": "stdio",
        "command": "new-command",
        "args": ["--safe"],
        "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
    }
    local_servers = updated["projects"]["/opaque/claude-project-key"]["mcpServers"]
    assert "probe" not in local_servers
    assert local_servers["unmanaged"] == {"type": "stdio", "command": "keep-me"}
    assert not call_log.exists()


def test_migrate_local_still_blocks_project_scope_shadow(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["projects"] = {
        "/opaque/claude-project-key": {
            "mcpServers": {"probe": {"command": "legacy-local", "args": [CANARY]}}
        }
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (repo / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"probe": {"command": "project-shadow", "args": [CANARY]}}}
        ),
        encoding="utf-8",
    )
    original = config_path.read_bytes()

    result = run_setup(repo, env, "--apply", "--migrate-local")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[SHADOWED] user/probe: project" in result.stderr
    assert "local" not in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


def test_migrate_local_requires_explicit_apply(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    original = config_path.read_bytes()

    result = run_setup(repo, env, "--migrate-local")

    assert result.returncode == 64
    assert "--migrate-local requires --apply" in result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize("preview_flag", ["--check", "--dry-run"])
def test_migrate_local_rejects_preview_mode(tmp_path, preview_flag):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    original = config_path.read_bytes()

    result = run_setup(repo, env, preview_flag, "--migrate-local")

    assert result.returncode == 64
    assert "--migrate-local requires --apply" in result.stderr
    assert config_path.read_bytes() == original
    assert not call_log.exists()


def test_apply_rejects_symlinked_user_config_without_leaking_contents(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    target = tmp_path / "actual-claude.json"
    target.write_bytes(config_path.read_bytes())
    original_target = target.read_bytes()
    config_path.unlink()
    config_path.symlink_to(target)

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user configuration is not a private regular file" in result.stderr
    assert CANARY not in combined
    assert target.read_bytes() == original_target
    assert not call_log.exists()


def test_preview_rejects_fifo_user_config_without_blocking(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config_path.unlink()
    os.mkfifo(config_path, mode=0o600)

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user configuration is not a private regular file" in result.stderr
    assert CANARY not in combined
    assert not call_log.exists()


@pytest.mark.parametrize("unsafe_kind", ["world-readable", "hard-linked"])
def test_apply_rejects_unsafe_user_config_metadata(tmp_path, unsafe_kind):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    if unsafe_kind == "world-readable":
        config_path.chmod(0o644)
    else:
        os.link(config_path, tmp_path / "second-config-link")
    original = config_path.read_bytes()

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user configuration is not a private regular file" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


def test_apply_rejects_symlinked_lock_without_touching_its_target(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    lock_target = tmp_path / "lock-target"
    lock_target.write_text(CANARY, encoding="utf-8")
    lock_path = config_path.with_name(".claude.json.mcp-sync.lock")
    lock_path.symlink_to(lock_target)
    original_config = config_path.read_bytes()

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] MCP synchronization lock could not be secured" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert lock_target.read_text(encoding="utf-8") == CANARY
    assert not call_log.exists()


def test_apply_rejects_group_or_world_writable_config_directory(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config_dir = config_path.parent
    config_dir.chmod(0o777)
    original = config_path.read_bytes()

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user configuration directory is not secure" in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not list(config_dir.glob(".claude.json.mcp-sync.*"))
    assert not call_log.exists()


def test_apply_rejects_symlinked_config_directory(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config_dir = config_path.parent
    target_dir = tmp_path / "actual-config-directory"
    config_dir.rename(target_dir)
    config_dir.symlink_to(target_dir, target_is_directory=True)
    target_config = target_dir / ".claude.json"
    original = target_config.read_bytes()

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "[BLOCKED] user configuration directory is not secure" in result.stderr
    assert CANARY not in combined
    assert target_config.read_bytes() == original
    assert not list(target_dir.glob(".claude.json.mcp-sync.*"))
    assert not call_log.exists()


def test_apply_creates_missing_user_config_with_private_mode(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config_path.unlink()

    result = run_setup(repo, env, "--apply")

    assert result.returncode == 0, result.stdout + result.stderr
    assert config_path.stat().st_mode & 0o777 == 0o600
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["mcpServers"]["probe"]["env"] == {
        "PROBE_TOKEN": "${PROBE_TOKEN}"
    }
    assert CANARY not in result.stdout + result.stderr
    assert not call_log.exists()


def test_atomic_apply_aborts_if_config_changes_before_commit(tmp_path, monkeypatch):
    helper = load_sync_helper()
    config_path = tmp_path / ".claude.json"
    initial = {"mcpServers": {}, "unmanaged": {"keep": True}}
    config_path.write_text(json.dumps(initial), encoding="utf-8")
    config_path.chmod(0o600)
    expected_raw = config_path.read_bytes()
    directory_metadata = tmp_path.stat(follow_symlinks=False)
    expected_directory = (directory_metadata.st_dev, directory_metadata.st_ino)
    concurrent = {"mcpServers": {}, "unmanaged": {"concurrent": True}}
    concurrent_raw = json.dumps(concurrent).encode("utf-8")
    original_read = helper._read_user_config
    calls = 0

    def interleaved_read(path, directory_fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(concurrent_raw)
            path.chmod(0o600)
        return original_read(path, directory_fd)

    monkeypatch.setattr(helper, "_read_user_config", interleaved_read)
    result = helper._atomic_apply(
        config_path,
        expected_raw,
        {"probe": {"type": "stdio", "command": "safe", "args": [], "env": {}}},
        set(),
        expected_directory,
    )

    assert result == "changed"
    assert config_path.read_bytes() == concurrent_raw
    leftovers = [
        path
        for path in tmp_path.glob(".claude.json.mcp-sync.*")
        if path.name != ".claude.json.mcp-sync.lock"
    ]
    assert not leftovers


def test_atomic_apply_rejects_config_directory_retarget_after_preview(tmp_path):
    helper = load_sync_helper()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_dir.chmod(0o700)
    config_path = config_dir / ".claude.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    config_path.chmod(0o600)
    expected_raw = config_path.read_bytes()
    metadata = config_dir.stat(follow_symlinks=False)
    expected_directory = (metadata.st_dev, metadata.st_ino)
    original_dir = tmp_path / "original-config"
    config_dir.rename(original_dir)
    config_dir.mkdir()
    config_dir.chmod(0o700)
    retargeted_config = config_dir / ".claude.json"
    retargeted_config.write_bytes(expected_raw)
    retargeted_config.chmod(0o600)
    desired = {"type": "stdio", "command": "safe", "args": [], "env": {}}

    result = helper._atomic_apply(
        retargeted_config,
        expected_raw,
        {"probe": desired},
        set(),
        expected_directory,
    )

    assert result == "changed"
    assert retargeted_config.read_bytes() == expected_raw
    assert (original_dir / ".claude.json").read_bytes() == expected_raw
    assert not list(config_dir.glob(".claude.json.mcp-sync.*"))


def test_atomic_apply_reports_durability_uncertain_after_committed_replace(
    tmp_path,
    monkeypatch,
):
    helper = load_sync_helper()
    config_path = tmp_path / ".claude.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    config_path.chmod(0o600)
    expected_raw = config_path.read_bytes()
    directory_metadata = tmp_path.stat(follow_symlinks=False)
    expected_directory = (directory_metadata.st_dev, directory_metadata.st_ino)
    desired = {"type": "stdio", "command": "safe", "args": [], "env": {}}
    original_fsync = helper.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory fsync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(helper.os, "fsync", fail_directory_fsync)
    result = helper._atomic_apply(
        config_path,
        expected_raw,
        {"probe": desired},
        set(),
        expected_directory,
    )

    assert result == "durability-uncertain"
    assert json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"][
        "probe"
    ] == desired


def test_check_does_not_report_committed_but_unconfirmed_apply_as_not_updated(
    tmp_path,
    monkeypatch,
    capsys,
):
    repo, env, config_path, _call_log = make_fixture(tmp_path)
    helper = load_sync_helper()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        helper,
        "_atomic_apply",
        lambda *_args, **_kwargs: "durability-uncertain",
    )
    original = config_path.read_bytes()

    result = helper.check(
        repo / "manifest" / "mcp.json",
        with_internal=False,
        apply=True,
    )
    captured = capsys.readouterr()

    assert result == 1
    assert "[APPLIED_UNCONFIRMED]" in captured.err
    assert "was not updated" not in captured.err
    assert CANARY not in captured.out + captured.err
    assert config_path.read_bytes() == original


def test_check_disables_shell_xtrace_before_processing_private_environment(tmp_path):
    repo, env, _config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {"BRAVE_API_KEY": "${BRAVE_API_KEY}"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    secret_file = repo / "secrets.local.env"
    secret_file.write_text(f"BRAVE_API_KEY={CANARY}\n", encoding="utf-8")
    secret_file.chmod(0o600)

    result = subprocess.run(
        ["bash", "-x", str(repo / "scripts" / "setup-mcp.sh")],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert CANARY not in combined
    assert not call_log.exists()


def test_repository_manifest_passes_secret_safe_planning(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    shutil.copy2(SOURCE_ROOT / "manifest" / "mcp.json", repo / "manifest" / "mcp.json")
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    result = run_setup(repo, env, "--no-internal")

    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert "[MISSING] user/morph-mcp" in result.stdout
    assert "cts-email" not in combined
    assert "cts-ta" not in combined
    assert "jhw-notion" not in combined
    assert "ssh-mcp" not in combined
    assert "[BLOCKED]" not in combined
    assert not call_log.exists()


def test_preview_blocks_literal_default_in_credential_placeholder(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"]["PROBE_TOKEN"] = f"${{PROBE_TOKEN:-{CANARY}}}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert (
        "[BLOCKED] user/probe: env.PROBE_TOKEN placeholder may not have a default"
        in result.stderr
    )
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


@pytest.mark.parametrize(
    "variable",
    [
        "PGPASSWORD",
        "accessToken",
        "refreshToken",
        "MYSQL_PWD",
        "DB_PASS",
        "SSH_PASSPHRASE",
        "GITHUB_PAT",
        "CLIENTSECRET",
    ],
)
def test_apply_blocks_literal_default_in_concatenated_credential_placeholder(
    tmp_path,
    variable,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {
        variable: f"${{{variable}:-{CANARY}}}",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_config = config_path.read_bytes()

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert (
        f"[BLOCKED] user/probe: env.{variable} placeholder may not have a default"
    ) in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("variable", "default"),
    [
        (
            "DATABASE_URL",
            f"postgresql://app:{CANARY}@db.example/prod",
        ),
        ("APP_DSN", "postgresql://localhost/prod"),
        ("PUBLIC_URL", f"https://{CANARY}@api.example/v1"),
        ("DATABASE_URI", f"Driver=PostgreSQL;Pwd={CANARY};Server=db.example"),
        (
            "ENCODED_URL",
            "postgresql%253A%252F%252Fapp%253A"
            f"{CANARY}%2540db.example%252Fprod",
        ),
    ],
)
def test_apply_blocks_connection_credential_defaults(
    tmp_path,
    variable,
    default,
):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {
        variable: f"${{{variable}:-{default}}}",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert (
        f"[BLOCKED] user/probe: env.{variable} placeholder may not have a default"
    ) in result.stderr
    assert CANARY not in combined
    assert config_path.read_bytes() == original
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("variable", "default"),
    [
        ("PUBLIC_URL", "https://example.com/api"),
        ("PWD", "/tmp"),
        ("OLDPWD", "/var/tmp"),
        ("DATABASE_URL_TIMEOUT", "https://example.com/timeout"),
        ("REPOSITORY_URL", "ssh://git@github.com/example/project"),
        ("REPOSITORY_IPV6_URL", "ssh://git@[2001:db8::1]"),
        (
            "ENCODED_PATH_URL",
            "https://example.com/path%252F%252Fuser%2540note",
        ),
        ("SQLITE_URL", "sqlite:///tmp/application.db"),
    ],
)
def test_apply_blocks_all_placeholder_defaults(tmp_path, variable, default):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    placeholder = f"${{{variable}:-{default}}}"
    manifest["probe"]["env"] = {variable: placeholder}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = config_path.read_bytes()

    result = run_setup(repo, env, "--apply")

    assert result.returncode == 1
    assert (
        f"[BLOCKED] user/probe: env.{variable} placeholder may not have a default"
        in result.stderr
    )
    assert config_path.read_bytes() == original
    assert not call_log.exists()


def test_check_returns_in_sync_without_cli_calls(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mcpServers"]["probe"] = {
        "type": "stdio",
        "command": "new-command",
        "args": ["--safe"],
        "env": {"PROBE_TOKEN": "${PROBE_TOKEN}"},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_setup(repo, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "[IN_SYNC] user/probe"
    assert not call_log.exists()


def test_apply_adds_missing_entry_without_claude_child(tmp_path):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "[MISSING] user/probe" in result.stdout
    assert "[APPLIED] user/probe" in result.stdout
    assert CANARY not in combined
    assert not call_log.exists()


@pytest.mark.parametrize("preview_flag", ["--check", "--dry-run"])
def test_setup_rejects_conflicting_preview_and_apply_modes(tmp_path, preview_flag):
    repo, env, config_path, call_log = make_fixture(tmp_path)
    original_config = config_path.read_bytes()

    result = run_setup(repo, env, preview_flag, "--apply")

    assert result.returncode == 64
    assert "conflicting mode options" in result.stderr
    assert config_path.read_bytes() == original_config
    assert not call_log.exists()


def test_setup_mcp_does_not_read_repo_secret_file(tmp_path):
    repo, env, _config_path, call_log = make_fixture(tmp_path)
    secret_file = repo / "secrets.local.env"
    secret_file.write_text(f"BRAVE_API_KEY={CANARY}\n", encoding="utf-8")
    secret_file.chmod(0o644)

    result = run_setup(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert "unsafe secret file" not in combined.lower()
    assert CANARY not in combined
    assert not call_log.exists()


def test_apply_does_not_spawn_claude_with_repo_secret_values(tmp_path):
    repo, env, _config_path, call_log = make_fixture(tmp_path)
    manifest_path = repo / "manifest" / "mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["probe"]["env"] = {"BRAVE_API_KEY": "${BRAVE_API_KEY}"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    secret_file = repo / "secrets.local.env"
    secret_file.write_text(f"BRAVE_API_KEY={CANARY}\n", encoding="utf-8")
    secret_file.chmod(0o600)
    result = run_setup(repo, env, "--apply")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert CANARY not in combined
    assert not call_log.exists()
