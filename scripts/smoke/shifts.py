from datetime import UTC, date, datetime
from sqlalchemy import select
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.processes import ProcessConflict, attachments, cards, get_card
from ama_rs.shifts import (
    accept_cash, accept_checklist, add_cash_document, add_checklist_photo,
    answer_cash_clarification, begin_checklist, checklist_schedule, complete_step,
    confirm_iiko_closure, request_cash_clarification, submit_cash,
)

now = datetime(2026, 11, 23, 9, tzinfo=UTC)
kinds = ("enterprise", "management", "cash")
roles = ("service_account", "manager", "cash_reviewer")
settings = {
    "id": "shift-test", "iiko_id": "shift-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {kind: {"kind": kind, "id": str(i), "platform": "telegram", "calendar": "always"}
              for i, kind in enumerate(kinds)},
    "grants": [{"chat": kind, "role": role, "user": str(i)}
               for i, (kind, role) in enumerate(zip(kinds, roles))],
    "shift_schedule": {"service_starts_at": "10:00", "service_ends_at": "02:00",
                       "preparation_minutes": 10, "closing_minutes": 20},
}
employee, manager, cashier = (Actor("telegram", str(n), str(n)) for n in range(3))
assert checklist_schedule(settings, date(2026, 11, 23))["evening"].day == 24

with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    morning = begin_checklist(conn, settings["id"], "morning", "shift-id", now)
    assert morning["id"] == begin_checklist(conn, settings["id"], "morning", "shift-id", now)["id"]
    for idx, step in enumerate(morning["data"]["steps"], 1):
        key = step["key"]
        for slot in step["photos"]:
            add_checklist_photo(conn, morning["id"], f"m-photo:{idx}:{slot}", key, slot, "file", employee, now)
        arguments = (conn, morning["id"], f"m:{idx}", idx, key, "opened" if key == "opening" else "ok", employee, now)
        assert complete_step(*arguments) == idx + 1
        assert complete_step(*arguments) == idx + 1
    morning = get_card(conn, morning["id"])
    assert accept_checklist(conn, morning["id"], "accept", morning["revision"], manager, now)
    cash_id = conn.scalar(select(cards.c.id).where(cards.c.kind == "cash_shift"))
    assert conn.scalar(select(attachments.c.card_id).where(attachments.c.item == "morning:receipt")) == cash_id
    evening = begin_checklist(conn, settings["id"], "evening", "shift-id", now)
    for idx, step in enumerate(evening["data"]["steps"], 1):
        key = step["key"]
        if key == "evening_cash":
            for doc in ("iiko_report", "stationary", "signed_count"):
                assert add_cash_document(conn, evening["id"], f"cash:{doc}", doc, "file", employee, now) == cash_id
            assert submit_cash(conn, cash_id, "submit", 1, employee, now,
                               no_pending_operations=True, expense_documents=[]) == 2
            try:
                accept_cash(conn, cash_id, "accept", 2, "accepted", cashier, now)
            except ProcessConflict:
                pass
            else:
                raise AssertionError("Cash accepted before iiko closure")
            assert request_cash_clarification(conn, cash_id, "question", 2, "Question", cashier, now) == 3
            assert confirm_iiko_closure(conn, settings["id"], "shift-id", "event-1", now)
            assert not confirm_iiko_closure(conn, settings["id"], "shift-id", "event-1", now)
            assert answer_cash_clarification(conn, cash_id, "answer", 4, "Answer", employee, now) == 5
            assert accept_cash(conn, cash_id, "accept", 5, "accepted", cashier, now) == 6
        for slot in step["photos"]:
            add_checklist_photo(conn, evening["id"], f"e-photo:{idx}:{slot}", key, slot, "file", employee, now)
        assert complete_step(conn, evening["id"], f"e:{idx}", idx, key, "ok", employee, now) == idx + 1
    assert get_card(conn, evening["id"])["status"] == "review"
    conn.rollback()
print("Morning/evening checks, cash route, clarification and iiko acceptance verified")
