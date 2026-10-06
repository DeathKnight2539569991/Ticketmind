"""Real PostgreSQL boundaries; deterministic capabilities and no paid calls."""
from docs_fakes import FakeDocStore, doc_hit
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import test_m1_api as m1
from ticketmind.agent import runtime
from ticketmind.agent.review import ReviewWorkflow
from ticketmind.agent.runtime import AgentRunner, RunFailure
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.db.checkpoints import checkpoint_resources

pytestmark = m1.pytestmark
database = m1.database


def snapshot():
    return {"run_id": uuid4().hex, "subject": "synthetic subject", "clarification_rounds": 0,
            "messages": [{"author_type": "customer", "body": "synthetic body"}]}


def test_retrieve_checkpoint_survives_new_saver_runtime_and_pool(database, monkeypatch):
    engine, _, _ = database
    now, calls, timeouts = [0.0], {"retrieve": 0, "decision": 0, "judge": 0, "close": 0}, []
    monkeypatch.setattr(runtime, "monotonic", lambda: now[0])
    def retrieve(*args, **kwargs):
        calls["retrieve"] += 1
        now[0] += 80
        return []
    monkeypatch.setattr(runtime, "retrieve_cases", retrieve)
    def close():
        calls["close"] += 1
        # Initial cleanup diagnostics must be in the successful retrieve checkpoint.
        if calls["close"] == 1:
            raise RuntimeError("synthetic close failure")
    def decide(state, timeout, usage):
        calls["decision"] += 1
        timeouts.append(timeout)
        usage.setdefault("decisions", []).append({"total_tokens": 3})
        now[0] += 2
        if calls["decision"] == 1:
            raise RuntimeError("synthetic interrupted decision")
        return {"next_step": "escalate", "reason": "missing facts", "reply": "请人工核查"}
    def judge(*args):
        calls["judge"] += 1
        return {"violations": []}
    def runner():
        return AgentRunner(
            QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
            MilvusSettings(_env_file=None, uri="http://unused.invalid"),
            ProcessingSettings(_env_file=None, retrieval_mode="bm25", processing_timeout_seconds=90),
            corpus=SimpleNamespace(evidence=lambda hits: []), milvus_factory=lambda _: SimpleNamespace(close=close),
            decision_fn=decide, judge_fn=judge, docs_store=FakeDocStore())
    thread = uuid4().hex
    with checkpoint_resources(engine) as saver:
        workflow = ReviewWorkflow(saver)
        with pytest.raises(RunFailure) as caught:
            workflow.start(snapshot(), thread, runner())
        assert caught.value.usage["execution_budget"]["observed_elapsed_seconds"] == 82
        state = workflow.graph().get_state(workflow.config(thread))
        assert state.next == ("decision",)
        data = state.values["agent_data"]
        json.dumps(state.values, allow_nan=False)
        assert data["compute_elapsed_seconds"] == 80
        assert len(data["tool_calls"]) == 1
        assert len(data["usage"]["cleanup_errors"]) == 1
        assert "decisions" not in data["usage"]
    now[0] += 10000  # pool/workflow/runtime recreated; offline elapsed is excluded
    with checkpoint_resources(engine) as saver:
        workflow = ReviewWorkflow(saver)
        output = workflow.continue_compute(thread, runner())
        data = workflow.graph().get_state(workflow.config(thread)).values["agent_data"]
        assert data["compute_elapsed_seconds"] == 84
        assert data["agent_steps"] == 2 and data["search_rounds"] == 1
        assert output.usage["decisions"] == [{"total_tokens": 3}]
        assert len(output.usage["cleanup_errors"]) == 1
        assert len(output.state["tool_calls"]) == 1
        json.dumps(data, allow_nan=False)
    assert calls == {"retrieve": 1, "decision": 2, "judge": 1, "close": 1}
    assert timeouts == [10, 8]
    with checkpoint_resources(engine) as saver:
        workflow = ReviewWorkflow(saver)
        assert workflow.pending_output(thread).state["proposal"] == output.state["proposal"]
        workflow.resume(thread, {"decision": "approve"})
        workflow.resume(thread, {"decision": "approve"})
    assert calls["decision"] == 2 and calls["judge"] == 1


