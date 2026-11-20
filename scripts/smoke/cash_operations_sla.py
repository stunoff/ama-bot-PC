from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.cash_discrepancy import explain_discrepancy, refer_discrepancy
from ama_rs.contact_center import close_operation, mark_order_ready, record_order_result, start_operation, take_operation
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message
from ama_rs.processes import cards, claim_delivery, create_card, deliveries, finish_delivery, get_card
from ama_rs.reminders import after_delivery, queue_due_reminders
from ama_rs.shifts import confirm_iiko_closure

now = datetime(2026, 11, 20, 9, tzinfo=UTC)
kinds = ("enterprise", "cash", "management", "branch", "contact_center", "contact_management")
roles = ("service_account", "cash_reviewer", "manager", "branch_lead", "operator", "contact_center_lead")
settings = {
    "id": "cash-ops-sla", "iiko_id": "cash-ops-sla", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "central", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {kind: {"kind": kind, "id": str(i), "platform": "telegram", "calendar": "always"}
              for i, kind in enumerate(kinds)},
    "grants": [{"chat": kind, "role": role, "user": str(i)}
               for i, (kind, role) in enumerate(zip(kinds, roles))],
}
settings["chats"]["cash"].update(cash_accept_by="14:00", calendar=[
    {"weekday": day, "start": "09:00", "end": "19:00"} for day in range(5)
])
employee, reviewer, manager, _, operator, _ = (Actor("telegram", str(i), str(i)) for i in range(6))


def confirm(connection, card_id, signal, instant):
    row = connection.scalar(select(deliveries.c.id).where(
        deliveries.c.card_id == card_id, deliveries.c.signal == signal))
    claimed = claim_delivery(connection, row, instant)
    assert claimed is not None
    assert finish_delivery(connection, row, claimed["attempt_id"], external_id="test")
    after_delivery(connection, claimed, instant)


