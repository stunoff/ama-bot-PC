from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import save_enterprise
from ama_rs.processes import cards, deliveries
from ama_rs.shift_scheduler import schedule_checklists

morning = datetime(2026, 11, 23, 6, 50, tzinfo=UTC)
evening = datetime(2026, 11, 23, 23, tzinfo=UTC)
settings = {
    "id": "scheduler-test", "iiko_id": "scheduler-iiko", "name": "Test", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Test"],
    "scheduled_checklists": True,
    "shift_schedule": {"service_starts_at": "10:00", "service_ends_at": "02:00",
                       "preparation_minutes": 10, "closing_minutes": 20},
    "chats": {"enterprise": {"platform": "telegram", "kind": "enterprise", "id": "11", "calendar": "always"},
              "management": {"platform": "telegram", "kind": "management", "id": "12", "calendar": "always"}},
    "grants": [{"chat": "enterprise", "role": "service_account", "user": "1"},
               {"chat": "management", "role": "manager", "user": "2"}],
}
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    selection = select(cards.c.kind, cards.c.source_key).where(cards.c.enterprise_id == settings["id"])
    schedule_checklists(conn, morning - timedelta(minutes=1))
    assert not conn.execute(selection).all()
    for _ in range(2):
        schedule_checklists(conn, morning)
    assert conn.execute(selection).all() == [("morning_checklist", "local:2026-11-23")]
    schedule_checklists(conn, evening + timedelta(minutes=21))
    assert len(conn.execute(selection).all()) == 1
    for _ in range(2):
        schedule_checklists(conn, evening)
    assert set(conn.execute(selection).all()) == {
        ("morning_checklist", "local:2026-11-23"), ("evening_checklist", "local:2026-11-23"),
    }
    assert len(conn.execute(select(deliveries.c.id).join(cards).where(
        cards.c.enterprise_id == settings["id"], deliveries.c.signal == "start-checklist",
    )).all()) == 2
    conn.rollback()
print("Configured shift timing, after-midnight business day, and startup deduplication verified")
