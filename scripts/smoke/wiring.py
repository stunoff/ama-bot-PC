import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from ama_rs.bot_actions import workflow_action
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.messengers import Message
from ama_rs.integrations import run_bot_command
from ama_rs.processes import cards, deliveries
from ama_rs.reminders import queue_due_reminders

now = datetime(2026, 11, 23, 18, tzinfo=UTC)
settings = {
    "id": "wiring-test", "iiko_id": "wiring-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {"management": {"platform": "telegram", "kind": "management", "id": "46", "calendar": "always"},
              "enterprise": {"platform": "telegram", "kind": "enterprise", "id": "47", "calendar": []}},
    "grants": [
        {"chat": "management", "role": "manager", "user": "7"},
        {"chat": "enterprise", "role": "service_account", "user": "8"},
    ],
}
manager = Actor("telegram", "7", "46")
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    payload = {"enterprise_id": settings["id"], "source_key": "task1", "title": "Test",
               "expected_result": "Complete", "due_at": (now + timedelta(minutes=5)).isoformat(),
               "recipient": {"chat": "enterprise", "role": "service_account", "user": "8"},
               "reviewer": {"chat": "management", "role": "manager"}}
    message = Message("132", manager, "/workflow task.create " + json.dumps(payload))
    assert "Карточка" in run_bot_command(conn, message, now)
    assert run_bot_command(conn, message, now) is None
    try:
        workflow_action(conn, "task.create " + json.dumps(payload | {"actor": "spoof"}), "id", manager, now)
    except ValueError:
        pass
    else:
        raise AssertionError("Messenger actor could be overwritten")
    for _ in range(2):
        queue_due_reminders(conn, now + timedelta(minutes=6))
    ids = conn.execute(select(deliveries.c.state).join(cards).where(
        cards.c.enterprise_id == settings["id"], deliveries.c.signal.like("timer:%"))).scalars().all()
    assert ids == ["pending"]
    conn.rollback()
print("Workflow commands, immutable actor context, and reminder deduplication verified")
