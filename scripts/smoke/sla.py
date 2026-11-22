from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.processes import cards, claim_delivery, deliveries, finish_delivery, get_card, timers
from ama_rs.refund_control import take_control
from ama_rs.refunds import (answer_refund_question, ask_refund_question, record_refund_result,
                            request_refund, take_refund)
from ama_rs.reminders import after_delivery, queue_due_reminders
from ama_rs.requests import make_task, submit_result, take_request

now = datetime(2026, 11, 23, 10, tzinfo=UTC)
kinds = ("enterprise", "management", "refunds", "branch", "headquarters")
roles = ("service_account", "manager", "finance_executor", "branch_lead", "operations_director")
settings = {
    "id": "sla-test", "iiko_id": "sla-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {kind: {"kind": kind, "id": str(i), "platform": "telegram", "calendar": "always"}
              for i, kind in enumerate(kinds)},
    "grants": [{"chat": kind, "role": role, "user": str(i)}
               for i, (kind, role) in enumerate(zip(kinds, roles))],
}
settings["chats"]["management"]["calendar"] = [
    {"weekday": 0, "start": "13:00", "end": "13:15"},
    {"weekday": 1, "start": "13:00", "end": "15:00"},
]
worker, manager, finance = (Actor("telegram", str(i), str(i)) for i in range(3))
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    refund = request_refund(conn, settings["id"], "refund", "Demo", "Order 1", "Reason", 500,
                            worker, now)
    refund_id = refund["id"]
    assert take_refund(conn, refund_id, "take", 1, finance, now + timedelta(minutes=5)) == 2
    assert ask_refund_question(conn, refund_id, "question", 2, "Question", finance,
                               now + timedelta(minutes=10)) == 3
    deadline = conn.execute(select(timers).where(timers.c.card_id == refund_id,
                            timers.c.key == "financial_deadline")).mappings().one()
    assert deadline["card_revision"] == 3
    assert deadline["due_at"] == (now + timedelta(hours=24)).replace(tzinfo=None)
    late = now + timedelta(hours=24)
    queue_due_reminders(conn, late)
    financial = conn.execute(select(deliveries).where(deliveries.c.card_id == refund_id,
                             deliveries.c.signal.like("timer:%"))).mappings().one()
    assert financial["chat_key"] == "refunds"
    assert financial["payload"]["await_financial_receipt"]
    claimed = claim_delivery(conn, financial["id"], late)
    assert finish_delivery(conn, financial["id"], claimed["attempt_id"], external_id="msg1")
    after_delivery(conn, claimed, late)
    after_finance = late + timedelta(minutes=10)
    linked = select(cards.c.id).where(cards.c.parent_id == refund_id, cards.c.kind == "refund_control")
    queue_due_reminders(conn, late + timedelta(minutes=9))
    assert conn.scalar(linked) is None
    queue_due_reminders(conn, after_finance)
    queue_due_reminders(conn, after_finance)
    control_id = conn.scalars(linked).one()
    branch_notice = conn.scalar(select(deliveries.c.id).where(
        deliveries.c.card_id == control_id, deliveries.c.signal == "control:branch"))
    notice = claim_delivery(conn, branch_notice, after_finance)
    assert notice["chat_key"] == "branch"
    assert finish_delivery(conn, branch_notice, notice["attempt_id"], external_id="branch")
    after_delivery(conn, notice, after_finance)
    queue_due_reminders(conn, after_finance + timedelta(minutes=39))
    assert get_card(conn, control_id)["status"] == "branch"
    final_at = after_finance + timedelta(minutes=40)
    queue_due_reminders(conn, final_at)
    assert get_card(conn, control_id)["assignment"]["chat"] == "headquarters"
    lead = Actor("telegram", "4", "4")
    assert take_control(conn, control_id, "control", 2, "Verify with accounting",
                        final_at + timedelta(hours=1), lead, final_at) == 3
    assert get_card(conn, refund_id)["status"] == "clarification"
    assert answer_refund_question(conn, refund_id, "answer", 3, "Clarified", worker, final_at) == 4
    assert record_refund_result(conn, refund_id, "no-refund", 4, "not_required",
                                "Previously refunded", finance, final_at) == 5
    assert get_card(conn, control_id)["status"] == "closed"

    task = make_task(conn, settings["id"], "task", "Fix", "Working", now + timedelta(hours=1),
                     {"chat": "enterprise", "role": "service_account", "user": "0"},
                     {"chat": "management", "role": "manager"}, manager, now)
    assert take_request(conn, task["id"], "take", 1, worker, now) == 2
    assert submit_result(conn, task["id"], "submit", 2, worker, "Fixed", now) == 3
    assert conn.scalar(select(timers.c.key).where(timers.c.card_id == task["id"],
                       timers.c.key == "review_reminder")) is None
    review = conn.scalar(select(deliveries.c.id).where(deliveries.c.card_id == task["id"],
                         deliveries.c.signal == "review:3"))
    claimed = claim_delivery(conn, review, now)
    assert finish_delivery(conn, review, claimed["attempt_id"], external_id="msg2")
    after_delivery(conn, claimed, now)
    stored = conn.scalar(select(timers.c.due_at).where(timers.c.card_id == task["id"],
                         timers.c.key == "review_reminder"))
    assert stored == (now + timedelta(days=1, minutes=15)).replace(tzinfo=None)
    refund = request_refund(conn, settings["id"], "working-question", "Demo", "Order 2", "Reason", 500,
                            worker, now)
    question_id = refund["id"]
    assert take_refund(conn, question_id, "take", 1, finance, now) == 2
    assert ask_refund_question(conn, question_id, "question", 2, "Check order", finance, now) == 3
    notices = select(deliveries).where(deliveries.c.card_id == question_id)
    question = conn.scalar(notices.with_only_columns(deliveries.c.id).where(deliveries.c.signal == "question:3"))
    assert conn.scalar(select(timers.c.key).where(
        timers.c.card_id == question_id, timers.c.key == "refund_question_first"
    )) is None
    delivered = now + timedelta(minutes=3)
    claim = claim_delivery(conn, question, delivered)
    assert finish_delivery(conn, question, claim["attempt_id"], external_id="question")
    after_delivery(conn, claim, delivered)
    for minutes, suffix, chat in ((10, "first", "enterprise"), (20, "second", "enterprise"),
                                   (40, "escalation", "management")):
        due = delivered + timedelta(minutes=minutes)
        key = f"refund_question_{suffix}"
        expected = notices.where(deliveries.c.signal == f"timer:3:{key}")
        queue_due_reminders(conn, due - timedelta(seconds=1))
        assert conn.execute(expected).first() is None
        queue_due_reminders(conn, due)
        assert conn.execute(expected).mappings().one()["chat_key"] == chat
    escalation = conn.scalar(notices.with_only_columns(deliveries.c.id).where(
        deliveries.c.signal == "timer:3:refund_question_escalation"
    ))
    assert answer_refund_question(conn, question_id, "response", 3, "Done", worker, due) == 4
    assert claim_delivery(conn, escalation, due) is None
    conn.rollback()
print("Unchanged financial deadline, first-delivery escalation gate and working-time review SLA verified")
