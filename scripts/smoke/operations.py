from datetime import UTC, datetime, timedelta
from sqlalchemy import select
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.operations import (
    AUDIT_SECTIONS, begin_audit, finish_regular_work, finish_zones, plan_regular_work, plan_zones,
    record_audit_section, review_audit, review_zone, submit_audit, submit_zone,
)
from ama_rs.processes import ProcessConflict, cards, get_card

now = datetime(2026, 11, 23, 18, tzinfo=UTC)
kinds = ("enterprise", "management", "branch", "maintenance")
roles = ("service_account", "manager", "branch_lead", "maintenance_manager")
settings = {
    "id": "operations-test", "iiko_id": "operations-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {kind: {"kind": kind, "id": str(i), "platform": "telegram", "calendar": "always"}
              for i, kind in enumerate(kinds)},
    "grants": [{"chat": kind, "role": role, "user": str(i)}
               for i, (kind, role) in enumerate(zip(kinds, roles))],
}
worker, manager, branch, maintenance = (Actor("telegram", str(i), str(i)) for i in range(4))
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    audit = begin_audit(conn, settings["id"], "audit", "manager", manager, now)
    for idx, section in enumerate(AUDIT_SECTIONS, 1):
        assert record_audit_section(conn, audit["id"], f"section:{idx}", idx, section,
                                   {"outcome": "ok"}, manager, now) == idx + 1
    revision = submit_audit(conn, audit["id"], "audit-submit", len(AUDIT_SECTIONS) + 1, manager, now)
    assert review_audit(conn, audit["id"], "audit-review", revision, True, "", branch, now) == revision + 1
    plan = plan_zones(conn, settings["id"], "inventory", "plan", {
        "stock": {"chat": "enterprise", "role": "service_account", "user": "0"},
    }, now + timedelta(hours=1), manager, now)
    zone = conn.scalar(select(cards.c.id).where(cards.c.parent_id == plan["id"]))
    try:
        finish_zones(conn, plan["id"], "plan-finish", 1, manager, now, posted_document="A")
    except ProcessConflict:
        pass
    else:
        raise AssertionError("Inventory finished before zone acceptance")
    assert submit_zone(conn, zone, "zone-submit", 1, "iiko", worker, now, reference="doc") == 2
    assert review_zone(conn, zone, "zone-review", 2, True, manager, now) == 3
    assert finish_zones(conn, plan["id"], "plan-finish", 1, manager, now, posted_document="A") == 2
    service = plan_regular_work(conn, settings["id"], "preventive_service", "service", "Inspect",
                                now + timedelta(hours=1), manager, now, equipment_id="oven-1")
    assert finish_regular_work(conn, service["id"], "completion", 1, "Complete", maintenance, now)
    assert get_card(conn, service["id"])["status"] == "closed"
    conn.rollback()
print("Audit review, inventory zone acceptance and equipment maintenance verified")
