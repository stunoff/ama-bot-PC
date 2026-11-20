from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message
from ama_rs.processes import ProcessPermissionError, get_card, timers
from ama_rs.refund_handoff import offer_refund_handoff, respond_refund_handoff
from ama_rs.refunds import answer_refund_question, ask_refund_question, record_refund_result, request_refund, reserve_refund, take_refund

now = datetime(2026, 11, 23, 8, tzinfo=UTC)
employee = Actor("telegram", "1", "work")
previous = Actor("telegram", "2", "finance")
recipient = Actor("telegram", "3", "finance")
enterprise_id = "refund-handoff-test"
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
        {"chat": "refunds", "role": "finance_executor", "user": "3"},
    ],
}
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    refund = request_refund(conn, enterprise_id, "request", "Brand", "Order", "Reason", 1200, employee, now)["id"]
    assert take_refund(conn, refund, "take", 1, previous, now) == 2
    assert ask_refund_question(conn, refund, "ask", 2, "Which order?", previous, now) == 3
    refused = offer_refund_handoff(conn, refund, "offer-1", 3, "3", "Shift change", previous, now)
    assert respond_refund_handoff(conn, refused, "decline", 1, False, "Unavailable", recipient, now) == 2
    assert get_card(conn, refund)["data"]["worker"]["user"] == "2"
    offer = offer_refund_handoff(conn, refund, "offer-2", 3, "3", "Shift change", previous, now)

    try:
        respond_refund_handoff(conn, offer, "foreign", 1, True, "", previous, now)
    except ProcessPermissionError:
        pass
    else:
        raise AssertionError("Previous executor accepted on behalf of recipient")

    menu = run_bot_command(conn, Message("list", recipient, "/tasks"), now)
    button = next(key for label, key in menu.buttons if label.startswith("Принять передачу возврата"))
    result = run_bot_command(conn, Message("accept", recipient, button, callback_id="cb"), now)
    assert "Готово" in result
    source = get_card(conn, refund)
    assert source["status"] == "clarification" and source["assignment"]["user"] == "1"
    assert source["data"]["worker"]["user"] == "3"
    deadline = conn.execute(select(timers).where(
        timers.c.card_id == refund, timers.c.key == "financial_deadline")).mappings().one()
    assert deadline["card_revision"] == 4 and deadline["due_at"] == (now + timedelta(hours=24)).replace(tzinfo=None)
    assert answer_refund_question(conn, refund, "answer", 4, "Order 1", employee, now) == 5
    assert get_card(conn, refund)["assignment"]["user"] == "3"
    assert reserve_refund(conn, refund, "reserve", 5, "payment", 1200, 0, recipient, now) == 6
    cancelled = offer_refund_handoff(conn, refund, "offer-final", 6, "2", "Shift change", recipient, now)
    assert record_refund_result(conn, refund, "done", 6, "executed", "provider", recipient, now) == 7
    assert get_card(conn, cancelled)["status"] == "closed"
    conn.rollback()
print("Financial handoff acceptance, refusal, clarification, unchanged deadline and cancellation verified")
