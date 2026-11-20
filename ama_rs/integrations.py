"""Webhook handlers and trusted mappings for incoming iiko events."""

import hashlib
import hmac
import json
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Column, DateTime, MetaData, String, Table, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import NoResultFound

from ama_rs.bot_actions import workflow_action
from ama_rs import chat_admin
from ama_rs.chat_menu import Screen, consume, menu, screen
from ama_rs.contact_center import take_operation
from ama_rs.enterprises import Actor, enterprises, has_role
from ama_rs.messengers import Message
from ama_rs.personnel import claim_invite, people, record_work_time, synchronize_person
from ama_rs.processes import ProcessConflict, ProcessPermissionError, cards, create_card, enqueue, utc_value
from ama_rs.refunds import take_refund
from ama_rs.reports import accessible_summaries, format_summary, import_coverage, import_order
from ama_rs.requests import report_problem, submit_result, take_request
from ama_rs.shifts import (
    accept_cash, accept_checklist, add_checklist_photo, begin_checklist, complete_step,
    confirm_iiko_closure, route,
)

receipts = Table(
    "integration_receipts", MetaData(),
    Column("source", String(16), primary_key=True),
    Column("event_id", String(255), primary_key=True),
    Column("claim", String(32), nullable=False),
    Column("recorded_at", DateTime, nullable=False),
)


def claim_event(connection: Connection, source: str, event_id: str, now: datetime) -> bool:
    if not event_id or len(event_id) > 255:
        raise ValueError("Invalid event ID")
    marker = uuid4().hex
    statement = mysql_insert(receipts).values(source=source, event_id=event_id, claim=marker,
                                                recorded_at=utc_value(now))
    connection.execute(statement.on_duplicate_key_update(claim=receipts.c.claim))
    return connection.execute(select(receipts.c.claim).where(
        receipts.c.source == source, receipts.c.event_id == event_id
    ).with_for_update()).scalar_one() == marker


def valid_signature(source: str, raw: bytes, supplied: str | None, secret: str | None) -> bool:
    if not supplied or not secret or not supplied.isascii():
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest() if source == "iiko" else secret
    return hmac.compare_digest(expected.encode(), supplied.encode())


def _scope(settings: dict, actor: Actor, now: datetime) -> list[str]:
    return [key for key in settings["chats"] if any(
        has_role(settings, actor, key, grant["role"], now, work=False)
        for grant in settings["grants"] if grant["chat"] == key
    )]


COMMANDS = {
    "/start", "/menu", "/tasks", "/problem", "/take", "/accept", "/result",
    "/step", "/photo", "/report", "/workflow", "/link", "/settings", "/timers", "/configure", "/settings_help",
}


def run_bot_command(connection: Connection, message: Message, now: datetime) -> str | Screen | None:
    """Handle supported commands using the authenticated messenger actor."""
    text = message.text.strip()
    if not message.callback_id and text.partition(" ")[0].split("@", 1)[0].lower() not in COMMANDS:
        return None
    if not claim_event(connection, message.actor.platform, message.event_id, now):
        return None

    actor = message.actor
    command_id = hashlib.sha256(f"{actor.platform}:{message.event_id}".encode()).hexdigest()
    try:
        if message.callback_id:
            callback = consume(connection, actor, now, text)
            if "admin_save" in callback:
                with connection.begin_nested():
                    return chat_admin.save(connection, actor, now, callback["admin_save"])
            if "report" in callback:
                reports = accessible_summaries(connection, actor, now)
                return "\n\n".join(map(format_summary, reports))[:3800]
            else:
                text = callback["text"]
        command, _, arguments = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        with connection.begin_nested():
            return _dispatch(connection, command, arguments, command_id, actor, now, message.file_id)
    except (ProcessConflict, ProcessPermissionError, ValueError, TypeError, LookupError, NoResultFound, AttributeError):
        return "Действие недоступно. Проверьте команду, права и версию карточки."


