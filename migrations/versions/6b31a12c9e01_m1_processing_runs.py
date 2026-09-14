"""M1 identity, idempotency and waiting-review runs; preserve existing rows."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "6b31a12c9e01"
down_revision = "55ee2375ea43"
branch_labels = depends_on = None


def upgrade():
    op.add_column("tickets", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    for table in ("tickets", "processing_results"):
        for name, size in (("actor_id", 64), ("idempotency_key", 128), ("request_hash", 64)):
            op.add_column(table, sa.Column(name, sa.String(size), nullable=True))
    op.create_unique_constraint("uq_tickets_actor_key", "tickets", ["actor_id", "idempotency_key"])
    op.create_unique_constraint("uq_runs_ticket_actor_key", "processing_results", ["ticket_id", "actor_id", "idempotency_key"])
    for name, kind in (
        ("ticket_version", sa.Integer()), ("thread_id", sa.String(128)),
        ("corpus_version", sa.String(128)), ("retrieval_mode", sa.String(16)),
        ("model_config", JSONB()), ("input_snapshot", JSONB()), ("proposal", JSONB()),
        ("usage", JSONB()), ("completed_at", sa.DateTime(timezone=True)),
        ("duration_ms", sa.Integer()), ("error_code", sa.String(64)),
    ):
        op.add_column("processing_results", sa.Column(name, kind, nullable=True))
    op.drop_constraint("processing_run_status", "processing_results", type_="check")
    op.alter_column("processing_results", "run_status", type_=sa.String(14), existing_type=sa.String(9))
    op.create_check_constraint("processing_run_status", "processing_results",
                               "run_status IN ('running', 'waiting_review', 'completed', 'failed')")
    op.create_index("uq_runs_active_ticket", "processing_results", ["ticket_id"], unique=True,
                    postgresql_where=sa.text("run_status IN ('running', 'waiting_review')"))


def downgrade():
    # Refuse lossy rollback while new states exist; never relabel user results.
    if op.get_bind().execute(sa.text("SELECT 1 FROM processing_results WHERE run_status='waiting_review' LIMIT 1")).first():
        raise RuntimeError("存在 waiting_review 记录，不能无损回退 M1")
    op.drop_index("uq_runs_active_ticket", table_name="processing_results")
    op.drop_constraint("processing_run_status", "processing_results", type_="check")
    op.alter_column("processing_results", "run_status", type_=sa.String(9), existing_type=sa.String(14))
    op.create_check_constraint("processing_run_status", "processing_results",
                               "run_status IN ('running', 'completed', 'failed')")
    op.drop_constraint("uq_runs_ticket_actor_key", "processing_results", type_="unique")
    op.drop_constraint("uq_tickets_actor_key", "tickets", type_="unique")
    for name in ("ticket_version", "thread_id", "corpus_version", "retrieval_mode", "model_config",
                 "input_snapshot", "proposal", "usage", "completed_at", "duration_ms", "error_code"):
        op.drop_column("processing_results", name)
    for table in ("tickets", "processing_results"):
        for name in ("actor_id", "idempotency_key", "request_hash"):
            op.drop_column(table, name)
    op.drop_column("tickets", "version")
