from datetime import UTC, datetime
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.processes import ProcessConflict
from ama_rs.refunds import record_refund_result, request_refund, reserve_refund, take_refund

now = datetime(2026, 11, 23, 12, tzinfo=UTC)
kinds = ("enterprise", "management", "refunds")
roles = ("service_account", "manager", "finance_executor")
settings = {
    "id": "refund-test", "iiko_id": "refund-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {kind: {"kind": kind, "id": str(i), "platform": "telegram", "calendar": "always"}
              for i, kind in enumerate(kinds)},
    "grants": [{"chat": kind, "role": role, "user": str(i)}
               for i, (kind, role) in enumerate(zip(kinds, roles))],
}
worker, _, finance = (Actor("telegram", str(i), str(i)) for i in range(3))
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    first = request_refund(conn, settings["id"], "first", "Demo", "Order 1", "Partial", 7000,
                           worker, now, positions=["item"])
    second = request_refund(conn, settings["id"], "second", "Demo", "Order 1", "Partial", 4000,
                            worker, now, positions=["other"])
    assert take_refund(conn, first["id"], "take", 1, finance, now) == 2
    assert reserve_refund(conn, first["id"], "reserve", 2, "payment-1", 10000, 0, finance, now) == 3
    assert record_refund_result(conn, first["id"], "uncertain", 3, "uncertain", "provider timeout", finance, now) == 4
    assert take_refund(conn, second["id"], "take", 1, finance, now) == 2
    try:
        reserve_refund(conn, second["id"], "reserve", 2, "payment-1", 10000, 0, finance, now)
    except ProcessConflict:
        pass
    else:
        raise AssertionError("Payment balance exceeded while first refund was uncertain")
    try:
        record_refund_result(conn, first["id"], "too-much", 4, "executed", "provider-1",
                             finance, now, executed_amount=7001)
    except ProcessConflict:
        pass
    else:
        raise AssertionError("An amount above the reservation was accepted")
    for _ in range(2):
        assert record_refund_result(conn, first["id"], "executed", 4, "executed", "provider-1",
                                    finance, now, executed_amount=5500) == 5
    assert reserve_refund(conn, second["id"], "reserve", 2, "payment-1", 10000, 0, finance, now) == 3
    conn.rollback()
print("Financial assignment, partial balance, actual settlement, and uncertain execution controls verified")
