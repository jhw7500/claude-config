# Backlog Drainer Implementation Plan

> For agentic workers: use superpowers:subagent-driven-development, one implementation slice at a time, with native role routing and an independent task review. Track steps with checkboxes.

**Goal:** 승인된 현재 Task 한 건을 native 단계 위임과 durable checkpoint로 진행하고 검증된 커밋에서 정지한다.
**Architecture:** 공통 Claude/Codex SKILL과 Python 표준 라이브러리 helper. codec/storage, admission, Git evidence, read-only queue, CLI를 분리한다. Registry·Task lifecycle·모델 호출은 helper가 실행하지 않는다.
**Tech Stack:** Python 3.10+, Git, gh, POSIX filesystem/flock, pytest. 새 dependency 없음.
**Spec:** ../specs/2026-11-04-backlog-drainer-design.md (사용자 승인, 독립 critic APPROVED).

## Global Constraints

- Task tsk-01a1570c-89ce-7cf6-8cd8-8b1902dad18f, Claim clm-01a1a6a9-410d-7963-b922-d5f46ea6766a. 좌표 자체는 실제 권한을 대신하지 않는다.
- 작업 root: /home/jhw/ai/opencode/worktrees/jhw-control/wt-8b1902dad18f-jhw7500-claude-config-31. baseline c5fb7ed961101b46281a8171afb883012cefe2b2. 다른 checkout 변경 금지.
- 모든 shell 명령은 rtk. 실제 HOME 설치·기존 MCP/lock 종료·Task lifecycle·다음 Issue 시작은 범위 밖이다.
- snapshot <=12KiB/15줄, manifest <=1MiB, raw log <=16MiB. 초과 시 truncate하지 않는다.
- supervisor만 checkpoint/admission을 쓴다. native actual permission·기존 launcher 계약과 진단용 로컬 metadata를 구분한다.
- 검증된 커밋 뒤 외부 리뷰 게이트·게시·병합·Task finish를 자동 진행하지 않는다. 기존 precompact/resume/launcher 의미는 바꾸지 않는다.
- 사용자 구현 승인은 이미 있으므로 실행 방법 선택을 다시 묻지 않는다. 단계별 executor 한 명과 독립 task reviewer로 직렬 진행한다. parent가 commit과 ledger를 소유한다.
- 각 단계는 표적 RED/GREEN, 영향받는 통합 검사만 수행한다. 전체 suite는 통합 후 한 번 실행한다. 검증 Python은 /home/jhw/ai/opencode/projects/claude-config/.superpowers/venv-issue264/bin/python 이다.

## Review Focus

1. symlink/다른 UID/path 치환, invalid UTF-8, duplicate JSON key, bool-as-int는 거부한다 (Task 1/2).
2. 승인된 successor 조정의 모든 crash boundary에서 old 또는 successor admission이 남는다 (Task 2).
3. mode/rename/untracked/commit hook 변경에 과거 검증을 잘못 재사용하지 않는다 (Task 3).
4. 완전 pagination·closed/external blocker·cycle을 구분하고 partial read는 queue를 발표하지 않는다 (Task 4).
5. native interrupt, 도구 종료, quiescence, fresh child context를 구분한다. 미지원 runtime은 unsupported다 (Task 5/6).

### Task 1: canonical snapshot와 immutable evidence

**Files:** Create scripts/backlog_checkpoint/__init__.py, codec.py, storage.py; tests/test_backlog_checkpoint.py.
**Interfaces:** codec.CheckpointError(ValueError), CheckpointIndeterminateError(CheckpointError); canonical_bytes(value)->bytes; sha256_bytes(data)->str; supervisor_fingerprint(origin_adapter,session_id,host)->str; validate_snapshot(value)->dict; encode_snapshot(value)->bytes; decode_snapshot(data)->dict. storage.read_snapshot(path)->dict; write_snapshot(path,snapshot,expected_generation)->dict; write_evidence(state_root,run_id,value)->dict(ref,sha256,size); read_evidence(state_root,run_id,reference)->dict.
**Contracts:** spec schema/types/enums/caps, nesting64, Unicode Cc. Pre-publish failures preserve old bytes; post-publish fsync failure is explicit indeterminate/PAUSED, no automatic rollback. Missing required nofollow/dirfd support fails before I/O. Markdown 4줄 prefix/fence와 단일 JSON record. 최초 expected_generation=None/gen0, 이후 lock 안에서 exact expected generation만 gen+1. immutable content-addressed evidence, current UID/0600/descriptor-relative nofollow. 상세 raw log reference는 bounded하게 확인한다.

