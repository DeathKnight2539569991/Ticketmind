"""Offline clarification regressions, not measurements of live model quality.

The five scenario rubrics are test-only. Coverage matching is deliberately limited
to these authored replies; it is not a semantic judge for arbitrary model output.
Model substitution verifies prompt/input/output plumbing, not model compliance.
"""
import json
from dataclasses import dataclass

import pytest

from ticketmind.agent import decide, semantic_judge
from ticketmind.agent.proposals import decision_adapter
from ticketmind.agent.schemas import AgentMessage
from ticketmind.core.config import QwenSettings
from ticketmind.retrieval.dense import RetrievalHit
from ticketmind.retrieval.schemas import DocEvidenceHit


@dataclass(frozen=True)
class Scenario:
    name: str
    subject: str
    customer: str
    evidence: str
    questions: tuple[str, ...]
    # Each diagnostic facet has alternative phrases, only for this fixture.
    facets: tuple[tuple[str, ...], ...]
    answered_question: str


SCENARIOS = (
    Scenario(
        "import", "导入只完成了一部分", "导入 CSV，共 230 行，成功 210 行。",
        "若失败记录来自字段校验，应核对错误提示、失败行结构与字段映射。",
        ("失败记录的原始错误提示是什么？",
         "请提供一条失败行的数据样例及当时使用的字段映射，可使用已有文件或截图。"),
        (("错误提示", "报错内容"), ("失败行", "失败记录样例"), ("字段映射", "列映射")),
        "导入文件是什么格式？",
    ),
    Scenario(
        "export", "导出数量与列表不同", "导出的 XLSX 有 17 行，列表显示 29 行。",
        "核对导出范围与列表筛选条件；生成文件期间记录变化也可能影响数量。",
        ("导出时选定的范围与列表筛选条件分别是什么？",
         "文件生成时间与列表查看时间分别是什么，期间是否已有记录变化？"),
        (("导出时选定的范围", "导出范围"), ("筛选条件", "过滤条件"),
         ("生成时间", "导出时间"), ("记录变化", "数据变化")),
        "导出文件是什么格式？",
    ),
    Scenario(
        "statistics", "两个统计结果不同", "查询区间为 6 月 1 日至 6 月 3 日，两个结果相差 12。",
        "统计口径、时间边界和时区不同可能导致结果差异，不能只核对日期。",
        ("两个结果各自统计什么对象、采用什么计数口径及筛选条件？",
         "两边实际使用的起止时间边界和时区分别是什么？"),
        (("计数口径", "统计口径"), ("筛选条件", "过滤条件"),
         ("时间边界", "边界包含"), ("时区", "时间偏移")),
        "查询的是哪几天？",
    ),
    Scenario(
        "permissions", "成员看不到报表", "成员只能查看，管理员可以查看同一报表；不申请修改权限。",
        "查看权限与资源所属范围都影响可见性，尚不能判断需要修改权限。",
        ("该成员当前角色和已有的可查看范围是什么？",
         "报表所属范围及该成员遇到的具体提示是什么，可提供已有截图。"),
        (("当前角色", "已有角色"), ("可查看范围", "授权范围"),
         ("报表所属范围", "资源所属范围"), ("具体提示", "错误提示")),
        "管理员能否查看这份报表？",
    ),
    Scenario(
        "api", "接口间歇失败", "POST /v2/search 间歇返回 502，未修改调用配置。",
        "定位间歇异常需要请求关联信息、发生时段与请求响应上下文；502 本身不能证明根因。",
        ("失败发生的时间及对应的请求关联标识是什么？",
         "失败与成功请求的参数结构、响应内容和出现频率有何差异，可提供已有日志片段。"),
        (("失败发生的时间", "异常时间"), ("请求关联标识", "请求 ID"),
         ("参数结构", "请求样例"), ("响应内容", "响应正文"), ("出现频率", "失败频率")),
        "返回的 HTTP 状态码是什么？",
    ),
)


def fixture_missing_facets(reply, scenario):
    """A narrow offline rubric; never passed into the production prompt/Judge."""
    return [alternatives for alternatives in scenario.facets
            if not any(phrase in reply for phrase in alternatives)]


def complete_reply(scenario):
    questions = "\n".join(f"{index}. {question}"
                          for index, question in enumerate(scenario.questions, 1))
    return questions + "\n材料请先脱敏，遮盖个人信息、业务敏感数据和凭据，保留结构与错误信息。"


