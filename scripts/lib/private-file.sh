#!/usr/bin/env bash
# 사설 설정 파일·디렉터리의 mode 계약.
#
# cp / mkdir 는 호출 환경의 umask 와 기존 파일 mode 에 결과가 좌우된다. 그러면
# 같은 install.sh 를 돌려도 호스트마다 다른 권한이 나온다. 여기서 명시적으로
# 고정해 설치 계약이 환경에 의존하지 않게 한다.

PRIVATE_DIR_MODE=700    # ~/.claude 등 — 다른 로컬 사용자 traverse 차단
PRIVATE_FILE_MODE=600   # settings.json, CLAUDE.md, 그 백업
DOC_FILE_MODE=644       # 복사되는 guidance 문서 — 비밀은 아니나 실행 비트는 없어야 한다

# private_dir <path>  — 없으면 만들고, 있으면 mode 를 강제한다.
private_dir() {
  local d=$1
  mkdir -p "$d" || return 1
  chmod "$PRIVATE_DIR_MODE" "$d"
}

# install_doc <src> <dest>  — 문서를 명시적 mode 로 설치한다(실행 비트 제거).
install_doc() {
  local src=$1 dest=$2
  cp -f "$src" "$dest" || return 1
  chmod "$DOC_FILE_MODE" "$dest"
}
