# jhw-control-host Secret Service 안정화 설계

- 날짜: 2026-08-29
- 상태: 사용자 승인
- 대상: claude-config #68
- 선행 결정: 재부팅 뒤 운영자의 1회 interactive unlock을 허용한다.

## 결정

`jhw-control-host`의 credential 정책은 `secure-store-only`로 유지한다. Project와 Notion
credential은 Python keyring의 exact Secret Service backend에서, repository credential은
GitHub CLI keyring에서만 읽는다. plaintext 또는 generic encrypted-file backend를 추가하지 않고,
기존 credential을 export/import하는 migration도 수행하지 않는다.

안정화의 기준점은 프로세스 목록이 아니라 현재 UID의 canonical systemd user bus
`/run/user/<uid>/bus`다. launcher는 ambient `XDG_RUNTIME_DIR`와
`DBUS_SESSION_BUS_ADDRESS`를 계속 무시한다. `org.freedesktop.secrets`의 well-known name을
가진 unique owner가 provider 실행 전후에 동일하고 login collection이 unlocked일 때만
credential-bearing `jhw-control` child를 실행한다.

## 위협과 실패 모델

1. tmux 또는 clean shell이 별도의 D-Bus 주소를 상속해 다른 Secret Service instance를 본다.
2. canonical bus에 Secret Service owner가 없고 provider의 D-Bus activation이 새 daemon을
   암묵적으로 시작한다.
3. credential을 읽는 도중 owner가 종료·교체되어 서로 다른 store의 값을 조합한다.
4. login collection이 잠겼는데 headless provider가 GUI prompt나 plaintext fallback으로 진행한다.
5. `/run/user/<uid>` 또는 `bus`가 다른 owner, 넓은 mode, ACL, symlink, non-socket으로 교체된다.
6. GNOME private unlock interface가 변경됐는데 암호를 먼저 읽거나 public prompt로 fallback한다.
7. 복구 과정에서 credential을 argv, 환경, 파일, 로그 또는 migration artifact에 남긴다.

현재 UID 자체는 credential store 접근 권한을 가진 신뢰 주체다. 같은 UID가 악의적으로 store나
process를 조작하는 공격은 이 launcher가 격리할 수 없다. launcher는 다른 local principal,
ambient process environment, 잘못된 D-Bus session, accidental plaintext fallback을 경계로 삼는다.

## 런타임 흐름

`unlock`을 제외한 허용 명령은 다음 순서로 동작한다.

1. invocation, private config, fixed executable trust chain과 canonical session-bus endpoint를
   기존 규칙대로 검증한다.
2. isolated Python helper가 canonical bus에 직접 연결하고 `NO_AUTO_START`로
   `org.freedesktop.secrets` owner를 조회한다.
3. helper가 GNOME private unlock contract, default alias의 canonical login collection과
   `Locked=false`를 credential을 읽지 않고 확인한다. owner와 `unlocked` 상태만 bounded JSON으로
   parent에 반환하고 외부 출력에는 포함하지 않는다.
4. exact Secret Service keyring backend와 GitHub CLI keyring에서 기존 방식으로 credential을
   읽는다. plaintext fallback은 계속 거부한다.
5. 동일한 non-starting probe를 한 번 더 실행한다. unique owner가 첫 probe와 다르거나 collection이
   다시 잠겼으면 `jhw-control`을 호출하지 않고 path-free stable error로 실패한다.
6. 두 probe가 동일할 때만 분리된 credential child environment를 만들고 hidden preflight 또는
   요청한 read-only/mutation command를 실행한다.

probe 자체는 daemon이나 collection을 만들거나 교체하지 않는다. provider가 첫 probe 직후의
owner 종료와 경쟁해 D-Bus activation을 일으킬 가능성까지 제거할 수는 없으므로, 두 번째 probe의
owner mismatch가 그 세대를 거부한다. 이 경우 credential은 downstream child에 전달되지 않는다.

## 상태와 오류 계약

- owner 부재, canonical bus 연결 실패, alias 불일치는
  `OS_CREDENTIAL_STORE_UNAVAILABLE`로 실패한다.
