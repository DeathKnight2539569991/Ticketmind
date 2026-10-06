from docs_fakes import doc_hit
import pytest

from ticketmind.agent.schemas import AgentMessage
from ticketmind.agent.proposals import decision_adapter, proposal_adapter, validate_proposal
from ticketmind.agent.dev_workflow import run_decision_workflow
from ticketmind.core.config import ProcessingSettings
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval.dense import RetrievalHit


@pytest.mark.parametrize("text", ["请尝试停用本地代理后反馈。", "是否可以关闭代理后重试？", "请重启后提供结果。",
                                 "Could you disable the proxy and retry?", "请修改权限再试。",
                                 "您是否同意将客户端请求超时时间调整为大于 5 秒（例如 10 秒）？",
                                 "Would you agree to increase the client timeout?", "是否允许调大客户端等待上限？"])
def test_semantic_clarification_is_deferred_to_judge(text):
    proposal = proposal_adapter.validate_python(
        {"next_step": "ask_clarification", "reason": "缺少环境", "reply": text}
    )
    validate_proposal(proposal, set())


def test_clarification_reply_can_ask_for_multiple_facts():
    proposal = proposal_adapter.validate_python({
        "next_step": "ask_clarification",
        "reason": "缺少环境",
        "reply": "请提供当前代理配置和报错时间，并说明当前 Python 版本。",
    })
    validate_proposal(proposal, set())


@pytest.mark.parametrize("subject, content, next_step", [
    ("报表刷新延迟", "每15分钟刷新；没有任何数据丢失提示。", "propose_resolution"),
    ("安全事件", "密钥泄露，需要安全人员核查。", "escalate"),
    ("账务争议", "显示重复扣款，需要人工核对账务。", "escalate"),
    ("权限请求", "需要提升用户权限。", "escalate"),
    ("恢复问题", "数据丢失，需要人工核对恢复范围。", "escalate"),
])
def test_risk_language_does_not_bypass_decision(subject, content, next_step):
    decision = (
        {"next_step": "propose_resolution", "reason": "依据已提供的刷新周期作解释",
         "reply": "页面数据可能按15分钟周期刷新。", "evidence_ids": []}
        if next_step == "propose_resolution" else
        {"next_step": "escalate", "reason": "需要人工处理", "reply": "建议人工核查。"}
    )
    result, _, seen, _, _ = execute([decision], changes={
        "subject": subject, "messages": [AgentMessage(role="customer", content=content)],
    })
    assert len(seen) == 1
    assert result.next_step == next_step
    assert result.evidence_ids == []
    if next_step == "escalate":
        assert result.risk_flags == []


def execute(decisions, *, changes=None, limits=None, tool_error=False):
    config = ProcessingSettings(_env_file=None, **(limits or {}))
    corpus = load_sources(config.corpus_path)
    ids = list(corpus.cases)[:3]
    hits = [RetrievalHit(source_id=i, text=build_case_text(corpus.cases[i]), score=0.5) for i in ids]
    state = {"subject": "API 超时", "messages": [AgentMessage(role="customer", content="Python 3.12，E_TIMEOUT")],
             "retrieval_query": "original", "retrieval_hits": hits[:2], **(changes or {})}
    seen, searches, audit = [], [], []
    def decide(current):
        seen.append(dict(current))
        return decisions[min(len(seen)-1, len(decisions)-1)]
    def search(query, record):
        searches.append(query)
        if tool_error:
            raise RuntimeError("synthetic failure")
        result = [hits[2]]
        # Retrieval-service diagnostics belong to the retrieval adapter, not
        # run_decision_workflow; mimic that boundary in this unit test.
        record["result_hits"] = [{"source_id": hits[2].source_id}]
        return result
    args = dict(decide=decide, judge=lambda *args: {"passed": True, "violations": []},
                corpus=corpus, config=config, remaining=lambda: 1.0, audit=audit, search_fn=search, docs_fn=lambda query, record: [doc_hit()])
    if tool_error:
        with pytest.raises(RuntimeError):
            run_decision_workflow(state, **args)
        assert audit[-1]["status"] == "failed" and audit[-1]["error"] == "tool_execution_failed"
        return
    result, evidence = run_decision_workflow(state, **args)
    return result, evidence, seen, searches, audit


SEARCH = {"next_step": "search_cases", "reason": "缺少适用证据，需要按客户事实重新检索", "query": "E_TIMEOUT Python 3.12"}
FINAL = {"next_step": "ask_clarification", "reason": "缺少信息", "reply": "请提供当前代理配置。"}



