"""Enterprise-confirmed delivery and pickup targets used by operational work and reports."""

from datetime import datetime

from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, String, Table, insert, select
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, enterprises, has_role
from ama_rs.processes import ProcessPermissionError, create_card, enqueue, utc_value
from ama_rs.requests import settings_for
from ama_rs.shifts import route

BASE_TARGETS = {"delivery": 60, "pickup": 30}
INCREMENTS = {0, 15, 30, 45, 60, 90, 120, 150, 180}
REASONS = {
    "Перегрузка кухни", "Перегрузка курьеров", "Перегрузка кухни и курьеров",
    "Недостаточно сотрудников кухни", "Недостаточно курьеров",
    "Дорожные или погодные условия", "Нагрузка нормализовалась",
}
history = Table(
    "order_target_history", MetaData(),
    Column("enterprise_id", String(64), ForeignKey(enterprises.c.id), primary_key=True),
    Column("fulfillment", String(16), primary_key=True),
    Column("command_id", String(128), primary_key=True),
    Column("effective_at", DATETIME(fsp=6), nullable=False),
    Column("previous_minutes", Integer),
    Column("minutes", Integer, nullable=False),
    Column("reason", String(128), nullable=False),
    Column("author", String(128), nullable=False),
    Index("ix_order_target_history_period", "enterprise_id", "fulfillment", "effective_at"),
)


def confirm_order_targets(connection: Connection, enterprise_id: str, command_id: str, reason: str,
                          actor: Actor, now: datetime, *, delivery_increment: int | None = None,
                          pickup_increment: int | None = None):
    increments = {kind: increment for kind, increment in
                  (("delivery", delivery_increment), ("pickup", pickup_increment)) if increment is not None}
    if not increments or any(type(value) is not int or value not in INCREMENTS for value in increments.values()):
        raise ValueError("Use an allowed increase from the base time")
    if reason not in REASONS:
        raise ValueError("Choose a configured reason")
    connection.execute(select(enterprises.c.id).where(enterprises.c.id == enterprise_id).with_for_update()).one()
    settings = settings_for(connection, enterprise_id)
    if not has_role(settings, actor, route(settings, "enterprise"), "service_account", now):
        raise ProcessPermissionError("Only the enterprise may confirm an order target")
    destination = route(settings, "contact_center" if settings["contact_mode"] == "central" else "management")
    role = "operator" if settings["contact_mode"] == "central" else "manager"
    changes = {}
    for kind, increment in increments.items():
        values = select(history).where(history.c.enterprise_id == enterprise_id, history.c.fulfillment == kind)
        if connection.execute(values.where(history.c.command_id == command_id).with_for_update()).first():
            continue
        previous = connection.execute(values.order_by(
            history.c.effective_at.desc(), history.c.command_id.desc()
        ).limit(1).with_for_update()).mappings().first()
        minutes = BASE_TARGETS[kind] + increment
        if previous and previous["minutes"] == minutes:
            continue
        effective_at = utc_value(now)
        if previous and effective_at <= previous["effective_at"]:
            raise ValueError("A newer target has already been confirmed")
        connection.execute(insert(history).values(
            enterprise_id=enterprise_id, fulfillment=kind, command_id=command_id, effective_at=effective_at,
            previous_minutes=previous["minutes"] if previous else None, minutes=minutes,
            reason=reason, author=actor.user_id,
        ))
        if previous or increment:
            changes[kind] = minutes
    if not changes:
        return None
    card = create_card(connection, enterprise_id, "delivery", f"target:{command_id}",
        {"chat": destination, "role": role},
        {"request": {"confirmed_targets": changes, "reason": reason}, "destination": destination,
         "mode": settings["contact_mode"]}, now)
    enqueue(connection, card["id"], "operation", destination, {"kind": "operation"}, now)
    return card


def history_until(connection: Connection, enterprise_id: str, end: datetime):
    rows = connection.execute(select(history).where(
        history.c.enterprise_id == enterprise_id, history.c.effective_at < end,
    ).order_by(history.c.fulfillment, history.c.effective_at, history.c.command_id)).mappings()
    result = {kind: [] for kind in BASE_TARGETS}
    for row in rows:
        result[row["fulfillment"]].append(row)
    return result
