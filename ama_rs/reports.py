"""Timezone-aware management reports and order completion metrics."""

from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import JSON, Column, DateTime, ForeignKey, MetaData, String, Table, case, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, enterprises, has_role
from ama_rs.processes import ProcessPermissionError, cards, create_card, enqueue, utc_value
from ama_rs.requests import settings_for

metadata = MetaData()
orders = Table(
    "report_orders", metadata,
    Column("enterprise_id", String(64), ForeignKey(enterprises.c.id), primary_key=True),
    Column("order_id", String(128), primary_key=True),
    Column("accepted_at", DateTime, nullable=False, index=True),
    Column("updated_at", DateTime, nullable=False),
    Column("data", JSON, nullable=False),
)
coverage = Table(
    "report_source_coverage", metadata,
    Column("enterprise_id", String(64), ForeignKey(enterprises.c.id), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("source", String(40), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

TARGETS = {"delivery": 60, "pickup": 30, "takeaway": 20, "dine_in": 20}
END_FIELD = {"delivery": "handed_at", "pickup": "ready_at", "takeaway": "issued_at", "dine_in": "issued_at"}
REPORT_ROLES = {"manager", "branch_lead"}


def import_order(connection: Connection, enterprise_id: str, data: dict) -> None:
    order_id = data["order_id"]
    if not isinstance(order_id, str) or not order_id or len(order_id) > 128:
        raise ValueError("Expected a stable order ID")
    accepted = utc_value(datetime.fromisoformat(data["accepted_at"]))
    updated = utc_value(datetime.fromisoformat(data["updated_at"]))
    projected = {field: data[field] for field in (
        "number", "fulfillment", "accepted_at", "ready_at", "issued_at", "handed_at",
        "preorder", "cancelled", "test", "redelivery",
    ) if field in data}
    for key in ("ready_at", "issued_at", "handed_at"):
        if projected.get(key):
            utc_value(datetime.fromisoformat(projected[key]))
    for key in ("preorder", "cancelled", "test", "redelivery"):
        if key in projected and type(projected[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    statement = mysql_insert(orders).values(enterprise_id=enterprise_id, order_id=order_id,
        accepted_at=accepted, updated_at=updated, data=projected)
    # MySQL evaluates assignments left-to-right. Keep the version assignment last.
    is_newer = orders.c.updated_at < statement.inserted.updated_at
    connection.execute(statement.on_duplicate_key_update([
        ("accepted_at", case((is_newer, statement.inserted.accepted_at), else_=orders.c.accepted_at)),
        ("data", case((is_newer, statement.inserted.data), else_=orders.c.data)),
        ("updated_at", case((is_newer, statement.inserted.updated_at), else_=orders.c.updated_at)),
    ]))


def import_coverage(connection: Connection, enterprise_id: str, data: dict) -> None:
    day = date.fromisoformat(data["day"]).isoformat()
    if data["source"] not in {"orders", "writeoffs", "returns", "attendance", "cash", "schedule"}:
        raise ValueError("Unsupported coverage source")
    if data["state"] not in {"complete", "partial", "unavailable"}:
        raise ValueError("Unsupported coverage state")
    updated = utc_value(datetime.fromisoformat(data["updated_at"]))
    stmt = mysql_insert(coverage).values(enterprise_id=enterprise_id, day=day, source=data["source"],
                                         state=data["state"], updated_at=updated)
    connection.execute(stmt.on_duplicate_key_update([
        ("state", case((coverage.c.updated_at < stmt.inserted.updated_at, stmt.inserted.state),
                       else_=coverage.c.state)),
        ("updated_at", func.greatest(coverage.c.updated_at, stmt.inserted.updated_at)),
    ]))


def _period(day_start: date, day_end: date, timezone: str) -> tuple[datetime, datetime]:
    if day_end <= day_start or day_end - day_start > timedelta(days=366):
        raise ValueError("Invalid report period")
    zone = ZoneInfo(timezone)
    return (utc_value(datetime.combine(day_start, time.min, zone)),
            utc_value(datetime.combine(day_end, time.min, zone)))


def _quality(states: dict, source: str, day_start: date, day_end: date) -> str:
    available = {states.get((source, (day_start + timedelta(days=i)).isoformat()))
                 for i in range((day_end - day_start).days)}
    if available == {"complete"}:
        return "complete"
    if available <= {None, "unavailable"}:
        return "unavailable"
    return "partial"


def summary(connection: Connection, enterprise_id: str, day_start: date, day_end: date) -> dict:
    settings = settings_for(connection, enterprise_id)
    start, end = _period(day_start, day_end, settings["timezone"])
    scope = (cards.c.enterprise_id == enterprise_id, cards.c.created_at >= start, cards.c.created_at < end)
    records = connection.execute(select(cards.c.kind, cards.c.status, cards.c.data).where(*scope)).mappings()
    by_kind = Counter()
    by_status = Counter()
    redelivery_causes = Counter()
    for row in records:
        if row["kind"] == "report":
            continue
        by_kind[row["kind"]] += 1
        by_status[row["status"]] += 1
        if row["kind"] == "redelivery":
            redelivery_causes[row["data"]["request"]["reason"]] += 1

    states = {(row.source, row.day): row.state for row in connection.execute(select(coverage).where(
        coverage.c.enterprise_id == enterprise_id, coverage.c.day >= day_start.isoformat(),
        coverage.c.day < day_end.isoformat(),
    ))}
    quality = {source: _quality(states, source, day_start, day_end)
               for source in ("orders", "writeoffs", "returns", "attendance", "cash", "schedule")}
    if quality["writeoffs"] == "unavailable":
        by_kind.pop("sales.writeoff", None)
    if quality["returns"] == "unavailable":
        by_kind.pop("sales.return", None)

    traffic = {kind: {"measured": 0, "over_target": 0, "unmeasured": 0, "total_seconds": 0}
               for kind in TARGETS}
    unclassified = 0
    for order in connection.execute(select(orders.c.data).where(
        orders.c.enterprise_id == enterprise_id, orders.c.accepted_at >= start, orders.c.accepted_at < end
    )).scalars():
        if any(order.get(flag) for flag in ("cancelled", "test", "preorder", "redelivery")):
            continue
        kind = order.get("fulfillment")
        if kind not in TARGETS:
            unclassified += 1
            continue
        metric = traffic[kind]
        finished = order.get(END_FIELD[kind])
        if not finished:
            metric["unmeasured"] += 1
            continue
        seconds = (datetime.fromisoformat(finished) - datetime.fromisoformat(order["accepted_at"])).total_seconds()
        if seconds < 0:
            metric["unmeasured"] += 1
            continue
        metric["measured"] += 1
        metric["over_target"] += seconds > TARGETS[kind] * 60
        metric["total_seconds"] += seconds
    if quality["orders"] == "complete" and (unclassified or any(m["unmeasured"] for m in traffic.values())):
        quality["orders"] = "partial"
    has_order_data = unclassified or any(m["measured"] or m["unmeasured"] for m in traffic.values())
    return {
        "enterprise": settings["name"], "timezone": settings["timezone"],
        "from": day_start.isoformat(), "until": day_end.isoformat(), "counts": dict(by_kind),
        "current_statuses": dict(by_status), "sources": quality, "redelivery_causes": dict(redelivery_causes),
        "orders": traffic if quality["orders"] != "unavailable" or has_order_data else None,
        "unclassified_orders": unclassified,
    }


def accessible_summaries(connection: Connection, actor: Actor, now: datetime,
                         day_start: date | None = None, day_end: date | None = None) -> list[dict]:
    result = []
    for entry in connection.execute(select(enterprises.c.id, enterprises.c.settings)).mappings():
        settings = entry["settings"]
        if any(
            has_role(settings, actor, grant["chat"], grant["role"], now)
            for grant in settings["grants"] if grant["role"] in REPORT_ROLES
        ):
            end = day_end if day_end is not None else now.astimezone(ZoneInfo(settings["timezone"])).date()
            start = day_start if day_start is not None else end - timedelta(days=1)
            result.append(summary(connection, entry["id"], start, end))
    if not result:
        raise ProcessPermissionError("No accessible enterprise reports")
    return result


def schedule_reports(connection: Connection, now: datetime) -> None:
    entries = connection.execute(select(enterprises.c.id, enterprises.c.settings)).mappings().all()
    for entry in entries:
        settings = entry["settings"]
        if not settings.get("scheduled_reports"):
            continue
        local = now.astimezone(ZoneInfo(settings["timezone"]))
        if local.hour < 10:
            continue
        day = local.date()
        plans = [("daily", "management", day - timedelta(days=1))]
        if day.weekday() == 0:
            plans.append(("weekly", "branch", day - timedelta(days=7)))
        if day.day == 1:
            plans.append(("monthly", "branch", (day - timedelta(days=1)).replace(day=1)))
        for kind, target, start in plans:
            matches = [key for key, chat in settings["chats"].items() if chat["kind"] == target]
            if len(matches) != 1:
                continue
            chat = matches[0]
            key = f"{kind}:{start}:{day}:{chat}"
            existing = connection.scalar(select(cards.c.id).where(
                cards.c.enterprise_id == entry["id"], cards.c.kind == "report", cards.c.source_key == key
            ))
            if existing is not None:
                continue
            report = create_card(connection, entry["id"], "report", key,
                {"chat": chat, "role": "manager" if target == "management" else "branch_lead"},
                {"report": summary(connection, entry["id"], start, day), "frequency": kind},
                now, status="closed")
            enqueue(connection, report["id"], "report", chat, {"kind": "report"}, now)


def format_summary(data: dict) -> str:
    end = date.fromisoformat(data["until"]) - timedelta(days=1)
    lines = [f"{data['enterprise']} · {data['from']} — {end} · {data['timezone']}"]
    labels = {"complete": "полные", "partial": "предварительные", "unavailable": "не получены"}
    lines.append(f"Данные о заказах: {labels[data['sources']['orders']]}")
    titles = {"delivery": "Доставка", "pickup": "Самовывоз", "takeaway": "С собой", "dine_in": "На месте"}
    for kind, metric in (data["orders"] or {}).items():
        if data["sources"]["orders"] != "complete" and not (metric["measured"] or metric["unmeasured"]):
            continue
        lines.append(f"{titles[kind]}: измерено {metric['measured']}, "
                     f"сверх нормы {metric['over_target']}, без отметки {metric['unmeasured']}")
    if data["unclassified_orders"]:
        lines.append(f"Без типа обслуживания: {data['unclassified_orders']}")
    kinds = {"problem": "Проблемы", "task": "Поручения", "audit": "Аудиты",
             "cash_shift": "Кассовые смены", "redelivery": "Довозы", "refund": "Онлайн-возвраты"}
    lines.extend(f"{label}: {data['counts'][key]}" for key, label in kinds.items() if key in data["counts"])
    return "\n".join(lines)
