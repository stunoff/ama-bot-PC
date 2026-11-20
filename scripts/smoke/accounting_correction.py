from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.accounting_correction import read_correction, request_correction, review_correction, submit_correction
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.processes import ProcessPermissionError, cards, create_card, deliveries, get_card
from ama_rs.reminders import queue_due_reminders

now = datetime(2026, 11, 23, 8, tzinfo=UTC)
manager = Actor("telegram", "2", "mgr")
executor = Actor("telegram", "3", "ops")
settings = {
    "id": "correct-test", "iiko_id": "correct-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {"management": {"kind": "management", "platform": "telegram", "id": "mgr", "calendar": "always"},
              "accounting": {"kind": "technical", "platform": "telegram", "id": "ops", "calendar": "always"},
              "branch": {"kind": "branch", "platform": "telegram", "id": "branch", "calendar": "always"}},
    "grants": [{"chat": "management", "role": "manager", "user": "2"},
               {"chat": "accounting", "role": "accounting_executor", "user": "3"}],
}
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    source = create_card(conn, settings["id"], "inventory", "inventory-plan",
                         {"chat": "management", "role": "manager"}, {"posted_document": "id1"}, now,
                         status="closed")["id"]
    due = now + timedelta(minutes=30)
    assert request_correction(conn, source, "request", 1, "id1", "22", "12", "Typo",
                              "accounting", "3", due, manager, now) == 2
    assert get_card(conn, source)["status"] == "closed"
    assert submit_correction(conn, source, "take", 2, "working", "Checking", executor, now) == 3
    for _ in range(2):
        queue_due_reminders(conn, due)
    assert conn.scalars(select(deliveries.c.chat_key).where(
        deliveries.c.card_id == source, deliveries.c.signal.like("%:accounting_due"))).one() == "branch"
    try:
        read_correction(conn, source, executor, now)
    except ProcessPermissionError:
        pass
    else:
        raise AssertionError("Correction details exposed in a group")
    assert read_correction(conn, source, Actor("telegram", "3", "3"), now)["expected_value"] == "12"
    assert submit_correction(conn, source, "fix", 3, "corrected", "Adjusted", executor, due,
                             confirmation="Ticket", corrected_by="Authorized accountant") == 4
    assert review_correction(conn, source, "confirm", 4, True, "", manager, due) == 5
    accepted = get_card(conn, source)
    assert accepted["status"] == "closed" and accepted["data"]["accounting"]["status"] == "confirmed"
    assert conn.scalars(select(cards.c.id).where(cards.c.enterprise_id == settings["id"])).one() == source
    conn.rollback()
print("Source-only corrections, separate roles, private details, original status and escalation verified")
