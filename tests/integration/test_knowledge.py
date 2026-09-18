"""Real PostgreSQL/HTTP; deterministic external doubles, zero provider calls."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from ticketmind.agent.proposals import Clarification, GetCaseDetail
from ticketmind.agent.runtime import AgentRunner
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.auth import Actor
from ticketmind.core.config import AuthSettings, MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.db.testing import isolated_database
from ticketmind.knowledge.models import KnowledgeCase, KnowledgeDataset, KnowledgeEmbedding, PRODUCTION_DATASET
from ticketmind.knowledge.repository import KnowledgeStore
from ticketmind.knowledge.seed import seed_knowledge, import_seed_vectors
from ticketmind.knowledge.service import KnowledgeWrite, approve_knowledge, change_knowledge
from ticketmind.knowledge.sync import KnowledgeSync, sync_lock
from ticketmind.main import create_app
from ticketmind.retrieval.schemas import IndexHit, RetrievalError
from ticketmind.retrieval.service import retrieve_cases

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.getenv("TICKETMIND_RUN_DB_TESTS") != "1", reason="requires isolated PostgreSQL")]


class MemoryIndex:
    def __init__(self):
        self.rows, self.upserts = {}, 0
        self.fail = self.fail_delete = False
        self.after_upsert = None

    def ensure(self, dataset):
        pass

    def matches(self, dataset, case):
        return self.rows.get((dataset.version, case.source_id)) == case.content_hash

    def upsert(self, dataset, case, vector):
        self.upserts += 1
        if self.fail:
            raise TimeoutError("synthetic-secret")
        self.rows[dataset.version, case.source_id] = case.content_hash
        if self.after_upsert:
            self.after_upsert()

    def delete(self, dataset, case):
        if self.fail_delete:
            raise TimeoutError("synthetic-secret")
        self.rows.pop((dataset.version, case.source_id), None)


class FakeEmbedding:
    def __init__(self):
        self.calls = 0

    def embed_documents(self, documents):
        self.calls += 1
        return [[1.0] * 1024 for _ in documents]


@pytest.fixture
def knowledge():
    with isolated_database(os.getenv("TICKETMIND_TEST_DATABASE_URL")) as (engine, factory, schema):
        config = ProcessingSettings(_env_file=None)
        seeded = seed_knowledge(factory, config.corpus_path)
        qwen = QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="knowledge-test")
        index, embeddings = MemoryIndex(), FakeEmbedding()
        sync = KnowledgeSync(factory, index, qwen, embedding_factory=lambda: embeddings, embedding_budget=20)
        auth = AuthSettings(_env_file=None, operator_token="x" * 32, reviewer_token="y" * 32)
        app = create_app(session_factory=factory, auth_settings=auth, knowledge_sync=sync)
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer " + "y" * 32
            yield SimpleNamespace(engine=engine, factory=factory, schema=schema, seeded=seeded, config=config,
                qwen=qwen, index=index, embeddings=embeddings, sync=sync, client=client)


def post(k, path, payload, key=None, operator=False):
    return k.client.post(path, json=payload, headers={"Idempotency-Key": key or uuid4().hex,
        "Authorization": "Bearer " + ("x" if operator else "y") * 32})


def resolved(k):
    result = post(k, "/tickets", {"subject": "会话沉淀", "body": "登录失败", "channel": "web", "requester_role": "user"})
    ticket_id = result.json()["id"]
    reply = post(k, f"/tickets/{ticket_id}/messages", {"kind": "human_reply", "body": "已核实客户端设置并恢复登录", "expected_version": 1})
    assert reply.status_code == 201
    closed = post(k, f"/tickets/{ticket_id}/close", {"reason": "客户确认恢复", "expected_version": 2})
    assert closed.status_code == 200
    return ticket_id, closed.json()["version"]


def publish(k):
    ticket_id, version = resolved(k)
    response = post(k, f"/tickets/{ticket_id}/knowledge/approve", {"expected_version": version})
    assert response.status_code == 201, response.text
    return response.json()


def active_seed(k):
    dataset = k.seeded["dataset_version"]
    return dataset, k.sync.reconcile(dataset)


def test_seed_idempotent_preserves_status_ids_and_production_separation(knowledge):
    k = knowledge
    dataset, rows = active_seed(k)
    second = seed_knowledge(k.factory, k.config.corpus_path)
    assert second["created"] == 0 and second["total"] == 12
    k.sync.reconcile(dataset)
    assert k.embeddings.calls == 12 and k.index.upserts == 12
    with k.factory() as s:
        assert s.scalar(select(func.count()).select_from(KnowledgeCase)) == 12
        assert s.scalar(select(func.count()).select_from(KnowledgeCase).where(KnowledgeCase.dataset_version == PRODUCTION_DATASET)) == 0
        assert all(s.get(KnowledgeCase, (dataset, row["source_id"])).status == "active" for row in rows)


def test_import_exact_existing_vectors_does_not_call_provider(knowledge, tmp_path):
    k = knowledge
    # Existing synthetic cache is optional locally; generate an exact test cache
    # with the existing adapter and deterministic embeddings, never a provider.
    from ticketmind.knowledge.sources import load_sources
    from ticketmind.knowledge.vector_cache import VectorCache, VectorRecord
    from ticketmind.knowledge.corpus import build_case_text
    import hashlib, json
    cases = list(load_sources(k.config.corpus_path).cases.values())
    documents = [{"source_id": c.source_id, "text": build_case_text(c)} for c in cases]
    identity = {"cache_version": 1, "provider": "dashscope", "region": "cn-beijing", "workspace_id": k.qwen.workspace_id,
        "model": k.qwen.embedding_model, "dimension": 1024, "text_type": "document", "documents": documents}
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cache = VectorCache(fingerprint=fingerprint, records=[VectorRecord(**d, embedding=[1.0]*1024) for d in documents])
    (tmp_path / (fingerprint + ".json")).write_text(cache.model_dump_json(), encoding="utf-8")
    for _ in range(2):
        assert import_seed_vectors(k.factory, k.config.corpus_path, k.qwen, tmp_path) == 12
    k.sync.embedding_budget = 0
    assert all(r["status"] == "active" for r in k.sync.reconcile(k.seeded["dataset_version"]))
    assert k.embeddings.calls == 0


def test_resolved_only_derives_candidate_until_explicit_approval(knowledge):
    k = knowledge
    ticket, version = resolved(k)
    state = k.client.get(f"/tickets/{ticket}/knowledge").json()
    assert state["knowledge"] is None and state["eligible"]
    assert [m["sequence_number"] for m in state["candidate"]["source"]["messages"]] == [1, 2, 3]
    assert "客户确认恢复" in state["candidate"]["content"]
    with k.factory() as s:
        assert s.scalar(select(KnowledgeCase).where(KnowledgeCase.source_ticket_id == UUID(ticket))) is None
    assert k.embeddings.calls == k.index.upserts == 0


def test_unresolved_and_operator_cannot_approve(knowledge):
    k = knowledge
    ticket = post(k, "/tickets", {"subject": "s", "body": "b", "channel": "web", "requester_role": "reviewer"}).json()["id"]
    assert not k.client.get(f"/tickets/{ticket}/knowledge").json()["eligible"]
    assert post(k, f"/tickets/{ticket}/knowledge/approve", {"expected_version": 1}).status_code == 409
    ticket, version = resolved(k)
    assert post(k, f"/tickets/{ticket}/knowledge/approve", {"expected_version": version}, operator=True).status_code == 403
    assert k.embeddings.calls == 0


def test_approve_audit_and_duplicate_keys_are_safe(knowledge):
    k = knowledge
    ticket, version = resolved(k)
    path, key, payload = f"/tickets/{ticket}/knowledge/approve", uuid4().hex, {"expected_version": version}
    first = post(k, path, payload, key)
    assert first.status_code == 201
    for request_key in (key, uuid4().hex):
        repeat = post(k, path, payload, request_key)
        assert repeat.status_code == 200 and repeat.json() == first.json()
    case = first.json()
    assert case["status"] == "active" and case["reviewer_id"] == "reviewer" and case["approved_at"]
    assert case["source_ticket_id"] == ticket and case["source_id"] == f"TICKET-{ticket}"
    assert post(k, path, {"expected_version": version+1}, key).status_code == 409
    assert k.embeddings.calls == k.index.upserts == 1


def test_concurrent_approvals_create_one_stable_case(knowledge):
    k = knowledge
    ticket, version = resolved(k)
    def call(_):
        return approve_knowledge(k.factory, UUID(ticket), KnowledgeWrite(expected_version=version), Actor("reviewer", "reviewer"), uuid4().hex)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, range(2)))
    assert sum(created for _, created in results) == 1
    assert len({case["source_id"] for case, _ in results}) == 1


@pytest.mark.parametrize("operation", ["retry", "retire"])
def test_management_permissions_versions_idempotency(knowledge, operation):
    k = knowledge
    case = publish(k)
    path = f"/knowledge/{case['dataset_version']}/{case['source_id']}/{operation}"
    payload, key = {"expected_version": case["version"]}, uuid4().hex
    assert post(k, path, payload, operator=True).status_code == 403
    assert post(k, path, {"expected_version": 999}).status_code == 409
    first = post(k, path, payload, key)
    assert first.status_code == 200
    assert post(k, path, payload, key).json() == first.json()
    assert post(k, path, {"expected_version": 999}, key).status_code == 409


def test_failed_index_retries_reuse_durable_embedding(knowledge):
    k = knowledge
    k.index.fail = True
    case = publish(k)
    assert case["status"] == "index_failed" and case["index_error"] == "knowledge_index_failed"
    assert "synthetic-secret" not in str(case)
    k.index.fail = False
    rows = k.sync.reconcile(PRODUCTION_DATASET)
    assert len(rows) == 1 and rows[0]["status"] == "active"
    assert k.embeddings.calls == 1 and k.index.upserts == 2
    with k.factory() as s:
        assert s.scalar(select(func.count()).select_from(KnowledgeEmbedding)) == 1


def test_default_zero_budget_is_observable_then_recoverable(knowledge):
    k = knowledge
    k.sync.embedding_budget = 0
    case = publish(k)
    assert case["status"] == "index_failed" and case["index_error"] == "embedding_cache_missing"
    assert k.embeddings.calls == 0
    k.sync.embedding_budget = 1
    assert k.sync.reconcile(PRODUCTION_DATASET)[0]["status"] == "active"


@pytest.mark.parametrize("configuration_failure", [False, True])
def test_default_http_sync_never_spends_embedding_budget(knowledge, monkeypatch, configuration_failure):
    from ticketmind.api.routes import knowledge as routes
    k = knowledge
    k.client.app.state.knowledge_sync = None
    monkeypatch.setattr(routes, "QwenSettings", lambda: k.qwen)
    monkeypatch.setattr(routes, "MilvusKnowledgeIndex", lambda *a: k.index)
    def client(settings):
        if configuration_failure:
            raise RuntimeError("secret provider connection details")
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(routes, "build_milvus_client", client)
    case = publish(k)
    assert case["status"] == "index_failed"
    assert case["index_error"] == ("knowledge_index_unavailable" if configuration_failure else "embedding_cache_missing")
    assert "secret provider" not in str(case) and k.embeddings.calls == 0


def test_oversized_knowledge_fails_before_embedding(knowledge):
    k = knowledge
    ticket = post(k, "/tickets", {"subject": "large", "body": "长" * 10000, "channel": "web", "requester_role": "user"}).json()["id"]
    post(k, f"/tickets/{ticket}/close", {"expected_version": 1, "reason": "closed"})
    result = post(k, f"/tickets/{ticket}/knowledge/approve", {"expected_version": 2}).json()
    assert result["status"] == "index_failed" and result["index_error"] == "knowledge_index_text_too_long"
    assert k.embeddings.calls == 0


def test_source_lookup_uses_requested_pg_dataset_even_without_jsonl(knowledge, monkeypatch):
    k = knowledge
    from ticketmind.knowledge import sources
    monkeypatch.setattr(sources, "load_sources", lambda *a: pytest.fail("source fallback used JSONL"))
    source = "SYN-HIST-V2-007"
    version = k.seeded["dataset_version"]
    assert k.client.get(f"/sources/{source}", params={"corpus_version": version}).status_code == 200
    assert k.client.get(f"/sources/{source}", params={"corpus_version": PRODUCTION_DATASET}).status_code == 404
    assert k.client.get("/sources/missing", params={"corpus_version": version}).status_code == 404


def test_new_knowledge_is_visible_to_an_existing_store(knowledge):
    k = knowledge
    store = KnowledgeStore(k.factory, PRODUCTION_DATASET)
    case = publish(k)
    assert store.get_case_detail(case["source_id"])["ticket_id"] == case["source_ticket_id"]


def test_approval_rejects_stale_ticket_version_without_creating_knowledge(knowledge):
    k = knowledge
    ticket, version = resolved(k)
    response = post(k, f"/tickets/{ticket}/knowledge/approve", {"expected_version": version-1})
    assert response.status_code == 409
    assert k.client.get(f"/tickets/{ticket}/knowledge").json()["knowledge"] is None


def test_pg_exists_index_missing_repair_active_without_embedding(knowledge):
    k = knowledge
    case = publish(k)
    k.index.rows.clear()
    assert k.sync.reconcile(PRODUCTION_DATASET) == []
    assert k.sync.reconcile(PRODUCTION_DATASET, repair_active=True)[0]["status"] == "active"
    assert k.embeddings.calls == 1 and len(k.index.rows) == 1


def test_milvus_written_then_pg_commit_failure_reconciles(knowledge):
    k = knowledge
    def fail_after_write():
        # Raise once on the ACTIVE update; rollback is a real PG transaction.
        def failure(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE knowledge_cases") and parameters.get("status") == "active":
                raise RuntimeError("injected database commit failure")
        event.listen(k.engine, "before_cursor_execute", failure)
        k.failure_listener = failure
    k.index.after_upsert = fail_after_write
    case = publish(k)
    assert case["status"] == "index_failed" and len(k.index.rows) == 1
    event.remove(k.engine, "before_cursor_execute", k.failure_listener)
    k.index.after_upsert = None
    assert k.sync.reconcile(PRODUCTION_DATASET)[0]["status"] == "active"
    assert k.index.upserts == k.embeddings.calls == 1


def test_retire_racing_with_upsert_never_reactivates(knowledge):
    k = knowledge
    ticket, version = resolved(k)
    case, _ = approve_knowledge(k.factory, UUID(ticket), KnowledgeWrite(expected_version=version), Actor("r", "reviewer"), uuid4().hex)
    def retire():
        change_knowledge(k.factory, PRODUCTION_DATASET, case["source_id"], KnowledgeWrite(expected_version=case["version"]),
                         Actor("r", "reviewer"), uuid4().hex, operation="retire")
    k.index.after_upsert = retire
    result = k.sync.one(PRODUCTION_DATASET, case["source_id"])
    assert result["status"] == "retired" and len(k.index.rows) == 1
    k.index.after_upsert = None
    assert k.sync.reconcile(PRODUCTION_DATASET)[0]["index_error"] is None
    assert not k.index.rows


def test_retired_with_index_delete_failure_filtered_and_repaired(knowledge):
    k = knowledge
    case = publish(k)
    k.index.fail_delete = True
    response = post(k, f"/knowledge/{PRODUCTION_DATASET}/{case['source_id']}/retire", {"expected_version": case["version"]})
    assert response.json()["status"] == "retired" and response.json()["index_error"]
    store = KnowledgeStore(k.factory, PRODUCTION_DATASET)
    audit = {}
    assert store.hydrate([IndexHit(source_id=case["source_id"], corpus_version=PRODUCTION_DATASET, rank=1, retrieval_mode="dense")], audit) == []
    assert audit["inconsistencies"][0]["error"] == "inactive_knowledge"
    # Historical source remains available after retirement.
    assert k.client.get(f"/sources/{case['source_id']}", params={"corpus_version": PRODUCTION_DATASET}).json()["source"] == case["source"]
    k.index.fail_delete = False
    k.sync.reconcile(PRODUCTION_DATASET)
    assert not k.index.rows


@pytest.mark.parametrize("status", ["pending_index", "index_failed", "retired"])
def test_hydration_order_batch_missing_inactive_and_hash(knowledge, status):
    k = knowledge
    dataset, cases = active_seed(k)
    ids = [r["source_id"] for r in cases[:3]]
    with k.factory() as s, s.begin():
        s.get(KnowledgeCase, (dataset, ids[1])).status = status
    store, audit, queries = KnowledgeStore(k.factory, dataset), {}, []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM knowledge_cases" in statement:
            queries.append(statement)
    event.listen(k.engine, "before_cursor_execute", capture)
    try:
        hits = [IndexHit(source_id=source, corpus_version=dataset, rank=i+1, retrieval_mode="bm25")
                for i, source in enumerate([ids[2], "missing", ids[1], ids[0]])]
        actual = store.hydrate(hits, audit)
        assert [h.source_id for h in actual] == [ids[2], ids[0]]
        assert [h.rank for h in actual] == [1, 4] and len(queries) == 1
        assert {i["error"] for i in audit["inconsistencies"]} == {"missing_postgres_source", "inactive_knowledge"}
        assert store.hydrate([hits[0].model_copy(update={"content_hash": "wrong"})], {}) == []
        assert store.get_case_detail(ids[2])["source_id"] == ids[2]
        with pytest.raises(RetrievalError):
            store.get_case_detail(ids[1])
    finally:
        event.remove(k.engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("mode", ["dense", "bm25", "hybrid"])
def test_search_reads_only_index_ids_then_pg_content(knowledge, monkeypatch, mode):
    k = knowledge
    case = publish(k)
    from ticketmind.knowledge.index import MilvusKnowledgeIndex
    monkeypatch.setattr(MilvusKnowledgeIndex, "validate", lambda *args: None)
    def search(**kwargs):
        assert "text" not in kwargs["output_fields"] and "bm25_text" not in kwargs["output_fields"]
        return [[{"entity": {"source_id": case["source_id"], "corpus_version": PRODUCTION_DATASET,
                              "content_hash": case["content_hash"], "text": "WRONG MILVUS BODY"}, "distance": 0.8}]]
    def embed(query):
        assert mode != "bm25"
        return [1.0]*1024
    audit = {}
    hits = retrieve_cases("登录", client=SimpleNamespace(search=search), embeddings=SimpleNamespace(embed_query=embed),
        corpus=KnowledgeStore(k.factory, PRODUCTION_DATASET), config=k.config.model_copy(update={"retrieval_mode": mode}),
        timeout=1, record=audit)
    assert hits[0].text == case["content"] and hits[0].metadata["ticket_version"] == 3
    assert hits[0].content_hash == case["content_hash"] and hits[0].knowledge_revision == 1
    assert audit["result_evidence"][0]["synthetic"] is False


def test_runtime_and_detail_use_pg_without_jsonl(knowledge, monkeypatch):
    k = knowledge
    case = publish(k)
    from ticketmind.knowledge import sources
    from ticketmind.knowledge.index import MilvusKnowledgeIndex
    monkeypatch.setattr(sources, "load_sources", lambda *a: pytest.fail("runtime loaded JSONL"))
    monkeypatch.setattr(MilvusKnowledgeIndex, "validate", lambda *a: None)
    client = SimpleNamespace(search=lambda **kw: [[{"entity": {"source_id": case["source_id"], "corpus_version": PRODUCTION_DATASET,
        "content_hash": case["content_hash"]}, "distance": 1.0}]], close=lambda: None)
    def decision(state, timeout, usage):
        if not state["case_details"]:
            return GetCaseDetail(next_step="get_case_detail", source_id=case["source_id"], reason="核对会话")
        assert state["case_details"][case["source_id"]] == case["source"]
        return Clarification(next_step="ask_clarification", reason="缺少现状", reply="请提供当前错误。", questions=["当前错误是什么？"])
    runner = AgentRunner(k.qwen, MilvusSettings(_env_file=None, uri="http://unused"),
        k.config.model_copy(update={"corpus_path": Path("does-not-exist"), "retrieval_mode": "bm25"}),
        session_factory=k.factory, milvus_factory=lambda _: client, decision_fn=decision,
        judge_fn=lambda *args: {"passed": True, "violations": []},
        understanding_fn=lambda **kw: TicketUnderstanding(summary="test", error_codes=[], environment=[]))
    assert not hasattr(runner.corpus, "cases")
    output = runner({"subject": "登录", "body": "失败"})
    assert output.evidence[0]["text"] == case["content"]
    assert output.state["tool_calls"][-1]["status"] == "succeeded"
    assert runner.metadata["corpus_version"] == PRODUCTION_DATASET


def test_advisory_lock_rejects_concurrent_sync_then_releases(knowledge):
    k = knowledge
    with sync_lock(k.factory, PRODUCTION_DATASET):
        with pytest.raises(RetrievalError, match="knowledge_sync_busy"):
            k.sync.reconcile(PRODUCTION_DATASET)
    assert k.sync.reconcile(PRODUCTION_DATASET) == []


def test_constraints_forbid_unapproved_ticket_and_false_active(knowledge):
    k = knowledge
    with k.factory() as s:
        case = s.scalar(select(KnowledgeCase))
        case.status = "active"
        with pytest.raises(IntegrityError):
            s.commit()
    with k.factory() as s:
        case = s.scalar(select(KnowledgeCase))
        case.source_type = "ticket"
        with pytest.raises(IntegrityError):
            s.commit()


def test_migration_empty_downgrade_and_nonempty_refusal(knowledge):
    from alembic import command
    from alembic.config import Config
    config = Config("alembic.ini")
    with knowledge.engine.begin() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="拒绝有损"):
            command.downgrade(config, "9c42d71ab203")
    with isolated_database(os.getenv("TICKETMIND_TEST_DATABASE_URL")) as (engine, _, _):
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "9c42d71ab203")
            command.upgrade(config, "head")


@pytest.mark.parametrize("index_failure", [False, True])
def test_cli_persists_attempt_budget_and_reuses_cache_on_restart(knowledge, tmp_path, monkeypatch, index_failure):
    import importlib.util, json, sys
    from ticketmind.agent import runtime
    k = knowledge
    ticket, version = resolved(k)
    case, _ = approve_knowledge(k.factory, UUID(ticket), KnowledgeWrite(expected_version=version), Actor("r", "reviewer"), uuid4().hex)
    spec = importlib.util.spec_from_file_location("knowledge_cli_test", Path(__file__).resolve().parents[2] / "scripts/knowledge.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    ledger = tmp_path / "attempts.json"
    monkeypatch.setattr(script, "SesstionLocal", k.factory)
    monkeypatch.setattr(script, "QwenSettings", lambda: k.qwen)
    monkeypatch.setattr(script, "build_milvus_client", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(script, "MilvusKnowledgeIndex", lambda *a: k.index)
    def embedding(*args):
        assert json.loads(ledger.read_text())["attempts"][0]["status"] == "started"
        return k.embeddings
    monkeypatch.setattr(runtime, "build_budgeted_embeddings", embedding)
    monkeypatch.setattr(sys, "argv", ["knowledge.py", "reconcile", "--dataset", PRODUCTION_DATASET,
        "--source-id", case["source_id"], "--embedding-budget", "1", "--ledger", str(ledger)])
    k.index.fail = index_failure
    if index_failure:
        with pytest.raises(SystemExit) as error:
            script.main()
        assert error.value.code == 1
    else:
        script.main()
    k.index.fail = False
    script.main()
    assert k.embeddings.calls == 1
    attempts = json.loads(ledger.read_text())["attempts"]
    assert len(attempts) == 1 and attempts[0]["status"] == "succeeded"
