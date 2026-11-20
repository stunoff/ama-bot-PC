"""Start due checklists inside explicitly enabled enterprise shift windows."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, chat_is_open, enterprises, has_role
from ama_rs.processes import ProcessConflict, cards
from ama_rs.shifts import begin_checklist, checklist_schedule, route


def schedule_checklists(connection: Connection, now: datetime) -> None:
    entries = connection.execute(select(enterprises.c.id, enterprises.c.settings)).mappings().all()
    for entry in entries:
        settings = entry["settings"]
        if not settings.get("scheduled_checklists") or not settings.get("shift_schedule"):
            continue
        local = now.astimezone(ZoneInfo(settings["timezone"]))
        for offset in (-1, 0, 1):
            day = local.date() + timedelta(days=offset)
            try:
                schedule = checklist_schedule(settings, day)
                enterprise = route(settings, "enterprise")
                management = route(settings, "management")
            except ProcessConflict:
                continue
            if not chat_is_open(settings, enterprise, now) or not all(
                _has_recipient(settings, key, role, now)
                for key, role in ((enterprise, "service_account"), (management, "manager"))
            ):
                continue
            close_end = schedule["evening"] + timedelta(minutes=settings["shift_schedule"]["closing_minutes"])
            windows = {
                "morning": (schedule["morning"], schedule["evening"]),
                "evening": (schedule["evening"], close_end),
            }
            source_key = f"local:{day}"
            for phase, (start, end) in windows.items():
                if not start.timestamp() <= now.timestamp() < end.timestamp():
                    continue
                present = connection.scalar(select(cards.c.id).where(
                    cards.c.enterprise_id == entry["id"], cards.c.kind == f"{phase}_checklist",
                    cards.c.source_key == source_key,
                ))
                if present is None:
                    begin_checklist(connection, entry["id"], phase, source_key, now, business_day=day)


def _has_recipient(settings: dict, key: str, role: str, now: datetime) -> bool:
    chat = settings["chats"][key]
    return any(
        has_role(settings, Actor(chat["platform"], item["user"], chat["id"], chat.get("topic_id")),
                 key, role, now)
        for item in settings["grants"] if item["chat"] == key and item["role"] == role
    )
