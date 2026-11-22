"""Deliver durable notifications while honoring enterprise calendars."""

import os
import time
from datetime import UTC, date, datetime, timedelta
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update

from ama_rs.accounting_correction import check_reserves, switch_to_reserve
from ama_rs.attendance import scan_attendance
from ama_rs.chat_menu import buttons
from ama_rs.db import create_database_engine
from ama_rs.messengers import (
    definitely_rejected, edit_message, report_correction_reference, report_edit_failure, send_message,
)
from ama_rs.processes import attachments, claim_delivery, deliveries, finish_delivery, get_card, utc_value
from ama_rs.operator_shift import outstanding
from ama_rs.refunds import confirmed_amount
from ama_rs.reminders import after_delivery, queue_due_reminders
from ama_rs.reports import format_summary, queue_report_edit, schedule_reports, summary
from ama_rs.shift_scheduler import schedule_checklists
from ama_rs.shifts import refresh_management_card


def _message(connection, record: dict) -> tuple[str, str | None]:
    card = get_card(connection, record["card_id"])
    data = card["data"]
    payload = record["payload"]
    status = f"{card['kind']} · {card['status']} · {card['id']} · v{card['revision']}"
    if payload.get("kind") == "reminder" and payload["timer"].startswith("refund_question_"):
        return (f"Ожидается ответ на уточнение по возврату {card['id']}\n"
                f"Предприятие: {card['enterprise_id']}\nВопрос: {data['question']}")[:3800], None
    if payload.get("kind") == "reminder":
        return f"Требуется действие: {status} · {payload['timer']}", None
    if payload.get("kind") == "route_probe":
        return (f"Проверка маршрута АМА РС · {card['enterprise_id']}\n"
                f"Назначенный участник, подтвердите получение в этой теме:\n"
                f"/route_confirm {card['id']}\nДля проверки фото используйте команду в подписи к тестовому снимку."), None
    if payload.get("kind") == "accounting":
        correction = data["accounting"]
        return (f"Исправление учёта · {correction['status']}\n"
                f"Исходная карточка {card['id']} · версия {card['revision']}"), None
    if payload.get("kind") == "sales_explanation":
        detail = payload.get("question") or "Требуется объяснение предприятия"
        return f"{status}\nДокумент iiko: {card['source_key']}\n{detail}"[:3800], None
    if payload.get("kind") == "sales_review":
        explanation = data["sale_explanation"]
        photo = "\nФото не предоставлено" if explanation["missing_photo"] else ""
        return (f"{status}\nДокумент iiko: {card['source_key']}\nПричина: {explanation['reason']}\n"
                f"{explanation['comment']}{photo}")[:3800], None
    if payload.get("kind") in {"attendance", "attendance_question", "attendance_answer"}:
        labels = {"no_arrival": "Приход не зафиксирован", "late": "Опоздание",
                  "unclosed": "Смена не закрыта", "early_leave": "Ранний уход"}
        details = payload.get("question") or data.get("reason") or ""
        return (f"{labels[data['deviation']]} · {data['employee']}\n"
                f"План: {data['planned_at']} · факт: {data['actual_at'] or '—'}\n"
                f"{details}\n{status}")[:3800], None
    if payload.get("kind") == "operator_shift_summary":
        open_work = outstanding(connection, card)
        return (f"Сводка к завершению смены · {card['assignment']['user']}\n"
                f"Незавершённые задачи: {len(open_work)}\n" + "\n".join(open_work))[:3800], None
    if payload.get("kind") == "operator_shift":
        if payload.get("problem"):
            return f"{status}\nПроблема ({payload['item']}): {payload['problem']}"[:3800], None
        if card["status"] == "closed":
            return (f"Смена оператора закрыта · {card['assignment']['user']}\n"
                    f"Начало: {data['started_at']}\nОкончание: {data['ended_at']}\n"
                    f"Рабочих минут: {data['worked_seconds'] // 60}"), None
        return f"Подготовка оператора · {card['assignment']['user']}\n{status}", None
    if payload.get("kind") == "handoff_offer":
        return (f"Передача задачи {card['parent_id']}\n"
                f"Получатель: {card['assignment']['user']}\n"
                f"{data['notes']}\n{status}")[:3800], None
    if payload.get("kind") == "handoff_declined":
        return (f"Передача задачи {card['parent_id']} отклонена. Причина: {payload['reason']}\n"
                f"Ответственный: {payload['recipient']}")[:3800], None
    if payload.get("kind") == "first_call":
        pending = ", ".join(payload["guest_orders"])
        return f"Ожидают первого обзвона: {pending}\n{status}"[:3800], None
    if payload.get("kind") == "guest_contact":
        return f"Нет решения после недозвона · заказов: {len(payload['guest_orders'])}\n{status}", None
    if payload.get("kind") == "request_control":
        source = get_card(connection, card["parent_id"])
        return (f"Требуется управленческое решение · {source['kind']} · {source['id']}\n"
                f"Контроль: {card['id']} · версия {card['revision']}"), None
    if payload.get("kind") == "refund_control":
        amount = data["requested_kopecks"] / 100
        return (f"Просрочен онлайн-возврат {data['refund_id']}\n"
                f"Бренд: {data['brand']} · {amount:.2f} ₽\n"
                f"Контроль: {card['id']} · версия {card['revision']}"), None
    if payload.get("kind") == "operation" and (targets := data.get("request", {}).get("confirmed_targets")):
        labels = {"delivery": "Доставка", "pickup": "Самовывоз"}
        values = ", ".join(f"{labels[kind]}: {minutes} мин" for kind, minutes in targets.items())
        return f"{status}\nПодтверждены новые сроки: {values}\n{data['request']['reason']}", None
    if payload.get("kind") == "report":
        report = data["report"]
        if record["signal"] == "report":
            report = summary(connection, card["enterprise_id"], date.fromisoformat(report["from"]),
                             date.fromisoformat(report["until"]))
        text = format_summary(report)
        if updated := data.get("updated_at"):
            local = datetime.fromisoformat(updated).astimezone(ZoneInfo(report["timezone"]))
            text = f"Обновлено {local:%d.%m.%Y %H:%M}\n" + text
        return text[:3800], None
    if payload.get("kind") == "attachment":
        file = connection.execute(select(attachments).where(
            attachments.c.card_id == card["id"], attachments.c.command_id == payload["command_id"]
        )).mappings().one()
        photo = file["file_id"] if file["platform"] == record["chat"]["platform"] else None
        return f"{status}\nМатериал: {file['item']}", photo
    if card["kind"] in {"morning_checklist", "evening_checklist"}:
        current, steps = data["current"], data["steps"]
        progress = f"Выполнено: {current}/{len(steps)}"
        if current < len(steps):
            progress += f"\n{steps[current]['key']} — {steps[current]['title']}"
        problems = sum(item["outcome"] == "problem" for item in data["results"].values())
        return f"{status}\n{progress}\nПроблемы: {problems}", None
    if payload.get("kind") in {"refund_handoff", "refund_handoff_declined"}:
        declined = payload["kind"] == "refund_handoff_declined"
        label = "Передача возврата отклонена" if declined else "Возврат предложен к передаче"
        user = data["previous"]["user"] if declined else card["assignment"]["user"]
        return f"{label} · {card['parent_id']}\nУчастник: {user}\n{status}"[:3800], None
    if payload.get("kind") == "refund_guest":
        outcome = "Возврат не требуется"
        if data["outcome"] == "executed":
            source = get_card(connection, card["parent_id"])
            outcome = f"Платёжный сервис подтвердил возврат {confirmed_amount(source['data']) / 100:.2f} ₽"
        return (f"Сообщите гостю результат · {outcome}\n"
                f"Онлайн-возврат: {card['parent_id']}\n{status}"), None
    if payload.get("kind") in {"financial_status", "financial_result"}:
        source = f"\nИсходное обращение: {payload['source_card']}" if payload.get("source_card") else ""
        amount = (confirmed_amount(data) if card["status"] == "executed"
                  else data.get("requested_kopecks", 0))
        return f"{status}\nСумма: {amount / 100:.2f} ₽{source}", None
    detail = data.get("title") or data.get("description") or payload.get("question") or payload.get("answer")
    if card["kind"] == "technical" and record["chat"]["kind"] in {"technical", "vipart"}:
        detail = f"{detail}\nСервер: {data['server_address']}\nЛМ: {data['lm_address']}"
    return (f"{status}\n{detail}" if detail else status)[:3800], None


