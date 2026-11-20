"""Convert elapsed workflow deadlines into scoped delivery obligations."""

from hashlib import sha256
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.engine import Connection

from ama_rs.enterprises import chat_is_open, get_enterprise, work_deadlines
from ama_rs.guest_contact import first_call_followup, pending_first_calls, pending_guest_orders
from ama_rs.processes import cards, deliveries, elapse_timer, enqueue, get_card, start_timer, timers, utc_value
from ama_rs.refund_control import REFUND_DONE, move_to_final, open_control
from ama_rs.request_control import advance_request_control, open_request_control
from ama_rs.timing import effective

TERMINAL = {"closed", "accepted", "executed", "not_required"}
TIMER_RANK = {
    "acceptance": 1, "reminder": 2, "breach": 3, "next_action": 4, "technical_plan_breach": 5,
    "execution_warning": 1, "execution": 2, "execution_escalation": 3,
    "refund_reminder_1": 1, "refund_reminder_2": 2, "refund_reminder_3": 3,
    "financial_deadline": 4, "refund_followup": 5,
    "review_reminder": 1, "review_overdue": 2, "review_escalation": 3,
    "operation_take_reminder": 1, "operation_take_escalation": 3,
    "operation_work_reminder": 1, "operation_work_repeat": 2, "operation_work_escalation": 3,
    "sales_first": 1, "sales_second": 2, "sales_escalation": 3,
    "cash_question_first": 1, "cash_question_second": 2, "cash_question_escalation": 3,
    "cash_review_due": 1, "cash_review_escalation": 3,
    "cash_discrepancy_due": 1, "cash_discrepancy_escalation": 3,
    "request_control_reminder": 1, "request_control_advance": 2,
    "control_reminder_1": 1, "control_reminder_2": 2, "control_final": 3,
}


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
        for key, deadline in zip(keys, work_deadlines(settings, chat, delivered_at, limits)):
            start_timer(connection, card["id"], key, delivered_at, deadline - delivered_at,
                        delivery_id=record["id"])
    elif card["kind"] == "refund" and payload.get("await_financial_receipt"):
        for deadline in work_deadlines(settings, chat, delivered_at, (10,)):
            start_timer(connection, card["id"], "refund_followup", delivered_at,
                        deadline - delivered_at, delivery_id=record["id"])
    elif card["kind"] == "refund_control" and payload.get("kind") == "refund_control":
        if payload.get("card_revision") != card["revision"] or card["status"] not in {"branch", "final"}:
            return
        keys = ("control_reminder_1", "control_reminder_2", "control_final")
        limits = effective(settings, f"refund.control.{card['status']}")
        for key, deadline in zip(keys, work_deadlines(settings, chat, delivered_at, limits)):
            start_timer(connection, card["id"], key, delivered_at, deadline - delivered_at,
                        delivery_id=record["id"])
    elif card["kind"] == "request_control" and payload.get("kind") == "request_control":
        if payload.get("card_revision") != card["revision"] or card["status"] != "waiting":
            return
        index = card["data"]["index"]
        route = card["data"]["route"]
        stage = route[index]["stage"]
        profile = f"control.{stage}"
        if stage in {"technical", "executive"}:
            profile += f".{card['data']['priority']}"
        limits = effective(settings, profile)
        if index == len(route) - 1:
            limits = limits[:1]
        for key, deadline in zip(
            ("request_control_reminder", "request_control_advance"),
            work_deadlines(settings, chat, delivered_at, limits),
        ):
            start_timer(connection, card["id"], key, delivered_at, deadline - delivered_at,
                        delivery_id=record["id"])
    elif card["kind"] == "cash_shift" and payload.get("kind") == "cash_review":
        if card["status"] in {"review", "clarification", "discrepancy"}:
            _cash_review_clock(connection, card, record["id"], settings, delivered_at)
    elif card["kind"] == "cash_shift" and payload.get("kind") == "cash_question":
        if card["status"] != "clarification" or payload["question_round"] != card["data"].get("question_round"):
            return
        _delivery_clocks(connection, card, record, settings, delivered_at,
                         ("cash_question_first", "cash_question_second", "cash_question_escalation"),
                         effective(settings, "cash.question"))
    elif card["kind"] == "cash_discrepancy" and payload.get("kind") == "cash_discrepancy":
        _delivery_clocks(connection, card, record, settings, delivered_at,
                         ("cash_discrepancy_due", "cash_discrepancy_escalation"),
                         effective(settings, "cash.discrepancy"))
    elif payload.get("kind") == "sales_explanation":
        if card["status"] in {"open", "clarification"} and payload.get("card_revision") == card["revision"]:
            _delivery_clocks(connection, card, record, settings, delivered_at,
                             ("sales_first", "sales_second", "sales_escalation"), effective(settings, "sales.reply"))
    elif card["kind"] == "operator_handoff" and payload.get("kind") == "handoff_offer":
        if card["status"] == "open":
            _delivery_clocks(connection, card, record, settings, delivered_at,
                             ("handoff_escalation",), effective(settings, "operation.handoff"))
    elif payload.get("kind") == "operation":
        if card["status"] == "open" and card["kind"] != "redelivery":
            profile = "operation.order_take" if card["kind"] in {"order_request", "order_delay"} else "operation.take"
            _delivery_clocks(connection, card, record, settings, delivered_at,
                             ("operation_take_reminder", "operation_take_escalation"),
                             effective(settings, profile))
        if card["kind"] in {"go_list", "delivery", "suspension"}:
            _delivery_clocks(connection, card, record, settings, delivered_at,
                             ("operation_work_reminder", "operation_work_repeat", "operation_work_escalation"),
                             effective(settings, "operation.work"))
    elif payload.get("kind") == "stop_work" and card["status"] == "working":
        _delivery_clocks(connection, card, record, settings, delivered_at,
                         ("operation_work_reminder", "operation_work_escalation"),
                         effective(settings, "operation.stop_work"))
    elif payload.get("first_call_followup") and card["status"] == "working":
        outstanding = set(pending_first_calls(card).values())
        limits = effective(settings, "operation.mass_first_call")
        for deadline in work_deadlines(settings, chat, delivered_at, (limits[2] - limits[1],)):
            for order in outstanding.intersection(payload["guest_orders"]):
                start_timer(connection, card["id"], first_call_followup(order), delivered_at,
                            deadline - delivered_at, delivery_id=record["id"])
    elif followup := payload.get("operation_follow_up"):
        if followup == "take" and card["status"] != "open":
            return
        delay = 5 if followup == "take" else 10
        if followup == "take" and card["kind"] in {"order_request", "order_delay"}:
            delay = 2
        _delivery_clocks(connection, card, record, settings, delivered_at,
                         (f"operation_{followup}_control_due",), (delay,))


