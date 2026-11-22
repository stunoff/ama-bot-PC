"""Enterprise configuration in the explicitly registered Telegram General topic."""

import json
from copy import deepcopy
from datetime import datetime

from sqlalchemy.engine import Connection

from ama_rs.admin_permissions import ADMIN_ROLES
from ama_rs.chat_menu import admin_scope, configuration_fields, screen
from ama_rs.enterprises import Actor, has_role, save_enterprise, validate_settings
from ama_rs.enterprise_wizard import creator_context
from ama_rs.checklist_setup import REQUIRED, route_issues, schedule_issues
from ama_rs.route_probe import route_tests
from ama_rs.readiness import readiness_report
from ama_rs.processes import ProcessPermissionError
from ama_rs.shifts import route
from ama_rs.scenario_activation import SCENARIOS, scenario_issues
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
        links = [("Как изменить", {"text": f"/settings_help {enterprise_id}"}),
                 ("Назад", {"text": "/settings"})]
        if "admin_access" in fields:
            links.insert(0, ("Мастер чек-листов", {"text": f"/checklist_setup {enterprise_id}"}))
            links.insert(0, ("Готовность к запуску", {"text": f"/readiness {enterprise_id}"}))
        if fields.intersection(SCENARIOS):
            links.insert(0, ("Включение сценариев", {"text": f"/scenarios {enterprise_id}"}))
        return screen(connection, actor, now, _summary(entry, fields), links)
    allowed = admin_scope(connection, actor, now)
    if not allowed:
        raise ProcessPermissionError("Administration requires the assigned General context")
    links = [
        ((entry["settings"]["name"] if "name" in configuration_fields(
            entry["settings"], actor, now
        ) else entry["id"])[:60], {"text": f"/settings {entry['id']}"}) for entry in allowed[:20]
    ]
    if creator_context(connection, actor, now) is not None:
        links.insert(0, ("Новое предприятие", {"text": "/enterprise_new"}))
    return screen(connection, actor, now, "Администрирование · выберите предприятие", links)


def readiness_screen(connection: Connection, actor: Actor, now: datetime, enterprise_id: str):
    entry = _entry(connection, actor, now, enterprise_id)
    if "admin_access" not in configuration_fields(entry["settings"], actor, now):
        raise ProcessPermissionError("Only a full administrator can inspect enterprise-wide readiness")
    return screen(connection, actor, now, readiness_report(connection, entry, now),
                  [("Проверить маршруты", {"text": f"/route_tests {enterprise_id}"}),
                   ("Назад", {"text": f"/settings {enterprise_id}"})])


def checklist_setup_screen(connection: Connection, actor: Actor, now: datetime, enterprise_id: str):
    entry = _entry(connection, actor, now, enterprise_id)
    if "admin_access" not in configuration_fields(entry["settings"], actor, now):
        raise ProcessPermissionError("Only a full administrator can run checklist setup")
    settings = entry["settings"]
    schedule = schedule_issues(settings, now)
    routes = route_issues(connection, enterprise_id, settings, now)
    lines = [f"Мастер чек-листов · {enterprise_id}", f"1. Расписание — {'требует настройки' if schedule else 'проверено'}"]
    lines.extend(schedule)
    if schedule:
        lines.append(f'/configure {enterprise_id} shift_schedule {{"service_starts_at":"09:00",'
                     '"service_ends_at":"21:00","preparation_minutes":10,"closing_minutes":20}')
    lines.append(f"2. Маршруты и роли — {'требуют проверки' if routes else 'проверены'}")
    lines.extend(routes)
    missing = [kind for kind in REQUIRED if not any(
        chat["kind"] == kind for chat in settings["chats"].values()
    )]
    if missing:
        example = json.dumps({"kind": missing[0], "platform": "telegram", "id": "ID_ЧАТА",
                              "user": "ID_ПОЛЬЗОВАТЕЛЯ"}, ensure_ascii=False)
        lines.append(f"/configure {enterprise_id} checklist_route {example}")
        lines.append("После добавления настройте рабочий календарь через chat_calendar.")
    lines.append(f"3. Автозапуск — {'включён' if settings.get('scheduled_checklists') else 'выключен'}")
    links = [("Проверить маршруты", {"text": f"/route_tests {enterprise_id}"})]
    if not schedule and not routes:
        links.append(("Подготовить включение", {"text": f"/configure {enterprise_id} scheduled_checklists true"}))
    links.append(("Настройки", {"text": f"/settings {enterprise_id}"}))
    return screen(connection, actor, now, "\n".join(lines), links)


def scenario_activation_screen(connection: Connection, actor: Actor, now: datetime, enterprise_id: str):
    entry = _entry(connection, actor, now, enterprise_id)
    fields = configuration_fields(entry["settings"], actor, now)
    lines, links = [f"Включение сценариев · {enterprise_id}"], []
    for field, (label, _) in SCENARIOS.items():
        if field not in fields:
            continue
        issues = scenario_issues(connection, entry["id"], entry["settings"], field, now)
        state = "включено" if entry["settings"].get(field) else "выключено"
        lines.append(f"{label} · {state}")
        if issues:
            lines.extend(issues)
        elif not entry["settings"].get(field):
            links.append((f"Включить: {label}", {"text": f"/configure {enterprise_id} {field} true"}))
        if entry["settings"].get(field):
            links.append((f"Выключить: {label}", {"text": f"/configure {enterprise_id} {field} false"}))
    if len(lines) == 1:
        raise ProcessPermissionError("No scenario activation permissions")
    if "admin_access" in fields:
        links.append(("Проверить маршруты", {"text": f"/route_tests {enterprise_id}"}))
    links.append(("Настройки", {"text": f"/settings {enterprise_id}"}))
    return screen(connection, actor, now, "\n".join(lines), links)


