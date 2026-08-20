#!/usr/bin/env python3
"""
Agent Name Delivery Guard (PreToolUse: Agent)

`name`을 붙여 띄운 서브에이전트는 메일박스 모드로 뜬다. 이 모드에서는 완료 시
`idle_notification`만 발생하고 에이전트의 **최종 보고 텍스트가 유실**된다.
에이전트는 정상적으로 일을 끝내지만 결과가 부모에게 도달하지 않는다.

실제 사고 (2026-08-19, jhw-notion #43): 적대적 리뷰어 3개를 `name`을 붙여 띄웠고
그중 2개의 보고가 통째로 유실됐다. 유실된 리뷰어들은 전체 테스트 스위트까지
돌려놓은 상태였다 — 작업 실패가 아니라 전달 실패였다.

따라서 `name`이 있는데 프롬프트에 SendMessage 전달 지시가 없으면 경고한다.
"""
import json
import sys

REMINDER = """[AGENT-NAME-DELIVERY] `name`을 붙인 Agent는 메일박스 모드로 뜬다 — 완료 시 idle_notification만 오고 **최종 보고 텍스트는 유실**된다. (2026-08-19 리뷰어 3개 중 2개 보고 소실, 작업은 정상 수행됨)

지금 둘 중 하나를 선택하라:
1. **`name`을 빼라** — 완료 알림이 최종 메시지를 담아 자동 도착한다 (권장 기본값)
2. `name`이 꼭 필요하면(실행 중 SendMessage로 지시를 보내야 하는 경우), 프롬프트에 "최종 보고는 반드시 SendMessage로 main에 보내라"를 **명시**하라 — 명시하지 않으면 결과를 못 받는다

결과를 받아야 하는 리뷰·검증·조사 에이전트에 `name`을 붙이지 마라."""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "Agent":
        return 0

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    name = tool_input.get("name")
    if not isinstance(name, str) or not name.strip():
        return 0

    prompt = tool_input.get("prompt")
    prompt = prompt if isinstance(prompt, str) else ""
    if "sendmessage" in prompt.lower():
        return 0

    sys.stdout.write(REMINDER)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