def _delivery_clocks(connection, card, record, settings, delivered_at, keys, limits):
    for key, deadline in zip(keys, work_deadlines(settings, record["chat_key"], delivered_at, limits)):
        start_timer(connection, card["id"], key, delivered_at, deadline - delivered_at,
                    delivery_id=record["id"])


def _cash_review_clock(connection, card, delivery_id, settings, now):
    key = card["data"]["cash_chat"]
    chat = settings["chats"].get(key)
    received = card["data"].get("closed_in_iiko_at")
    if not chat or not chat.get("cash_accept_by") or not received:
        return
    zone = ZoneInfo(chat.get("timezone", settings["timezone"]))
    first_day = datetime.fromisoformat(received).astimezone(zone).date()
    clock = time.fromisoformat(chat["cash_accept_by"])
    for offset in range(1, 15):
        candidate = datetime.combine(first_day + timedelta(days=offset), clock, zone).astimezone(UTC)
        if chat_is_open(settings, key, candidate - timedelta(microseconds=1)):
            due = max(candidate, now + timedelta(seconds=1))
            start_timer(connection, card["id"], "cash_review_due", now, due - now, delivery_id=delivery_id)
            for deadline in work_deadlines(settings, key, due, effective(settings, "cash.review_overdue")):
                start_timer(connection, card["id"], "cash_review_escalation", now, deadline - now,
                            delivery_id=delivery_id)
            return