def _dispatch(connection: Connection, command: str, arguments: str, command_id: str,
              actor: Actor, now: datetime, file_id: str | None) -> str | Screen:
    if command in {"/settings", "/timers"}:
        return chat_admin.settings_screen(connection, actor, now, arguments.strip())
    if command == "/settings_help":
        return chat_admin.help_text(connection, actor, now, arguments.strip())
    if command == "/configure":
        return chat_admin.preview(connection, actor, now, arguments)
    if command == "/workflow":
        return workflow_action(connection, arguments, command_id, actor, now, file_id=file_id)
    if command == "/link" or (command == "/start" and arguments.startswith("invite_")):
        token = arguments.removeprefix("invite_").strip()
        claim_invite(connection, token, actor, now)
        return "Аккаунт ожидает подтверждения руководителем."
    if command in {"/start", "/menu"}:
        return menu(connection, actor, now)
    if command == "/report":
        start, end = (date.fromisoformat(value) for value in arguments.split())
        reports = accessible_summaries(connection, actor, now, start, end)
        return "\n\n".join(map(format_summary, reports))[:3800]
    if command == "/tasks":
        lines = []
        links = []
        for entry in connection.execute(select(enterprises)).mappings():
            scope = _scope(entry["settings"], actor, now)
            if not scope:
                continue
            records = connection.execute(select(cards).where(
                cards.c.enterprise_id == entry["id"],
                cards.c.status.not_in(("closed", "accepted", "executed", "not_required")),
            ).order_by(cards.c.created_at.desc()).limit(100)).mappings()
            for row in records:
                target = row["assignment"]
                if target["chat"] in scope and target.get("user") in (None, actor.user_id):
                    text = f"{row['kind']} · {row['status']} · {row['id']} · v{row['revision']}"
                    if row["kind"] in {"morning_checklist", "evening_checklist"} and row["status"] == "filling":
                        step = row["data"]["steps"][row["data"]["current"]]
                        text += f"\n{step['key']} — {step['title']}"
                        if step["photos"]:
                            text += f" ({', '.join(step['photos'])})"
                    lines.append(text)
                    if len(links) < 10 and has_role(entry["settings"], actor, target["chat"], target["role"], now):
                        identifier = f"{row['id']} {row['revision']}"
                        if row["status"] == "review" and row["kind"] in {"cash_shift", "morning_checklist", "evening_checklist"}:
                            links.append((f"Принять · {row['id'][:8]}", {"text": f"/accept {identifier}"}))
                        if row["status"] == "open" and row["kind"] in {"task", "refund", "technical"}:
                            if target["role"] != "vipart_specialist":
                                links.append((f"Взять · {row['id'][:8]}", {"text": f"/take {identifier}"}))
        return screen(connection, actor, now,
                      "\n".join(lines[:20])[:3800] or "Нет доступных активных карточек.", links)
    if command == "/problem":
        enterprise_id, _, description = arguments.partition(" ")
        card = report_problem(connection, enterprise_id, command_id, description, actor, now)
        return f"Проблема зарегистрирована. Карточка {card['id']}."
    if command == "/photo":
        card_id, step, slot = arguments.split()
        if not file_id:
            raise ValueError("Attach a photo")
        add_checklist_photo(connection, card_id, command_id, step, slot, file_id, actor, now)
        return "Фото добавлено."
    if command in {"/step", "/result"}:
        card_id, raw_revision, value = arguments.split(maxsplit=2)
        if command == "/step":
            step, outcome, *details = value.split(maxsplit=2)
            fields = json.loads(details[0]) if details else None
            revision = complete_step(connection, card_id, command_id, int(raw_revision), step,
                                     outcome, actor, now, details=fields)
        else:
            revision = submit_result(connection, card_id, command_id, int(raw_revision), actor, value, now)
        return f"Карточка обновлена. Версия {revision}."
    card_id, raw_revision = arguments.split()
    revision = int(raw_revision)
    row = connection.execute(select(cards).where(cards.c.id == card_id)).mappings().one_or_none()
    if row is None:
        raise ValueError("Unknown card")
    if command == "/take":
        if row["kind"] == "refund":
            revision = take_refund(connection, card_id, command_id, revision, actor, now)
        elif row["kind"] in {"task", "technical", "maintenance_request"}:
            revision = take_request(connection, card_id, command_id, revision, actor, now)
        else:
            revision = take_operation(connection, card_id, command_id, revision, actor, now)
    elif row["kind"] == "cash_shift":
        revision = accept_cash(connection, card_id, command_id, revision, "accepted", actor, now)
    else:
        revision = accept_checklist(connection, card_id, command_id, revision, actor, now)
    return f"Карточка обновлена. Версия {revision}."


