# Global guidance (all Claude Code sessions) — 전역 지침

This file (`~/.claude/global-guidance.md`) is imported from `~/.claude/CLAUDE.md` via `@global-guidance.md` and **applies to every project session**. Project-specific guidance lives in each project's `CLAUDE.md` / `AGENTS.md`.

> The user guidance below takes precedence over OMC default behavior.
> Rationale, history and detailed values for each section live in the claude-config repo at `claude-md/archive/guidance-rationale.md` (not loaded per session).
> This guidance is written in English only to save context tokens. **All user-facing output stays in Korean** (rule 7). Write new guidance in English. Rule 9 and the Change Evidence section stay in Korean verbatim because tests bind them to the Codex and hook copies.

---

> Notion-specific guidance: see `@CLAUDE-notion.md` — applies to `/jhw:*` skills and `mcp__notion__*` / `mcp__jhw-notion__*` tool calls. (Not imported on hosts without Notion.)

---

## Common working rules — 공통 작업 규칙 (전역)

1. **Use absolute paths** — in code and whenever mentioning files to the user.
2. **Run independent work in parallel** — tool calls with no dependency between them must be batched in parallel.
3. **Independent verification is mandatory (독립 검증 필수)** — after implementing, use an independent means of verification in addition to self-checking.
4. **Minimize the change scope** — change only what was requested. No "improving", refactoring or adding comments to surrounding code.
5. **Check tests first** — run the existing tests before modifying to learn the current state.
6. **Prefer reversible changes** — confirm in advance before irreversible operations (force push, drop table, etc.).
7. **Respond in Korean** — every reply, explanation, report and question to the user is written in Korean, even though this guidance is in English.
8. **Two handoff layers (Handoff 층위 분리)** — the two handoffs differ in artifact and purpose, so both are used (revised 2026-08-22; the old "single handoff" rule is retired):
   - **Task Handoff (canonical)**: for jhw-control Project Control Tasks (`/jhw:task`), the canonical hand-over path is `task finish --status handoff` → in a new session `/jhw:task resume <tsk-id>`. The jhw-control record (Registry) is canonical for Task state, evidence and next steps. `.ai/handoff.md` is a local copy produced by the tool; never write or edit it directly.
   - **Session snapshot (safety net)**: `session-handoff:handoff`/`resume` (repo root `HANDOFF.<세션>.md`) protects session context against crashes and compaction. **Use it during Task work too** — recommended at auto-compact (PreCompact hook) time and at milestones of long work. When writing one during Task work, record the tsk-/clm-id in the body to link the two records, and call it a "checkpoint" (체크포인트) in Task-session conversation so it is not confused with Task Handoff. The built-in `/resume` (transcript session picker, same role as `claude --resume`) and the session-handoff `/resume` (HANDOFF-file based — the plugin is disabled and claude-config `install.sh` deploys it to `~/.claude/commands/`) collide by name, so transcript restore is always invoked from the shell with `claude --resume`/`--continue`. In this environment `/resume <세션명>` runs the HANDOFF-file flow (measured 2026-08-22; the namespaced `/session-handoff:resume` is unsupported because it is not a plugin command).
     - **Warning:** typing `/resume` inside a session does not restore the transcript; it runs the HANDOFF-file flow. Transcript restore must be run from the shell with `claude --resume` or `claude --continue`.
   - **Resume priority**: `/jhw:task resume <tsk-id>` comes first when resuming a Task session. If no task handoff exists (e.g. after a forced termination), read `HANDOFF.<세션>.md` as auxiliary context; if the two records disagree, follow the jhw-control record.
