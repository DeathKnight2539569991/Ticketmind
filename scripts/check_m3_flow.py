"""Real PostgreSQL/checkpointer + ASGI HTTP + Milvus; synthetic model decisions."""
import argparse
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from evaluate_retrieval import load_queries
from ticketmind.agent.proposals import Clarification, SearchCases
from ticketmind.agent.runtime import AgentRunner
from ticketmind.agent.schemas import AgentMessage
from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.core.config import AuthSettings, MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.db.testing import isolated_database
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.sources import load_sources
from ticketmind.main import create_app
from ticketmind.retrieval.case_collection import CASE_COLLECTION
from ticketmind.retrieval.milvus_client import build_milvus_client
from ticketmind.retrieval.versioned_collection import collection_for, analyzer_for, validate_collection
from ticketmind.tickets.models import ProcessingResult, TicketMessage

ROOT = Path(__file__).resolve().parents[1]


def stored_rows(client, collection, corpus, timeout):
    rows = client.get(collection_name=collection, ids=list(corpus.cases),
        output_fields=["source_id", "text", "embedding"], timeout=timeout, consistency_level="Strong")
    count = client.query(collection_name=collection, filter="", output_fields=["count(*)"],
                         timeout=timeout, consistency_level="Strong")[0]["count(*)"]
    assert count == len(rows) == len(corpus.cases)
    assert {r["source_id"]: r["text"] for r in rows} == {i: build_case_text(c) for i, c in corpus.cases.items()}
    return sorted(rows, key=lambda row: row["source_id"])


