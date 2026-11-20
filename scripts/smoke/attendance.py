from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from ama_rs.attendance import answer_explanation, request_explanation, scan_attendance
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.personnel import people, record_work_time, synchronize_person
from ama_rs.processes import cards, get_card
from ama_rs.reports import import_coverage

start = datetime(2026, 11, 23, 8, tzinfo=UTC)
end = start + timedelta(hours=8)
settings = {
    "id": "attendance-test", "iiko_id": "attendance-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "attendance_monitoring": True,
    "chats": {"management": {"kind": "management", "platform": "telegram", "id": "mgr", "calendar": "always"},
              "personal": {"kind": "personal", "platform": "telegram", "id": "17", "calendar": "always"}},
    "grants": [{"chat": "management", "role": "manager", "user": "2"},
               {"chat": "personal", "role": "employee", "user": "17"}],
}
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    person = synchronize_person(conn, settings["id"], "employee-1", "Test employee", "kitchen")
    person_id = person["id"]
    conn.execute(update(people).where(people.c.id == person_id)
                 .values(state="active", platform="telegram", user_id="17"))
    assert record_work_time(conn, person_id, "planned", "plan", start, end, start)
    result = select(cards).where(cards.c.enterprise_id == settings["id"], cards.c.kind == "attendance")
    five = start + timedelta(minutes=5)
    for source, state in (("schedule", "complete"), ("attendance", "partial")):
        import_coverage(conn, settings["id"], {"day": "2026-11-23", "source": source, "state": state,
                                               "updated_at": start.isoformat()})
    scan_attendance(conn, five)
    assert not conn.execute(result).all()
    import_coverage(conn, settings["id"], {"day": "2026-11-23", "source": "attendance", "state": "complete",
                                           "updated_at": five.isoformat()})
    scan_attendance(conn, five)
    scan_attendance(conn, five)
    missing = conn.execute(result).mappings().one()
    assert missing["data"]["deviation"] == "no_arrival"
    assert record_work_time(conn, person_id, "actual", "first", start + timedelta(minutes=1), None, five)
    scan_attendance(conn, five)
    scan_attendance(conn, five)
    rows = {row["data"]["deviation"]: row for row in conn.execute(result).mappings()}
    assert rows["no_arrival"]["status"] == "closed"
    late = rows["late"]
    assert late["data"]["delta_minutes"] == 1
    assert request_explanation(conn, late["id"], "ask", 1, "Почему?", Actor("telegram", "2", "mgr"), five) == 2
    assert answer_explanation(conn, late["id"], "reply", 2, "Транспорт", "", Actor("telegram", "17", "17"), five) == 3
    assert get_card(conn, late["id"])["status"] == "review"
    at_exit = end + timedelta(minutes=15)
    import_coverage(conn, settings["id"], {"day": "2026-11-23", "source": "attendance", "state": "complete",
                                           "updated_at": at_exit.isoformat()})
    scan_attendance(conn, at_exit)
    rows = {row["data"]["deviation"]: row for row in conn.execute(result).mappings()}
    assert rows["unclosed"]["status"] == "open"
    assert record_work_time(conn, person_id, "actual", "first", start + timedelta(minutes=1),
                            end - timedelta(minutes=7), at_exit)
    scan_attendance(conn, at_exit)
    rows = {row["data"]["deviation"]: row for row in conn.execute(result).mappings()}
    assert rows["unclosed"]["status"] == "closed"
    assert rows["early_leave"]["data"]["delta_minutes"] == 7
    conn.rollback()
print("Confirmed coverage, attendance deduplication, late arrival, missing exit, early departure, and private explanations verified")
