#!/usr/bin/env python3
"""탐색 위임률 계측 — transcript(~/.claude/projects) 직접 집계.

데이터 소스를 `.session-stats.json`(OMC 플러그인 소유, 서브에이전트 호출이 부모
세션에 합산됨)이 아니라 Claude Code transcript 원본으로 잡는다. 근거:

- 메인 스레드는 `<project>/<session>.jsonl`, 서브에이전트는
  `<project>/<session>/subagents/agent-*.jsonl` 로 **물리적으로 분리 저장**된다.
  → 위임된 작업이 direct 로 오분류되지 않는다 (Codex 리뷰 P1).
- 레코드마다 ISO timestamp 가 있어 **호출 시각 기준** 일별 버킷팅이 된다.
  자정을 넘긴 세션이 시작일에 몰리지 않는다 (Codex 리뷰 P2).
- tool_result 크기를 잴 수 있어 위임의 실제 목적(메인 스레드 컨텍스트 보존)을
  직접 측정한다: 서브에이전트가 소화한 tool_result 바이트는 메인 컨텍스트에
  들어오지 않은 바이트다.

지표:
- delegation% = Agent / (Agent + 메인스레드 정보수집 호출).
  정보수집 = Bash/Grep/Glob/Read + WebSearch/WebFetch/ToolSearch +
  mcp__* 중 search/fetch/retrieve/recall/query 류 (Codex 리뷰 P2 — 범위 명시).
- offload% = 서브에이전트 tool_result 바이트 / 전체 tool_result 바이트.
  "위임이 메인 컨텍스트에서 몇 %의 도구 출력을 치웠는가".

용례:
    delegation-ratio.py                 # 최근 14일
    delegation-ratio.py --days 30
    delegation-ratio.py --since 2026-08-01
    delegation-ratio.py --overall       # 전체 기간 (transcript 전량 스캔 — 느림)
    delegation-ratio.py --json
"""

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.expanduser("~/.claude/projects")

DIRECT_EXPLORE = {"Bash", "Grep", "Glob", "Read"}
DIRECT_INFO = {"WebSearch", "WebFetch", "ToolSearch"}
# mcp__<server>__<op> 의 마지막 연산명 세그먼트만 본다 — 서버 네임스페이스에
# 동사가 들어간 action 도구의 오분류 방지 (Codex 리뷰 P2).
# hooks/delegate-nudge-hook.py 의 is_mcp_info() 와 동기화 유지.
MCP_INFO_VERB_RE = re.compile(r"(search|fetch|retrieve|recall|query)", re.IGNORECASE)
DELEGATE_TOOLS = {"Agent", "Task"}  # Task 는 구버전 위임 도구명

# json.loads 전에 싸게 거르기 위한 문자열 마커
_TOOL_MARKERS = ('"tool_use"', '"tool_result"')


def is_direct(name):
    if name in DIRECT_EXPLORE or name in DIRECT_INFO:
        return True
    if not name.startswith("mcp__"):
        return False
    return bool(MCP_INFO_VERB_RE.search(name.rsplit("__", 1)[-1]))


