# Claude Code 무거운 플러그인 토글 (전역 off → 필요시 켜기)
# 사용: plug on bkit   /   plug off docs   (이후 세션에서 /reload-plugins)
plug() {
  local act=$1 key=$2 name verb
  case $act in
    on)  verb=enable ;;
    off) verb=disable ;;
    *) echo "unknown action: $act  (on|off)"; return 1 ;;
  esac
  case $key in
    bkit)     name=bkit@bkit-marketplace ;;
    docs)     name=document-skills@anthropic-agent-skills ;;
    pw)       name=playwright@claude-plugins-official ;;
    pyright)  name=pyright-lsp@claude-plugins-official ;;
    compound) name=compound-engineering@every-marketplace ;;
    *) echo "unknown key: $key  (bkit|docs|pw|pyright|compound)"; return 1 ;;
  esac
  claude plugin "$verb" "$name" && echo ">>> 세션에서 /reload-plugins 실행하세요"
}