def _send_delivery(engine, record: dict, token: str, text: str, photo: str | None, now: datetime) -> str | None:
    chat, payload = record["chat"], record["payload"]
    platform = chat["platform"]
    if message_id := payload.get("edit"):
        try:
            return edit_message(platform, token, chat, message_id, text)
        except HTTPError as error:
            if not record["signal"].startswith("report-edit:"):
                raise
            failure = report_edit_failure(platform, error)
            if failure == "unchanged":
                return message_id
            if failure not in {"forbidden", "missing"}:
                raise
            replacement = dict(payload)
            replacement.pop("edit")
            replacement["replaces_report"] = message_id
            if failure == "forbidden" or platform == "telegram":
                replacement["reply_to"] = message_id
            # Persist the change of operation before any new message can be sent.
            with engine.begin() as connection:
                connection.execute(update(deliveries).where(
                    deliveries.c.id == record["id"], deliveries.c.attempt_id == record["attempt_id"],
                    deliveries.c.state == "uncertain",
                ).values(payload=replacement, state="pending", attempt_id=None, available_at=utc_value(now)))
            return None
    if original := payload.get("replaces_report"):
        reference = report_correction_reference(chat, original)
        text = f"Корректировка отчёта · исходное сообщение: {reference}\n{text}"[:4000]
    return send_message(platform, token, chat, text, quiet=payload.get("kind") == "attachment",
                        photo=photo, reply_to=payload.get("reply_to"))


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
            rendered_revision = get_card(connection, record["card_id"])["revision"]
            text, photo = _message(connection, record)
        try:
            external_id = _send_delivery(engine, record, token, text, photo, now)
            if external_id is None:
                continue
        except HTTPError as error:
            with engine.begin() as connection:
                if error.code == 429:
                    finish_delivery(connection, delivery_id, record["attempt_id"],
                                    retry_at=now + timedelta(minutes=1))
                elif definitely_rejected(error):
                    correction = record["payload"].get("accounting_request")
                    if correction:
                        get_card(connection, record["card_id"], lock=True)
                    updated = connection.execute(update(deliveries).where(
                        deliveries.c.id == delivery_id, deliveries.c.attempt_id == record["attempt_id"],
                        deliveries.c.state == "uncertain",
                    ).values(state="failed"))
                    if correction and updated.rowcount:
                        switch_to_reserve(connection, record["card_id"], datetime.now(UTC),
                                          failed_payload=record["payload"])
        except (OSError, RuntimeError, ValueError, KeyError):
            # The remote service may have received the message. Reconciliation
            # is necessary before repeating uncertain sends.
            continue
        else:
            with engine.begin() as connection:
                card = get_card(connection, record["card_id"], lock=True)
                if finish_delivery(connection, delivery_id, record["attempt_id"], external_id=external_id):
                    delivered_at = datetime.now(UTC)
                    after_delivery(connection, record, delivered_at)
                    if record["signal"] == "management-card" and card["revision"] != rendered_revision:
                        refresh_management_card(connection, card, card["revision"], delivered_at)
                    if (record["payload"].get("kind") == "report"
                            and card["revision"] != rendered_revision):
                        queue_report_edit(connection, card, card["revision"], delivered_at)


def main() -> None:
    engine = create_database_engine()
    next_report_scan = 0.0
    while True:
        now = datetime.now(UTC)
        if time.monotonic() >= next_report_scan:
            with engine.begin() as connection:
                connection.execute(delete(buttons).where(buttons.c.expires_at <= utc_value(now)))
                schedule_checklists(connection, now)
                scan_attendance(connection, now)
                check_reserves(connection, now)
                schedule_reports(connection, now)
            next_report_scan = time.monotonic() + 60
        with engine.begin() as connection:
            queue_due_reminders(connection, now)
        deliver_batch(engine, now)
        time.sleep(5)


if __name__ == "__main__":
    main()
