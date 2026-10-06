"""Controlled capabilities verify graph routing, evidence and independent budgets."""
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, END, StateGraph

from docs_fakes import doc_hit
from test_agent_durable import run_durable, runner, SEARCH, DETAIL, FINAL
from ticketmind.agent.review import ReviewWorkflow, ReviewState
from ticketmind.agent.runtime import RunFailure
from ticketmind.agent.semantic_judge import judge_messages
from ticketmind.agent.state import durable_state, typed_state


def test_rejected_case_search_can_switch_to_docs_with_same_query(monkeypatch):
    output, calls, seen, workflow = run_durable(monkeypatch,
        [SEARCH, {**DETAIL, "query": SEARCH["query"]}, {"next_step": "propose_resolution",
        "reason": "product rule", "reply": "请核对产品配置", "evidence_ids": [doc_hit().source_id]}],
        limits={"max_search_rounds": 1})
    assert calls == {"retrieval": 1, "detail": 1, "decision": 3, "judge": 1, "close": 2}
    audit = output.state["tool_calls"]
    assert [call["status"] for call in audit] == ["succeeded", "rejected", "succeeded"]
    assert audit[1]["error"] == "search_limit"
    assert seen[1]["tool_calls"][1] == audit[1]
    assert seen[-1]["agent_steps"] == 6 and seen[-1]["search_rounds"] == 1
    assert seen[-1]["docs_search_rounds"] == 1
    assert seen[-1]["seen_docs_queries"] == [SEARCH["query"].casefold()]
    evidence = output.evidence[-1]
    assert evidence == {"kind": "docs", **doc_hit("case detail").model_dump(mode="json")}
    assert output.state["proposal"].evidence_ids == [evidence["source_id"]]
    frozen = workflow.graph().get_state(workflow.config("guard-thread")).values["agent_data"]
    restored = typed_state(json.loads(json.dumps(frozen, allow_nan=False)))
    assert restored["docs_hits"][0] == doc_hit("case detail")
    _, prompt = judge_messages(restored, output.state["proposal"])
    payload = json.loads(prompt)
    assert payload["docs"][0]["text"] == "case detail" and payload["cases"][0]["text"] == "case"
    assert set(payload["system_capabilities"]["tools"]) == {"search_cases", "search_docs"}


def test_case_and_docs_quotas_are_independent(monkeypatch):
    output, calls, seen, workflow = run_durable(monkeypatch,
        [SEARCH, {**DETAIL, "query": SEARCH["query"]}, {**DETAIL, "query": "产品配置"}, FINAL])
    assert calls["retrieval"] == 2 and calls["detail"] == 2 and calls["judge"] == 1
    data = workflow.graph().get_state(workflow.config("guard-thread")).values["agent_data"]
    assert data["search_rounds"] == data["docs_search_rounds"] == 2
    assert data["agent_steps"] == 8 and data["decision_rounds"] == 4
    assert SEARCH["query"].casefold() in data["seen_queries"] and SEARCH["query"].casefold() in data["seen_docs_queries"]
    assert all(call["status"] == "succeeded" for call in data["tool_calls"])
    assert output.state["proposal"].next_step == "ask_clarification"


@pytest.mark.parametrize("first", [SEARCH, DETAIL])
def test_repeated_rejections_exhaust_steps_without_judge(monkeypatch, first):
    limits = {"max_search_rounds": 1} if first == SEARCH else {"max_docs_search_rounds": 0}
    output, calls, _, workflow = run_durable(monkeypatch, [first], limits=limits)
    assert output.state["proposal"].next_step == "escalate" and calls["judge"] == 0
    data = workflow.graph().get_state(workflow.config("guard-thread")).values["agent_data"]
    assert data["agent_steps"] == 8 and data["search_rounds"] == 1 and data["docs_search_rounds"] == 0
    assert len(data["tool_calls"]) == 4 and all(call["status"] == "rejected" for call in data["tool_calls"][1:])
    assert data["agent_steps"] == len(data["tool_calls"]) + data["decision_rounds"]


@pytest.mark.parametrize("illegal", [
    {"next_step": "get_case_detail", "source_id": "case-1", "reason": "legacy"},
    {"next_step": "search_docs", "source_id": "docs:unknown", "reason": "bad params"},
    {"next_step": "execute_shell", "reason": "unknown", "query": "x"},
])
def test_illegal_tool_protocol_is_terminal_failure(monkeypatch, illegal):
    failure, calls, _, workflow = run_durable(monkeypatch, [illegal])
    assert isinstance(failure, RunFailure) and calls["judge"] == 0
    assert len(failure.partial["tool_calls"]) == 1
    checkpoint = workflow.graph().get_state(workflow.config("guard-thread"))
    assert checkpoint.next == () and checkpoint.values["failure_descriptor"]["fatal"]
    assert workflow.pending_output("guard-thread") is None


