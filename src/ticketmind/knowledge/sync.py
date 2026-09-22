"""Explicit, serialized reconciliation. No distributed transaction or background worker."""
import hashlib
import logging
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import select, text

from ticketmind.knowledge.models import KnowledgeCase, KnowledgeDataset, KnowledgeEmbedding
from ticketmind.knowledge.seed import cache_vector, content_hash, embedding_identity
from ticketmind.knowledge.service import read_case, require_case
from ticketmind.retrieval.schemas import RetrievalError

logger = logging.getLogger(__name__)


@contextmanager
def sync_lock(factory, dataset):
    # Session-level lock releases on crash. A dedicated connection is retained,
    # but there is no database transaction/row lock during network calls.
    with factory() as session:
        engine = session.get_bind()
    with engine.connect() as connection:
        schema = connection.execute(text("SELECT current_schema()")).scalar_one()
        lock = int.from_bytes(hashlib.sha256(f"{schema}:knowledge-index:{dataset}".encode()).digest()[:8], "big", signed=True)
        acquired = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock}).scalar_one()
        connection.commit()
        if not acquired:
            raise RetrievalError("knowledge_sync_busy")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})
            connection.commit()


class KnowledgeSync:
    def __init__(self, factory, index, qwen, *, embedding_factory=None, embedding_budget=0):
        self.factory, self.index, self.qwen = factory, index, qwen
        self.embedding_factory, self.embedding_budget = embedding_factory, embedding_budget
        self.embedding_calls = 0
        self.orphans_removed = 0

    def vector(self, case):
        from ticketmind.retrieval.case_collection import TEXT_MAX_BYTES
        if len(case.content.encode()) > TEXT_MAX_BYTES:
            raise RetrievalError("knowledge_index_text_too_long")
        if self.qwen is None:
            raise RetrievalError("embedding_configuration_unavailable")
        if self.qwen.embedding_model != "text-embedding-v4":
            raise RetrievalError("embedding_model_mismatch")
        identity = embedding_identity(self.qwen, case.content)
        with self.factory() as session:
            cached = session.get(KnowledgeEmbedding, content_hash(identity))
            if cached:
                if cached.identity != identity:
                    raise RetrievalError("embedding_cache_mismatch")
                return cached.vector
        if self.embedding_calls >= self.embedding_budget or self.embedding_factory is None:
            raise RetrievalError("embedding_cache_missing")
        # Increment before sending. Exceptions consume budget and are not retried.
        self.embedding_calls += 1
        vector = self.embedding_factory().embed_documents([case.content])[0]
        with self.factory() as session, session.begin():
            cache_vector(session, identity, vector)
        return vector

    def one(self, dataset_version, source_id, *, repair_active=False):
        with sync_lock(self.factory, dataset_version):
            return self._one(dataset_version, source_id, repair_active=repair_active)

    def _text_dataset(self, dataset):
        """Switch to a vector-free BM25 collection only after copying active text.

        Called under the dataset sync lock. Until the database pointer commits,
        retrieval continues using the existing combined index. A failed copy is
        harmless and can be resumed through idempotent upserts, with no models.
        """
        existed = self.index.text_exists(dataset)
        if dataset.bm25_collection_name and not existed:
            # Persist the incomplete rebuild before recreating the collection.
            # A crash midway must not make a partial collection look ready.
            with self.factory() as session, session.begin():
                current = session.get(KnowledgeDataset, dataset.version, with_for_update=True)
                current.bm25_ready = False
            dataset.bm25_ready = False
        name = self.index.ensure_text(dataset)
        if dataset.bm25_collection_name:
            if dataset.bm25_collection_name != name:
                raise RetrievalError("bm25_collection_version_mismatch")
            if existed and dataset.bm25_ready:
                return dataset
        last = None
        while True:
            with self.factory() as session:
                query = select(KnowledgeCase).where(KnowledgeCase.dataset_version == dataset.version,
                                                   KnowledgeCase.status == "active")
                if last is not None:
                    query = query.where(KnowledgeCase.source_id > last)
                cases = session.scalars(query.order_by(KnowledgeCase.source_id).limit(100)).all()
            if not cases:
                break
            for case in cases:
                if not self.index.text_matches(dataset, case, name):
                    self.index.upsert_text(dataset, case, name)
            last = cases[-1].source_id
        with self.factory() as session, session.begin():
            current = session.get(KnowledgeDataset, dataset.version, with_for_update=True)
            current.bm25_collection_name = name
            current.bm25_ready = True
        dataset.bm25_collection_name = name
        dataset.bm25_ready = True
        return dataset

    def _one(self, dataset_version, source_id, *, repair_active=False):
        with self.factory() as session:
            case = require_case(session, dataset_version, source_id)
            dataset = session.get(KnowledgeDataset, dataset_version)
            if case.status == "active" and not repair_active and not case.dense_index_error:
                return read_case(case)
            observed_version = case.version
        dense_hash, dense_error = None, None
        try:
            if case.status == "retired":
                self.index.delete(dataset, case)
            else:
                if hasattr(self.index, "ensure_text"):
                    dataset = self._text_dataset(dataset)
                    if not self.index.text_matches(dataset, case, dataset.bm25_collection_name):
                        self.index.upsert_text(dataset, case, dataset.bm25_collection_name)
                    # BM25 publication survives missing vectors or a Dense failure.
                    # Existing exact caches/explicit budgets remain the only way
                    # to obtain document vectors; HTTP has no paid fallback.
                    try:
                        self.index.ensure(dataset)
                        if not self.index.matches(dataset, case):
                            self.index.upsert(dataset, case, self.vector(case))
                        dense_hash = case.content_hash
                    except Exception as exc:
                        dense_error = exc.code if isinstance(exc, RetrievalError) else "dense_index_failed"
                else:
                    # Explicit demo adapters retain their combined-index contract.
                    self.index.ensure(dataset)
                    if not self.index.matches(dataset, case):
                        self.index.upsert(dataset, case, self.vector(case))
                    dense_hash = case.content_hash
            with self.factory() as session, session.begin():
                current = require_case(session, dataset_version, source_id, lock=True)
                # A retire/retry racing with network IO wins; never reactivate it.
                if current.version == observed_version:
                    if current.status != "retired":
                        current.status, current.indexed_hash = "active", current.content_hash
                        current.indexed_at = datetime.now(UTC)
                    else:
                        current.indexed_hash = None
                    current.dense_indexed_hash, current.dense_index_error = dense_hash, dense_error
                    current.index_error = None
                    current.version += 1
                    session.flush()
                return read_case(current)
        except Exception as exc:
            code = exc.code if isinstance(exc, RetrievalError) else "knowledge_index_failed"
            logger.error("knowledge_sync_failed dataset=%s source=%s error=%s", dataset_version, source_id, code)
            # If PG is unavailable this also fails; the old pending state remains
            # recoverable. A successful Milvus write is detected on the next run.
            with self.factory() as session, session.begin():
                current = require_case(session, dataset_version, source_id, lock=True)
                if current.version == observed_version:
                    if current.status != "retired":
                        current.status = "index_failed"
                    current.index_error = code
                    current.version += 1
                    session.flush()
                return read_case(current)

    def _prune_orphans(self, dataset_version):
        with self.factory() as session:
            dataset = session.get(KnowledgeDataset, dataset_version)
            if dataset is None:
                raise RetrievalError("knowledge_dataset_missing")
        removed = 0
        try:
            collections = [None]
            if dataset.bm25_collection_name and hasattr(self.index, "ensure_text"):
                collections.append(dataset.bm25_collection_name)
            for name in collections:
                options = {"collection_name": name} if name else {}
                for source_ids in self.index.iter_source_id_batches(dataset, **options):
                    with self.factory() as session:
                        existing = set(session.scalars(select(KnowledgeCase.source_id).where(
                            KnowledgeCase.dataset_version == dataset_version,
                            KnowledgeCase.source_id.in_(source_ids))).all())
                    orphans = [source_id for source_id in source_ids if source_id not in existing]
                    if orphans:
                        self.index.delete_source_ids(dataset, orphans, **options)
                        removed += len(orphans)
                        logger.warning("knowledge_orphans_removed dataset=%s count=%s", dataset_version, len(orphans))
        except Exception as exc:
            code = exc.code if isinstance(exc, RetrievalError) else "orphan_index_cleanup_failed"
            logger.error("knowledge_orphan_cleanup_failed dataset=%s error=%s", dataset_version, code)
            raise RetrievalError(code) from exc
        return removed

    def reconcile(self, dataset_version, *, repair_active=False, limit=100):
        self.orphans_removed = 0
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self.factory() as session:
            query = select(KnowledgeCase.source_id).where(KnowledgeCase.dataset_version == dataset_version)
            if not repair_active:
                from sqlalchemy import or_, and_
                query = query.where(or_(KnowledgeCase.status.in_(["pending_index", "index_failed"]),
                    and_(KnowledgeCase.status == "active", KnowledgeCase.dense_index_error.is_not(None)),
                    and_(KnowledgeCase.status == "retired", KnowledgeCase.index_error.is_not(None))))
            ids = session.scalars(query.order_by(KnowledgeCase.updated_at, KnowledgeCase.source_id).limit(limit)).all()
        with sync_lock(self.factory, dataset_version):
            if not ids:
                with self.factory() as session:
                    dataset = session.get(KnowledgeDataset, dataset_version)
                    if dataset is None:
                        raise RetrievalError("knowledge_dataset_missing")
                self.index.ensure(dataset)
            results = [self._one(dataset_version, source_id, repair_active=repair_active) for source_id in ids]
            self.orphans_removed = self._prune_orphans(dataset_version)
            return results
