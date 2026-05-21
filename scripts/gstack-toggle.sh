#!/usr/bin/env bash
# gstack-toggle — gstack 스킬셋을 한 번에 enable/disable
# 권한 700인 디렉토리(gstack setup이 생성)와 gstack 본체만 처리.
# 사용자가 따로 추가한 비-gstack 스킬(agent-council, email-assistant 등)은 건드리지 않음.

set -e

SKILLS_DIR="$HOME/.claude/skills"
DISABLED_DIR="$HOME/.claude/_disabled/gstack-set"
MANIFEST="$DISABLED_DIR/.manifest"

list_gstack_dirs() {
  # 권한 700인 directory + gstack 본체
  find "$SKILLS_DIR" -maxdepth 1 -mindepth 1 -type d -perm 700 2>/dev/null
  [ -d "$SKILLS_DIR/gstack" ] && echo "$SKILLS_DIR/gstack"
}

cmd_status() {
  if [ -d "$SKILLS_DIR/gstack" ] || [ -n "$(list_gstack_dirs 2>/dev/null)" ]; then
    local count
    count=$(list_gstack_dirs | wc -l)
    echo "ENABLED ($count gstack 디렉토리)"
  elif [ -d "$DISABLED_DIR" ]; then
    local count
    count=$(find "$DISABLED_DIR" -maxdepth 1 -mindepth 1 -type d | wc -l)
    echo "DISABLED ($count 디렉토리 보관 중: $DISABLED_DIR)"
  else
    echo "UNKNOWN — gstack 흔적 없음"
  fi
}

cmd_off() {
  if [ ! -d "$SKILLS_DIR/gstack" ] && [ -z "$(list_gstack_dirs 2>/dev/null)" ]; then
    echo "이미 비활성 상태"
    return
  fi
  mkdir -p "$DISABLED_DIR"
  : > "$MANIFEST"
  local count=0
  while IFS= read -r dir; do
    [ -z "$dir" ] && continue
    local name
    name=$(basename "$dir")
    mv "$dir" "$DISABLED_DIR/$name"
    echo "$name" >> "$MANIFEST"
    count=$((count + 1))
  done < <(list_gstack_dirs)
  echo "비활성화 완료: $count 디렉토리 → $DISABLED_DIR"
  echo "→ 새 Claude Code 세션부터 반영"
}

cmd_on() {
  if [ ! -d "$DISABLED_DIR" ]; then
    echo "오류: 보관된 gstack 디렉토리 없음 ($DISABLED_DIR)"
    return 1
  fi
  local count=0
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    if [ -d "$DISABLED_DIR/$name" ]; then
      mv "$DISABLED_DIR/$name" "$SKILLS_DIR/$name"
      count=$((count + 1))
    fi
  done < "$MANIFEST"
  rm -f "$MANIFEST"
  rmdir "$DISABLED_DIR" 2>/dev/null || true
  echo "활성화 완료: $count 디렉토리 복원"
  echo "→ 새 Claude Code 세션부터 반영"
}

cmd_update() {
  local was_disabled=0
  if [ ! -d "$SKILLS_DIR/gstack" ]; then
    echo "현재 비활성 상태 → 임시 활성화"
    cmd_on
    was_disabled=1
  fi
  echo ""
  echo "=== git pull ==="
  (cd "$SKILLS_DIR/gstack" && git pull --ff-only) 2>&1 | tail -10
  echo ""
  echo "=== setup 실행 (build + skill register) ==="
  (cd "$SKILLS_DIR/gstack" && ./setup) 2>&1 | tail -20
  echo ""
  if [ "$was_disabled" = 1 ]; then
    echo "원래 비활성 상태였으므로 다시 비활성화"
    cmd_off
  fi
  echo ""
  echo "✓ 업데이트 완료 — 새 Claude Code 세션부터 반영"
}

case "${1:-status}" in
  on|enable)   cmd_on ;;
  off|disable) cmd_off ;;
  update|up)   cmd_update ;;
  status)      cmd_status ;;
  *)
    echo "사용법: $(basename "$0") [on|off|update|status]"
    echo "  on/enable   — gstack 스킬셋 활성화"
    echo "  off/disable — gstack 스킬셋 비활성화"
    echo "  update/up   — git pull + setup (자동: 비활성 시 임시 활성→복원)"
    echo "  status      — 현재 상태 확인 (기본)"
    exit 1
    ;;
esac
