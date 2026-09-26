import pytest

from ticketmind.agent import runtime
from ticketmind.agent.dev_decision_cache import CachedDecision
from ticketmind.agent.proposals import Clarification
from ticketmind.agent.runtime import AgentRunner, RunFailure
from ticketmind.agent.schemas import AgentMessage, AgentRunInput
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.sources import load_sources


@pytest.fixture
def settings():
    return QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unit-only", DASHSCOPE_WORKSPACE_ID="unit-only")


def run_input(subject="s", content="b"):
    return AgentRunInput(subject=subject, messages=[AgentMessage(role="customer", content=content)])


def proposal():
    return Clarification(next_step="ask_clarification", reason="missing information",
                         reply="unit question", evidence_ids=["SYN-HIST-V2-007"])


@pytest.mark.parametrize("failure", [None, "embedding", "decision", "source"])
def test_real_graph_orchestration_and_partial_failure(monkeypatch, settings, failure):
    # This unit test exercises the original frozen synthetic fixture, not the
    # optional live-E2E dataset selected in the developer's shell or .env.
    from pathlib import Path
    fixture_path = Path(__file__).resolve().parents[1] / "data/synthetic/v2/historical_cases.jsonl"
    config = ProcessingSettings(
        _env_file=None, corpus_path=fixture_path, retrieval_mode="dense",
        decision_model="glm-5.3", judge_model="deepseek-v4.1-flash",
    )
    corpus = load_sources(config.corpus_path)
    case = corpus.cases["SYN-HIST-V2-007"]

    class Client:
        closed = False

        def search(self, **kwargs):
            assert kwargs["timeout"] > 0
            return [[{"entity": {"source_id": case.source_id,
                                  "text": "wrong corpus text" if failure == "source" else build_case_text(case)},
                      "distance": 0.6}]]

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(runtime, "build_milvus_client", lambda settings: client)

    class Embedding:
        def embed_query(self, text):
            if failure == "embedding":
                raise RuntimeError("synthetic embedding failure")
            return [1.0] * 1024

    calls = []

    def decide(state, timeout, usage):
        calls.append("decision")
        if failure == "decision":
            raise RuntimeError("synthetic decision failure")
        return proposal()

    runner = AgentRunner(settings, MilvusSettings(_env_file=None, uri="http://unit.invalid"), config,
        embedding_factory=lambda remaining: Embedding(), decision_fn=decide, corpus=corpus,
        judge_fn=lambda *args: {"passed": True, "violations": []})
    if failure:
        with pytest.raises(RunFailure) as error:
            runner(run_input())
        assert "understanding" not in error.value.partial
        # M3 validates source contents inside retrieval, before any decision is possible.
        assert error.value.stage == {"embedding": "retrieval", "decision": "decision", "source": "retrieval"}[failure]
        if failure == "decision":
            assert error.value.evidence[0]["source_id"] == case.source_id
        else:
            assert not calls
    else:
        result = runner(run_input())
        assert result.state["proposal"] == proposal()
        assert result.evidence[0]["corpus_version"] == runner.metadata["corpus_version"]
    assert client.closed


def test_decision_cache_is_bound_to_actual_request(tmp_path, settings, monkeypatch):
    from ticketmind.agent import dev_decision_cache
    from ticketmind.retrieval.dense import RetrievalHit
    state = {"subject": "s", "messages": [AgentMessage(role="customer", content="b")],
             "clarification_rounds": 0, "tool_calls": [],
             "retrieval_hits": [RetrievalHit(source_id="SYN-HIST-V2-007", text="unit evidence", score=0.5)]}
    monkeypatch.setattr(dev_decision_cache, "decide_ticket", lambda *args, **kwargs: proposal())
    cache = CachedDecision(settings, tmp_path / "decision.json", allow_call=True)
    assert cache(state, 1, {}).next_step == "ask_clarification"
    assert cache(state, 1, {}).next_step == "ask_clarification"
    assert cache.calls == cache.cache_hits == 1
    with pytest.raises(ValueError, match="不匹配"):
        cache({**state, "messages": [AgentMessage(role="customer", content="different body")]}, 1, {})
    assert cache.calls == 1


@pytest.mark.parametrize("decision_fails", [False, True])
def test_cleanup_failure_preserves_result_or_original_failure(monkeypatch, settings, caplog, decision_fails):
    from pathlib import Path
    from ticketmind.retrieval.dense import RetrievalHit

    monkeypatch.setattr(runtime.logger, "disabled", False)
    monkeypatch.setattr(runtime.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(runtime.logger, "propagate", False)
    corpus = load_sources(Path(__file__).resolve().parents[1] / "data/synthetic/v2/historical_cases.jsonl")
    config = ProcessingSettings(_env_file=None, retrieval_mode="bm25")
    original = RuntimeError("synthetic original failure")
    close_calls = []
    case = corpus.cases["SYN-HIST-V2-007"]
    hit = RetrievalHit(source_id=case.source_id, text=build_case_text(case), score=0.5)

    class Client:
        def close(self):
            close_calls.append(True)
            raise RuntimeError("SYNTHETIC_PRIVATE_CLOSE_MESSAGE")

    monkeypatch.setattr(runtime, "retrieve_cases", lambda *args, **kwargs: [hit])

    def decide(state, timeout, usage):
        usage["decisions"] = [{"total_tokens": 3}]
        if decision_fails:
            raise original
        return Clarification(next_step="ask_clarification", reason="missing facts", reply="Please clarify")

    runner = AgentRunner(settings, MilvusSettings(_env_file=None, uri="http://unit.invalid"), config,
                         corpus=corpus, milvus_factory=lambda _: Client(), decision_fn=decide,
                         judge_fn=lambda *args: {"violations": []})
    if decision_fails:
        with pytest.raises(RunFailure) as caught:
            runner(run_input())
        result = caught.value
        assert result.__cause__ is original and result.stage == "decision"
        assert result.partial["tool_calls"][0]["status"] == "succeeded"
    else:
        result = runner(run_input())
        assert result.state["proposal"].next_step == "ask_clarification"
        assert result.usage["semantic_judge"][0]["status"] == "passed"
    assert close_calls == [True]
    assert result.evidence == corpus.evidence([hit])
    assert result.usage["decisions"] == [{"total_tokens": 3}]
    assert result.usage["cleanup_errors"] == [
        {"resource": "milvus", "code": "milvus_close_failed", "error_type": "RuntimeError"}]
    assert "SYNTHETIC_PRIVATE_CLOSE_MESSAGE" not in caplog.text
    assert "agent_cleanup_failed resource=milvus error=RuntimeError" in caplog.text
