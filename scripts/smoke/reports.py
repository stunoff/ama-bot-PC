from datetime import UTC, date, datetime

from sqlalchemy import func, select
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import ingest_iiko, run_bot_command
from ama_rs.messengers import Message
from ama_rs.processes import ProcessPermissionError, cards, claim_delivery, deliveries, finish_delivery
from ama_rs.reports import accessible_summaries, format_summary, import_coverage, import_order, schedule_reports, summary

now = datetime(2026, 11, 24, 9, tzinfo=UTC)
start, end = date(2026, 11, 23), date(2026, 11, 24)
settings = {
    "id": "report-test", "iiko_id": "report-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"], "scheduled_reports": True,
    "chats": {
        "management": {"kind": "management", "id": "42", "platform": "telegram", "calendar": "always"},
        "enterprise": {"kind": "enterprise", "id": "43", "platform": "telegram", "calendar": "always"},
    },
    "grants": [{"chat": "management", "role": "manager", "user": "7"},
               {"chat": "enterprise", "role": "service_account", "user": "8"}],
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
    import_order(conn, settings["id"], data | {"order_id": "44", "state": "completed",
                                             "ready_at": "2026-11-23T14:50+03:00"})
    import_order(conn, settings["id"], data | {"order_id": "cancelled", "cancelled": True,
                                             "handed_at": "2026-11-23T19:00+03:00"})
    import_order(conn, settings["id"], data | {"order_id": "test-order", "test": True,
                                             "handed_at": "2026-11-23T20:00+03:00"})
    import_order(conn, settings["id"], data | {"order_id": "unknown", "fulfillment": "unclear"})
    report = summary(conn, settings["id"], start, end)
    assert report["orders"]["delivery"] == {"measured": 2, "over_target": 1, "unmeasured": 1, "total_seconds": 7201}
    assert report["sources"]["orders"] == "partial" and report["unclassified_orders"] == 1
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
    def confirm(event, local_time, minutes, actor=Actor("telegram", "8", "43")):
        text = ('/workflow operation.confirm_targets {"enterprise_id":"report-test",'
                f'"reason":"Перегрузка кухни","delivery_increment":{minutes}' + "}")
        instant = datetime.fromisoformat(f"2026-11-23T{local_time}+03:00")
        return run_bot_command(conn, Message(event, actor, text), instant)

    assert "недоступно" in confirm("target-denied", "18:50", 30, Actor("telegram", "7", "42"))
    assert "Материал сохранён" in confirm("base-delivery", "19:00", 0)
    assert "Карточка" in confirm("raise-delivery", "20:00", 30)
    assert "Материал сохранён" in confirm("keep-delivery", "21:00", 30)
    assert "Карточка" in confirm("raise-again", "22:00", 60)
    for key, accepted, handed in (("base", "19:30", "20:45"), ("within", "20:00", "21:20"),
                                  ("over", "20:15", "22:00")):
        import_order(conn, settings["id"], data | {"order_id": key,
            "accepted_at": f"2026-11-23T{accepted}+03:00", "handed_at": f"2026-11-23T{handed}+03:00"})
    increased = summary(conn, settings["id"], start, end)
    groups = increased["target_exceedances"]["delivery"]
    assert groups == {"base": 1, "within_increase": 1, "over_increase": 1, "unknown": 2}
    assert sum(groups.values()) == increased["orders"]["delivery"]["over_target"]
    timeline = increased["target_increases"]["delivery"]
    assert timeline["count"] == 2 and len(timeline["intervals"]) == 2
    assert timeline["intervals"][0]["duration_seconds"] == 7200
    previous_increase = summary(conn, settings["id"], end, following_day)["target_increases"]["delivery"]
    assert previous_increase["count"] == 0 and previous_increase["intervals"][0]["started_before_period"]
    conn.rollback()
print("Scoped reports, timestamps, strict thresholds, coverage and schedule deduplication verified")
