import pytest
from pydantic import ValidationError

from ticketmind.retrieval.schemas import DocEvidenceHit


def test_doc_evidence_hit_preserves_doc_and_chunk_identity():
    hit = DocEvidenceHit(source_id="doc-7f3a", doc_id="auth-guide", chunk_id="auth-guide:3",
                        title="认证指南", section="令牌刷新", text="刷新令牌前先校验签名。",
                        score=0.8, docs_version="docs-v1", content_hash="abc123",
                        synthetic=True, mode="bm25", rank=2)
    assert hit.source_id == "doc-7f3a"
    assert hit.model_dump()["chunk_id"] == "auth-guide:3"


def test_doc_evidence_hit_rejects_invalid_rank_and_non_finite_score():
    base = {"source_id": "doc-a", "doc_id": "guide", "chunk_id": "guide:1", "title": "指南",
            "text": "正文", "score": 0.5, "docs_version": "v1", "content_hash": "hash",
            "synthetic": False, "mode": "hybrid", "rank": 1}
    with pytest.raises(ValidationError):
        DocEvidenceHit.model_validate({**base, "rank": 0})
    with pytest.raises(ValidationError):
        DocEvidenceHit.model_validate({**base, "score": float("inf")})
