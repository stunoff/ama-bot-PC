"""Deliver durable notifications while honoring enterprise calendars."""

import os
import time
from datetime import UTC, date, datetime, timedelta
from urllib.error import HTTPError

from sqlalchemy import delete, select, update

from ama_rs.chat_menu import buttons
from ama_rs.db import create_database_engine
from ama_rs.messengers import definitely_rejected, send_message
from ama_rs.processes import attachments, claim_delivery, deliveries, finish_delivery, get_card, utc_value
from ama_rs.reminders import queue_due_reminders
from ama_rs.reports import format_summary, schedule_reports, summary


def _message(connection, record: dict) -> tuple[str, str | None]:
    card = get_card(connection, record["card_id"])
    data = card["data"]
    payload = record["payload"]
    status = f"{card['kind']} · {card['status']} · {card['id']} · v{card['revision']}"
    if payload.get("kind") == "reminder":
        return f"Требуется действие: {status} · {payload['timer']}", None
    if payload.get("kind") == "report":
        period = data["report"]
        latest = summary(connection, card["enterprise_id"], date.fromisoformat(period["from"]),
                         date.fromisoformat(period["until"]))
        return format_summary(latest)[:3800], None
    if payload.get("kind") == "attachment":
        file = connection.execute(select(attachments).where(
            attachments.c.card_id == card["id"], attachments.c.command_id == payload["command_id"]
        )).mappings().one()
        photo = file["file_id"] if file["platform"] == record["chat"]["platform"] == "telegram" else None
        return f"{status}\nМатериал: {file['item']}", photo
    if payload.get("kind") in {"financial_status", "financial_result"}:
        return f"{status}\nСумма: {data.get('requested_kopecks', 0) / 100:.2f} ₽", None
    detail = data.get("title") or data.get("description") or payload.get("question") or payload.get("answer")
    if card["kind"] == "technical" and record["chat"]["kind"] in {"technical", "vipart"}:
        detail = f"{detail}\nСервер: {data['server_address']}\nЛМ: {data['lm_address']}"
    return (f"{status}\n{detail}" if detail else status)[:3800], None


def deliver_batch(engine, now: datetime) -> None:
    with engine.connect() as connection:
        ids = connection.execute(select(deliveries.c.id).where(
            deliveries.c.state == "pending", deliveries.c.available_at <= utc_value(now),
        ).order_by(deliveries.c.available_at, deliveries.c.id).limit(50)).scalars().all()
    for delivery_id in ids:
        with engine.begin() as connection:
            record = claim_delivery(connection, delivery_id, now)
            if record is None:
                connection.execute(update(deliveries).where(
                    deliveries.c.id == delivery_id, deliveries.c.state == "pending",
                    deliveries.c.available_at <= utc_value(now),
                ).values(available_at=utc_value(now + timedelta(minutes=1))))
                continue
            platform = record["chat"]["platform"]
            token = os.environ.get(f"{platform.upper()}_BOT_TOKEN")
            if not token:
                finish_delivery(connection, delivery_id, record["attempt_id"],
                                retry_at=now + timedelta(minutes=5))
                continue
            text, photo = _message(connection, record)
        try:
            external_id = send_message(platform, token, record["chat"], text,
                                       quiet=record["payload"].get("kind") == "attachment", photo=photo)
        except HTTPError as error:
            with engine.begin() as connection:
                if error.code == 429:
                    finish_delivery(connection, delivery_id, record["attempt_id"],
                                    retry_at=now + timedelta(minutes=1))
                elif definitely_rejected(error):
                    connection.execute(update(deliveries).where(
                        deliveries.c.id == delivery_id, deliveries.c.attempt_id == record["attempt_id"],
                        deliveries.c.state == "uncertain",
                    ).values(state="failed"))
        except (OSError, RuntimeError, ValueError, KeyError):
            # The remote service may have received the message. Reconciliation
            # is necessary before repeating uncertain sends.
            continue
        else:
            with engine.begin() as connection:
                finish_delivery(connection, delivery_id, record["attempt_id"], external_id=external_id)


def main() -> None:
    engine = create_database_engine()
    next_report_scan = 0.0
    while True:
        now = datetime.now(UTC)
        if time.monotonic() >= next_report_scan:
            with engine.begin() as connection:
                connection.execute(delete(buttons).where(buttons.c.expires_at <= utc_value(now)))
                schedule_reports(connection, now)
            next_report_scan = time.monotonic() + 60
        with engine.begin() as connection:
            queue_due_reminders(connection, now)
        deliver_batch(engine, now)
        time.sleep(5)


if __name__ == "__main__":
    main()
