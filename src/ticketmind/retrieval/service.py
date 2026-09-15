"""One read-only retrieval entry for first search, tools and evaluation."""
from time import monotonic

from ticketmind.retrieval.bm25 import search_bm25
from ticketmind.retrieval.case_collection import CASE_COLLECTION
from ticketmind.retrieval.dense import search_case_vectors
from ticketmind.retrieval.hybrid import reciprocal_rank_fusion
from ticketmind.retrieval.schemas import EvidenceHit, RetrievalError
from ticketmind.retrieval.versioned_collection import validate_collection, selected_collection


def retrieve_cases(query, *, client, embeddings, corpus, config, timeout, record, model="text-embedding-v4"):
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