def state_for(scenario, *, repair=False, followup=False):
    messages = [AgentMessage(role="customer", content=scenario.customer)]
    if followup:
        # The same known facts may arrive over several customer turns.
        messages = [AgentMessage(role="customer", content="请帮忙核对这个问题。"),
                    AgentMessage(role="customer", content=scenario.customer)]
    messages.append(AgentMessage(role="support", content="此前推测与客户代理设置有关，尚未核实。"))
    state = {
        "subject": scenario.subject, "messages": messages,
        "retrieval_hits": [RetrievalHit(source_id="case-fixture", text=scenario.evidence, score=0.7)],
        "docs_hits": [DocEvidenceHit(
            source_id="doc-fixture", doc_id="d", chunk_id="c", title="诊断依据",
            section="适用条件", text=scenario.evidence, score=0.8,
            docs_version="test-only", content_hash="fixture", synthetic=True, mode="bm25", rank=1,
        )],
        "tool_calls": [], "clarification_rounds": 0,
        "expected_action": "EVALUATION_ONLY_SENTINEL",
        "necessary_questions": scenario.facets,
        "expected_answer": complete_reply(scenario),
    }
    if repair:
        state["guardrail_feedback"] = {
            "violations": [{"type": "false_status_claim", "text": "已通知", "reason": "没有执行"}],
        }
    return state


@pytest.mark.parametrize("requirement", [
    "区分已知、未确认和诊断必需的事实",
    "会改变诊断、证据适用性或下一步处理的缺口",
    "subject 和全部客户消息",
    "不套固定清单",
    "最多 5 个问题",
    "不用一个编号塞入大量独立问题",
    "不臆测功能、菜单、参数或错误含义",
    "产品知识缺口", "有依据的只读获取方式", "请求日志、截图、数据样例须提醒脱敏",
    "输出前检查必要缺口是否覆盖", "修正时保留正确追问及脱敏提醒",
])
def test_prompt_requires_general_fact_driven_completeness(requirement):
    assert requirement in decide.SYSTEM_PROMPT


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
@pytest.mark.parametrize("repair,followup", [(False, False), (False, True), (True, True)])
def test_scenario_context_and_complete_reply_survive_decision(monkeypatch, scenario, repair, followup):
    state = state_for(scenario, repair=repair, followup=followup)
    captured = []
    reply = complete_reply(scenario)

    def substitute(**kwargs):
        captured.append(kwargs)
        return json.dumps({"next_step": "ask_clarification", "reason": "仍缺少区分原因的事实",
                           "reply": reply}, ensure_ascii=False)

    monkeypatch.setattr(decide, "generate_text", substitute)
    settings = QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused",
                            DASHSCOPE_WORKSPACE_ID="unused", model="qwen3.8-flash")
    proposal = decide.decide_ticket(settings, state)
    assert proposal.reply == reply
    assert fixture_missing_facets(proposal.reply, scenario) == []
    assert scenario.answered_question not in proposal.reply
    assert len(scenario.questions) <= 5 and "脱敏" in proposal.reply
    system = captured[0]["system_prompt"]
    payload = json.loads(captured[0]["user_prompt"])
    assert payload["messages"] == [message.model_dump() for message in state["messages"]]
    assert payload["cases"][0]["text"] == payload["docs"][0]["text"] == scenario.evidence
    for private_key in ("expected_action", "necessary_questions", "expected_answer"):
        assert private_key not in payload
    assert "EVALUATION_ONLY_SENTINEL" not in system + captured[0]["user_prompt"]
    assert scenario.questions[0] not in system  # No scenario few-shots in production.
    assert "输出前检查必要缺口是否覆盖" in system


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
def test_completeness_rubric_catches_missing_question_even_with_valid_action(scenario):
    assert fixture_missing_facets(complete_reply(scenario), scenario) == []
    for omitted in range(len(scenario.questions)):
        reply = "\n".join(question for index, question in enumerate(scenario.questions) if index != omitted)
        proposal = decision_adapter.validate_python({
            "next_step": "ask_clarification", "reason": "需要补充事实", "reply": reply,
        })
        # A correct action and valid Schema cannot establish diagnostic completeness.
        assert proposal.next_step == "ask_clarification"
        assert fixture_missing_facets(proposal.reply, scenario)


def test_judge_duties_stay_at_four_without_completeness_grading():
    schema = semantic_judge.Violation.model_json_schema()
    assert set(schema["properties"]["type"]["enum"]) == {
        "operation_in_clarification", "repeated_known_fact",
        "false_status_claim", "unsupported_commitment",
    }
    assert "不检查追问完整性" in semantic_judge.SYSTEM_PROMPT


def test_changed_prompt_cannot_reuse_previous_decision_cache(monkeypatch):
    from ticketmind.agent.dev_decision_cache import decision_fingerprint

    settings = QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused",
                            DASHSCOPE_WORKSPACE_ID="unused", model="qwen3.8-flash")
    state = state_for(SCENARIOS[0])
    current = decision_fingerprint(settings, state)
    monkeypatch.setattr(decide, "SYSTEM_PROMPT", "previous prompt fixture")
    assert decision_fingerprint(settings, state) != current
