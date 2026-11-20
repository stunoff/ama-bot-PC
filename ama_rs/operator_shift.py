"""Planned operator shifts and personal opening/closing checks."""

from datetime import datetime, timedelta
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.contact_center import OPERATIONS, _worker_role
from ama_rs.enterprises import Actor, has_role
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, apply_action, attach, attachments,
    cards, create_card, enqueue, get_card, utc_value,
)
from ama_rs.refund_guest import KIND as REFUND_GUEST
from ama_rs.requests import settings_for
from ama_rs.shifts import route

PREPARATION = {
    "equipment": ("Компьютер и гарнитура", False),
    "internet": ("Рабочее интернет-соединение", True),
    "phones": ("Рабочие телефоны", False),
    "telegram_pc": ("Telegram на компьютере", True),
    "telegram_phone": ("Telegram на телефоне", True),
    "sellkit": ("SellKit КЦ", True),
    "mango": ("Телефония MANGO", True),
    "test_calls": ("Контрольные звонки MANGO", True),
}
ADMINISTRATION = {
    "sellkit_admin": ("SellKit Админка", True),
    "aggregators": ("Кабинеты агрегаторов", True),
    "handoffs": ("Передачи от предыдущей смены", False),
    "call_backlog": ("Оставшиеся звонки", False),
    "enterprise_data": ("Операционные данные предприятий", False),
}
CLOSING = {
    "active_orders": ("Проверить активные заказы", False),
    "temporary_settings": ("Проверить временные настройки", False),
    "outstanding_tasks": ("Завершить или передать обязательные задачи", False),
    "mango_logout": ("Выйти из MANGO и поставить устройства на зарядку", False),
}
CHECKS = {"preparing": PREPARATION, "administration": ADMINISTRATION, "closing": CLOSING}


def plan_shift(connection: Connection, enterprise_id: str, recipient_id: str, planned_start: datetime,
               planned_end: datetime, actor: Actor, now: datetime) -> str:
    settings = settings_for(connection, enterprise_id)
    management = route(settings, "contact_management")
    if settings["contact_mode"] != "central" or not has_role(settings, actor, management, "contact_center_lead", now):
        raise ProcessPermissionError("Only contact-center management can plan shifts")
    if utc_value(planned_end) <= max(utc_value(planned_start), utc_value(now)):
        raise ValueError("The shift must have a future end after its planned start")
    work = route(settings, "contact_center")
    chat = settings["chats"][work]
    recipient = Actor(chat["platform"], recipient_id, chat["id"], chat.get("topic_id"))
    role = _worker_role(settings, recipient, work, planned_start)
    key = sha256(f"{recipient_id}:{utc_value(planned_start).isoformat()}".encode()).hexdigest()
    card = create_card(connection, enterprise_id, "operator_shift", key,
        {"chat": work, "role": role, "user": recipient_id}, {
            "planned_start": planned_start.isoformat(), "planned_end": planned_end.isoformat(),
            "management": management, "results": {}, "issues": {},
        }, now, status="planned")
    if card["data"]["planned_end"] != planned_end.isoformat():
        raise ProcessConflict("A shift already exists for this operator and time")
    enqueue(connection, card["id"], "shift-preparation", work,
            {"kind": "operator_shift", "card_revision": 1}, planned_start - timedelta(minutes=10))
    for scope, destination, minutes in (("operator", work, 60), ("management", management, 30)):
        notify_at = planned_end - timedelta(minutes=minutes)
        if notify_at >= max(now, planned_start):
            enqueue(connection, card["id"], f"shift-summary:{scope}", destination,
                    {"kind": "operator_shift_summary"}, notify_at)
    return card["id"]


def shift_card(connection: Connection, card_id: str, actor: Actor, now: datetime):
    card = get_card(connection, card_id)
    assignment = card["assignment"]
    if card["kind"] != "operator_shift" or assignment.get("user") != actor.user_id:
        raise ProcessPermissionError("This is another operator's shift")
    if not has_role(settings_for(connection, card["enterprise_id"]), actor,
                    assignment["chat"], assignment["role"], now):
        raise ProcessPermissionError("The operator shift is outside this chat")
    return card


def _action(connection, card, command_id, revision, name, status, actor, now, updates):
    target = card["assignment"]
    return apply_action(connection, card["id"], command_id, revision,
        Transition(name, frozenset({card["status"]}), status, frozenset({target["role"]})),
        actor, target["chat"], target["role"], now, updates=updates)


