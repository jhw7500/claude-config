# MCP credential 노출 대응 절차

이 문서는 MCP 설정·진단 출력·프로세스 인자·세션 기록에 credential literal이 노출됐거나
노출 가능성이 확인됐을 때의 저장소 밖 운영 절차다. 실제 값, 대상 시스템 식별 정보,
세션 경로는 이 문서나 GitHub 이슈·PR에 기록하지 않는다.

## 즉시 대응

1. 관련 터미널·세션·CI 로그의 공유와 추가 복제를 중단한다.
2. 로컬의 제한된 증거만 이용해 공급자와 credential 종류를 식별한다. 값을 채팅·이슈·파일에 복사하지 않는다.
3. 공급자 또는 대상 호스트에서 기존 credential을 revoke/rotate한다.
4. 새 값은 승인된 credential store에 갱신하고 저장소·manifest·Claude 설정에는 넣지 않는다.
5. credential store가 환경을 주입한 새 host-control/Claude 세션에서 `scripts/setup-mcp.sh --check`를 실행한다.
6. preview의 이름·상태·변경 필드만 검토한 뒤 `--apply`를 실행하고 다시 `--check`한다.
7. 이전 credential이 실제로 거부되고 새 credential이 정상 동작하는지 공급자 측에서 확인한다.

회전 작업은 공급자와 대상 호스트 권한이 필요하므로 이 저장소의 setup 스크립트가 자동 수행하지 않는다.

## 기록 처리

조사 대상에는 Claude/Codex 세션 transcript, 도구 호출 출력, 터미널 캡처와 scrollback,
shell history, CI artifact, 디버그 로그, 이슈·PR 댓글을 포함한다.

- 보존 정책과 감사 요구를 먼저 확인하고, 삭제·수정 권한이 있는 담당자와 범위를 합의한다.
- 삭제 가능한 로컬 기록은 승인된 정리 도구로 제거하고 동기화·백업 사본도 같은 범위로 처리한다.
- 공유된 CI artifact나 원격 세션 기록은 해당 서비스의 보존·삭제 절차를 사용한다.
- Git 이력이나 공동 기록은 일방적으로 rewrite하지 않는다. 이미 push된 기록은 저장소 관리자와 별도 조정한다.
- 정리 완료 증거에는 credential 값 대신 회전 시각, 담당 범위, 폐기 확인 상태만 남긴다.

## 재발 방지 확인

- `manifest/mcp.json`의 env 값은 기본값 없는 `${VAR}` 형식이며 `${VAR:-default}`는 사용하지 않는다.
- command에는 placeholder를 사용하지 않는다. args에는 승인된 정확한 경로 표현
  `${FILESYSTEM_MCP_ROOT}`, `${JHW_NOTION_DIST}/index.js`만 사용하고 credential은 env로만 전달한다.
- credential형 값, URL userinfo, credential query/DSN label, capability URL은 raw,
  percent-encode, JSON string escape 형태로도 command/args에 존재하지 않는다.
- Oracle TNS형 `name/value@alias`는 package ref와 모호하므로 argv에서 fail-closed로 차단한다.
  target 없는 `name/value`는 정확한 `sqlplus`/`sql`/`sqlcl` 실행 문맥과
  `--connect`/`--logon` 값 문맥에서 차단하고, 그 밖의 일반 `owner/repository` 경로는 허용한다.
- 명령별 단축 credential 옵션은 해당 실행 파일에서만 차단한다. `sh` 계열의 `-c`, `env`,
  `sudo`, `timeout`, `nohup`, `nice`, `stdbuf`로 감싼 하위 명령과 JSON command/args 표현에도
  같은 검사가 적용되는지 확인한다.
  stdin·prompt·파일 경로로 값을 받는 안전한 형식은 literal argv와 구분한다.
- preflight는 임의의 모든 외부 CLI 문법을 자동으로 아는 범용 secret detector가 아니다.
  manifest의 command나 args 형식을 새로 추가·변경할 때는 해당 CLI의 공식 credential 문법을
  검토하고, literal 차단 사례와 정상 대조군을 같은 변경의 보안 회귀 테스트에 추가한다.
- `scripts/setup-mcp.sh --check` 출력에는 서버 이름, scope, 변경 필드만 나타난다.
- project shadow가 있으면 자동 삭제하지 않고 `SHADOWED`를 수동 해소한 뒤 재시도한다.
- legacy local shadow는 내용을 검토한 뒤에만 `--apply --migrate-local`로 user scope에 이동한다.
- apply 후 다시 `--check`하여 `IN_SYNC`인지 확인한다.

`setup-mcp.sh`는 저장소의 `secrets.local.env`를 읽지 않는다. MCP credential은 승인된
credential store에서 Claude를 시작하는 host-control 환경에 주입하고, 저장소와 manifest에는
literal을 남기지 않는다. `secrets.local.env`는 Slack 브릿지 setup 전용이다.

apply는 Claude CLI의 remove/add를 호출하지 않고, 안전한 설정 directory를 dirfd로 고정한 뒤
private user 설정을 lock·전체 바이트 비교한다. mode `0600` 임시 파일을 같은 dirfd에서 한 번의
atomic replace로 커밋하며, directory inode·user 설정 바이트·project `.mcp.json` snapshot이
변경되면 적용 없이 중단하므로 다른 설정 작업이 끝난 뒤 preview부터 재시도한다. lock을 따르지 않는
외부 프로세스는 마지막 비교와 replace 사이에 경쟁할 수 있으므로 user 설정이나 project `.mcp.json`을
동시에 변경하면서 apply하지 않는다. 전원 종료나 파일 시스템 오류 뒤에는 `--check`로 실제 상태를
확인한 뒤 복구한다. 특히
`APPLIED_UNCONFIRMED`는 replace 이후 durability 확인만 실패한 상태이므로 미적용으로 단정하지 않는다.
