"""Deterministic regression tests with synthetic doubles; NOT real-chain evidence."""

import importlib.util
import json
from pathlib import Path

import pytest
from requests import Response
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError

from ticketmind.agent import dev_cache
from ticketmind.agent.dev_cache import CachedQueryEmbeddings, CachedUnderstanding
from ticketmind.agent.graph import build_ticket_graph
from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.agent.run_cache import QueryVectorCache, query_fingerprint
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.config import MilvusSettings, QwenSettings


@pytest.fixture
def settings():
    return QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unit-test-only",
                        DASHSCOPE_WORKSPACE_ID="unit-test-only")


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected external call")


@pytest.fixture
def entry():
    path = Path(__file__).resolve().parents[1] / "scripts/check_ticket_graph.py"
    spec = importlib.util.spec_from_file_location("graph_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_graph_saves_before_search_failure_and_replays(tmp_path, monkeypatch, settings):
    state = {"subject": "test subject", "body": "test body"}
    query = build_retrieval_query(**state)
    result = TicketUnderstanding(summary="synthetic test", error_codes=[], environment=[])
    understanding = CachedUnderstanding(tmp_path / "u.json", allow_call=True)
    monkeypatch.setattr(dev_cache, "understand_ticket", lambda **kwargs: result)

    class Embedder:
        def embed_query(self, text):
            assert understanding.path.exists()  # Understanding survives embedding failure, too.
            assert text == query
            return [1.0] * 1024

    embeddings = CachedQueryEmbeddings(settings, tmp_path / "q.json", factory=Embedder, allow_call=True)

    class Search:
        failed = False

        def search(self, **kwargs):
            assert embeddings.path.exists()  # Both paid results persisted BEFORE Milvus search.
            assert kwargs["data"] == [[1.0] * 1024]
            if not self.failed:
                self.failed = True
                raise RuntimeError("synthetic Milvus failure")
            return [[{"entity": {"source_id": "unit-only", "text": "synthetic evidence"}, "distance": 0.5}]]

    graph = build_ticket_graph(settings, embeddings=embeddings, client=Search(), top_k=3,
                               timeout=1, understanding_fn=understanding)
    with pytest.raises(RuntimeError, match="Milvus failure"):
        graph.invoke(state)
    monkeypatch.setattr(dev_cache, "understand_ticket", forbidden)
    embeddings.factory = forbidden
    final = graph.invoke(state)
    assert final["subject"] == state["subject"] and final["body"] == state["body"]
    assert final["understanding"] == result and final["retrieval_hits"][0].source_id == "unit-only"
    assert (understanding.calls, embeddings.calls) == (1, 1)
    assert (understanding.cache_hits, embeddings.cache_hits) == (1, 1)


@pytest.mark.parametrize("change", ["input", "model", "workspace", "metadata", "corrupt", "dimension", "zero"])
def test_invalid_query_cache_never_falls_back_to_model(tmp_path, settings, change):
    path = tmp_path / "q.json"
    data = QueryVectorCache(query="q", model=settings.embedding_model,
                            request_fingerprint=query_fingerprint(settings=settings, query="q"),
                            vector=[1.0] * 1024).model_dump()
    query = "q"
    if change == "input":
        query = "different q"
    elif change == "model":
        settings = settings.model_copy(update={"embedding_model": "different-model"})
    elif change == "workspace":
        settings = settings.model_copy(update={"workspace_id": "different-workspace"})
    elif change == "metadata":
        data["query"] = "tampered"
    elif change == "dimension":
        data["vector"] = [1.0]
    elif change == "zero":
        data["vector"] = [0.0] * 1024
    path.write_text("broken json" if change == "corrupt" else json.dumps(data), encoding="utf-8")
    embeddings = CachedQueryEmbeddings(settings, path, factory=forbidden, allow_call=True)
    with pytest.raises(ValueError):
        embeddings.embed_query(query)
    assert embeddings.calls == 0


def test_missing_cache_denies_calls_by_default(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(dev_cache, "understand_ticket", forbidden)
    with pytest.raises(RuntimeError, match="理解缓存"):
        CachedUnderstanding(tmp_path / "u.json")(settings=settings, subject="s", body="b")
    with pytest.raises(RuntimeError, match="查询缓存"):
        CachedQueryEmbeddings(settings, tmp_path / "q.json", factory=forbidden).embed_query("q")


def test_failed_embedding_keeps_understanding_and_does_not_retry(tmp_path, settings, monkeypatch):
    result = TicketUnderstanding(summary="unit only", error_codes=[], environment=[])
    monkeypatch.setattr(dev_cache, "understand_ticket", lambda **kwargs: result)
    understanding = CachedUnderstanding(tmp_path / "u.json", allow_call=True)
    understanding(settings=settings, subject="s", body="b")

    class FailingEmbedding:
        def embed_query(self, text):
            raise RuntimeError("synthetic provider failure")

    embeddings = CachedQueryEmbeddings(settings, tmp_path / "q.json", factory=FailingEmbedding, allow_call=True)
    with pytest.raises(RuntimeError, match="provider failure"):
        embeddings.embed_query("q")
    with pytest.raises(RuntimeError, match="最多一次"):
        embeddings.embed_query("q")
    assert understanding.path.exists() and not embeddings.path.exists()


@pytest.mark.parametrize("change", ["body", "model", "prompt", "options"])
def test_understanding_fingerprint_rejects_changes(tmp_path, settings, monkeypatch, change):
    from ticketmind.agent import run_cache
    result = TicketUnderstanding(summary="unit only", error_codes=[], environment=[])
    monkeypatch.setattr(dev_cache, "understand_ticket", lambda **kwargs: result)
    adapter = CachedUnderstanding(tmp_path / "u.json", allow_call=True)
    adapter(settings=settings, subject="s", body="b")
    monkeypatch.setattr(dev_cache, "understand_ticket", forbidden)
    body = "b"
    if change == "body":
        body = "changed"
    elif change == "model":
        settings = settings.model_copy(update={"model": "changed"})
    elif change == "prompt":
        monkeypatch.setattr(run_cache, "SYSTEM_PROMPT", "changed prompt")
    else:
        monkeypatch.setitem(run_cache.GENERATION_OPTIONS, "temperature", 0.7)
    with pytest.raises(ValueError, match="不匹配"):
        adapter(settings=settings, subject="s", body=body)
    assert adapter.calls == 1


def test_cache_write_failure_stops_before_embedding(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(dev_cache, "understand_ticket", lambda **kwargs:
                        TicketUnderstanding(summary="unit only", error_codes=[], environment=[]))

    def write_failed(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(dev_cache, "save_cache", write_failed)
    adapter = CachedUnderstanding(tmp_path / "u.json", allow_call=True)
    embeddings = CachedQueryEmbeddings(settings, tmp_path / "q.json", factory=forbidden, allow_call=True)
    graph = build_ticket_graph(settings, embeddings=embeddings, client=None,
                               top_k=3, timeout=1, understanding_fn=adapter)
    with pytest.raises(OSError, match="disk failure"):
        graph.invoke({"subject": "s", "body": "b"})
    assert embeddings.calls == 0


def test_business_graph_accepts_new_ticket_without_cache(settings, monkeypatch):
    from ticketmind.agent import understand
    monkeypatch.setattr(understand, "generate_text", lambda **kwargs:
                        '{"summary":"unit only","error_codes":[],"environment":[]}')

    class Embedder:
        def embed_query(self, text):
            return [1.0] * 1024

    class Search:
        def search(self, **kwargs):
            return [[]]

    graph = build_ticket_graph(settings, embeddings=Embedder(), client=Search(), top_k=3, timeout=1)
    result = graph.invoke({"subject": "new", "body": "ticket"})
    assert result["understanding"].summary == "unit only"
    assert result["retrieval_hits"] == []


def test_entry_preflight_is_offline_and_missing_cache_stops_before_clients(tmp_path, settings, monkeypatch, entry):
    monkeypatch.setattr(entry, "QwenSettings", lambda: settings)
    monkeypatch.setattr(entry, "MilvusSettings", lambda: MilvusSettings(_env_file=None, uri="http://unit.invalid"))
    monkeypatch.setattr(entry, "build_milvus_client", forbidden)
    monkeypatch.setattr(entry, "build_single_attempt_embeddings", forbidden)
    entry.run_check(cache_dir=tmp_path, check_only=True)
    with pytest.raises(RuntimeError, match="缺少理解缓存"):
        entry.run_check(cache_dir=tmp_path)


def test_entry_closes_milvus_on_preflight_failure(tmp_path, settings, monkeypatch, entry):
    class EmptyClient:
        closed = False

        def has_collection(self, **kwargs):
            return False

        def close(self):
            self.closed = True

    client = EmptyClient()
    monkeypatch.setattr(entry, "QwenSettings", lambda: settings)
    monkeypatch.setattr(entry, "MilvusSettings", lambda: MilvusSettings(_env_file=None, uri="http://unit.invalid"))
    monkeypatch.setattr(entry, "build_milvus_client", lambda settings: client)
    monkeypatch.setattr(dev_cache, "understand_ticket", forbidden)
    with pytest.raises(RuntimeError, match="集合不存在"):
        entry.run_check(cache_dir=tmp_path, allow_understanding=True, allow_embedding=True)
    assert client.closed


@pytest.mark.parametrize("connection_error", [False, True])
def test_actual_embedding_sdk_uses_one_http_attempt(monkeypatch, settings, connection_error):
    """Run locked SDK serialization/retry code against a fake HTTP transport."""
    sends = []

    def fake_send(self, request, **kwargs):
        sends.append(request)
        payload = json.loads(request.body)
        assert payload["input"]["texts"] == ["unit query"]
        assert payload["parameters"]["text_type"] == "query"
        assert payload["parameters"]["dimension"] == 1024
        assert kwargs["timeout"] == 30
        if connection_error:
            raise ConnectionError("synthetic dropped connection")
        response = Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({"output": {"embeddings": [{"embedding": [1.0] * 1024}]}}).encode()
        return response

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    embedding = dev_cache.build_single_attempt_embeddings(settings)
    if connection_error:
        with pytest.raises(RuntimeError, match="禁止自动重发"):
            embedding.embed_query("unit query")
    else:
        assert embedding.embed_query("unit query") == [1.0] * 1024
    assert len(sends) == 1
