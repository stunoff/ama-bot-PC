from datetime import UTC, datetime

from sqlalchemy import select
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise, work_deadlines
from ama_rs.processes import ProcessConflict, claim_delivery, deliveries
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
    early = datetime(2026, 11, 23, 5, tzinfo=UTC)
    timed = request_refund(conn, settings["id"], "timed", "Demo", "Timed", "Partial", 100, worker, early)
    notices = dict(conn.execute(select(deliveries.c.signal, deliveries.c.id).where(
        deliveries.c.card_id == timed["id"]
    )).all())
    finance_notice = notices["financial-request"]
    management_notice = notices["management-info"]
    assert claim_delivery(conn, finance_notice, early) is None
    assert claim_delivery(conn, finance_notice, early.replace(minute=59)) is None
    assert claim_delivery(conn, finance_notice, early.replace(hour=20)) is None
    assert claim_delivery(conn, management_notice, early.replace(hour=18)) is None
    assert claim_delivery(conn, finance_notice, early.replace(hour=6)) is not None
    assert claim_delivery(conn, management_notice, early.replace(hour=17, minute=59)) is not None
    closed = settings | {"chats": settings["chats"] | {
        "refunds": settings["chats"]["refunds"] | {"calendar_exceptions": {"2026-11-24": []}}
    }}
    assert list(work_deadlines(closed, "refunds", early.replace(hour=19, minute=55), (10,),
                               refund_close_hour=23)) == [early.replace(day=25, hour=6, minute=5)]
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
