# jhw-control-host Secret Service 운영 절차

이 문서는 headless build account와 tmux에서 `jhw-control-host`의 credential store를 복구하고
검증하는 절차다. credential 값을 조회·출력·복사하는 절차는 포함하지 않는다.

## 결정과 범위

- Project와 Notion credential은 Python keyring의 exact Secret Service backend만 사용한다.
- repository credential은 GitHub CLI가 secure credential store에 저장한 keyring entry만 사용한다.
- plaintext, generic encrypted-file 또는 암시적 fallback backend는 지원하지 않는다.
- 재부팅 뒤 운영자가 interactive terminal에서 한 번 unlock하는 운영 모델을 채택한다.
- launcher는 daemon을 시작·재시작·종료하거나 collection을 생성하지 않는다.

launcher가 신뢰하는 대상은 process 목록이 아니라 현재 UID의 canonical user bus
`/run/user/<uid>/bus`에서 `org.freedesktop.secrets`를 소유한 unique owner다. 같은 invocation의
provider 실행 전후 owner가 달라지면 credential을 downstream control child에 전달하지 않는다.

## 부팅과 user manager

전용 build account가 로그아웃 뒤에도 계속 동작해야 하는지 먼저 운영 정책으로 결정한다. 현재
상태는 다음처럼 확인한다.

```bash
loginctl show-user "$(id -un)" --property=Linger
systemctl --user --no-pager status gnome-keyring-daemon.service
busctl --user --no-pager status org.freedesktop.secrets
```

배포판에 따라 GNOME keyring unit 이름이나 activation 방식은 다를 수 있다. canonical user bus의
well-known owner가 최종 기준이다. 위 진단 결과의 PID, process path 또는 owner name을 일반 Issue나
공유 로그에 복사하지 않는다.

로그아웃 뒤 user manager 유지가 명시적으로 필요한 전용 계정에서만 운영자가 다음 host mutation을
한 번 수행한다.

```bash
loginctl enable-linger "$(id -un)"
```

installer와 launcher는 lingering 또는 systemd user unit을 변경하지 않는다.

## 재부팅 뒤 unlock

canonical owner가 준비된 뒤 interactive terminal에서 실행한다.

```bash
"$HOME/.local/bin/jhw-control-host" unlock
```

이미 unlocked이면 password를 묻지 않는다. locked이면 login keyring password를 TTY에서 한 번 읽고
GNOME private interface의 검증된 method로 canonical login collection만 해제한다. password를 argv,
환경, 파일 또는 출력으로 전달하지 않는다.

## clean environment 검증

unlock 뒤 ambient D-Bus 변수를 전달하지 않은 상태로 contract와 visible preflight를 검증한다.

```bash
env -i HOME="$HOME" LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin \
  "$HOME/.local/bin/jhw-control-host" --contract
env -i HOME="$HOME" LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin \
  "$HOME/.local/bin/jhw-control-host" preflight
```

contract version은 `4`, credential policy는 `secure-store-only`여야 한다. preflight가 성공하기 전에는
Project Control mutation을 실행하지 않는다.

## tmux 검증

새 tmux session을 열고 그 안에서 clean preflight 명령을 그대로 실행한다.

```bash
tmux new-session -s jhw-control-preflight
env -i HOME="$HOME" LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin \
  "$HOME/.local/bin/jhw-control-host" preflight
```

성공 여부가 pane의 `XDG_RUNTIME_DIR` 또는 `DBUS_SESSION_BUS_ADDRESS`에 의존하면 안 된다. launcher는
두 값을 현재 UID의 canonical runtime에서 다시 파생한다. 검증이 끝난 tmux session의 종료는
운영자가 해당 session 안에서 수행한다.

## 오류별 조치