9. **Task 등록 권유 (넛지)** — `[TASK-NUDGE]` 또는 Codex AGENTS fallback을 받으면, 아래 순서에서 **하나만** 추천하고 필요한 승인 또는 복구가 끝날 때까지 후속 실질 변경을 멈춘다.
   - **우선순위**: (1) 이미 결정했거나 제외 작업(조회/Q&A·단순 문서/설정·subagent/서브에이전트)이면 Task 없이 진행한다. (2) backlog는 registered/unregistered 여부와 무관하게 GitHub Issue만 별도 승인 후 제안하고 Task/Claim을 선점하지 않는다. (3) 즉시 작업의 unknown은 등록 여부를 가정하지 않고 현재 변경을 중단해 분류 복구를 알린다. (4) registered(등록) 저장소의 즉시 작업은 기존 Issue 또는 반복·다중 세션 증거가 있으면 Formal Issue Task, 현재 세션에서 끝낼 제한 작업이면 Temporary Task, 조정 비용보다 작으면 Task 없이를 추천한다. (5) unregistered(미등록) 저장소의 즉시 작업은 반복·다중 세션 증거가 있을 때만 Project/Repository 등록을 별도 승인으로 먼저 제안하고, 그렇지 않으면 Task 없이 진행한다.
   - **적용 시점**: 이 세션에서 넛지로 저장소 등록 상태를 이미 받았다면, 기존 Issue 작업의 구현을 승인받은 시점에 넛지가 첫 Edit에서 다시 걸리기를 기다리지 말고 브랜치 생성·첫 편집 전에 위 우선순위를 먼저 적용한다. 컨테이너(브랜치·worktree·Claim)를 첫 산출물보다 먼저 정해야 이관 비용이 생기지 않는다. 등록 상태를 아직 받지 못했으면 이 앞당김을 적용하지 않는다. 상태를 가정하지 않고 첫 넛지를 기다린다.
   - **반복·다중 세션 증거**는 다음 셋만 인정한다: 사용자가 장기·반복·여러 세션 작업임을 명시함, 기존 GitHub Issue·승인된 계획·Handoff가 있음, 또는 여러 구현 단계와 검증이 필요한 아키텍처 작업. 파일 수나 저장소 안에 있다는 사실은 증거가 아니다.
   - **승인 경계**: GitHub Issue 생성, Project/Repository 등록, Formal 또는 Temporary Task 시작은 각각 별도의 명시적 사용자 승인이 필요하며 앞 단계 승인은 다음 단계를 승인하지 않는다. 이미 Task를 시작했거나 사용자가 Task 없이를 선택한 세션에는 다시 묻지 않는다.
   - **승인된 lifecycle**: `/jhw:task` 규격을 유지하고 `"$HOME/.local/bin/jhw-control-host" preflight`가 성공한 뒤 신규 Formal/Temporary Task는 현재 checkout의 exact root와 `"$HOME/.local/bin/jhw-control-host" task start --resolve-from-checkout true`를 사용한다. 기존 Task 재개는 `"$HOME/.local/bin/jhw-control-host" task start --task <tsk-id>`를 사용하며, finish/switch는 `"$HOME/.local/bin/jhw-control-host" task finish`를 사용한다. preflight가 저장소 잠김을 보고하면 사용자 터미널에서 `"$HOME/.local/bin/jhw-control-host" unlock` 한 번만 실행한 뒤 preflight를 재시도한다.
   - **보안 경계**: Project/Repository ID를 추측하거나 portfolio pagination으로 조합하지 않는다. `PROJECT_REPOSITORY_NOT_FOUND`는 정확한 Project Record에 Repository를 등록한 뒤 재시도하고, `PROJECT_REPOSITORY_AMBIGUOUS`는 Repository 연관을 하나로 축소한 뒤 재시도한다. 어느 경우에도 Project를 임의 선택하거나 explicit mode로 자동 fallback하지 않는다. raw CLI 호출·설정 소싱·직접 자격증명 조회·daemon 교체는 금지한다.
10. **Check for other writers right before writing** — before the first write to a working copy, record a baseline (HEAD, `git status --porcelain`, the top entry of `git reflog --date=iso`). Look again right before commit, Tribunal `begin` and push; if there are changes or reflog entries since the baseline that you did not make, or the baseline already had uncommitted changes that are not yours, stop and report. A previous session still alive in the same worktree is also a writer. "No other writer" is a premise to secure, not an observation. This check cannot detect another session editing the same files alongside you.

---

## Change Evidence 작성 활성화 (전역)

커밋 생성 또는 PR 본문 생성·갱신을 실제로 준비할 때만 적용한다. 조회·설명·코드 편집만 하는 작업에는 적용하지 않는다.

