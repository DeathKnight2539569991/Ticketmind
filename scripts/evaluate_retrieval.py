"""Real Milvus comparison, exact query-vector caches only; never calls a model."""
import argparse
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path
from time import monotonic

from ticketmind.agent.run_cache import QueryVectorCache, load_cache, query_fingerprint
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval.milvus_client import build_milvus_client
from ticketmind.retrieval.service import retrieve_cases
from ticketmind.retrieval.schemas import RetrievalError
from ticketmind.retrieval.versioned_collection import analyzer_for, collection_for, manifest_for

ROOT = Path(__file__).resolve().parents[1]


class CachedQuery:
    def __init__(self, query, vector):
        self.query, self.vector = query, vector

    def embed_query(self, query):
        if query != self.query or self.vector is None:
            raise RuntimeError("无精确匹配查询向量，禁止调用模型")
        return self.vector


def load_queries(path, settings, *, need_vectors):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not rows or len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("查询集为空或 ID 重复")
    for row in rows:
        if not row["query"].strip() or row["label_status"] not in ("pending_review", "reviewed"):
            raise ValueError("查询或标签状态无效")
        row["vector"] = None
        if need_vectors:
            cache_path = row.get("cache_path")
            if not cache_path:
                raise ValueError(f"{row['case_id']} 缺少精确向量缓存；此入口不生成向量")
            cache = load_cache(ROOT / cache_path, QueryVectorCache,
                expected_fingerprint=query_fingerprint(settings=settings, query=row["query"]))
            if cache is None or cache.query != row["query"] or cache.model != settings.embedding_model:
                raise ValueError(f"{row['case_id']} 查询缓存缺失或不匹配")
            row["vector"] = cache.vector
    return rows


def metrics(rows, mode, *, label_status):
    selected = [row for row in rows if row["label_status"] == label_status and row["relevant_source_ids"]]
    hit_count, recall, reciprocal = 0, 0.0, 0.0
    for row in selected:
        ids = [hit["source_id"] for hit in row["modes"][mode].get("hits", [])]
        relevant = set(row["relevant_source_ids"])
        found = relevant.intersection(ids)
        hit_count += bool(found)
        recall += len(found) / len(relevant)
        reciprocal += next((1 / rank for rank, source_id in enumerate(ids, 1) if source_id in relevant), 0)
    n = len(selected)
    return {"denominator": n, "hit_count": hit_count, "hit_at_k": hit_count / n if n else None,
            "recall_at_k": recall / n if n else None, "mrr_at_k": reciprocal / n if n else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=ROOT / "data/synthetic/m3/cached_queries.jsonl")
    parser.add_argument("--modes", nargs="+", choices=["dense", "bm25", "hybrid"], default=["dense", "bm25", "hybrid"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    qwen, milvus = QwenSettings(), MilvusSettings()
    config = ProcessingSettings(retrieval_top_k=args.top_k, retrieval_candidate_k=args.candidate_k,
                                retrieval_rrf_k=args.rrf_k)
    corpus = load_sources(config.corpus_path)
    queries = load_queries(args.queries, qwen, need_vectors=any(mode != "bm25" for mode in args.modes))
    for row in queries:
        if set(row["relevant_source_ids"]) - corpus.cases.keys():
            raise ValueError("标签包含未知来源")
    if args.check_only:
        print(f"预检通过：{len(queries)} 条；模型调用 0；未访问 Milvus")
        return
    report = {"created_at": datetime.now(UTC).isoformat(), "synthetic": True, "dataset_kind": "development_diagnostics",
        "query_set_sha256": hashlib.sha256(args.queries.read_bytes()).hexdigest(), "manifest": manifest_for(corpus),
        "collection": collection_for(corpus), "top_k": args.top_k, "candidate_k": args.candidate_k,
        "rrf_k": args.rrf_k, "model_calls": 0, "embedding_source": "exact_cache" if any(m != "bm25" for m in args.modes) else "unused",
        "rows": [], "summary": {}}
    client = build_milvus_client(milvus)
    try:
        report["server_version"] = client.get_server_version(timeout=milvus.timeout_seconds)
        probes = ["中文工单登录失败", "E_TIMEOUT E_CURSOR_INVALID E_RATE_LIMIT", "Python 3.12 3.11 HTTP_PROXY"]
        report["analyzer_probes"] = [{"text": query, "tokens": client.run_analyzer(texts=[query.lower()],
            analyzer_params=analyzer_for(corpus), timeout=milvus.timeout_seconds)[0].tokens} for query in probes]
        for query in queries:
            row = {key: value for key, value in query.items() if key != "vector"}
            row["modes"] = {}
            for mode in args.modes:
                audit = {}
                started = monotonic()
                try:
                    hits = retrieve_cases(query["query"], client=client,
                        embeddings=CachedQuery(query["query"], query["vector"]), corpus=corpus,
                        config=config.model_copy(update={"retrieval_mode": mode}), timeout=milvus.timeout_seconds,
                        record=audit, model=qwen.embedding_model)
                    result = {"status": "succeeded", "hits": [hit.model_dump() for hit in hits]}
                except RetrievalError as exc:
                    result = {"status": "failed", "error": exc.code, "hits": []}
                row["modes"][mode] = {**result, "duration_ms": round((monotonic() - started) * 1000), "audit": audit}
            report["rows"].append(row)
        for mode in args.modes:
            report["summary"][mode] = {"reviewed": metrics(report["rows"], mode, label_status="reviewed"),
                "pending_review_exploratory": metrics(report["rows"], mode, label_status="pending_review"),
                "failures": sum(row["modes"][mode]["status"] == "failed" for row in report["rows"]),
                "no_relevant_source_candidates": [{"case_id": row["case_id"], "ids": [h["source_id"] for h in row["modes"][mode]["hits"]]}
                    for row in report["rows"] if not row["relevant_source_ids"]]}
    finally:
        client.close()
    output = args.output or ROOT / "data/cache/m3" / ("retrieval-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "summary": report["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
