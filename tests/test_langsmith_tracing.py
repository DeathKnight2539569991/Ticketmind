"""Tracing is optional diagnostics; business graph and state stay deterministic."""
import json

import pytest
from pydantic import SecretStr

from test_agent_durable import DETAIL, FINAL, SEARCH, run_durable
from ticketmind.agent import review, tracing
from ticketmind.core.config import TraceSettings


class Sink:
    def __init__(self):
        self.events = []
        self.outcomes = []

    def event(self, name, *, node, **kwargs):
        self.events.append((name, node, kwargs))

    def finish(self, **kwargs):
        self.outcomes.append(kwargs)


def test_disabled_or_missing_key_never_calls_sdk(monkeypatch):
    monkeypatch.setattr(tracing, "_submit", lambda *args: pytest.fail("SDK must not be called"))
    snapshot = {"run_id": "run"}
    for enabled, key in ((False, SecretStr("secret")), (True, None)):
        trace = tracing.Trace(TraceSettings(_env_file=None, enabled=enabled, api_key=key),
                              snapshot=snapshot, thread_id="thread", phase="start")
        trace.event("ignored", node="decision")
        trace.finish(outcome="computed")
        assert not trace.enabled


def test_langsmith_run_hierarchy_and_safe_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(tracing, "_submit", lambda *args: calls.append(args))
    trace = tracing.Trace(TraceSettings(_env_file=None, enabled=True, api_key=SecretStr("key")),
                          snapshot={"run_id": "business-id"}, thread_id="thread-1", phase="start",
                          metadata={"agent_version": "v2", "docs_catalog_hash": "hash"},
                          checkpoint_id="checkpoint-1", attempt=2)
    trace.event("ticketmind.tool.search_docs", node="search_docs",
                outputs={"candidates": [{"source_id": "docs:1"}]})
    trace.finish(outcome="waiting_review")
    creates = [args[3] for args in calls if args[2] == "create"]
    assert len(creates) == 3
    root, attempt, child = creates
    assert root["id"] == root["trace_id"] == attempt["trace_id"] == child["trace_id"]
    assert attempt["parent_run_id"] == root["id"]
    assert child["parent_run_id"] == attempt["id"]
    assert attempt["dotted_order"].startswith(root["dotted_order"] + ".")
    assert child["dotted_order"].startswith(attempt["dotted_order"] + ".")
    assert child["extra"]["metadata"]["checkpoint_id"] == "checkpoint-1"
    assert child["extra"]["metadata"]["business_run_id"] == "business-id"
    assert "key" not in repr([args[3] for args in calls])
    assert calls[-1][3]["outputs"] == {"outcome": "waiting_review"}


def test_full_path_records_actual_candidates_rejection_and_review(monkeypatch):
    sink = Sink()
    monkeypatch.setattr(review, "trace_for", lambda *args, **kwargs: sink)
    output, _, _, workflow = run_durable(monkeypatch,
        [SEARCH, {**SEARCH, "query": SEARCH["query"]}, DETAIL, FINAL])
    assert output.state["proposal"].next_step == "ask_clarification"
    tools = [(name, data["outputs"]) for name, _, data in sink.events if name.startswith("ticketmind.tool.")]
    assert tools[0][1]["candidates"][0]["source_id"] == "case-1"
    assert any(item["record"]["status"] == "rejected" and item["record"]["error"] == "duplicate_query"
               and item["candidates"] == [] for _, item in tools)
    assert any(item["candidates"][0]["source_id"].startswith("docs:") for _, item in tools
               if item["record"]["tool"] == "search_docs")
    assert any(name == "ticketmind.model.decision" for name, _, _ in sink.events)
    assert any(name == "ticketmind.model.judge" for name, _, _ in sink.events)
    model_inputs = [data["inputs"] for name, _, data in sink.events
                    if name == "ticketmind.model.decision"]
    assert model_inputs[0]["subject"] == "API 超时"
    assert model_inputs[-1]["tool_calls"][-1]["status"] == "succeeded"
    assert model_inputs[-1]["docs"][0]["source_id"].startswith("docs:")
    assert any(name == "ticketmind.review.interrupt" for name, _, _ in sink.events)
    assert sink.outcomes == [{"outcome": "waiting_review", "error": None}]
    saved = workflow.graph().get_state(workflow.config("guard-thread")).values
    json.dumps(saved, allow_nan=False)
    assert "trace" not in repr(saved).lower()


def test_judge_failure_repair_and_budget_fallback(monkeypatch):
    sink = Sink()
    monkeypatch.setattr(review, "trace_for", lambda *args, **kwargs: sink)
    bad = {"next_step": "escalate", "reason": "needs human", "reply": "人工一定会处理"}
    rejected = {"violations": [{"type": "unsupported_commitment",
                                "text": bad["reply"], "reason": "unsupported"}]}
    run_durable(monkeypatch, [bad, FINAL], judgments=[rejected, {"violations": []}])
    judges = [event for event in sink.events if event[0] == "ticketmind.model.judge"]
    assert [event[2]["outputs"]["judgment"]["passed"] for event in judges] == [False, True]
    assert any(event[0] == "ticketmind.repair" for event in sink.events)
    sink.events.clear()
    run_durable(monkeypatch, [SEARCH], limits={"max_agent_steps": 3})
    assert any(event[0] == "ticketmind.budget.fallback" for event in sink.events)
    assert sum(event[0] == "ticketmind.model.judge" for event in sink.events) == 0
    sink.events.clear()
    run_durable(monkeypatch, [DETAIL, bad], limits={"max_agent_steps": 4}, judgments=[rejected])
    assert any(event[0] == "ticketmind.budget.fallback" and event[1] == "repair" for event in sink.events)
    assert sum(event[0] == "ticketmind.model.decision" for event in sink.events) == 2
    assert sum(event[0] == "ticketmind.model.judge" for event in sink.events) == 1


