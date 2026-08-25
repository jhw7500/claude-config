#!/bin/bash
# MCP 서버 등록 (opt-in). claude-config 철학상 install.sh 본체와 분리한다.
#   - 호스트별 상태(설치 MCP·키·경로)가 달라 install.sh에 묶지 않고 별도 실행한다.
# 사용: bash scripts/setup-mcp.sh [--no-internal] [--check|--dry-run|--apply] [--migrate-local]
#   --no-internal : 사내 MCP(cts-*, jhw-notion, ssh-mcp) 제외
#   --check       : manifest와 user scope를 비교만 함 (기본값)
#   --dry-run     : --check 호환 별칭
#   --apply       : preview 후 missing/drift 항목을 user scope에 적용
#   --migrate-local : --apply 시 같은 이름의 legacy local 항목을 user로 이동
# Private values must never enter shell tracing output.
case "$-" in
  *x*) set +x ;;
esac
set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_INTERNAL=1
APPLY=0
MIGRATE_LOCAL=0
MODE_SET=""
for a in "$@"; do
  case "$a" in
    --no-internal) WITH_INTERNAL=0 ;;
    --with-internal) WITH_INTERNAL=1 ;;
    --check|--dry-run)
      if [ "$MODE_SET" = "apply" ]; then
        echo "conflicting mode options: preview and apply" >&2
        exit 64
      fi
      MODE_SET="preview"
      ;;
    --apply)
      if [ "$MODE_SET" = "preview" ]; then
        echo "conflicting mode options: preview and apply" >&2
        exit 64
      fi
      MODE_SET="apply"
      APPLY=1
      ;;
    --migrate-local) MIGRATE_LOCAL=1 ;;
    -h|--help) echo "usage: setup-mcp.sh [--no-internal] [--check|--dry-run|--apply] [--migrate-local]"; exit 0 ;;
    *) echo "unknown: $a"; exit 1 ;;
  esac
done

if [ "$MIGRATE_LOCAL" = "1" ] && [ "$APPLY" != "1" ]; then
  echo "--migrate-local requires --apply" >&2
  exit 64
fi

helper_args=(
  --manifest "$REPO_DIR/manifest/mcp.json"
)
if [ "$WITH_INTERNAL" = "1" ]; then
  helper_args+=(--with-internal)
fi
if [ "$APPLY" = "1" ]; then
  helper_args+=(--apply)
fi
if [ "$MIGRATE_LOCAL" = "1" ]; then
  helper_args+=(--migrate-local)
fi
python3 "$REPO_DIR/scripts/lib/mcp_config_sync.py" "${helper_args[@]}"
