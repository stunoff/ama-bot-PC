"""Role-scoped menus and single-use chat buttons."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, delete, insert, select
from sqlalchemy.engine import Connection

from ama_rs.admin_permissions import EDITABLE
from ama_rs.enterprises import Actor, enterprises, has_role
from ama_rs.processes import ProcessPermissionError, utc_value

buttons = Table(
    "chat_buttons", MetaData(),
    Column("id", String(32), primary_key=True),
    Column("owner", JSON, nullable=False),
    Column("command", JSON, nullable=False),
    Column("expires_at", DateTime, nullable=False),
)


@dataclass(frozen=True)
class Screen:
    text: str
    buttons: tuple[tuple[str, str], ...] = ()


def screen(connection: Connection, actor: Actor, now: datetime, text: str,
           links: list[tuple[str, dict]]) -> Screen:
    available = []
    for title, command in links:
        key = uuid4().hex
        connection.execute(insert(buttons).values(
            id=key, owner=asdict(actor), command=command,
            expires_at=utc_value(now + timedelta(minutes=30)),
        ))
        available.append((title, key))
    return Screen(text, tuple(available))


def consume(connection: Connection, actor: Actor, now: datetime, key: str) -> dict:
    record = connection.execute(select(buttons).where(buttons.c.id == key)
                                .with_for_update()).mappings().one_or_none()
    if not record or record["owner"] != asdict(actor) or record["expires_at"] <= utc_value(now):
        raise ProcessPermissionError("Button is unavailable")
    connection.execute(delete(buttons).where(buttons.c.id == key))
    return record["command"]


def configuration_fields(settings: dict, actor: Actor, now: datetime) -> frozenset[str]:
    if any(has_role(settings, actor, key, "system_admin", now) for key in settings["chats"]):
        return EDITABLE | {"admin_access", "checklist_route"}
    return frozenset(
        field for grant in settings["grants"]
        if grant["role"] == "delegated_admin" and grant["user"] == actor.user_id
        and has_role({**settings, "grants": [grant]}, actor, grant["chat"], "delegated_admin", now)
        for field in grant["fields"]
    )


def admin_scope(connection: Connection, actor: Actor, now: datetime):
    if actor.platform != "telegram":
        return []
    entries = connection.execute(select(enterprises).order_by(enterprises.c.id)).mappings()
    return [
        entry for entry in entries
        if configuration_fields(entry["settings"], actor, now)
    ]


def menu(connection: Connection, actor: Actor, now: datetime) -> Screen:
    links = [("Мои задачи", {"text": "/tasks"}), ("Формы процессов", {"text": "/workflow"}),
             ("Сводка за вчера", {"report": "yesterday"})]
    if admin_scope(connection, actor, now):
        links.append(("Администрирование", {"text": "/settings"}))
    return screen(connection, actor, now, "Меню АМА РС", links)
