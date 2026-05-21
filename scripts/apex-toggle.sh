#!/usr/bin/env bash
# apex-toggle — APEX 7 framework (apex/base/paul/seed/skillsmith/aegis)을 한 번에 enable/disable
# carl은 base 디렉토리 안에 carl-hygiene.md로 통합돼있어 별도 그룹 없음

set -e

COMMANDS_DIR="$HOME/.claude/commands"
DISABLED_DIR="$HOME/.claude/_disabled/commands"
APEX_GROUPS=(apex base paul seed skillsmith aegis)

cmd_status() {
  local on=0 off=0
  for g in "${APEX_GROUPS[@]}"; do
    [ -d "$COMMANDS_DIR/$g" ] && on=$((on+1))
    [ -d "$DISABLED_DIR/$g" ] && off=$((off+1))
  done
  if [ "$on" -gt 0 ] && [ "$off" -eq 0 ]; then
    echo "ENABLED ($on/${#APEX_GROUPS[@]} 그룹 활성: ${APEX_GROUPS[*]})"
  elif [ "$on" -eq 0 ] && [ "$off" -gt 0 ]; then
    echo "DISABLED ($off/${#APEX_GROUPS[@]} 그룹 비활성, 보관: $DISABLED_DIR)"
  elif [ "$on" -gt 0 ] && [ "$off" -gt 0 ]; then
    echo "MIXED — 활성 $on개, 비활성 $off개"
    echo "  활성: $(for g in "${APEX_GROUPS[@]}"; do [ -d "$COMMANDS_DIR/$g" ] && echo -n "$g "; done)"
    echo "  비활성: $(for g in "${APEX_GROUPS[@]}"; do [ -d "$DISABLED_DIR/$g" ] && echo -n "$g "; done)"
  else
    echo "UNKNOWN — APEX 그룹 흔적 없음"
  fi
}

cmd_on() {
  local count=0
  mkdir -p "$COMMANDS_DIR"
  for g in "${APEX_GROUPS[@]}"; do
    if [ -d "$DISABLED_DIR/$g" ] && [ ! -d "$COMMANDS_DIR/$g" ]; then
      mv "$DISABLED_DIR/$g" "$COMMANDS_DIR/$g"
      count=$((count+1))
      echo "활성: $g"
    fi
  done
  if [ "$count" -eq 0 ]; then
    echo "이동할 그룹 없음 (이미 활성 또는 보관 위치 비어있음)"
  else
    echo "활성화 완료: $count 그룹"
    echo "→ 새 Claude Code 세션부터 반영"
  fi
}

cmd_off() {
  local count=0
  mkdir -p "$DISABLED_DIR"
  for g in "${APEX_GROUPS[@]}"; do
    if [ -d "$COMMANDS_DIR/$g" ] && [ ! -d "$DISABLED_DIR/$g" ]; then
      mv "$COMMANDS_DIR/$g" "$DISABLED_DIR/$g"
      count=$((count+1))
      echo "비활성: $g"
    fi
  done
  if [ "$count" -eq 0 ]; then
    echo "이동할 그룹 없음 (이미 비활성)"
  else
    echo "비활성화 완료: $count 그룹"
    echo "→ 새 Claude Code 세션부터 반영"
  fi
}

case "${1:-status}" in
  on|enable)   cmd_on ;;
  off|disable) cmd_off ;;
  status)      cmd_status ;;
  *)
    echo "사용법: $(basename "$0") [on|off|status]"
    echo "  on/enable   — APEX 7 framework 활성화 (apex/base/paul/seed/skillsmith/aegis)"
    echo "  off/disable — APEX 비활성화"
    echo "  status      — 현재 상태"
    echo ""
    echo "업데이트는 'npx @chrisai/{framework}' 직접 실행 (개별 npm 패키지 관리)"
    exit 1
    ;;
esac
