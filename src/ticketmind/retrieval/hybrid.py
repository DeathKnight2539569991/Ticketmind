from ticketmind.retrieval.schemas import EvidenceHit


def reciprocal_rank_fusion(dense: list[EvidenceHit], bm25: list[EvidenceHit], *, top_k: int, k: int = 60):
    """Fuse ranks, never raw scores. Ties have a stable source-id order."""
    if top_k < 1 or k < 1:
        raise ValueError("top_k 和 RRF k 必须为正数")
    merged = {}
    for channel, hits in (("dense", dense), ("bm25", bm25)):
        seen = set()
        for hit in hits:
            if hit.source_id in seen:
                continue
            seen.add(hit.source_id)
            rank = len(seen)
            if hit.source_id not in merged:
                merged[hit.source_id] = hit.model_copy(update={"retrieval_mode": "hybrid", "fusion_score": 0.0})
            current = merged[hit.source_id]
            if (getattr(current, "text", None), current.corpus_version, getattr(current, "content_hash", None)) != (getattr(hit, "text", None), hit.corpus_version, getattr(hit, "content_hash", None)):
                raise ValueError("融合来源版本不一致")
            setattr(current, f"{channel}_score", getattr(hit, f"{channel}_score"))
            setattr(current, f"{channel}_rank", rank)
            current.fusion_score += 1.0 / (k + rank)
    ordered = sorted(merged.values(), key=lambda hit: (-hit.fusion_score, hit.source_id))[:top_k]
    return [hit.model_copy(update={"rank": rank}) for rank, hit in enumerate(ordered, 1)]
