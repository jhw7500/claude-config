# pre-pr-tribunal v2: 경로별 정책과 리뷰어 설정

Status: 설계 승인 대기. Issue: https://github.com/jhw7500/claude-config/issues/143
Source baseline: `8e2dadc58ef231ecf561d4c945ca8797ca92447e`.

## Outcome and boundary

pre 단계는 **머지 후 되돌리기 비싼 것만** 검증한다. 스타일·중복·죽은 코드·단순성은
PR 계층(`claude-code-review`, `gemini-auto-review`, `pytest`, `shellcheck`,
`commitlint`)에 위임한다.

리뷰 강도는 diff가 건드린 경로가 정한다. 리뷰어의 활성화와 모델은 저장소에
커밋되는 설정으로 연다.

게이트 자체, 증거 스키마, 격리 계약, 3라운드 상한, HIGH/CRITICAL 판정 기준은
유지한다. 백엔드 혼합은 이 변경의 범위가 아니다.

## Why

#113 심사 실측(3라운드, 리뷰어 dispatch 12회, 서브에이전트 약 188만 토큰,
벽시계 약 77분):

| 리뷰어 | findings | 블로커 | 블로커 비율 |
| --- | --- | --- | --- |
| A 정확성·보안 | 7 | 2 | 29% |
| B 실증 검증 | 8 | 4 | 50% |
| C 단순성·범위 | 21 | 0 | 0% |
| 합계 | 36 | 6 | 17% |

블로커 6건은 전부 "스냅샷이 실행 바이트를 묶지 못한다"는 한 부류였고 CI가 잡을 수
없는 종류다. 비블로커 30건은 린터와 PR 리뷰어의 영역이다. 가치는 좁고 비용은 넓다.

라운드는 줄이는 것이 공짜가 아니다. #113에서 블로커는 1→2→3으로 **늘었고**, 늘어난
블로커는 직전 수정이 만든 새 결함이었다. 그래서 라운드는 제거가 아니라 선택으로 둔다.

## Existing surfaces

- `verdict_store.py`가 라운드 전이(`begin_round`, 1103-1140행)와 3라운드 상한을 강제한다.
  `--runtime`은 `{claude, codex}`만 검증하며 `gate.py`·`review_context.py`에서는
  참조되지 않는 관측용 라벨이다.
- `gate.py`가 verdict를 7-튜플(repository, base_ref, base_sha, head_ref, head_sha,
  merge_base_sha, diff_sha256)에 결속한다. 수정은 반드시 재심사를 강제한다.
- `review_store.py:49`의 `check_review_ignored`가 `.review/`의 gitignore를 요구한다.
  따라서 커밋되는 설정을 `.review/` 안에 둘 수 없다(실측: `.review/` 제외 시 하위 파일
  negation 불가; `.review/*`로 바꾸면 verdict가 커밋 가능해진다).
- 리뷰어 dispatch는 코드가 아니라 `SKILL.md`가 정한다 — "On Claude use three native
  `Agent` calls. On Codex use three native `collaboration.spawn_agent` calls."
- `finalize`는 A/B/C 세 receipt를 모두 요구한다.

## Design

### 1. 모드

| 모드 | 리뷰어 | decisions 연속성 | 게이트 |
| --- | --- | --- | --- |
| `off` | 없음 | 해당 없음 | verdict 없이 통과, 생략 사실이 기록됨 |
| `single` | 활성 리뷰어 1회 | 사용 안 함 | 통과 verdict 필요 |
| `iterative` | 활성 리뷰어, 최대 3라운드 | `decisions.json`·`prior_decisions`·원제기자 수락 | 통과 verdict 필요 |

`single`은 라운드 기계장치를 지나가지 않는다. 블로커가 나오면 고치고, 새 스냅샷에서
`single`을 다시 돌린다. `iterative`는 현재 동작을 그대로 유지한다.

### 2. 경로별 정책

diff가 건드린 경로마다 모드를 구하고 **가장 높은 모드**를 적용한다
(`off` < `single` < `iterative`). 매칭되지 않은 경로는 `**` 기본값을 쓴다.

```toml
[policy]
"docs/**"    = "off"
"**/*.md"    = "off"
"hooks/**"   = "iterative"
"scripts/**" = "single"
"**"         = "single"
```

이 설계의 핵심 안전 속성: **코드 변경이 설정으로 `off`가 되지 않는다.** `off`는
경로 패턴이 허용한 범위에서만 적용되며, 사람이 호출 시점에 모드를 낮출 수 없다.
#113과 같은 `hooks/pre_pr_tribunal/**` 변경은 자동으로 `iterative`가 된다.

### 3. 리뷰어 설정

```toml
[reviewer.A]
enabled = true
model   = { claude = "opus", codex = "gpt-5.4" }

[reviewer.B]
enabled = true
model   = { claude = "sonnet", codex = "gpt-5.4" }

[reviewer.C]
enabled = false
```

- 컨트롤러의 네이티브 dispatch를 그대로 쓴다. codex에서 돌리면 codex 모델, claude면
  claude 모델이 적용된다. 백엔드는 섞지 않는다.
- 알 수 없는 모델 이름은 **dispatch 실패**로 처리한다. 기본값으로 조용히 강등하지 않는다.
- 설정은 verdict에 기록하되 게이트 판정에는 넣지 않는다(현재 `runtime`과 같은 취급).
- 설정 파일이 없으면 기본값: A·B 활성, C 비활성, 정책은 `"**" = "single"`.