def _unique_chat(settings: dict, kind: str) -> str | None:
    matches = [key for key, chat in settings["chats"].items() if chat["kind"] == kind]
    return matches[0] if len(matches) == 1 else None


def _target(card, key: str, settings: dict) -> str | None:
    if key == "acceptance" and card["kind"] == "technical" and not card["data"].get("night"):
        return None
    if card["kind"] == "refund":
        return card["data"]["worker"]["chat"]
    if key in {"cash_review_escalation", "cash_discrepancy_escalation", "cash_question_escalation"}:
        kind = "management" if key == "cash_question_escalation" else "branch"
        return _unique_chat(settings, kind)
    if key == "accounting_due":
        return _unique_chat(settings, "branch") or card["data"]["accounting"]["manager"]
    if key == "sales_escalation":
        return _unique_chat(settings, "management")
    if key == "handoff_escalation":
        return _unique_chat(settings, "contact_management")
    if key == "cash_review_due":
        return card["data"]["cash_chat"]
    return card["assignment"]["chat"]


def _queue_guest_reminders(connection: Connection, card, keys: list[str], now: datetime) -> None:
    orders = pending_guest_orders(card)
    due_keys = [key for key in keys if key.startswith("guest_") and elapse_timer(
        connection, card["id"], key, now
    )]
    people = {orders[key] for key in due_keys if key in orders}
    if people:
        pending = connection.execute(select(deliveries.c.id, deliveries.c.payload).where(
            deliveries.c.card_id == card["id"], deliveries.c.signal.startswith("guest-contact:"),
            deliveries.c.state == "pending",
        ).with_for_update()).all()
        outstanding = set(orders.values())
        for item in pending:
            people.update(outstanding.intersection(item.payload["guest_orders"]))
        if pending:
            connection.execute(update(deliveries).where(
                deliveries.c.id.in_([item.id for item in pending])
            ).values(state="superseded"))
        batch = sha256("|".join(sorted(due_keys)).encode()).hexdigest()[:24]
        enqueue(connection, card["id"], f"guest-contact:{batch}", card["assignment"]["chat"],
                {"kind": "guest_contact", "guest_orders": sorted(people)}, now)


def _queue_first_call_reminders(connection: Connection, card, keys: list[str], now: datetime) -> None:
    outstanding = pending_first_calls(card)
    due = [key for key in keys if key.startswith("first_call_") and elapse_timer(
        connection, card["id"], key, now
    )]
    orders = {outstanding[key] for key in due if key in outstanding}
    if not orders:
        return
    expired = {outstanding[key] for key in due
               if key.startswith(("first_call_3:", "first_call_4:")) and key in outstanding}
    followup = False
    if expired:
        sent = connection.execute(select(deliveries.c.payload).where(
            deliveries.c.card_id == card["id"], deliveries.c.signal.startswith("first-call:"),
            deliveries.c.state == "sent",
        )).scalars()
        delivered = {order for payload in sent for order in payload["guest_orders"]}
        confirmed = expired.intersection(delivered)
        if confirmed or any(key.startswith("first_call_4:") for key in due):
            open_request_control(connection, card, now)
            pending = connection.execute(select(deliveries.c.id, deliveries.c.payload).where(
                deliveries.c.card_id == card["id"], deliveries.c.signal.startswith("first-call:"),
                deliveries.c.state == "pending",
            ).with_for_update()).all()
            for item in pending:
                active = sorted(set(item.payload["guest_orders"]) - expired)
                values = {"payload": item.payload | {"guest_orders": active}} if active else {"state": "superseded"}
                connection.execute(update(deliveries).where(deliveries.c.id == item.id).values(**values))
            return
        followup = True
        orders = expired
    batch = sha256("|".join(sorted(due)).encode()).hexdigest()[:24]
    connection.execute(update(deliveries).where(
        deliveries.c.card_id == card["id"], deliveries.c.signal.startswith("first-call:"),
        deliveries.c.state == "pending",
    ).values(state="superseded"))
    enqueue(connection, card["id"], f"first-call:{batch}", card["assignment"]["chat"],
            {"kind": "first_call", "guest_orders": sorted(orders), "first_call_followup": followup}, now)