def _check_activation(connection, entry, candidate, field, now):
    if not candidate.get(field):
        return
    if field == "scheduled_checklists":
        issues = schedule_issues(candidate, now) + route_issues(connection, entry["id"], candidate, now)
        flag = "verified_checklist_activation"
    elif field in SCENARIOS:
        issues = scenario_issues(connection, entry["id"], candidate, field, now)
        flag = f"verified_{field}"
    else:
        return
    if issues:
        raise ProcessPermissionError("The automatic scenario prerequisites are incomplete")
    candidate[flag] = True


def route_tests_screen(connection: Connection, actor: Actor, now: datetime, enterprise_id: str, page: int):
    entry = _entry(connection, actor, now, enterprise_id)
    if "admin_access" not in configuration_fields(entry["settings"], actor, now):
        raise ProcessPermissionError("Only a full administrator can test routes")
    routes = route_tests(connection, entry, now)
    if page < 0 or page > max(0, (len(routes) - 1) // 8):
        raise ValueError("Invalid page")
    start = page * 8
    visible = routes[start:start + 8]
    text = f"Проверка маршрутов · {entry['id']}\n" + "\n".join(
        f"{key}: {status}" for key, status in visible)
    links = [(f"Проверить {key}"[:60], {
        "route_probe": {"enterprise_id": enterprise_id, "key": key, "revision": entry["revision"]}
    }) for key, _ in visible if entry["settings"]["chats"][key].get("calendar")]
    if page:
        links.append(("Предыдущая", {"text": f"/route_tests {enterprise_id} {page - 1}"}))
    if start + 8 < len(routes):
        links.append(("Следующая", {"text": f"/route_tests {enterprise_id} {page + 1}"}))
    links.append(("К готовности", {"text": f"/readiness {enterprise_id}"}))
    return screen(connection, actor, now, text, links)


def preview(connection: Connection, actor: Actor, now: datetime, arguments: str):
    enterprise_id, field, raw = arguments.split(maxsplit=2)
    entry = _entry(connection, actor, now, enterprise_id)
    if field not in configuration_fields(entry["settings"], actor, now):
        raise ProcessPermissionError("Field is outside this administrator's scope")
    value = json.loads(raw)
    revised = _change(entry["settings"], field, value, now)
    _check_activation(connection, entry, revised, field, now)
    previous = entry["settings"].get(field)
    if field == "checklist_route":
        previous = None
    if field == "admin_access":
        previous = _matching_grants(entry["settings"], value)
    if field == "chat_calendar":
        chat = entry["settings"]["chats"][value["chat"]]
        previous = {"calendar": chat.get("calendar")}
        if "cash_accept_by" in chat:
            previous["cash_accept_by"] = chat["cash_accept_by"]
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


def _add_checklist_route(settings: dict, value: dict) -> None:
    required = {"kind", "platform", "id", "user"}
    if not required <= value.keys() <= required | {"topic_id"}:
        raise ValueError("Specify the route, platform, chat ID and user ID")
    kind, platform = value["kind"], value["platform"]
    if kind not in REQUIRED or platform not in {"telegram", "max"}:
        raise ValueError("Expected a checklist route and a supported platform")
    if kind in settings["chats"] or any(chat["kind"] == kind for chat in settings["chats"].values()):
        raise ValueError("A route of this kind already exists")
    for field in ("id", "user", "topic_id"):
        if field not in value:
            continue
        key = value[field]
        if (type(key) is not str or not key.isascii() or not key.removeprefix("-").isdecimal()
                or len(key) > 21 or (field != "id" and key.startswith("-"))):
            raise ValueError(f"Invalid messenger identifier for {field}")
    if "topic_id" in value and platform != "telegram":
        raise ValueError("Only Telegram routes take a forum topic")
    chat = {"kind": kind, "platform": platform, "id": value["id"]}
    if "topic_id" in value:
        chat["topic_id"] = value["topic_id"]
    settings["chats"][kind] = chat
    settings["grants"].append({"chat": kind, "role": REQUIRED[kind], "user": value["user"]})


def _change(settings: dict, field: str, value, now: datetime) -> dict:
    expected = {"scheduled_reports": bool, "scheduled_checklists": bool, "attendance_monitoring": bool,
                "facilities": list, "writeoff_reasons": list, "aggregator_accounts": list, "shift_schedule": dict, "timing_overrides": dict,
                "chat_calendar": dict, "admin_access": dict, "checklist_route": dict}.get(field, str)
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
        if (not {"chat", "calendar"} <= value.keys() <= {"chat", "calendar", "cash_accept_by"}
                or value["chat"] not in result["chats"]):
            raise ValueError("Specify the existing chat and its calendar")
        calendar = value["calendar"]
        if isinstance(calendar, list) and any(
            not isinstance(window, dict) or set(window) != {"weekday", "start", "end"}
            for window in calendar
        ):
            raise ValueError("Unexpected calendar fields")
        chat = result["chats"][value["chat"]]
        chat["calendar"] = calendar
        if "cash_accept_by" in value:
            if chat["kind"] != "cash" or not isinstance(value["cash_accept_by"], str):
                raise ValueError("Specify a cash acceptance cutoff for a cash route")
            chat["cash_accept_by"] = value["cash_accept_by"]
    elif field == "checklist_route":
        _add_checklist_route(result, value)
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
    _check_activation(connection, entry, revised, field, now)
    revision = save_enterprise(connection, revised, command["revision"], f"{actor.platform}:{actor.user_id}")
    return f"Настройки сохранены. Версия {revision}."