def ingest_iiko(connection: Connection, payload: dict, now: datetime) -> bool:
    """Accept normalized events from an authenticated iiko bridge."""
    enterprise = connection.execute(select(enterprises).where(
        enterprises.c.iiko_id == payload["enterprise_id"])).mappings().one_or_none()
    if enterprise is None:
        raise ProcessConflict("iiko enterprise is not mapped")
    enterprise_id = enterprise["id"]
    event_id = str(payload["id"])
    key = hashlib.sha256(json.dumps([enterprise_id, event_id]).encode()).hexdigest()
    if not claim_event(connection, "iiko", key, now):
        return False
    kind, data = payload["type"], payload["data"]
    if kind == "staff.updated":
        synchronize_person(connection, enterprise_id, data["employee_id"], data["name"], data["profile"])
    elif kind in {"staff.schedule", "staff.attendance"}:
        person = connection.scalar(select(people.c.id).where(
            people.c.enterprise_id == enterprise_id, people.c.iiko_id == data["employee_id"]
        ))
        if person is None:
            raise ProcessConflict("iiko employee is not mapped")
        record_work_time(connection, person, "planned" if kind == "staff.schedule" else "actual",
                         data["source_key"], datetime.fromisoformat(data["starts_at"]),
                         datetime.fromisoformat(data["ends_at"]) if data.get("ends_at") else None,
                         datetime.fromisoformat(data["observed_at"]))
    elif kind == "orders.updated":
        import_order(connection, enterprise_id, data)
    elif kind == "source.coverage":
        import_coverage(connection, enterprise_id, data)
    elif kind == "shift.checklist_due":
        if not enterprise["settings"].get("scheduled_checklists"):
            begin_checklist(connection, enterprise_id, data["phase"], data["shift_id"], now)
    elif kind == "cash_shift.closed":
        shift_id = data["shift_id"]
        scope = (cards.c.enterprise_id == enterprise_id, cards.c.kind == "cash_shift")
        cash = connection.execute(select(cards.c.status).where(
            *scope, cards.c.source_key == shift_id
        ).with_for_update()).scalar_one_or_none()
        if cash is None and data.get("business_day") is not None:
            shift_id = f"local:{date.fromisoformat(data['business_day']).isoformat()}"
            cash = connection.execute(select(cards.c.status).where(
                *scope, cards.c.source_key == shift_id
            ).with_for_update()).scalar_one_or_none()
        if cash not in {"review", "accepted"} and not confirm_iiko_closure(
            connection, enterprise_id, shift_id, key, now
        ):
            raise ProcessConflict("The cash package is not ready for this closure")
    elif kind in {"sales.writeoff", "sales.return"}:
        settings = enterprise["settings"]
        enterprise_chat = route(settings, "enterprise")
        management = route(settings, "management")
        card = create_card(connection, enterprise_id, kind, str(data["document_id"]),
                           {"chat": enterprise_chat, "role": "service_account"}, data, now)
        enqueue(connection, card["id"], "explain", enterprise_chat, {"kind": "card"}, now)
        enqueue(connection, card["id"], "info", management, {"kind": "card"}, now)
    else:
        raise ValueError("Unsupported iiko event type")
    return True
