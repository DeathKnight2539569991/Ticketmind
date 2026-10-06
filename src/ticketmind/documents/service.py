"""Read-only product-doc retrieval and explicit readiness state."""
import hashlib
import json
from sqlalchemy import func, select

from ticketmind.documents.index import MilvusDocsIndex
from ticketmind.documents.models import DEFAULT_DOCS_VERSION, DocsDataset, Document, DocumentChunk
from ticketmind.retrieval.schemas import DocEvidenceHit, RetrievalError


class DocStore:
    def __init__(self, factory, version: str = DEFAULT_DOCS_VERSION):
        self.factory = factory
        self.version = version

    def metadata(self) -> dict:
        """Safe for Case-only startup: configured version remains visible before import."""
        with self.factory() as session:
            dataset = session.get(DocsDataset, self.version)
            if dataset is None:
                return {"docs_version": self.version, "status": "not_ready", "synthetic": True,
                        "catalog_hash": None}
            catalog = session.execute(select(Document.doc_id, Document.title, Document.content_hash).where(
                Document.dataset_version == self.version).order_by(Document.doc_id)).all()
            fingerprint = hashlib.sha256(json.dumps([list(row) for row in catalog], ensure_ascii=False,
                separators=(",", ":")).encode()).hexdigest()
            total = session.scalar(select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.dataset_version == self.version))
            ready_count = session.scalar(select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.dataset_version == self.version,
                DocumentChunk.indexed_hash == DocumentChunk.content_hash))
            return {"docs_version": self.version, "status": "ready" if total and ready_count == total else "not_ready",
                    "synthetic": dataset.synthetic, "collection": dataset.collection_name,
                    "catalog_hash": fingerprint}

    def dataset(self) -> DocsDataset:
        with self.factory() as session:
            dataset = session.get(DocsDataset, self.version)
            if dataset is None:
                raise RetrievalError("docs_dataset_missing")
            if not dataset.synthetic:
                raise RetrievalError("docs_dataset_kind_unsupported")
            return dataset

    def hydrate(self, rows, *, mode: str, rank_start: int = 1, record: dict | None = None) -> list[DocEvidenceHit]:
        ids = [row["entity"]["source_id"].removeprefix("docs:") for row in rows]
        if not ids:
            return []
        with self.factory() as session:
            pairs = session.execute(select(DocumentChunk, Document).join(Document,
                (DocumentChunk.dataset_version == Document.dataset_version) & (DocumentChunk.doc_id == Document.doc_id))
                .where(DocumentChunk.dataset_version == self.version, DocumentChunk.chunk_id.in_(ids))).all()
            by_id = {chunk.chunk_id: (chunk, document) for chunk, document in pairs}
        result = []
        for row in rows:
            entity = row["entity"]
            source_id = entity["source_id"]
            pair = by_id.get(source_id.removeprefix("docs:")) if source_id.startswith("docs:") else None
            error = ("docs_version_mismatch" if entity.get("docs_version") != self.version else
                     "docs_chunk_missing" if pair is None else
                     "docs_chunk_hash_mismatch" if entity.get("content_hash") != pair[0].content_hash else
                     "docs_chunk_not_indexed" if pair[0].indexed_hash != pair[0].content_hash else None)
            if error:
                if record is not None:
                    record.setdefault("inconsistencies", []).append({"source_id": source_id, "error": error})
                continue
            chunk, document = pair
            result.append(DocEvidenceHit(source_id=source_id, doc_id=chunk.doc_id, chunk_id=chunk.chunk_id,
                title=document.title, section=chunk.section, text=chunk.text, score=float(row["distance"]),
                docs_version=self.version, content_hash=chunk.content_hash, synthetic=True,
                mode=mode, rank=rank_start + len(result)))
        return result


def retrieve_docs(query: str, *, client, store: DocStore, mode: str = "bm25",
                  top_k: int = 3, candidate_k: int = 20, timeout=10,
                  record: dict | None = None) -> list[DocEvidenceHit]:
    """BM25 uses no embeddings. Dense has an explicit readiness error."""
    if not query.strip():
        raise RetrievalError("retrieval_empty_query")
    if mode not in ("bm25", "dense", "hybrid"):
        raise ValueError("unknown_docs_retrieval_mode")
    if not 1 <= top_k <= 100 or not 1 <= candidate_k <= 100:
        raise ValueError("invalid_docs_search_limit")
    record = record if record is not None else {}
    record.update(docs_version=store.version, retrieval_mode=mode, channels={})
    dataset = store.dataset()
    record["collection"] = dataset.collection_name
    if mode == "dense":
        record["channels"]["dense"] = {"status": "skipped", "reason": "docs_dense_index_not_ready"}
        raise RetrievalError("docs_dense_index_not_ready")
    if mode == "hybrid":
        record["channels"]["dense"] = {"status": "skipped", "reason": "docs_dense_index_not_ready"}
    index = MilvusDocsIndex(client, timeout)
    target = max(top_k, candidate_k) if mode == "hybrid" else top_k
    seen: set[str] = set()
    results: list[DocEvidenceHit] = []
    offset = 0
    while len(results) < target and offset < 1000:
        limit = min(max(target, candidate_k), 1000 - offset)
        page = index.search(dataset, query, limit=limit, offset=offset)
        if not page:
            break
        unique = []
        for row in page:
            source_id = row["entity"]["source_id"]
            if source_id not in seen:
                seen.add(source_id)
                unique.append(row)
        # Hybrid currently has no document vectors; evidence reports the channel actually used.
        results.extend(store.hydrate(unique, mode="bm25", rank_start=len(results) + 1, record=record))
        if len(page) < limit or not unique:
            break
        offset += limit
    record["channels"]["bm25"] = {"status": "succeeded", "valid_candidates": len(results)}
    final = [hit.model_copy(update={"rank": rank}) for rank, hit in enumerate(results[:top_k], 1)]
    record["result_hits"] = [hit.model_dump() for hit in final]
    return final
