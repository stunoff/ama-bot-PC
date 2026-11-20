"""Evidence of outgoing and incoming delivery for an enterprise route."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.chat_menu import configuration_fields
from ama_rs.enterprises import Actor, enterprises, get_enterprise, has_role
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, apply_action, attach, cards, create_card,
    deliveries, enqueue, get_card, utc_value,
)

LIFETIME = timedelta(hours=24)


def identity(chat: dict) -> dict:
    return {field: chat.get(field) for field in ("platform", "id", "topic_id")}


def _working_roles(settings: dict, key: str, actor: Actor, now: datetime) -> set[str]:
    return {grant["role"] for grant in settings["grants"] if grant["chat"] == key
            and grant["user"] == actor.user_id
            and has_role(settings, actor, key, grant["role"], now)}


def start_probe(connection: Connection, enterprise_id: str, key: str, command_id: str,
                revision: int, actor: Actor, now: datetime) -> str:
    entry = connection.execute(select(enterprises).where(enterprises.c.id == enterprise_id)
                               .with_for_update()).mappings().one_or_none()
    if entry is None or "admin_access" not in configuration_fields(entry["settings"], actor, now):
        raise ProcessPermissionError("Only the enterprise's full administrator can start a route test")
    if entry["revision"] != revision:
        raise ProcessConflict("The enterprise configuration changed")
    settings = entry["settings"]
    chat = settings["chats"].get(key)
    if not chat or not chat.get("calendar"):
        raise ProcessConflict("Configure the target chat and calendar first")
    target = identity(chat)
    for grant in settings["grants"]:
        if grant["chat"] != key:
            continue
        recipient = Actor(chat["platform"], grant["user"], chat["id"], chat.get("topic_id"))
        if _working_roles(settings, key, recipient, now):
            break
    else:
        raise ProcessConflict("Assign an active recipient in the target chat")

    # A retry of the same admin action keeps its original challenge.
    previous = connection.scalar(select(cards.c.id).where(
        cards.c.enterprise_id == enterprise_id, cards.c.kind == "route_probe", cards.c.source_key == command_id,
    ))
    if previous:
        return previous
    pending = connection.execute(select(cards.c.id, cards.c.data, deliveries.c.state).join(deliveries)
        .where(cards.c.enterprise_id == enterprise_id, cards.c.kind == "route_probe",
               cards.c.status == "open", deliveries.c.signal == "route-probe",
               deliveries.c.chat_key == key, deliveries.c.state.in_(("pending", "uncertain", "sent")))
    ).mappings()
    for probe in pending:
        if probe["data"]["identity"] == target and datetime.fromisoformat(probe["data"]["expires_at"]) > now:
            return probe["id"]
    card = create_card(connection, enterprise_id, "route_probe", command_id,
        {"chat": key, "role": "system_admin"},
        {"chat": key, "identity": target, "expires_at": (now + LIFETIME).isoformat(),
         "configuration_revision": revision},
        now, status="open")
    enqueue(connection, card["id"], "route-probe", key,
            {"kind": "route_probe", "expected_chat": target, "card_revision": 1}, now)
    return card["id"]


def confirm_probe(connection: Connection, card_id: str, command_id: str, actor: Actor, now: datetime,
                  *, file_id: str | None = None) -> int:
    card = get_card(connection, card_id, lock=True)
    if card["kind"] != "route_probe":
        raise ProcessConflict("Expected a route test")
    data = card["data"]
    entry = get_enterprise(connection, card["enterprise_id"])
    settings = entry["settings"]
    key = data["chat"]
    chat = settings["chats"].get(key)
    if not chat or identity(chat) != data["identity"] or (actor.platform, actor.chat_id, actor.topic_id) != (
        chat["platform"], chat["id"], chat.get("topic_id")
    ):
        raise ProcessPermissionError("Confirm the test in its original chat and topic")
    roles = _working_roles(settings, key, actor, now)
    if not roles:
        raise ProcessPermissionError("The person confirming must have an active role in the route")
    if utc_value(now) >= utc_value(datetime.fromisoformat(data["expires_at"])):
        raise ProcessConflict("This test has expired")
    delivered = connection.execute(select(deliveries.c.state, deliveries.c.payload).where(
        deliveries.c.card_id == card_id, deliveries.c.signal == "route-probe",
    )).mappings().one()
    if delivered["state"] != "sent" or delivered["payload"].get("delivery_chat") != data["identity"]:
        raise ProcessConflict("The message delivery has not been confirmed")
    if card["status"] == "accepted":
        return card["revision"]
    role = sorted(roles)[0]
    if file_id:
        attach(connection, card_id, command_id, "probe_photo", file_id, actor, key, role, now, {},
               assignee_only=False)
    return apply_action(connection, card_id, command_id, card["revision"],
        Transition("confirm_route", frozenset({"open"}), "accepted", frozenset(roles), assignee_only=False),
        actor, key, role, now, updates={"confirmed_at": now.isoformat(), "confirmed_by": actor.user_id,
                                        "confirmed_role": role, "confirmed_roles": sorted(roles), "confirmed_photo": bool(file_id)})


def route_tests(connection: Connection, entry: dict, now: datetime) -> list[tuple[str, str]]:
    settings = entry["settings"]
    query = select(cards.c.data, cards.c.status, deliveries.c.state.label("delivery_state")).join(deliveries).where(
        cards.c.enterprise_id == entry["id"], cards.c.kind == "route_probe", deliveries.c.signal == "route-probe",
    ).order_by(cards.c.created_at.desc(), cards.c.id.desc())
    results = {}
    for row in connection.execute(query).mappings():
        data = row["data"]
        key = data["chat"]
        chat = settings["chats"].get(key)
        if not chat or identity(chat) != data["identity"] or key in results:
            continue
        if row["status"] == "accepted":
            actor = Actor(chat["platform"], data["confirmed_by"], chat["id"], chat.get("topic_id"))
            valid = has_role(settings, actor, key, data["confirmed_role"], now)
            status = "доставка и ответ подтверждены"
            if data.get("confirmed_photo"):
                status += ", фото принято"
            results[key] = status if valid else "роль проверяющего изменилась"
        elif datetime.fromisoformat(data["expires_at"]) <= now:
            results[key] = "проверка истекла"
        else:
            results[key] = {
                "pending": "в очереди", "uncertain": "статус отправки уточняется",
                "sent": "доставлено, ждём ответ", "failed": "ошибка доставки",
                "superseded": "отправка отменена",
            }[row["delivery_state"]]
    return [(key, results.get(key, "проверка не запускалась")) for key in sorted(settings["chats"])]


def confirmed_routes(connection: Connection, enterprise_id: str, settings: dict,
                     now: datetime) -> set[tuple[str, str]]:
    records = connection.execute(select(cards.c.data).where(
        cards.c.enterprise_id == enterprise_id, cards.c.kind == "route_probe", cards.c.status == "accepted",
    )).scalars()
    verified = set()
    for data in records:
        key = data["chat"]
        chat = settings["chats"].get(key)
        if chat and identity(chat) == data["identity"]:
            actor = Actor(chat["platform"], data["confirmed_by"], chat["id"], chat.get("topic_id"))
            for role in data.get("confirmed_roles", [data["confirmed_role"]]):
                if has_role(settings, actor, key, role, now):
                    verified.add((key, role))
    return verified
