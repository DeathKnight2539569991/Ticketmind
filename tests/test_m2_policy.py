from types import SimpleNamespace

import pytest

from ticketmind.agent.policy import input_risks
from ticketmind.agent.schemas import AgentMessage
from ticketmind.agent.proposals import decision_adapter, proposal_adapter, validate_proposal, validate_decision_evidence
from ticketmind.agent.tools import bounded_decision
from ticketmind.core.config import ProcessingSettings
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval.dense import RetrievalHit


@pytest.mark.parametrize("text", ["请尝试停用本地代理后反馈。", "是否可以关闭代理后重试？", "请重启后提供结果。",
                                 "Could you disable the proxy and retry?", "请修改权限再试。",
                                 "您是否同意将客户端请求超时时间调整为大于 5 秒（例如 10 秒）？",
                                 "Would you agree to increase the client timeout?", "是否允许调大客户端等待上限？"])
def test_semantic_questions_are_deferred_to_judge(text):
    for field in ("reply", "questions"):
        proposal = proposal_adapter.validate_python({"next_step": "ask_clarification", "reason": "缺少环境",
            "reply": text if field == "reply" else "请提供当前信息。",
            "questions": [text if field == "questions" else "是否使用代理？"]})
        validate_proposal(proposal, set())


def test_questions_can_ask_existing_facts():
    proposal = proposal_adapter.validate_python({"next_step": "ask_clarification", "reason": "缺少环境",
        "reply": "请提供当前代理配置和报错时间。", "questions": ["是否使用代理？", "当前 Python 版本是什么？"]})
    validate_proposal(proposal, set())


@pytest.mark.parametrize("text, flag", [("密钥泄露", "security"), ("支付失败但已扣款，重复扣款", "payment"),
                                       ("请提升用户权限", "permissions"), ("数据丢失", "data_loss")])
def test_risk_rules_independent_of_model_flags(text, flag):
    assert flag in input_risks(text)


def execute(decisions, *, changes=None, limits=None, tool_error=False):
    config = ProcessingSettings(_env_file=None, **(limits or {}))
    corpus = load_sources(config.corpus_path)
    ids = list(corpus.cases)[:3]
    hits = [RetrievalHit(source_id=i, text=build_case_text(corpus.cases[i]), score=0.5) for i in ids]
    state = {"subject": "API 超时", "messages": [AgentMessage(role="customer", content="Python 3.12，E_TIMEOUT")],
             "retrieval_query": "original", "retrieval_hits": hits[:2], **(changes or {})}
    seen, searches, vectors, audit = [], [], [], []
    def decide(current):
        seen.append(dict(current))
        return decisions[min(len(seen)-1, len(decisions)-1)]
    def search(**kwargs):
        searches.append(kwargs)
        if tool_error:
            raise RuntimeError("synthetic failure")
        return [[{"entity": {"source_id": hits[2].source_id, "text": hits[2].text}, "distance": 0.4}]]
    def embed(query):
        vectors.append(query)
        return [1.0] * 1024
    args = dict(decide=decide, judge=lambda *args: {"passed": True, "violations": []},
                embeddings=SimpleNamespace(embed_query=embed), client=SimpleNamespace(search=search),
                corpus=corpus, config=config, remaining=lambda: 1.0, audit=audit)
    if tool_error:
        with pytest.raises(RuntimeError):
            bounded_decision(state, **args)
        assert audit[-1]["status"] == "failed" and audit[-1]["error"] == "tool_execution_failed"
        return
    result, evidence = bounded_decision(state, **args)
    return result, evidence, seen, searches, vectors, audit


SEARCH = {"next_step": "search_cases", "reason": "需要更多证据", "query": "E_TIMEOUT Python 3.12", "missing_evidence": "原因差异"}
FINAL = {"next_step": "ask_clarification", "reason": "缺少信息", "reply": "请提供现有配置。", "questions": ["当前代理配置是什么？"]}



def test_step_budget_counts_only_the_initial_retrieval_before_first_decision():
    result, _, seen, *_ = execute([FINAL])
    assert result.next_step == "ask_clarification"
    assert seen[0]["agent_steps"] == 2  # initial retrieval + first decision

def test_tools_execute_and_return_new_evidence_to_decision():
    config = ProcessingSettings(_env_file=None)
    ids = list(load_sources(config.corpus_path).cases)
    detail = {"next_step": "get_case_detail", "reason": "核对完整案例", "source_id": ids[0]}
    result, evidence, seen, searches, vectors, audit = execute([SEARCH, detail, FINAL])
    assert result.next_step == "ask_clarification"
    assert len(searches) == len(vectors) == 1 and len(evidence) == 3
    assert seen[-1]["case_details"][ids[0]]["source_id"] == ids[0]
    assert [r["tool"] for r in audit] == ["search_cases", "get_case_detail"]
    assert all(r["status"] == "succeeded" and r["duration_ms"] >= 0 for r in audit)
    assert audit[0]["missing_evidence"] == SEARCH["missing_evidence"]
    assert audit[0]["result_evidence"][0]["source_id"] == evidence[-1].source_id
    assert seen[-1]["agent_steps"] <= 8


