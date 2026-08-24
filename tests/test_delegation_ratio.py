"""scripts/delegation-ratio.py 테스트 (transcript 기반).

계측 대상은 Claude Code transcript(~/.claude/projects) 원본이다.
`.session-stats.json` 기반 1차안은 Codex 리뷰에서 기각됐다 — 서브에이전트 호출이
부모 세션에 합산돼 위임이 direct 로 오분류되고(P1), 세션 수명 집계를 시작일에
몰아넣으며(P2), 정보수집 도구가 분모에서 빠지고(P2), --days 0/-1 이 빈 결과를
정상 출력했다(P3). 아래 테스트들은 그 4건의 회귀 테스트다.

CLI 도구이므로 훅의 "조용히 실패 + exit 0" 규약을 따르지 않는다 — 잘못된 입력은
exit 1 이어야 한다. 조용히 0을 뱉으면 틀린 숫자를 근거로 삼게 된다.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "delegation-ratio.py"


def local_iso(y, m, d, hh=12, mm=0):
    """호스트 타임존 기준 해당 날짜가 보장되는 ISO timestamp."""
    return datetime(y, m, d, hh, mm).astimezone().isoformat()


def tool_use(name, ts, sidechain=False):
    return json.dumps({
        "type": "assistant", "timestamp": ts, "isSidechain": sidechain,
        "message": {"content": [{"type": "tool_use", "id": "t", "name": name}]},
    })


def tool_result(content, ts, sidechain=False):
    return json.dumps({
        "type": "user", "timestamp": ts, "isSidechain": sidechain,
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t",
                                 "content": content}]},
    })


def result_bytes(content):
    """스크립트와 동일한 방식의 tool_result 크기 (UTF-8 인코딩 후 길이)."""
    return len(json.dumps(content, ensure_ascii=False).encode("utf-8"))


def make_tree(tmp_path, main_lines, sub_lines=None, session="sess1"):
    proj = tmp_path / "root" / "proj1"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session}.jsonl").write_text("\n".join(main_lines) + "\n", encoding="utf-8")
    if sub_lines is not None:
        subdir = proj / session / "subagents"
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "agent-x.jsonl").write_text("\n".join(sub_lines) + "\n", encoding="utf-8")
    return tmp_path / "root"


def run(args, root=None):
    argv = [sys.executable, str(SCRIPT)]
    if root is not None:
        argv += ["--root", str(root)]
    argv += args
    return subprocess.run(argv, text=True, capture_output=True, check=False)


def overall(args, root):
    r = run(args + ["--json"], root)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


TS = local_iso(2026, 8, 20)


def test_subagent_calls_excluded_from_direct(tmp_path):
    """회귀: Codex 리뷰 P1 — 서브에이전트의 탐색 호출은 direct 분모에 들어가면 안 된다."""
    root = make_tree(
        tmp_path,
        main_lines=[tool_use("Bash", TS), tool_use("Bash", TS), tool_use("Agent", TS)],
        sub_lines=[tool_use("Bash", TS)] * 10,
    )
    o = overall(["--since", "2026-08-20"], root)["overall"]
    assert o["direct"] == 2       # 서브의 Bash 10건 미포함
    assert o["delegate"] == 1
    assert o["delegation_pct"] == pytest.approx(33.33, abs=0.01)


def test_sidechain_records_in_main_file_are_sub(tmp_path):
    """구버전 transcript 는 서브에이전트 턴을 메인 파일에 isSidechain 으로 남긴다."""
    root = make_tree(
        tmp_path,
        main_lines=[
            tool_use("Bash", TS),
            tool_use("Bash", TS, sidechain=True),
            tool_result("X" * 50, TS, sidechain=True),
        ],
    )
    o = overall(["--since", "2026-08-20"], root)["overall"]
    assert o["direct"] == 1
    assert o["sub_bytes"] == result_bytes("X" * 50)


def test_midnight_crossing_buckets_by_call_time(tmp_path):
    """회귀: Codex 리뷰 P2 — 자정을 넘긴 세션은 호출 시각 기준으로 이틀에 나뉜다."""
    root = make_tree(
        tmp_path,
        main_lines=[
            tool_use("Bash", local_iso(2026, 8, 20, 23, 0)),
            tool_use("Bash", local_iso(2026, 8, 21, 1, 0)),
        ],
    )
    payload = overall(["--since", "2026-08-19"], root)
    days = {d["day"]: d for d in payload["days"]}
    assert set(days) == {"2026-08-20", "2026-08-21"}
    assert days["2026-08-20"]["direct"] == 1
    assert days["2026-08-21"]["direct"] == 1


def test_since_filters_by_call_time_within_file(tmp_path):
    """회귀: Codex 리뷰 P2 — 창 필터도 세션 시작일이 아니라 호출 시각 기준."""
    root = make_tree(
        tmp_path,
        main_lines=[
            tool_use("Bash", local_iso(2026, 8, 18)),
            tool_use("Bash", local_iso(2026, 8, 22)),
        ],
    )
    o = overall(["--since", "2026-08-20"], root)["overall"]
    assert o["direct"] == 1


def test_info_tools_counted_in_denominator(tmp_path):
    """회귀: Codex 리뷰 P2 — WebSearch/ToolSearch/MCP search·fetch 류도 정보수집 분모."""
    root = make_tree(
        tmp_path,
        main_lines=[
            tool_use("WebSearch", TS),
            tool_use("ToolSearch", TS),
            tool_use("mcp__notion__notion-search", TS),
            tool_use("mcp__jhw-notion__jhw_retrieve", TS),
            tool_use("mcp__notion__notion-create-pages", TS),  # action 도구 — 제외
            tool_use("Edit", TS),                              # 작성 도구 — 제외
            # 회귀: Codex 리뷰 P2 — 서버 네임스페이스의 동사는 매칭하면 안 된다
            tool_use("mcp__mcp_search__build_corpus", TS),
            tool_use("mcp__query_admin__delete_index", TS),
        ],
    )
    o = overall(["--since", "2026-08-20"], root)["overall"]
    assert o["direct"] == 4


def test_utf8_bytes_not_codepoints(tmp_path):
    """회귀: Codex 리뷰 P2 — 한글 출력을 코드포인트 수로 재면 1/3 과소집계."""
    content = "한" * 100
    root = make_tree(tmp_path, main_lines=[tool_result(content, TS)])
    o = overall(["--since", "2026-08-20"], root)["overall"]
    assert o["main_bytes"] == result_bytes(content)
    assert o["main_bytes"] > 300  # 인코딩 전 len 은 102


def test_naive_timestamp_skipped_not_crash(tmp_path):
    """회귀: Codex 리뷰 P2 — offset 없는 naive timestamp 레코드가 리포트를 죽이면 안 된다."""
    naive = json.dumps({
        "type": "assistant", "timestamp": "2026-08-20T12:00:00",  # tz 없음
        "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash"}]},
    })
    root = make_tree(tmp_path, main_lines=[naive, tool_use("Bash", TS)])
    o = overall(["--since", "2026-08-20"], root)["overall"]
    assert o["direct"] == 1  # naive 레코드는 스킵, 나머지는 정상 집계


def test_offload_bytes_split_main_vs_sub(tmp_path):
    """offload% = 서브에이전트가 소화한 tool_result 바이트 비중."""
    main_payload, sub_payload = "m" * 100, "s" * 300
    root = make_tree(
        tmp_path,
        main_lines=[tool_result(main_payload, TS)],
        sub_lines=[tool_result(sub_payload, TS)],
    )
    o = overall(["--since", "2026-08-20"], root)["overall"]
    mb, sb = result_bytes(main_payload), result_bytes(sub_payload)
    assert o["main_bytes"] == mb
    assert o["sub_bytes"] == sb
    assert o["offload_pct"] == pytest.approx(sb / (mb + sb) * 100, abs=0.01)


def test_bash_is_subset_of_direct(tmp_path):
    root = make_tree(tmp_path, main_lines=[tool_use("Bash", TS), tool_use("Read", TS)])
    o = overall(["--since", "2026-08-20"], root)["overall"]
    assert o["bash"] == 1
    assert o["direct"] == 2


@pytest.mark.parametrize("days", ["0", "-1", "abc"])
def test_nonpositive_days_rejected(tmp_path, days):
    """회귀: Codex 리뷰 P3 — --days 0/-1 이 빈 결과를 '정상' 출력하면 안 된다."""
    root = make_tree(tmp_path, main_lines=[tool_use("Bash", TS)])
    r = run(["--days", days], root)
    assert r.returncode != 0
    assert "--days" in r.stderr


def test_missing_root_exits_nonzero(tmp_path):
    r = run(["--overall"], tmp_path / "nope")
    assert r.returncode != 0
    assert "not found" in r.stderr


def test_bad_since_format_exits_nonzero(tmp_path):
    root = make_tree(tmp_path, main_lines=[tool_use("Bash", TS)])
    r = run(["--since", "8/24"], root)
    assert r.returncode != 0
    assert "YYYY-MM-DD" in r.stderr


def test_malformed_lines_are_skipped(tmp_path):
    """깨진 JSON / timestamp 없음 / content 비배열은 조용히 건너뛰고 나머지는 집계."""
    no_ts = json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": "Bash"}]}})
    str_content = json.dumps({"type": "assistant", "timestamp": TS,
                              "message": {"content": "tool_use 아님"}})
    root = make_tree(
        tmp_path,
        main_lines=['{broken json "tool_use"', no_ts, str_content, tool_use("Bash", TS)],
    )
    o = overall(["--since", "2026-08-20"], root)["overall"]
    assert o["direct"] == 1


def test_empty_root_reports_zero(tmp_path):
    (tmp_path / "root").mkdir()
    o = overall(["--overall"], tmp_path / "root")["overall"]
    assert o["sessions"] == 0
    assert o["delegation_pct"] == 0.0
    assert o["offload_pct"] == 0.0


def test_json_schema(tmp_path):
    root = make_tree(tmp_path, main_lines=[tool_use("Bash", TS), tool_use("Agent", TS)])
    payload = overall(["--since", "2026-08-20"], root)
    assert set(payload) == {"overall", "days"}
    day = payload["days"][0]
    for key in ("day", "sessions", "direct", "bash", "delegate",
                "delegation_pct", "main_kb", "sub_kb", "offload_pct"):
        assert key in day
