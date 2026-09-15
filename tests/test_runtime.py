import pytest

from ticketmind.agent import runtime
from ticketmind.agent.dev_decision_cache import CachedDecision
from ticketmind.agent.proposals import Clarification
from ticketmind.agent.runtime import AgentRunner, RunFailure
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.sources import load_sources


@pytest.fixture
def settings():
    return QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unit-only", DASHSCOPE_WORKSPACE_ID="unit-only")


def proposal():
    return Clarification(next_step="ask_clarification", reason="missing information", reply="unit reply",
                          questions=["unit question"], evidence_ids=["SYN-HIST-V2-007"])


@pytest.mark.parametrize("failure", [None, "embedding", "decision", "source"])
def test_real_graph_orchestration_and_partial_failure(monkeypatch, settings, failure):
    corpus = load_sources(ProcessingSettings().corpus_path)
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

    runner = AgentRunner(settings, MilvusSettings(_env_file=None, uri="http://unit.invalid"), ProcessingSettings(),
        understanding_fn=lambda **kwargs: TicketUnderstanding(summary="unit understanding", error_codes=[], environment=[]),
        embedding_factory=lambda remaining: Embedding(), decision_fn=decide)
    if failure:
        with pytest.raises(RunFailure) as error:
            runner({"subject": "s", "body": "b"})
        assert error.value.partial["understanding"].summary == "unit understanding"
        # M3 validates source contents inside retrieval, before any decision is possible.
        assert error.value.stage == {"embedding": "retrieval", "decision": "decision", "source": "retrieval"}[failure]
        if failure == "decision":
            assert error.value.evidence[0]["source_id"] == case.source_id
        else:
            assert not calls
    else:
        result = runner({"subject": "s", "body": "b"})
        assert result.state["proposal"] == proposal()
        assert result.evidence[0]["corpus_version"] == runner.metadata["corpus_version"]
    assert client.closed


def test_decision_cache_is_bound_to_actual_request(tmp_path, settings, monkeypatch):
    from ticketmind.agent import dev_decision_cache
    from ticketmind.retrieval.dense import RetrievalHit
    state = {"subject": "s", "body": "b", "understanding": TicketUnderstanding(summary="unit", error_codes=[], environment=[]),
             "retrieval_hits": [RetrievalHit(source_id="SYN-HIST-V2-007", text="unit evidence", score=0.5)]}
    monkeypatch.setattr(dev_decision_cache, "decide_ticket", lambda *args, **kwargs: proposal())
    cache = CachedDecision(settings, tmp_path / "decision.json", allow_call=True)
    assert cache(state, 1, {}).next_step == "ask_clarification"
    assert cache(state, 1, {}).next_step == "ask_clarification"
    assert cache.calls == cache.cache_hits == 1
    with pytest.raises(ValueError, match="不匹配"):
        cache({**state, "body": "different body"}, 1, {})
    assert cache.calls == 1
