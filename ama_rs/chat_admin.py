"""Enterprise configuration in the explicitly registered Telegram General topic."""

import json
from copy import deepcopy
from datetime import datetime

from sqlalchemy.engine import Connection

from ama_rs.admin_permissions import ADMIN_ROLES
from ama_rs.chat_menu import admin_scope, configuration_fields, screen
from ama_rs.enterprises import Actor, has_role, save_enterprise, validate_settings
from ama_rs.processes import ProcessPermissionError
from ama_rs.shifts import route
from ama_rs.timing import DEFAULTS, effective

def _entry(connection: Connection, actor: Actor, now: datetime, enterprise_id: str):
    entry = next((entry for entry in admin_scope(connection, actor, now)
                  if entry["id"] == enterprise_id), None)
    if entry is None:
        raise ProcessPermissionError("Enterprise configuration is outside this administrator's scope")
    return entry


def help_text(connection: Connection, actor: Actor, now: datetime, enterprise_id: str) -> str:
    entry = _entry(connection, actor, now, enterprise_id)
    fields = configuration_fields(entry["settings"], actor, now)
    return f"/configure {enterprise_id} <поле> <JSON>\nПоля: " + ", ".join(sorted(fields))


def _summary(entry, fields: frozenset[str]) -> str:
    data = entry["settings"]
    if "admin_access" not in fields:
        values = {}
        for field in sorted(fields):
            if field == "chat_calendar":
                values[field] = {key: chat.get("calendar") for key, chat in data["chats"].items()}
            elif field == "timing_overrides":
                values[field] = {key: effective(data, key) for key in DEFAULTS}
            else:
                values[field] = data.get(field)
        return (f"{entry['id']} · версия {entry['revision']}\n" +
                json.dumps(values, ensure_ascii=False, indent=2))[:3800]
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
        fields = configuration_fields(entry["settings"], actor, now)
        return screen(connection, actor, now, _summary(entry, fields), [
            ("Как изменить", {"text": f"/settings_help {enterprise_id}"}),
            ("Назад", {"text": "/settings"}),
        ])
    allowed = admin_scope(connection, actor, now)
    if not allowed:
        raise ProcessPermissionError("Administration requires the assigned General context")
    return screen(connection, actor, now, "Администрирование · выберите предприятие", [
        ((entry["settings"]["name"] if "name" in configuration_fields(
            entry["settings"], actor, now
        ) else entry["id"])[:60], {"text": f"/settings {entry['id']}"}) for entry in allowed[:20]
    ])


def preview(connection: Connection, actor: Actor, now: datetime, arguments: str):
    enterprise_id, field, raw = arguments.split(maxsplit=2)
    entry = _entry(connection, actor, now, enterprise_id)
    if field not in configuration_fields(entry["settings"], actor, now):
        raise ProcessPermissionError("Field is outside this administrator's scope")
    value = json.loads(raw)
    _change(entry["settings"], field, value, now)
    previous = entry["settings"].get(field)
    if field == "admin_access":
        previous = _matching_grants(entry["settings"], value)
    if field == "chat_calendar":
        previous = entry["settings"]["chats"][value["chat"]].get("calendar")
    before = json.dumps(previous, ensure_ascii=False)
    after = json.dumps(value, ensure_ascii=False)
    if max(len(before), len(after)) > 500:
        raise ValueError("Preview is too long")
    return screen(connection, actor, now,
        f"{enterprise_id} · {field}\nБыло: {before}\nБудет: {after}\n"
        f"Версия {entry['revision']} · применяется после сохранения",
        [("Сохранить", {"admin_save": {"id": enterprise_id, "revision": entry["revision"],
                                       "field": field, "value": value}}),
         ("Отмена", {"text": f"/settings {enterprise_id}"})])


def _matching_grants(settings: dict, rule: dict) -> list[dict]:
    return [grant for grant in settings["grants"] if grant["role"] in ADMIN_ROLES
            and grant["chat"] == rule["chat"] and grant["user"] == rule["user"]]


def _change_access(settings: dict, rule: dict, now: datetime) -> None:
    if set(rule) != {"chat", "user", "access"} or not isinstance(rule["user"], str):
        raise ValueError("Expected chat, Telegram user ID and access")
    if not rule["user"].isascii() or not rule["user"].isdecimal() or not 1 <= len(rule["user"]) <= 20:
        raise ValueError("Expected a Telegram user ID")
    target = settings["chats"].get(rule["chat"], {})
    if target.get("platform") != "telegram" or "general_topic_id" not in target:
        raise ValueError("Choose a registered admin forum")
    matching = _matching_grants(settings, rule)
    settings["grants"] = [grant for grant in settings["grants"] if grant not in matching]
    if rule["access"] is not None:
        role = "system_admin" if rule["access"] == "full" else "delegated_admin"
        grant = {"chat": rule["chat"], "user": rule["user"], "role": role}
        if role == "delegated_admin":
            grant["fields"] = rule["access"]
        settings["grants"].append(grant)
    for grant in settings["grants"]:
        if grant["role"] != "system_admin" or "ends_at" in grant:
            continue
        chat = settings["chats"][grant["chat"]]
        actor = Actor(chat["platform"], grant["user"], chat["id"], chat.get("general_topic_id"))
        if has_role(settings, actor, grant["chat"], "system_admin", now):
            return
    raise ProcessPermissionError("At least one permanent active administrator is required")


def _change(settings: dict, field: str, value, now: datetime) -> dict:
    expected = {"scheduled_reports": bool, "scheduled_checklists": bool,
                "facilities": list, "writeoff_reasons": list, "shift_schedule": dict, "timing_overrides": dict,
                "chat_calendar": dict, "admin_access": dict}.get(field, str)
    if type(value) is not expected:
        raise ValueError("Invalid setting value")
    if field == "facilities" and (any(type(item) is not str for item in value)
            or set(value) - {"guest_entrance", "guest_hall", "reception", "guest_restroom", "staff_restroom"}):
        raise ValueError("Unknown facility")
    if field == "shift_schedule" and set(value) != {
        "service_starts_at", "service_ends_at", "preparation_minutes", "closing_minutes"
    }:
        raise ValueError("Unexpected shift schedule fields")
    result = deepcopy(settings)
    if field == "chat_calendar":
        if set(value) != {"chat", "calendar"} or value["chat"] not in result["chats"]:
            raise ValueError("Specify the existing chat and its calendar")
        calendar = value["calendar"]
        if isinstance(calendar, list) and any(
            not isinstance(window, dict) or set(window) != {"weekday", "start", "end"}
            for window in calendar
        ):
            raise ValueError("Unexpected calendar fields")
        result["chats"][value["chat"]]["calendar"] = calendar
    elif field == "admin_access":
        _change_access(result, value, now)
    else:
        result[field] = value
    validate_settings(result)
    if result.get("scheduled_checklists"):
        for kind in ("enterprise", "management"):
            key = route(result, kind)
            if not result["chats"][key].get("calendar"):
                raise ValueError(f"Calendar is required for {kind}")
    return result


def save(connection: Connection, actor: Actor, now: datetime, command: dict) -> str:
    entry = _entry(connection, actor, now, command["id"])
    field = command["field"]
    if field not in configuration_fields(entry["settings"], actor, now):
        raise ProcessPermissionError("Field is outside this administrator's scope")
    revised = _change(entry["settings"], field, command["value"], now)
    revision = save_enterprise(connection, revised, command["revision"], f"{actor.platform}:{actor.user_id}")
    return f"Настройки сохранены. Версия {revision}."
