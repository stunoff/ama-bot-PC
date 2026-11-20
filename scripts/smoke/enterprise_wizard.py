import json
from datetime import UTC, datetime

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, get_enterprise, history, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message

now = datetime(2026, 11, 23, 8, tzinfo=UTC)
admin = Actor("telegram", "1", "general", "17")
other = Actor("telegram", "2", "general", "17")
values = {
    "id": "enterprise-wizard-test", "iiko_id": "enterprise-wizard-iiko", "name": "New Enterprise",
    "timezone": "Europe/Moscow", "format": "delivery", "server_address": "server.invalid",
    "lm_address": "lm.invalid", "contact_mode": "independent", "final_route": "headquarters",
    "brands": ["Demo"],
}
seed_grants = [{"chat": "technical", "role": "system_admin", "user": "1", "create_enterprises": True},
               {"chat": "technical", "role": "system_admin", "user": "2"}]
seed = values | {"id": "wizard-seed", "iiko_id": "seed-iiko", "chats": {
    "technical": {"kind": "technical", "platform": "telegram", "id": "general", "general_topic_id": "17"}},
    "grants": seed_grants}

with create_database_engine().begin() as conn:
    save_enterprise(conn, seed, 0, "test")

    def command(event, actor, text, callback=False):
        return run_bot_command(conn, Message(event, actor, text, callback_id=event if callback else None), now)

    assert "Новое предприятие" in dict(command("settings", admin, "/settings").buttons)
    assert "Новое предприятие" not in dict(command("other-settings", other, "/settings").buttons)
    assert "недоступно" in command("other", other, "/enterprise_new")
    assert "недоступно" in command("wrong-topic", Actor("telegram", "1", "general", "18"), "/enterprise_new")
    request = "/enterprise_new " + json.dumps(values)
    first = command("input", admin, request)
    assert "server.invalid" not in first.text and "lm.invalid" not in first.text
    assert get_enterprise(conn, values["id"]) is None
    button = dict(first.buttons)["Создать предприятие"]
    assert "недоступно" in command("foreign", other, button, callback=True)
    revoked = seed | {"grants": [{k: v for k, v in grant.items() if k != "create_enterprises"}
                                 for grant in seed_grants]}
    save_enterprise(conn, revoked, 1, "test")
    assert "недоступно" in command("revoked", admin, button, callback=True)
    assert get_enterprise(conn, values["id"]) is None
    save_enterprise(conn, seed, 2, "test")

    preview = command("new-input", admin, request)
    button = dict(preview.buttons)["Создать предприятие"]
    duplicate = command("duplicate-input", admin, request)
    assert "создано" in command("create", admin, button, callback=True).text
    assert "недоступно" in command("duplicate-create", admin, dict(duplicate.buttons)["Создать предприятие"], callback=True)
    entry = get_enterprise(conn, values["id"])
    assert entry["settings"]["grants"] == [{"chat": "technical", "user": "1", "role": "system_admin"}]
    assert len(entry["settings"]["chats"]) == 1 and not entry["settings"].get("scheduled_checklists")
    assert conn.scalar(select(history.c.operator).where(history.c.enterprise_id == values["id"])) == "telegram:1"
    assert "недоступно" in command("duplicate-iiko", admin,
        "/enterprise_new " + json.dumps(values | {"id": "other-new"}))
    conn.rollback()
print("Enterprise creation, bootstrap scope, revoked confirmation, and distinct IDs verified")
