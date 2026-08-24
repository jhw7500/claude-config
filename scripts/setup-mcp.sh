#!/bin/bash
# MCP 서버 등록 (opt-in). claude-config 철학상 install.sh 본체와 분리한다.
#   - 호스트별 상태(설치 MCP·키·경로)가 달라 install.sh에 묶지 않고 별도 실행한다.
# 사용: bash scripts/setup-mcp.sh [--no-internal] [--dry-run]
#   --no-internal : 사내 MCP(cts-*, jhw-notion, ssh-mcp) 제외
#   --dry-run     : 등록 명령을 출력만
# 키/경로는 secrets.local.env 에서 읽는다 (없으면 placeholder).
set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET_FILE="$REPO_DIR/secrets.local.env"
# shellcheck source=scripts/lib/secure-env-file.sh
. "$REPO_DIR/scripts/lib/secure-env-file.sh"
WITH_INTERNAL=1
DRY_RUN="${DRY_RUN:-0}"
for a in "$@"; do
  case "$a" in
    --no-internal) WITH_INTERNAL=0 ;;
    --with-internal) WITH_INTERNAL=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) echo "usage: setup-mcp.sh [--no-internal] [--dry-run]"; exit 0 ;;
    *) echo "unknown: $a"; exit 1 ;;
  esac
done

if [ -e "$SECRET_FILE" ] || [ -L "$SECRET_FILE" ]; then
  load_private_env_file "$SECRET_FILE"
else
  echo "[setup-mcp] secrets.local.env 없음 — 키 placeholder로 진행 (install -m 600 secrets.example.env secrets.local.env)"
fi

existing="$(claude mcp list 2>/dev/null || true)"

while IFS=$'\t' read -r name rest; do
  IFS=$'\t' read -ra tokens <<< "$rest"
  if printf '%s' "$existing" | grep -qF "$name"; then
    echo "[setup-mcp] 이미 등록: $name"
  elif [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] claude mcp add $name ${tokens[*]}"
  else
    echo "[setup-mcp] 등록: $name"
    claude mcp add "$name" "${tokens[@]}"
  fi
done < <(python3 - "$REPO_DIR/manifest/mcp.json" "$WITH_INTERNAL" "$DRY_RUN" <<'PY'
import json, sys, os
mcp = json.load(open(sys.argv[1]))
with_internal = sys.argv[2] == "1"
dry_run = sys.argv[3] == "1"
for name, c in mcp.items():
    if c.get("internal") and not with_internal:
        continue
    tokens = []
    # 실제 문법: claude mcp add <name> -e KEY=val -- <command> <args...>
    for k, v in c.get("env", {}).items():
        value = "<redacted>" if dry_run else os.path.expandvars(v)
        tokens += ["-e", "%s=%s" % (k, value)]
    tokens.append("--")
    tokens.append(os.path.expandvars(c["command"]))
    for a in c.get("args", []):
        tokens.append(os.path.expandvars(a))
    print(name + "\t" + "\t".join(tokens))
PY
)
echo "[setup-mcp] 완료. notion(OAuth)·repowire-channel 은 수동 확인 필요."
