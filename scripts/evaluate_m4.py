"""M4: prepare offline; retrieve with caches; execute only within explicit cumulative ceilings."""
import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import secrets
from time import monotonic
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from ticketmind.agent.dev_acceptance import AcceptanceAdapters, AttemptLedger, CATEGORIES, acceptance_lock, write_json
from ticketmind.agent.run_cache import QueryVectorCache, load_cache, query_fingerprint
from ticketmind.agent.runtime import AgentRunner
from ticketmind.core.config import AuthSettings, MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.db.testing import isolated_database
from ticketmind.evaluation.dataset import digest, load_dataset, read_jsonl, retrieval_queries, load_development_labels
from ticketmind.evaluation.scoring import apply_adjudication, score_prediction, summarize
from ticketmind.knowledge.sources import load_sources
from ticketmind.main import create_app
from ticketmind.retrieval.milvus_client import build_milvus_client
from ticketmind.retrieval.schemas import RetrievalError
from ticketmind.retrieval.service import retrieve_cases
from ticketmind.retrieval.versioned_collection import manifest_for
from ticketmind.tickets.models import ProcessingResult

from evaluate_retrieval import CachedQuery, metrics

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/synthetic/m4"
CACHE = ROOT / "data/cache/m4"


def compact_evidence(value):
    # Source content is reproducible by manifest/source_id; omit thousands of repeated strings.
    if isinstance(value, list):
        return [compact_evidence(item) for item in value]
    if isinstance(value, dict):
        return {key: compact_evidence(item) for key, item in value.items() if key != "text"}
    return value


def cached_vector(query, qwen):
    fp = query_fingerprint(settings=qwen, query=query)
    cache = load_cache(CACHE / "query" / f"{fp}.json", QueryVectorCache, expected_fingerprint=fp)
    if cache is not None and (cache.query != query or cache.model != qwen.embedding_model):
        raise ValueError("查询缓存内容与模型不匹配")
    return cache.vector if cache else None


def run_retrieval(queries, qwen, config, corpus, modes):
    rows = []
    client = build_milvus_client(MilvusSettings())
    try:
        server = client.get_server_version(timeout=MilvusSettings().timeout_seconds)
        for query in queries:
            vector = cached_vector(query["query"], qwen) if any(mode != "bm25" for mode in modes) else None
            row = {**query, "modes": {}}
            for mode in modes:
                if mode != "bm25" and vector is None:
                    row["modes"][mode] = {"status": "not_run", "reason": "missing_exact_query_vector", "hits": []}
                    continue
                started, audit = monotonic(), {}
                try:
                    hits = retrieve_cases(query["query"], client=client, embeddings=CachedQuery(query["query"], vector),
                        corpus=corpus, config=config.model_copy(update={"retrieval_mode": mode}),
                        timeout=MilvusSettings().timeout_seconds, record=audit, model=qwen.embedding_model)
                    value = {"status": "succeeded", "hits": [hit.model_dump() for hit in hits]}
                except RetrievalError as exc:
                    value = {"status": "failed", "error_code": exc.code, "hits": []}
                row["modes"][mode] = {**value, "duration_ms": round((monotonic() - started) * 1000), "audit": audit}
            rows.append(row)
    finally:
        client.close()
    summary = {}
    for split in sorted({row["split"] for row in rows}):
        summary[split] = {}
        for mode in modes:
            planned = [row for row in rows if row["split"] == split]
            executed = [row for row in planned if row["modes"][mode]["status"] != "not_run"]
            summary[split][mode] = {"planned": len(planned), "executed": len(executed),
                "not_run": len(planned) - len(executed),
                "failed": sum(row["modes"][mode]["status"] == "failed" for row in executed),
                "reviewed": metrics(executed, mode, label_status="reviewed"),
                "pending_review_exploratory": metrics(executed, mode, label_status="pending_review"),
                "no_answer_candidates": [{"case_id": row["case_id"], "ids": [hit["source_id"] for hit in row["modes"][mode]["hits"]]}
                                         for row in executed if not row["answer_available"]]}
    return {"rows": compact_evidence(rows), "summary": summary, "server_version": server, "model_calls": 0,
            "evidence_text": "omitted_reconstruct_from_versioned_corpus_and_source_id",
            "embedding_source": "exact_cache_or_not_run"}


