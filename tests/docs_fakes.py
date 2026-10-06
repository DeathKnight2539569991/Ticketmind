"""Explicit deterministic Docs capability used by Agent tests; no default DB access."""
import hashlib
from ticketmind.retrieval.schemas import DocEvidenceHit


def doc_hit(text="product documentation", *, chunk="product:001:001", version="synthetic-product-docs-v1"):
    return DocEvidenceHit(source_id="docs:" + chunk, doc_id="product", chunk_id=chunk,
        title="Synthetic Product", section="Configuration", text=text, score=1.0,
        docs_version=version, content_hash=hashlib.sha256(text.encode()).hexdigest(),
        synthetic=True, mode="bm25", rank=1)


class FakeDocStore:
    def __init__(self, version="synthetic-product-docs-v1"):
        self.version = version
        self.catalog_hash = "frozen-docs-catalog"
    def metadata(self):
        return {"docs_version": self.version, "status": "ready", "synthetic": True,
                "catalog_hash": self.catalog_hash, "collection": "docs_bm25_test"}
