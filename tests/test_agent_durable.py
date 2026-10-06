"""Fail closed before any resource or model call on invalid continuation."""
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ticketmind.agent.review import ReviewWorkflow
from ticketmind.agent.runtime import AgentRunner
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings


def runner():
    def forbidden(*args):
        pytest.fail("invalid continuation must not invoke any external capability")
    return AgentRunner(
        QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
        MilvusSettings(_env_file=None, uri="http://unused.invalid"),
        ProcessingSettings(_env_file=None, retrieval_mode="bm25"),
        corpus=SimpleNamespace(evidence=forbidden), milvus_factory=forbidden, decision_fn=forbidden)


@pytest.mark.parametrize("invalid", ["missing", "legacy_compute", "missing_data", "negative_budget", "changed_input"])
def test_compute_continuation_rejects_invalid_checkpoint_without_new_run(invalid):
    workflow, runtime = ReviewWorkflow(InMemorySaver()), runner()
    graph, config = workflow.graph(), workflow.config("same-thread")
    snapshot = {"run_id": "run", "subject": "s", "clarification_rounds": 0,
                "messages": [{"author_type": "customer", "body": "b"}]}
    data = {"state_version": 1, "subject": "s", "messages": [{"role": "customer", "content": "b"}],
            "clarification_rounds": 0, "retrieval_query": "s b", "retrieval_hits": [],
            "tool_calls": [{"tool": "search_cases", "status": "succeeded"}],
            "agent_steps": 1, "search_rounds": 1, "repair_attempt": 0, "seen_queries": ["s b"],
            "usage": {}, "evidence": [], "compute_elapsed_seconds": 1}
    if invalid == "legacy_compute":
        from langgraph.graph import StateGraph, START, END
        from ticketmind.agent.review import ReviewState
        builder = StateGraph(ReviewState)
        builder.add_node("compute", lambda state: {})
        builder.add_edge(START, "compute")
        builder.add_edge("compute", END)
        builder.compile(checkpointer=workflow.checkpointer).update_state(config, {"snapshot": snapshot}, as_node="__start__")
    elif invalid != "missing":
        if invalid == "negative_budget":
            data["compute_elapsed_seconds"] = -1
        elif invalid == "changed_input":
            data["subject"] = "different"
        values = {"snapshot": snapshot}
        if invalid != "missing_data":
            values["agent_data"] = data
        graph.update_state(config, values, as_node="retrieve")
    with pytest.raises((RuntimeError, ValueError, KeyError)):
        workflow.continue_compute("same-thread", runtime)


FINAL = {"next_step": "ask_clarification", "reason": "missing facts", "reply": "请补充配置"}
SEARCH = {"next_step": "search_cases", "reason": "inspect facts", "query": "E_TIMEOUT Python 3.12"}
DETAIL = {"next_step": "get_case_detail", "reason": "inspect case", "source_id": "case-1"}


@pytest.mark.parametrize("invalid_usage", [SimpleNamespace(client="runtime-only"), float("nan")])
@pytest.mark.parametrize("estimated", [False, True])
def test_invalid_adapter_usage_still_persists_terminal_failure(monkeypatch, invalid_usage, estimated):
    import json
    from ticketmind.agent import runtime
    from ticketmind.agent.runtime import RunFailure

    calls = []
    runtime_runner = runner()
    runtime_runner.milvus_factory = lambda _: SimpleNamespace(close=lambda: None)
    runtime_runner.corpus.evidence = lambda hits: []
    monkeypatch.setattr(runtime, "retrieve_cases", lambda *args, **kwargs: [])
    monkeypatch.setattr(runtime, "monotonic", lambda: 0.)
    ledger = [{"classification": "conservative/estimated", "effective_timeout_seconds": 30,
               "origin_checkpoint_id": "test-hard-crash", "node": "decision"}]
    if estimated:
        initialize = runtime.AgentExecution.__init__
        def initialized(self, *args, **kwargs):
            initialize(self, *args, **kwargs)
            if self.partial.get("agent_steps") == 1:
                self.partial.update(compute_estimated_seconds=30, compute_observed_seconds=0,
                                    compute_elapsed_seconds=30, compute_estimates=ledger)
                self.budget.previous_elapsed = 30
        monkeypatch.setattr(runtime.AgentExecution, "__init__", initialized)

    def invalid_decision(state, timeout, usage):
        calls.append("decision")
        usage["invalid"] = invalid_usage
        return FINAL

    runtime_runner.decision_fn = invalid_decision
    workflow = ReviewWorkflow(InMemorySaver())
    snapshot = {"run_id": "run", "subject": "s", "clarification_rounds": 0,
                "messages": [{"author_type": "customer", "body": "b"}]}
    with pytest.raises(RunFailure) as caught:
        workflow.start(snapshot, "invalid-usage", runtime_runner)
    assert isinstance(caught.value.__cause__, ValueError)
    checkpoint = workflow.graph().get_state(workflow.config("invalid-usage"))
    assert checkpoint.next == () and checkpoint.values["failure_descriptor"]["fatal"] is True
    json.dumps(checkpoint.values, allow_nan=False)
    json.dumps(caught.value.usage, allow_nan=False)
    assert caught.value.usage["diagnostic"]["code"] == "invalid_usage_data"
    budget = caught.value.usage["execution_budget"]
    assert budget["observed_elapsed_seconds"] == 0
    assert budget["estimated_elapsed_seconds"] == (30 if estimated else 0)
    assert budget["estimates"] == (ledger if estimated else [])
    with pytest.raises(RuntimeError):
        workflow.continue_compute("invalid-usage", runtime_runner)
    assert calls == ["decision"]


