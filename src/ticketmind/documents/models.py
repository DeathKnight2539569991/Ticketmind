"""PostgreSQL is authoritative for product-doc content and index readiness."""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ticketmind.db.base import Base

DEFAULT_DOCS_VERSION = "synthetic-product-docs-v1"


class DocsDataset(Base):
    __tablename__ = "docs_datasets"
    version: Mapped[str] = mapped_column(String(128), primary_key=True)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    collection_name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    dataset_version: Mapped[str] = mapped_column(ForeignKey("docs_datasets.version"), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (CheckConstraint("revision > 0", name="ck_document_revision"),
                      UniqueConstraint("dataset_version", "source_path", name="uq_document_source_path"))


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    dataset_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    section: Mapped[str] = mapped_column(String(500), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    indexed_hash: Mapped[str | None] = mapped_column(String(64))
    dense_indexed_hash: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    __table_args__ = (
        ForeignKeyConstraint(["dataset_version", "doc_id"], ["documents.dataset_version", "documents.doc_id"], ondelete="CASCADE"),
        CheckConstraint("revision > 0", name="ck_document_chunk_revision"),
        Index("ix_document_chunk_ready", "dataset_version", "indexed_hash"),
    )

    @property
    def source_id(self) -> str:
        return "docs:" + self.chunk_id
