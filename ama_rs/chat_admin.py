"""Enterprise configuration in the explicitly registered Telegram General topic."""

import json
from copy import deepcopy
from datetime import datetime

from sqlalchemy.engine import Connection

from ama_rs.chat_menu import admin_scope, screen
from ama_rs.enterprises import Actor, save_enterprise, validate_settings
from ama_rs.processes import ProcessPermissionError
from ama_rs.timing import DEFAULTS, effective

EDITABLE = {"name", "timezone", "format", "shift_schedule", "facilities", "contact_mode",
            "final_route", "scheduled_reports", "timing_overrides"}


def _entry(connection: Connection, actor: Actor, now: datetime, enterprise_id: str):
    entry = next((entry for entry in admin_scope(connection, actor, now)
                  if entry["id"] == enterprise_id), None)
    if entry is None:
        raise ProcessPermissionError("Enterprise configuration is outside this administrator's scope")
    return entry


def _summary(entry) -> str:
    data = entry["settings"]
    missing = [key for key, chat in data["chats"].items() if chat.get("calendar") is None]
    schedule = data.get("shift_schedule", {})
    return (f"{data['name']} · {entry['id']} · версия {entry['revision']}\n"
            f"Часовой пояс: {data['timezone']}\nФормат: {data['format']}\n"
            f"Обслуживание: {schedule.get('service_starts_at', '—')}–{schedule.get('service_ends_at', '—')}\n"
            f"Режим КЦ: {data['contact_mode']}\nКонечный маршрут: {data['final_route']}\n"
            f"Календари требуют настройки: {', '.join(missing) or '—'}\n"
            "Тайминги (минуты):\n" + "\n".join(
                f"{key}: {', '.join(map(str, effective(data, key)))}"
                f" ({'предприятие' if key in data.get('timing_overrides', {}) else 'системные'})"
                for key in DEFAULTS
            ))


def settings_screen(connection: Connection, actor: Actor, now: datetime, enterprise_id: str = ""):
    if enterprise_id:
        entry = _entry(connection, actor, now, enterprise_id)
        return screen(connection, actor, now, _summary(entry), [
            ("Как изменить", {"text": f"/settings_help {enterprise_id}"}),
            ("Назад", {"text": "/settings"}),
        ])
    allowed = admin_scope(connection, actor, now)
    if not allowed:
        raise ProcessPermissionError("Administration requires the assigned General context")
    return screen(connection, actor, now, "Администрирование · выберите предприятие", [
        (entry["settings"]["name"][:60], {"text": f"/settings {entry['id']}"}) for entry in allowed[:20]
    ])


def preview(connection: Connection, actor: Actor, now: datetime, arguments: str):
    enterprise_id, field, raw = arguments.split(maxsplit=2)
    entry = _entry(connection, actor, now, enterprise_id)
    if field not in EDITABLE:
        raise ValueError("Field is unavailable from chat")
    value = json.loads(raw)
    _change(entry["settings"], field, value)
    before = json.dumps(entry["settings"].get(field), ensure_ascii=False)
    after = json.dumps(value, ensure_ascii=False)
    if max(len(before), len(after)) > 500:
        raise ValueError("Preview is too long")
    return screen(connection, actor, now,
        f"{entry['settings']['name']} · {field}\nБыло: {before}\nБудет: {after}\n"
        f"Версия {entry['revision']} · применяется после сохранения",
        [("Сохранить", {"admin_save": {"id": enterprise_id, "revision": entry["revision"],
                                       "field": field, "value": value}}),
         ("Отмена", {"text": f"/settings {enterprise_id}"})])


def _change(settings: dict, field: str, value) -> dict:
    expected = {"scheduled_reports": bool, "facilities": list, "shift_schedule": dict,
                "timing_overrides": dict}.get(field, str)
    if type(value) is not expected:
        raise ValueError("Invalid setting value")
    if field == "facilities" and (any(type(item) is not str for item in value)
            or set(value) - {"guest_entrance", "guest_hall", "reception", "guest_restroom", "staff_restroom"}):
        raise ValueError("Unknown facility")
    result = deepcopy(settings)
    result[field] = value
    validate_settings(result)
    return result


def save(connection: Connection, actor: Actor, now: datetime, command: dict) -> str:
    entry = _entry(connection, actor, now, command["id"])
    field = command["field"]
    if field not in EDITABLE:
        raise ValueError("Field is unavailable from chat")
    revised = _change(entry["settings"], field, command["value"])
    revision = save_enterprise(connection, revised, command["revision"], f"{actor.platform}:{actor.user_id}")
    return f"Настройки сохранены. Версия {revision}."
