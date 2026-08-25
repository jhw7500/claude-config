#!/usr/bin/env python3
"""배선된 훅이 실제로 발화하는지 점검한다.

배경: stop-text-required.py 는 2026-05-21 배선 후 약 3개월간 무동작이었다.
transcript 를 JSONL 로 파싱하지 못해 매번 fail-open 으로 통과했고, settings.json
에 배선돼 있다는 사실만으로는 그 상태를 알 수 없었다. fail-open 훅은 *정상 동작*
과 *조용한 무동작*이 외부에서 구분되지 않는다.

이 스크립트는 배선 목록과 실제 발화 이력을 대조해 그 간극을 드러낸다.

마커 형태가 아니어도 출력이 이미 고유하면 소스에 `# HOOK-OBSERVABLE: <문자열>`
로 선언해 관측 대상으로 삼는다 (scripts/timestamp-hook.py 참고).

훅 대부분은 조건부 출력이라 "출력 없음"이 정상 상태일 수 있다. 그래서 마커만
보면 조건 미충족과 무동작이 구분되지 않는다. 훅이 매 호출마다 하트비트 파일을
남기면(hooks/precompact-handoff.sh 참고) 그 mtime 을 발화 증거로 함께 읽는다.

발견 항목:
  MISSING       배선됐지만 스크립트 파일이 없음
  UNMANAGED     저장소 심링크가 아님 — 다른 호스트에서 재현되지 않음
  UNOBSERVABLE  마커를 주입하지 않아 발화 여부를 관측할 수 없음
  SILENT        마커가 있는데 관측 창에서 한 번도 발화하지 않음
  UNKNOWN       hook 증거 schema/event가 불명확해 안전하게 판정할 수 없음

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
DEFAULT_HEARTBEATS = Path(os.path.expanduser("~/.claude/hook-heartbeat"))
MARKER_RE = re.compile(r"\[([A-Z][A-Z0-9_-]{3,})\]")
# 대괄호 없는 마커는 문자열 리터럴의 "MARKER: ..." 형태만 인정한다.
# 그냥 대문자 상수(SETTLE_ATTEMPTS 등)까지 잡으면 허위 SILENT 가 쏟아진다.
BARE_MARKER_RE = re.compile(r"""["']([A-Z][A-Z0-9_]{5,}):""")
# 마커는 구분자로 이어진 두 글자 이상 토막이어야 한다 (TASK-NUDGE, STOP_HOOK_BLOCK).
# 구분자 없는 낱말(CRITICAL, MANDATORY, TODO)과 정규식 문자클래스 조각(A-Z0-9)을 배제한다.
MARKER_SHAPE_RE = re.compile(r"^[A-Z][A-Z0-9]+(?:[_-][A-Z0-9]{2,})+$")
# 마커 형태가 아니지만 출력이 이미 고유한 훅은 관측 문자열을 소스에 선언한다.
#   # HOOK-OBSERVABLE: 🕐 prompt @
# 사용자에게 보이는 출력을 바꾸지 않고 관측 가능하게 만드는 경로다.
DECLARE_KEYWORD = "HOOK-OBSERVABLE"
DECLARE_RE = re.compile(rf"{DECLARE_KEYWORD}:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)
SCRIPT_RE = re.compile(r"(?:\$HOME|~)?[\w./$-]*\.(?:py|sh)")

# Claude Code transcript에서 실제 hook 결과로 확인된 attachment schema와
# marker가 실리는 필드만 허용한다. 레코드 전체를 검색하면 사용자 원문,
# tool_result, 첨부한 소스 파일의 marker까지 발화로 오인한다.
HOOK_ATTACHMENT_FIELDS = {
    "hook_success": ("stdout", "stderr", "content"),
    "hook_system_message": ("content",),
    "hook_additional_context": ("content",),
    "async_hook_response": ("stdout", "stderr"),
    "hook_non_blocking_error": ("stdout", "stderr"),
    "hook_blocking_error": (),
}


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
    # 선언은 주석에 쓰므로 주석 제거 전에 뽑는다.
    declared = {d.strip() for d in DECLARE_RE.findall(src) if d.strip()}
    # 주석 전용 줄의 [TODO] 같은 표기는 주입되지 않는다 — 세면 영구 SILENT 소음이 된다.
    body = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    markers = set(MARKER_RE.findall(body))
    # STOP_HOOK_BLOCK 처럼 대괄호 없이 쓰는 마커도 잡는다.
    markers |= set(BARE_MARKER_RE.findall(body))
    markers = {m for m in markers if MARKER_SHAPE_RE.match(m)}
    markers.discard(DECLARE_KEYWORD)  # 선언 키워드 자체는 마커가 아니다
    return markers | declared


def payload_text(value: object) -> str:
    """허용된 payload 값만 marker 검색 가능한 문자열로 바꾼다."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def command_script(command: object) -> Path | None:
    """hook 증거의 command에서 실행 스크립트 힌트를 뽑는다."""
    if not isinstance(command, str):
        return None
    match = SCRIPT_RE.search(command)
    if not match:
        return None
    raw = os.path.expandvars(match.group(0))
    raw = raw.replace("~", os.path.expanduser("~"))
    return Path(raw).resolve()


def attachment_payload(schema: str, attachment: dict) -> str:
    """schema별 출력 payload만 읽고 command/response provenance는 제외한다."""
    if schema == "hook_blocking_error":
        blocking = attachment.get("blockingError")
        if isinstance(blocking, dict):
            blocking = blocking.get("blockingError")
        return payload_text(blocking)
    return "\n".join(
        payload_text(attachment.get(name))
        for name in HOOK_ATTACHMENT_FIELDS[schema]
    )


def hook_evidence(record: dict) -> dict | None:
    """신뢰 가능한 hook payload 또는 판정 불가 hook-like payload를 반환한다.

    ``trusted``가 참일 때만 실제 발화로 센다. 미래 schema나 event가 빠진
    hook 레코드는 marker를 포함하더라도 정상으로 추정하지 않고 UNKNOWN 후보가
    된다. 일반 user/system/file attachment는 None으로 버린다.
    """
    record_type = record.get("type")
    if record_type == "attachment":
        attachment = record.get("attachment")
        if not isinstance(attachment, dict):
            return None
        schema = attachment.get("type")
        if isinstance(schema, str) and schema in HOOK_ATTACHMENT_FIELDS:
            event = attachment.get("hookEvent")
            return {
                "trusted": isinstance(event, str) and bool(event),
                "event": event if isinstance(event, str) and event else None,
                "text": attachment_payload(schema, attachment),
                "script": command_script(attachment.get("command")),
                "attachment": attachment,
            }
        if isinstance(schema, str) and (
            schema.startswith("hook_") or schema == "async_hook_response"
        ):
            event = attachment.get("hookEvent")
            return {
                "trusted": False,
                "event": event if isinstance(event, str) and event else None,
                "text": payload_text(attachment),
                "script": command_script(attachment.get("command")),
                "attachment": attachment,
            }
        return None

    subtype = record.get("subtype")
    if record_type == "system" and subtype == "stop_hook_summary":
        # hookInfos에는 command 등 payload가 아닌 provenance도 있으므로 제외한다.
        text = "\n".join((
            payload_text(record.get("hookErrors")),
            payload_text(record.get("hookAdditionalContext")),
        ))
        return {
            "trusted": True,
            "event": "Stop",
            "text": text,
            "script": None,
            "attachment": record,
        }
    if record_type == "system" and isinstance(subtype, str) and subtype.startswith("hook_"):
        return {
            "trusted": False,
            "event": None,
            "text": payload_text(record),
            "script": None,
            "attachment": record,
        }
    return None


def evidence_signature(record: dict, evidence: dict, path: Path, line_number: int) -> tuple:
    """동일 hook 호출의 여러 transcript 표현을 한 번만 세기 위한 서명."""
    attachment = evidence["attachment"]
    session = record.get("sessionId") or record.get("session_id")
    hook_id = (
        attachment.get("toolUseID")
        or record.get("toolUseID")
        or attachment.get("processId")
        or record.get("processId")
    )
    event = evidence.get("event") or ""
    if session and hook_id:
        return ("hook", str(session), str(hook_id), event)
    uuid = record.get("uuid")
    if uuid:
        return ("uuid", str(uuid), event)
    return ("line", str(path), line_number, event)


def script_matches(target: tuple[str, str], hint: Path) -> bool:
    """command의 해석된 실경로가 설정 target과 정확히 같은지 확인한다."""
    return Path(target[0]) == hint


def scan_transcripts(
    root: Path, targets: dict[tuple[str, str], set[str]], days: int
) -> dict:
    """실제 hook 증거를 ``(실제 script, event)``별로 집계한다."""
    hits = {target: 0 for target in targets}
    unknown = {target: 0 for target in targets}
    last_seen: dict[tuple[str, str], str] = {}
    unknown_last_seen: dict[tuple[str, str], str] = {}
    markers = set().union(*targets.values()) if targets else set()
    marker_hits = {marker: 0 for marker in markers}
    if not markers or not root.is_dir():
        return {
            "hits": hits,
            "unknown": unknown,
            "last_seen": last_seen,
            "unknown_last_seen": unknown_last_seen,
            "marker_hits": marker_hits,
            "files": 0,
        }

    by_event_marker: dict[tuple[str, str], set[tuple[str, str]]] = {}
    by_marker: dict[str, set[tuple[str, str]]] = {}
    for target, target_markers in targets.items():
        for marker in target_markers:
            by_event_marker.setdefault((target[1], marker), set()).add(target)
            by_marker.setdefault(marker, set()).add(target)

    # JSON이 non-ASCII marker를 \u escape한 경우에도 파싱 대상을 놓치지 않는다.
    line_needles = {
        marker: (marker, json.dumps(marker, ensure_ascii=True)[1:-1])
        for marker in markers
    }
    seen: dict[tuple[str, str], set[tuple]] = {target: set() for target in targets}
    unknown_seen: dict[tuple[str, str], set[tuple]] = {target: set() for target in targets}
    marker_seen: dict[str, set[tuple]] = {marker: set() for marker in markers}
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
            for line_number, line in enumerate(handle, 1):
                present = {
                    marker for marker, needles in line_needles.items()
                    if any(needle in line for needle in needles)
                }
                if not present:
                    continue  # 대부분의 줄은 여기서 끝난다 (파싱 회피)
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                evidence = hook_evidence(record)
                if evidence is None:
                    continue
                present = {marker for marker in present if marker in evidence["text"]}
                if not present:
                    continue

                day = (record.get("timestamp") or "")[:10]
                signature = evidence_signature(record, evidence, path, line_number)
                valid_targets: set[tuple[str, str]] = set()
                uncertain_targets: set[tuple[str, str]] = set()
                for marker in present:
                    event = evidence.get("event")
                    candidates = (
                        set(by_event_marker.get((event, marker), set()))
                        if event else set(by_marker.get(marker, set()))
                    )
                    if not candidates:
                        continue
                    hint = evidence.get("script")
                    if hint is not None:
                        matched = {target for target in candidates if script_matches(target, hint)}
                        if not matched:
                            uncertain_targets |= candidates
                            continue
                        candidates = matched

                    if evidence["trusted"] and len(candidates) == 1:
                        target = next(iter(candidates))
                        valid_targets.add(target)
                        marker_key = signature + (target,)
                        if marker_key not in marker_seen[marker]:
                            marker_seen[marker].add(marker_key)
                            marker_hits[marker] += 1
                    else:
                        uncertain_targets |= candidates

                for target in valid_targets:
                    if signature not in seen[target]:
                        seen[target].add(signature)
                        hits[target] += 1
                    if day and day > last_seen.get(target, ""):
                        last_seen[target] = day
                for target in uncertain_targets:
                    if signature not in unknown_seen[target]:
                        unknown_seen[target].add(signature)
                        unknown[target] += 1
                    if day and day > unknown_last_seen.get(target, ""):
                        unknown_last_seen[target] = day
    return {
        "hits": hits,
        "unknown": unknown,
        "last_seen": last_seen,
        "unknown_last_seen": unknown_last_seen,
        "marker_hits": marker_hits,
        "files": files,
    }


def heartbeat_seen(heartbeats: Path, name: str, days: int) -> str:
    """관측 창 안의 하트비트면 날짜(YYYY-MM-DD)를, 아니면 빈 문자열을 반환한다."""
    path = heartbeats / name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    if mtime < time.time() - days * 86400:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


def audit(settings: Path, repo: Path, transcripts: Path, days: int,
          heartbeats: Path = DEFAULT_HEARTBEATS) -> dict:
    entries = wired_hooks(settings)
    repo_root = str(repo.resolve())
    rows = []
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
        if row["status"] == "ok" and not markers:
            row["status"] = "UNOBSERVABLE"
        rows.append(row)

    targets: dict[tuple[str, str], set[str]] = {}
    script_events: dict[str, set[str]] = {}
    heartbeat_owners: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        if not row["real"]:
            continue
        key = (row["real"], row["event"])
        targets.setdefault(key, set()).update(row["markers"])
        script_events.setdefault(row["real"], set()).add(row["event"])
        heartbeat_owners.setdefault(os.path.basename(row["script"]), set()).add(key)

    scan = scan_transcripts(transcripts, targets, days)
    for row in rows:
        if row["status"] in ("inline", "MISSING"):
            continue
        key = (row["real"], row["event"])
        beat = heartbeat_seen(heartbeats, os.path.basename(row["script"]), days)
        row["heartbeat"] = beat
        fired = scan["hits"].get(key, 0)
        unknown = scan["unknown"].get(key, 0)
        multi_event = len(script_events.get(row["real"], set())) > 1
        shared_heartbeat = len(
            heartbeat_owners.get(os.path.basename(row["script"]), set())
        ) > 1
        ambiguous_heartbeat = bool(beat and (multi_event or shared_heartbeat))
        row["fired"] = fired
        row["unknown"] = unknown
        row["heartbeat_ambiguous"] = ambiguous_heartbeat
        row["last_seen"] = max(
            scan["last_seen"].get(key, ""),
            scan["unknown_last_seen"].get(key, ""),
            beat,
        )

        if fired:
            row["evidence"] = "marker"
        elif beat and not ambiguous_heartbeat:
            row["evidence"] = "heartbeat"
        elif unknown:
            row["evidence"] = "unknown-schema"
        elif ambiguous_heartbeat:
            row["evidence"] = "ambiguous-heartbeat"
        else:
            row["evidence"] = ""

        if row["status"] == "UNMANAGED":
            continue
        if fired or (beat and not ambiguous_heartbeat):
            row["status"] = "ok"
        elif unknown or ambiguous_heartbeat:
            row["status"] = "UNKNOWN"
        elif row["markers"]:
            row["status"] = "SILENT"
        else:
            row["status"] = "UNOBSERVABLE"

    target_hits = [
        {
            "script": script,
            "event": event,
            "hits": scan["hits"].get((script, event), 0),
            "unknown": scan["unknown"].get((script, event), 0),
            "last_seen": scan["last_seen"].get((script, event), ""),
        }
        for script, event in sorted(targets)
    ]
    return {
        "days": days,
        "files_scanned": scan["files"],
        "rows": rows,
        "marker_hits": scan["marker_hits"],
        "target_hits": target_hits,
    }


def render(result: dict) -> str:
    lines = [f"훅 자가진단 — 최근 {result['days']}일 / transcript {result['files_scanned']}파일", ""]
    lines.append(f"  {'상태':<13} {'이벤트':<17} {'스크립트':<34} {'발화':>6} {'근거':<10} 마지막")
    order = {
        "MISSING": 0,
        "SILENT": 1,
        "UNKNOWN": 2,
        "UNMANAGED": 3,
        "UNOBSERVABLE": 4,
        "ok": 5,
        "inline": 6,
    }
    for row in sorted(result["rows"], key=lambda r: (order.get(r["status"], 9), r["event"])):
        name = os.path.basename(row["script"]) if row["script"] else row["command"][:34]
        fired = row.get("fired", "")
        lines.append(f"  {row['status']:<13} {row['event']:<17} {name:<34} "
                     f"{fired:>6} {row.get('evidence', ''):<10} {row.get('last_seen', '')}")
    problems = [r for r in result["rows"]
                if r["status"] in (
                    "MISSING", "SILENT", "UNKNOWN", "UNMANAGED", "UNOBSERVABLE"
                )]
    lines += ["", f"발견 {len(problems)}건"]
    for row in problems:
        name = os.path.basename(row["script"]) if row["script"] else row["command"]
        hint = {
            "MISSING": "배선됐지만 파일이 없다 — 배선을 지우거나 파일을 복구한다",
            "SILENT": "마커가 있는데 관측 창에서 한 번도 발화하지 않았다 — 무동작 의심",
            "UNKNOWN": "hook 증거 schema/event 귀속이 불명확하다 — 검증 규칙 갱신 필요",
            "UNMANAGED": "저장소 심링크가 아니다 — 다른 호스트에서 재현되지 않는다",
            "UNOBSERVABLE": "마커도 하트비트도 없어 발화를 관측할 수 없다",
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
    parser.add_argument("--heartbeats", default=str(DEFAULT_HEARTBEATS),
                        help="훅이 남기는 발화 하트비트 디렉터리")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--strict", action="store_true",
                        help="MISSING/SILENT/UNKNOWN 발견 시 exit 1")
    args = parser.parse_args(argv)

    result = audit(Path(args.settings), Path(args.repo), Path(args.transcripts), args.days,
                   Path(args.heartbeats))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.as_json else render(result))
    if args.strict and any(
        r["status"] in ("MISSING", "SILENT", "UNKNOWN") for r in result["rows"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
