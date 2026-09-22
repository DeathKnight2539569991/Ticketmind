"""Explicit review actions, recovery audit and independent search readiness."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "d91e6b72a430"
down_revision = "c3f1a7d4e902"
branch_labels = depends_on = None


def upgrade():
    op.add_column("processing_reviews", sa.Column("final_action", sa.String(17), nullable=True))
    op.create_check_constraint("review_final_action", "processing_reviews",
                               "final_action IN ('resolve', 'ask_clarification', 'escalate')")
    op.create_table("processing_recoveries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("processing_results.id"), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("previous_status", sa.String(32), nullable=False),
        sa.Column("resulting_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "actor_id", "idempotency_key", name="uq_recovery_request"))
    op.add_column("knowledge_datasets", sa.Column("bm25_collection_name", sa.String(128), nullable=True))
    op.add_column("knowledge_datasets", sa.Column("bm25_ready", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_unique_constraint("uq_knowledge_datasets_bm25_collection", "knowledge_datasets", ["bm25_collection_name"])
    op.add_column("knowledge_cases", sa.Column("dense_indexed_hash", sa.String(64), nullable=True))
    op.add_column("knowledge_cases", sa.Column("dense_index_error", sa.String(64), nullable=True))
    # Existing active entries were verified in the combined Dense/BM25 index.
    op.execute("UPDATE knowledge_cases SET dense_indexed_hash = indexed_hash WHERE status = 'active'")


def downgrade():
    checks = (
        "SELECT 1 FROM processing_reviews WHERE final_action IS NOT NULL LIMIT 1",
        "SELECT 1 FROM processing_recoveries LIMIT 1",
        "SELECT 1 FROM knowledge_datasets WHERE bm25_collection_name IS NOT NULL LIMIT 1",
        "SELECT 1 FROM knowledge_cases WHERE dense_index_error IS NOT NULL LIMIT 1",
    )
    if any(op.get_bind().execute(sa.text(sql)).first() for sql in checks):
        raise RuntimeError("已有新审核/恢复/独立索引数据，拒绝有损回退")
    op.drop_column("knowledge_cases", "dense_index_error")
    op.drop_column("knowledge_cases", "dense_indexed_hash")
    op.drop_constraint("uq_knowledge_datasets_bm25_collection", "knowledge_datasets", type_="unique")
    op.drop_column("knowledge_datasets", "bm25_ready")
    op.drop_column("knowledge_datasets", "bm25_collection_name")
    op.drop_table("processing_recoveries")
    op.drop_constraint("review_final_action", "processing_reviews", type_="check")
    op.drop_column("processing_reviews", "final_action")
