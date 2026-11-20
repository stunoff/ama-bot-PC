"""Add personnel records, working time and training history."""

from alembic import op
from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, UniqueConstraint

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

metadata = MetaData()
Table("enterprises", metadata, Column("id", String(64), primary_key=True))
people = Table(
    "personnel", metadata,
    Column("id", String(32), primary_key=True),
    Column("enterprise_id", String(64), ForeignKey("enterprises.id"), nullable=False),
    Column("iiko_id", String(128), nullable=False),
    Column("name", String(160), nullable=False),
    Column("profile", String(40), nullable=False),
    Column("platform", String(16)),
    Column("user_id", String(128)),
    Column("invite_hash", String(64), unique=True),
    Column("invite_expiry", DateTime),
    Column("state", String(20), nullable=False),
    Column("training", JSON, nullable=False),
    Column("revision", Integer, nullable=False),
    UniqueConstraint("enterprise_id", "iiko_id", name="uq_person_iiko"),
    UniqueConstraint("enterprise_id", "platform", "user_id", name="uq_person_chat_user"),
)
work_time = Table(
    "personnel_work_time", metadata,
    Column("person_id", String(32), ForeignKey(people.c.id), primary_key=True),
    Column("kind", String(16), primary_key=True),
    Column("source_key", String(128), primary_key=True),
    Column("observed_at", DateTime, nullable=False),
    Column("starts_at", DateTime, nullable=False),
    Column("ends_at", DateTime),
)
training_history = Table(
    "personnel_training_history", metadata,
    Column("person_id", String(32), ForeignKey(people.c.id), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("actor", String(128), nullable=False),
    Column("event", JSON, nullable=False),
    Column("recorded_at", DateTime, nullable=False),
)


TABLES = (people, work_time, training_history)


def upgrade() -> None:
    metadata.create_all(op.get_bind(), tables=TABLES, checkfirst=False)


def downgrade() -> None:
    metadata.drop_all(op.get_bind(), tables=TABLES, checkfirst=False)
