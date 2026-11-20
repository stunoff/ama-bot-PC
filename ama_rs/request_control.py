"""Management escalation for a task or technical request."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, get_enterprise
from ama_rs.processes import (
    ProcessConflict, Transition, advance_system_card, apply_action, cards, create_card, enqueue, get_card,
    start_timer, utc_value,
)


def _route(settings: dict, kind: str, role: str) -> dict | None:
    matches = [key for key, chat in settings["chats"].items() if chat["kind"] == kind]
    return {"chat": matches[0], "role": role} if len(matches) == 1 else None


def open_request_control(connection: Connection, request, now: datetime) -> None:
    settings = get_enterprise(connection, request["enterprise_id"])["settings"]
    executive = _route(settings, "headquarters", "operations_director")
    general = _route(settings, "headquarters", "general_director")
    if request["kind"] == "technical":
        stages = [
            ("technical", _route(settings, "technical_management", "technical_director")),
            ("executive", executive), ("general", general),
        ]
    else:
        stages = []
        if request["data"]["reviewer"]["role"] == "manager":
            stages.append(("branch", _route(settings, "branch", "branch_lead")))
        if settings["final_route"] == "partner":
            stages.append(("partner", _route(settings, "partner", "franchise_partner")))
        else:
            stages += [("executive", executive), ("general", general)]
    if not stages or stages[0][1] is None:
        return
    # A missing intermediate route ends automatic escalation at the last valid level.
    route = []
    for stage, target in stages:
        if target is None:
            break
        route.append({"stage": stage, **target})
    first = route[0]
    control = create_card(connection, request["enterprise_id"], "request_control", request["id"],
        {"chat": first["chat"], "role": first["role"]},
        {"route": route, "index": 0, "kind": request["kind"],
         "priority": request["data"].get("priority", "normal")},
        now, status="waiting", parent_id=request["id"])
    if control["revision"] != 1:
        return
    _notify(connection, control, first, 1, now)


def _notify(connection: Connection, control, target: dict, revision: int, now: datetime) -> None:
    send_at = now
    if target["stage"] == "technical":
        local = now.astimezone(ZoneInfo("Europe/Moscow"))
        opens = local.replace(hour=9, minute=0, second=0, microsecond=0)
        if local.hour >= 20:
            send_at = opens + timedelta(days=1)
        elif local.hour < 9:
            send_at = opens
    enqueue(connection, control["id"], f"request-control:{revision}", target["chat"],
            {"kind": "request_control", "card_revision": revision}, send_at)


def advance_request_control(connection: Connection, control, now: datetime) -> None:
    index = control["data"]["index"] + 1
    route = control["data"]["route"]
    if index >= len(route):
        return
    target = route[index]
    settings = get_enterprise(connection, control["enterprise_id"])["settings"]
    if target["chat"] not in settings["chats"]:
        return
    revision = advance_system_card(connection, control, "waiting", "transfer_request_control", now,
        assignment={"chat": target["chat"], "role": target["role"]},
        data=control["data"] | {"index": index})
    _notify(connection, control, target, revision, now)


def finish_request_control(connection: Connection, source_id: str, now: datetime) -> None:
    control = connection.execute(select(cards).where(
        cards.c.parent_id == source_id, cards.c.kind == "request_control"
    ).with_for_update()).mappings().one_or_none()
    if control is not None and control["status"] != "closed":
        advance_system_card(connection, control, "closed", "request_resolved", now)


def accept_request_control(connection: Connection, card_id: str, command_id: str, revision: int,
                           note: str, next_due: datetime, actor: Actor, now: datetime) -> int:
    control = get_card(connection, card_id, lock=True)
    if control["kind"] != "request_control" or not note.strip() or utc_value(next_due) <= utc_value(now):
        raise ProcessConflict("Provide a next action and a future deadline")
    target = control["assignment"]
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("accept_control", frozenset({"waiting", "controlled"}), "controlled",
                   frozenset({target["role"]})),
        actor, target["chat"], target["role"], now,
        updates={"next_action": note, "next_due": next_due.isoformat()},
        assignment=target | {"user": actor.user_id})
    if get_card(connection, card_id)["revision"] == updated:
        start_timer(connection, card_id, "request_owner_due", now, next_due - now)
    return updated
