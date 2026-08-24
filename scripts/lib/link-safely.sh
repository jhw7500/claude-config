#!/usr/bin/env bash
# 심링크 배포 공통 헬퍼.
#
# ln -sfn 은 목적지가 실디렉터리일 때 실패하지 않고 그 "안에" 링크를 만든다.
# 그러면 배포는 조용히 실패하고, settings.json 이 가리키는 경로는 여전히
# 디렉터리라 훅이 실행되지 않는다. 훅은 안 도는 것과 정상인 것이 겉으로 같아서
# 이 실패는 오래 숨는다 — scripts/hook-selfcheck.py 가 필요했던 이유와 같다.
#
# link_safely <src> <dest>
#   목적지가 심링크가 아닌 실체(파일·디렉터리)면 타임스탬프를 붙여 옆으로 치운 뒤
#   링크한다. 치우지 못하면 링크하지 않고 1을 반환한다 — 조용히 덮지 않는다.

link_safely() {
  local src=$1 dest=$2 backup
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    backup="$dest.replaced.$(date +%Y%m%d%H%M%S)"
    if ! mv "$dest" "$backup" 2>/dev/null; then
      echo "[link_safely] 경고: $dest 를 치우지 못해 링크를 건너뛴다" >&2
      return 1
    fi
    echo "[link_safely] 기존 실체 백업 -> $backup"
  fi
  ln -sfn "$src" "$dest"
}
