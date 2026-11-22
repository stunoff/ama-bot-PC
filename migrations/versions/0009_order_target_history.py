"""Store confirmed delivery and pickup targets."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_target_history",
        sa.Column("enterprise_id", sa.String(64), sa.ForeignKey("enterprises.id"), primary_key=True),
        sa.Column("fulfillment", sa.String(16), primary_key=True),
        sa.Column("command_id", sa.String(128), primary_key=True),
        sa.Column("effective_at", DATETIME(fsp=6), nullable=False),
        sa.Column("previous_minutes", sa.Integer()),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("author", sa.String(128), nullable=False),
    )
    op.create_index("ix_order_target_history_period", "order_target_history",
                    ["enterprise_id", "fulfillment", "effective_at"])


def downgrade() -> None:
    op.drop_table("order_target_history")