def test_docs_infrastructure_failure_preserves_failed_audit(monkeypatch):
    from ticketmind.agent import runtime
    # run_durable installs its own stub; inject failure at the execution boundary instead.
    monkeypatch.setattr(runtime.AgentExecution, "search_docs",
        lambda *args: (_ for _ in ()).throw(RuntimeError("docs infrastructure failed")))
    failure, calls, _, workflow = run_durable(monkeypatch, [DETAIL])
    assert isinstance(failure, RunFailure) and failure.stage == "docs_retrieval"
    assert failure.partial["tool_calls"][-1]["status"] == "failed"
    assert failure.partial["tool_calls"][-1]["error"] == "tool_execution_failed"
    assert calls["judge"] == 0 and workflow.pending_output("guard-thread") is None
    checkpoint = workflow.inspect_compute("guard-thread")
    assert checkpoint.next == ("search_docs",)


@pytest.mark.parametrize("code", ["docs_collection_version_mismatch", "docs_collection_schema_mismatch",
                                 "docs_collection_analyzer_mismatch", "docs_collection_index_mismatch",
                                 "docs_invalid_response"])
def test_docs_protocol_and_configuration_failures_are_terminal(monkeypatch, code):
    from ticketmind.agent import runtime
    from ticketmind.retrieval.schemas import RetrievalError
    def fail(*args):
        raise RetrievalError(code)
    monkeypatch.setattr(runtime.AgentExecution, "search_docs", fail)
    failure, calls, _, workflow = run_durable(monkeypatch, [DETAIL])
    assert isinstance(failure, RunFailure) and runtime.fatal_failure(failure)
    assert calls["judge"] == 0 and workflow.pending_output("guard-thread") is None
    checkpoint = workflow.graph().get_state(workflow.config("guard-thread"))
    assert checkpoint.next == () and checkpoint.values["failure_descriptor"]["code"] == code


@pytest.mark.parametrize("position", ["retrieve", "decision", "bootstrap_retrieve"])
def test_old_compute_checkpoint_is_never_upgraded(position):
    saver = InMemorySaver()
    snapshot = {"run_id": "old", "subject": "s", "messages": [{"author_type": "customer", "body": "b"}],
                "runtime_contract": {"model_config": {"decision_protocol": "semantic-guardrail-decision-v2"}}}
    builder = StateGraph(ReviewState)
    builder.add_node(position, lambda state: {})
    builder.add_edge(START, position)
    builder.add_edge(position, END)
    values = {"snapshot": snapshot}
    if position == "decision":
        values["agent_data"] = {"state_version": 1, "case_details": {}}
    builder.compile(checkpointer=saver).update_state(ReviewWorkflow.config("old"), values, as_node="__start__")
    workflow = ReviewWorkflow(saver)
    assert workflow.recovery_kind("old", snapshot) == "legacy_compute"
    with pytest.raises((RuntimeError, ValueError)):
        workflow.continue_compute("old", runner())


@pytest.mark.parametrize("corruption", ["docs_count", "docs_query", "audit_status", "docs_snapshot", "docs_version", "steps", "source"])
def test_recovery_rejects_corrupt_docs_audit_before_capabilities(monkeypatch, corruption):
    output, _, _, workflow = run_durable(monkeypatch, [DETAIL, FINAL])
    saved = workflow.graph().get_state(workflow.config("guard-thread")).values
    data = saved["agent_data"]
    for key in ("proposal", "candidate_proposal", "judge_result"):
        data.pop(key, None)
    data["decision_result"] = FINAL
    if corruption == "docs_count": data["docs_search_rounds"] += 1
    elif corruption == "docs_query": data["seen_docs_queries"] = ["other query"]
    elif corruption == "audit_status": data["tool_calls"][-1]["status"] = "failed"
    elif corruption == "docs_snapshot": data["evidence"][-1]["text"] = "altered"
    elif corruption == "docs_version":
        saved["snapshot"]["runtime_contract"] = {"model_config": {"state_version": 2, "docs": {"docs_version": "changed"}}}
    elif corruption == "steps": data["agent_steps"] += 1
    else: data["tool_calls"][-1]["result_source_ids"] = ["docs:unknown"]
    workflow.graph().update_state(workflow.config("corrupt-docs"), saved, as_node="search_docs")
    with pytest.raises((ValueError, RuntimeError)):
        workflow.continue_compute("corrupt-docs", runner())
