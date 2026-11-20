from datetime import UTC, datetime, timedelta

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.personnel import (
    acknowledge_training, claim_invite, confirm_link, issue_invite, personal_schedule,
    record_skill_check, record_work_time, synchronize_person,
)
from ama_rs.processes import ProcessConflict, ProcessPermissionError

now = datetime(2026, 11, 23, 18, tzinfo=UTC)
settings = {
    "id": "people-test", "iiko_id": "people-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {"management": {"kind": "management", "id": "41", "platform": "telegram"}},
    "grants": [{"chat": "management", "role": "manager", "user": "7"}],
}
manager, employee = Actor("telegram", "7", "41"), Actor("telegram", "8", "8")
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    person = synchronize_person(conn, settings["id"], "employee-1", "Test", "kitchen")
    key = person["id"]
    assert synchronize_person(conn, settings["id"], "employee-1", "Test 2", "kitchen")["id"] == key
    invite = issue_invite(conn, key, manager, now)
    assert claim_invite(conn, invite, employee, now) == key
    try:
        claim_invite(conn, invite, employee, now)
    except ProcessConflict:
        pass
    else:
        raise AssertionError("Invite reused")
    assert confirm_link(conn, key, 3, True, manager, now) == 4
    assert record_work_time(conn, key, "planned", "schedule1", now, now + timedelta(hours=4), now)
    assert not record_work_time(conn, key, "planned", "schedule1", now, now + timedelta(hours=4), now)
    assert len(personal_schedule(conn, key, employee)) == 1
    try:
        personal_schedule(conn, key, manager)
    except ProcessPermissionError:
        pass
    else:
        raise AssertionError("Private schedule exposed")
    for module in ("safety", "production", "sanitation"):
        acknowledge_training(conn, key, module, employee, now)
    assert record_skill_check(conn, key, manager, now, "approved", "OK") == 8
    conn.rollback()
print("Personnel sync, one-time binding, private schedule, training and skill check verified")
