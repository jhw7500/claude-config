#!/usr/bin/env bash
# 심링크 배포 공통 헬퍼.
#
# ln -sfn 은 목적지가 실디렉터리일 때 실패하지 않고 그 "안에" 링크를 만든다.
# 그러면 배포는 조용히 실패하고, settings.json 이 가리키는 경로는 여전히
# 디렉터리라 훅이 실행되지 않는다. 훅은 안 도는 것과 정상인 것이 겉으로 같아서
# 이 실패는 오래 숨는다 — scripts/hook-selfcheck.py 가 필요했던 이유와 같다.
#
# link_safely <src> <dest> [archive_dir]
#   목적지가 심링크가 아닌 실체(파일·디렉터리)면 치운 뒤 링크한다.
#   치우지 못하면 링크하지 않고 1을 반환한다 — 조용히 덮지 않는다.
#
#   archive_dir 를 주면 그 디렉터리 안으로 옮기고, 없으면 목적지 옆에
#   `.replaced.<타임스탬프>` 로 붙인다.
#
#   스킬 배포에는 archive_dir 가 필수다. ~/.claude/skills/ 에서는 SKILL.md 보유가
#   곧 스킬 인식 조건이라(실측: SKILL.md 를 가진 10개 디렉터리가 로드된 스킬 10개와
#   정확히 일치, 없는 3개는 미로드), 스킬 디렉터리를 옆에 백업하면 그 사본이
#   중복 스킬로 로드된다. 백업을 스캔 범위 밖으로 빼야 한다.

link_safely() {
  local src=$1 dest=$2 archive_dir=${3:-} backup
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    if [ -n "$archive_dir" ]; then
      if ! mkdir -p "$archive_dir" 2>/dev/null; then
        echo "[link_safely] 경고: 백업 디렉터리 $archive_dir 를 만들지 못해 링크를 건너뛴다" >&2
        return 1
      fi
      backup="$archive_dir/$(basename "$dest")"
    else
      backup="$dest.replaced.$(date +%Y%m%d%H%M%S)"
    fi
    # 백업 경로가 이미 있으면 덮지 않고 번호를 붙인다. archive_dir 는 basename 만
    # 쓰므로 같은 디렉터리를 재사용하면 충돌하고, 옆에 두는 경로도 같은 초에 두 번
    # 호출되면 겹친다. 어느 쪽이든 기존 백업을 잃지 않는다.
    if [ -e "$backup" ]; then
      local n=1
      while [ -e "$backup.$n" ]; do n=$((n + 1)); done
      backup="$backup.$n"
    fi
    if ! mv "$dest" "$backup" 2>/dev/null; then
      echo "[link_safely] 경고: $dest 를 치우지 못해 링크를 건너뛴다" >&2
      return 1
    fi
    echo "[link_safely] 기존 실체 백업 -> $backup"
  fi
  ln -sfn "$src" "$dest"
}
