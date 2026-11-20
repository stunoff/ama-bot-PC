from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.contact_center import close_operation, start_operation, take_operation
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message
from ama_rs.operator_handoff import offer_handoff, respond_handoff
from ama_rs.processes import (
    ProcessPermissionError, claim_delivery, deliveries, finish_delivery, get_card, timers,
)
from ama_rs.reminders import after_delivery, queue_due_reminders

now = datetime(2026, 11, 23, 9, tzinfo=UTC)
kinds = ("enterprise", "contact_center", "contact_management")
settings = {
    "id": "handoff-test", "iiko_id": "handoff-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "central", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {kind: {"kind": kind, "id": str(i), "platform": "telegram", "calendar": "always"}
              for i, kind in enumerate(kinds)},
    "grants": [
        {"chat": "enterprise", "role": "service_account", "user": "0"},
        {"chat": "contact_center", "role": "operator", "user": "1"},
        {"chat": "contact_center", "role": "senior_operator", "user": "2"},
    ],
}
employee = Actor("telegram", "0", "0")
first = Actor("telegram", "1", "1")
next_operator = Actor("telegram", "2", "1")


def confirm(connection, card_id, signal):
    delivery = connection.scalar(select(deliveries.c.id).where(
        deliveries.c.card_id == card_id, deliveries.c.signal == signal))
    claim = claim_delivery(connection, delivery, now)
    assert finish_delivery(connection, delivery, claim["attempt_id"], external_id="fixture")
    after_delivery(connection, claim, now)


with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    operation = start_operation(conn, settings["id"], "delivery", "delivery",
        {"enabled": True, "pickup_minutes": 30, "delivery_minutes": 60}, employee, now)
    source_id = operation["id"]
    confirm(conn, source_id, "operation")
    assert take_operation(conn, source_id, "take", 1, first, now) == 2
    deadline = select(timers).where(timers.c.card_id == source_id,
                                    timers.c.key == "operation_work_escalation")
    original = conn.execute(deadline).mappings().one()
    declined = offer_handoff(conn, source_id, "offer1", 2, next_operator.user_id,
                             "Pending update", first, now)
    assert respond_handoff(conn, declined, "reject", 1, False, "Shift ended", next_operator, now) == 2
    assert get_card(conn, source_id)["assignment"]["user"] == first.user_id
    offer = offer_handoff(conn, source_id, "offer2", 2, next_operator.user_id, "Pending update", first, now)
    confirm(conn, offer, "handoff-offer")
    try:
        respond_handoff(conn, offer, "wrong", 1, True, "", first, now)
    except ProcessPermissionError:
        pass
    else:
        raise AssertionError("Outgoing operator accepted their own handoff")
    later = now + timedelta(minutes=10)
    for _ in range(2):
        queue_due_reminders(conn, later)
    target = conn.scalars(select(deliveries.c.chat_key).where(
        deliveries.c.card_id == offer, deliveries.c.signal.like("%:handoff_escalation"))).one()
    assert target == "contact_management"
    listing = run_bot_command(conn, Message("handoff-list", next_operator, "/tasks"), later)
    button = next(key for label, key in listing.buttons if label.startswith("Принять передачу"))
    result = run_bot_command(conn, Message("handoff-accept", next_operator, button, callback_id="cb"), later)
    assert "Готово" in result
    assert get_card(conn, source_id)["assignment"]["user"] == next_operator.user_id
    preserved = conn.execute(deadline).mappings().one()
    assert preserved["due_at"] == original["due_at"] and preserved["card_revision"] == 3
    assert close_operation(conn, source_id, "finish", 3, "Updated platforms", next_operator, later) == 4
    conn.rollback()
print("Handoff rights, rejection, delivery SLA, button acceptance, and preserved deadlines verified")