def execute_agent(case, qwen, config, ledger, decision_model):
    # Only customer input enters HTTP / Agent. No label or rule sheet is supplied.
    adapters = AcceptanceAdapters(qwen, CACHE, ledger)
    decisions = AcceptanceAdapters(qwen.model_copy(update={"model": decision_model}), CACHE, ledger)
    runner = AgentRunner(qwen, MilvusSettings(), config,
                         embedding_factory=adapters.embeddings, decision_fn=decisions.decision,
                         corpus=load_sources(config.corpus_path))
    class EvaluationRunner:
        @property
        def metadata(self):
            meta = runner.metadata
            meta["model_config"]["decision"] = decision_model
            return meta

        def __call__(self, agent_input, *, clarification_rounds=0):
            return runner(agent_input, clarification_rounds=clarification_rounds)
    auth = AuthSettings(_env_file=None, operator_token=secrets.token_urlsafe(32), reviewer_token=secrets.token_urlsafe(32))
    report = {"case_id": case["case_id"], "input_hash": digest(case["input"]),
              "verification": "real_asgi_http_postgresql_checkpointer_milvus_model_or_exact_cache",
              "business_review": "not_executed", "status": "failed"}
    try:
        with isolated_database(os.getenv("TICKETMIND_TEST_DATABASE_URL")) as (_, factory, schema):
            app = create_app(session_factory=factory, runner=EvaluationRunner(), auth_settings=auth, processing_settings=config)
            with TestClient(app) as client:
                client.headers["Authorization"] = "Bearer " + auth.operator_token.get_secret_value()
                response = client.post("/tickets", json=case["input"], headers={"Idempotency-Key": uuid4().hex})
                assert response.status_code == 201
                ticket_id = response.json()["id"]
                ticket = client.get(f"/tickets/{ticket_id}").json()
                payload = {"expected_version": ticket["version"], "trigger_message_id": ticket["messages"][-1]["id"]}
                key = {"Idempotency-Key": uuid4().hex}
                response = client.post(f"/tickets/{ticket_id}/runs", json=payload, headers=key)
                assert response.status_code == 201
                run = response.json()
                report.update(status="succeeded" if run["run_status"] == "waiting_review" else "failed",
                    run=run, final_proposal=run["proposal"], retrieval_evidence=run["retrieval_evidence"],
                    temporary_schema=schema, raw_decisions=[], raw_proposal=None,
                    evaluation_config={"models": run["models"], "corpus_version": run["corpus_version"],
                                       "retrieval_mode": run["retrieval_mode"], "agent_version": run["agent_version"]})
                for entry in (run.get("usage") or {}).get("acceptance_decisions", []):
                    raw = json.loads(decisions.path("decision", entry["fingerprint"]).read_text(encoding="utf-8"))["response"]
                    report["raw_decisions"].append(raw)
                    content = raw["choices"][0]["content"] if raw.get("choices") else None
                    try:
                        value = json.loads(content or "")
                    except (ValueError, TypeError):
                        value = None
                    # Last raw response only: an earlier successful proposal cannot mask a later malformed response.
                    report["raw_proposal"] = value if isinstance(value, dict) else None
                with factory() as session:
                    saved = session.get(ProcessingResult, UUID(run["id"]))
                    assert saved.proposal == run["proposal"] and saved.retrieval_evidence == run["retrieval_evidence"]
                repeated = client.post(f"/tickets/{ticket_id}/runs", json=payload, headers=key)
                assert repeated.status_code == 200 and repeated.json() == run
                current = client.get(f"/tickets/{ticket_id}").json()
                assert current["status"] == "open" and current["version"] == 1 and len(current["messages"]) == 1
                report["http_database_consistent"] = report["idempotent_no_extra_run"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        write_json(CACHE / "predictions" / f'{case["case_id"]}-{uuid4().hex}.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["prepare", "retrieval", "vectors", "agent", "score"], default="prepare")
    parser.add_argument("--dataset", type=Path, default=DATA)
    parser.add_argument("--label-reviews", type=Path)
    parser.add_argument("--development-label-reviews", type=Path)
    parser.add_argument("--predictions", type=Path, nargs="+", help="Agent 原始预测 JSON 文件；score 不调用外部服务")
    parser.add_argument("--adjudications", type=Path, help="独立人工语义审核 JSONL")
    parser.add_argument("--case", action="append")
    parser.add_argument("--modes", nargs="+", choices=["dense", "bm25", "hybrid"], default=["dense", "bm25", "hybrid"])
    parser.add_argument("--agent-mode", choices=["dense", "bm25", "hybrid"], default="hybrid")
    parser.add_argument("--decision-model", default="glm-5.2")
    parser.add_argument("--execute", action="store_true", help="vectors/agent 执行；参数本身不构成付费授权")
    parser.add_argument("--include-m3-diagnostics", action="store_true")
    parser.add_argument("--output", type=Path)
    for category in CATEGORIES:
        parser.add_argument("--" + category.replace("_", "-") + "-ceiling", type=int, default=0)
    args = parser.parse_args()
    ceilings = {c: getattr(args, c + "_ceiling") for c in CATEGORIES}
    if any(v < 0 for v in ceilings.values()):
        parser.error("累计上限不能为负；失败请求同样计次")
    qwen = QwenSettings()
    config = ProcessingSettings(retrieval_mode=args.agent_mode, retrieval_top_k=5, retrieval_candidate_k=20, retrieval_rrf_k=60)
    corpus = load_sources(config.corpus_path)
    for path in (args.label_reviews, args.development_label_reviews):
        if path is not None and not path.exists():
            parser.error("显式指定的标签审核文件不存在")
    review_path = args.label_reviews or args.dataset / "label_reviews.jsonl"
    rows = load_dataset(args.dataset, corpus, review_path if review_path.exists() else None)
    overlay_path = args.dataset / "development_labels_overlay.jsonl"
    overlay = read_jsonl(overlay_path) if overlay_path.exists() else []
    originals = {r["case_id"]: r for name in ("v2/evaluation_cases.jsonl", "m3/cached_queries.jsonl", "m3/retrieval_diagnostics.jsonl")
                 for r in read_jsonl(ROOT / "data/synthetic" / name)}
    development_review_path = args.development_label_reviews or args.dataset / "development_label_reviews.jsonl"
    development_labels = load_development_labels(overlay, originals, corpus,
        development_review_path if development_review_path.exists() else None) if overlay else {}
    if args.case and set(args.case) - {r["case"]["case_id"] for r in rows}:
        parser.error("存在未知 case_id")
    rows = [row for row in rows if not args.case or row["case"]["case_id"] in args.case]
    queries = retrieval_queries(rows)
    if args.include_m3_diagnostics:
        for query in read_jsonl(ROOT / "data/synthetic/m3/retrieval_diagnostics.jsonl"):
            label = development_labels.get(query["case_id"], query)
            queries.append({**query, **{key: label[key] for key in ("relevant_source_ids", "distractor_source_ids", "answer_available", "rationale")},
                            "split": "development", "label_status": label["label_status"],
                            "review_method": label.get("review_method"), "label_hash": label.get("label_hash", digest(label))})
    report = {"created_at": datetime.now(UTC).isoformat(), "stage": args.stage, "synthetic": True,
        "dataset_sha256": digest(rows), "query_set_sha256": digest(queries), "development_overlay_sha256": digest(overlay),
        "manifest": manifest_for(corpus), "config": config.model_dump(mode="json"),
        "decision_model": args.decision_model, "embedding_model": qwen.embedding_model,
        "decision_protocol": AgentRunner(qwen, MilvusSettings(), config, corpus=corpus).metadata["model_config"]["decision_protocol"],
        "new_call_ceilings": ceilings, "price": None, "price_status": "not_available", "rows": []}
    output = args.output or CACHE / "reports" / f"{args.stage}-{uuid4().hex}.json"
    try:
        if args.stage == "score":
            if not args.predictions:
                parser.error("score 需要 --predictions")
            selected = {r["case"]["case_id"]: r for r in rows}
            adjudications = {r["case_id"]: r for r in read_jsonl(args.adjudications)} if args.adjudications else {}
            seen, scores, configurations = set(), [], set()
            for path in args.predictions:
                prediction = json.loads(path.read_text(encoding="utf-8"))
                case_id = prediction["case_id"]
                if case_id in seen or case_id not in selected:
                    raise ValueError("预测重复或不属于选定样本集")
                seen.add(case_id)
                if prediction.get("evaluation_config", {}).get("corpus_version") != corpus.version:
                    raise ValueError("预测语料版本与评测标签来源不一致")
                configurations.add(digest(prediction["evaluation_config"]))
                if len(configurations) != 1:
                    raise ValueError("不能合并不同模型/协议/检索配置的预测")
                score = score_prediction(selected[case_id], prediction, corpus)
                if case_id in adjudications:
                    score = apply_adjudication(score, adjudications[case_id])
                scores.append(score)
            if set(adjudications) - seen:
                raise ValueError("语义审核存在没有预测的样本")
            report.update(scores=scores, summary=summarize(scores), model_calls=0,
                          prediction_config=prediction["evaluation_config"],
                          not_run_case_ids=sorted(set(selected) - seen))
        elif args.stage == "retrieval":
            report.update(run_retrieval(queries, qwen, config, corpus, args.modes))
        elif args.stage in {"vectors", "agent"} and args.execute:
            if args.stage == "agent" and not args.case:
                parser.error("Agent 执行必须显式列出获授权样本 --case")
            with acceptance_lock(CACHE):
                ledger = AttemptLedger(CACHE / "attempts.json", ceilings)
                try:
                    if args.stage == "vectors":
                        adapters = AcceptanceAdapters(qwen, CACHE, ledger)
                        for query in queries:
                            # Every fixed evaluation query is an initial embedding, not an Agent research call.
                            adapters.embeddings(lambda: 30.0).embed_query(query["query"])
                            report["rows"].append({"case_id": query["case_id"], "status": "cached"})
                    else:
                        scores = []
                        for row in rows:
                            prediction = execute_agent(row["case"], qwen, config, ledger, args.decision_model)
                            report["rows"].append(prediction)
                            scores.append(score_prediction(row, prediction, corpus))
                            report.update(scores=scores, summary=summarize(scores))
                            write_json(output, report)
                            # Preserve failures, continue the fixed set; ledger never retries a failed fingerprint.
                finally:
                    report["cumulative_attempts"] = ledger.data["attempts"]
                    report["session_cache_hits"] = ledger.cache_hits
        else:
            report.update(model_calls=0, external_services_accessed=False,
                queries=[{**q, "has_exact_vector": cached_vector(q["query"], qwen) is not None} for q in queries],
                label_review_template=[{"case_id": row["case"]["case_id"], "label_hash": row["label_hash"],
                    "decision": "pending_review", "reviewer": None, "reviewed_at": None} for row in rows],
                agent_quality={"executed": 0, "status": "not_run", "reason": "new_calls_require_authorization"})
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        write_json(output, report)
    print(json.dumps({"output": str(output), "summary": report.get("summary"),
                      "stage": args.stage, "row_count": len(report["rows"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