def run_durable(monkeypatch, decisions, *, limits=None, clarification_rounds=0, tool_error=False, judgments=None):
    from ticketmind.agent import runtime
    from ticketmind.agent.runtime import RunFailure
    from ticketmind.retrieval.dense import RetrievalHit

    calls = {"retrieval": 0, "detail": 0, "decision": 0, "judge": 0, "close": 0}
    seen = []
    def search(*args, **kwargs):
        calls["retrieval"] += 1
        if tool_error and calls["retrieval"] > 1:
            raise RuntimeError("tool failed")
        return [RetrievalHit(source_id="case-1", text="case", score=0.5)]
    monkeypatch.setattr(runtime, "retrieve_cases", search)
    def detail(source_id):
        calls["detail"] += 1
        return {"source_id": source_id, "text": "case detail"}
    def decide(state, timeout, usage):
        seen.append(state)
        calls["decision"] += 1
        return decisions[min(len(seen)-1, len(decisions)-1)]
    def judge(state, proposal, *args):
        calls["judge"] += 1
        assert len(state["tool_calls"]) >= 1
        value = (judgments or [{"violations": []}])[min(calls["judge"] - 1, len(judgments or [None]) - 1)]
        if isinstance(value, Exception):
            raise value
        return value
    def close():
        calls["close"] += 1
    runtime_runner = AgentRunner(
        QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
        MilvusSettings(_env_file=None, uri="http://unused.invalid"),
        ProcessingSettings(_env_file=None, retrieval_mode="bm25", **(limits or {})),
        corpus=SimpleNamespace(evidence=lambda hits: [{"source_id": h.source_id} for h in hits], get_case_detail=detail),
        milvus_factory=lambda _: SimpleNamespace(close=close), decision_fn=decide, judge_fn=judge)
    workflow = ReviewWorkflow(InMemorySaver())
    snapshot = {"run_id": "run", "subject": "API 超时", "clarification_rounds": clarification_rounds,
                "messages": [{"author_type": "customer", "body": "Python 3.12，E_TIMEOUT"}]}
    try:
        output = workflow.start(snapshot, "guard-thread", runtime_runner)
    except RunFailure as exc:
        output = exc
    return output, calls, seen, workflow


@pytest.mark.parametrize("decisions,limits,error", [
    ([SEARCH], {"max_agent_steps": 3}, "agent_step_limit"),
    ([SEARCH], {"max_search_rounds": 1}, "search_limit_or_duplicate"),
    ([SEARCH, SEARCH], {}, "search_limit_or_duplicate"),
    ([{**SEARCH, "query": "标题：API 超时\n\n客户问题：Python 3.12，E_TIMEOUT"}], {}, "search_limit_or_duplicate"),
    ([{**SEARCH, "query": "E_UNKNOWN 9.99"}], {}, "invented_query_facts"),
    ([{**DETAIL, "source_id": "unknown"}], {}, "unknown_candidate"),
    ([DETAIL], {"max_case_details": 0}, "detail_limit_or_duplicate"),
    ([DETAIL, DETAIL], {}, "detail_limit_or_duplicate"),
])
def test_durable_graph_rejects_tools_and_still_runs_judge_review(monkeypatch, decisions, limits, error):
    output, calls, seen, workflow = run_durable(monkeypatch, decisions, limits=limits)
    assert output.state["proposal"].next_step == "escalate"
    assert output.state["tool_calls"][-1]["status"] == "rejected"
    assert output.state["tool_calls"][-1]["error"] == error
    assert calls["judge"] == 1 and calls["decision"] == len(decisions)
    assert calls["retrieval"] == (2 if len(decisions) == 2 and decisions[0] == SEARCH else 1)
    assert calls["detail"] == int(len(decisions) == 2 and decisions[0] == DETAIL)
    assert seen[0]["agent_steps"] == 2
    assert workflow.pending_output("guard-thread") is not None