@pytest.mark.parametrize("tool", ["search_cases", "search_docs"])
def test_successful_tool_checkpoint_is_not_repeated_after_failed_decision(database, monkeypatch, tool):
    from ticketmind.retrieval.dense import RetrievalHit

    engine, _, _ = database
    calls = {"retrieve": 0, "search_cases": 0, "search_docs": 0, "decision": 0, "judge": 0, "close": 0}
    seen = []
    hit = RetrievalHit(source_id="case-1", text="synthetic case", score=0.5)
    def search(query, **kwargs):
        key = "retrieve" if calls["retrieve"] == 0 else "search_cases"
        calls[key] += 1
        kwargs["record"]["result_hits"] = [{"source_id": hit.source_id}]
        return [hit]
    monkeypatch.setattr(runtime, "retrieve_cases", search)
    def detail(query, **kwargs):
        calls["search_docs"] += 1
        return [doc_hit("full case")]
    monkeypatch.setattr(runtime, "retrieve_docs", detail)
    def close():
        calls["close"] += 1
    def decide(state, timeout, usage):
        calls["decision"] += 1
        seen.append(state)
        usage.setdefault("decisions", []).append({"total_tokens": 7})
        if calls["decision"] == 1:
            return {"next_step": tool, "reason": "inspect facts", **(
                {"query": "additional customer facts"} if tool == "search_cases" else {"query": "additional customer facts"})}
        if calls["decision"] == 2:
            raise RuntimeError("unfinished decision")
        return {"next_step": "propose_resolution", "reason": "facts checked", "reply": "请核对配置", "evidence_ids": [hit.source_id]}
    def judge(*args):
        calls["judge"] += 1
        return {"violations": []}
    def runner():
        return AgentRunner(
            QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
            MilvusSettings(_env_file=None, uri="http://unused.invalid"),
            ProcessingSettings(_env_file=None, retrieval_mode="bm25"),
            corpus=SimpleNamespace(evidence=lambda hits: [{"source_id": h.source_id} for h in hits]),
            milvus_factory=lambda _: SimpleNamespace(close=close), decision_fn=decide, judge_fn=judge, docs_store=FakeDocStore())
    thread = uuid4().hex
    with checkpoint_resources(engine) as saver:
        workflow = ReviewWorkflow(saver)
        with pytest.raises(RunFailure) as caught:
            workflow.start(snapshot(), thread, runner())
        assert caught.value.stage == "decision"
        state = workflow.graph().get_state(workflow.config(thread))
        assert state.next == ("decision",)
        data = state.values["agent_data"]
        json.dumps(data, allow_nan=False)
        assert data["agent_steps"] == 3 and len(data["tool_calls"]) == 2
        assert all(record["status"] == "succeeded" for record in data["tool_calls"])
        assert data["usage"]["decisions"] == [{"total_tokens": 7}]
        assert data["search_rounds"] == (2 if tool == "search_cases" else 1)
        if tool == "search_docs":
            assert data["seen_docs_queries"] == ["additional customer facts"]
            assert data["docs_hits"][0]["text"] == "full case"
            assert data["docs_search_rounds"] == 1
        saved_audit = data["tool_calls"]
        decision_boundary = next(item for item in workflow.graph().get_state_history(workflow.config(thread))
                                 if item.next == (tool,))
        decision_data = decision_boundary.values["agent_data"]
        assert decision_data["decision_result"]["next_step"] == tool
        assert decision_data["agent_steps"] == 2 and len(decision_data["tool_calls"]) == 1
        assert decision_data["search_rounds"] == 1 and decision_data["seen_docs_queries"] == []
        assert decision_data["usage"]["decisions"] == [{"total_tokens": 7}]
    # Explicit continuation with all runtime, workflow, saver and pool objects replaced.
    with checkpoint_resources(engine) as saver:
        workflow = ReviewWorkflow(saver)
        output = workflow.continue_compute(thread, runner())
        data = workflow.graph().get_state(workflow.config(thread)).values["agent_data"]
        assert data["agent_steps"] == 4
        assert output.state["tool_calls"] == saved_audit
        assert output.usage["decisions"] == [{"total_tokens": 7}, {"total_tokens": 7}]
        assert len(output.usage["semantic_judge"]) == 1
        assert data["seen_queries"] == ([data["retrieval_query"].strip().casefold(), "additional customer facts"]
                                        if tool == "search_cases" else [data["retrieval_query"].strip().casefold()])
        workflow.resume(thread, {"decision": "approve"})
        workflow.resume(thread, {"decision": "approve"})
    assert calls == {"retrieve": 1, "search_cases": int(tool == "search_cases"),
                     "search_docs": int(tool == "search_docs"), "decision": 3, "judge": 1,
                     "close": 2}
    assert [state["agent_steps"] for state in seen] == [2, 4, 4]
    # Both failed and retried turns receive the exact completed-tool state.
    for key in ("retrieval_hits", "docs_hits", "tool_calls", "seen_queries", "seen_docs_queries"):
        assert seen[1][key] == seen[2][key]


