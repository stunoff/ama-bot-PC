from datetime import UTC, datetime, timedelta
from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, apply_action, attach, attachments,
    claim_delivery, create_card, deliveries, elapse_timer, enqueue, finish_delivery, start_timer,
)

now = datetime(2026, 11, 23, 18, tzinfo=UTC)
transition = Transition("submit", frozenset({"open"}), "review", frozenset({"service_account"}))
settings = {
    "id": "process-demo", "iiko_id": "demo", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {
        "enterprise": {"id": "42", "kind": "enterprise", "platform": "telegram", "calendar": "always"},
        "management": {"id": "43", "kind": "management", "platform": "telegram",
                       "calendar": [{"weekday": 0, "start": "22:00", "end": "23:00"}]},
    },
    "grants": [{"chat": "enterprise", "role": "service_account", "user": "8"}],
}
actor = Actor("telegram", "8", "42")
engine = create_database_engine()
with engine.begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    assignment = {"chat": "enterprise", "role": "service_account"}
    card = create_card(conn, settings["id"], "demo", "external-1", assignment, {}, now)
    card_id = card["id"]
    assert create_card(conn, settings["id"], "demo", "external-1", assignment, {}, now)["id"] == card_id
    for _ in range(2):
        attach(conn, card_id, "photo-1", "inventory", "file-id", actor, "enterprise", "service_account", now, {})
    assert len(conn.execute(select(attachments).where(attachments.c.card_id == card_id)).all()) == 1
    try:
        apply_action(conn, card_id, "command-1", 1, transition, Actor("telegram", "9", "42"),
                     "enterprise", "service_account", now)
    except ProcessPermissionError:
        pass
    else:
        raise AssertionError("Incorrect actor accepted")
    for _ in range(2):
        assert apply_action(conn, card_id, "command-1", 1, transition, actor,
                            "enterprise", "service_account", now) == 2
    try:
        apply_action(conn, card_id, "stale-1", 1, transition, actor, "enterprise", "service_account", now)
    except ProcessConflict:
        pass
    else:
        raise AssertionError("Stale action accepted")
    enqueue(conn, card_id, "review", "management", {"summary": "a"}, now)
    enqueue(conn, card_id, "review", "management", {"summary": "b"}, now)
    delivery = conn.execute(select(deliveries).where(deliveries.c.card_id == card_id)).mappings().one()
    assert delivery["payload"]["summary"] == "b"
    assert claim_delivery(conn, delivery["id"], now) is None
    try:
        start_timer(conn, card_id, "review", now, timedelta(minutes=15), delivery_id=delivery["id"])
    except ProcessConflict:
        pass
    else:
        raise AssertionError("Unsent delivery activated timer")
    open_time = now + timedelta(hours=1)
    attempt = claim_delivery(conn, delivery["id"], open_time)
    assert claim_delivery(conn, delivery["id"], open_time) is None
    assert finish_delivery(conn, delivery["id"], attempt["attempt_id"], external_id="message-1")
    start_timer(conn, card_id, "review", open_time, timedelta(minutes=15), delivery_id=delivery["id"])
    assert not elapse_timer(conn, card_id, "review", open_time)
    assert elapse_timer(conn, card_id, "review", open_time + timedelta(minutes=15))
    assert not elapse_timer(conn, card_id, "review", open_time + timedelta(minutes=15))
    conn.rollback()
print("Card, idempotent transitions, scoped rights, attachments, outbox and timers verified")