def test_durable_clarification_limit_and_unknown_evidence(monkeypatch):
    from ticketmind.agent.runtime import RunFailure

    output, calls, _, workflow = run_durable(monkeypatch, [FINAL], clarification_rounds=2)
    assert output.state["proposal"].next_step == "escalate" and calls["judge"] == 1
    assert workflow.pending_output("guard-thread") is not None
    invalid = {"next_step": "propose_resolution", "reason": "x", "reply": "x", "evidence_ids": ["unknown"]}
    output, calls, _, workflow = run_durable(monkeypatch, [invalid])
    assert isinstance(output, RunFailure) and calls["judge"] == 0
    assert workflow.pending_output("guard-thread") is None


def test_durable_tool_failure_has_audit_without_publishing(monkeypatch):
    from ticketmind.agent.runtime import RunFailure

    failure, calls, _, workflow = run_durable(monkeypatch, [SEARCH], tool_error=True)
    assert isinstance(failure, RunFailure)
    assert failure.stage == "retrieval"
    assert failure.partial["tool_calls"][-1]["status"] == "failed"
    assert failure.partial["tool_calls"][-1]["error"] == "tool_execution_failed"
    assert calls["judge"] == 0 and calls["close"] == 2
    assert workflow.pending_output("guard-thread") is None


@pytest.mark.parametrize("repair", [FINAL, SEARCH, DETAIL])
def test_durable_finish_retains_one_final_only_repair(monkeypatch, repair):
    from ticketmind.agent.runtime import RunFailure

    bad = {"next_step": "escalate", "reason": "needs human", "reply": "人工一定会处理"}
    rejected = {"violations": [{"type": "unsupported_commitment", "text": bad["reply"], "reason": "unsupported"}]}
    output, calls, seen, workflow = run_durable(monkeypatch, [bad, repair], judgments=[rejected, {"violations": []}])
    assert calls["decision"] == 2 and calls["retrieval"] == 1 and calls["detail"] == 0
    assert seen[1]["agent_steps"] == 3 and "guardrail_feedback" in seen[1]
    if repair == FINAL:
        assert output.state["proposal"].next_step == "ask_clarification"
        assert calls["judge"] == 2 and workflow.pending_output("guard-thread") is not None
    else:
        assert isinstance(output, RunFailure) and calls["judge"] == 1
        assert output.__cause__.code == "guardrail_repair_failed"
        assert workflow.pending_output("guard-thread") is None


@pytest.mark.parametrize("mode", ["second_reject", "step_limit", "repair_clarification", "repair_evidence", "judge_error"])
def test_durable_judge_repair_safety_boundaries(monkeypatch, mode):
    from ticketmind.agent.runtime import RunFailure

    bad = {"next_step": "escalate", "reason": "human", "reply": "人工一定会处理"}
    rejected = {"violations": [{"type": "unsupported_commitment", "text": bad["reply"], "reason": "unsupported"}]}
    original = TimeoutError("synthetic judge exception")
    repair = ({**FINAL, "evidence_ids": ["unknown"]} if mode == "repair_evidence" else
              FINAL if mode == "repair_clarification" else bad)
    output, calls, seen, workflow = run_durable(monkeypatch, [DETAIL, bad, repair] if mode == "step_limit" else [bad, repair],
        limits={"max_agent_steps": 4} if mode == "step_limit" else {},
        clarification_rounds=2 if mode == "repair_clarification" else 0,
        judgments=[original] if mode == "judge_error" else [rejected])
    assert isinstance(output, RunFailure) and "proposal" not in output.partial
    assert output.stage == "semantic_guardrail" and workflow.pending_output("guard-thread") is None
    code = {"second_reject": "semantic_guardrail_failure", "step_limit": "guardrail_step_limit",
            "repair_clarification": "guardrail_repair_failed", "repair_evidence": "guardrail_repair_failed",
            "judge_error": "semantic_judge_error"}[mode]
    assert output.__cause__.code == code
    assert calls["decision"] == (1 if mode == "judge_error" else 2)
    assert calls["judge"] == (2 if mode == "second_reject" else 1)
    assert calls["retrieval"] == calls["close"] == 1 and calls["detail"] == int(mode == "step_limit")
    if mode == "step_limit":
        assert output.partial["agent_steps"] == 4 and output.partial["repair_attempt"] == 0
    elif len(seen) == 2:
        assert seen[1]["repair_attempt"] == 1 and seen[1]["agent_steps"] == 3
        assert "judge_result" not in seen[1]
    if mode == "judge_error":
        assert output.__cause__.__cause__ is original


