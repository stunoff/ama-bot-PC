from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from ama_rs.chat_menu import Screen
from ama_rs.db import create_database_engine
from ama_rs.enterprises import Actor, get_enterprise, save_enterprise
from ama_rs.integrations import run_bot_command
from ama_rs.messengers import Message, normalize, send_message

now = datetime(2026, 11, 23, 17, tzinfo=UTC)
admin = Actor("telegram", "8", "-1001", "76")
outsider = Actor("telegram", "9", "-1001", "76")
foreign_topic = Actor("telegram", "8", "-1001", "1")
settings = {
    "id": "menu-test", "iiko_id": "menu-iiko", "name": "Test", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Test"],
    "chats": {"technical": {"platform": "telegram", "kind": "technical", "id": "-1001",
                            "general_topic_id": "76", "calendar": "always"}},
    "grants": [{"chat": "technical", "role": "system_admin", "user": "8"}],
}

with create_database_engine().begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    home = run_bot_command(conn, Message("m1", admin, "/menu"), now)
    assert "Администрирование" in dict(home.buttons)
    assert isinstance(run_bot_command(conn, Message("m2", foreign_topic, "/settings"), now), str)
    admin_button = dict(home.buttons)["Администрирование"]
    event = {"update_id": 90, "callback_query": {
        "id": "callback90", "from": {"id": 8}, "data": admin_button,
        "message": {"chat": {"id": -1001}, "message_thread_id": 76},
    }}
    assert normalize("telegram", event).actor == admin
    assert "выберите" in run_bot_command(conn, normalize("telegram", event), now).text

    cmd = '/configure menu-test timing_overrides {"technical.high":[8,16,28]}'
    confirmation = run_bot_command(conn, Message("m3", admin, cmd), now)
    assert isinstance(confirmation, Screen)
    save_button = dict(confirmation.buttons)["Сохранить"]
    assert "недоступно" in run_bot_command(conn, Message("m4", outsider, save_button, callback_id="cb4"), now)
    old_preview = run_bot_command(conn, Message("m5", admin, cmd), now)
    assert "Версия 2" in run_bot_command(conn, Message("m6", admin, save_button, callback_id="cb6"), now)
    assert get_enterprise(conn, "menu-test")["settings"]["timing_overrides"] == {"technical.high": [8, 16, 28]}
    stale = dict(old_preview.buttons)["Сохранить"]
    assert "недоступно" in run_bot_command(conn, Message("m7", admin, stale, callback_id="cb7"), now)
    expired = run_bot_command(conn, Message("m8", admin, cmd), now)
    assert "недоступно" in run_bot_command(
        conn, Message("m9", admin, dict(expired.buttons)["Сохранить"], callback_id="cb9"),
        now + timedelta(minutes=31),
    )
    conn.rollback()

with patch("ama_rs.messengers._post", return_value={"message": {"body": {"mid": "1"}}}) as post:
    send_message("max", "test-token", {"id": "1", "kind": "group"}, "Menu", buttons=(("Open", "abc"),))
    payload = post.call_args.args[1]["attachments"][0]["payload"]
    assert payload["buttons"][0][0] == {"type": "callback", "text": "Open", "payload": "abc"}
print("Menu transport, General role, foreign/expired buttons, and conflicting admin saves verified")