def move_shift(connection: Connection, card_id: str, command_id: str, revision: int,
               action: str, actor: Actor, now: datetime, *, late_reason: str = "") -> int:
    get_card(connection, card_id, lock=True)
    card = shift_card(connection, card_id, actor, now)
    stages = {"open": ("planned", "preparing"), "continue": ("serving", "administration"),
              "close": ("active", "closing"), "confirm": ("closing", "closed")}
    stage, status = stages[action]
    if card["status"] != stage:
        raise ProcessConflict("Shift is in a different stage")
    updates = {}
    if action == "open":
        updates["prepared_at"] = now.isoformat()
    if action == "continue" and card["data"].get("late_seconds"):
        reason = card["data"].get("late_reason") or late_reason.strip()
        if not reason:
            raise ValueError("Record the reason for the late start")
        updates["late_reason"] = reason
    if action == "confirm":
        if not set(CLOSING) <= card["data"]["results"].keys() or outstanding(connection, card):
            raise ProcessConflict("Finish closing checks and transfer outstanding work")
        started = datetime.fromisoformat(card["data"]["started_at"])
        updates |= {"ended_at": now.isoformat(), "worked_seconds": max(0, int((now - started).total_seconds()))}
    updated = _action(connection, card, command_id, revision, action, status, actor, now, updates)
    if status == "closed":
        enqueue(connection, card_id, f"shift-{status}", card["data"]["management"],
                {"kind": "operator_shift", "card_revision": updated}, now)
    if status == "preparing":
        enqueue(connection, card_id, "shift-preparing", card["data"]["management"],
                {"kind": "operator_shift"}, now)
    return updated


def outstanding(connection: Connection, card) -> list[str]:
    records = connection.execute(select(cards.c.id, cards.c.assignment).where(
        cards.c.enterprise_id == card["enterprise_id"], cards.c.kind.in_(OPERATIONS | {REFUND_GUEST}),
        cards.c.status == "working"
    )).mappings()
    return [record["id"] for record in records if record["assignment"] == card["assignment"]]


def record_shift_check(connection: Connection, card_id: str, command_id: str, revision: int,
                       item: str, actor: Actor, now: datetime, *, problem: str = "",
                       late_reason: str = "") -> int:
    get_card(connection, card_id, lock=True)
    card = shift_card(connection, card_id, actor, now)
    stage = card["status"]
    checks = CHECKS.get(stage, {})
    if item not in checks or item in card["data"]["results"]:
        raise ProcessConflict("This check is unavailable")
    results = dict(card["data"]["results"])
    issues = dict(card["data"]["issues"])
    status = stage
    progress = {}
    if problem.strip():
        issues[item] = problem
    else:
        if checks[item][1] and connection.scalar(select(attachments.c.command_id).where(
            attachments.c.card_id == card_id, attachments.c.item == item
        ).limit(1)) is None:
            raise ProcessConflict("Attach the screenshot for this check")
        if item == "outstanding_tasks" and outstanding(connection, card):
            raise ProcessConflict("Finish or transfer the outstanding tasks")
        issues.pop(item, None)
        results[item] = now.isoformat()
        if set(checks) <= results.keys():
            if stage == "preparing":
                status = "serving"
                planned = datetime.fromisoformat(card["data"]["planned_start"])
                late = max(0, int((now - planned).total_seconds()))
                progress = {"started_at": now.isoformat(), "late_seconds": late,
                            "late_reason": late_reason.strip()}
            elif stage == "administration":
                status = "active"
    updated = _action(connection, card, command_id, revision, f"check:{item}", status, actor, now,
                      {"results": results, "issues": issues, **progress})
    if problem.strip():
        enqueue(connection, card_id, f"shift-issue:{updated}", card["data"]["management"],
                {"kind": "operator_shift", "item": item, "problem": problem}, now)
    return updated


def add_shift_photo(connection: Connection, card_id: str, command_id: str, item: str,
                    file_id: str, actor: Actor, now: datetime) -> None:
    get_card(connection, card_id, lock=True)
    card = shift_card(connection, card_id, actor, now)
    if not CHECKS.get(card["status"], {}).get(item, ("", False))[1] or item in card["data"]["results"]:
        raise ProcessConflict("The check is not accepting photos")
    target = card["assignment"]
    attach(connection, card_id, command_id, item, file_id, actor, target["chat"], target["role"], now, {})
    enqueue(connection, card_id, f"shift-photo:{command_id}", card["data"]["management"],
            {"kind": "attachment", "command_id": command_id}, now)
