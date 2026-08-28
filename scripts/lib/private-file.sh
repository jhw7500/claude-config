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

# assert_trusted_command_path <colon-separated path> — installer가 command
# lookup에 쓰기 전에 모든 고정 PATH 디렉터리와 조상 경로를 검사한다. 다른 local
# principal이 쓸 수 있거나 POSIX ACL이 있으면 command lookup 자체를 중단한다.
assert_trusted_command_path() {
  /usr/bin/python3 -I - "$1" <<'PY'
import errno
import grp
import os
import pwd
import stat
import sys
from pathlib import Path

uid = os.getuid()


def private_group(gid):
    try:
        group = grp.getgrgid(gid)
        members = {entry.pw_uid for entry in pwd.getpwall() if entry.pw_gid == gid}
        members.update(pwd.getpwnam(name).pw_uid for name in group.gr_mem)
    except (KeyError, OSError):
        return False
    return members <= {uid}


def extended_acl(path):
    unsupported = {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}
    if hasattr(errno, "ENOATTR"):
        unsupported.add(errno.ENOATTR)
    for name in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            os.getxattr(path, name)
        except OSError as error:
            if error.errno in unsupported:
                continue
            return True
        else:
            return True
    return False


def trusted_directory(path):
    try:
        metadata = path.stat()
    except OSError:
        return False
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, uid}:
        return False
    if metadata.st_mode & stat.S_IWOTH:
        return metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX) and not extended_acl(path)
    if metadata.st_mode & stat.S_IWGRP and not private_group(metadata.st_gid):
        return False
    return not extended_acl(path)


def trusted_path_entry(path):
    try:
        metadata = path.stat()
    except OSError:
        return False
    return not (metadata.st_mode & stat.S_IWOTH) and trusted_directory(path)


entries = sys.argv[1].split(":")
if not entries or any(not entry or not os.path.isabs(entry) for entry in entries):
    raise SystemExit(1)
for raw in entries:
    original = Path(raw)
    try:
        resolved = original.resolve(strict=True)
    except OSError:
        raise SystemExit(1)
    ancestors = {*original.parents, *resolved.parents}
    if not trusted_path_entry(original) or not trusted_path_entry(resolved):
        raise SystemExit(1)
    if any(not trusted_directory(path) for path in ancestors):
        raise SystemExit(1)
PY
}

# assert_private_path_chain <dir>... — launcher 설치 경로의 원래/해결된 전체
# 디렉터리 체인을 검사한다. root/current UID만 소유할 수 있고, 다른 principal의
# write 권한이나 확장 POSIX ACL이 있으면 실패한다. root 소유 sticky 디렉터리
# (/tmp 같은 안전한 상위 경계)는 허용하지만 설치 대상 디렉터리 자체는 별도로
# private_dir에서 0700으로 고정한다.
assert_private_path_chain() {
  /usr/bin/python3 -I - "$@" <<'PY'
import errno
import grp
import os
import pwd
import stat
import sys
from pathlib import Path

uid = os.getuid()


def private_group(gid):
    try:
        group = grp.getgrgid(gid)
        members = {entry.pw_uid for entry in pwd.getpwall() if entry.pw_gid == gid}
        members.update(pwd.getpwnam(name).pw_uid for name in group.gr_mem)
    except (KeyError, OSError):
        return False
    return members <= {uid}


def extended_acl(path):
    unsupported = {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}
    if hasattr(errno, "ENOATTR"):
        unsupported.add(errno.ENOATTR)
    for name in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            os.getxattr(path, name)
        except OSError as error:
            if error.errno in unsupported:
                continue
            return True
        else:
            return True
    return False


def trusted_directory(path):
    try:
        metadata = path.stat()
    except OSError:
        return False
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, uid}:
        return False
    if metadata.st_mode & stat.S_IWOTH:
        return metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX) and not extended_acl(path)
    if metadata.st_mode & stat.S_IWGRP and not private_group(metadata.st_gid):
        return False
    return not extended_acl(path)


for raw in sys.argv[1:]:
    original = Path(raw).absolute()
    try:
        resolved = original.resolve(strict=True)
    except OSError:
        raise SystemExit(1)
    chain = {original, *original.parents, resolved, *resolved.parents}
    if any(not trusted_directory(path) for path in chain):
        raise SystemExit(1)
PY
}

# install_doc <src> <dest>  — 문서를 명시적 mode 로 설치한다(실행 비트 제거).
install_doc() {
  local src=$1 dest=$2
  cp -f "$src" "$dest" || return 1
  chmod "$DOC_FILE_MODE" "$dest"
}