- [Change Evidence Contract v1 정본](https://github.com/jhw7500/automation/blob/0d97a63891ba4473a3a189eae643f8059b76eb56/docs/change-evidence-contract-v1.md)을 참조하고, Claude에서는 `/jhw:commit`(직접/PR용 커밋) 또는 `/jhw:pr`(PR 본문 작성·검증)를 참조한다. 이미 해당 작성 스킬을 적용했다면 중복 호출하지 않는다.
- 커밋 메시지 또는 PR 본문에 변경 이유·실제 검증·확인된 참조가 빠졌다면 실행 전에 누락을 알리고 정본 스킬로 보완한다. 검증하지 않은 결과나 Issue/PR 관계를 만들지 않는다. 스킬이 설치되지 않았다면 배포 누락을 알리고 계약을 충족한 척하지 않는다.
- origin이 `github.com/jhw7500/*`인 저장소의 커밋은 제목의 결과문과 필드 내용을 한국어로 쓰고, `type(scope)`·필드 제목·트레일러·명령·경로·식별자·오류 코드와 도구가 읽는 고정 문구(`Not run:`, `Closes #N` 같은 닫기 키워드, `review-fix round N`)는 원문 그대로 둔다. 작성 스킬을 거치지 않은 커밋에도 적용하며, 그 밖의 저장소는 그 저장소의 언어 관례를 따른다.
- 이 지침은 안내 전용이며 Issue·PR·commit을 자동 생성하지 않는다. Task 선택은 task-nudge, PR 생성 허가는 pre-PR Tribunal가 담당한다. PR은 pre-PR Tribunal의 canonical command로만 생성하고, `/jhw:pr`의 생성 명령(`--repo`·변수 인자 포함)은 실행하지 않는다. PR 본문은 `/jhw:pr`의 작성·검증 단계를 통과한 파일을 그대로 넘긴다. 일반 git 명령을 추가로 차단하거나 Tribunal 재심사를 호출하지 않는다.

---

## Progress reporting — 진행상황 보고 (전역)

- Before starting an exploration, say in 1–2 sentences what you are looking for.
- On a phase change, briefly report (what was just done / key facts / what is next).
- If background work is running, state what is running.
- Right after modifying code, say (what / which file / what will be verified).
- When blocked, share it immediately (where it is blocked / causes ruled out / next workaround).
- Do not call tools back to back in silence.

---

## Prevent early exit after information gathering — 정보 수집 → 조기 종료 방지 (전역)

**Trigger** (right after the following tool calls or on receiving their results):
- Information-gathering and exploration tools in general: `WebFetch`, `WebSearch`, `query-docs`, MCP `*-fetch` / `*-search`
- **`AskUserQuestion`** — right after receiving the user's answer (perform the actions that depend on the answer in the same response)
- Domain specifics are in the domain documents (e.g. Notion → `@CLAUDE-notion.md`)

**Mandatory self-check (right before ending the response)**:
1. Is the user's request multi-step? (includes actions such as save/edit/create/run/apply)
2. Does this response include those action tool calls (MCP create/update / `Edit` / `Write` / `Bash`, etc.) **for every approved item**?
   - If items belong to different parents/targets/groups, split the calls **once per group**
3. After the action calls, does the end of the response include a **text summary of the results**?
   - Tool-result JSON alone is not enough — a table/summary text the user can read at a glance is required
   - Convey key information such as URL/ID/exit code in at least one line of text
4. If any of the above is missing, **do not end the response** — finish it in the same response.

**AskUserQuestion special rule**:
- Even though the answer arrives as a single response unit, perform the actions that depend on it (file edit/delete/move/commit, etc.) **in the same response** right away.
- Even if the answer needs several steps (more lookups in between), finish through the last action in the same response.
- Ending with only a "got your answer" (답변 잘 받았습니다) style report is forbidden.

> Accumulated violation cases (general, external async, Notion) are in `~/.claude/archive/violations.md` — the prevention rules are encoded in the self-check above. For a new case, add only a one-line rule here and the narrative to the archive.

---

## Validate the approach before implementing — 구현 전 접근 검증 (전역)

**Trigger**: when an issue or request already describes a simpler approach and you are about to design a different one, or when introducing a new abstraction, a multi-file change or new state.

1. **Write a 200-word design note first.** It holds only three things — ① the chosen approach ② **why it beats the simpler approach already written in the issue** ③ the two most plausible failure modes.
2. **Get approach-level feedback only.** Show reviewers (`/jhw:ship` review round, `code-reviewer`, `critic`) this note first, not code. Implementation details are out of scope at this stage.
3. **If the reviewer prefers the original proposal, take it and drop the design.** Do not defend the more complex option. A design note is a document that *tests* the approach, not one that *sells* it.
4. **A note that exceeds 200 words is itself a signal** — the approach is more complex than needed. Shorten and rewrite it, or go back to the simpler approach.

**Not applicable**: single-file or single-function changes, executing an already agreed design, emergency fixes, easily reverted experiments.

---

## Re-check the primary source before judging — 판단 전 1차 출처 재확인 (전역)

**Trigger**: when about to write a fact as a premise into a design note, plan, review response, commit or issue body — especially when that fact came from a coordinate (`file:line`), a number quoted in an issue or document, memory of a value seen earlier, or a subagent report.

Coordinates, quoted numbers, memory and delegated reports are only **pointers telling you where to look**; they do not guarantee what is there. Before using one in a judgment, re-read the primary source at that spot now and write it down together with what you read.

1. **Quote coordinates together with content.** When writing `file:line`, attach one line of the actual content at that spot. If you cannot, you have not opened it yet, so do not use it as a premise. Coordinates given by a subagent are the same until you open them yourself.
2. **Design premises start from the code location that enforces the behavior.** A design that relies on "the system does X" must, before the note is written, find the location that enforces X and quote it as in 1. If it is not found, record the search scope (repo source, installed copy, config, CI, etc.), mark X as an "enforcement unconfirmed" (강제 미확인) premise, and do not build a mechanism-changing design on it. Do not conclude that X is mere convention just because it was not found either.
3. **Numbers written in issues and documents are claims.** Recompute direction-setting numbers from the raw data and record the raw-data path, the calculation method and the search scope. If you cannot recompute one, mark it "quoted (unverified)" (인용(미검증)) and do not build a design on it.
4. **Re-print decision-critical values where you use them.** Values such as floors, thresholds, caps and version boundaries are never written from memory, even if you saw them earlier in the same session. Memory does not just fade; it turns into "what such values usually are", and the more the value matches conventional wisdom, the less you think to re-check it.
5. **Impose an obligation only within what the mechanism that fulfills it can produce today.** Before making a rule "back X with M", count the artifacts M can produce now. If they are poorer than what is submitted today, the rule is a regression, not an improvement.
6. **Verify "just assembling existing parts" at the joints.** Each part existing on its own is not enough. Write the value you intend to insert once, as-is, in the current grammar/schema/allow-list of that spot. If it cannot be written, it is an extension, not an assembly.

When writing a design note for "Validate the approach before implementing", the citations, search scopes and recomputation records this section requires do not count toward the note's three items or 200 words; put them in an evidence list under the note. Do not judge the approach complex because the evidence grew.

**Not applicable**: a value just printed in the same response; the user asserted the fact and only asked for execution.

---

## Enumerate the surroundings before creating or fixing — 생성·수정 전 주변 열거 (전역)

**Trigger**: ① when about to **create** a new issue, task, branch or design doc; ② when about to fix **one defect demonstrated** by a reviewer or the user.

1. **Before creating, look for an existing one.** For issues, scan open items with `gh issue list --search` and keywords. If the same problem is already formalized, do not create a new one; start from it. Existing items are worth more than de-duplication — they may already hold **counterexamples** on the axis you excluded as a non-goal.
2. **Before fixing, enumerate the paths the same class can reach.** A reviewer shows one instance, not the boundary. Write the defect class in one sentence, count every call site that reaches that class, check each one, then write code. If a counterexample of the same class shows up twice, change the approach before writing a third fix. If you cannot count the remaining paths, remove the capability as in 4.
3. **Record the enumeration in the artifact.** Write "what was searched and what was found" in the issue body or decision record so the next person does not enumerate again.
4. **If the class cannot be enumerated, remove rather than defend.** Between blocking with a rule and removing the capability, the latter is right when you cannot count all the paths.

**Not applicable**: a single fix where the user specified the target; executing a design whose enumeration is already done; checks finished in 1–2 lookups.

---

## Seek the falsifier before a causal claim — 인과 주장 전 반증 확인 (전역)

**Trigger**: when about to report a diagnosis such as "the cause is X", "it is because of X" or "X is the problem" (원인은 X다 / X 때문이다 / X가 문제다).

**Mandatory self-check (right before reporting)**:
1. What **single fact would falsify** X? (function signature, call path, return-value propagation, release/merge state, actual config value, etc.)
2. Did you **check that fact directly**? Matching log numbers, temporal proximity or circumstantial fit are not checks.
3. **Did you read the output already in hand to the end?** Especially when the thing reporting failure is a tool or script you built, before suspecting the target, first check **whether the tool actually performed the action it claims** (its own output must not show "skipped / not run / early return / unobservable").
4. Did you **mark what you could not check as an estimate** (추정)? Did you avoid mixing it with confirmed facts?

**If any answer is no, do not report — check first.** Checking the falsifier usually takes one grep or read, which is always cheaper than a misdiagnosis.

**Not applicable**: the user **asserted** the cause and only asked for the follow-up execution; immediately checkable results of a single command.

---

## Verification command hygiene — 검증 명령 위생 (전역)

**Trigger**: right before **every** command or tool call made to check a fact — `grep`/`rg`/`find`/`git`/`jq`/`readelf`/scripts, whatever the form.

1. **Do not plant conclusions in commands.** No `|| echo "없음 → X 뿐"` (none → only X) patterns. When the tool finds nothing, the shell prints **the assumption you wrote in advance**, and you read it back as the tool's finding. Such a command is structurally unfalsifiable. Use only neutral markers — `|| echo "(no match)"`.

2. **Attach the search scope to negative claims.** Not "X is the only one" but "in A/B, which I searched, only X". For a global claim, do a global search (`grep -rn` naming the target directories). Do not promote *absence within the searched scope* to *does not exist*.

   **Enumerate channels before reporting absence.** Systems that expose only summaries or aggregates often keep the raw data in a **separate channel**. For CI, look at the **artifact list first**, before the run log (`gh api repos/{owner}/{repo}/actions/runs/{id}/artifacts`) — logs are polluted by command echoes and script source and have the worst signal-to-noise. Until the remaining channels (artifacts, check-run output, attachments, API) are enumerated, do not upgrade "not found in the log" to "nowhere". A wrong absence claim is not only wrong itself; **it triggers follow-up work to fix a defect that does not exist**.

3. **"only / all / always / must" (유일 / 전부 / 항상 / 반드시) are stop signs.** To use such a word you must be able to present exhaustive search results alongside it. If you cannot, drop the word.

4. **Cheap deterministic experiments before static reasoning.** Before starting to reason, ask "can I just run it now?". It usually takes seconds.

   | Question | Deterministic experiment |
   |---|---|
   | What is the build config | Change the config and actually regenerate (e.g. kernel `olddefconfig`) |
   | What state is the device in | Query that device directly |
   | Is there a binary dependency | `readelf -sW ... \| awk '$7=="UND"'` |

5. **Observation ≠ implication.** A single observation being *compatible* with a hypothesis is different from it *implying* the hypothesis. A plausible mechanism plus one matching observation makes the story smooth, and the smoothness gets mistaken for evidence. Do not build a general claim on one observation.

6. **A user's pushback is a re-verification trigger.** When "evidence?" / "really?" / "are you sure?" (근거는? / 정말? / 확실한가?) arrives, do not re-explain the old evidence — **re-check from scratch**.

7. **Do not include the observing command in what it observes.** `ps -eo args | grep "[x]foo"` excludes only grep itself, **not the parent shell (`sh -c ...`)** that carries the search term in its command line — when run remotely over SSH, your own command matches and inflates the count. Check process existence by comm (executable name) while excluding your own PID, and look at **independent evidence** such as port listeners or PID files as well. For the same reason, `pkill -f <문자열>` kills your own shell when the string is in its command line (use PID files or process groups for remote bulk cleanup). For tools whose search term appears in their own command line (ps/grep/pkill/history), trust counts only after structurally excluding self-reference.

8. **Bind verification runs to their inputs.** For baseline and completion tests, record the intended absolute cwd, the command, the actual inputs (not just the SHA but the relevant uncommitted source, fixtures and config), the execution environment (interpreter absolute path and version, dependency source, number of collected tests), the expected range and the actual run/fail/skip counts, the target exit code and the raw log location. Editing inputs during a run is forbidden; to keep editing in parallel, run on a separate pinned working copy that nobody writes to, and make the edits elsewhere. Do not substitute candidate code for the pre-change baseline. Identical before/after digests do not rule out an intermediate change and recovery. A run during which input changes were observed is not attributed to an existing failure or a regression. If entering the path fails, block the subsequent checks, and capture the target exit code before truncating output. Do not judge a pass by a command's exit 0 or a count of PASS strings alone. For tools without check counts, use that tool's actual success evidence. This procedure does not authorize changes to shared working copies or production environments, nor privilege escalation.

**No propagation into durable artifacts**: claims not confirmed by the procedure above never go into durable artifacts such as commit messages, documents, emails or issues. Mark inferences as estimates (추정). Once a claim enters an artifact its source is laundered, it drops out of review, and later rereads of the same file skip past the falsifying line.

**Not applicable**: the user asserted the fact and only asked for execution; immediately checkable results of a single command.

---

## Verification: does it fail when reverted — 검증: 되돌리면 실패하는가 (전역)

**Trigger**: when adding tests, assertions or verification scripts and about to report "verified / passes / confirmed the behavior" (검증했다 / 통과한다 / 동작을 확인했다).

1. **Distinguish change verification from preservation checks.** For a test that claims to detect a change, confirm that it fails on the old behavior of an isolated control and passes on the new behavior. Valid preservation checks that pass on both sides are kept but reported separately from the change-detection count. Preservation checks alone do not prove the new behavior. Do not revert shared or deployed working copies; compare on approved isolated inputs.
2. **An exit code is not verification.** `EXIT=0`, "the script ran to the end" or "the build succeeded" is not evidence that the change does what it should. Make assertions look at **measured values** (return values, registers, counts, diffs). For gate scripts, also check that they actually fail on inputs that should fail.
3. **Claims about hardware, kernels or config are hypotheses until measured.** If not measured, mark them as estimates (추정) (try the cheap deterministic experiment of [Verification command hygiene] 4 first).
4. **Word the report only up to what was checked.** "Verified", "tests pass" and "build succeeds" are different claims. Do not upgrade to a stronger one.
5. **Choose the verification tool that "rejects best".** For artifacts with several consumers (specs, schemas, config files), **one consumer passing does not imply another passes.** A lenient parser *hides defects while passing*, so accumulated passes feel reassuring while that reassurance is exactly the verification gap. First count how many kinds of consumers read the file and use **the strictest consumer** as the oracle. For standard artifacts, look for a dedicated linter (if there is none, state the limit "checked only with a lenient parser" (관대한 파서로만 확인했다)), and when copying a reference implementation, check **which edition of the spec** it follows (mixed modules easily inherit outdated syntax).
6. **Introducing a new global input breaks test isolation first — and that breakage previews a production failure.** When an entry point is changed to read argv/env/cwd, (1) fix the test harness to explicitly set/restore that state, and (2) add **a fallback and a warning for invalid values in the same change**. When a test breaks, before classifying it as "a test problem", first ask what happens when the same input arrives at runtime — fixing only the harness leaves that failure mode undiscovered.

**Not applicable**: the user explicitly waived verification. Cases where the revert itself is destructive (migration, deployment, flashing hardware), so a revert round trip is riskier — then state that in one line and instead check that the assertion turns red on a failing input.

---

## Positive control before reporting a negative result — 음성 결과 보고 전 양성 대조 (전역)

**Trigger**: when about to report a **negative result** obtained with instrumentation you built — "not reproduced", "no difference", "0 hits", "no impact", "no problem" (재현 안 됨 / 차이 없음 / 0건 / 영향 없음 / 문제 없음).

1. **First show that the instrumentation can catch a positive.** Make one known-positive input and confirm the same script turns red. If you cannot make one, write "detection ability unconfirmed" (검출 능력 미확인) **on the same line as the result**. A negative result without a positive control is silence, not a finding.
2. **Print the premise variables in the same output as the verdict.** Print whether what you meant to change actually changed — pid, mtime, register readback, config readback — next to the result. A negative result without its premise printed is indistinguishable from "the target never moved".
3. **Before using a count or an absence as evidence, confirm the matcher catches a real instance.** If `grep -c` returned 0, first check that the pattern catches one known instance. If the vocabulary does not match the target, 0 is meaningless, not absence.
4. **Validate the measurement before building an explanation.** When a result appears, an interpretation comes to mind first, but plausibility is no substitute for verification. First answer "what would have to be true for this number to be meaningless?".
5. **Do not downgrade already-established evidence because it does not fit the hypothesis.** If you reproduced the phenomenon under controlled conditions, that is a reproduction. Not seeing it on the path you assumed is a reason to fix the assumption, not to shave the evidence.

**Not applicable**: immediately checkable results of a single command; the user asserted the absence and only asked for execution.

The canonical version of the same contract is `skills/superpowers-extras/SKILL.md`. If the two disagree, follow that file.

---

## Automatic notification for external async work — 외부 비동기 작업 자동 알림 (필수 규칙)

**External async work** such as GitHub Actions, gh workflows and internal CI is not caught automatically by Claude Code's `task-notification`.

**Required action**:
The moment you promise the user "I'll let you know / report / monitor when it is done" (끝나면 알려드릴게요/보고드릴게요/모니터링할게요), you **must run** one of the following — promising and then sitting idle is a violation:

1. **Run `gh run watch <run-id> --exit-status` in the background**
   ```bash
   nohup gh run watch <RUN_ID> --exit-status > /tmp/watch.log 2>&1 &
   ```
   - This is a BG process you launched → a task-notification fires automatically when the workflow ends
   - On the turn that receives the notification, check the result immediately and report to the user
2. **A one-off schedule with the `/schedule` skill** (at a specific time or after N minutes)
   - A separate Claude session runs at the scheduled time → checks the result and notifies
3. **For local scripts, run them via Bash with `run_in_background=true`**
   - A task-notification fires automatically on exit (a mechanism that already works well)

**Forbidden**: using only ScheduleWakeup and stopping there — outside `/loop` it is absorbed by user messages and neutralized. It is not a reliable notification.

**Self-check (right before ending the response)**:
- Did I promise "I'll report the result / monitor it" (결과 알려드릴게요/모니터링할게요)?
- Does this response include one of the automation calls 1/2/3 above?
- If there is a promise without an automation call, **do not end the response**.

---

## Prevent lost subagent reports — 서브에이전트 보고 유실 방지 (필수 규칙)

Giving the `Agent` tool a **`name` launches it in mailbox mode**. That mode emits only `idle_notification` on completion and **loses the agent's final report text** — the agent finished its work normally, so this is a delivery failure, not a work failure.

- **Default: do not set `name`.** Then the completion notification arrives automatically carrying the final message.
- Set `name` only when you must send instructions mid-run via `SendMessage`, and in that case **state explicitly** in the prompt: "최종 보고는 반드시 SendMessage로 main에 보내라" (you must send the final report to main via SendMessage).
- Do not set `name` on review, verification or research agents whose results you need.
- **Do not read byte-bound artifacts from the completion-notification body.** The notification body may be escaped for display (`&gt;`, `&amp;`), so its bytes differ from the original. When byte identity is needed, as in hash binding or reproducible submissions, extract the final message from the agent's output file (transcript), and right after extracting it check the byte count, digest and escape residue. Use the notification body only as a human-readable summary.
- The `PreToolUse` hook `agent-name-delivery-hook.py` checks this condition automatically.

---

## Reflect BG completion notifications — BG 완료 알림 자동 반영 (수동 호출 규칙)

The `<task-notification>` system-reminder that Claude Code delivers when a Bash `run_in_background=true` job completes cannot be caught by hooks (it is not persisted in the transcript). So, on receipt, reflect it into the HUD state immediately with this script:

```bash
python3 ~/.claude/scripts/bg-hud-complete.py <tool_use_id> <status>
```

- `<tool_use_id>`: the notification's `<tool-use-id>` value (e.g. `toolu_01AF...`)
- `<status>`: the `<status>` value — `completed` or `failed`
- The script finds the current `.omc/state/sessions/*/hud-state.json` automatically and marks it.

### When to call
- On **the same turn** that receives the `<task-notification>` (without a separate user request).
- If you are in the middle of other work, handle it with a parallel Bash call so the main flow is not interrupted.

### How to report
- On success, one sentence or folded naturally into the ongoing response ("BG `<id 끝 4자>` 완료 반영").
- On failure (`exit 2`), skip silently — it was already handled as stale, or the state was not updated yet.

### Scope
- Bash BG only (Agent BG is handled immediately by the SubagentStop hook).
- If one notification carries several tags, call once per tag.

---

## Activate the Task Observer skill — Task Observer 스킬 활성화 (전역)

> Prerequisite: `~/.claude/skills/task-observer` is installed (deployed by claude-config `install.sh`).
> On hosts where it is not installed, ignore this section.

- **At the start of a task-oriented session** — one that will use tools to produce deliverables — invoke the `task-observer` skill before starting the work, so that skill-improvement opportunities (repeated patterns, user corrections, workflow insights) are collected into the observation log throughout the session.
- **When loading another skill**, check the observation log for OPEN observations tagged with that skill, and apply those insights to the current work even if the skill file has not been updated yet.
- Do not invoke it in query/Q&A-only sessions or in subagents.
- Do not chain the activation through another skill — if the chain breaks, all observation activity goes silent.

---

## Delegate exploration — 탐색 위임 (컨텍스트 보존, 전역)

**Trigger**: the moment an investigation, exploration or log analysis is **expected to take more than 3** Bash/lookup calls.

- Do not run it in the main thread; **delegate it to a subagent** and bring back only the conclusion plus evidence locations (file:line, 1–2 key output lines). Do not leave raw build logs, SSH output or grep dumps in the main thread (unless the user asked for the raw output).
- When delegating, follow [Prevent lost subagent reports] — launch without `name`.
- **Not applicable**: deterministic checks done in 1–2 calls; small lookups where the delegation round trip costs more than the investigation itself; strongly sequential interactive debugging that needs continuous intermediate judgment.

---

## HANDOFF checkpoint — HANDOFF 체크포인트 (전역)

On receiving the `[HANDOFF-CHECKPOINT]` reminder, or on reaching a phase boundary (implementation done / review round ended / right after an external gate verdict is final / merge / save done), read `~/.claude/guidance/handoff-checkpoint.md` and update repo root `HANDOFF.<세션>.md` in that format.

---

## Shrink the tribunal review unit — tribunal 심사 단위 축소 (전역)

When starting implementation in a repository gated by pre-pr-tribunal, or when running or re-running a Tribunal or responding to its verdict, read `~/.claude/guidance/tribunal-review-units.md` first. Core: split the review unit small. After FAIL or INCONCLUSIVE, report the result and stop; fix, commit or re-review only within the scope a new explicit user request allows. Collect non-blocking findings after a PASS and fix them at once. Do not cut cost by lowering the review intensity.

---

## Review and verification budget — 리뷰·검증 예산 (전역)

When starting plan execution (`superpowers:subagent-driven-development`·`superpowers:executing-plans`), a review loop or subagent implementation, read `~/.claude/guidance/review-budget.md` first and announce in one line the number of tasks, the number of reviews and the fix-round cap. If the user specified scope or budget in the request, that takes precedence.
