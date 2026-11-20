"""Convert elapsed workflow deadlines into scoped delivery obligations."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from ama_rs.enterprises import chat_is_open, get_enterprise
from ama_rs.processes import cards, deliveries, elapse_timer, enqueue, get_card, start_timer, timers, utc_value
from ama_rs.refund_control import REFUND_DONE, move_to_final, open_control
from ama_rs.timing import effective

TERMINAL = {"closed", "accepted", "executed", "not_required"}
TIMER_RANK = {
    "acceptance": 1, "reminder": 2, "breach": 3, "next_action": 4,
    "execution_warning": 1, "execution": 2, "execution_escalation": 3,
    "refund_reminder_1": 1, "refund_reminder_2": 2, "refund_reminder_3": 3,
    "financial_deadline": 4, "refund_followup": 5,
    "review_reminder": 1, "review_overdue": 2, "review_escalation": 3,
    "control_reminder_1": 1, "control_reminder_2": 2, "control_final": 3,
}


def _unique_route(settings: dict, kind: str) -> str | None:
    matches = [key for key, chat in settings["chats"].items() if chat["kind"] == kind]
    return matches[0] if len(matches) == 1 else None


def _work_deadlines(settings: dict, target: str, start: datetime, limits: tuple[int, ...]):
    """Return UTC deadlines after the requested cumulative open minutes."""
    cursor = start.astimezone(UTC)
    if target not in settings["chats"]:
        return
    chat = settings["chats"][target]
    if chat.get("calendar") == "always" and not chat.get("calendar_exceptions"):
        for limit in limits:
            yield cursor + timedelta(minutes=limit)
        return
    if not chat.get("calendar") and not chat.get("calendar_exceptions"):
        return

    elapsed = timedelta()
    thresholds = iter(map(lambda count: timedelta(minutes=count), limits))
    threshold = next(thresholds, None)
    horizon = cursor + timedelta(days=14)
    while threshold is not None and cursor < horizon:
        next_minute = cursor.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if chat_is_open(settings, target, cursor):
            available = next_minute - cursor
            while threshold is not None and elapsed + available >= threshold:
                yield cursor + (threshold - elapsed)
                threshold = next(thresholds, None)
            elapsed += available
        cursor = next_minute


def after_delivery(connection: Connection, record: dict, delivered_at: datetime) -> None:
    card = get_card(connection, record["card_id"], lock=True)
    payload = record["payload"]
    if card["status"] in TERMINAL:
        return
    settings = get_enterprise(connection, card["enterprise_id"])["settings"]
    chat = record["chat_key"]
    if (payload.get("kind") == "review" and card["status"] == "review"
            and card["kind"] in {"task", "technical"}
            and payload.get("card_revision") == card["revision"]):
        keys = ("review_reminder", "review_overdue", "review_escalation")
        limits = effective(settings, f"{card['kind']}.review")
        for key, deadline in zip(keys, _work_deadlines(settings, chat, delivered_at, limits)):
            start_timer(connection, card["id"], key, delivered_at, deadline - delivered_at,
                        delivery_id=record["id"])
    elif card["kind"] == "refund" and payload.get("await_financial_receipt"):
        for deadline in _work_deadlines(settings, chat, delivered_at, (10,)):
            start_timer(connection, card["id"], "refund_followup", delivered_at,
                        deadline - delivered_at, delivery_id=record["id"])
    elif card["kind"] == "refund_control" and payload.get("kind") == "refund_control":
        if payload.get("card_revision") != card["revision"] or card["status"] not in {"branch", "final"}:
            return
        keys = ("control_reminder_1", "control_reminder_2", "control_final")
        limits = effective(settings, f"refund.control.{card['status']}")
        for key, deadline in zip(keys, _work_deadlines(settings, chat, delivered_at, limits)):
            start_timer(connection, card["id"], key, delivered_at, deadline - delivered_at,
                        delivery_id=record["id"])


def _target(card, key: str, settings: dict) -> str | None:
    if key == "acceptance" and card["kind"] == "technical" and not card["data"].get("night"):
        return None
    if key == "breach" and card["kind"] == "technical" and not card["data"].get("night"):
        return _unique_route(settings, "technical_management") or card["assignment"]["chat"]
    if key in {"execution_escalation", "review_escalation"} and card["kind"] == "task":
        role = card["data"]["reviewer"]["role"]
        return _unique_route(settings, "branch" if role == "manager" else settings["final_route"])
    if card["kind"] == "refund":
        return card["data"]["worker"]["chat"]
    return card["assignment"]["chat"]


def queue_due_reminders(connection: Connection, now: datetime) -> None:
    due = connection.execute(select(timers.c.card_id, timers.c.key).join(cards).where(
        timers.c.elapsed_at.is_(None), timers.c.due_at <= utc_value(now),
        timers.c.card_revision == cards.c.revision, cards.c.status.not_in(TERMINAL),
    ).order_by(timers.c.due_at, timers.c.card_id, timers.c.key).limit(100)).all()
    grouped = {}
    for card_id, key in due:
        grouped.setdefault(card_id, []).append(key)
    for card_id, keys in grouped.items():
        card = get_card(connection, card_id, lock=True)
        if card["status"] in TERMINAL:
            continue
        if card["kind"] == "refund_control" and get_card(connection, card["parent_id"])["status"] in REFUND_DONE:
            continue
        settings = get_enterprise(connection, card["enterprise_id"])["settings"]
        emitted = set()
        for key in sorted(keys, key=lambda item: TIMER_RANK.get(item, 0), reverse=True):
            if not elapse_timer(connection, card_id, key, now):
                continue
            if key == "control_final":
                move_to_final(connection, card, now)
                break
            if key == "refund_followup":
                open_control(connection, card, now)
                continue
            target = _target(card, key, settings)
            payload = {"kind": "reminder", "timer": key, "card_revision": card["revision"]}
            if key == "financial_deadline":
                finance = card["data"]["worker"]["chat"]
                prior = connection.scalar(select(deliveries.c.id).where(
                    deliveries.c.card_id == card_id, deliveries.c.chat_key == finance,
                    deliveries.c.signal.startswith("timer:"), deliveries.c.state == "sent",
                ).limit(1))
                if prior is not None:
                    open_control(connection, card, now)
                    continue
                target = finance
                payload["await_financial_receipt"] = True
            if not target or target in emitted or target not in settings["chats"]:
                continue
            connection.execute(update(deliveries).where(
                deliveries.c.card_id == card_id, deliveries.c.chat_key == target,
                deliveries.c.signal.startswith("timer:"), deliveries.c.state == "pending",
            ).values(state="superseded"))
            enqueue(connection, card_id, f"timer:{card['revision']}:{key}", target, payload, now)
            emitted.add(target)
