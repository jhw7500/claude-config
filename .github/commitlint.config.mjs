export default {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'header-max-length': [2, 'always', 100],
    'subject-empty': [2, 'never'],
    'type-empty': [2, 'never'],
    'type-enum': [2, 'always', [
      'feat', 'fix', 'chore', 'refactor', 'docs',
      'test', 'build', 'ci', 'perf', 'style'
    ]],
    // 한글 커밋 메시지 허용
    'subject-case': [0],
  },

  // pre-pr-tribunal 이 지시하는 커밋은 conventional 형식이 아니다.
  // 스킬 9 단계가 blocker 수정마다 `review-fix round N` 커밋을 만들게 한다.
  // conventional 파서는 콜론 앞 전체를 타입으로 읽으므로 `review-fix` 를
  // type-enum 에 넣어도 타입 토큰은 `review-fix round 1` 이 되어 걸리고,
  // 콜론 없는 짧은 형태는 type-empty 에 걸린다. 린트를 켜려고 리뷰 계약을
  // 바꾸는 대신, 린터가 그 형식을 인정하게 한다.
  // merge/revert/reapply 는 defaultIgnores 가 이미 걸러내므로 여기 없다.
  //
  // 종결자에 개행이 필요하다. 이 술어는 subject 가 아니라 커밋 메시지 전체를
  // 받는데(액션이 GitHub API 의 commit.message 를 그대로 lint 에 넘긴다),
  // 본문이 있으면 제목 뒤가 개행이라 `:` 도 `$` 도 걸리지 않는다. JS 의 `$` 는
  // Perl/Python 과 달리 trailing newline 앞에서 매치되지 않는다. 이 저장소의
  // attribution 규칙이 Co-Authored-By 트레일러를 요구하므로 본문 없는
  // review-fix 커밋은 앞으로 나오지 않는다.
  //
  // `m` 플래그는 쓰지 않는다. 붙이면 본문 줄이 `review-fix round 1:` 로
  // 시작하는 무관한 커밋까지 통째로 면제된다.
  ignores: [(message) => /^review-fix round \d+(:|\r?\n|$)/.test(message)],
};
