"""Timezone-aware management reports and order completion metrics."""

from bisect import bisect_right
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, MetaData, String, Table, and_, case, func, literal, or_, select
from sqlalchemy.dialects.mysql import DATETIME, insert as mysql_insert
from sqlalchemy.engine import Connection

from ama_rs.checklist_setup import scenario_routes_ok
from ama_rs.enterprises import Actor, enterprises, has_role
from ama_rs.order_targets import BASE_TARGETS, history_until
from ama_rs.processes import (
    ProcessPermissionError, advance_system_card, cards, create_card, deliveries, enqueue, get_card, utc_value,
)
from ama_rs.requests import settings_for

metadata = MetaData()
orders = Table(
    "report_orders", metadata,
    Column("enterprise_id", String(64), ForeignKey(enterprises.c.id), primary_key=True),
    Column("order_id", String(128), primary_key=True),
    Column("accepted_at", DateTime, nullable=False, index=True),
    Column("completed_at", DATETIME(fsp=6)),
    Column("updated_at", DateTime, nullable=False),
    Column("data", JSON, nullable=False),
    Index("ix_report_orders_completion", "enterprise_id", "completed_at"),
)
coverage = Table(
    "report_source_coverage", metadata,
    Column("enterprise_id", String(64), ForeignKey(enterprises.c.id), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("source", String(40), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

TARGETS = BASE_TARGETS | {"takeaway": 20, "dine_in": 20}
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
        "preorder", "promised_at", "state", "cancelled", "test", "redelivery",
    ) if field in data}
    endpoints = {}
    for key in ("ready_at", "issued_at", "handed_at", "promised_at"):
        if projected.get(key):
            endpoints[key] = utc_value(datetime.fromisoformat(projected[key]))
    for key in ("preorder", "cancelled", "test", "redelivery"):
        if key in projected and type(projected[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    kind = projected.get("fulfillment")
    completed = endpoints.get(END_FIELD.get(kind)) if isinstance(kind, str) else None
    if "state" in projected and projected["state"] not in ("open", "completed"):
        raise ValueError("Expected an open or completed order state")
    if projected.get("preorder") and "promised_at" in endpoints:
        projected["initial_promised_at"] = projected["promised_at"]
    statement = mysql_insert(orders).values(enterprise_id=enterprise_id, order_id=order_id,
        accepted_at=accepted, completed_at=completed, updated_at=updated, data=projected)
    # MySQL evaluates assignments left-to-right. Keep the version assignment last.
    is_newer = orders.c.updated_at < statement.inserted.updated_at
    initial_promise = orders.c.data["initial_promised_at"].as_string()
    latest_data = case(
        (and_(literal(projected.get("preorder", False)), initial_promise.is_not(None)),
         func.json_set(statement.inserted.data, "$.initial_promised_at", initial_promise)),
        else_=statement.inserted.data,
    )
    connection.execute(statement.on_duplicate_key_update([
        ("accepted_at", case((is_newer, statement.inserted.accepted_at), else_=orders.c.accepted_at)),
        ("completed_at", case((is_newer, statement.inserted.completed_at), else_=orders.c.completed_at)),
        ("data", case((is_newer, latest_data), else_=orders.c.data)),
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
    sales = {}
    attendance = Counter()
    for row in records:
        if row["kind"] == "report":
            continue
        by_kind[row["kind"]] += 1
        by_status[row["status"]] += 1
        if row["kind"] in {"sales.writeoff", "sales.return"}:
            metric = sales.setdefault(row["kind"], {"reasons": Counter(), "return_types": Counter(),
                                                   "missing_photo": 0, "unreviewed": 0,
                                                   "known_amount_kopecks": 0, "with_amount": 0})
            answer = row["data"].get("sale_explanation", {})
            if reason := answer.get("reason"):
                metric["reasons"][reason] += 1
            if return_type := answer.get("return_type"):
                metric["return_types"][return_type] += 1
            metric["missing_photo"] += bool(answer.get("missing_photo"))
            metric["unreviewed"] += row["status"] != "closed"
            amount = row["data"].get("amount_kopecks")
            if type(amount) is int:
                metric["known_amount_kopecks"] += amount
                metric["with_amount"] += 1
        if row["kind"] == "attendance" and not row["data"].get("reconciled"):
            attendance[row["data"]["deviation"]] += 1
        if row["kind"] == "redelivery":
            redelivery_causes[row["data"]["request"]["reason"]] += 1

    states = {(row.source, row.day): row.state for row in connection.execute(select(coverage).where(
        coverage.c.enterprise_id == enterprise_id, coverage.c.day >= day_start.isoformat(),
        coverage.c.day < day_end.isoformat(),
    ))}
    quality = {source: _quality(states, source, day_start, day_end)
               for source in ("orders", "writeoffs", "returns", "attendance", "cash", "schedule")}
    for source, kind in (("writeoffs", "sales.writeoff"), ("returns", "sales.return")):
        if quality[source] == "unavailable" and kind in by_kind:
            quality[source] = "partial"

    traffic = {kind: {"measured": 0, "over_target": 0, "unmeasured": 0, "total_seconds": 0}
               for kind in TARGETS}
    target_rows = history_until(connection, enterprise_id, end)
    target_times = {kind: [row["effective_at"] for row in rows] for kind, rows in target_rows.items()}
    exceedances = {kind: dict.fromkeys(("base", "within_increase", "over_increase", "unknown"), 0)
                   for kind in BASE_TARGETS}
    increases = {kind: _increases(rows, BASE_TARGETS[kind], start, end)
                 for kind, rows in target_rows.items()}
    preorders = {kind: {"completed": 0, "late": 0, "late_seconds": 0, "unmeasured": 0} for kind in TARGETS}
    unfinished = dict.fromkeys(TARGETS, 0)
    unclassified = 0
    for order, completed in connection.execute(select(
        orders.c.data, orders.c.completed_at
    ).where(
        orders.c.enterprise_id == enterprise_id,
        or_(
            and_(orders.c.completed_at >= start, orders.c.completed_at < end),
            and_(orders.c.completed_at.is_(None), orders.c.accepted_at >= start, orders.c.accepted_at < end),
            and_(orders.c.accepted_at < end, or_(
                orders.c.completed_at >= end,
                and_(orders.c.completed_at.is_(None), orders.c.data["state"].as_string() == "open"),
            )),
        ),
    )):
        if any(order.get(flag) for flag in ("cancelled", "test", "redelivery")):
            continue
        kind = order.get("fulfillment")
        if not isinstance(kind, str) or kind not in TARGETS:
            unclassified += 1
            continue
        if (completed is not None and completed >= end
                or completed is None and order.get("state") == "open"):
            if not order.get("preorder"):
                unfinished[kind] += 1
            continue
        if order.get("preorder"):
            metric = preorders[kind]
            if completed is not None:
                metric["completed"] += 1
            promise = order.get("initial_promised_at")
            if completed is None or not promise:
                metric["unmeasured"] += 1
            else:
                late = (completed - utc_value(datetime.fromisoformat(promise))).total_seconds()
                metric["late"] += late > 0
                metric["late_seconds"] += max(late, 0)
            continue
        metric = traffic[kind]
        if completed is None:
            metric["unmeasured"] += 1
            continue
        accepted = utc_value(datetime.fromisoformat(order["accepted_at"]))
        seconds = (completed - accepted).total_seconds()
        if seconds < 0:
            metric["unmeasured"] += 1
            continue
        metric["measured"] += 1
        if seconds > TARGETS[kind] * 60:
            metric["over_target"] += 1
            if kind in BASE_TARGETS:
                index = bisect_right(target_times[kind], accepted) - 1
                if index < 0:
                    group = "unknown"
                else:
                    target = target_rows[kind][index]["minutes"]
                    if target == BASE_TARGETS[kind]:
                        group = "base"
                    else:
                        group = "over_increase" if seconds > target * 60 else "within_increase"
                exceedances[kind][group] += 1
        metric["total_seconds"] += seconds
    if quality["orders"] == "complete" and (unclassified or any(m["unmeasured"] for m in traffic.values())):
        quality["orders"] = "partial"
    has_order_data = unclassified or any(m["measured"] or m["unmeasured"] for m in traffic.values())
    return {
        "enterprise": settings["name"], "timezone": settings["timezone"],
        "from": day_start.isoformat(), "until": day_end.isoformat(), "counts": dict(by_kind),
        "current_statuses": dict(by_status), "sources": quality, "redelivery_causes": dict(redelivery_causes),
        "orders": traffic if quality["orders"] != "unavailable" or has_order_data else None,
        "target_increases": increases, "target_exceedances": exceedances,
        "preorders": preorders, "unfinished_orders": unfinished, "unclassified_orders": unclassified, "sales": sales, "attendance": dict(attendance),
    }


def _increases(rows: list, base: int, start: datetime, end: datetime) -> dict:
    count = 0
    intervals = []
    for index, row in enumerate(rows):
        effective = row["effective_at"]
        minutes = row["minutes"]
        previous = row["previous_minutes"]
        if start <= effective and previous is not None and minutes > previous:
            count += 1
        until = rows[index + 1]["effective_at"] if index + 1 < len(rows) else end
        if minutes <= base or until <= start:
            continue
        intervals.append({
            "minutes": minutes, "starts_at": max(effective, start).replace(tzinfo=UTC).isoformat(),
            "until": until.replace(tzinfo=UTC).isoformat(), "started_before_period": effective < start,
            "active_at_period_end": index + 1 == len(rows), "reason": row["reason"],
            "duration_seconds": (until - max(effective, start)).total_seconds(),
        })
    return {"count": count, "intervals": intervals}


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
        if not settings.get("scheduled_reports") or not scenario_routes_ok(
            connection, entry["id"], settings, "scheduled_reports", now
        ):
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


def refresh_reports(connection: Connection, enterprise_id: str, now: datetime, since: date) -> None:
    candidates = connection.scalars(select(cards.c.id).where(
        cards.c.enterprise_id == enterprise_id, cards.c.kind == "report",
        cards.c.data["report"]["until"].as_string() > since.isoformat(),
    ).order_by(cards.c.id)).all()
    for card_id in candidates:
        card = get_card(connection, card_id, lock=True)
        previous = card["data"]["report"]
        latest = summary(connection, enterprise_id, date.fromisoformat(previous["from"]),
                         date.fromisoformat(previous["until"]))
        if latest == previous:
            continue
        revision = advance_system_card(connection, card, "closed", "report_updated", now,
            data=card["data"] | {"report": latest, "updated_at": now.isoformat()})
        queue_report_edit(connection, card, revision, now)


def queue_report_edit(connection: Connection, card, revision: int, now: datetime) -> None:
    chat = card["assignment"]["chat"]
    previous = connection.execute(select(deliveries).where(
        deliveries.c.card_id == card["id"], deliveries.c.chat_key == chat,
        or_(deliveries.c.signal == "report", deliveries.c.signal.startswith("report-edit:")),
        deliveries.c.state == "sent",
    ).order_by(deliveries.c.payload["card_revision"].as_integer().desc()).limit(1)).mappings().first()
    if previous is None or not previous["payload"].get("delivery_chat"):
        return
    enqueue(connection, card["id"], f"report-edit:{revision}", chat, {
        "kind": "report", "edit": previous["external_id"],
        "expected_chat": previous["payload"]["delivery_chat"], "card_revision": revision,
    }, now)


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
    for kind, breakdown in data.get("target_exceedances", {}).items():
        if any(breakdown.values()):
            lines.append(f"{titles[kind]} · превышения базы: без повышения {breakdown['base']}, "
                         f"в пределах повышения {breakdown['within_increase']}, "
                         f"сверх повышения {breakdown['over_increase']}, неизвестно {breakdown['unknown']}")
    zone = ZoneInfo(data["timezone"])
    for kind, stats in data.get("target_increases", {}).items():
        if stats["intervals"]:
            lines.append(f"{titles[kind]} · повышений за период: {stats['count']}")
        for interval in stats["intervals"]:
            begin = datetime.fromisoformat(interval["starts_at"]).astimezone(zone)
            finish = datetime.fromisoformat(interval["until"]).astimezone(zone)
            note = " · действует на конец периода" if interval["active_at_period_end"] else ""
            if interval["started_before_period"]:
                note += " · началось до периода"
            lines.append(f"База {BASE_TARGETS[kind]} → {interval['minutes']} мин · "
                         f"{begin:%d.%m %H:%M}–{finish:%d.%m %H:%M} · "
                         f"{interval['duration_seconds'] / 60:g} мин · {interval['reason']}{note}")
    for kind, count in data.get("unfinished_orders", {}).items():
        if count:
            lines.append(f"{titles[kind]} · незавершено на конец периода: {count}")
    for kind, metric in data.get("preorders", {}).items():
        if metric["completed"] or metric["unmeasured"]:
            lines.append(f"{titles[kind]} · предзаказы: выполнено {metric['completed']}, "
                         f"с опозданием {metric['late']} (суммарно {metric['late_seconds'] / 60:g} мин), "
                         f"без данных {metric['unmeasured']}")
    if data["unclassified_orders"]:
        lines.append(f"Без типа обслуживания: {data['unclassified_orders']}")
    kinds = {"problem": "Проблемы", "task": "Поручения", "audit": "Аудиты",
             "cash_shift": "Кассовые смены", "redelivery": "Довозы", "refund": "Онлайн-возвраты"}
    lines.extend(f"{label}: {data['counts'][key]}" for key, label in kinds.items() if key in data["counts"])
    for kind, label, source in (("sales.writeoff", "Списания", "writeoffs"), ("sales.return", "Возвраты iiko", "returns")):
        stats = data.get("sales", {}).get(kind)
        if not stats:
            continue
        lines.append(f"{label}: {data['counts'][kind]}, без фото {stats['missing_photo']}, "
                     f"не разобрано {stats['unreviewed']} · {labels[data['sources'][source]]}")
        if stats["reasons"]:
            lines.append(", ".join(f"{reason} — {count}" for reason, count in stats["reasons"].items()))
    deviations = {"late": "опоздания", "early_leave": "ранние уходы", "no_arrival": "нет отметки прихода",
                  "unclosed": "незакрытые смены"}
    if data.get("attendance"):
        lines.append("Отклонения: " + ", ".join(f"{deviations[key]} — {count}"
                     for key, count in data["attendance"].items()))
    return "\n".join(lines)