def queue_due_reminders(connection: Connection, now: datetime) -> None:
    due = connection.execute(select(timers.c.card_id, timers.c.key).join(cards).where(
        timers.c.elapsed_at.is_(None), timers.c.due_at <= utc_value(now),
        timers.c.card_revision == cards.c.revision,
        or_(cards.c.status.not_in(TERMINAL), timers.c.key == "accounting_due"),
    ).order_by(timers.c.due_at, timers.c.card_id, timers.c.key).limit(100)).all()
    grouped = {}
    for card_id, key in due:
        grouped.setdefault(card_id, []).append(key)
    for card_id, keys in grouped.items():
        card = get_card(connection, card_id, lock=True)
        if card["status"] in TERMINAL:
            keys = [key for key in keys if key == "accounting_due"]
            if not keys:
                continue
        if card["kind"] == "refund_control" and get_card(connection, card["parent_id"])["status"] in REFUND_DONE:
            continue
        settings = get_enterprise(connection, card["enterprise_id"])["settings"]
        _queue_guest_reminders(connection, card, keys, now)
        _queue_first_call_reminders(connection, card, keys, now)
        emitted = set()
        for key in sorted((key for key in keys if not key.startswith(("guest_", "first_call_"))), key=lambda item: TIMER_RANK.get(item, 0), reverse=True):
            if not elapse_timer(connection, card_id, key, now):
                continue
            if key in {"operation_take_control_due", "operation_work_control_due"}:
                open_request_control(connection, card, now)
                continue
            if key in {"operation_take_escalation", "operation_work_escalation"}:
                stage = "take" if key == "operation_take_escalation" else "work"
                if connection.scalar(select(deliveries.c.id).where(
                    deliveries.c.card_id == card_id, deliveries.c.state == "sent",
                    deliveries.c.signal.like(f"timer:%:operation_{stage}_reminder"),
                ).limit(1)):
                    open_request_control(connection, card, now)
                    continue
            if key == "accounting_due" and card["data"].get("accounting", {}).get("status") not in {"pending", "working"}:
                continue
            if key == "request_control_advance":
                advance_request_control(connection, card, now)
                break
            if card["kind"] == "technical" and key == "next_action" and card["data"].get("next_due"):
                deadline = datetime.fromisoformat(card["data"]["next_due"])
                if card["data"].get("night"):
                    escalations = (deadline + timedelta(minutes=10),)
                elif card["data"]["priority"] == "critical":
                    escalations = (deadline + timedelta(minutes=5),)
                else:
                    escalations = work_deadlines(settings, card["assignment"]["chat"], deadline, (10,))
                for escalation in escalations:
                    start_timer(connection, card_id, "technical_plan_breach", deadline, escalation - deadline)
            if ((card["kind"] == "technical" and key in {"breach", "technical_plan_breach"})
                    or (card["kind"] == "task" and key in {"execution_escalation", "review_escalation"})):
                open_request_control(connection, card, now)
                if card["kind"] != "technical" or not card["data"].get("night"):
                    continue
            if key == "control_final":
                move_to_final(connection, card, now)
                break
            if key == "refund_followup":
                open_control(connection, card, now)
                continue
            if key == "request_owner_due" and card["data"]["index"] + 1 < len(card["data"]["route"]):
                due = datetime.fromisoformat(card["data"]["next_due"])
                for followup in work_deadlines(settings, card["assignment"]["chat"], due, (10,)):
                    start_timer(connection, card_id, "request_control_advance", due, followup - due)
            target = _target(card, key, settings)
            payload = {"kind": "reminder", "timer": key, "card_revision": card["revision"]}
            if key in {"operation_take_escalation", "operation_work_escalation"}:
                payload["operation_follow_up"] = "take" if key == "operation_take_escalation" else "work"
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
