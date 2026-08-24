#!/usr/bin/env python3
"""배선된 훅이 실제로 발화하는지 점검한다.

배경: stop-text-required.py 는 2026-05-21 배선 후 약 3개월간 무동작이었다.
transcript 를 JSONL 로 파싱하지 못해 매번 fail-open 으로 통과했고, settings.json
에 배선돼 있다는 사실만으로는 그 상태를 알 수 없었다. fail-open 훅은 *정상 동작*
과 *조용한 무동작*이 외부에서 구분되지 않는다.

이 스크립트는 배선 목록과 실제 발화 이력을 대조해 그 간극을 드러낸다.

발견 항목:
  MISSING       배선됐지만 스크립트 파일이 없음
  UNMANAGED     저장소 심링크가 아님 — 다른 호스트에서 재현되지 않음
  UNOBSERVABLE  마커를 주입하지 않아 발화 여부를 관측할 수 없음
  SILENT        마커가 있는데 관측 창에서 한 번도 발화하지 않음

사용:
  python3 hook-selfcheck.py                 # 최근 7일
  python3 hook-selfcheck.py --days 30 --json
  python3 hook-selfcheck.py --strict        # 발견 시 exit 1 (CI/cron 용)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parents[1]
MARKER_RE = re.compile(r"\[([A-Z][A-Z0-9_-]{3,})\]")
# 대괄호 없는 마커는 문자열 리터럴의 "MARKER: ..." 형태만 인정한다.
# 그냥 대문자 상수(SETTLE_ATTEMPTS 등)까지 잡으면 허위 SILENT 가 쏟아진다.
BARE_MARKER_RE = re.compile(r"""["']([A-Z][A-Z0-9_]{5,}):""")
SCRIPT_RE = re.compile(r"(?:\$HOME|~)?[\w./$-]*\.(?:py|sh)")


def wired_hooks(settings_path: Path) -> list[dict]:
    """settings.json 의 훅 배선을 (event, matcher, command, script) 로 편다."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for event, groups in (data.get("hooks") or {}).items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            matcher = group.get("matcher", "*")
            for hook in group.get("hooks", []) or []:
                command = hook.get("command", "") or ""
                match = SCRIPT_RE.search(command)
                script = None
                if match:
                    script = os.path.expandvars(
                        match.group(0).replace("~", os.path.expanduser("~"))
                    )
                out.append({"event": event, "matcher": matcher,
                            "command": command, "script": script})
    return out


def extract_markers(path: Path) -> set[str]:
    """훅 소스가 주입하는 마커를 소스에서 직접 뽑는다 (추측하지 않는다)."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    markers = set(MARKER_RE.findall(src))
    # STOP_HOOK_BLOCK 처럼 대괄호 없이 쓰는 마커도 잡는다.
    markers |= set(BARE_MARKER_RE.findall(src))
    return {m for m in markers if not m.isdigit()}


def record_is_assistant(record: dict) -> bool:
    """모델 자신이 마커를 언급한 레코드인지. 발화로 세면 과대계상된다."""
    if record.get("type") == "assistant":
        return True
    message = record.get("message")
    return isinstance(message, dict) and message.get("role") == "assistant"


def scan_transcripts(root: Path, markers: set[str], days: int) -> dict:
    """마커별 발화 횟수와 마지막 발화일.

    훅 출력은 한 형태로 저장되지 않는다 — attachment.stdout, message.content,
    중첩 content[0].content, system 레코드에 흩어진다. 따라서 레코드 전체를
    문자열로 훑되 assistant 레코드만 제외한다.
    """
    hits = {m: 0 for m in markers}
    last_seen: dict[str, str] = {}
    if not markers or not root.is_dir():
        return {"hits": hits, "last_seen": last_seen, "files": 0}
    cutoff = time.time() - days * 86400
    files = 0
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        files += 1
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                present = [m for m in markers if m in line]
                if not present:
                    continue  # 대부분의 줄은 여기서 끝난다 (파싱 회피)
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record_is_assistant(record):
                    continue
                day = (record.get("timestamp") or "")[:10]
                for marker in present:
                    hits[marker] += 1
                    if day and day > last_seen.get(marker, ""):
                        last_seen[marker] = day
    return {"hits": hits, "last_seen": last_seen, "files": files}


