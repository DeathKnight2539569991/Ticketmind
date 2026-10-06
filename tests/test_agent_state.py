from docs_fakes import FakeDocStore, doc_hit
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from ticketmind.agent.decide import decision_messages
from ticketmind.agent.proposals import proposal_adapter
from ticketmind.agent.runtime import AgentRunner, ExecutionBudget, RunFailure
from ticketmind.agent.schemas import AgentMessage, AgentRunInput
from ticketmind.agent.semantic_judge import judge_messages
from ticketmind.agent.state import durable_state, typed_state
from ticketmind.agent.dev_workflow import run_decision_workflow
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.retrieval.dense import RetrievalHit
from ticketmind.retrieval.schemas import EvidenceHit, KnowledgeEvidenceHit


def state():
    return {
        "subject": "API 超时", "messages": [AgentMessage(role="customer", content="Python 3.12，E_TIMEOUT")],
        "clarification_rounds": 0, "tool_calls": [{"tool": "search_cases", "parameters": {"query": "original"},
                                                 "status": "succeeded", "result_source_ids": ["a"]}],
        "retrieval_query": "original", "retrieval_hits": [RetrievalHit(source_id="a", text="case", score=0.5)],
    }


@pytest.mark.parametrize("hit", [
    RetrievalHit(source_id="a", text="case", score=0.5),
    EvidenceHit(source_id="a", text="case", title="title", corpus_version="v1", rank=1,
                retrieval_mode="hybrid", dense_score=0.5, bm25_score=3.2, fusion_score=0.02),
    KnowledgeEvidenceHit(source_id="a", text="case", title="title", corpus_version="v1", rank=1,
                         retrieval_mode="bm25", knowledge_revision=7, content_hash="hash",
                         synthetic=False, metadata={"owner": {"team": ["support"]}}),
])
def test_durable_roundtrip_preserves_types_payloads_and_independent_values(hit):
    original = {**state(), "retrieval_hits": [hit], "docs_hits": [doc_hit()],
                "agent_steps": 4, "search_rounds": 1, "seen_queries": ["original"],
                "seen_docs_queries": ["docs query"], "docs_search_rounds": 1, "repair_attempt": 0,
                "usage": {"decisions": [{"total_tokens": 3}]}, "compute_elapsed_seconds": 2.5,
                "evidence": [{"source_id": "a", "metadata": {"x": [1]}}]}
    proposal = proposal_adapter.validate_python({"next_step": "ask_clarification", "reason": "missing",
                                                 "reply": "当前配置是什么？", "evidence_ids": ["a"]})
    data = durable_state(original)
    restored = typed_state(json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False)))
    assert type(restored["retrieval_hits"][0]) is type(hit)
    assert restored["retrieval_hits"][0] == hit
    assert durable_state(restored) == data
    assert decision_messages(restored) == decision_messages(original)
    assert judge_messages(restored, proposal) == judge_messages(original, proposal)
    from ticketmind.agent.dev_decision_cache import decision_fingerprint
    from ticketmind.agent.dev_acceptance import judge_fingerprint
    settings = QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused")
    assert decision_fingerprint(settings, restored) == decision_fingerprint(settings, original)
    assert judge_fingerprint(settings, restored, proposal) == judge_fingerprint(settings, original, proposal)
    restored["messages"][0].content = "mutated"
    restored["docs_hits"][0].text = "mutated"
    restored["usage"]["decisions"][0]["total_tokens"] = 99
    assert original["messages"][0].content == "Python 3.12，E_TIMEOUT"
    assert original["docs_hits"][0].text == "product documentation"
    assert data["usage"]["decisions"][0]["total_tokens"] == 3


@pytest.mark.parametrize("extra", [
    {"client": object()}, {"timer": lambda: 1}, {"seen_queries": {"q"}},
    {"compute_elapsed_seconds": float("nan")}, {"compute_elapsed_seconds": -1},
    {"compute_elapsed_seconds": True}, {"state_version": True}, {"state_version": 1},
    {"agent_steps": -1}, {"search_rounds": "1"}, {"repair_attempt": 2},
    {"seen_docs_queries": ["a", "a"]}, {"usage": {"tokens": float("inf")}},
])
def test_durable_state_rejects_resources_and_invalid_control_data(extra):
    with pytest.raises(ValueError):
        durable_state({**state(), **extra})


