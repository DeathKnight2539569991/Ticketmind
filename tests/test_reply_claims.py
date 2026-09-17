"""Deterministic proposal policy checks; historical M4 data is read-only."""
import json
from pathlib import Path

import pytest

from ticketmind.agent import decide
from ticketmind.agent.proposals import UnsupportedActionClaim, proposal_adapter, validate_proposal
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.config import QwenSettings
from ticketmind.retrieval.dense import RetrievalHit


def proposal(reply, next_step="escalate"):
    return proposal_adapter.validate_python({
        "next_step": next_step, "reason": "核查当前问题", "reply": reply,
        "evidence_ids": ["actual"] if next_step == "propose_resolution" else [],
        "questions": ["当前错误码是什么？"] if next_step == "ask_clarification" else [],
    })


@pytest.mark.parametrize("reply", [
    "已将该工单转交人工团队。",
    "我们已经通知支付团队处理。",
    "该请求已提交给工程师。",
    "技术人员稍后会联系您。",
    "稍后一定会有客服回复您。",
    "已转交人工。", "已经升级给技术团队。", "我们已提交处理。",
    "已联系相关团队。", "已处理您的请求。", "已经执行操作。",
    "已为您退款。", "已修改您的权限。", "您的权限已经修改。",
    "已将数据恢复。", "已经删除相关数据。",
    "已发送邮件。", "已创建工单。", "已为您派单。",
    "我们会通知支付团队。", "将由工程师处理。",
    "客服一定回复您。", "客服会在核查后处理该请求。",
    "支付支持人员会核对实际入账状态后与您联系。",
    "建议人工核查。我们已经通知团队。",
    "需要人工确认，稍后一定会有客服回复您。",
    "已 将该工单\n转交人工团队。",
])
def test_reject_unsupported_reply_claims_without_rewriting(reply):
    value = proposal(reply)
    original = value.model_dump()
    with pytest.raises(UnsupportedActionClaim) as error:
        validate_proposal(value, set())
    assert error.value.code == "proposal_unsupported_action_claim"
    assert str(error.value) == str(UnsupportedActionClaim())
    assert value.model_dump() == original


@pytest.mark.parametrize("reply,next_step", [
    ("建议转交人工团队进一步确认。", "escalate"),
    ("该问题需要人工支持进一步处理。", "escalate"),
    ("建议人工核查支付状态后再回复客户。", "escalate"),
    ("该问题需要人工审核后再决定是否转交。", "escalate"),
    ("需要人工进一步确认。", "escalate"),
    ("当前尚未转交人工，也未通知团队。", "escalate"),
    ("系统不会自动联系您。建议转交人工核查。", "escalate"),
    ("请提供当前错误码、发生时间及是否使用代理。", "ask_clarification"),
    ("建议根据已测得的只读请求耗时调整等待上限后验证。", "propose_resolution"),
    ("修改配置可能会影响后续查询，请先核对适用环境。", "propose_resolution"),
])
def test_allow_advice_existing_fact_questions_and_non_external_future(reply, next_step):
    validate_proposal(proposal(reply, next_step), {"actual"})


@pytest.mark.parametrize("next_step", ["escalate", "ask_clarification", "propose_resolution"])
def test_reply_check_applies_to_every_proposal_action(next_step):
    with pytest.raises(UnsupportedActionClaim):
        validate_proposal(proposal("已通知相关团队。", next_step), {"actual"})


@pytest.mark.parametrize("case_id", ["006", "015", "018"])
def test_m4_original_failures_rejected_by_production_decision_entry(monkeypatch, case_id):
    path = Path(__file__).resolve().parents[1] / "docs" / "m4-predictions" / f"SYN-EVAL-M4-{case_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data["raw_proposal"]
    # No output regeneration, label changes, or provider calls. Historical rows
    # remain parseable; current policy rejects them when used as new decisions.
    assert proposal_adapter.validate_python(raw).reply == raw["reply"]
    calls = []

    def recorded_response(**kwargs):
        calls.append(kwargs)
        return json.dumps(raw, ensure_ascii=False)

    monkeypatch.setattr(decide, "generate_text", recorded_response)
    state = {"subject": "离线历史响应回放", "body": "检查草稿状态声明",
             "understanding": TicketUnderstanding(summary="离线替身", error_codes=[], environment=[]),
             "retrieval_hits": [RetrievalHit(source_id=e["source_id"], text=e["text"], score=0.5)
                                for e in data["retrieval_evidence"]]}
    settings = QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused")
    with pytest.raises(UnsupportedActionClaim):
        decide.decide_ticket(settings, state)
    assert len(calls) == 1


def test_m4_015_future_promise_is_rejected_even_without_completed_handoff():
    # Independently exercises the second failure in 015, so catching "已转交"
    # cannot hide an ineffective future-commitment rule.
    with pytest.raises(UnsupportedActionClaim):
        validate_proposal(proposal("支付支持人员会核对实际入账状态后与您联系。"), set())
