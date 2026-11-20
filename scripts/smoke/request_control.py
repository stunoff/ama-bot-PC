from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.processes import cards, claim_delivery, deliveries, finish_delivery, get_card
from ama_rs.reminders import after_delivery, queue_due_reminders
from ama_rs.requests import make_task, open_technical_request, review_result, set_technical_plan, submit_result, take_request

now = datetime(2026, 11, 23, 9, tzinfo=UTC)
kinds = ("enterprise", "technical", "technical_management", "headquarters", "management", "branch", "partner")
settings = {
    "id": "control-test", "iiko_id": "control-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {kind: {"kind": kind, "id": str(i), "platform": "telegram", "calendar": "always"}
              for i, kind in enumerate(kinds)},
    "grants": [
        {"chat": "enterprise", "role": "service_account", "user": "0"},
        {"chat": "technical", "role": "technician", "user": "1"},
        {"chat": "management", "role": "manager", "user": "4"},
    ],
}

def confirm(connection, control_id, revision, target, instant):
    delivery_id = connection.scalar(select(deliveries.c.id).where(
        deliveries.c.card_id == control_id, deliveries.c.signal == f"request-control:{revision}"))
    delivery = claim_delivery(connection, delivery_id, instant)
    assert delivery["chat_key"] == target
    assert finish_delivery(connection, delivery_id, delivery["attempt_id"], external_id=str(revision))
    after_delivery(connection, delivery, instant)

with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    employee = Actor("telegram", "0", "0")
    worker = Actor("telegram", "1", "1")
    work = open_technical_request(conn, settings["id"], "tech", "other", {"inspect": "done"},
        "Issue", "high", {"chat": "enterprise", "role": "service_account"}, employee, now)
    source = work["id"]
    breach = now + timedelta(minutes=30)
    queue_due_reminders(conn, breach)
    queue_due_reminders(conn, breach)
    control = conn.scalars(select(cards.c.id).where(
        cards.c.kind == "request_control", cards.c.parent_id == source)).one()
    queue_due_reminders(conn, breach + timedelta(minutes=60))
    assert get_card(conn, control)["assignment"]["role"] == "technical_director"
    delivered = breach + timedelta(minutes=61)
    confirm(conn, control, 1, "technical_management", delivered)
    upper = delivered + timedelta(minutes=30)
    queue_due_reminders(conn, upper)
    assert get_card(conn, control)["assignment"]["role"] == "operations_director"
    confirm(conn, control, 2, "headquarters", upper)
    final = upper + timedelta(minutes=60)
    queue_due_reminders(conn, final)
    assert get_card(conn, control)["assignment"]["role"] == "general_director"
    assert get_card(conn, source)["status"] == "open"
    assert take_request(conn, source, "take", 1, worker, final) == 2
    assert submit_result(conn, source, "result", 2, worker, "Fixed", final) == 3
    assert review_result(conn, source, "review", 3, employee, True, "", final) == 4
    assert get_card(conn, control)["status"] == "closed"
    second = open_technical_request(conn, settings["id"], "tech-2", "other", {"inspect": "done"},
        "Issue", "high", {"chat": "enterprise", "role": "service_account"}, employee, now)
    second_id = second["id"]
    assert take_request(conn, second_id, "take", 1, worker, now) == 2
    next_control = select(cards.c.id).where(cards.c.kind == "request_control", cards.c.parent_id == second_id)
    queue_due_reminders(conn, now + timedelta(minutes=29))
    assert conn.scalar(next_control) is None
    queue_due_reminders(conn, now + timedelta(minutes=30))
    assert conn.scalar(next_control) is not None
    third = open_technical_request(conn, settings["id"], "tech-3", "other", {"inspect": "done"},
        "Issue", "critical", {"chat": "enterprise", "role": "service_account"}, employee, now)
    third_id = third["id"]
    assert take_request(conn, third_id, "take", 1, worker, now) == 2
    promised = now + timedelta(hours=1)
    assert set_technical_plan(conn, third_id, "plan", 2, "Check network", promised, worker, now) == 3
    planned_control = select(cards.c.id).where(cards.c.kind == "request_control", cards.c.parent_id == third_id)
    queue_due_reminders(conn, promised)
    queue_due_reminders(conn, promised + timedelta(minutes=4))
    assert conn.scalar(planned_control) is None
    queue_due_reminders(conn, promised + timedelta(minutes=5))
    assert conn.scalar(planned_control) is not None
    save_enterprise(conn, settings | {"final_route": "partner"}, 1, "test")
    task = make_task(conn, settings["id"], "task", "Inspect", "Verified",
        final + timedelta(minutes=30), {"chat": "enterprise", "role": "service_account", "user": "0"},
        {"chat": "management", "role": "manager"}, Actor("telegram", "4", "4"), final)
    task_control_at = final + timedelta(minutes=60)
    queue_due_reminders(conn, task_control_at)
    task_control = conn.scalars(select(cards.c.id).where(
        cards.c.kind == "request_control", cards.c.parent_id == task["id"])).one()
    confirm(conn, task_control, 1, "branch", task_control_at)
    partner_at = task_control_at + timedelta(minutes=20)
    queue_due_reminders(conn, partner_at)
    assert get_card(conn, task_control)["assignment"]["role"] == "franchise_partner"
    confirm(conn, task_control, 2, "partner", partner_at)
    queue_due_reminders(conn, partner_at + timedelta(hours=1))
    assert get_card(conn, task_control)["revision"] == 2
    assert get_card(conn, task["id"])["status"] == "open"
    conn.rollback()
print("Technical and task escalation chains, delivery gates and source closure verified")
