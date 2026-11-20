from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, get_enterprise, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message
from ama_rs.processes import cards, claim_delivery, deliveries, finish_delivery
from ama_rs.reports import import_coverage, schedule_reports
from ama_rs.route_probe import confirm_probe, start_probe

now = datetime(2026, 11, 23, 8, tzinfo=UTC)
admin = Actor("telegram", "1", "general", "17")
enterprise_id = "scenario-setup-test"
settings = {
    "id": enterprise_id, "iiko_id": enterprise_id, "name": "Test", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Test"],
    "chats": {
        "admin": {"kind": "technical", "platform": "telegram", "id": "general", "general_topic_id": "17"},
        "management": {"kind": "management", "platform": "telegram", "id": "manager", "calendar": "always"},
        "branch": {"kind": "branch", "platform": "telegram", "id": "branch", "calendar": "always"},
    },
    "grants": [
        {"chat": "admin", "role": "system_admin", "user": "1"},
        {"chat": "management", "role": "manager", "user": "2"},
        {"chat": "branch", "role": "branch_lead", "user": "3"},
    ],
}

with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")

    def command(event, text, callback=False):
        return run_bot_command(conn, Message(event, admin, text, callback_id=event if callback else None), now)

    def coverage(source, state, updated_at=now):
        import_coverage(conn, enterprise_id, {
            "source": source, "day": "2026-11-23", "state": state, "updated_at": updated_at.isoformat(),
        })

    assert "orders: нужна" in command("view", f"/scenarios {enterprise_id}").text
    for key, user, chat in (("management", "2", "manager"), ("branch", "3", "branch")):
        probe = start_probe(conn, enterprise_id, key, f"probe-{key}", 1, admin, now)
        delivery_id = conn.scalar(select(deliveries.c.id).where(deliveries.c.card_id == probe))
        sent = claim_delivery(conn, delivery_id, now)
        assert finish_delivery(conn, delivery_id, sent["attempt_id"], external_id=f"fixture-{key}")
        confirm_probe(conn, probe, f"confirmed-{key}", Actor("telegram", user, chat), now)

    report_toggle = f"/configure {enterprise_id} scheduled_reports true"
    assert "недоступно" in command("blocked", report_toggle)
    coverage("orders", "partial", now - timedelta(minutes=2))
    stale = command("stale-preview", report_toggle)
    coverage("orders", "unavailable", now - timedelta(minutes=1))
    assert "недоступно" in command("stale-save", dict(stale.buttons)["Сохранить"], callback=True)
    assert not get_enterprise(conn, enterprise_id)["settings"].get("scheduled_reports")
    coverage("orders", "complete")
    overview = command("eligible", f"/scenarios {enterprise_id}")
    report = command("prepare", dict(overview.buttons)["Включить: Плановые сводки"], callback=True)
    assert "Версия 2" in command("save", dict(report.buttons)["Сохранить"], callback=True)
    assert "Включить: Контроль рабочего времени" not in dict(overview.buttons)
    coverage("schedule", "complete")
    coverage("attendance", "partial")
    attendance = command("attendance", f"/configure {enterprise_id} attendance_monitoring true")
    assert "Версия 3" in command("attendance-save", dict(attendance.buttons)["Сохранить"], callback=True)

    schedule_reports(conn, now)
    query = select(cards.c.id).where(cards.c.enterprise_id == enterprise_id, cards.c.kind == "report")
    before = conn.scalars(query).all()
    assert before
    active = get_enterprise(conn, enterprise_id)["settings"]
    changed = active | {"chats": active["chats"] | {"branch": active["chats"]["branch"] | {"id": "branch-2"}}}
    save_enterprise(conn, changed, 3, "test")
    schedule_reports(conn, now + timedelta(days=1))
    assert conn.scalars(query).all() == before
    conn.rollback()
print("Scenario-specific iiko evidence, independent activation, stale confirmation, and changed routes verified")
