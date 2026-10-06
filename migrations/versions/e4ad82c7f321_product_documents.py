"""Separate source-of-truth tables for synthetic product documents."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "e4ad82c7f321"
down_revision = "d91e6b72a430"
branch_labels = depends_on = None


def upgrade():
    op.create_table("docs_datasets",
        sa.Column("version", sa.String(128), primary_key=True),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("collection_name", sa.String(128), nullable=False, unique=True),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("documents",
        sa.Column("dataset_version", sa.String(128), sa.ForeignKey("docs_datasets.version"), primary_key=True),
        sa.Column("doc_id", sa.String(64), primary_key=True),
        sa.Column("source_path", sa.String(512), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("revision > 0", name="ck_document_revision"),
        sa.UniqueConstraint("dataset_version", "source_path", name="uq_document_source_path"))
    op.create_table("document_chunks",
        sa.Column("dataset_version", sa.String(128), primary_key=True),
        sa.Column("doc_id", sa.String(64), primary_key=True),
        sa.Column("chunk_id", sa.String(128), primary_key=True),
        sa.Column("section", sa.String(500), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("indexed_hash", sa.String(64)),
        sa.Column("dense_indexed_hash", sa.String(64)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["dataset_version", "doc_id"], ["documents.dataset_version", "documents.doc_id"], ondelete="CASCADE"),
        sa.CheckConstraint("revision > 0", name="ck_document_chunk_revision"))
    op.create_index("ix_document_chunk_ready", "document_chunks", ["dataset_version", "indexed_hash"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM docs_datasets LIMIT 1")).first():
        raise RuntimeError("文档数据已存在，拒绝有损降级")
    op.drop_index("ix_document_chunk_ready", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_table("docs_datasets")
