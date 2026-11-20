"""Create an enterprise from General using an explicitly scoped bootstrap grant."""

import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ama_rs.chat_menu import screen
from ama_rs.enterprises import Actor, enterprises, has_role, save_enterprise, validate_settings
from ama_rs.processes import ProcessConflict, ProcessPermissionError

FIELDS = {
    "id", "iiko_id", "name", "timezone", "format", "server_address", "lm_address",
    "contact_mode", "final_route", "brands",
}


def creator_context(connection: Connection, actor: Actor, now: datetime, *, lock: bool = False):
    if actor.platform != "telegram":
        return None
    rows = connection.execute(select(enterprises).order_by(enterprises.c.id)).mappings().all()
    for entry in rows:
        candidate = entry
        if not _creator_key(candidate["settings"], actor, now):
            continue
        if lock:
            candidate = connection.execute(select(enterprises).where(enterprises.c.id == entry["id"])
                                           .with_for_update()).mappings().one_or_none()
        if candidate is not None:
            key = _creator_key(candidate["settings"], actor, now)
            if key:
                return candidate, key
    return None


def _creator_key(settings: dict, actor: Actor, now: datetime) -> str | None:
    for grant in settings["grants"]:
        if (grant["user"] != actor.user_id or grant["role"] != "system_admin"
                or grant.get("create_enterprises") is not True):
            continue
        key = grant["chat"]
        # Test the effective grant itself, including its expiry.
        if has_role(settings | {"grants": [grant]}, actor, key, "system_admin", now):
            return key
    return None


def _configuration(values: dict, creator, actor: Actor) -> dict:
    if not isinstance(values, dict) or set(values) != FIELDS:
        raise ValueError("Specify exactly the enterprise registration fields")
    if not isinstance(values["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", values["id"]):
        raise ValueError("Use a short route-safe enterprise ID")
    if len(json.dumps(values, ensure_ascii=False)) > 2500:
        raise ValueError("Registration fields are too large")
    entry, key = creator
    return values | {
        "chats": {"technical": entry["settings"]["chats"][key]},
        "grants": [{"chat": "technical", "user": actor.user_id, "role": "system_admin"}],
    }


def registration_screen(connection: Connection, actor: Actor, now: datetime, raw: str):
    creator = creator_context(connection, actor, now)
    if creator is None:
        raise ProcessPermissionError("Enterprise creation requires an explicit General bootstrap grant")
    if not raw:
        fields = ", ".join(sorted(FIELDS))
        return screen(connection, actor, now,
            "Новое предприятие · шаг 1 из 2\n"
            "Передайте поля командой /enterprise_new <JSON>.\n"
            f"Обязательные поля: {fields}.\n"
            "Режим КЦ: central или independent. Конечный маршрут: headquarters или partner.",
            [("Вернуться к списку", {"text": "/settings"})])
    values = json.loads(raw)
    validate_settings(_configuration(values, creator, actor))
    exists = connection.scalar(select(enterprises.c.id).where(
        (enterprises.c.id == values["id"]) | (enterprises.c.iiko_id == values["iiko_id"])
    ).limit(1))
    if exists:
        raise ProcessConflict("The enterprise ID or iiko ID is already registered")
    return screen(connection, actor, now,
        f"Новое предприятие · шаг 2 из 2\n{values['name']} · {values['id']}\n"
        f"Часовой пояс: {values['timezone']}\nРежим КЦ: {values['contact_mode']}\n"
        f"Конечный маршрут: {values['final_route']}\nПосле создания будут доступны настройки маршрутов.",
        [("Создать предприятие", {"enterprise_new": values}),
         ("Отмена", {"text": "/settings"})])


def create_enterprise(connection: Connection, actor: Actor, now: datetime, values: dict):
    creator = creator_context(connection, actor, now, lock=True)
    if creator is None:
        raise ProcessPermissionError("The bootstrap grant was revoked or expired")
    settings = _configuration(values, creator, actor)
    try:
        with connection.begin_nested():
            save_enterprise(connection, settings, 0, f"{actor.platform}:{actor.user_id}")
    except IntegrityError as exc:
        raise ProcessConflict("The enterprise ID or iiko ID is already registered") from exc
    enterprise_id = settings["id"]
    return screen(connection, actor, now, f"Предприятие создано · {enterprise_id}",
        [("Мастер чек-листов", {"text": f"/checklist_setup {enterprise_id}"}),
         ("Настройки", {"text": f"/settings {enterprise_id}"})])
