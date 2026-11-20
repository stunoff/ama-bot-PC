"""Readiness checks for enterprise setup in General."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, has_role
from ama_rs.processes import ProcessConflict, cards, deliveries
from ama_rs.reports import coverage
from ama_rs.shifts import checklist_schedule

ROLE_NAMES = {
    "service_account": "служебный аккаунт", "manager": "управляющий",
    "branch_lead": "руководитель филиала", "cash_reviewer": "принимающий кассу",
    "maintenance_manager": "менеджер эксплуатации", "technician": "специалист УК",
    "vipart_specialist": "специалист ВИПАРТ", "technical_director": "технический директор",
    "operations_director": "операционный директор", "franchise_partner": "партнёр",
    "operator": "оператор КЦ", "contact_center_lead": "руководитель КЦ",
    "finance_executor": "финансовый исполнитель", "system_admin": "администратор General",
}


def _requirements(settings):
    management = ("management", "manager")
    service = ("enterprise", "service_account")
    branch = ("branch", "branch_lead")
    final = settings["final_route"]
    operation = [("contact_center", "operator"), ("contact_management", "contact_center_lead")
                 ] if settings["contact_mode"] == "central" else [management]
    return {
        "Администрирование": [("technical", "system_admin")],
        "Смена предприятия": [service, management],
        "Касса": [service, ("cash", "cash_reviewer"), branch],
        "Эксплуатация": [management, ("maintenance", "maintenance_manager")],
        "Техническая поддержка": [("technical", "technician"), ("vipart", "vipart_specialist"),
                                 ("technical_management", "technical_director")],
        "Управленческие эскалации": [management, branch, (final,
                                     "franchise_partner" if final == "partner" else "operations_director")],
        "Операционная работа": [service, *operation],
        "Онлайн-возвраты": [*operation, ("refunds", "finance_executor"), branch],
        "Сводки": [management, branch],
    }


def _recipient(settings, key, role, now):
    chat = settings["chats"][key]
    candidates = ("operator", "senior_operator") if role == "operator" else (role,)
    topic = chat.get("general_topic_id") if role == "system_admin" else chat.get("topic_id")
    return any(
        has_role(settings, Actor(chat["platform"], grant["user"], chat["id"], topic),
                 key, grant["role"], now)
        for grant in settings["grants"] if grant["chat"] == key and grant["role"] in candidates
    )


def _observed_chat_identity(connection, enterprise_id):
    observed = {}
    for row in connection.execute(select(deliveries.c.chat_key, deliveries.c.payload).join(cards).where(
        cards.c.enterprise_id == enterprise_id, deliveries.c.state == "sent",
    )).mappings():
        identity = row["payload"].get("delivery_chat") or row["payload"].get("expected_chat")
        if identity is not None:
            observed.setdefault(row["chat_key"], []).append(identity)
    return observed


def readiness_report(connection: Connection, entry, now: datetime) -> str:
    settings = entry["settings"]
    observed = _observed_chat_identity(connection, entry["id"])
    checks = []
    for name, routes in _requirements(settings).items():
        failures = set()
        unverified = set()
        for kind, role in routes:
            keys = [key for key, chat in settings["chats"].items() if chat["kind"] == kind]
            if len(keys) != 1:
                failures.add(f"{kind}: {'нет чата' if not keys else 'несколько маршрутов'}")
                continue
            key = keys[0]
            chat = settings["chats"][key]
            if role != "system_admin" and not chat.get("calendar"):
                failures.add(f"{key}: нет рабочего окна")
            if not _recipient(settings, key, role, now):
                failures.add(f"{key}: {ROLE_NAMES[role]} не назначен")
            identity = {field: chat.get(field) for field in ("platform", "id", "topic_id")}
            if role == "system_admin":
                identity["topic_id"] = chat.get("general_topic_id")
            if identity not in observed.get(key, []):
                unverified.add(key)
        if name == "Смена предприятия" and settings.get("scheduled_checklists"):
            try:
                checklist_schedule(settings, now.astimezone(ZoneInfo(settings["timezone"])).date())
            except (ProcessConflict, KeyError, ValueError):
                failures.add("Проверьте рабочие окна и расписание чек-листов на текущую дату")
        if failures:
            checks.append(f"{name} — требуется настройка: {', '.join(sorted(failures))}.")
        elif unverified:
            checks.append(f"{name} — настроено, ожидает проверки доставки: {', '.join(sorted(unverified))}.")
        else:
            checks.append(f"{name} — настроено, есть подтверждённая доставка.")
    latest = {}
    for record in connection.execute(select(coverage.c.source, coverage.c.state, coverage.c.updated_at)
                                     .where(coverage.c.enterprise_id == entry["id"])
                                     .order_by(coverage.c.updated_at.desc())):
        latest.setdefault(record.source, (record.state, record.updated_at))
    labels = {"complete": "полные", "partial": "частичные", "unavailable": "недоступны"}
    sources = []
    zone = ZoneInfo(settings["timezone"])
    for source in ("schedule", "attendance", "cash", "orders", "returns", "writeoffs"):
        state, updated = latest.get(source, (None, None))
        timestamp = f" ({updated.replace(tzinfo=UTC).astimezone(zone).isoformat(timespec='minutes')})" if updated else ""
        sources.append(f"{source}: {labels.get(state, 'нет данных')}{timestamp}")
    return (f"Готовность к запуску · {entry['id']}\n" + "\n".join(checks)
            + "\nДанные iiko · " + ", ".join(sources))[:3800]
