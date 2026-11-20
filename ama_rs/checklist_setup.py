"""Prerequisites for activating automatic enterprise checklists."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, chat_is_open, has_role
from ama_rs.processes import ProcessConflict
from ama_rs.route_probe import confirmed_routes
from ama_rs.shifts import checklist_schedule, route

REQUIRED = {"enterprise": "service_account", "management": "manager", "cash": "cash_reviewer"}
SCENARIO_ROUTES = {
    "scheduled_reports": {"management": "manager", "branch": "branch_lead"},
    "attendance_monitoring": {"management": "manager"},
}


def schedule_issues(settings: dict, now: datetime) -> list[str]:
    shift = settings.get("shift_schedule")
    if not shift or not shift["closing_minutes"]:
        return ["Укажите время обслуживания, подготовки и окно закрытия."]
    try:
        enterprise = route(settings, "enterprise")
    except ProcessConflict:
        return ["Для проверки расписания нужен один маршрут «Предприятие»."]
    today = now.astimezone(ZoneInfo(settings["timezone"])).date()
    for offset in range(7):
        try:
            times = checklist_schedule(settings, today + timedelta(days=offset))
        except ProcessConflict:
            continue
        closing_end = times["evening"] + timedelta(minutes=shift["closing_minutes"])
        if chat_is_open(settings, enterprise, closing_end - timedelta(microseconds=1)):
            return []
    return ["На ближайшие семь дней нет ни одной смены с доступными окнами подготовки и закрытия."]


def route_issues(connection: Connection, enterprise_id: str, settings: dict, now: datetime,
                 *, required: dict[str, str] = REQUIRED) -> list[str]:
    verified = confirmed_routes(connection, enterprise_id, settings, now)
    issues = []
    for kind, role in required.items():
        try:
            key = route(settings, kind)
        except ProcessConflict:
            issues.append(f"{kind}: нужен один рабочий чат.")
            continue
        chat = settings["chats"][key]
        if not chat.get("calendar"):
            issues.append(f"{key}: нет рабочего календаря.")
        if not any(
            has_role(settings, Actor(chat["platform"], grant["user"], chat["id"], chat.get("topic_id")),
                     key, role, now)
            for grant in settings["grants"] if grant["chat"] == key and grant["role"] == role
        ):
            issues.append(f"{key}: назначьте роль {role}.")
        if (key, role) not in verified:
            issues.append(f"{key}: требуется ответ в пробной отправке от роли {role}.")
    return issues


def scenario_routes_ok(connection: Connection, enterprise_id: str, settings: dict,
                       field: str, now: datetime) -> bool:
    return not settings.get(f"verified_{field}") or not route_issues(
        connection, enterprise_id, settings, now, required=SCENARIO_ROUTES[field]
    )
