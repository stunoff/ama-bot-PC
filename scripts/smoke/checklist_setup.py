from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, get_enterprise, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message
from ama_rs.processes import cards, claim_delivery, deliveries, finish_delivery
from ama_rs.route_probe import confirm_probe, start_probe
from ama_rs.shift_scheduler import schedule_checklists

now = datetime(2026, 11, 23, 7, 50, tzinfo=UTC)
admin = Actor("telegram", "1", "admin", "1")
settings = {
    "id": "checklist-setup-test", "iiko_id": "checklist-setup-iiko",
    "name": "Test", "timezone": "Europe/Moscow", "format": "delivery",
    "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Test"],
    "shift_schedule": {"service_starts_at": "11:00", "service_ends_at": "21:00",
                       "preparation_minutes": 10, "closing_minutes": 20},
    "chats": {
        "admin": {"kind": "technical", "platform": "telegram", "id": "admin", "general_topic_id": "1"},
        "work": {"kind": "enterprise", "platform": "telegram", "id": "work", "calendar": "always"},
        "management": {"kind": "management", "platform": "telegram", "id": "management", "calendar": "always"},
        "cash": {"kind": "cash", "platform": "telegram", "id": "cash", "calendar": "always"},
    },
    "grants": [
        {"chat": "admin", "role": "system_admin", "user": "1"},
        {"chat": "work", "role": "service_account", "user": "2"},
        {"chat": "management", "role": "manager", "user": "3"},
        {"chat": "cash", "role": "cash_reviewer", "user": "4"},
    ],
}

with create_database_engine().begin() as conn:
    initial_chats = {key: chat for key, chat in settings["chats"].items() if key != "cash"}
    initial_grants = [grant for grant in settings["grants"] if grant["chat"] != "cash"]
    save_enterprise(conn, settings | {"chats": initial_chats, "grants": initial_grants}, 0, "test")

    def command(event, text, callback=False):
        return run_bot_command(conn, Message(event, admin, text, callback_id=event if callback else None), now)

    setup = "/checklist_setup checklist-setup-test"
    toggle = "/configure checklist-setup-test scheduled_checklists true"
    assert "Маршруты и роли — требуют проверки" in command("initial", setup).text
    assert "недоступно" in command("blocked", toggle)
    add = command("add-cash", '/configure checklist-setup-test checklist_route '
                  '{"kind":"cash","platform":"telegram","id":"-1004","user":"4"}')
    assert "Версия 2" in command("add-save", dict(add.buttons)["Сохранить"], callback=True)
    calendar = command("cash-calendar", '/configure checklist-setup-test chat_calendar '
                       '{"chat":"cash","calendar":"always"}')
    assert "Версия 3" in command("calendar-save", dict(calendar.buttons)["Сохранить"], callback=True)
    for key, user, chat_id in (("work", "2", "work"), ("management", "3", "management"),
                              ("cash", "4", "-1004")):
        probe = start_probe(conn, settings["id"], key, f"probe-{key}", 3, admin, now)
        delivery_id = conn.scalar(select(deliveries.c.id).where(deliveries.c.card_id == probe))
        attempt = claim_delivery(conn, delivery_id, now)
        assert finish_delivery(conn, delivery_id, attempt["attempt_id"], external_id=f"fixture-{key}")
        confirm_probe(conn, probe, f"confirm-{key}", Actor("telegram", user, chat_id), now)

    ready = command("ready", setup)
    prepare = dict(ready.buttons)["Подготовить включение"]
    preview = command("preview", prepare, callback=True)
    assert not get_enterprise(conn, settings["id"])["settings"].get("scheduled_checklists")
    assert "Версия 4" in command("save", dict(preview.buttons)["Сохранить"], callback=True)
    query = select(cards.c.id).where(cards.c.enterprise_id == settings["id"],
                                    cards.c.kind == "morning_checklist")
    schedule_checklists(conn, now)
    first = conn.scalars(query).one()

    current = get_enterprise(conn, settings["id"])["settings"]
    changed = current | {"chats": current["chats"] | {"cash": current["chats"]["cash"] | {"id": "other-cash"}}}
    save_enterprise(conn, changed, 4, "test")
    schedule_checklists(conn, now + timedelta(days=1))
    assert conn.scalars(query).all() == [first]
    assert "cash: требуется ответ" in command("changed", setup).text
    conn.rollback()
print("Checklist setup activation, required role confirmations and suspension after route change verified")
