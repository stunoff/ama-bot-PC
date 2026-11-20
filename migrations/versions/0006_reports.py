"""Add source coverage and order metrics data."""

from alembic import op
from sqlalchemy import JSON, Column, DateTime, ForeignKey, MetaData, String, Table

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

metadata = MetaData()
Table("enterprises", metadata, Column("id", String(64), primary_key=True))
orders = Table(
    "report_orders", metadata,
    Column("enterprise_id", String(64), ForeignKey("enterprises.id"), primary_key=True),
    Column("order_id", String(128), primary_key=True),
    Column("accepted_at", DateTime, nullable=False, index=True),
    Column("updated_at", DateTime, nullable=False),
    Column("data", JSON, nullable=False),
)
coverage = Table(
    "report_source_coverage", metadata,
    Column("enterprise_id", String(64), ForeignKey("enterprises.id"), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("source", String(40), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("updated_at", DateTime, nullable=False),
)


TABLES = (orders, coverage)


def upgrade() -> None:
    metadata.create_all(op.get_bind(), tables=TABLES, checkfirst=False)


def downgrade() -> None:
    metadata.drop_all(op.get_bind(), tables=TABLES, checkfirst=False)
