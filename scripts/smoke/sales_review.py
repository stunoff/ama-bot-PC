from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import ingest_iiko, run_bot_command
from ama_rs.messengers import Message
from ama_rs.processes import ProcessConflict, cards, claim_delivery, deliveries, finish_delivery, get_card
from ama_rs.reminders import after_delivery, queue_due_reminders
from ama_rs.reports import summary
from ama_rs.requests import review_result, submit_result, take_request
from ama_rs.sales_review import accept_sale_explanation, ask_sale_question, assign_sale_task, attach_sale_photo, submit_sale_explanation

now = datetime(2026, 11, 23, 8, tzinfo=UTC)
employee = Actor("telegram", "1", "work")
manager = Actor("telegram", "2", "manager")
settings = {
    "id": "sales-test", "iiko_id": "sales-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {
        "management": {"kind": "management", "platform": "telegram", "id": "manager", "calendar": "always"},
        "work": {"kind": "enterprise", "platform": "telegram", "id": "work", "calendar": "always"},
    },
    "grants": [{"chat": "management", "role": "manager", "user": "2"},
               {"chat": "work", "role": "service_account", "user": "1"}],
}
event = {"id": "writeoff-1", "enterprise_id": "sales-test", "type": "sales.writeoff",
         "data": {"document_id": "doc1", "amount_kopecks": 1299}}

with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    assert ingest_iiko(conn, event, now)
    assert not ingest_iiko(conn, event, now)
    card = conn.scalars(select(cards.c.id).where(cards.c.enterprise_id == settings["id"])).one()
    request = conn.scalar(select(deliveries.c.id).where(deliveries.c.card_id == card,
                          deliveries.c.signal == "explain"))
    claim = claim_delivery(conn, request, now)
    assert finish_delivery(conn, request, claim["attempt_id"], external_id="fixture")
    after_delivery(conn, claim, now)
    queue_due_reminders(conn, now + timedelta(minutes=40))
    assert conn.scalar(select(deliveries.c.chat_key).where(
        deliveries.c.card_id == card, deliveries.c.signal.like("%:sales_escalation"))) == "management"
    reason = "Порча продукта"
    assert submit_sale_explanation(conn, card, "first", 1, reason, "", employee, now,
                                   photo="unavailable") == 2
    assert get_card(conn, card)["data"]["sale_explanation"]["missing_photo"]
    assert ask_sale_question(conn, card, "ask", 2, "Photo?", manager, now) == 3
    try:
        submit_sale_explanation(conn, card, "no-photo", 3, reason, "", employee, now, photo="attached")
    except ProcessConflict:
        pass
    else:
        raise AssertionError("Accepted nonexistent evidence")
    attach_sale_photo(conn, card, "photo", "fixture-photo", employee, now)
    assert submit_sale_explanation(conn, card, "second", 3, reason, "", employee, now,
                                   photo="attached") == 4
    menu = run_bot_command(conn, Message("sales-list", manager, "/tasks"), now)
    button = next(key for label, key in menu.buttons if label.startswith("Принять объяснение"))
    result = run_bot_command(conn, Message("sales-accept", manager, button, callback_id="cb"), now)
    assert "5" in result and get_card(conn, card)["status"] == "closed"
    report = summary(conn, settings["id"], date(2026, 11, 23), date(2026, 11, 24))
    assert report["sales"]["sales.writeoff"]["reasons"][reason] == 1
    assert report["sales"]["sales.writeoff"]["unreviewed"] == 0
    assert report["sources"]["writeoffs"] == "partial"
    iiko_return = event | {"id": "return-2", "type": "sales.return", "data": {"document_id": "doc2"}}
    assert ingest_iiko(conn, iiko_return, now)
    returned = conn.scalars(select(cards.c.id).where(cards.c.enterprise_id == settings["id"],
                            cards.c.kind == "sales.return")).one()
    assert submit_sale_explanation(conn, returned, "answer-return", 1, "Ошибка в заказе", "Wrong order",
                                   employee, now, return_type="money") == 2
    task = assign_sale_task(conn, returned, "create-task", 2, "Check iiko", "Report verified",
                            {"chat": "work", "role": "service_account"}, now + timedelta(hours=3), manager, now)
    assert get_card(conn, task)["parent_id"] == returned
    assert get_card(conn, returned)["status"] == "task"
    try:
        accept_sale_explanation(conn, returned, "early", 3, manager, now)
    except ProcessConflict:
        pass
    else:
        raise AssertionError("Original sales review closed before the task was accepted")
    assert take_request(conn, task, "take", 1, employee, now) == 2
    assert submit_result(conn, task, "result", 2, employee, "Verified", now) == 3
    assert review_result(conn, task, "review", 3, manager, True, "", now) == 4
    items = run_bot_command(conn, Message("sales-task-list", manager, "/tasks"), now)
    close_button = dict(items.buttons)[f"Принять объяснение · {returned[:8]}"]
    closed = run_bot_command(conn, Message("sales-task-finish", manager, close_button, callback_id="cb"), now)
    assert "4" in closed and get_card(conn, returned)["status"] == "closed"
    conn.rollback()
print("iiko sales deduplication, delivered reply SLA, evidence validation linked tasks and review verified")
