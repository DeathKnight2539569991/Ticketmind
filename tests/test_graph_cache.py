"""Deterministic regression tests with synthetic doubles; NOT real-chain evidence."""

import importlib.util
import json
from pathlib import Path

import pytest
from requests import Response
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError

from ticketmind.agent import dev_cache
from ticketmind.agent.dev_cache import CachedQueryEmbeddings
from ticketmind.agent.graph import build_ticket_graph
from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.agent.run_cache import QueryVectorCache, query_fingerprint, save_cache
from ticketmind.agent.schemas import AgentMessage
from ticketmind.core.config import MilvusSettings, QwenSettings


@pytest.fixture
def settings():
    return QwenSettings(
        _env_file=None,
        DASHSCOPE_API_KEY="unit-test-only",
        DASHSCOPE_WORKSPACE_ID="unit-test-only",
    )


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected external call")


@pytest.fixture
def entry():
    path = Path(__file__).resolve().parents[1] / "scripts/check_ticket_graph.py"
    spec = importlib.util.spec_from_file_location("graph_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def agent_state(subject="test subject", content="test body"):
    return {
        "subject": subject,
        "messages": [AgentMessage(role="customer", content=content)],
        "clarification_rounds": 0,
        "tool_calls": [],
    }


def test_graph_persists_query_cache_before_search_failure_and_replays(tmp_path, settings):
    state = agent_state()
    query = build_retrieval_query(subject=state["subject"], messages=state["messages"])

    class Embedder:
        def embed_query(self, text):
            assert text == query
            return [1.0] * 1024

    embeddings = CachedQueryEmbeddings(
        settings,
        tmp_path / "q.json",
        factory=Embedder,
        allow_call=True,
    )

    class Search:
        failed = False

        def search(self, **kwargs):
            assert embeddings.path.exists()
            assert kwargs["data"] == [[1.0] * 1024]
            if not self.failed:
                self.failed = True
                raise RuntimeError("synthetic Milvus failure")
            return [[{"entity": {"source_id": "unit-only", "text": "synthetic evidence"}, "distance": 0.5}]]

    graph = build_ticket_graph(embeddings=embeddings, client=Search(), top_k=3, timeout=1)
    with pytest.raises(RuntimeError, match="Milvus failure"):
        graph.invoke(state)

    embeddings.factory = forbidden
    final = graph.invoke(state)
    assert final["subject"] == state["subject"]
    assert final["messages"] == state["messages"]
    assert final["retrieval_hits"][0].source_id == "unit-only"
    assert embeddings.calls == 1
    assert embeddings.cache_hits == 1


@pytest.mark.parametrize("change", ["input", "model", "workspace", "metadata", "corrupt", "dimension", "zero"])
def test_invalid_query_cache_never_falls_back_to_model(tmp_path, settings, change):
    path = tmp_path / "q.json"
    data = QueryVectorCache(
        query="q",
        model=settings.embedding_model,
        request_fingerprint=query_fingerprint(settings=settings, query="q"),
        vector=[1.0] * 1024,
    ).model_dump()
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


def test_missing_query_cache_denies_calls_by_default(tmp_path, settings):
    with pytest.raises(RuntimeError, match="查询缓存"):
        CachedQueryEmbeddings(settings, tmp_path / "q.json", factory=forbidden).embed_query("q")


def test_failed_embedding_does_not_retry_or_persist(tmp_path, settings):
    class FailingEmbedding:
        def embed_query(self, text):
            raise RuntimeError("synthetic provider failure")

    embeddings = CachedQueryEmbeddings(
        settings,
        tmp_path / "q.json",
        factory=FailingEmbedding,
        allow_call=True,
    )
    with pytest.raises(RuntimeError, match="provider failure"):
        embeddings.embed_query("q")
    with pytest.raises(RuntimeError, match="最多一次"):
        embeddings.embed_query("q")
    assert not embeddings.path.exists()


def test_cache_write_failure_stops_before_search(tmp_path, settings, monkeypatch):
    def write_failed(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(dev_cache, "save_cache", write_failed)

    class Embedder:
        def embed_query(self, text):
            return [1.0] * 1024

    class Search:
        def search(self, **kwargs):
            pytest.fail("search must not run when cache persistence failed")

    embeddings = CachedQueryEmbeddings(
        settings,
        tmp_path / "q.json",
        factory=Embedder,
        allow_call=True,
    )
    graph = build_ticket_graph(embeddings=embeddings, client=Search(), top_k=3, timeout=1)
    with pytest.raises(OSError, match="disk failure"):
        graph.invoke(agent_state("s", "b"))


def test_business_graph_accepts_new_ticket_without_understanding_node():
    class Embedder:
        def embed_query(self, text):
            return [1.0] * 1024

    class Search:
        def search(self, **kwargs):
            return [[]]

    graph = build_ticket_graph(embeddings=Embedder(), client=Search(), top_k=3, timeout=1)
    result = graph.invoke(agent_state("new", "ticket"))
    assert "understanding" not in result
    assert result["retrieval_hits"] == []


def test_entry_preflight_is_offline_and_missing_cache_stops_before_clients(tmp_path, settings, monkeypatch, entry):
    monkeypatch.setattr(entry, "QwenSettings", lambda: settings)
    monkeypatch.setattr(
        entry,
        "MilvusSettings",
        lambda: MilvusSettings(_env_file=None, uri="http://unit.invalid"),
    )
    monkeypatch.setattr(entry, "build_milvus_client", forbidden)
    monkeypatch.setattr(entry, "build_single_attempt_embeddings", forbidden)

    entry.run_check(cache_dir=tmp_path, check_only=True)
    with pytest.raises(RuntimeError, match="缺少查询缓存"):
        entry.run_check(cache_dir=tmp_path)


def test_entry_closes_milvus_on_preflight_failure(tmp_path, settings, monkeypatch, entry):
    query = build_retrieval_query(
        subject=entry.sample_state()["subject"],
        messages=entry.sample_state()["messages"],
    )
    save_cache(
        tmp_path / "query.json",
        QueryVectorCache(
            query=query,
            model=settings.embedding_model,
            request_fingerprint=query_fingerprint(settings=settings, query=query),
            vector=[1.0] * 1024,
        ),
    )

    class EmptyClient:
        closed = False

        def has_collection(self, **kwargs):
            return False

        def close(self):
            self.closed = True

    client = EmptyClient()
    monkeypatch.setattr(entry, "QwenSettings", lambda: settings)
    monkeypatch.setattr(
        entry,
        "MilvusSettings",
        lambda: MilvusSettings(_env_file=None, uri="http://unit.invalid"),
    )
    monkeypatch.setattr(entry, "build_milvus_client", lambda settings: client)
    with pytest.raises(RuntimeError, match="集合不存在"):
        entry.run_check(cache_dir=tmp_path)
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
        response._content = json.dumps(
            {"output": {"embeddings": [{"embedding": [1.0] * 1024}]}}
        ).encode()
        return response

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    embedding = dev_cache.build_single_attempt_embeddings(settings)
    if connection_error:
        with pytest.raises(RuntimeError, match="禁止自动重发"):
            embedding.embed_query("unit query")
    else:
        assert embedding.embed_query("unit query") == [1.0] * 1024
    assert len(sends) == 1
