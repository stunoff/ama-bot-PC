"""Index order completion dates and backfill existing order projections."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

orders = sa.table(
    "report_orders",
    sa.column("enterprise_id", sa.String),
    sa.column("order_id", sa.String),
    sa.column("data", sa.JSON),
    sa.column("completed_at", sa.DateTime),
)


def upgrade() -> None:
    op.add_column("report_orders", sa.Column("completed_at", DATETIME(fsp=6)))
    connection = op.get_bind()
    endpoints = {"delivery": "handed_at", "pickup": "ready_at", "takeaway": "issued_at", "dine_in": "issued_at"}
    page = sa.select(orders.c.enterprise_id, orders.c.order_id, orders.c.data).order_by(
        orders.c.enterprise_id, orders.c.order_id
    ).limit(1000)
    after = None
    while True:
        statement = page if after is None else page.where(sa.or_(
            orders.c.enterprise_id > after.enterprise_id,
            sa.and_(orders.c.enterprise_id == after.enterprise_id, orders.c.order_id > after.order_id),
        ))
        rows = connection.execute(statement).all()
        if not rows:
            break
        updates = []
        for row in rows:
            kind = row.data.get("fulfillment")
            endpoint = row.data.get(endpoints.get(kind)) if isinstance(kind, str) else None
            if endpoint:
                completed = datetime.fromisoformat(endpoint).astimezone(UTC).replace(tzinfo=None)
                updates.append({"enterprise": row.enterprise_id, "order": row.order_id, "completion": completed})
        if updates:
            connection.execute(orders.update().where(
                orders.c.enterprise_id == sa.bindparam("enterprise"),
                orders.c.order_id == sa.bindparam("order"),
            ).values(completed_at=sa.bindparam("completion")), updates)
        after = rows[-1]
    op.create_index("ix_report_orders_completion", "report_orders", ["enterprise_id", "completed_at"])


def downgrade() -> None:
    op.drop_index("ix_report_orders_completion", table_name="report_orders")
    op.drop_column("report_orders", "completed_at")
