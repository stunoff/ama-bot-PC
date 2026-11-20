from datetime import UTC, datetime
from ama_rs.contact_center import close_operation, record_order_result, start_operation, take_operation
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, has_role, save_enterprise
from ama_rs.processes import ProcessConflict

now = datetime(2026, 11, 23, 12, tzinfo=UTC)
kinds = ("enterprise", "management", "contact_center")
roles = ("service_account", "manager", "operator")
settings = {
    "id": "contact-test", "iiko_id": "contact-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "central", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {kind: {"kind": kind, "id": str(i), "platform": "telegram", "calendar": "always"}
              for i, kind in enumerate(kinds)},
    "grants": [{"chat": kind, "role": role, "user": str(i)}
               for i, (kind, role) in enumerate(zip(kinds, roles))],
}
worker, manager, operator = (Actor("telegram", str(i), str(i)) for i in range(3))
combined = settings | {"grants": [
    *settings["grants"],
    {"chat": "contact_center", "role": "contact_center_lead", "user": "2"},
]}
assert not has_role(combined, operator, "contact_center", "operator", now)
assert has_role(combined, operator, "contact_center", "contact_center_lead", now, work=False)
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    data = {"reason": "Delay", "orders": [{"platform": "site", "number": "42", "date": "2026-11-23"}]}
    card = start_operation(conn, settings["id"], "order_delay", "delay-1", data, worker, now)
    assert card["assignment"]["chat"] == "contact_center"
    assert take_operation(conn, card["id"], "take", 1, operator, now) == 2
    assert record_order_result(conn, card["id"], "miss", 2, "site:42", "not_reached", operator, now) == 3
    try:
        close_operation(conn, card["id"], "close", 3, "Done", operator, now)
    except ProcessConflict:
        pass
    else:
        raise AssertionError("Unresolved guest contact closed")
    assert record_order_result(conn, card["id"], "reach", 3, "site:42", "warned", operator, now) == 4
    assert close_operation(conn, card["id"], "close", 4, "Done", operator, now) == 5
    one = start_operation(conn, settings["id"], "redelivery", "button1", data, worker, now)
    two = start_operation(conn, settings["id"], "redelivery", "button2", data, worker, now)
    assert one["id"] == two["id"]
    save_enterprise(conn, settings | {"contact_mode": "independent"}, 1, "test")
    independent = start_operation(conn, settings["id"], "stop_list", "stop1", {"items": []}, worker, now)
    assert independent["assignment"]["chat"] == "management"
    assert take_operation(conn, independent["id"], "take", 1, manager, now) == 2
    conn.rollback()
print("Both modes, delay decisions, and redelivery deduplication verified")