@pytest.mark.parametrize("decision, limits, error", [
    (SEARCH, {"max_search_rounds": 1}, "search_limit_or_duplicate"),
    ({**SEARCH, "query": "original"}, {}, "search_limit_or_duplicate"),
    ({**SEARCH, "query": "E_UNKNOWN 9.99"}, {}, "invented_query_facts"),
    ({"next_step": "get_case_detail", "reason": "probe", "source_id": "invented"}, {}, "unknown_candidate"),
])
def test_denied_tools_never_call_external_services(decision, limits, error):
    result, _, _, searches, vectors, audit = execute([decision], limits=limits)
    assert result.next_step == "escalate" and searches == vectors == []
    assert audit[-1]["error"] == error


def test_repeated_search_stops_at_two_total_rounds():
    result, _, seen, searches, vectors, audit = execute([SEARCH])
    assert result.next_step == "escalate" and len(searches) == len(vectors) == 1 and len(seen) == 2


def test_step_limit_does_not_start_unfinishable_tool():
    result, _, seen, searches, vectors, audit = execute([SEARCH], limits={"max_agent_steps": 3})
    assert result.next_step == "escalate" and searches == vectors == []
    assert audit[-1]["error"] == "agent_step_limit"
    assert seen[0]["execution_limits"]["max_agent_steps"] == 3


def test_two_clarifications_then_escalate():
    result, *_ = execute([FINAL], changes={"clarification_rounds": 2})
    assert result.next_step == "escalate"


def test_repeated_details_and_detail_limit():
    ids = list(load_sources(ProcessingSettings().corpus_path).cases)
    detail = {"next_step": "get_case_detail", "reason": "核对", "source_id": ids[0]}
    result, _, _, _, _, audit = execute([detail])
    assert result.next_step == "escalate" and audit[-1]["error"] == "detail_limit_or_duplicate"
    result, _, _, _, _, audit = execute([detail], limits={"max_case_details": 0})
    assert result.next_step == "escalate" and audit[-1]["status"] == "rejected"


def test_tool_failure_is_recorded():
    execute([SEARCH], tool_error=True)


def test_unregistered_tool_rejected():
    with pytest.raises(ValueError):
        decision_adapter.validate_python({"next_step": "execute_shell", "command": "anything"})


def test_expired_budget_stops_before_decision_or_tool():
    def expired():
        raise TimeoutError("budget exhausted")
    def forbidden(*args):
        pytest.fail("expired budget executed a decision")
    with pytest.raises(TimeoutError):
        bounded_decision({"subject": "s", "messages": [AgentMessage(role="customer", content="b")],
                          "retrieval_query": "q", "retrieval_hits": []},
            decide=forbidden, judge=forbidden, embeddings=None, client=None, corpus=None,
            config=ProcessingSettings(_env_file=None), remaining=expired, audit=[])


@pytest.mark.parametrize("quotes", [{}, {"SYN-HIST-V2-007": "调整超时或优化查询是本案例提供的通用解决思路。"},
                                    {"SYN-HIST-V2-006": "该只读全年聚合查询的耗时超过客户端原超时设置。"}])
def test_resolution_cannot_attach_invented_support_to_real_source(quotes):
    corpus = load_sources(ProcessingSettings().corpus_path)
    hit = RetrievalHit(source_id="SYN-HIST-V2-007", text=build_case_text(corpus.cases["SYN-HIST-V2-007"]), score=0.5)
    proposal = proposal_adapter.validate_python({"next_step": "propose_resolution", "reason": "诊断", "reply": "建议调整等待上限。",
        "evidence_ids": [hit.source_id], "evidence_quotes": quotes})
    with pytest.raises(ValueError, match="原文"):
        validate_decision_evidence(proposal, [hit])


def test_resolution_preserves_a_verbatim_support_excerpt():
    corpus = load_sources(ProcessingSettings().corpus_path)
    case = corpus.cases["SYN-HIST-V2-006"]
    hit = RetrievalHit(source_id=case.source_id, text=build_case_text(case), score=0.5)
    proposal = proposal_adapter.validate_python({"next_step": "propose_resolution", "reason": "适用的只读诊断", "reply": "核对等待上限。",
        "evidence_ids": [case.source_id], "evidence_quotes": {case.source_id: case.resolution.summary}})
    validate_decision_evidence(proposal, [hit])