- [x] Step 1: malformed/duplicate JSON, bool generation, unknown enum/key, large manifest, traversal/symlink, stale CAS, partial-write old snapshot 보존의 동작 테스트를 먼저 작성한다. literal 기대값을 사용한다.
- [x] Step 2: pytest -q tests/test_backlog_checkpoint.py 로 feature 부재 RED를 확인한다.
- [x] Step 3: 위 인터페이스를 최소 구현한다. 쓰기는 owned private 경로만 사용한다.
- [x] Step 4: 표적 GREEN, ruff F/E9, 공백 검사와 RED/GREEN 증거를 보고한다.
- [x] Step 5: 독립 task review 후 parent가 Change Evidence v1 한국어 commit과 ledger를 기록한다.

### Task 2: exclusive admission과 atomic successor 조정

**Files:** Create scripts/backlog_checkpoint/admission.py; tests/test_backlog_admission.py.
**Interfaces:** consume Task 1 codec/private filesystem primitives. acquire_admission(state_root,worktree_root,record)->dict; release_admission(state_root,worktree_root,expected_record)->None; reconcile_admission(state_root,worktree_root,expected_record,successor_record,proof)->dict.
**Contracts:** record는 run/root-key/checkpoint/owner observations만 담는다. proof는 승인된 외부 lifecycle 결과의 관측 입력이고 권한을 부여하지 않는다. 같은 root lock에서 old active 유지, exact archive copy/fsync, successor snapshot/temp fsync, CAS + active atomic replace. 처음만 O_EXCL 생성한다.

- [x] Step 1: duplicate run/alias, wrong expected bytes, missing proof, foreign owner와 archive/publish 전후 실제 fixture subprocess 중단 회귀를 작성한다.
- [x] Step 2: pytest -q tests/test_backlog_admission.py RED.
- [x] Step 3: 최소 구현한다. production test-only crash hook 없이 테스트 harness에서 실제 syscall 경계를 제어한다.
- [x] Step 4: Tasks 1/2 표적 GREEN과 정적 검사.
- [x] Step 5: 독립 task review 후 parent commit/ledger.

### Task 3: Git 격리·source manifest·post-commit binding

**Files:** Create scripts/backlog_checkpoint/git_evidence.py; tests/test_backlog_git_evidence.py.
**Interfaces:** consume codec. inspect_worktree(root,expected_branch,expected_worktree_ref)->dict; collect_sources(root,allowed_paths)->dict(paths,count,path_set_digest,change_digest,sources,parent_head); bind_commit(root,parent_head,manifest,commit_oid)->dict.
**Contracts:** porcelain -z를 bytes로 파싱, exact root/realpath/branch와 canonical worktree record 하나 확인, alias/child cwd 이탈 거부. source explicit allowlist, mode/내용/삭제/untracked 포함, rename delete+add. helper는 git add/commit/reset/rebase를 실행하지 않는다.

- [x] Step 1: 실제 tmp Git repos에서 modify/add/delete/rename/mode, unknown path, alias, exact single-parent commit, hook 변경, 추가 commit/multiple candidate 회귀를 작성한다.
- [x] Step 2: pytest -q tests/test_backlog_git_evidence.py RED.
- [x] Step 3: read-only Git 인터페이스와 exact manifest commit binding을 구현한다. 임의 dirty 파일을 숨기지 않는다.
- [x] Step 4: 표적 GREEN/정적 검사.
- [x] Step 5: 독립 task review 후 parent commit/ledger.

### Task 4: 완전 조회 기반 읽기 전용 dependency queue

**Files:** Create scripts/backlog_checkpoint/queue.py; tests/test_backlog_queue.py.
**Interfaces:** rank_issues(issues,dependencies)->dict(status,queue,blocked,reasons); collect_queue(repo,explicit_edges=(),runner=subprocess.run)->dict.
**Contracts:** exact Issue URL/state/B->I edge. 공식 blocked_by + Link pagination, gh list argv/shell=False. 열린 Issue1000/관계10000 상한. incomplete/error는 queue 없이 INCOMPLETE. closed satisfied, external-open downstream blocked, Kahn+number tie, SCC cycle/downstream 구분.

