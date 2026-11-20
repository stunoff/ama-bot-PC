from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message
from ama_rs.processes import cards, claim_delivery, deliveries, finish_delivery, get_card

now = datetime(2026, 11, 23, 8, tzinfo=UTC)
admin = Actor("telegram", "1", "tech", "11")
recipient = Actor("telegram", "2", "enterprise", "22")
settings = {
    "id": "route-test", "iiko_id": "route-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {
        "technical": {"kind": "technical", "platform": "telegram", "id": "tech",
                      "general_topic_id": "11", "calendar": "always"},
        "work": {"kind": "enterprise", "platform": "telegram", "id": "enterprise",
                 "topic_id": "22", "calendar": "always"},
    },
    "grants": [{"chat": "technical", "role": "system_admin", "user": "1"},
               {"chat": "work", "role": "service_account", "user": "2"}],
}

with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    def command(event, actor, text, instant=now, callback=False, file_id=None):
        message = Message(event, actor, text, file_id=file_id, callback_id=event if callback else None)
        return run_bot_command(conn, message, instant)

    listing = command("list", admin, "/route_tests route-test")
    assert "проверка не запускалась" in listing.text
    button = dict(listing.buttons)["Проверить work"]
    assert "поставлена" in command("start", admin, button, callback=True)
    second = command("again", admin, "/route_tests route-test")
    assert "в очереди" in second.text
    button = dict(second.buttons)["Проверить work"]
    assert "поставлена" in command("again-start", admin, button, callback=True)
    probe = conn.scalars(select(cards.c.id).where(
        cards.c.enterprise_id == settings["id"], cards.c.kind == "route_probe")).one()
    assert "недоступно" in command("early", recipient, f"/route_confirm {probe}")
    delivery = conn.scalars(select(deliveries.c.id).where(deliveries.c.card_id == probe)).one()
    attempt = claim_delivery(conn, delivery, now)
    assert finish_delivery(conn, delivery, attempt["attempt_id"], external_id="fixture")
    assert "недоступно" in command("wrong-topic", Actor("telegram", "2", "enterprise", "23"),
                                  f"/route_confirm {probe}")
    assert "недоступно" in command("wrong-user", Actor("telegram", "3", "enterprise", "22"),
                                  f"/route_confirm {probe}")
    assert "подтверждены" in command("confirm", recipient, f"/route_confirm {probe}", file_id="fixture-photo")
    assert get_card(conn, probe)["status"] == "accepted"
    assert "доставка и ответ подтверждены, фото принято" in command("confirmed", admin, "/route_tests route-test").text
    changed = settings | {"chats": settings["chats"] | {
        "work": settings["chats"]["work"] | {"topic_id": "25"}
    }}
    save_enterprise(conn, changed, 1, "test")
    new = command("changed", admin, "/route_tests route-test")
    assert "work: проверка не запускалась" in new.text
    assert "поставлена" in command("new-start", admin, dict(new.buttons)["Проверить work"], callback=True)
    later = now + timedelta(hours=25)
    expiry = conn.scalars(select(deliveries.c.id).where(
        deliveries.c.card_id != probe, deliveries.c.signal == "route-probe")).one()
    assert claim_delivery(conn, expiry, later) is None
    assert "проверка истекла" in command("expired", admin, "/route_tests route-test", later).text
    conn.rollback()
print("Route tests, deduplication, confirmed delivery, assigned responder, topic changes and expiry verified")