@pytest.mark.parametrize("boundary", ["first_judge", "repair", "second_judge"])
def test_judge_and_repair_checkpoint_survive_rebuilt_runtime(database, monkeypatch, boundary):
    """Interrupt outside run_node: Judge errors themselves remain fail closed."""
    engine, _, _ = database
    calls = {"retrieve": 0, "decision": 0, "judge": 0, "close": 0}
    now, seen, timeouts = [0.0], [], []
    monkeypatch.setattr(runtime, "monotonic", lambda: now[0])
    bad = {"next_step": "escalate", "reason": "human", "reply": "人工一定会处理"}
    good = {"next_step": "ask_clarification", "reason": "missing facts", "reply": "请补充配置"}
    def retrieve(*args, **kwargs):
        calls["retrieve"] += 1
        now[0] += 70
        return []
    monkeypatch.setattr(runtime, "retrieve_cases", retrieve)
    def close():
        calls["close"] += 1
    def decision(state, timeout, usage):
        calls["decision"] += 1
        seen.append(state)
        timeouts.append(timeout)
        usage.setdefault("decisions", []).append({"total_tokens": 7})
        now[0] += 2
        return good if boundary == "first_judge" or state.get("guardrail_feedback") else bad
    def judge(state, proposal, timeout, usage):
        calls["judge"] += 1
        timeouts.append(timeout)
        now[0] += 3
        if proposal.reply == bad["reply"]:
            return {"violations": [{"type": "unsupported_commitment", "text": proposal.reply, "reason": "unsupported"}]}
        return {"violations": []}
    def runner():
        return AgentRunner(
            QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
            MilvusSettings(_env_file=None, uri="http://unused.invalid"),
            ProcessingSettings(_env_file=None, retrieval_mode="bm25", processing_timeout_seconds=90),
            corpus=SimpleNamespace(evidence=lambda hits: []), milvus_factory=lambda _: SimpleNamespace(close=close),
            decision_fn=decision, judge_fn=judge, docs_store=FakeDocStore())
    run_node = runtime.AgentExecution.run_node
    interrupted = [False]
    interruption = RuntimeError("synthetic process interruption outside capability")
    def interrupt_boundary(execution, node):
        data = run_node(execution, node)
        should_interrupt = ((boundary == "first_judge" and node == "judge") or
                            (boundary == "repair" and node == "repair") or
                            (boundary == "second_judge" and node == "judge" and data["repair_attempt"] == 1))
        if should_interrupt and not interrupted[0]:
            interrupted[0] = True
            raise interruption
        return data
    monkeypatch.setattr(runtime.AgentExecution, "run_node", interrupt_boundary)
    thread = uuid4().hex
    with checkpoint_resources(engine) as saver:
        workflow = ReviewWorkflow(saver)
        with pytest.raises(RunFailure) as caught:
            workflow.start(snapshot(), thread, runner())
        assert caught.value.__cause__ is interruption
        state = workflow.graph().get_state(workflow.config(thread))
        assert state.next == (("repair",) if boundary == "repair" else ("judge",))
        data = state.values["agent_data"]
        assert "proposal" not in data and "output" not in state.values
        assert data["agent_steps"] == (3 if boundary == "second_judge" else 2)
        assert data["repair_attempt"] == int(boundary == "second_judge")
        assert len(data["usage"]["decisions"]) == (2 if boundary == "second_judge" else 1)
        if boundary == "first_judge":
            assert "semantic_judge" not in data["usage"] and "candidate_proposal" not in data
        else:
            assert len(data["usage"]["semantic_judge"]) == 1
            assert data["usage"]["semantic_judge"][0]["status"] == "rejected"
            if boundary == "repair":
                assert data["judge_result"]["passed"] is False
                assert data["candidate_proposal"]["reply"] == bad["reply"]
            else:
                assert "judge_result" not in data
                assert data["candidate_proposal"]["reply"] == good["reply"]
        saved_audit = data["tool_calls"]
        json.dumps(state.values, allow_nan=False)
    now[0] += 10000
    with checkpoint_resources(engine) as saver:
        workflow = ReviewWorkflow(saver)
        output = workflow.continue_compute(thread, runner())
        state = workflow.graph().get_state(workflow.config(thread))
        data = state.values["agent_data"]
        assert state.next == ("review",) and any(task.interrupts for task in state.tasks)
        assert data["agent_steps"] == (2 if boundary == "first_judge" else 3)
        assert data["repair_attempt"] == int(boundary != "first_judge")
        assert data["compute_elapsed_seconds"] == {"first_judge": 78, "repair": 82, "second_judge": 83}[boundary]
        assert len(output.usage["decisions"]) == (1 if boundary == "first_judge" else 2)
        assert len(output.usage["semantic_judge"]) == (1 if boundary == "first_judge" else 2)
        assert data["judge_result"]["passed"] is True
        assert output.state["proposal"].reply == good["reply"]
        assert output.state["tool_calls"] == saved_audit
    completed_calls = dict(calls)
    with checkpoint_resources(engine) as saver:
        workflow = ReviewWorkflow(saver)
        assert workflow.pending_output(thread).state["proposal"] == output.state["proposal"]
        workflow.resume(thread, {"decision": "approve"})
        workflow.resume(thread, {"decision": "approve"})
    assert calls == completed_calls  # E: review continuation/replay adds zero model calls.
    assert calls == {"retrieve": 1, "decision": 3 if boundary == "repair" else 1 if boundary == "first_judge" else 2,
                     "judge": 2 if boundary != "second_judge" else 3, "close": 1}
    assert all(timeout <= 20 for timeout in timeouts)
    assert sum(bool(state.get("guardrail_feedback")) for state in seen) == (2 if boundary == "repair" else int(boundary == "second_judge"))
    assert all(state["repair_attempt"] == 1 and "judge_result" not in state
               for state in seen if state.get("guardrail_feedback"))
