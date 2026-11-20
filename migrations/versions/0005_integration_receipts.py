"""Add unique integration webhook receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_receipts",
        sa.Column("source", sa.String(16), primary_key=True),
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("claim", sa.String(32), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("integration_receipts")
