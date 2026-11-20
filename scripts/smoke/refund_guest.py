from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message
from ama_rs.processes import cards, claim_delivery, create_card, deliveries, finish_delivery, get_card, timers
from ama_rs.refund_guest import record_contact
from ama_rs.refunds import record_refund_result, request_refund, take_refund
from ama_rs.reminders import after_delivery, queue_due_reminders

now = datetime(2026, 11, 23, 8, tzinfo=UTC)
employee = Actor("telegram", "1", "work")
executor = Actor("telegram", "2", "finance")
enterprise_id = "refund-guest-test"
settings = {
    "id": enterprise_id, "iiko_id": enterprise_id, "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Brand"],
    "chats": {
        "enterprise": {"kind": "enterprise", "platform": "telegram", "id": "work", "calendar": "always"},
        "refunds": {"kind": "refunds", "platform": "telegram", "id": "finance", "calendar": "always"},
        "management": {"kind": "management", "platform": "telegram", "id": "management", "calendar": "always"},
    },
    "grants": [
        {"chat": "enterprise", "role": "service_account", "user": "1"},
        {"chat": "refunds", "role": "finance_executor", "user": "2"},
    ],
}

with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")

    def process(key, source=None):
        refund = request_refund(conn, enterprise_id, key, "Brand", "Order", "Reason", 1200,
                                employee, now, parent_id=source)
        card_id = refund["id"]
        assert take_refund(conn, card_id, "take", 1, executor, now) == 2
        for _ in range(2):
            assert record_refund_result(conn, card_id, "done", 2, "not_required", "Already paid", executor, now) == 3
        return card_id

    refund_id = process("request")
    followup = conn.scalars(select(cards.c.id).where(
        cards.c.parent_id == refund_id, cards.c.kind == "refund_guest")).one()
    query = select(timers.c.key).where(timers.c.card_id == followup)
    assert not conn.scalars(query).all()
    sent_at = now + timedelta(minutes=1)
    notice = conn.scalar(select(deliveries.c.id).where(
        deliveries.c.card_id == followup, deliveries.c.signal == "notify-guest"))
    attempt = claim_delivery(conn, notice, sent_at)
    assert finish_delivery(conn, notice, attempt["attempt_id"], external_id="fixture")
    after_delivery(conn, attempt, sent_at)
    assert set(conn.scalars(query)) == {"refund_guest_reminder", "refund_guest_escalation"}
    queue_due_reminders(conn, sent_at + timedelta(minutes=5))
    assert conn.scalar(select(deliveries.c.chat_key).where(
        deliveries.c.card_id == followup, deliveries.c.signal.like("%:refund_guest_reminder"))) == "enterprise"
    overdue = sent_at + timedelta(minutes=10)
    queue_due_reminders(conn, overdue)
    assert conn.scalar(select(deliveries.c.chat_key).where(
        deliveries.c.card_id == followup, deliveries.c.signal.like("%:refund_guest_escalation"))) == "management"
    next_due = overdue + timedelta(minutes=15)
    assert record_contact(conn, followup, "attempt", 1, "unreachable", "No answer", employee, overdue,
                          next_action="Call again", next_due=next_due) == 2
    assert get_card(conn, refund_id)["status"] == "not_required"
    menu = run_bot_command(conn, Message("list", employee, "/tasks"), next_due)
    button = next(key for label, key in menu.buttons if label.startswith("Результат сообщён гостю"))
    result = run_bot_command(conn, Message("confirm", employee, button, callback_id="cb"), next_due)
    assert "Готово" in result and get_card(conn, followup)["status"] == "closed"

    original = create_card(conn, enterprise_id, "order_request", "order",
        {"chat": "enterprise", "role": "service_account", "user": "1"}, {}, now)["id"]
    linked = process("linked", original)
    assert not conn.scalars(select(cards.c.id).where(
        cards.c.kind == "refund_guest", cards.c.parent_id == linked)).all()
    linked_result = conn.scalar(select(deliveries.c.payload).where(
        deliveries.c.card_id == linked, deliveries.c.signal == "financial-result"))
    assert linked_result["source_card"] == original
    conn.rollback()
print("Standalone and linked refunds, delivery-based guest follow-up, retries and explicit confirmation verified")