def test_trace_sink_failures_never_change_proposal_or_business_failure(monkeypatch):
    class Broken:
        def event(self, *args, **kwargs):
            raise RuntimeError("trace create/update failed")
        def finish(self, *args, **kwargs):
            raise RuntimeError("trace end failed")
    monkeypatch.setattr(review, "trace_for", lambda *args, **kwargs: Broken())
    output, _, _, _ = run_durable(monkeypatch, [FINAL])
    assert output.state["proposal"].next_step == "ask_clarification"
    failure, _, _, _ = run_durable(monkeypatch, [{"next_step": "execute_shell"}])
    from ticketmind.agent.runtime import RunFailure
    assert isinstance(failure, RunFailure)


def test_concurrent_trace_instances_have_distinct_hierarchy(monkeypatch):
    calls = []
    monkeypatch.setattr(tracing, "_submit", lambda *args: calls.append(args))
    settings = TraceSettings(_env_file=None, enabled=True, api_key=SecretStr("key"))
    first = tracing.Trace(settings, snapshot={"run_id": "one"}, thread_id="one", phase="start")
    second = tracing.Trace(settings, snapshot={"run_id": "two"}, thread_id="two", phase="start")
    first.event("decision", node="decision")
    second.event("judge", node="judge")
    creates = [args[3] for args in calls if args[2] == "create"]
    assert creates[4]["parent_run_id"] == first.attempt_id
    assert creates[5]["parent_run_id"] == second.attempt_id
    assert creates[4]["trace_id"] == first.root_id
    assert creates[5]["trace_id"] == second.root_id


def test_review_resume_is_correlated_without_recomputing(monkeypatch):
    invocations = []
    def make_trace(snapshot, thread_id, phase, **kwargs):
        sink = Sink()
        invocations.append((snapshot["run_id"], thread_id, phase, sink))
        return sink
    monkeypatch.setattr(review, "trace_for", make_trace)
    output, calls, _, workflow = run_durable(monkeypatch, [FINAL])
    workflow.resume("guard-thread", {"decision": "approve"})
    assert output.state["proposal"].next_step == "ask_clarification"
    assert calls["decision"] == calls["judge"] == 1
    assert [(run, thread, phase) for run, thread, phase, _ in invocations] == [
        ("run", "guard-thread", "start"), ("run", "guard-thread", "review_resume")]
    assert any(name == "ticketmind.review.resumed" for name, _, _ in invocations[1][3].events)


@pytest.mark.parametrize("failure", ["init", "create", "update"])
def test_sdk_failures_are_contained_by_background_worker(monkeypatch, failure):
    import langsmith
    class BrokenClient:
        def __init__(self, **kwargs):
            if failure == "init":
                raise RuntimeError("init failed")
        def create_run(self, **kwargs):
            if failure == "create":
                raise RuntimeError("start failed")
        def update_run(self, **kwargs):
            if failure == "update":
                raise RuntimeError("end failed")
    monkeypatch.setattr(langsmith, "Client", BrokenClient)
    tracing._client = None
    tracing._client_key = None
    trace = tracing.Trace(
        TraceSettings(_env_file=None, enabled=True, api_key=SecretStr("key")),
        snapshot={"run_id": "run"}, thread_id="thread", phase="start")
    trace.event("result", node="decision", outputs={"decision": {"next_step": "escalate"}})
    trace.finish(outcome="computed")
    tracing._queue.join()
    assert tracing._queue.empty()
    tracing._client = None
    tracing._client_key = None


def test_unserializable_trace_result_is_dropped_without_error(monkeypatch):
    calls = []
    monkeypatch.setattr(tracing, "_submit", lambda *args: calls.append(args))
    trace = tracing.Trace(
        TraceSettings(_env_file=None, enabled=True, api_key=SecretStr("key")),
        snapshot={"run_id": "run"}, thread_id="thread", phase="start")
    trace.event("result", node="decision", outputs={"client": object()})
    trace.finish(outcome="computed")
    assert len([args for args in calls if args[2] == "create"]) == 2


def test_common_credentials_in_model_inputs_are_redacted(monkeypatch):
    calls = []
    monkeypatch.setattr(tracing, "_submit", lambda *args: calls.append(args))
    trace = tracing.Trace(
        TraceSettings(_env_file=None, enabled=True, api_key=SecretStr("key")),
        snapshot={"run_id": "run"}, thread_id="thread", phase="start")
    trace.event("decision", node="decision",
                inputs={"subject": "API_KEY=private123 Bearer private456"})
    event = [args[3] for args in calls if args[2] == "create"][-1]
    assert "private123" not in repr(event)
    assert "private456" not in repr(event)