@pytest.mark.parametrize("failure", [False, True])
def test_loop_exports_control_state_and_audit_without_mutating_input(failure):
    original = state()
    before = deepcopy(original)
    audit, snapshots, calls = [], [], []
    decisions = [
        {"next_step": "search_cases", "reason": "search", "query": "E_TIMEOUT Python 3.12"},
        {"next_step": "search_docs", "reason": "docs", "query": "product rules"},
        {"next_step": "ask_clarification", "reason": "missing", "reply": "当前代理配置是什么？"},
    ]
    def decide(current):
        calls.append(durable_state(current))
        # Even a custom adapter's accidental mutation stays in its projection.
        current["messages"][0].content = "adapter changed input"
        return decisions[len(calls) - 1]
    def search(query, record):
        if failure:
            raise RuntimeError("synthetic tool failure")
        return [RetrievalHit(source_id="b", text="second", score=0.6)]
    corpus = SimpleNamespace()
    kwargs = dict(decide=decide, judge=lambda *args: {"violations": []}, corpus=corpus,
                  config=ProcessingSettings(_env_file=None), remaining=lambda: 1,
                  audit=audit, search_fn=search, docs_fn=lambda query, record: [doc_hit()], state_callback=snapshots.append)
    if failure:
        with pytest.raises(RuntimeError, match="synthetic tool"):
            run_decision_workflow(original, **kwargs)
    else:
        proposal, hits = run_decision_workflow(original, **kwargs)
        assert proposal.next_step == "ask_clarification" and [hit.source_id for hit in hits] == ["a", "b"]
    assert original == before
    snapshot = snapshots[0]
    assert snapshot["repair_attempt"] == 0
    assert snapshot["tool_calls"] == audit
    assert snapshot["agent_steps"] == (3 if failure else 6)
    assert snapshot["search_rounds"] == (1 if failure else 2)
    assert snapshot["seen_docs_queries"] == ([] if failure else ["product rules"])
    assert snapshot["docs_search_rounds"] == (0 if failure else 1)
    assert snapshot["seen_queries"] == (["original"] if failure else ["original", "e_timeout python 3.12"])
    assert audit[0]["status"] == ("failed" if failure else "succeeded")
    assert all(call["messages"][0]["content"] == before["messages"][0].content for call in calls)
    assert "proposal" not in snapshot
    if not failure:
        assert snapshot["decision_result"]["next_step"] == "ask_clarification"
        assert snapshot["judge_result"] == {"violations": [], "passed": True}
        audit[0]["status"] = "mutated sink"
        assert snapshot["tool_calls"][0]["status"] == "succeeded"


def test_budget_accumulates_active_scopes_and_excludes_offline_time():
    now = [100.0]
    clock = lambda: now[0]
    first = ExecutionBudget(90, elapsed_seconds=40, clock=clock)
    assert first.remaining() == 30  # per-request cap, not a new total budget
    now[0] += 20
    assert first.stop() == 60
    now[0] += 1000  # review/offline wait is outside the stopped scope
    assert first.elapsed_seconds == 60
    second = ExecutionBudget(90, elapsed_seconds=first.elapsed_seconds, clock=clock)
    now[0] += 15
    assert second.remaining() == 15
    now[0] += 15
    with pytest.raises(TimeoutError):
        second.remaining()


def test_multiple_capabilities_share_one_budget():
    now = [0.0]
    budget = ExecutionBudget(90, clock=lambda: now[0])
    for elapsed in (20, 45, 80):
        now[0] = elapsed
        assert budget.remaining() == min(30, 90 - elapsed)
    now[0] = 91
    with pytest.raises(TimeoutError):
        budget.remaining()


@pytest.mark.parametrize("elapsed", [-1, float("inf"), True, "0"])
def test_budget_rejects_invalid_accumulated_time(elapsed):
    with pytest.raises(ValueError):
        ExecutionBudget(90, elapsed_seconds=elapsed)


@pytest.mark.parametrize("guardrail_failure", [False, True])
def test_real_runner_path_exports_pure_control_and_usage_state(monkeypatch, guardrail_failure):
    from ticketmind.agent import runtime
    monkeypatch.setattr(runtime, "retrieve_cases", lambda *args, **kwargs: [])
    client = SimpleNamespace(close=lambda: None)
    config = ProcessingSettings(_env_file=None, retrieval_mode="bm25")
    decisions = []
    def decision(current, timeout, usage):
        decisions.append(current)
        usage.setdefault("custom", []).append({"tokens": 2})
        current["messages"][0].content = "mutated adapter input"
        return {"next_step": "escalate", "reason": "missing", "reply": "建议人工核查"}
    def judge(current, proposal, timeout, usage):
        return {"violations": [{"type": "unsupported_commitment", "text": proposal.reply,
                                "reason": "synthetic rejection"}]} if guardrail_failure else {"violations": []}
    runner = AgentRunner(
        QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
        MilvusSettings(_env_file=None, uri="http://unused.invalid"), config,
        corpus=SimpleNamespace(evidence=lambda hits: []), milvus_factory=lambda settings: client,
        decision_fn=decision, judge_fn=judge, docs_store=FakeDocStore())
    supplied = AgentRunInput(subject="s", messages=[AgentMessage(role="customer", content="original")])
    if guardrail_failure:
        with pytest.raises(RunFailure) as caught:
            runner(supplied)
        result = caught.value
        exported = result.partial
        assert "proposal" not in exported and exported["repair_attempt"] == 1
        assert len(result.usage["semantic_judge"]) == 2
    else:
        result = runner(supplied)
        exported = result.state
        assert exported["repair_attempt"] == 0 and exported["agent_steps"] == 2
        assert len(result.usage["semantic_judge"]) == 1
    json.dumps(durable_state(exported), allow_nan=False)
    assert exported["messages"][0].content == supplied.messages[0].content == "original"
    assert exported["seen_queries"] and exported["compute_elapsed_seconds"] >= 0
    assert exported["usage"] == result.usage
    assert exported["usage"]["custom"] == [{"tokens": 2}] * len(decisions)
