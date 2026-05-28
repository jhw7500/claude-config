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
};
