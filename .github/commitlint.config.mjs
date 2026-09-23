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
  ignores: [(message) => /^review-fix round \d+(:|$)/.test(message)],
};
