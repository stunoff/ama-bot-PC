"""Store enterprise configuration and its revision history."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enterprises",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("iiko_id", sa.String(128), nullable=False, unique=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
    )
    op.create_table(
        "enterprise_configuration_history",
        sa.Column("enterprise_id", sa.String(64), sa.ForeignKey("enterprises.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("operator", sa.String(128), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
    )


def downgrade() -> None:
    op.drop_table("enterprise_configuration_history")
    op.drop_table("enterprises")