### 4. 범위 프롬프트

`reviewer-a.md`와 `reviewer-b.md`에 판정 기준을 명시한다.

> 보고 대상은 머지 후 되돌리기 비싼 것뿐이다: 보안 결함, 데이터 손실, 계약 파괴,
> 되돌리기 어려운 설계 선택. 스타일, 중복, 죽은 코드, 명명, 단순성, import 배치는
> 이 리뷰의 범위가 아니며 PR 리뷰어가 담당한다.

리뷰어가 MEDIUM/LOW를 내는 것 자체는 막지 않는다. 런타임이 severity를 거부하면 정보를
버리게 되고, 그건 계층 이동이 아니라 삭제다.

### 5. 비블로커 이관

`finalize` 결과에 활성 리뷰어의 MEDIUM/LOW findings를 모아 PR 본문 부록으로 쓸 수 있는
마크다운 조각을 만든다. 블로커 판정에는 영향을 주지 않는다.

### 6. 스냅샷이 바뀌면 새 리뷰를 시작할 수 있어야 한다

현재 `begin_round`의 round-1 경로는 저장된 verdict가 FAIL이면 **스냅샷을 비교하지 않고**
거부한다(`verdict_store.py:1121-1124`): round 1 FAIL이면 `ROUND_TRANSITION_INVALID`,
round 3 FAIL이면 `ROUND_LIMIT_EXHAUSTED`. 코드가 바뀌었는지 보지 않는다.

그래서 `single` 모드의 "고치고 새 스냅샷에서 다시 1회"가 현재 런타임에서는 불가능하다.
이 변경 없이는 `single`이 성립하지 않는다.

이 차단에는 지켜야 할 이유가 있다 — **같은 스냅샷으로 통과할 때까지 다시 굴리는 것**을
막는다. 따라서 조건을 스냅샷에 붙인다.

| 저장된 verdict | 현재 스냅샷 | round 1 시작 |
| --- | --- | --- |
| FAIL | 저장된 것과 **동일** | 거부 (재굴림 방지, 현재 동작 유지) |
| FAIL | 저장된 것과 **다름** | 허용 (새 코드는 새 심사) |
| IN_PROGRESS | 무관 | 거부 (현재 동작 유지) |
| PASS | 무관 | 허용 |

`iterative`의 라운드 2·3 전이 규칙과 3라운드 상한은 그대로 둔다. 바뀌는 것은 round 1을
새로 시작할 수 있는 조건뿐이다.

### 7. finalize

"세 리뷰어 전원 sealed"를 "활성 리뷰어 전원 sealed"로 바꾼다. 비활성 리뷰어의 슬롯은
만들지 않으며, 2-of-2를 2-of-3의 완화로 해석하지 않는다.

## Error handling

- 설정 파일 파싱 실패, 알 수 없는 키, 알 수 없는 모드 이름은 **fail-closed**로 멈춘다.
  기본값으로 넘어가지 않는다 — 설정이 깨진 상태에서 조용히 약한 모드로 도는 것이
  이 설계에서 가장 위험한 실패다.
- 경로 패턴이 하나도 매칭되지 않고 `**` 기본값도 없으면 `single`로 간주하고 그 사실을
  기록한다.
- `off`로 판정된 diff는 verdict를 만들지 않는다. 게이트는 "이 diff의 정책이 `off`"임을
  스스로 재계산해 확인하며, 컨트롤러의 주장을 믿지 않는다.

## Testing

- 경로 정책: `docs/`만 바뀐 diff → `off`, `hooks/`가 섞인 diff → `iterative`,
  매칭 없음 → 기본값. 가장 높은 모드가 이기는지.
- 게이트: `off` 판정 diff는 verdict 없이 통과하고, 같은 브랜치에 `hooks/` 파일을
  하나 더하면 즉시 막히는지.
- 설정: C 비활성 시 `finalize`가 A·B만으로 판정하는지. 알 수 없는 모델이 실패하는지.
  설정 파일 부재 시 기본값이 적용되는지. 깨진 설정이 fail-closed인지.
- 회귀: #113 스냅샷과 같은 성격의 변경에서 블로커 탐지가 유지되는지.

## Non-goals

- 백엔드 혼합(A=claude, B=codex). 네이티브 dispatch 밖으로 나가는 새 경로가 필요하고,
  응답 바이트 수집과 형식 준수를 백엔드별로 다시 검증해야 한다. 별도 이슈.
- 라운드·결정 기계장치 제거. `iterative`가 사용한다.
- 게이트 우회 경로 추가.
- 증거 스키마·격리 계약 변경.

## 설정 파일 자체를 바꾸는 diff

게이트가 `off` 판정을 스스로 재계산하려면 게이트도 설정 파일을 읽어야 한다. 그러면 설정
파일이 게이트의 입력이 되므로, 정책을 약하게 바꾸는 커밋이 스스로를 약한 모드로 심사시킬
수 있는지가 문제가 된다.

닫는 방법: `.pre-pr-tribunal.toml`을 건드리는 diff는 `hooks/**`와 같이 `iterative`로
강제한다. 그러면 정책을 약화시키는 커밋 자체가 최대 강도 심사를 통과해야 하고, 그 커밋이
머지된 뒤에야 약한 정책이 후속 diff에 적용된다. 약화가 보이지 않게 들어올 경로가 없다.

기본 정책에 이 항목을 넣는다:

```toml
".pre-pr-tribunal.toml" = "iterative"
```
