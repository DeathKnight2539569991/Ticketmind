"""One compiled topology with invocation-scoped resources and failure sinks."""
from docs_fakes import FakeDocStore, doc_hit
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ticketmind.agent.review import ReviewWorkflow
from ticketmind.agent.runtime import AgentRunner, RunFailure
from ticketmind.agent.schemas import AgentRunInput
from ticketmind.core.config import QwenSettings, MilvusSettings, ProcessingSettings


def runtime(monkeypatch, decision):
    from ticketmind.agent import runtime as module
    monkeypatch.setattr(module, "retrieve_cases", lambda *args, **kwargs: [])
    return AgentRunner(
        QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
        MilvusSettings(_env_file=None, uri="http://unused.invalid"),
        ProcessingSettings(_env_file=None, retrieval_mode="bm25"),
        corpus=SimpleNamespace(evidence=lambda hits: []),
        milvus_factory=lambda settings: SimpleNamespace(close=lambda: None),
        decision_fn=decision, judge_fn=lambda *args: {"violations": []}, docs_store=FakeDocStore())


def snapshot(subject):
    return {"run_id": subject, "subject": subject,
            "messages": [{"author_type": "customer", "body": "details"}]}


def test_compiled_graph_is_shared_but_runtime_and_failures_are_isolated(monkeypatch):
    workflow = ReviewWorkflow(InMemorySaver())
    graph = workflow.graph()
    barrier = Barrier(2)
    calls = []
    def good(state, timeout, usage):
        calls.append(state["subject"])
        barrier.wait(timeout=10)
        return {"next_step": "ask_clarification", "reason": "missing", "reply": state["subject"]}
    def bad(state, timeout, usage):
        calls.append(state["subject"])
        barrier.wait(timeout=10)
        raise ValueError("deterministic adapter failure")
    good_runner, bad_runner = runtime(monkeypatch, good), runtime(monkeypatch, bad)
    with ThreadPoolExecutor(max_workers=2) as pool:
        successful = pool.submit(workflow.start, snapshot("good"), "good", good_runner)
        failed = pool.submit(workflow.start, snapshot("bad"), "bad", bad_runner)
        assert successful.result(timeout=20).state["proposal"].reply == "good"
        with pytest.raises(RunFailure):
            failed.result(timeout=20)
    assert workflow.graph() is graph
    assert sorted(calls) == ["bad", "good"]
    for thread in ("good", "bad"):
        history = list(graph.get_state_history(workflow.config(thread)))
        for checkpoint in history:
            json.dumps(checkpoint.values, allow_nan=False)
            json.dumps(checkpoint.metadata, allow_nan=False)
            assert "runner" not in checkpoint.values and "context" not in checkpoint.values
    assert workflow.pending_output("good") is not None
    assert workflow.pending_output("bad") is None
    assert graph.get_state(workflow.config("bad")).values["failure_descriptor"]["fatal"]
    approved = {"decision": "approve"}
    workflow.resume("good", approved)
    workflow.resume("good", approved)
    assert sorted(calls) == ["bad", "good"]


def test_direct_runner_reuses_the_same_topology_without_state_leak(monkeypatch):
    calls = []
    def decision(state, timeout, usage):
        calls.append(state["subject"])
        assert state["agent_steps"] == 2
        return {"next_step": "ask_clarification", "reason": "missing", "reply": state["subject"]}
    runner = runtime(monkeypatch, decision)
    first = runner(AgentRunInput(subject="first", messages=[{"role": "customer", "content": "details"}]))
    graph = runner._workflow.graph()
    second = runner(AgentRunInput(subject="second", messages=[{"role": "customer", "content": "details"}]))
    assert runner._workflow.graph() is graph
    assert first.state["proposal"].reply == "first"
    assert second.state["proposal"].reply == "second"
    assert calls == ["first", "second"]
    assert "compute" not in graph.nodes
    assert set(graph.nodes) == {"__start__", "bootstrap_retrieve", "decision", "search_cases", "search_docs",
                                "judge", "repair", "review", "resume_gate"}


def test_standalone_compute_rejects_durable_workflow_before_execution():
    workflow = ReviewWorkflow(InMemorySaver())
    with pytest.raises(ValueError, match="without a checkpointer"):
        workflow.compute(AgentRunInput(subject="subject", messages=[{"role": "customer", "content": "details"}]), object())


def test_callable_failure_never_becomes_recoverable_compute(monkeypatch):
    workflow = ReviewWorkflow(InMemorySaver())
    calls = []
    def adapter(*args, **kwargs):
        calls.append("called")
        raise RuntimeError("adapter unavailable")
    frozen = snapshot("adapter")
    with pytest.raises(RuntimeError, match="adapter unavailable"):
        workflow.start(frozen, "adapter", adapter)
    assert workflow.recovery_kind("adapter", frozen) == "legacy_compute"
    with pytest.raises(RuntimeError, match="cannot resume compute"):
        workflow.inspect_compute("adapter")
    assert workflow.pending_output("adapter") is None
    capabilities = []
    runner = runtime(monkeypatch, lambda *args: capabilities.append("decision"))
    def forbidden(*args, **kwargs):
        capabilities.append("execution")
        pytest.fail("callable checkpoint must reject before runtime capability")
    monkeypatch.setattr(runner, "new_execution", forbidden)
    with pytest.raises(RuntimeError, match="cannot resume compute"):
        workflow.continue_compute("adapter", runner)
    assert capabilities == [] and calls == ["called"]