- collection 잠금은 `OS_CREDENTIAL_STORE_LOCKED`와
  `jhw-control-host unlock` 조치로 실패한다.
- GNOME private interface/signature 불일치는
  `OS_CREDENTIAL_STORE_UNLOCK_UNSUPPORTED`로 실패한다.
- provider 실행 전후 owner가 다르면 새 stable code
  `OS_CREDENTIAL_STORE_CHANGED`로 실패한다. 재시도 전에 user-session의 단일 owner를
  복구하고 visible preflight를 다시 수행하라는 path-free action만 제공한다.
- helper의 stdout/stderr, owner name, PID와 process path는 public result에 전달하지 않는다.
- `--contract` version과 13개 command family는 바꾸지 않는다.

## 중복 daemon 판정

프로세스 수만으로 중복 store를 판정하지 않는다. 로그인 세션, stale process 또는 서로 다른 bus의
daemon이 함께 보일 수 있기 때문이다. launcher가 신뢰하는 활성 store는 canonical bus에서
well-known name을 가진 정확히 한 unique owner다. 다음만 실패 조건이다.

- canonical bus에 owner가 없음
- owner가 지원하는 interface/collection 계약과 불일치
- 한 invocation의 provider 구간에서 owner가 교체됨

launcher는 process를 kill하거나 daemon을 start/restart하지 않는다. 운영자는 systemd user manager와
Secret Service unit의 상태를 별도로 복구한 뒤 interactive terminal에서 한 번 unlock한다.

## 운영과 복구

전용 build account가 로그아웃 뒤에도 동작해야 하면 운영자가 명시적으로 user lingering을 켠다.
launcher와 installer는 `loginctl` 또는 `systemctl --user`를 변경하지 않는다. 재부팅 뒤 순서는
다음으로 고정한다.

1. canonical systemd user manager와 Secret Service owner가 준비됐는지 확인한다.
2. interactive terminal에서 `jhw-control-host unlock`을 한 번 실행한다.
3. ambient D-Bus 변수가 없는 clean environment에서 `--contract`와 `preflight`를 실행한다.
4. 기존 tmux pane 또는 새 noninteractive tmux command에서 `preflight`를 다시 실행한다.
5. 두 preflight가 성공한 뒤에만 producer/consumer Task mutation을 허용한다.

실패 시 per-tmux `dbus-run-session`, 추가 `gnome-keyring-daemon`, plaintext file 또는 자동 process
종료로 우회하지 않는다. user manager/owner를 복구하고 같은 순서를 처음부터 수행한다.

## Migration과 rollback

이번 rollout의 migration은 backend migration이 아니라 launcher 재설치뿐이다. 기존 Secret Service와
GitHub CLI keyring entry를 그대로 둔다. installer와 launcher는 credential 값을 읽어 다른 저장소에
쓰지 않는다.

rollback은 이전 검증된 launcher revision을 atomic 재설치한 뒤 동일한 unlock/preflight 절차를
수행한다. credential export, plaintext backup 또는 file backend 변환은 rollback 절차가 아니다.
향후 1Password, Bitwarden, pass 또는 encrypted-file backend를 도입하려면 bootstrap secret,
key rotation, revoke, migration/rollback과 provider dependency를 별도 Issue와 설계로 승인해야 한다.

## 검증 기준

- probe는 `NO_AUTO_START` owner 조회만 사용하고 prompt·daemon mutation을 하지 않는다.
- locked/missing/unsupported/owner-changed 상태가 서로 다른 stable error로 fail-closed한다.
- owner가 바뀌면 keyring/gh에서 값을 읽었더라도 control child는 호출되지 않는다.
- alternate ambient bus, clean environment와 tmux-equivalent environment가 모두 canonical bus를 쓴다.
- runtime directory의 owner/mode/ACL/symlink와 bus의 owner/ACL/symlink/socket 검증을 회귀 테스트로 고정한다.
- helper의 임의 출력과 credential canary가 public stdout/stderr에 나타나지 않는다.
- 전체 launcher/installer test suite와 compile 검증이 통과한다.
