"""Linked management control for overdue online refunds."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, get_enterprise
from ama_rs.processes import (
    ProcessConflict, Transition, advance_system_card, apply_action, cards, create_card, enqueue,
    get_card, start_timer, utc_value,
)

REFUND_DONE = {"executed", "not_required", "closed"}


def control_routes(settings: dict) -> dict:
    final = settings["final_route"]
    roles = {"branch": "branch_lead", "headquarters": "operations_director", "partner": "franchise_partner"}
    routes = {}
    for kind, key in (("branch", "branch"), (final, "final")):
        matched = [name for name, chat in settings["chats"].items() if chat["kind"] == kind]
        if len(matched) == 1:
            routes[key] = {"chat": matched[0], "role": roles[kind]}
    return routes


def open_control(connection: Connection, refund, now: datetime) -> None:
    settings = get_enterprise(connection, refund["enterprise_id"])["settings"]
    routes = refund["data"].get("control_routes", control_routes(settings))
    branch = routes.get("branch")
    if branch is None or branch["chat"] not in settings["chats"]:
        return
    card = create_card(connection, refund["enterprise_id"], "refund_control", refund["id"], branch,
        {"route": routes, "refund_id": refund["id"], "brand": refund["data"]["brand"],
         "requested_kopecks": refund["data"]["requested_kopecks"]},
        now, status="branch", parent_id=refund["id"])
    if card["revision"] == 1 and card["status"] == "branch":
        enqueue(connection, card["id"], "control:branch", branch["chat"],
                {"kind": "refund_control", "card_revision": 1}, now)


def move_to_final(connection: Connection, control, now: datetime) -> None:
    target = control["data"]["route"].get("final")
    settings = get_enterprise(connection, control["enterprise_id"])["settings"]
    if control["status"] != "branch" or not target or target["chat"] not in settings["chats"]:
        return
    revision = advance_system_card(connection, control, "final", "transfer_final", now, assignment=target)
    enqueue(connection, control["id"], "control:final", target["chat"],
            {"kind": "refund_control", "card_revision": revision}, now)


def finish_control(connection: Connection, refund_id: str, now: datetime) -> None:
    control = connection.execute(select(cards).where(
        cards.c.parent_id == refund_id, cards.c.kind == "refund_control",
    ).with_for_update()).mappings().one_or_none()
    if control is not None and control["status"] != "closed":
        advance_system_card(connection, control, "closed", "refund_resolved", now)


def take_control(connection: Connection, card_id: str, command_id: str, revision: int,
                 note: str, next_due: datetime, actor: Actor, now: datetime) -> int:
    control = get_card(connection, card_id, lock=True)
    if (control["kind"] != "refund_control" or not note.strip()
            or utc_value(next_due) <= utc_value(now)):
        raise ProcessConflict("Provide the next action and a future deadline")
    if get_card(connection, control["parent_id"])["status"] in REFUND_DONE:
        raise ProcessConflict("Refund has already been resolved")
    assigned = control["assignment"]
    current = apply_action(connection, card_id, command_id, revision,
        Transition("take_control", frozenset({"branch", "final"}), "controlled",
                   frozenset({assigned["role"]})), actor, assigned["chat"], assigned["role"], now,
        updates={"next_action": note, "next_due": next_due.isoformat()},
        assignment=assigned | {"user": actor.user_id})
    start_timer(connection, card_id, "control_owner_due", now, next_due - now)
    return current