- [x] Step 1: spec 고정 fixture [1,2,3,4,8], cycle {5,6}, downstream {7}; multi-page/closed/external/failure/cap을 literal 응답과 실제 fake gh executable로 검증한다.
- [x] Step 2: pytest -q tests/test_backlog_queue.py RED.
- [x] Step 3: bounded read adapter와 deterministic rank를 구현한다. 제목 유사도는 dependency가 아니다.
- [x] Step 4: 표적 GREEN/정적 검사.
- [x] Step 5: 독립 task review 후 parent commit/ledger.

### Task 5: 명시 CLI·공통 SKILL·임시 HOME 설치

**Files:** Create scripts/backlog-checkpoint.py, scripts/backlog_checkpoint/cli.py, scripts/install-backlog-drain.py, skills/backlog-drain/SKILL.md, skills/backlog-drain/references/protocol.md; modify install.sh, README.md, scripts/backlog_checkpoint/git_evidence.py, tests/test_backlog_git_evidence.py; tests/test_backlog_cli.py, tests/test_backlog_install.py. Git 수정은 exact untracked checkpoint 한 파일의 명시 제외·검증을 연결하는 통합 범위에 한정한다.
**Interfaces:** snapshot read/write, evidence write/read, admission acquire/release/reconcile, git inspect/collect/bind, queue plan. JSON 파일/stdin과 명시 argv만 받으며 next_action shell은 실행하지 않는다. symlink entry는 __file__.resolve() 옆 package를 사용한다.
**Contracts:** common SKILL이 실제 native capability, 현재 Task 계약, exact cwd, serial executor+independent reviewer, phase 시작/저장/검증/정지를 담당한다. 정상 fix는 follow-up, fault recovery만 새 child ID/context다. 설치는 양쪽 SKILL/helper symlink, foreign target 보존, 실제 HOME 미실행.

- [x] Step 1: 실제 CLI stdin/JSON/output/exit와 임시 HOME discovery/helper호출/재설치/foreign 보존 회귀를 먼저 작성한다.
- [x] Step 2: 표적 RED.
- [x] Step 3: CLI·SKILL·installer·README를 구현한다. 문서 text grep 테스트를 쓰지 않는다.
- [x] Step 4: 새 표적+기존 installer GREEN, ruff F/E9, shell syntax/ShellCheck, 공백 검사.
- [x] Step 5: 독립 task review 후 parent commit/ledger.

### Task 6: native 중단 증거와 최종 검증

**Files:** docs/validation/2026-11-04-backlog-drainer.md; integration repair in scripts/backlog_checkpoint/storage.py, admission.py, tests/test_backlog_checkpoint.py, tests/test_backlog_git_evidence.py and directly related regression tests as required. raw native receipts는 이 계획의 ignored workspace에 보존한다.
**Contracts:** 현재 Claim의 실제 root 안의 격리 fixture를 사용한다. native proof 때문에 새 Task/Claim을 만들지 않는다. synthetic status와 실제 lifecycle receipt를 구분한다. 실제 scope/Claim 확인은 parent가 기존 launcher 계약으로 수행한다.

- [x] Step 0: 실제 snapshot write 뒤 private sidecar lock이 unknown Git source로 나타나는 통합 단절을 RED로 재현하고, snapshot parent-directory FD flock을 공유하여 추가 소스 산출물 없이 CAS·admission 조정을 유지한다. 표적 검사·독립 통합 리뷰·parent commit 후 native 실험을 시작한다.

- [ ] Step 1: Codex pre/post-edit barrier의 running child를 실제 interrupt, interrupted/quiescent/도구종료 각각 확인. replacement는 새 ID/fork_turns=none, old transcript 없이 snapshot/Git/다음 단계만 받는다.
- [ ] Step 2: 실제 설치 Claude stop/terminal/non-fork capability를 확인한다. 지원 시 같은 실험, 아니면 구체 근거와 unsupported를 기록한다. SDK 이벤트가 TUI에도 있다고 추정하지 않는다.
- [ ] Step 3: 새 표적 후 전체 pytest -q -n 8, ruff, ShellCheck, diff check. 이전 5210 결과를 이번 통과로 재사용하지 않는다.
- [ ] Step 4: fresh whole-branch 독립 리뷰. material finding은 RED/GREEN으로 수정하고 필요한 검증만 다시 실행한다.
- [ ] Step 5: 검증/지원 runtime/한계를 문서화하고 parent 최종 v1 commit. READY_FOR_TRIBUNAL과 현재 Claim을 보고한다. 원안 전체 Issue 완료나 Task finish를 자동 처리하지 않는다.
