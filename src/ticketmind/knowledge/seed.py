"""Explicit seed only; datasets and content are immutable on repeated import."""
import hashlib
import json

from sqlalchemy.dialects.postgresql import insert

from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.index import production_collection, production_manifest
from ticketmind.knowledge.models import KnowledgeCase, KnowledgeDataset, KnowledgeEmbedding, PRODUCTION_DATASET
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval.versioned_collection import collection_for, manifest_for


def content_hash(source):
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def ensure_production(session):
    session.execute(insert(KnowledgeDataset).values(version=PRODUCTION_DATASET, kind="production",
        manifest=production_manifest(), collection_name=production_collection()).on_conflict_do_nothing())


def seed_knowledge(factory, path):
    corpus = load_sources(path)
    with factory() as session, session.begin():
        ensure_production(session)
        session.execute(insert(KnowledgeDataset).values(version=corpus.version, kind="synthetic",
            manifest=manifest_for(corpus), collection_name=collection_for(corpus)).on_conflict_do_nothing())
        count = 0
        for case in corpus.cases.values():
            source = case.model_dump(mode="json")
            values = dict(dataset_version=corpus.version, source_id=case.source_id, title=case.request.subject,
                problem=case.request.body, content=build_case_text(case), source=source,
                case_metadata={"synthetic": True, "dataset_version": corpus.version}, source_type="synthetic",
                content_hash=content_hash(source))
            inserted = session.execute(insert(KnowledgeCase).values(**values).on_conflict_do_nothing().returning(KnowledgeCase.source_id)).first()
            count += bool(inserted)
            existing = session.get(KnowledgeCase, (corpus.version, case.source_id))
            if existing.content_hash != values["content_hash"] or existing.source != source or existing.content != values["content"]:
                raise ValueError("不可变数据集内容冲突；停止导入，不覆盖旧记录")
    return {"dataset_version": corpus.version, "created": count, "total": len(corpus.cases)}


def embedding_identity(qwen, text):
    return {"provider": "dashscope", "region": "cn-beijing", "workspace_id": qwen.workspace_id,
            "model": qwen.embedding_model, "dimension": 1024, "text_type": "document", "text": text}


def cache_vector(session, identity, vector):
    from ticketmind.knowledge.index import validate_vector
    validate_vector(vector)
    fingerprint = content_hash(identity)
    session.execute(insert(KnowledgeEmbedding).values(fingerprint=fingerprint, identity=identity, vector=vector).on_conflict_do_nothing())


def import_seed_vectors(factory, path, qwen, cache_dir):
    # The old exact batch cache is validated in full before importing; never pay here.
    from ticketmind.knowledge.vector_cache import load_or_build_records
    corpus = load_sources(path)
    records = load_or_build_records(list(corpus.cases.values()), qwen, cache_dir, allow_embedding=False)
    with factory() as session, session.begin():
        for record in records:
            case = session.get(KnowledgeCase, (corpus.version, record.source_id))
            if case is None or case.content != record.text:
                raise ValueError("请先 seed 对应 PostgreSQL 数据集")
            cache_vector(session, embedding_identity(qwen, case.content), record.embedding)
    return len(records)
