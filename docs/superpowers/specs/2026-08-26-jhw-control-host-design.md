# jhw-control host launcher 설계 노트

- 날짜: 2026-08-26
- 상태: 사용자 승인, 독립 보안 리뷰 수정 반영
- 대상: claude-config #28

## 택한 접근

실행 가능한 Python supervisor `jhw-control-host`를 mode `0500` 전용 사본으로 atomic 설치한다. installer는 `$HOME`부터 entrypoint/target까지 기존 원본·해결 경로 체인을 먼저 검증하고 `.local/bin`, `.local/lib`, 전용 디렉터리를 `0700`으로 고정한다. 런처는 한 FD로 `0600`·현재 UID·regular file·hard-link 1개인 `control.env`를 리터럴 파싱한다. 격리된 Python helper는 `PYTHON_KEYRING_BACKEND`를 Secret Service로 강제하고 실제 class identity를 확인한 뒤 Project·Notion 토큰을 함께 읽는다. Repo 토큰은 고정 `gh auth status --hostname github.com --active --show-token --json hosts`의 단일 active entry에서 정확한 owner와 `tokenSource=keyring`을 함께 검증한다. missing/insecure 상태의 조치는 secure storage가 기본인 interactive `gh auth login`이며 결과를 다시 검증한다. provider에는 timeout·출력 상한·빈 stdin을 적용한다. 현재 UID만 신뢰하고 다른 group member, POSIX ACL 또는 world가 쓸 수 있는 executable·ancestor 및 고정 PATH의 모든 검색 디렉터리를 credential 조회 전에 거부한다. 허용 명령은 `unlock`, `preflight`, `portfolio status`, `task start`다. `unlock`은 UID에서 canonical runtime/bus를 검증·파생하고 기존 D-Bus owner와 GNOME private method의 exact signature를 고정한 뒤 login collection만 해제한다. daemon·collection을 생성하거나 교체하지 않고, 암호는 child의 TTY·메모리·로컬 D-Bus에만 존재하며 parent는 모든 종료 경로에서 terminal state를 복구하고 읽지 않은 입력 큐를 폐기한다. 하위 출력은 12 KiB 단일 JSON·stream·명령별 closed success/error schema와 code/exit 조합을 검증해 canonical 재직렬화한다. `task start`는 명시된 Task/Project/Repository 요청 좌표에 응답을 묶고 검증된 Task/Claim/branch/worktree 좌표와 optional Handoff만 반환한다. 경쟁 호스트의 정상 선점 충돌은 host를 bounded 입력으로 검사하되 공개 projection에서는 제거한다.

## 더 단순한 shell wrapper보다 나은 이유

shell의 `source`는 설정을 코드로 실행하고 로그·프로세스 유출 경계를 만들며 파일 검증과 읽기 사이 교체도 막기 어렵다. Python은 검증한 동일 FD만 읽고 provider·child 환경을 분리한다.

## 가장 그럴듯한 실패 모드

1. 잠긴 keyring 또는 평문 GitHub credential을 정상으로 오인한다. backend/source가 정확하지 않으면 provision/login 조치만 반환하고 자동 migration하지 않는다.
2. 하위 출력 계약이 변해 보호 데이터가 전달된다. strict schema, 중복-key 거부, downstream과 같은 Unicode UTF-16 길이·GitHub ID byte·slug 경계, 양 stream·decoded JSON과 padded/unpadded base64·대소문자를 정규화한 hex/percent escape·표준 URL quote canary 검사로 전체 출력을 폐기한다.
3. capture setup/read 예외 뒤 child가 계속 mutation한다. `Popen` 호출부터 결과 생성까지 하나의 예외 경계로 감싸 모든 예외에서 process group을 종료하고 wait한다.
4. 고정 PATH의 앞 디렉터리나 설치 경로 상위 항목이 바뀌어 다른 실행 파일을 선점한다. credential 조회 전 전체 검색/설치 경로를 검증하고 symlink-to-directory target 및 비-private ACL/write 권한을 거부한다.
5. headless prompt가 실패하거나 private GNOME 계약이 바뀐다. `unlock`은 interactive TTY가 없으면 시작하지 않고, exact interface/method/input signature가 다르면 암호 입력 전에 중단한다. public prompt나 daemon replacement로 fallback하지 않는다.
