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

from ama_rs.accounting_correction import read_correction
from ama_rs.attendance import scan_attendance
from ama_rs.bot_actions import workflow_action
from ama_rs import chat_admin
from ama_rs.chat_menu import Screen, consume, menu, screen
from ama_rs.contact_center import OPERATIONS, take_operation
from ama_rs.enterprise_wizard import create_enterprise, registration_screen
from ama_rs.enterprises import Actor, enterprises, get_enterprise, has_role
from ama_rs.messengers import Document, Message
from ama_rs.operator_shift import shift_card, shift_checks
from ama_rs.personnel import claim_invite, people, record_work_time, synchronize_person
from ama_rs.processes import ProcessConflict, ProcessPermissionError, cards, create_card, enqueue, get_card, utc_value
from ama_rs.refund_report import refund_csv, refund_report
from ama_rs.refunds import take_refund
from ama_rs.route_probe import confirm_probe, start_probe
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
    "/step", "/photo", "/accounting", "/checklist", "/cash_menu", "/operator_shift", "/report", "/refund_report", "/refund_export", "/sales_task_help", "/workflow", "/link", "/settings", "/timers", "/configure", "/settings_help", "/readiness", "/route_tests", "/route_confirm", "/checklist_setup", "/enterprise_new", "/scenarios",
}


def run_bot_command(connection: Connection, message: Message, now: datetime) -> str | Screen | Document | None:
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
            if "enterprise_new" in callback:
                with connection.begin_nested():
                    return create_enterprise(connection, actor, now, callback["enterprise_new"])
            if "route_probe" in callback:
                with connection.begin_nested():
                    card_id = start_probe(connection, command_id=command_id, actor=actor, now=now,
                                          **callback["route_probe"])
                    return f"Проверка маршрута поставлена в очередь · {card_id[:8]}"
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


def _checklist_screen(connection: Connection, card_id: str, actor: Actor, now: datetime) -> Screen:
    card = get_card(connection, card_id)
    if card["kind"] not in {"morning_checklist", "evening_checklist"} or card["status"] != "filling":
        raise ProcessConflict("Checklist is not active")
    target = card["assignment"]
    settings = get_enterprise(connection, card["enterprise_id"])["settings"]
    if target.get("user") not in (None, actor.user_id) or not has_role(
        settings, actor, target["chat"], "service_account", now
    ):
        raise ProcessPermissionError("Checklist belongs to another recipient")
    data = card["data"]
    step = data["steps"][data["current"]]
    key = step["key"]
    prefix = f"/step {card_id} {card['revision']} {key} "
    text = f"{data['current'] + 1}/{len(data['steps'])} · {step['title']}"
    labels = {"continue": "Продолжить", "opening": "Открылись и принимаем заказы",
              "finish": "Завершить чек-лист"}
    links = [(labels.get(key, "Выполнено"), {"text": prefix + ("opened" if key == "opening" else "ok")})]
    if step["photos"]:
        text += "\nПодписи к фото по порядку:\n" + "\n".join(
            f"/photo {card_id} {key} {slot}" for slot in step["photos"])
    if key == "opening":
        text += ("\nЕсли открытие задерживается, передайте причину и ожидаемое время:\n"
                 + prefix + 'delayed {"reason":"…","expected_at":"…"}')
    elif key not in {"continue", "finish"}:
        links.append(("Есть проблема", {"text": f"/checklist_problem_help {card_id}"}))
    if step["optional"]:
        links.append(("У нас этого нет", {"text": prefix + "absent"}))
    links.append(("Мои задачи", {"text": "/tasks"}))
    return screen(connection, actor, now, text, links)


