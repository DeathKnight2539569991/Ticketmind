"""Real PG + Milvus. Dedicated disposable collection; no provider/model calls."""
import os
import re

import pytest
from sqlalchemy import select

from test_knowledge import knowledge, post, publish
from ticketmind.knowledge.index import MilvusKnowledgeIndex
from ticketmind.knowledge.models import KnowledgeDataset, PRODUCTION_DATASET
from ticketmind.knowledge.repository import KnowledgeStore
from ticketmind.knowledge.sync import KnowledgeSync
from ticketmind.retrieval.milvus_client import build_milvus_client
from ticketmind.core.config import MilvusSettings, QwenSettings
from ticketmind.retrieval.service import retrieve_cases

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("TICKETMIND_RUN_DB_TESTS") != "1" or os.getenv("TICKETMIND_RUN_MILVUS_TESTS") != "1",
    reason="requires PostgreSQL and Milvus opt-in; zero paid calls")]


def test_real_publish_three_modes_retire_and_repair(knowledge):
    k = knowledge
    name = k.schema + "_knowledge"
    assert re.fullmatch(r"tm_test_[0-9a-f]{32}_knowledge", name)
    milvus = MilvusSettings()
    client = build_milvus_client(milvus)
    assert not client.has_collection(collection_name=name, timeout=milvus.timeout_seconds)
    with k.factory() as s, s.begin():
        s.get(KnowledgeDataset, PRODUCTION_DATASET).collection_name = name
    index = MilvusKnowledgeIndex(client, milvus.timeout_seconds)
    sync = KnowledgeSync(k.factory, index, k.qwen, embedding_factory=lambda: k.embeddings, embedding_budget=1)
    k.client.app.state.knowledge_sync = sync
    try:
        case = publish(k)
        assert case["status"] == "active", case
        # The approved case is immediately usable by a new production Agent run,
        # without loading JSONL or restarting the API. Only model decisions are doubles.
        from ticketmind.agent.runtime import AgentRunner
        from ticketmind.agent.proposals import Clarification, GetCaseDetail
        def decide(state, timeout, usage):
            if not state["case_details"]:
                return GetCaseDetail(next_step="get_case_detail", source_id=case["source_id"], reason="核对已解决会话")
            assert state["case_details"][case["source_id"]] == case["source"]
            return Clarification(next_step="ask_clarification", reason="需核对当前环境",
                                 reply="请提供当前错误。", evidence_ids=[case["source_id"]])
        k.client.app.state.runner = AgentRunner(k.qwen, milvus,
            k.config.model_copy(update={"retrieval_mode": "bm25"}), session_factory=k.factory,
            judge_fn=lambda *args: {"passed": True, "violations": []},
            decision_fn=decide)
        ticket = post(k, "/tickets", {"subject": "登录失败", "body": "登录失败", "channel": "web", "requester_role": "user"}).json()
        detail = k.client.get(f"/tickets/{ticket['id']}").json()
        run = post(k, f"/tickets/{ticket['id']}/runs", {"expected_version": 1, "trigger_message_id": detail["messages"][-1]["id"]})
        assert run.status_code == 201 and run.json()["run_status"] == "waiting_review", run.text
        assert run.json()["retrieval_evidence"][0]["content_hash"] == case["content_hash"]
        store = KnowledgeStore(k.factory, PRODUCTION_DATASET)
        for mode in ("dense", "bm25", "hybrid"):
            audit = {}
            hits = retrieve_cases("登录失败", client=client,
                embeddings=type("QueryDouble", (), {"embed_query": lambda self, query: [1.0]*1024})(),
                corpus=store, config=k.config.model_copy(update={"retrieval_mode": mode}), timeout=milvus.timeout_seconds,
                record=audit, model=k.qwen.embedding_model)
            assert hits and hits[0].source_id == case["source_id"] and hits[0].text == case["content"]
            assert audit["result_hits"][0]["content_hash"] == case["content_hash"]
            assert "text" not in audit["result_hits"][0] and "metadata" not in audit["result_hits"][0]
        # Delete only our test document, then repair ACTIVE from PG's exact cache.
        client.delete(collection_name=name, ids=[case["source_id"]], timeout=milvus.timeout_seconds)
        repaired = sync.reconcile(PRODUCTION_DATASET, repair_active=True)
        assert repaired[0]["status"] == "active" and sync.embedding_calls == 1
        response = post(k, f"/knowledge/{PRODUCTION_DATASET}/{case['source_id']}/retire",
                        {"expected_version": repaired[0]["version"]})
        assert response.json()["status"] == "retired" and response.json()["index_error"] is None
        assert not client.get(collection_name=name, ids=[case["source_id"]], output_fields=["source_id"],
                              consistency_level="Strong", timeout=milvus.timeout_seconds)
        # Inject an orphan index record only into this owned test collection.
        from ticketmind.knowledge.service import require_case
        with k.factory() as s:
            saved = require_case(s, PRODUCTION_DATASET, case["source_id"])
            saved.source_id = "orphan-test-source"
        index.upsert(store.dataset(), saved, [1.0]*1024)
        audit = {}
        assert retrieve_cases("登录失败", client=client, embeddings=None, corpus=store,
            config=k.config.model_copy(update={"retrieval_mode": "bm25"}), timeout=milvus.timeout_seconds,
            record=audit, model=k.qwen.embedding_model) == []
        assert audit["inconsistencies"][0]["error"] == "missing_postgres_source"
    finally:
        # Exact name checked above; never touch shared production/evaluation collections.
        client.drop_collection(collection_name=name, timeout=milvus.timeout_seconds)
        client.close()


def test_frozen_m3_index_hydrates_from_pg_with_unchanged_rankings(knowledge):
    from pathlib import Path
    from ticketmind.knowledge.seed import import_seed_vectors
    from ticketmind.knowledge.sources import load_sources
    from ticketmind.retrieval.versioned_collection import collection_for
    from ticketmind.agent.run_cache import QueryVectorCache
    k = knowledge
    corpus = load_sources(k.config.corpus_path)
    milvus, qwen = MilvusSettings(), QwenSettings()
    client = build_milvus_client(milvus)
    try:
        # No writes to the old collection: verify all rows already match first.
        store = KnowledgeStore(k.factory, corpus.version)
        index = MilvusKnowledgeIndex(client, milvus.timeout_seconds)
        dataset = store.dataset()
        index.validate(dataset)
        for case in store.read_many(list(corpus.cases)).values():
            assert index.matches(dataset, case)
        sync = KnowledgeSync(k.factory, index, qwen)  # zero budget and no embedding factory
        assert all(row["status"] == "active" for row in sync.reconcile(corpus.version))
        assert sync.embedding_calls == 0
        query_cache = QueryVectorCache.model_validate_json(Path("data/cache/graph/api_timeout/query.json").read_text(encoding="utf-8"))
        embeddings = type("ExactCache", (), {"embed_query": lambda self, q: query_cache.vector})()
        for mode in ("dense", "bm25", "hybrid"):
            config = k.config.model_copy(update={"retrieval_mode": mode, "retrieval_top_k": 5})
            legacy = retrieve_cases(query_cache.query, client=client, embeddings=embeddings, corpus=corpus,
                                    config=config, timeout=milvus.timeout_seconds, record={})
            current = retrieve_cases(query_cache.query, client=client, embeddings=embeddings, corpus=store,
                                     config=config, timeout=milvus.timeout_seconds, record={})
            assert [h.source_id for h in current] == [h.source_id for h in legacy]
            assert [h.text for h in current] == [h.text for h in legacy]
    finally:
        client.close()
