"""Immutable knowledge content; lifecycle/index updates have a separate version."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ticketmind.db.base import Base

PRODUCTION_DATASET = "production-v1"


class KnowledgeDataset(Base):
    __tablename__ = "knowledge_datasets"
    version: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    bm25_collection_name: Mapped[str | None] = mapped_column(String(128), unique=True)
    bm25_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("kind IN ('synthetic', 'production')", name="ck_knowledge_dataset_kind"),)


class KnowledgeCase(Base):
    __tablename__ = "knowledge_cases"
    dataset_version: Mapped[str] = mapped_column(ForeignKey("knowledge_datasets.version"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    problem: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON holds the original structured case / ordered ticket message snapshot.
    source: Mapped[dict] = mapped_column(JSONB, nullable=False)
    case_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ticket_id: Mapped[UUID | None] = mapped_column(ForeignKey("tickets.id"), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending_index", server_default="pending_index")
    reviewer_id: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_hash: Mapped[str | None] = mapped_column(String(64))
    dense_indexed_hash: Mapped[str | None] = mapped_column(String(64))
    dense_index_error: Mapped[str | None] = mapped_column(String(64))
    index_error: Mapped[str | None] = mapped_column(String(64))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint("status IN ('pending_index', 'active', 'index_failed', 'retired')", name="ck_knowledge_status"),
        CheckConstraint("source_type IN ('synthetic', 'ticket')", name="ck_knowledge_source_type"),
        CheckConstraint("revision > 0 AND version > 0", name="ck_knowledge_versions"),
        CheckConstraint("status != 'active' OR (indexed_hash = content_hash AND indexed_hash IS NOT NULL)", name="ck_knowledge_active_index"),
        CheckConstraint("source_type != 'ticket' OR (source_ticket_id IS NOT NULL AND reviewer_id IS NOT NULL AND approved_at IS NOT NULL)", name="ck_knowledge_approval"),
        Index("ix_knowledge_dataset_status", "dataset_version", "status"),
    )


class KnowledgeOperation(Base):
    __tablename__ = "knowledge_operations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resource: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("resource", "actor_id", "operation", "idempotency_key", name="uq_knowledge_operation"),)


class KnowledgeEmbedding(Base):
    """Durable exact cache; a successful embedding survives an index failure."""
    __tablename__ = "knowledge_embeddings"
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity: Mapped[dict] = mapped_column(JSONB, nullable=False)
    vector: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