def audit(settings: Path, repo: Path, transcripts: Path, days: int) -> dict:
    entries = wired_hooks(settings)
    repo_root = str(repo.resolve())
    rows, all_markers = [], set()
    for entry in entries:
        script = entry["script"]
        row = dict(entry, status="ok", markers=[], real=None)
        if script is None:
            row["status"] = "inline"       # repowire/rtk 등 스크립트 아닌 배선
            rows.append(row)
            continue
        path = Path(script)
        if not path.exists():
            row["status"] = "MISSING"
            rows.append(row)
            continue
        real = path.resolve()
        row["real"] = str(real)
        if not str(real).startswith(repo_root):
            row["status"] = "UNMANAGED"
        markers = extract_markers(real)
        row["markers"] = sorted(markers)
        all_markers |= markers
        if row["status"] == "ok" and not markers:
            row["status"] = "UNOBSERVABLE"
        rows.append(row)

    scan = scan_transcripts(transcripts, all_markers, days)
    for row in rows:
        if not row["markers"]:
            continue
        fired = sum(scan["hits"].get(m, 0) for m in row["markers"])
        row["fired"] = fired
        row["last_seen"] = max((scan["last_seen"].get(m, "") for m in row["markers"]),
                               default="")
        if fired == 0 and row["status"] == "ok":
            row["status"] = "SILENT"
    return {"days": days, "files_scanned": scan["files"], "rows": rows,
            "marker_hits": scan["hits"]}


def render(result: dict) -> str:
    lines = [f"훅 자가진단 — 최근 {result['days']}일 / transcript {result['files_scanned']}파일", ""]
    lines.append(f"  {'상태':<13} {'이벤트':<17} {'스크립트':<34} {'발화':>6}  마지막")
    order = {"MISSING": 0, "SILENT": 1, "UNMANAGED": 2, "UNOBSERVABLE": 3, "ok": 4, "inline": 5}
    for row in sorted(result["rows"], key=lambda r: (order.get(r["status"], 9), r["event"])):
        name = os.path.basename(row["script"]) if row["script"] else row["command"][:34]
        fired = row.get("fired", "")
        lines.append(f"  {row['status']:<13} {row['event']:<17} {name:<34} "
                     f"{fired:>6}  {row.get('last_seen', '')}")
    problems = [r for r in result["rows"]
                if r["status"] in ("MISSING", "SILENT", "UNMANAGED", "UNOBSERVABLE")]
    lines += ["", f"발견 {len(problems)}건"]
    for row in problems:
        name = os.path.basename(row["script"]) if row["script"] else row["command"]
        hint = {
            "MISSING": "배선됐지만 파일이 없다 — 배선을 지우거나 파일을 복구한다",
            "SILENT": "마커가 있는데 관측 창에서 한 번도 발화하지 않았다 — 무동작 의심",
            "UNMANAGED": "저장소 심링크가 아니다 — 다른 호스트에서 재현되지 않는다",
            "UNOBSERVABLE": "마커를 주입하지 않아 발화를 관측할 수 없다",
        }[row["status"]]
        lines.append(f"  [{row['status']}] {name} ({row['event']}) — {hint}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="배선된 훅의 실제 발화 여부를 점검한다")
    parser.add_argument("--settings", default=os.path.expanduser("~/.claude/settings.json"))
    parser.add_argument(
        "--repo", default=str(DEFAULT_REPO),
        help="관리 기준이 되는 저장소 체크아웃. 배선된 심링크가 가리키는 체크아웃을 "
             "줘야 한다 — worktree 경로를 주면 본 체크아웃 배포분이 전부 UNMANAGED 로 보인다")
    parser.add_argument("--transcripts", default=os.path.expanduser("~/.claude/projects"))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--strict", action="store_true",
                        help="MISSING/SILENT 발견 시 exit 1")
    args = parser.parse_args(argv)

    result = audit(Path(args.settings), Path(args.repo), Path(args.transcripts), args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.as_json else render(result))
    if args.strict and any(r["status"] in ("MISSING", "SILENT") for r in result["rows"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
