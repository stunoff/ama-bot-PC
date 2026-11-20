from datetime import UTC, datetime
from sqlalchemy import func, select
from ama_rs.db import create_database_engine
from ama_rs.enterprises import (
    Actor, ConfigurationConflict, chat_is_open, get_enterprise, has_role, history, save_enterprise,
)

data = {
    "id": "test-enterprise", "iiko_id": "test-iiko", "name": "Test",
    "timezone": "Europe/Moscow", "format": "delivery",
    "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters",
    "brands": ["Test"],
    "chats": {
        "enterprise": {
            "id": "42", "kind": "enterprise", "platform": "telegram",
            "calendar": [{"weekday": 0, "start": "20:00", "end": "02:00"}],
            "calendar_exceptions": {"2026-11-24": []},
        },
    },
    "grants": [
        {"chat": "enterprise", "role": "manager", "user": "7"},
        {"chat": "enterprise", "role": "service_account", "user": "8", "ends_at": "2026-11-23T22:00+03:00"},
    ],
}
now = datetime(2026, 11, 23, 18, tzinfo=UTC)
assert chat_is_open(data, "enterprise", now)
assert not chat_is_open(data, "enterprise", datetime(2026, 11, 23, 22, tzinfo=UTC))
assert has_role(data, Actor("telegram", "7", "42"), "enterprise", "manager", now, work=False)
assert not has_role(data, Actor("telegram", "7", "42"), "enterprise", "manager", now)
assert not has_role(data, Actor("max", "8", "42"), "enterprise", "service_account", now)
assert has_role(data, Actor("telegram", "8", "42"), "enterprise", "service_account", now)
assert not has_role(data, Actor("telegram", "8", "42"), "enterprise", "service_account",
                    datetime(2026, 11, 23, 19, tzinfo=UTC))
engine = create_database_engine()
with engine.begin() as conn:
    assert save_enterprise(conn, data, 0, "test") == 1
    data["name"] = "Renamed"
    assert save_enterprise(conn, data, 1, "test") == 2
    assert get_enterprise(conn, data["id"])["settings"]["name"] == "Renamed"
    try:
        save_enterprise(conn, data, 1, "test")
    except ConfigurationConflict:
        pass
    else:
        raise AssertionError("Stale revision accepted")
    assert conn.scalar(select(func.count()).select_from(history)) == 2
    conn.rollback()
print("Enterprise storage, history, access, overnight calendar, and date exception verified")