@pytest.mark.parametrize("corruption", ["repair_on_decision", "repair_without_rejection", "repaired_without_feedback",
                                       "stale_judgment", "final_output", "second_repair"])
def test_continuation_rejects_invalid_judge_repair_positions(monkeypatch, corruption):
    output, _, _, workflow = run_durable(monkeypatch, [FINAL])
    runtime_runner = runner()  # all capabilities forbidden
    graph, config = workflow.graph(), workflow.config("guard-thread")
    saved = graph.get_state(config).values
    data = saved["agent_data"]
    config = workflow.config("corrupt-thread")
    data.pop("proposal")
    data.pop("judge_result")
    data.pop("guardrail_feedback", None)
    as_node = "repair"  # next judge
    if corruption == "repair_on_decision":
        as_node = "retrieve"
        data["repair_attempt"] = 1
    elif corruption == "repair_without_rejection":
        as_node = "judge"
        data["judge_result"] = {"passed": False, "violations": [{"type": "repeated_known_fact",
                    "text": data["candidate_proposal"]["reply"], "reason": "synthetic"}]}
    elif corruption == "repaired_without_feedback":
        data["repair_attempt"] = 1
    elif corruption == "stale_judgment":
        data["judge_result"] = {"passed": True, "violations": []}
    elif corruption == "second_repair":
        as_node = "judge"
        data["repair_attempt"] = 1
        data["judge_result"] = {"passed": False, "violations": [{"type": "repeated_known_fact",
                    "text": data["candidate_proposal"]["reply"], "reason": "synthetic"}]}
    else:
        data["proposal"] = data["candidate_proposal"]
    graph.update_state(config, {"snapshot": saved["snapshot"], "agent_data": data}, as_node=as_node)
    with pytest.raises((RuntimeError, ValueError)):
        workflow.continue_compute("corrupt-thread", runtime_runner)


def test_repair_uses_remaining_total_active_budget(monkeypatch):
    from ticketmind.agent import runtime
    from ticketmind.agent.runtime import RunFailure

    now = [0.0]
    monkeypatch.setattr(runtime, "monotonic", lambda: now[0])
    original = runtime.AgentExecution.decide
    def slow_decide(execution, state):
        now[0] += 11 if state.get("guardrail_feedback") else 80
        return original(execution, state)
    monkeypatch.setattr(runtime.AgentExecution, "decide", slow_decide)
    bad = {"next_step": "escalate", "reason": "human", "reply": "人工一定会处理"}
    rejected = {"violations": [{"type": "unsupported_commitment", "text": bad["reply"], "reason": "unsupported"}]}
    failure, calls, _, workflow = run_durable(monkeypatch, [bad, FINAL], judgments=[rejected])
    assert isinstance(failure, RunFailure) and "proposal" not in failure.partial
    assert failure.__cause__.code == "guardrail_repair_failed"
    assert isinstance(failure.__cause__.__cause__, TimeoutError)
    assert failure.usage["execution_budget"]["observed_elapsed_seconds"] == 91
    assert calls["decision"] == calls["judge"] == calls["retrieval"] == 1
    assert failure.partial["repair_attempt"] == 1 and failure.partial["agent_steps"] == 3
    assert workflow.pending_output("guard-thread") is None


def test_durable_custom_decision_requires_explicit_judge(monkeypatch):
    from ticketmind.agent import runtime
    from ticketmind.agent.runtime import RunFailure

    original = runtime.AgentRunner.__init__
    def without_judge(runner, *args, **kwargs):
        kwargs["judge_fn"] = None
        original(runner, *args, **kwargs)
    monkeypatch.setattr(runtime.AgentRunner, "__init__", without_judge)
    def forbidden(*args, **kwargs):
        pytest.fail("custom decision must not silently add paid Judge calls")
    monkeypatch.setattr(runtime, "judge_proposal", forbidden)
    failure, calls, _, workflow = run_durable(monkeypatch, [FINAL])
    assert isinstance(failure, RunFailure) and failure.__cause__.code == "semantic_judge_error"
    assert calls["decision"] == 1 and calls["judge"] == 0
    assert "proposal" not in failure.partial and workflow.pending_output("guard-thread") is None
