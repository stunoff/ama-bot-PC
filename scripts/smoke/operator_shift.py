from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message
from ama_rs.operator_shift import (
    CLOSING, PREPARATION, add_shift_photo, move_shift, plan_shift, record_shift_check, shift_checks,
)
from ama_rs.processes import ProcessConflict, deliveries, get_card

now = datetime(2026, 11, 23, 8, tzinfo=UTC)
manager = Actor("telegram", "2", "manager")
operator = Actor("telegram", "1", "work")
settings = {
    "id": "operator-shift-test", "iiko_id": "operator-shift-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "aggregator_accounts": ["Яндекс Еда", "Купер"],
    "contact_mode": "central", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {
        "management": {"kind": "contact_management", "platform": "telegram", "id": "manager", "calendar": "always"},
        "work": {"kind": "contact_center", "platform": "telegram", "id": "work", "calendar": "always"},
    },
    "grants": [{"chat": "management", "role": "contact_center_lead", "user": "2"},
               {"chat": "work", "role": "operator", "user": "1"}],
}
with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    planned = now + timedelta(minutes=10)
    end = planned + timedelta(hours=8)
    shift = plan_shift(conn, settings["id"], operator.user_id, planned, end, manager, now)
    assert plan_shift(conn, settings["id"], operator.user_id, planned, end, manager, now) == shift
    notices = conn.execute(select(deliveries.c.signal, deliveries.c.available_at)
                           .where(deliveries.c.card_id == shift)).all()
    assert dict(notices)["shift-preparation"] == now.replace(tzinfo=None)
    assert dict(notices)["shift-summary:management"] == (end - timedelta(minutes=30)).replace(tzinfo=None)
    menu = run_bot_command(conn, Message("shift-ui-open", operator, f"/operator_shift {shift}"), now)
    button = dict(menu.buttons)["Открыть смену"]
    opened = run_bot_command(conn, Message("shift-ui-open-button", operator, button, callback_id="1"), now)
    assert "Компьютер" in opened.text
    assert record_shift_check(conn, shift, "issue", 2, "equipment", operator, now, problem="No audio") == 3
    try:
        record_shift_check(conn, shift, "no-photo", 3, "internet", operator, now)
    except ProcessConflict:
        pass
    else:
        raise AssertionError("Completed a required photo check without evidence")
    actual_start = planned + timedelta(minutes=2)
    revision = 3
    for item, (_, needs_photo) in PREPARATION.items():
        if needs_photo:
            add_shift_photo(conn, shift, f"photo:{item}", item, "test-photo", operator, actual_start)
        revision = record_shift_check(conn, shift, f"done:{item}", revision, item, operator, actual_start)
    current = get_card(conn, shift)
    assert current["status"] == "serving" and current["data"]["late_seconds"] == 120
    assert current["data"]["started_at"] == actual_start.isoformat()
    revision = move_shift(conn, shift, "continue", revision, "continue", operator, actual_start,
                          late_reason="Headset replaced")
    checks = shift_checks(get_card(conn, shift))
    assert list(checks)[1:3] == ["aggregator_1", "aggregator_2"]
    assert checks["aggregator_2"] == ("Кабинет агрегатора · Купер", True)
    revised_settings = settings | {"aggregator_accounts": []}
    save_enterprise(conn, revised_settings, 1, "test")
    assert shift_checks(get_card(conn, shift)) == checks
    for item, (_, needs_photo) in checks.items():
        if needs_photo:
            add_shift_photo(conn, shift, f"photo:{item}", item, "test-photo", operator, actual_start)
        revision = record_shift_check(conn, shift, f"done:{item}", revision, item, operator, actual_start)
    revision = move_shift(conn, shift, "close", revision, "close", operator, end)
    for item in CLOSING:
        revision = record_shift_check(conn, shift, f"done:{item}", revision, item, operator, end)
    move_shift(conn, shift, "confirm", revision, "confirm", operator, end)
    current = get_card(conn, shift)
    assert current["data"]["worked_seconds"] == 478 * 60 and current["status"] == "closed"
    conn.rollback()
print("Operator shift planning, photo checks, actual start, lateness, summaries and closing verified")