with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    cash = create_card(conn, settings["id"], "cash_shift", "shift-1",
        {"chat": "enterprise", "role": "service_account"}, {"cash_chat": "cash"}, now,
        status="awaiting_iiko")
    cash_id = cash["id"]
    assert confirm_iiko_closure(conn, settings["id"], "shift-1", "closed-iiko", now)
    confirm(conn, cash_id, "review", now)
    discrepancy = refer_discrepancy(conn, cash_id, "refer", 2, 9000, "Check the receipt", reviewer, now)
    assert get_card(conn, cash_id)["status"] == "discrepancy"
    confirm(conn, discrepancy, "cash-discrepancy", now)
    queue_due_reminders(conn, now + timedelta(minutes=45))
    assert conn.scalar(select(deliveries.c.chat_key).where(
        deliveries.c.card_id == discrepancy,
        deliveries.c.signal.like("%:cash_discrepancy_escalation"))) == "branch"
    assert explain_discrepancy(conn, discrepancy, "answer", 1, "Explained", manager, now) == 2
    assert get_card(conn, cash_id)["status"] == "review"

    monday_due = datetime(2026, 11, 23, 11, tzinfo=UTC)
    filters = (deliveries.c.card_id == cash_id, deliveries.c.signal.like("%:cash_review_due"))
    queue_due_reminders(conn, monday_due - timedelta(seconds=1))
    assert conn.scalar(select(deliveries.c.id).where(*filters)) is None
    queue_due_reminders(conn, monday_due)
    assert conn.scalar(select(deliveries.c.chat_key).where(*filters)) == "cash"
    queue_due_reminders(conn, monday_due + timedelta(hours=2))
    assert conn.scalar(select(deliveries.c.chat_key).where(
        deliveries.c.card_id == cash_id,
        deliveries.c.signal.like("%:cash_review_escalation"))) == "branch"
    denied = run_bot_command(conn, Message("cash-menu-denied", employee, f"/cash_menu {cash_id}"), monday_due)
    assert "недоступно" in denied
    menu = run_bot_command(conn, Message("cash-menu", reviewer, f"/cash_menu {cash_id}"), monday_due)
    explained = dict(menu.buttons)["Принято · расхождение объяснено"]
    result = run_bot_command(conn, Message("cash-menu-accept", reviewer, explained, callback_id="cb"), monday_due)
    assert "5" in result
    assert get_card(conn, cash_id)["data"]["outcome"] == "explained"

    operation = start_operation(conn, settings["id"], "delivery", "delivery-1",
                             {"enabled": True, "pickup_minutes": 25, "delivery_minutes": 55}, employee, now)
    operation_id = operation["id"]
    confirm(conn, operation_id, "operation", now)
    later = now + timedelta(minutes=10)
    queue_due_reminders(conn, later)
    control = select(cards.c.id).where(cards.c.parent_id == operation_id, cards.c.kind == "request_control")
    assert conn.scalar(control) is None
    confirm(conn, operation_id, "timer:1:operation_take_escalation", later)
    queue_due_reminders(conn, later + timedelta(minutes=5))
    control_id = conn.scalars(control).one()
    assert get_card(conn, control_id)["assignment"]["chat"] == "contact_management"
    assert take_operation(conn, operation_id, "take", 1, operator, later) == 2
    assert close_operation(conn, operation_id, "finish", 2, "Updated", operator, later) == 3
    assert get_card(conn, control_id)["status"] == "closed"
    mass = start_operation(conn, settings["id"], "order_delay", "delay-1",
        {"reason": "Preparation", "orders": [{"platform": "site", "number": "1"},
                                            {"platform": "site", "number": "2"}]}, employee, now)
    mass_id = mass["id"]
    assert take_operation(conn, mass_id, "take", 1, operator, now) == 2
    assert record_order_result(conn, mass_id, "miss-first", 2, "site:1", "not_reached", operator, now) == 3
    assert record_order_result(conn, mass_id, "miss-second", 3, "site:2", "not_reached", operator, now) == 4
    after_five = now + timedelta(minutes=5)
    after_ten = now + timedelta(minutes=10)
    queue_due_reminders(conn, after_five)
    guest_filter = (deliveries.c.card_id == mass_id, deliveries.c.signal.startswith("guest-contact:"))
    first_notice = conn.scalars(select(deliveries.c.payload).where(*guest_filter)).one()
    assert first_notice["guest_orders"] == ["site:1", "site:2"]
    assert record_order_result(conn, mass_id, "miss-again", 4, "site:2", "not_reached", operator, after_five) == 5
    assert record_order_result(conn, mass_id, "warned", 5, "site:1", "warned", operator, after_five) == 6
    queue_due_reminders(conn, after_ten)
    pending = conn.scalars(select(deliveries.c.payload).where(*guest_filter, deliveries.c.state == "pending")).one()
    assert pending["guest_orders"] == ["site:2"]
    queue_due_reminders(conn, now + timedelta(hours=1))
    assert len(conn.execute(select(deliveries.c.id).where(*guest_filter)).all()) == 2
    assert conn.scalar(select(cards.c.id).where(cards.c.parent_id == mass_id)) is None
    first = start_operation(conn, settings["id"], "order_delay", "delay-first-call",
        {"reason": "Preparation", "orders": [{"platform": "site", "number": str(n)} for n in (3, 4, 5)],
         "waiting_for_details": ["site:5"]}, employee, now)
    first_id = first["id"]
    assert take_operation(conn, first_id, "take", 1, operator, now) == 2
    assert record_order_result(conn, first_id, "warned", 2, "site:3", "warned", operator, now) == 3
    queue_due_reminders(conn, after_five)
    filter_calls = (deliveries.c.card_id == first_id, deliveries.c.signal.startswith("first-call:"))
    call = conn.execute(select(deliveries.c.signal, deliveries.c.payload)
                        .where(*filter_calls, deliveries.c.state == "pending")).one()
    assert call.payload["guest_orders"] == ["site:4"]
    confirm(conn, first_id, call.signal, after_five)
    assert mark_order_ready(conn, first_id, "ready", 3, "site:5", operator, after_five) == 4
    assert take_operation(conn, first_id, "take", 1, operator, now) == 2
    queue_due_reminders(conn, after_ten)
    call = conn.execute(select(deliveries.c.payload)
                        .where(*filter_calls, deliveries.c.state == "pending")).scalar_one()
    assert call["guest_orders"] == ["site:4", "site:5"]
    queue_due_reminders(conn, now + timedelta(minutes=15))
    related = select(cards.c.id).where(cards.c.parent_id == first_id, cards.c.kind == "request_control")
    assert conn.scalars(related).one()
    after_escalation = conn.execute(select(deliveries.c.payload)
                        .where(*filter_calls, deliveries.c.state == "pending")).scalar_one()
    assert after_escalation["guest_orders"] == ["site:5"]
    queue_due_reminders(conn, now + timedelta(hours=1))
    assert conn.scalars(related).one()
    conn.rollback()
print("Cash deadlines, first-delivery escalation and grouped guest-contact reminders verified")
