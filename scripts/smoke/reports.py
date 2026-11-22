from datetime import UTC, date, datetime

from sqlalchemy import func, select
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import ingest_iiko
from ama_rs.processes import ProcessPermissionError, cards, claim_delivery, deliveries, finish_delivery
from ama_rs.reports import accessible_summaries, format_summary, import_coverage, import_order, schedule_reports, summary

now = datetime(2026, 11, 24, 9, tzinfo=UTC)
start, end = date(2026, 11, 23), date(2026, 11, 24)
settings = {
    "id": "report-test", "iiko_id": "report-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"], "scheduled_reports": True,
    "chats": {"management": {"kind": "management", "id": "42", "platform": "telegram", "calendar": "always"}},
    "grants": [{"chat": "management", "role": "manager", "user": "7"}],
}
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    no_data = summary(conn, settings["id"], start, end)
    assert no_data["orders"] is None and no_data["sources"]["orders"] == "unavailable"
    import_coverage(conn, settings["id"], {"day": start.isoformat(), "source": "orders",
                                            "state": "complete", "updated_at": now.isoformat()})
    data = {"order_id": "42", "accepted_at": "2026-11-23T14:00+03:00",
            "fulfillment": "delivery", "updated_at": now.isoformat()}
    import_order(conn, settings["id"], data | {"handed_at": "2026-11-23T15:00+03:00"})
    import_order(conn, settings["id"], data | {"handed_at": "2026-11-23T17:00+03:00",
                                               "updated_at": "2026-11-23T18:00+03:00"})
    import_order(conn, settings["id"], data | {"order_id": "43", "handed_at": "2026-11-23T15:00:01+03:00"})
    import_order(conn, settings["id"], data | {"order_id": "44"})
    report = summary(conn, settings["id"], start, end)
    assert report["orders"]["delivery"] == {"measured": 2, "over_target": 1, "unmeasured": 1, "total_seconds": 7201}
    assert report["sources"]["orders"] == "partial"
    assert "предварительные" in format_summary(report)
    assert accessible_summaries(conn, Actor("telegram", "7", "42"), now, start, end) == [report]
    try:
        accessible_summaries(conn, Actor("telegram", "8", "42"), now, start, end)
    except ProcessPermissionError:
        pass
    else:
        raise AssertionError("Report exposed to another user")
    following_day = date(2026, 11, 25)
    for kind, endpoint in (("delivery", "handed_at"), ("pickup", "ready_at"),
                           ("takeaway", "issued_at"), ("dine_in", "issued_at")):
        order = data | {"order_id": kind, "fulfillment": kind, "accepted_at": "2026-11-23T23:30+03:00"}
        import_order(conn, settings["id"], order | {endpoint: "2026-11-24T00:45+03:00"})
        import_order(conn, settings["id"], order | {endpoint: "2026-11-23T23:50+03:00",
                                                     "updated_at": "2026-11-24T01:00+03:00"})
    prior_day = summary(conn, settings["id"], start, end)
    assert prior_day["orders"] == report["orders"]
    assert prior_day["unfinished_orders"] == dict.fromkeys(report["orders"], 1)
    next_report = summary(conn, settings["id"], end, following_day)
    for metric in next_report["orders"].values():
        assert metric == {"measured": 1, "over_target": 1, "unmeasured": 0, "total_seconds": 4500}
    import_order(conn, settings["id"], data | {"order_id": "near-midnight",
                 "accepted_at": "2026-11-24T23:29:59.499999+03:00", "handed_at": "2026-11-24T23:59:59.999999+03:00"})
    metric = summary(conn, settings["id"], end, following_day)["orders"]["delivery"]
    assert metric["measured"] == 2 and metric["total_seconds"] == 6300.5
    preorder = data | {"order_id": "preorder", "preorder": True, "promised_at": "2026-11-24T00:30+03:00",
                       "accepted_at": "2026-11-23T23:00+03:00"}
    import_order(conn, settings["id"], preorder)
    import_order(conn, settings["id"], preorder | {
        "handed_at": "2026-11-24T00:45+03:00", "promised_at": "2026-11-24T01:00+03:00",
        "updated_at": "2026-11-24T21:00+03:00",
    })
    import_order(conn, settings["id"], data | {"order_id": "unfinished", "state": "open"})
    first_day = summary(conn, settings["id"], start, end)
    assert first_day["unfinished_orders"]["delivery"] == 2
    second_day = summary(conn, settings["id"], end, following_day)
    assert second_day["unfinished_orders"]["delivery"] == 1
    assert second_day["preorders"]["delivery"] == {"completed": 1, "late": 1, "late_seconds": 900, "unmeasured": 0}
    assert second_day["orders"]["delivery"] == metric
    for _ in range(2):
        schedule_reports(conn, now)
    assert conn.scalar(select(func.count()).select_from(deliveries.join(cards)).where(
        cards.c.enterprise_id == settings["id"], deliveries.c.signal == "report")) == 1
    original = conn.scalar(select(deliveries.c.id).select_from(deliveries.join(cards)).where(
        cards.c.enterprise_id == settings["id"], deliveries.c.signal == "report"))
    attempt = claim_delivery(conn, original, now)
    assert finish_delivery(conn, original, attempt["attempt_id"], external_id="42")
    correction = {"id": "order-correction", "enterprise_id": settings["iiko_id"], "type": "orders.updated",
                  "data": data | {"order_id": "44", "handed_at": "2026-11-23T15:30+03:00",
                                  "updated_at": "2026-11-24T18:00+03:00"}}
    assert ingest_iiko(conn, correction, now)
    assert not ingest_iiko(conn, correction, now)
    assert ingest_iiko(conn, correction | {"id": "same-data"}, now)
    edit = conn.execute(select(deliveries).where(
        deliveries.c.card_id == attempt["card_id"], deliveries.c.signal.startswith("report-edit:")
    )).mappings().one()
    claimed_edit = claim_delivery(conn, edit["id"], now)
    assert claimed_edit["payload"]["edit"] == "42"
    assert claimed_edit["payload"]["expected_chat"]["id"] == "42"
    conn.rollback()
print("Scoped reports, timestamps, strict thresholds, coverage and schedule deduplication verified")
