"""Persist short-lived messenger button actions."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_buttons",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner", sa.JSON(), nullable=False),
        sa.Column("command", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_chat_buttons_expiry", "chat_buttons", ["expires_at"])


def downgrade() -> None:
    op.drop_table("chat_buttons")
