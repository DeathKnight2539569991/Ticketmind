from ticketmind.retrieval.schemas import EvidenceHit, RetrievalError


def search_bm25(client, query, *, collection, corpus, top_k, timeout):
    results = client.search(collection_name=collection, data=[query.lower()], anns_field="sparse",
        search_params={"metric_type": "BM25"}, output_fields=["source_id", "text", "corpus_version"],
        limit=top_k, timeout=timeout, consistency_level="Strong")
    if len(results) != 1:
        raise RetrievalError("bm25_invalid_response")
    hits = []
    for rank, row in enumerate(results[0], 1):
        entity = row["entity"]
        if entity["corpus_version"] != corpus.version or entity["source_id"] not in corpus.cases:
            raise RetrievalError("corpus_version_mismatch")
        hits.append(EvidenceHit(source_id=entity["source_id"], text=entity["text"],
            title=corpus.cases[entity["source_id"]].request.subject, corpus_version=corpus.version,
            rank=rank, bm25_rank=rank, bm25_score=row["distance"], retrieval_mode="bm25"))
    try:
        corpus.evidence(hits)
    except ValueError as exc:
        raise RetrievalError("corpus_version_mismatch") from exc
    return hits