class FaultClient:
    """Read-only fault injection: malformed search reaches the real Milvus server."""
    def __init__(self, client, fault):
        self.client, self.fault, self.sparse_calls = client, fault, 0

    def __getattr__(self, name):
        return getattr(self.client, name)

    def describe_collection(self, **kwargs):
        result = self.client.describe_collection(**kwargs)
        if self.fault == "version":
            result = {**result, "description": "explicit-test-version-mismatch"}
        return result

    def search(self, **kwargs):
        if kwargs["anns_field"] == "sparse":
            self.sparse_calls += 1
            if self.fault == "bm25" or (self.fault == "second_bm25" and self.sparse_calls == 2):
                kwargs["anns_field"] = "explicit_missing_test_field"
            elif self.fault == "empty_bm25":
                kwargs["data"] = ["zzqxvnmzzqxvnm"]
        elif self.fault == "dense":
            kwargs["anns_field"] = "explicit_missing_test_field"
        return self.client.search(**kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    qwen, milvus = QwenSettings(), MilvusSettings()
    config = ProcessingSettings(retrieval_top_k=5)
    corpus = load_sources(config.corpus_path)
    queries = load_queries(ROOT / "data/synthetic/m3/cached_queries.jsonl", qwen, need_vectors=True)
    vectors = {row["query"]: row["vector"] for row in queries}
    original = next(row["query"] for row in queries if row["query"].startswith("标题：") and "[3 customer]" in row["query"])
    requery = next(row["query"] for row in queries if not row["query"].startswith("标题："))
    subject, body = original.removeprefix("标题：").split("\n\n问题描述：", 1)
    current_query = build_retrieval_query(
        subject=subject,
        messages=[AgentMessage(role="customer", content=body)],
    )
    # M3 predates the structured-message query protocol. Reuse its historical vector
    # only as a deterministic retrieval fixture; it is not an exact cache hit for current_query.
    vectors[current_query] = vectors[original]
    native = build_milvus_client(milvus)
    try:
        name = validate_collection(native, corpus, timeout=milvus.timeout_seconds)
        before = stored_rows(native, CASE_COLLECTION, corpus, milvus.timeout_seconds)
        assert stored_rows(native, name, corpus, milvus.timeout_seconds) == before
        tokens = native.run_analyzer(texts=["中文工单登录失败 e_timeout e_cursor_invalid created_at 3.12"],
            analyzer_params=analyzer_for(corpus), timeout=milvus.timeout_seconds)[0].tokens
        assert {"中文", "工单", "登录", "失败", "e_timeout", "e_cursor_invalid", "created_at", "3.12"} <= set(tokens)
    finally:
        native.close()
    report = {"created_at": datetime.now(UTC).isoformat(), "real_dependencies": ["PostgreSQL", "checkpointer", "ASGI HTTP", "Milvus"],
        "decision": "synthetic_test_double", "embedding": "historical_vector_fixture_for_query_protocol_migration", "model_calls": 0,
        "corpus_version": corpus.version, "collection": collection_for(corpus), "tokens": tokens,
        "legacy_dense_vectors_equal_new": True, "scenarios": []}
    auth = AuthSettings(_env_file=None, operator_token="m3-operator-" + "x"*32, reviewer_token="m3-reviewer-" + "y"*32)
    with isolated_database() as (_, factory, schema):
        for mode, fault in [("dense", None), ("bm25", None), ("hybrid", None),
                            ("hybrid", "dense"), ("hybrid", "bm25"), ("hybrid", "empty_bm25"),
                            ("hybrid", "version"), ("hybrid", "second_bm25")]:
            decisions, embedded = [], []

            class Embeddings:
                def embed_query(self, query):
                    embedded.append(query)
                    return vectors[query]  # any new input fails, no paid fallback

            def decide(state, timeout, usage):
                decisions.append(state["search_rounds"])
                if state["search_rounds"] == 1:
                    return SearchCases(next_step="search_cases", query=requery, reason="测试受控重检索接线",
                                       missing_evidence="核对全年查询超时案例")
                return Clarification(next_step="ask_clarification", reason="合成替身用于验证待审持久化",
                    reply="请提供当前客户端配置。", questions=["当前客户端配置是什么？"],
                    evidence_ids=[state["retrieval_hits"][0].source_id])

            runner = AgentRunner(qwen, milvus, config.model_copy(update={"retrieval_mode": mode}),
                decision_fn=decide, embedding_factory=lambda remaining: Embeddings(),
                milvus_factory=lambda settings: FaultClient(build_milvus_client(settings), fault), corpus=corpus)
            app = create_app(session_factory=factory, runner=runner, auth_settings=auth)
            with TestClient(app) as http:
                http.headers["Authorization"] = "Bearer " + auth.operator_token.get_secret_value()
                created = http.post("/tickets", headers={"Idempotency-Key": uuid4().hex}, json={
                    "subject": subject, "body": body, "channel": "web", "requester_role": "customer"})
                assert created.status_code == 201, created.text
                ticket_id = created.json()["id"]
                ticket = http.get(f"/tickets/{ticket_id}").json()
                payload = {"expected_version": ticket["version"], "trigger_message_id": ticket["messages"][-1]["id"]}
                key = uuid4().hex
                url = f"/tickets/{ticket_id}/runs"
                response = http.post(url, headers={"Idempotency-Key": key}, json=payload)
                assert response.status_code == 201, response.text
                result = response.json()
                expected = "failed" if fault else "waiting_review"
                assert result["run_status"] == expected, result
                assert result["retrieval_mode"] == mode
                again = http.post(url, headers={"Idempotency-Key": key}, json=payload)
                assert again.status_code == 200 and again.json()["id"] == result["id"]
                detail = http.get(f"/tickets/{ticket_id}").json()
                assert detail["status"] == "open" and detail["version"] == 1
                assert len(detail["messages"]) == 1 and detail["messages"][0]["body"] == body
                with factory() as session:
                    persisted = session.get(ProcessingResult, UUID(result["id"]))
                    assert persisted.tool_calls == result["tool_calls"]
                    assert persisted.retrieval_evidence == result["retrieval_evidence"]
                    assert session.scalar(select(func.count()).select_from(TicketMessage).where(TicketMessage.ticket_id == UUID(ticket_id))) == 1
                    assert session.scalar(select(func.count()).select_from(ProcessingResult).where(ProcessingResult.ticket_id == UUID(ticket_id))) == 1
                if not fault:
                    assert len(decisions) == 2 and len(result["tool_calls"]) == 2
                    assert all(call["retrieval_mode"] == mode and call["status"] == "succeeded" for call in result["tool_calls"])
                    assert all(hit["retrieval_mode"] == mode for hit in result["retrieval_evidence"])
                else:
                    expected_error = {"dense": "dense_retrieval_failed", "bm25": "bm25_retrieval_failed",
                        "empty_bm25": "bm25_empty_results", "version": "collection_version_mismatch",
                        "second_bm25": "bm25_retrieval_failed"}[fault]
                    assert result["error_code"] == expected_error, result
                    if fault in ("bm25", "empty_bm25", "second_bm25"):
                        assert result["tool_calls"][-1]["channels"]["dense"]["candidates"]
                    if fault == "second_bm25":
                        assert len(decisions) == 1 and result["retrieval_evidence"]
                    else:
                        assert not decisions
                assert len(embedded) == (0 if mode == "bm25" else (0 if fault == "version" else 2 if not fault or fault == "second_bm25" else 1))
                report["scenarios"].append({"mode": mode, "fault": fault, "run_status": result["run_status"],
                    "error_code": result["error_code"], "tool_calls": result["tool_calls"],
                    "decision_double_calls": len(decisions), "query_cache_hits": len(embedded),
                    "ticket_status": detail["status"], "message_count": 1, "idempotent_run_count": 1,
                    "duration_ms": result["duration_ms"]})
    native = build_milvus_client(milvus)
    try:
        assert stored_rows(native, CASE_COLLECTION, corpus, milvus.timeout_seconds) == before
        assert stored_rows(native, name, corpus, milvus.timeout_seconds) == before
    finally:
        native.close()
    report["legacy_unchanged"] = True
    report["legacy_data_sha256"] = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
    report["temporary_schema_cleaned"] = schema
    output = args.output or ROOT / "data/cache/m3" / ("flow-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": len(report["scenarios"]), "output": str(output), "model_calls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
