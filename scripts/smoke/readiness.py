from datetime import UTC, datetime

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message
from ama_rs.processes import claim_delivery, create_card, deliveries, enqueue, finish_delivery
from ama_rs.reports import import_coverage

now = datetime(2026, 11, 23, 8, tzinfo=UTC)
admin = Actor("telegram", "2", "general", "42")
settings = {
    "id": "readiness-test", "iiko_id": "readiness-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {
        "technical": {"kind": "technical", "platform": "telegram", "id": "general",
                      "general_topic_id": "42", "calendar": "always"},
        "management": {"kind": "management", "platform": "telegram", "id": "mgr", "calendar": "always"},
        "enterprise": {"kind": "enterprise", "platform": "telegram", "id": "work", "calendar": "always"},
    },
    "grants": [{"chat": "technical", "role": "system_admin", "user": "2"},
               {"chat": "technical", "role": "delegated_admin", "user": "5", "fields": ["name"]},
               {"chat": "management", "role": "manager", "user": "3"},
               {"chat": "enterprise", "role": "service_account", "user": "4"}],
}


def screen(connection, event, actor=admin):
    return run_bot_command(connection, Message(event, actor, "/readiness readiness-test"), now)


with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    assert "недоступно" in screen(conn, "delegate", Actor("telegram", "5", "general", "42"))
    before = screen(conn, "before").text
    assert "Смена предприятия — настроено, ожидает проверки доставки: enterprise, management" in before
    assert "Касса — требуется настройка" in before
    assert "orders: нет данных" in before
    card = create_card(conn, settings["id"], "test", "sample",
        {"chat": "management", "role": "manager"}, {}, now)["id"]
    for key in ("enterprise", "management"):
        enqueue(conn, card, "fixture", key, {"kind": "card"}, now)
        delivery_id = conn.scalar(select(deliveries.c.id).where(deliveries.c.card_id == card,
                                  deliveries.c.chat_key == key))
        claimed = claim_delivery(conn, delivery_id, now)
        assert finish_delivery(conn, delivery_id, claimed["attempt_id"], external_id=key)
    import_coverage(conn, settings["id"], {"source": "orders", "day": "2026-11-23", "state": "partial",
                                          "updated_at": now.isoformat()})
    after = screen(conn, "after").text
    assert "Смена предприятия — настроено, есть подтверждённая доставка" in after
    assert "orders: частичные" in after
    revised = settings | {"chats": settings["chats"] | {
        "enterprise": settings["chats"]["enterprise"] | {"id": "work-new"}
    }}
    save_enterprise(conn, revised, 1, "test")
    changed = screen(conn, "changed").text
    assert "Смена предприятия — настроено, ожидает проверки доставки: enterprise" in changed
    assert "server.invalid" not in changed
    conn.rollback()
print("General readiness access, missing routes, source health and exact delivery identity verified")
