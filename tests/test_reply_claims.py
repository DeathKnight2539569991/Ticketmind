"""Semantic reply regression fixtures; fake Judge responses do not measure model accuracy."""
import json

import pytest

from ticketmind.agent import semantic_judge
from ticketmind.agent.proposals import proposal_adapter, validate_proposal
from ticketmind.agent.schemas import AgentMessage
from ticketmind.core.config import QwenSettings


@pytest.mark.parametrize("reply,kind", [
    ("不要做任何修改", None),
    ("之前是否停用过代理", None),
    ("之前是否尝试过停用代理？", None),
    ("请停用代理后重试", "operation_in_clarification"),
    ("建议人工核查", None),
    ("届时会由人工确认恢复范围", "unsupported_commitment"),
    ("人工之后会联系您", "unsupported_commitment"),
    ("我们已经通知团队", "false_status_claim"),
    ("您是否使用代理？", "repeated_known_fact"),
])
def test_semantic_fixtures_via_structured_judge(monkeypatch, reply, kind):
    value = proposal_adapter.validate_python({
        "next_step": "ask_clarification", "reason": "核对事实", "reply": reply,
    })
    # The deterministic path must not reject negation or historical operations.
    validate_proposal(value, set())
    violations = [] if kind is None else [
        {"type": kind, "text": reply, "reason": "离线预设判定，仅验证协议和接线"}]
    expected = {"violations": violations, "passed": kind is None}
    calls = []
    def response(**kwargs):
        calls.append(kwargs)
        return json.dumps({"violations": violations}, ensure_ascii=False)
    monkeypatch.setattr(semantic_judge, "generate_text", response)
    state = {"subject": "连接失败",
             "messages": [AgentMessage(role="customer", content="当前使用本地代理。")],
             "tool_calls": [], "label": "FORBIDDEN_LABEL", "expected_action": "FORBIDDEN_ANSWER"}
    result = semantic_judge.judge_proposal(
        QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
        state, value)
    assert result.model_dump() == expected
    payload = json.loads(calls[0]["user_prompt"])
    assert set(payload) == {"subject", "messages", "proposal", "tool_calls", "system_capabilities"}
    assert payload["proposal"] == value.model_dump()
    assert payload["messages"] == [{"role": "customer", "content": "当前使用本地代理。"}]
    assert "FORBIDDEN" not in calls[0]["user_prompt"]
    assert calls[0]["json_mode"] is True
