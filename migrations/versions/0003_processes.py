"""Add process cards, audit actions, attachments, outbox, and timers."""

from alembic import op
from sqlalchemy import (
    JSON, Column, DateTime, ForeignKey, Index, Integer, MetaData, String, Table,
    UniqueConstraint,
)

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

metadata = MetaData()
Table("enterprises", metadata, Column("id", String(64), primary_key=True))

cards = Table(
    "process_cards", metadata,
    Column("id", String(32), primary_key=True),
    Column("enterprise_id", String(64), ForeignKey("enterprises.id"), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("source_key", String(128), nullable=False),
    Column("status", String(40), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("assignment", JSON, nullable=False),
    Column("data", JSON, nullable=False),
    Column("parent_id", String(32), ForeignKey("process_cards.id")),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("enterprise_id", "kind", "source_key", name="uq_card_source"),
)
actions = Table(
    "process_actions", metadata,
    Column("card_id", String(32), ForeignKey(cards.c.id), primary_key=True),
    Column("command_id", String(128), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("action", String(48), nullable=False),
    Column("actor", JSON, nullable=False),
    Column("input", JSON, nullable=False),
    Column("previous_status", String(40)),
    Column("status", String(40), nullable=False),
    Column("recorded_at", DateTime, nullable=False),
)
attachments = Table(
    "process_attachments", metadata,
    Column("card_id", String(32), ForeignKey(cards.c.id), primary_key=True),
    Column("command_id", String(128), primary_key=True),
    Column("item", String(64), nullable=False),
    Column("platform", String(16), nullable=False),
    Column("file_id", String(512), nullable=False),
    Column("metadata", JSON, nullable=False),
    Column("received_at", DateTime, nullable=False),
)
deliveries = Table(
    "process_deliveries", metadata,
    Column("id", String(32), primary_key=True),
    Column("card_id", String(32), ForeignKey(cards.c.id), nullable=False),
    Column("signal", String(128), nullable=False),
    Column("chat_key", String(100), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("state", String(12), nullable=False),
    Column("available_at", DateTime, nullable=False),
    Column("attempt_id", String(32)),
    Column("external_id", String(128)),
    Index("ix_delivery_due", "state", "available_at"),
    UniqueConstraint("card_id", "signal", "chat_key", name="uq_delivery_signal"),
)
timers = Table(
    "process_timers", metadata,
    Column("card_id", String(32), ForeignKey(cards.c.id), primary_key=True),
    Column("key", String(48), primary_key=True),
    Column("card_revision", Integer, nullable=False),
    Column("due_at", DateTime, nullable=False),
    Column("elapsed_at", DateTime),
    Index("ix_timer_due", "elapsed_at", "due_at"),
)


TABLES = (cards, actions, attachments, deliveries, timers)


def upgrade() -> None:
    metadata.create_all(op.get_bind(), tables=TABLES, checkfirst=False)


def downgrade() -> None:
    metadata.drop_all(op.get_bind(), tables=TABLES, checkfirst=False)
