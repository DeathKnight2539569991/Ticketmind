"""One read-only retrieval entry for first search, tools and evaluation."""
from time import monotonic

from ticketmind.retrieval.bm25 import search_bm25
from ticketmind.retrieval.case_collection import CASE_COLLECTION
from ticketmind.retrieval.dense import search_case_vectors
from ticketmind.retrieval.hybrid import reciprocal_rank_fusion
from ticketmind.retrieval.schemas import EvidenceHit, RetrievalError
from ticketmind.retrieval.versioned_collection import validate_collection, selected_collection


def retrieve_cases(query, *, client, embeddings, corpus, config, timeout, record, model="text-embedding-v4"):
    from ticketmind.knowledge.repository import KnowledgeStore
    if isinstance(corpus, KnowledgeStore):
        return retrieve_knowledge(query, client=client, embeddings=embeddings, store=corpus,
                                  config=config, timeout=timeout, record=record, model=model)
    mode = config.retrieval_mode
    if mode not in ("dense", "bm25", "hybrid"):
        raise ValueError("未知检索模式")
    if not query.strip():
        raise RetrievalError("retrieval_empty_query")
    budget = timeout if callable(timeout) else lambda: timeout
    record.update(retrieval_mode=mode, channels={}, candidate_k=config.retrieval_candidate_k,
                  top_k=config.retrieval_top_k, rrf_k=config.retrieval_rrf_k)
    collection = selected_collection(corpus, mode, model)
    if model != "text-embedding-v4":
        raise RetrievalError("embedding_model_mismatch")
    if collection != CASE_COLLECTION:
        try:
            collection = validate_collection(client, corpus, model=model, timeout=budget, require_data=True)
        except RetrievalError:
            raise
        except Exception as exc:
            raise RetrievalError("collection_validation_failed") from exc
    record["collection"] = collection
    results = {}
    for channel in (["dense", "bm25"] if mode == "hybrid" else [mode]):
        audit = {"status": "failed", "candidates": []}
        record["channels"][channel] = audit
        started = monotonic()
        try:
            limit = max(config.retrieval_candidate_k, config.retrieval_top_k) if mode == "hybrid" else config.retrieval_top_k
            audit["limit"] = limit
            if channel == "dense":
                vector = embeddings.embed_query(query)
                raw = search_case_vectors(client, vector, top_k=limit, timeout=budget(), collection_name=collection,
                    expected_corpus_version=corpus.version if collection != CASE_COLLECTION else None)
                try:
                    corpus.evidence(raw)
                except ValueError as exc:
                    raise RetrievalError("corpus_version_mismatch") from exc
                hits = [EvidenceHit(source_id=hit.source_id, text=hit.text, title=corpus.cases[hit.source_id].request.subject,
                    corpus_version=corpus.version, rank=rank, dense_rank=rank, dense_score=hit.score,
                    retrieval_mode="dense") for rank, hit in enumerate(raw, 1)]
            else:
                hits = search_bm25(client, query, collection=collection, corpus=corpus, top_k=limit, timeout=budget())
            audit["candidates"] = [hit.model_dump() for hit in hits]
            if not hits:
                raise RetrievalError(f"{channel}_empty_results")
            audit["status"] = "succeeded"
            results[channel] = hits
        except Exception as exc:
            audit["error"] = exc.code if isinstance(exc, RetrievalError) else f"{channel}_retrieval_failed"
            raise RetrievalError(audit["error"]) from exc
        finally:
            audit["duration_ms"] = round((monotonic() - started) * 1000)
    hits = reciprocal_rank_fusion(results["dense"], results["bm25"], top_k=config.retrieval_top_k,
                                 k=config.retrieval_rrf_k) if mode == "hybrid" else results[mode]
    record["result_evidence"] = corpus.evidence(hits)
    return hits


def retrieve_knowledge(query, *, client, embeddings, store, config, timeout, record, model):
    from ticketmind.knowledge.index import MilvusKnowledgeIndex, validate_vector
    from ticketmind.retrieval.schemas import IndexHit
    if not query.strip():
        raise RetrievalError("retrieval_empty_query")
    budget = timeout if callable(timeout) else lambda: timeout
    mode = config.retrieval_mode
    record.update(retrieval_mode=mode, channels={}, candidate_k=config.retrieval_candidate_k,
                  top_k=config.retrieval_top_k, rrf_k=config.retrieval_rrf_k)
    dataset = store.dataset()
    if model != dataset.manifest["embedding_model"]:
        raise RetrievalError("embedding_model_mismatch")
    record["collection"] = dataset.collection_name
    MilvusKnowledgeIndex(client, budget).validate(dataset)
    results = {}
    for channel in (["dense", "bm25"] if mode == "hybrid" else [mode]):
        audit = {"status": "failed", "candidates": []}
        record["channels"][channel] = audit
        started = monotonic()
        try:
            limit = max(config.retrieval_candidate_k, config.retrieval_top_k) if mode == "hybrid" else config.retrieval_top_k
            data = embeddings.embed_query(query) if channel == "dense" else query.lower()
            if channel == "dense":
                validate_vector(data)
            fields = ["source_id", "corpus_version"]
            if dataset.manifest["schema_version"] == 2:
                fields.append("content_hash")
            raw = client.search(collection_name=dataset.collection_name, data=[data],
                anns_field="embedding" if channel == "dense" else "sparse",
                search_params={"metric_type": "COSINE" if channel == "dense" else "BM25"},
                output_fields=fields, limit=limit, timeout=budget(), consistency_level="Strong")
            if len(raw) != 1:
                raise RetrievalError(f"{channel}_invalid_response")
            hits = []
            for rank, row in enumerate(raw[0], 1):
                entity = row["entity"]
                if entity.get("corpus_version") != store.version:
                    raise RetrievalError("corpus_version_mismatch")
                if dataset.manifest["schema_version"] == 2 and not entity.get("content_hash"):
                    raise RetrievalError("knowledge_index_hash_missing")
                hits.append(IndexHit(source_id=entity["source_id"], corpus_version=store.version,
                    content_hash=entity.get("content_hash"), rank=rank, retrieval_mode=channel,
                    **{f"{channel}_score": row["distance"], f"{channel}_rank": rank}))
            audit.update(status="succeeded", limit=limit, candidates=[hit.model_dump() for hit in hits])
            results[channel] = hits
        except Exception as exc:
            audit["error"] = exc.code if isinstance(exc, RetrievalError) else f"{channel}_retrieval_failed"
            raise RetrievalError(audit["error"]) from exc
        finally:
            audit["duration_ms"] = round((monotonic() - started) * 1000)
    hits = reciprocal_rank_fusion(results["dense"], results["bm25"], top_k=config.retrieval_top_k,
                                 k=config.retrieval_rrf_k) if mode == "hybrid" else results[mode]
    hydrated = store.hydrate(hits, record)
    record["result_evidence"] = store.evidence(hydrated)
    return hydrated