def _dispatch(connection: Connection, command: str, arguments: str, command_id: str,
              actor: Actor, now: datetime, file_id: str | None) -> str | Screen | Document:
    if command == "/accounting":
        item = read_correction(connection, arguments.strip(), actor, now)
        return (f"Исправление учёта · {item['status']} · версия {item['revision']}\n"
                f"Документ: {item['document_id']}\nБыло: {item['current_value']}\n"
                f"Требуется: {item['expected_value']}\nПричина: {item['reason']}\n"
                f"Срок: {item['due_at']}\nВопрос: {item.get('question', '—')}\n"
                f"Ответ: {item.get('answer', '—')}\nРезультат: {item.get('result', '—')}")[:3800]
    if command in {"/settings", "/timers"}:
        return chat_admin.settings_screen(connection, actor, now, arguments.strip())
    if command == "/settings_help":
        return chat_admin.help_text(connection, actor, now, arguments.strip())
    if command == "/scenarios":
        return chat_admin.scenario_activation_screen(connection, actor, now, arguments.strip())
    if command == "/enterprise_new":
        return registration_screen(connection, actor, now, arguments.strip())
    if command == "/checklist_setup":
        return chat_admin.checklist_setup_screen(connection, actor, now, arguments.strip())
    if command == "/route_tests":
        enterprise_id, *page = arguments.split()
        return chat_admin.route_tests_screen(connection, actor, now, enterprise_id, int(page[0]) if page else 0)
    if command == "/route_confirm":
        confirm_probe(connection, arguments.strip(), command_id, actor, now, file_id=file_id)
        return "Доставка и ответ в рабочем чате подтверждены."
    if command == "/readiness":
        return chat_admin.readiness_screen(connection, actor, now, arguments.strip())
    if command == "/configure":
        return chat_admin.preview(connection, actor, now, arguments)
    if command == "/workflow":
        response = workflow_action(connection, arguments, command_id, actor, now, file_id=file_id)
        action, _, data = arguments.partition(" ")
        if data and action in {"operator_shift.move", "operator_shift.check", "operator_shift.photo"}:
            return _dispatch(connection, "/operator_shift", json.loads(data)["card_id"],
                             command_id, actor, now, None)
        return response
    if command == "/link" or (command == "/start" and arguments.startswith("invite_")):
        token = arguments.removeprefix("invite_").strip()
        claim_invite(connection, token, actor, now)
        return "Аккаунт ожидает подтверждения руководителем."
    if command in {"/start", "/menu"}:
        return menu(connection, actor, now)
    if command == "/sales_task_help":
        card_id = arguments.strip()
        card = get_card(connection, card_id)
        settings = get_enterprise(connection, card["enterprise_id"])["settings"]
        if (card["kind"] not in {"sales.writeoff", "sales.return"} or card["status"] != "review"
                or not has_role(settings, actor, card["assignment"]["chat"], "manager", now)):
            raise ProcessPermissionError("This sales review cannot be assigned here")
        form = {"card_id": card_id, "revision": card["revision"], "title": "…",
                "expected_result": "…", "due_at": "YYYY-MM-DDTHH:MM:SS+03:00",
                "recipient": {"chat": "…", "role": "service_account"}}
        return "Укажите задачу, ожидаемый результат, срок и получателя.\n/workflow sales.task " + json.dumps(
            form, ensure_ascii=False
        )
    if command in {"/refund_report", "/refund_export"}:
        if command == "/refund_export" and actor.platform != "telegram":
            return "Выгрузка CSV доступна в Telegram. Здесь используйте /refund_report."
        if not arguments:
            return (f"{command} YYYY-MM-DD YYYY-MM-DD {{фильтры}}\n"
                    "Фильтры: enterprise, brand, reason, requester, executor, status, request.")
        start, finish, *raw = arguments.split(maxsplit=2)
        filters = json.loads(raw[0]) if raw else {}
        begin, until = date.fromisoformat(start), date.fromisoformat(finish)
        if command == "/refund_export":
            data = refund_csv(connection, actor, now, begin, until, filters)
            if len(data) > 50_000_000:
                return "Файл превышает ограничение Telegram. Выберите более узкий период."
            return Document(f"refunds-{begin}-{until}.csv", data)
        return refund_report(connection, actor, now, begin, until, filters)
    if command == "/report":
        start, end = (date.fromisoformat(value) for value in arguments.split())
        reports = accessible_summaries(connection, actor, now, start, end)
        return "\n\n".join(map(format_summary, reports))[:3800]
    if command == "/checklist":
        return _checklist_screen(connection, arguments.strip(), actor, now)
    if command == "/checklist_problem_help":
        card_id = arguments.strip()
        card = get_card(connection, card_id)
        settings = get_enterprise(connection, card["enterprise_id"])["settings"]
        if card["kind"] not in {"morning_checklist", "evening_checklist"} or card["status"] != "filling":
            raise ProcessConflict("Checklist is not active")
        if not has_role(settings, actor, card["assignment"]["chat"], "service_account", now):
            raise ProcessPermissionError("Checklist belongs to another recipient")
        step = card["data"]["steps"][card["data"]["current"]]["key"]
        draft = json.dumps({"card_id": card_id, "step_key": step, "description": "…"}, ensure_ascii=False)
        return (f"Сначала опишите проблему:\n/workflow checklist.problem {draft}\n"
                f"Если нужно фото, добавьте подпись /photo {card_id} {step} problem\n"
                f"Затем завершите пункт:\n/step {card_id} {card['revision']} {step} problem " +
                '{"description":"…"}')
    if command == "/operator_shift":
        card = shift_card(connection, arguments.strip(), actor, now)
        status = card["status"]
        fields = {"card_id": card["id"], "revision": card["revision"]}
        complete = card["data"]["results"]
        links = []
        text = f"Смена оператора · {status}"
        checks = shift_checks(card)
        if checks:
            remaining = [key for key in checks if key not in complete]
            if remaining:
                item = next((key for key in remaining if key not in card["data"]["issues"]), remaining[0])
                title, photo = checks[item]
                choice = "/workflow operator_shift.check " + json.dumps(fields | {"item": item})
                text += f"\n{title}"
                if photo:
                    text += ("\nПодпись к скриншоту:\n/workflow operator_shift.photo " +
                             json.dumps({"card_id": card["id"], "item": item}))
                text += "\nСообщить проблему: " + choice[:-1] + ', "problem":"…"}'
                links.append(("Проверено", {"text": choice}))
        stages = {"planned": ("Открыть смену", "open"), "serving": ("Продолжить открытие", "continue"),
                  "active": ("Закрыть смену", "close"), "closing": ("Подтвердить закрытие", "confirm")}
        if status in stages and (status != "closing" or not remaining):
            label, action = stages[status]
            command_text = "/workflow operator_shift.move " + json.dumps(fields | {"action": action})
            if status == "serving" and card["data"].get("late_seconds") and not card["data"].get("late_reason"):
                text += "\nУкажите причину опоздания: " + command_text[:-1] + ', "late_reason":"…"}'
            else:
                links.append((label, {"text": command_text}))
        return screen(connection, actor, now, text, links + [("Мои задачи", {"text": "/tasks"})])
    if command == "/cash_menu":
        card_id = arguments.strip()
        cash = get_card(connection, card_id)
        if cash["kind"] != "cash_shift" or cash["status"] != "review":
            raise ProcessConflict("Cash shift is not ready for acceptance")
        chat = cash["data"]["cash_chat"]
        settings = get_enterprise(connection, cash["enterprise_id"])["settings"]
        if not has_role(settings, actor, chat, "cash_reviewer", now):
            raise ProcessPermissionError("Cash review belongs to another recipient")
        revision = cash["revision"]
        choices = (
            ("Принято без замечаний", "accepted"),
            ("Принято · расхождение объяснено", "explained"),
            ("Принято · нужно корректирующее действие", "correction_required"),
        )
        links = [(label, {"text": "/workflow cash.accept " + json.dumps(
            {"cash_id": card_id, "revision": revision, "outcome": outcome}
        )}) for label, outcome in choices]
        links.append(("Мои задачи", {"text": "/tasks"}))
        return screen(connection, actor, now, f"Кассовая смена · {card_id} · версия {revision}", links)
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
                cards.c.kind != "route_probe",
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
                        if row["status"] == "review" and row["kind"] == "cash_shift":
                            links.append((f"Открыть приёмку · {row['id'][:8]}",
                                          {"text": f"/cash_menu {row['id']}"}))
                        if row["status"] == "review" and row["kind"] in {"morning_checklist", "evening_checklist"}:
                            links.append((f"Принять · {row['id'][:8]}", {"text": f"/accept {identifier}"}))
                        if row["status"] == "filling" and row["kind"] in {"morning_checklist", "evening_checklist"}:
                            links.append((f"Открыть чек-лист · {row['id'][:8]}",
                                          {"text": f"/checklist {row['id']}"}))
                        if row["kind"] in {"sales.writeoff", "sales.return"}:
                            linked = row["data"].get("linked_sale_task")
                            may_close = row["status"] == "review" or (
                                row["status"] == "task" and linked and get_card(connection, linked)["status"] == "closed"
                            )
                            if may_close:
                                fields = json.dumps({"card_id": row["id"], "revision": row["revision"]})
                                links.append((f"Принять объяснение · {row['id'][:8]}",
                                              {"text": "/workflow sales.accept " + fields}))
                            if row["status"] == "review" and len(links) < 10:
                                links.append((f"Создать поручение · {row['id'][:8]}",
                                              {"text": f"/sales_task_help {row['id']}"}))
                        if row["kind"] == "refund_handoff" and row["status"] == "open":
                            fields = {"card_id": row["id"], "revision": row["revision"],
                                      "accepted": True, "reason": ""}
                            links.append(("Принять передачу возврата · " + row["id"][:8],
                                          {"text": "/workflow refund.handoff_response " + json.dumps(fields)}))
                        if row["kind"] == "refund_guest" and row["status"] == "working":
                            fields = {"card_id": row["id"], "revision": row["revision"], "outcome": "informed", "note": ""}
                            links.append(("Результат сообщён гостю · " + row["id"][:8],
                                          {"text": "/workflow refund.guest_contact " + json.dumps(fields)}))
                        if row["kind"] == "operator_shift":
                            links.append(("Смена оператора", {"text": f"/operator_shift {row['id']}"}))
                        if row["status"] == "open" and row["kind"] == "operator_handoff":
                            fields = {"card_id": row["id"], "revision": row["revision"],
                                      "accepted": True, "reason": ""}
                            links.append((f"Принять передачу · {row['id'][:8]}", {
                                "text": "/workflow operation.handoff_response " + json.dumps(fields)
                            }))
                        if row["status"] == "open" and row["kind"] in (OPERATIONS | {"task", "refund", "technical"}):
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
            if get_card(connection, card_id)["status"] == "filling":
                return _checklist_screen(connection, card_id, actor, now)
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
        scan_attendance(connection, now, enterprise_id=enterprise_id)
    elif kind == "orders.updated":
        import_order(connection, enterprise_id, data)
    elif kind == "source.coverage":
        import_coverage(connection, enterprise_id, data)
        if data["source"] in {"schedule", "attendance"}:
            scan_attendance(connection, now, enterprise_id=enterprise_id)
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
        enqueue(connection, card["id"], "explain", enterprise_chat,
                {"kind": "sales_explanation", "card_revision": 1}, now)
        enqueue(connection, card["id"], "info", management, {"kind": "card"}, now)
    else:
        raise ValueError("Unsupported iiko event type")
    return True