| Error code | 의미 | 조치 |
| --- | --- | --- |
| `OS_CREDENTIAL_STORE_LOCKED` | canonical login collection이 locked | interactive terminal에서 launcher `unlock`을 한 번 실행 |
| `OS_CREDENTIAL_STORE_UNAVAILABLE` | canonical bus/owner/alias 또는 endpoint 검증 실패 | user manager와 하나의 canonical owner를 복구한 뒤 visible preflight |
| `OS_CREDENTIAL_STORE_UNLOCK_UNSUPPORTED` | GNOME private unlock interface/signature 불일치 | provider/runtime 버전을 검토하고 지원 계약이 승인될 때까지 중단 |
| `OS_CREDENTIAL_STORE_CHANGED` | credential provider 실행 도중 unique owner가 교체됨 | mutation을 재실행하지 말고 user session을 안정화한 뒤 clean/tmux preflight를 모두 반복 |
| `KEYRING_RUNTIME_UNAVAILABLE` | system Python의 필수 credential runtime 누락 | system package를 복구한 뒤 unlock과 두 preflight를 반복 |

모든 오류에서 helper stdout/stderr, credential, owner, PID와 private path를 공유 기록에 남기지 않는다.

## 금지된 복구

- tmux pane마다 `dbus-run-session`을 시작하지 않는다.
- 추가 `gnome-keyring-daemon` process를 직접 spawn하지 않는다.
- process 수만 보고 PID를 kill하지 않는다.
- credential을 plaintext 또는 임의 encrypted file로 export하지 않는다.
- GitHub CLI의 plaintext `hosts.yml` token fallback을 허용하지 않는다.
- Project Control Registry나 Claim state를 credential-store 복구 수단으로 수정하지 않는다.

canonical bus에는 well-known name의 active owner가 하나뿐이다. 다른 bus의 daemon이나 owner가 아닌 stale
process는 launcher authority가 아니다. owner를 복구하고 interactive unlock과 visible preflight를 다시
수행하는 것이 유일한 launcher recovery다.

## Migration과 rollback

이번 변경에는 credential migration이 없다. rollout은 launcher 코드의 atomic 재설치만 수행하고 기존
Secret Service 및 GitHub CLI keyring entry를 그대로 사용한다.

rollback도 이전 검증된 launcher revision을 atomic 재설치할 뿐이다. rollback 전후에 credential을
export/import하거나 backup file을 만들지 않는다. 재설치 뒤 같은 unlock, clean preflight와 tmux
preflight 순서를 수행한다.

## Rollout 순서

1. producer 변경을 merge한다.
2. stable checkout에서 `install.sh`를 다시 실행한다.
3. canonical user manager와 Secret Service owner 상태를 확인한다.
4. 필요하면 interactive unlock을 한 번 수행한다.
5. clean environment에서 contract와 preflight를 검증한다.
6. tmux에서 같은 clean preflight를 검증한다.
7. 두 경로가 성공한 뒤에만 consumer Task workflow를 새 launcher에 맞춘다.
8. 승인된 실제 Task mutation으로 최종 통합을 검증한다.

실패하면 consumer rollout을 진행하지 않고 launcher 또는 user-session 상태를 복구한 뒤 3단계부터 다시
검증한다. 자동 file-backend fallback은 rollback이 아니다.

## Zero-touch와 향후 backend

재부팅 뒤 사람 개입 없는 zero-touch가 필수가 되면 현재 local Secret Service 모델을 확장하지 않는다.
별도 Issue에서 외부 secret manager 또는 hardware-backed agent를 설계한다. 새 provider는 최소한 다음을
승인받아야 한다.

- bootstrap secret의 저장 위치와 접근 주체
- least privilege, rotation과 revoke
- noninteractive outage 및 rate-limit 동작
- credential을 출력하지 않는 migration과 rollback
- provider dependency pinning과 clean/tmux 검증
- 기존 secure-store-only contract의 versioning과 consumer rollout

관련 upstream 근거는 [Python keyring headless Linux 안내](https://keyring.readthedocs.io/en/latest/),
[Secret Service unlock](https://specifications.freedesktop.org/secret-service/latest/unlocking.html),
[Secret Service prompt](https://specifications.freedesktop.org/secret-service/latest/prompts.html),
[systemd loginctl](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)을 참고한다.
