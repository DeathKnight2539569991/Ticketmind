"""Knowledge source of truth, audit and durable document vector cache."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "b812ce904a61"
down_revision = "9c42d71ab203"
branch_labels = depends_on = None


def upgrade():
    op.create_table("knowledge_datasets",
        sa.Column("version", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("collection_name", sa.String(128), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('synthetic', 'production')", name="ck_knowledge_dataset_kind"))
    op.create_table("knowledge_cases",
        sa.Column("dataset_version", sa.String(128), sa.ForeignKey("knowledge_datasets.version"), primary_key=True),
        sa.Column("source_id", sa.String(128), primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("problem", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", JSONB(), nullable=False),
        sa.Column("case_metadata", JSONB(), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_ticket_id", sa.UUID(), sa.ForeignKey("tickets.id"), unique=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(16), server_default="pending_index", nullable=False),
        sa.Column("reviewer_id", sa.String(64)), sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("indexed_hash", sa.String(64)), sa.Column("index_error", sa.String(64)),
        sa.Column("indexed_at", sa.DateTime(timezone=True)), sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('pending_index', 'active', 'index_failed', 'retired')", name="ck_knowledge_status"),
        sa.CheckConstraint("source_type IN ('synthetic', 'ticket')", name="ck_knowledge_source_type"),
        sa.CheckConstraint("revision > 0 AND version > 0", name="ck_knowledge_versions"),
        sa.CheckConstraint("status != 'active' OR (indexed_hash = content_hash AND indexed_hash IS NOT NULL)", name="ck_knowledge_active_index"),
        sa.CheckConstraint("source_type != 'ticket' OR (source_ticket_id IS NOT NULL AND reviewer_id IS NOT NULL AND approved_at IS NOT NULL)", name="ck_knowledge_approval"))
    op.create_index("ix_knowledge_dataset_status", "knowledge_cases", ["dataset_version", "status"])
    op.create_table("knowledge_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("resource", sa.String(256), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("resource", "actor_id", "operation", "idempotency_key", name="uq_knowledge_operation"))
    op.create_table("knowledge_embeddings",
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        sa.Column("identity", JSONB(), nullable=False), sa.Column("vector", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade():
    for table in ("knowledge_cases", "knowledge_operations", "knowledge_embeddings", "knowledge_datasets"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("存在知识/审核/向量记录，拒绝有损降级；先备份并明确处理数据")
    op.drop_table("knowledge_embeddings")
    op.drop_table("knowledge_operations")
    op.drop_index("ix_knowledge_dataset_status", table_name="knowledge_cases")
    op.drop_table("knowledge_cases")
    op.drop_table("knowledge_datasets")
