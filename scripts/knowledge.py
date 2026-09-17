"""Explicit seed/cache import/reconcile; default external model budget is zero."""
import argparse
import json
from pathlib import Path

from ticketmind.core.config import ProcessingSettings, QwenSettings, MilvusSettings
from ticketmind.db.session import SesstionLocal
from ticketmind.knowledge.index import MilvusKnowledgeIndex
from ticketmind.knowledge.seed import seed_knowledge, import_seed_vectors, ensure_production
from ticketmind.knowledge.sync import KnowledgeSync
from ticketmind.retrieval.milvus_client import build_milvus_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seed", "import-cache", "reconcile"])
    parser.add_argument("--dataset", default="production-v1")
    parser.add_argument("--source-id")
    parser.add_argument("--repair-active", action="store_true", help="检查 ACTIVE 的索引缺失；复用缓存修复")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--embedding-budget", type=int, default=0, help="本进程最多新增文档向量请求；仅在另行授权后使用")
    parser.add_argument("--ledger", type=Path, help="正预算必填：持久化调用台账，禁止删除重置预算")
    args = parser.parse_args()
    if args.embedding_budget < 0 or (args.embedding_budget and not args.ledger):
        parser.error("正向量预算需要 --ledger，且预算不能为负")
    config = ProcessingSettings()
    if args.action == "seed":
        print(json.dumps(seed_knowledge(SesstionLocal, config.corpus_path)))
        return
    qwen = QwenSettings()
    if args.action == "import-cache":
        print({"imported": import_seed_vectors(SesstionLocal, config.corpus_path, qwen, Path("data/cache/embeddings")), "embedding_calls": 0})
        return
    with SesstionLocal() as session, session.begin():
        ensure_production(session)
    from contextlib import nullcontext
    from ticketmind.agent.dev_acceptance import AttemptLedger, acceptance_lock, CATEGORIES
    from ticketmind.agent.runtime import build_budgeted_embeddings
    lock = acceptance_lock(args.ledger.parent) if args.embedding_budget else nullcontext()
    with lock:
        embedding_factory = None
        if args.embedding_budget:
            ceilings = {category: 0 for category in CATEGORIES}
            ceilings["initial_embedding"] = args.embedding_budget
            ledger = AttemptLedger(args.ledger, ceilings)
            class BudgetedDocumentEmbedding:
                def embed_documents(self, documents):
                    from ticketmind.knowledge.seed import content_hash, embedding_identity
                    fingerprint = content_hash(embedding_identity(qwen, documents[0]))
                    def call(record):
                        return build_budgeted_embeddings(qwen, lambda: 30,
                            lambda usage: record.update(usage=usage)).embed_documents(documents)
                    return ledger.attempt("initial_embedding", fingerprint, call)
            embedding_factory = BudgetedDocumentEmbedding
        milvus = MilvusSettings()
        client = build_milvus_client(milvus)
        try:
            sync = KnowledgeSync(SesstionLocal, MilvusKnowledgeIndex(client, milvus.timeout_seconds), qwen,
                embedding_factory=embedding_factory, embedding_budget=args.embedding_budget)
            results = ([sync.one(args.dataset, args.source_id, repair_active=args.repair_active)] if args.source_id else
                       sync.reconcile(args.dataset, repair_active=args.repair_active, limit=args.limit))
            print(json.dumps({"results": [{k: row[k] for k in ("source_id", "status", "index_error", "version")} for row in results],
                              "embedding_calls": sync.embedding_calls}, ensure_ascii=False))
            if any(row["index_error"] for row in results):
                raise SystemExit(1)
        finally:
            client.close()


if __name__ == "__main__":
    main()
