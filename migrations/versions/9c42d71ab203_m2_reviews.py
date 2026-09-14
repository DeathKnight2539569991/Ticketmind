"""M2 immutable reviews, message audit and cancellation; preserve existing data."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "9c42d71ab203"
down_revision = "6b31a12c9e01"
branch_labels = depends_on = None


def upgrade():
    for name, size in (("actor_id", 64), ("operation", 32), ("idempotency_key", 128), ("request_hash", 64)):
        op.add_column("ticket_messages", sa.Column(name, sa.String(size)))
    op.create_unique_constraint("uq_messages_request", "ticket_messages",
                                ["ticket_id", "actor_id", "operation", "idempotency_key"])
    op.add_column("processing_results", sa.Column("published_message_id", UUID(as_uuid=True)))
    op.create_foreign_key("fk_run_published_message", "processing_results", "ticket_messages", ["published_message_id"], ["id"])
    op.create_unique_constraint("uq_run_published_message", "processing_results", ["published_message_id"])
    op.drop_constraint("processing_run_status", "processing_results", type_="check")
    op.create_check_constraint("processing_run_status", "processing_results",
                              "run_status IN ('running', 'waiting_review', 'completed', 'failed', 'cancelled')")
    op.create_table("processing_reviews",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("processing_results.id"), nullable=False, unique=True),
        sa.Column("reviewer_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("edited_reply", sa.Text()), sa.Column("comment", sa.Text()),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("decision IN ('approve', 'edit', 'escalate')", name="ck_review_decision"))


def downgrade():
    bind = op.get_bind()
    if any(bind.execute(sa.text(sql)).first() for sql in (
        "SELECT 1 FROM processing_reviews LIMIT 1",
        "SELECT 1 FROM ticket_messages WHERE operation IS NOT NULL LIMIT 1",
        "SELECT 1 FROM processing_results WHERE run_status='cancelled' LIMIT 1")):
        raise RuntimeError("存在 M2 审计数据，拒绝有损回退")
    op.drop_table("processing_reviews")
    op.drop_constraint("uq_run_published_message", "processing_results", type_="unique")
    op.drop_constraint("fk_run_published_message", "processing_results", type_="foreignkey")
    op.drop_column("processing_results", "published_message_id")
    op.drop_constraint("processing_run_status", "processing_results", type_="check")
    op.create_check_constraint("processing_run_status", "processing_results",
                              "run_status IN ('running', 'waiting_review', 'completed', 'failed')")
    op.drop_constraint("uq_messages_request", "ticket_messages", type_="unique")
    for name in ("actor_id", "operation", "idempotency_key", "request_hash"):
        op.drop_column("ticket_messages", name)
