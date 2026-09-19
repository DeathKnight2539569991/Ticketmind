"""Remove obsolete processing-result duplicate fields after verifying they contain no unique data."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "c3f1a7d4e902"
down_revision = "b812ce904a61"
branch_labels = depends_on = None


def _exists(sql: str) -> bool:
    return op.get_bind().execute(sa.text(sql)).first() is not None


def upgrade():
    if _exists("SELECT 1 FROM processing_results WHERE confidence IS NOT NULL LIMIT 1"):
        raise RuntimeError("processing_results.confidence contains data; refusing destructive cleanup")
    if _exists(
        "SELECT 1 FROM processing_results "
        "WHERE extracted_information IS DISTINCT FROM '{}'::jsonb LIMIT 1"
    ):
        raise RuntimeError(
            "processing_results.extracted_information contains non-empty data; refusing destructive cleanup"
        )
    if _exists(
        "SELECT 1 FROM processing_results "
        "WHERE reason IS NOT NULL AND "
        "(proposal IS NULL OR proposal->>'reason' IS DISTINCT FROM reason) LIMIT 1"
    ):
        raise RuntimeError(
            "processing_results.reason contains data not represented by proposal; refusing destructive cleanup"
        )
    if _exists(
        "SELECT 1 FROM processing_results "
        "WHERE final_reply IS NOT NULL AND "
        "(proposal IS NULL OR proposal->>'reply' IS DISTINCT FROM final_reply) LIMIT 1"
    ):
        raise RuntimeError(
            "processing_results.final_reply contains data not represented by proposal; refusing destructive cleanup"
        )

    op.drop_constraint("ck_processing_results_confidence_range", "processing_results", type_="check")
    for column in ("confidence", "reason", "final_reply", "extracted_information"):
        op.drop_column("processing_results", column)


def downgrade():
    op.add_column("processing_results", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column("processing_results", sa.Column("confidence", sa.Float(), nullable=True))
    op.add_column("processing_results", sa.Column("final_reply", sa.Text(), nullable=True))
    op.add_column(
        "processing_results",
        sa.Column(
            "extracted_information",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE processing_results "
            "SET reason = proposal->>'reason', final_reply = proposal->>'reply' "
            "WHERE proposal IS NOT NULL"
        )
    )
    op.alter_column("processing_results", "extracted_information", server_default=None)
    op.create_check_constraint(
        "ck_processing_results_confidence_range",
        "processing_results",
        "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
    )