def test_step_budget_counts_only_the_initial_retrieval_before_first_decision():
    result, _, seen, *_ = execute([FINAL])
    assert result.next_step == "ask_clarification"
    assert seen[0]["agent_steps"] == 2  # initial retrieval + first decision

def test_tools_execute_and_return_new_evidence_to_decision():
    config = ProcessingSettings(_env_file=None)
    ids = list(load_sources(config.corpus_path).cases)
    detail = {"next_step": "search_docs", "reason": "核对产品规则", "query": "product rules"}
    result, evidence, seen, searches, audit = execute([SEARCH, detail, FINAL])
    assert result.next_step == "ask_clarification"
    assert searches == [SEARCH["query"]] and len(evidence) == 3
    assert seen[-1]["docs_hits"][0] == doc_hit()
    assert [r["tool"] for r in audit] == ["search_cases", "search_docs"]
    assert all(r["status"] == "succeeded" and r["duration_ms"] >= 0 for r in audit)
    assert audit[0]["result_hits"] == [{"source_id": evidence[-1].source_id}]
    assert seen[-1]["agent_steps"] <= 8


@pytest.mark.parametrize("decision, limits, error", [
    (SEARCH, {"max_search_rounds": 1}, "search_limit"),
    ({**SEARCH, "query": "original"}, {}, "duplicate_query"),
    ({**SEARCH, "query": "E_UNKNOWN 9.99"}, {}, "invented_query_facts"),
    ({"next_step": "search_docs", "reason": "probe", "query": "product rules"}, {"max_docs_search_rounds": 0}, "search_limit"),
])
def test_denied_tools_never_call_external_services(decision, limits, error):
    result, _, _, searches, audit = execute([decision], limits=limits)
    assert result.next_step == "escalate" and searches == []
    assert any(call.get("error") == error for call in audit)


def test_repeated_search_stops_at_two_total_rounds():
    result, _, seen, searches, audit = execute([SEARCH])
    assert result.next_step == "escalate" and searches == [SEARCH["query"]] and len(seen) == 4


def test_last_tool_step_executes_then_routes_fixed_review_proposal():
    result, _, seen, searches, audit = execute([SEARCH], limits={"max_agent_steps": 3})
    assert result.next_step == "escalate" and searches == [SEARCH["query"]]
    assert audit[-1]["status"] == "succeeded"
    assert seen[0]["execution_limits"]["max_agent_steps"] == 3


def test_two_clarifications_then_escalate():
    result, *_ = execute([FINAL], changes={"clarification_rounds": 2})
    assert result.next_step == "escalate"


def test_repeated_docs_and_docs_limit():
    ids = list(load_sources(ProcessingSettings().corpus_path).cases)
    detail = {"next_step": "search_docs", "reason": "核对", "query": "product rules"}
    result, _, _, _, audit = execute([detail])
    assert result.next_step == "escalate" and audit[-1]["error"] == "duplicate_query"
    result, _, _, _, audit = execute([detail], limits={"max_docs_search_rounds": 0})
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
        run_decision_workflow({"subject": "s", "messages": [AgentMessage(role="customer", content="b")],
                          "retrieval_query": "q", "retrieval_hits": []},
            decide=forbidden, judge=forbidden, corpus=None,
            config=ProcessingSettings(_env_file=None), remaining=expired, audit=[], search_fn=forbidden)


def test_resolution_with_retrieved_source_id_passes_without_quotes():
    corpus = load_sources(ProcessingSettings().corpus_path)
    case = next(iter(corpus.cases.values()))
    hit = RetrievalHit(source_id=case.source_id, text=build_case_text(case), score=0.5)
    result, evidence, *_ = execute([{
        "next_step": "propose_resolution", "reason": "按历史案例核对", "reply": "请核对本次配置。",
        "evidence_ids": [hit.source_id],
    }])
    assert result.next_step == "propose_resolution"
    assert result.evidence_ids == [hit.source_id]
    assert "evidence_quotes" not in result.model_dump()
    assert any(item.source_id == hit.source_id for item in evidence)


def test_resolution_with_unknown_source_id_still_rejected():
    with pytest.raises(ValueError, match="不存在"):
        execute([{
            "next_step": "propose_resolution", "reason": "引用错误", "reply": "请核对本次配置。",
            "evidence_ids": ["invented"],
        }])
