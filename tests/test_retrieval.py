import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ticketmind.core.config import ProcessingSettings
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval import service
from ticketmind.retrieval.hybrid import reciprocal_rank_fusion
from ticketmind.retrieval.schemas import EvidenceHit, RetrievalError
from ticketmind.retrieval.versioned_collection import collection_for, manifest_for, validate_collection


def hit(source, rank=1, channel="dense", score=0.8):
    return EvidenceHit(source_id=source, corpus_version="test", title=source, text=source,
        rank=rank, retrieval_mode=channel, **{f"{channel}_rank": rank, f"{channel}_score": score})


def test_rrf_uses_ranks_deduplicates_and_preserves_scores():
    dense = [hit("a", score=1000), hit("b", 2), hit("b", 3)]
    bm25 = [hit("b", channel="bm25", score=0.001), hit("c", 2, "bm25")]
    result = reciprocal_rank_fusion(dense, bm25, top_k=3)
    assert [h.source_id for h in result] == ["b", "a", "c"]
    assert result[0].fusion_score == pytest.approx(1 / 62 + 1 / 61)
    assert result[0].dense_rank == 2 and result[0].bm25_rank == 1
    assert result[0].bm25_score == 0.001 and result[1].dense_score == 1000
    assert dense[0].fusion_score is None  # inputs remain immutable
    tied = reciprocal_rank_fusion([hit("z")], [hit("a", channel="bm25")], top_k=2)
    assert [h.source_id for h in tied] == ["a", "z"]
    with pytest.raises(ValueError, match="版本"):
        reciprocal_rank_fusion([hit("a")], [hit("a").model_copy(update={"corpus_version": "other"})], top_k=1)


@pytest.mark.parametrize("failure", [
    None, "empty_dense", "empty_bm25", "empty_both", "dense", "bm25", "wrong_version", "wrong_text"
])
def test_hybrid_never_hides_single_channel_failure(monkeypatch, failure):
    config = ProcessingSettings(_env_file=None, retrieval_mode="hybrid", retrieval_top_k=1)
    corpus = load_sources(config.corpus_path)
    case = next(iter(corpus.cases.values()))
    monkeypatch.setattr(service, "validate_collection", lambda *a, **kw: "versioned")
    searches = []

    def search(**kwargs):
        channel = "dense" if kwargs["anns_field"] == "embedding" else "bm25"
        searches.append(channel)
        assert kwargs["limit"] == 20
        if failure == channel:
            raise TimeoutError("secret upstream details")
        if failure == "empty_both" or failure == "empty_" + channel:
            return [[]]
        return [[{"entity": {"source_id": case.source_id, "text": "wrong" if failure == "wrong_text" else build_case_text(case),
                              "corpus_version": "wrong" if failure == "wrong_version" else corpus.version}, "distance": 0.4}]]

    audit = {}
    args = dict(client=SimpleNamespace(search=search), embeddings=SimpleNamespace(embed_query=lambda q: [1.0]*1024),
                corpus=corpus, config=config, timeout=lambda: 1, record=audit)
    hard_failure = failure in {"dense", "bm25", "wrong_version", "wrong_text"}
    if hard_failure:
        with pytest.raises(RetrievalError) as error:
            service.retrieve_cases("query", **args)
        assert "secret" not in str(error.value)
        assert audit["channels"][searches[-1]]["status"] == "failed"
        if searches == ["dense", "bm25"]:
            assert audit["channels"]["dense"]["candidates"]
    else:
        hits = service.retrieve_cases("query", **args)
        if failure == "empty_both":
            assert hits == [] and audit["result_hits"] == []
            assert all(channel["status"] == "succeeded" and not channel["candidates"]
                       for channel in audit["channels"].values())
        else:
            assert len(hits) == 1 and hits[0].retrieval_mode == "hybrid"
            expected = 1 / 61 if failure in {"empty_dense", "empty_bm25"} else 2 / 61
            assert audit["result_hits"][0]["fusion_score"] == pytest.approx(expected)
            if failure in {"empty_dense", "empty_bm25"}:
                empty_channel = failure.removeprefix("empty_")
                assert audit["channels"][empty_channel]["status"] == "succeeded"
                assert audit["channels"][empty_channel]["candidates"] == []
            assert "text" not in audit["result_hits"][0] and "title" not in audit["result_hits"][0]
            assert all("text" not in candidate for channel in audit["channels"].values()
                       for candidate in channel["candidates"])