def parse_ts(iso):
    """ISO-8601(Z 포함) → aware datetime. 실패/offset 없는 naive 는 None."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    # naive datetime 은 aware 인 since_dt 와 비교 시 TypeError 로 전체 리포트를
    # 죽인다 — 레코드 단위로 스킵한다 (Codex 리뷰 P2)
    return dt if dt.tzinfo is not None else None


def new_bucket():
    return {
        "direct": 0, "delegate": 0, "bash": 0,
        "main_bytes": 0, "sub_bytes": 0, "sub_calls": 0,
        "sessions": set(),
    }


def scan_file(path, kind, session, days, since_dt):
    """transcript 한 파일을 스트리밍 집계. kind: 'main' | 'sub'."""
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            if not any(m in line for m in _TOOL_MARKERS):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_ts(rec.get("timestamp"))
            if ts is None:
                continue
            if since_dt is not None and ts < since_dt:
                continue
            # 구버전은 서브에이전트 턴을 메인 파일에 isSidechain 으로 남겼다
            eff_kind = "sub" if (kind == "sub" or rec.get("isSidechain") is True) else "main"
            day = ts.astimezone().strftime("%Y-%m-%d")
            row = days[day]
            row["sessions"].add(session)

            rtype = rec.get("type")
            msg = rec.get("message")
            if not isinstance(msg, dict):  # 비객체 message — 스킵 (Codex P2)
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            if rtype == "assistant":
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
                        continue
                    name = item.get("name") or ""
                    if eff_kind != "main":
                        continue  # 서브에이전트의 호출은 direct 분모에 넣지 않는다 (P1)
                    if name in DELEGATE_TOOLS:
                        row["delegate"] += 1
                    elif is_direct(name):
                        row["direct"] += 1
                        if name == "Bash":
                            row["bash"] += 1
            elif rtype == "user":
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_result":
                        continue
                    try:
                        # UTF-8 인코딩 후 길이 — 코드포인트 수는 비ASCII 를
                        # 과소집계한다 (Codex 리뷰 P2)
                        size = len(json.dumps(item.get("content", ""),
                                              ensure_ascii=False).encode("utf-8"))
                    except (TypeError, ValueError):
                        continue
                    if eff_kind == "sub":
                        row["sub_bytes"] += size
                        row["sub_calls"] += 1
                    else:
                        row["main_bytes"] += size


def collect(root, since_dt):
    """root 아래 모든 프로젝트의 메인/서브 transcript 를 일별 집계."""
    if not os.path.isdir(root):
        sys.exit(f"transcript root not found: {root}")
    days = defaultdict(new_bucket)
    since_ts = since_dt.timestamp() if since_dt else None

    for main in glob.glob(os.path.join(root, "*", "*.jsonl")):
        session = os.path.splitext(os.path.basename(main))[0]
        # 창 밖 파일은 통째로 건너뛴다 (mtime = 마지막 기록 시각)
        try:
            if since_ts is not None and os.path.getmtime(main) < since_ts:
                continue
        except OSError:
            continue
        scan_file(main, "main", session, days, since_dt)
        for sub in glob.glob(os.path.join(root, "*", session, "subagents", "agent-*.jsonl")):
            scan_file(sub, "sub", session, days, since_dt)
    return days


def pct(part, whole):
    return (part / whole * 100) if whole else 0.0


def summarize(days):
    agg = {k: 0 for k in ("direct", "delegate", "bash", "main_bytes", "sub_bytes", "sub_calls")}
    sessions = set()
    for row in days.values():
        for k in agg:
            agg[k] += row[k]
        sessions |= row["sessions"]
    agg["sessions"] = len(sessions)
    agg["delegation_pct"] = round(pct(agg["delegate"], agg["delegate"] + agg["direct"]), 2)
    agg["offload_pct"] = round(pct(agg["sub_bytes"], agg["main_bytes"] + agg["sub_bytes"]), 2)
    return agg


def positive_int(value):
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"정수가 아니다: {value!r}")
    if n <= 0:
        raise argparse.ArgumentTypeError("--days 는 양수여야 한다")  # Codex 리뷰 P3
    return n


def main():
    ap = argparse.ArgumentParser(description="탐색 위임률 계측 (transcript 기반)")
    ap.add_argument("--days", type=positive_int, default=14, help="최근 N일 (기본 14)")
    ap.add_argument("--since", help="YYYY-MM-DD 이후만 (--days 보다 우선)")
    ap.add_argument("--overall", action="store_true", help="전체 기간 (전량 스캔 — 느림)")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--root", default=ROOT, help="transcript 루트 (기본 ~/.claude/projects)")
    args = ap.parse_args()

    if args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d").astimezone()
        except ValueError:
            sys.exit("--since 형식은 YYYY-MM-DD 여야 한다")
    elif args.overall:
        since_dt = None
    else:
        since_dt = datetime.now(tz=timezone.utc) - timedelta(days=args.days)

    days = collect(args.root, since_dt)
    agg = summarize(days)

    def day_row(day, row):
        return {
            "day": day,
            "sessions": len(row["sessions"]),
            "direct": row["direct"],
            "bash": row["bash"],
            "delegate": row["delegate"],
            "delegation_pct": round(pct(row["delegate"], row["delegate"] + row["direct"]), 2),
            "main_kb": round(row["main_bytes"] / 1024, 1),
            "sub_kb": round(row["sub_bytes"] / 1024, 1),
            "offload_pct": round(pct(row["sub_bytes"], row["main_bytes"] + row["sub_bytes"]), 2),
        }

    if args.as_json:
        print(json.dumps(
            {"overall": agg, "days": [day_row(d, r) for d, r in sorted(days.items())]},
            indent=2, ensure_ascii=False))
        return

    if not args.overall:
        print(f"{'DAY':<12}{'SESS':>5}{'DIRECT':>8}{'BASH':>7}{'AGENT':>7}"
              f"{'DELEG%':>8}{'MAIN_KB':>10}{'SUB_KB':>9}{'OFFLOAD%':>10}")
        for day, row in sorted(days.items()):
            r = day_row(day, row)
            print(f"{day:<12}{r['sessions']:>5}{r['direct']:>8}{r['bash']:>7}"
                  f"{r['delegate']:>7}{r['delegation_pct']:>7.2f}%"
                  f"{r['main_kb']:>10}{r['sub_kb']:>9}{r['offload_pct']:>9.2f}%")
        print()

    scope = args.since or ("all time" if args.overall else f"last {args.days}d")
    print(f"[{scope}] sessions={agg['sessions']} direct={agg['direct']} "
          f"(bash={agg['bash']}) agent={agg['delegate']} "
          f"delegation={agg['delegation_pct']}% | "
          f"main={agg['main_bytes'] // 1024}KB sub={agg['sub_bytes'] // 1024}KB "
          f"offload={agg['offload_pct']}%")


if __name__ == "__main__":
    main()
