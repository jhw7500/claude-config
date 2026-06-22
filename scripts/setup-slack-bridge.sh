#!/bin/bash
# Slack 브릿지 설치 (opt-in). setup-mcp.sh 패턴.
# 사용: bash scripts/setup-slack-bridge.sh [--dry-run]
#   secrets.local.env 에서 SLACK_* 토큰을 읽어 systemd --user 서비스를 설치/기동.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_DIR="$REPO_DIR/slack-bridge"
DRY_RUN=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) echo "usage: setup-slack-bridge.sh [--dry-run]"; exit 0 ;;
    *) echo "unknown: $a"; exit 1 ;;
  esac
done

if [ ! -f "$REPO_DIR/secrets.local.env" ]; then
  echo "[setup] secrets.local.env 없음 — cp secrets.example.env secrets.local.env 후 SLACK_* 채우기"; exit 1
fi
# shellcheck source=/dev/null
set -a; . "$REPO_DIR/secrets.local.env"; set +a
for v in SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_CHANNEL_ID SLACK_ALLOWED_USER_ID; do
  if [ -z "${!v:-}" ]; then echo "[setup] $v 비어있음 (secrets.local.env)"; exit 1; fi
done
echo "[setup] tokens OK (bot ${SLACK_BOT_TOKEN:0:9}…, channel $SLACK_CHANNEL_ID)"

CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ]; then echo "[setup] 'claude' not on PATH"; exit 1; fi
SERVICE_PATH="$HOME/.config/systemd/user/claude-slack-bridge.service"
UNIT="$(sed "s|__PATH__|$PATH|g" "$BRIDGE_DIR/claude-slack-bridge.service.template")"

if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] venv: $BRIDGE_DIR/.venv ; deps: slack_bolt"
  echo "[dry-run] would write $SERVICE_PATH:"; echo "$UNIT"
  echo "[dry-run] systemctl --user enable --now claude-slack-bridge.service"
  exit 0
fi

echo "[setup] creating venv + installing deps"
# Prefer uv (no ensurepip needed). Fall back to stdlib venv (requires python3-venv).
if command -v uv >/dev/null 2>&1; then
  uv venv "$BRIDGE_DIR/.venv"
  uv pip install --python "$BRIDGE_DIR/.venv/bin/python" -q -r "$BRIDGE_DIR/requirements.txt"
else
  python3 -m venv "$BRIDGE_DIR/.venv"
  "$BRIDGE_DIR/.venv/bin/pip" install -q -r "$BRIDGE_DIR/requirements.txt"
fi

mkdir -p "$(dirname "$SERVICE_PATH")"
[ -f "$SERVICE_PATH" ] && cp "$SERVICE_PATH" "$SERVICE_PATH.bak"
printf '%s\n' "$UNIT" > "$SERVICE_PATH"
systemctl --user daemon-reload
systemctl --user enable --now claude-slack-bridge.service
sleep 2
systemctl --user --no-pager status claude-slack-bridge.service | head -12 || true
echo "[setup] 완료. 로그: tail -f ~/.claude-slack-bridge.log"
echo "[setup] Slack 채널에서 'sessions' 입력해 동작 확인."
