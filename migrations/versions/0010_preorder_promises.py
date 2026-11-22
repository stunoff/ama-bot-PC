"""Preserve the versioned iiko promises supplied for a preorder."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("report_orders", "updated_at", existing_type=sa.DateTime(),
                    type_=DATETIME(fsp=6), existing_nullable=False)
    promises = op.create_table(
        "report_preorder_promises",
        sa.Column("enterprise_id", sa.String(64), primary_key=True),
        sa.Column("order_id", sa.String(128), primary_key=True),
        sa.Column("source_updated_at", DATETIME(fsp=6), primary_key=True),
        sa.Column("promised_at", DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["enterprise_id", "order_id"],
                                ["report_orders.enterprise_id", "report_orders.order_id"]),
    )
    orders = sa.table("report_orders", sa.column("enterprise_id", sa.String), sa.column("order_id", sa.String),
                      sa.column("updated_at", sa.DateTime), sa.column("data", sa.JSON))
    connection = op.get_bind()
    page = sa.select(orders).where(orders.c.data["promised_at"].as_string().is_not(None)).order_by(
        orders.c.enterprise_id, orders.c.order_id
    ).limit(1000)
    after = None
    while True:
        statement = page if after is None else page.where(
            sa.tuple_(orders.c.enterprise_id, orders.c.order_id) > (after.enterprise_id, after.order_id)
        )
        rows = connection.execute(statement).all()
        if not rows:
            break
        values = [{
            "enterprise_id": row.enterprise_id, "order_id": row.order_id, "source_updated_at": row.updated_at,
            "promised_at": datetime.fromisoformat(row.data["promised_at"]).astimezone(UTC).replace(tzinfo=None),
        } for row in rows if row.data.get("preorder") and row.data.get("promised_at")]
        if values:
            connection.execute(promises.insert(), values)
        after = rows[-1]


def downgrade() -> None:
    op.drop_table("report_preorder_promises")
    op.alter_column("report_orders", "updated_at", existing_type=DATETIME(fsp=6),
                    type_=sa.DateTime(), existing_nullable=False)