def test_bm25_never_embeds(monkeypatch):def test_bm25_never_embeds(monkeypatch):
    corpus = load_sources(ProcessingSettings().corpus_path)
    monkeypatch.setattr(service, "validate_collection", lambda *a, **kw: "versioned")
    case = next(iter(corpus.cases.values()))
    client = SimpleNamespace(search=lambda **kw: [[{"entity": {"source_id": case.source_id,
        "text": build_case_text(case), "corpus_version": corpus.version}, "distance": 1.0}]])
    result = service.retrieve_cases("中文", client=client, embeddings=None, corpus=corpus,
        config=ProcessingSettings(_env_file=None, retrieval_mode="bm25"), timeout=1, record={})
    assert result[0].dense_score is None


def test_version_identity_and_mismatch_stop_before_search():
    corpus = load_sources(ProcessingSettings().corpus_path)
    changed = type(corpus)("new-version", corpus.cases)
    assert collection_for(corpus) != collection_for(changed)
    with pytest.raises(RetrievalError, match="model_mismatch"):
        manifest_for(corpus, "other-model")
    client = SimpleNamespace(has_collection=lambda **kw: True, describe_collection=lambda **kw: {"description": "old"})
    with pytest.raises(RetrievalError, match="version_mismatch"):
        validate_collection(client, corpus, timeout=1)
    with pytest.raises(ValidationError):
        ProcessingSettings(_env_file=None, retrieval_mode="automatic")


def test_changed_corpus_never_falls_back_to_old_dense():
    corpus = load_sources(ProcessingSettings().corpus_path)
    changed = type(corpus)("other-corpus-version", corpus.cases)
    client = SimpleNamespace(has_collection=lambda **kw: False)
    with pytest.raises(RetrievalError, match="collection_missing"):
        service.retrieve_cases("query", client=client, embeddings=None, corpus=changed,
            config=ProcessingSettings(_env_file=None), timeout=1, record={})


def test_audit_latency_and_nonselected_candidates_do_not_enter_model_prompt():
    from ticketmind.agent.decide import decision_messages
    from ticketmind.agent.schemas import AgentMessage
    state = {"subject": "s", "messages": [AgentMessage(role="customer", content="b")],
        "retrieval_hits": [hit("a")], "tool_calls": [{"tool": "search_cases",
            "parameters": {"query": "q"}, "reason": "old model rationale", "status": "succeeded",
            "result_source_ids": ["a"], "result_summary": "1 result", "retrieval_mode": "hybrid",
            "duration_ms": 10, "channels": {"dense": {"duration_ms": 12, "candidates": ["unselected"]}}}]}
    first = decision_messages(state)
    payload = json.loads(first[1])
    assert payload["tool_calls"] == [{
        "tool": "search_cases", "parameters": {"query": "q"}, "status": "succeeded"
    }]
    state["tool_calls"][0]["channels"]["dense"]["duration_ms"] = 999
    assert decision_messages(state) == first
    assert "unselected" not in first[1]
    state["retrieval_hits"] = [hit("b")]
    assert decision_messages(state) != first


def test_partial_import_is_rejected_before_search():
    from pymilvus import FunctionType
    from ticketmind.retrieval.versioned_collection import analyzer_for, manifest_text
    corpus = load_sources(ProcessingSettings().corpus_path)
    fields = [{"name": name} for name in ("source_id", "text", "corpus_version", "sparse")]
    fields += [{"name": "embedding", "params": {"dim": 1024}},
               {"name": "bm25_text", "params": {"analyzer_params": analyzer_for(corpus)}}]
    description = {"description": manifest_text(corpus), "fields": fields,
        "functions": [{"type": FunctionType.BM25, "input_field_names": ["bm25_text"], "output_field_names": ["sparse"]}]}
    client = SimpleNamespace(has_collection=lambda **kw: True, describe_collection=lambda **kw: description,
        describe_index=lambda **kw: {"metric_type": "COSINE" if kw["index_name"] == "embedding_flat" else "BM25",
                                    "field_name": "embedding" if kw["index_name"] == "embedding_flat" else "sparse"})
    for count in (0, len(corpus.cases)-1, len(corpus.cases)+1):
        client.query = lambda **kw: [{"count(*)": count}]
        with pytest.raises(RetrievalError, match="data_incomplete"):
            validate_collection(client, corpus, timeout=1, require_data=True)
