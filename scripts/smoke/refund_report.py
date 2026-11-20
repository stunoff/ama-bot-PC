from datetime import UTC, datetime, timedelta

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message
from ama_rs.refunds import record_refund_result, request_refund, reserve_refund, take_refund

now = datetime(2026, 11, 23, 8, tzinfo=UTC)
employee = Actor("telegram", "1", "work")
executor = Actor("telegram", "2", "finance")
manager = Actor("telegram", "3", "management")
branch = Actor("telegram", "4", "branch")
enterprise_id = "refund-report-test"
settings = {
    "id": enterprise_id, "iiko_id": enterprise_id, "name": "Demo refund report", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["One", "Two"],
    "chats": {
        "enterprise": {"kind": "enterprise", "platform": "telegram", "id": "work", "calendar": "always"},
        "refunds": {"kind": "refunds", "platform": "telegram", "id": "finance", "calendar": "always"},
        "management": {"kind": "management", "platform": "telegram", "id": "management", "calendar": "always"},
        "branch": {"kind": "branch", "platform": "telegram", "id": "branch", "calendar": "always"},
    },
    "grants": [
        {"chat": "enterprise", "role": "service_account", "user": "1"},
        {"chat": "refunds", "role": "finance_executor", "user": "2"},
        {"chat": "management", "role": "manager", "user": "3"},
        {"chat": "branch", "role": "branch_lead", "user": "4"},
    ],
}

with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    refund_ids = []
    for n in (1, 2):
        refund = request_refund(conn, enterprise_id, f"request-{n}", "One", "Order", "Reason", 1200,
                                employee, now, positions=["item"])
        card_id = refund["id"]
        refund_ids.append(card_id)
        later = now + timedelta(minutes=5)
        assert take_refund(conn, card_id, "take", 1, executor, later) == 2
        assert reserve_refund(conn, card_id, "reserve", 2, "payment-1", 10000, 0, executor, later) == 3
        assert record_refund_result(conn, card_id, "done", 3, "executed", "trace", executor,
                                    now + timedelta(minutes=20)) == 4
    request_refund(conn, enterprise_id, "third", "Two", "Order", "Reason", 300, employee, now)

    def show(event, actor, filters=""):
        text = "/refund_report 2026-11-23 2026-11-24" + (f" {filters}" if filters else "")
        return run_bot_command(conn, Message(event, actor, text), now)

    full = show("all", executor)
    assert "Запросов: 3. Полных: 1. Частичных: 2." in full
    assert "Запрошено: 27.00 ₽. Исполнено: 12.00 ₽." in full
    assert "до принятия: 5 мин. До исполнения: 20 мин." in full
    selected = show("brand", branch, '{"brand":"Two"}')
    assert "Запросов: 1" in selected and "Исполнено: 0.00 ₽" in selected
    assert "недоступно" in show("denied", manager)
    expanded = show("details", branch, '{"request":"' + refund_ids[0] + '"}')
    assert f"Карточка: {refund_ids[0]}" in expanded and "Бренд: One" in expanded
    assert "payment-1" not in expanded and "trace" not in expanded
    conn.rollback()
print("Refund report scope, filters, verified totals, duplicate confirmations and detail access verified")
