"""Short, controlled PostgreSQL reads. Never load a full runtime corpus."""
import logging

from sqlalchemy import select

from ticketmind.knowledge.models import KnowledgeCase, KnowledgeDataset
from ticketmind.retrieval.schemas import KnowledgeEvidenceHit, RetrievalError

logger = logging.getLogger(__name__)


class KnowledgeStore:
    def __init__(self, factory, dataset_version):
        self.factory, self.version = factory, dataset_version

    def dataset(self):
        with self.factory() as session:
            value = session.get(KnowledgeDataset, self.version)
            if value is None:
                raise RetrievalError("knowledge_dataset_missing")
            return value

    def read_many(self, source_ids):
        with self.factory() as session:
            return {case.source_id: case for case in session.scalars(select(KnowledgeCase).where(
                KnowledgeCase.dataset_version == self.version, KnowledgeCase.source_id.in_(source_ids))).all()}

    def get_case_detail(self, source_id):
        case = self.read_many([source_id]).get(source_id)
        if case is None or case.status != "active":
            raise RetrievalError("knowledge_source_unavailable")
        if case.source_type == "ticket" and "article" in case.source:
            return {"source_id": source_id, "title": case.title, "article": case.source["article"]}
        return case.source

    def has_dense_sources(self):
        with self.factory() as session:
            return session.scalar(select(KnowledgeCase.source_id).where(
                KnowledgeCase.dataset_version == self.version, KnowledgeCase.status == "active",
                KnowledgeCase.dense_indexed_hash == KnowledgeCase.content_hash).limit(1)) is not None

    def hydrate(self, hits, record):
        cases = self.read_many([hit.source_id for hit in hits]) if hits else {}
        result = []
        for hit in hits:
            case = cases.get(hit.source_id)
            error = ("missing_postgres_source" if case is None else
                     "inactive_knowledge" if case.status != "active" else
                     "dense_index_not_ready" if hit.retrieval_mode == "dense" and case.dense_indexed_hash != case.content_hash else
                     "knowledge_revision_mismatch" if hit.content_hash and hit.content_hash != case.content_hash else None)
            if error:
                issue = {"source_id": hit.source_id, "dataset_version": self.version, "error": error}
                record.setdefault("inconsistencies", []).append(issue)
                logger.warning("knowledge_inconsistency %s", issue)
                continue
            result.append(KnowledgeEvidenceHit(source_id=hit.source_id, corpus_version=self.version,
                title=case.title, text=case.content, rank=hit.rank, dense_rank=hit.dense_rank,
                bm25_rank=hit.bm25_rank, dense_score=hit.dense_score, bm25_score=hit.bm25_score,
                fusion_score=hit.fusion_score, retrieval_mode=hit.retrieval_mode,
                knowledge_revision=case.revision, content_hash=case.content_hash,
                synthetic=case.source_type == "synthetic", metadata=case.case_metadata))
        return result

    def evidence(self, hits):
        # Hits are immutable hydrated snapshots, already verified against PG.
        return [hit.model_dump() for hit in hits]
