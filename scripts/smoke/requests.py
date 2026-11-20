from datetime import UTC, datetime, timedelta

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.processes import ProcessConflict, get_card
from ama_rs.requests import (
    finish_linked_problem, make_task, open_technical_request, report_problem,
    resolve_problem, review_result, submit_result, take_request,
)

now = datetime(2026, 11, 23, 18, tzinfo=UTC)
kinds = ("enterprise", "management", "technical", "vipart")
roles = ("service_account", "manager", "technician", "vipart_specialist")
settings = {
    "id": "work-test", "iiko_id": "work-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {kind: {"kind": kind, "id": str(i), "platform": "telegram", "calendar": "always"}
              for i, kind in enumerate(kinds)},
    "grants": [{"chat": kind, "role": role, "user": str(i)}
               for i, (kind, role) in enumerate(zip(kinds, roles))],
}
worker, manager, _, vipart = (Actor("telegram", str(i), str(i)) for i in range(4))
recipient = {"chat": "enterprise", "role": "service_account", "user": "0"}
reviewer = {"chat": "management", "role": "manager"}
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    problem = report_problem(conn, settings["id"], "problem", "broken", worker, now)
    task = make_task(conn, settings["id"], "task", "Fix", "Working", now + timedelta(hours=1),
                     recipient, reviewer, manager, now, parent_id=problem["id"])
    assert resolve_problem(conn, problem["id"], "link", 1, "task", manager, now,
                           note="Task", linked_card_id=task["id"]) == 2
    try:
        finish_linked_problem(conn, problem["id"], "close", 2, manager, now)
    except ProcessConflict:
        pass
    else:
        raise AssertionError("Problem closed before linked work")
    assert take_request(conn, task["id"], "take", 1, worker, now) == 2
    assert submit_result(conn, task["id"], "submit", 2, worker, "Fixed", now) == 3
    assert review_result(conn, task["id"], "accept", 3, manager, True, "", now) == 4
    assert finish_linked_problem(conn, problem["id"], "close", 2, manager, now) == 3
    night = open_technical_request(conn, settings["id"], "tech", "other", {"inspect": "done"},
                                   "Broken", "high", recipient, worker, now)
    assert night["assignment"]["role"] == "vipart_specialist"
    assert take_request(conn, night["id"], "take", 1, vipart, now,
                        next_action="Inspect", next_due=now + timedelta(minutes=10)) == 2
    assert get_card(conn, night["id"])["assignment"]["user"] == "3"
    conn.rollback()
print("Linked problems, task review, and VIPART routing verified")
