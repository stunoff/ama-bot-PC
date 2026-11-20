"""Operational routing, sequential morning updates, and order handling."""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, has_role, work_deadlines
from ama_rs.guest_contact import first_call_keys, guest_timer_keys
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, apply_action, cards,
    create_card, enqueue, get_card, start_timer,
)
from ama_rs.request_control import finish_request_control
from ama_rs.requests import settings_for
from ama_rs.shifts import route
from ama_rs.timing import effective

MORNING_SEQUENCE = ("stop_list", "delivery", "go_list")
OPERATIONS = set(MORNING_SEQUENCE) | {"suspension", "order_request", "order_delay", "redelivery"}
CONTACT_RESULTS = {"not_reached", "warned", "decision_needed", "resolved"}


def operation_destination(settings: dict) -> str:
    return route(settings, "contact_center" if settings["contact_mode"] == "central" else "management")


def _worker_role(settings: dict, actor: Actor, destination: str, now: datetime) -> str:
    roles = ("operator", "senior_operator") if settings["contact_mode"] == "central" else ("manager",)
    for role in roles:
        if has_role(settings, actor, destination, role, now):
            return role
    raise ProcessPermissionError("Actor is outside the assigned operational center")


def _order_keys(payload: dict) -> list[str]:
    orders = payload.get("orders", [])
    keys = []
    for item in orders:
        fields = tuple(item.get(key) for key in ("platform", "number"))
        if any(not isinstance(value, str) or not value.strip() or ":" in value for value in fields):
            raise ValueError("Each order needs a source and number")
        keys.append(":".join(fields))
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("Supply a unique list of orders")
    return keys


def _validate(kind: str, payload: dict) -> dict:
    if kind in {"stop_list", "go_list"}:
        items = payload["items"]
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError("Supply a list of item names, or an empty list")
        if len(items) != len(set(items)):
            raise ValueError("Remove repeated items")
    if kind == "delivery":
        if type(payload.get("enabled")) is not bool:
            raise ValueError("Confirm whether delivery is available")
        for field in ("pickup_minutes", "delivery_minutes"):
            if field == "delivery_minutes" and not payload["enabled"]:
                continue
            if type(payload.get(field)) is not int or payload[field] <= 0:
                raise ValueError("Supply a positive order time in minutes")
    if kind in {"suspension", "order_delay", "redelivery", "order_request"}:
        if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
            raise ValueError("Describe the reason")
    if kind in {"order_request", "order_delay", "redelivery"}:
        keys = _order_keys(payload)
        if kind == "redelivery" and len(keys) != 1:
            raise ValueError("Reference the original order for this redelivery")
        waiting = payload.get("waiting_for_details", [])
        if not isinstance(waiting, list) or any(not isinstance(key, str) or key not in keys for key in waiting):
            raise ValueError("Orders awaiting details must be part of the request")
        return {"order_results": {key: None for key in keys}, "waiting_for_details": waiting}
    return {}


def start_operation(connection: Connection, enterprise_id: str, kind: str, source_key: str,
                    payload: dict, actor: Actor, now: datetime, *, parent_id: str | None = None):
    if kind not in OPERATIONS:
        raise ValueError("Unknown operation")
    settings = settings_for(connection, enterprise_id)
    destination = operation_destination(settings)
    enterprise = route(settings, "enterprise")
    if not has_role(settings, actor, enterprise, "service_account", now):
        _worker_role(settings, actor, destination, now)
    extras = _validate(kind, payload)
    assigned_role = "operator" if settings["contact_mode"] == "central" else "manager"
    if kind == "redelivery":
        original = payload["orders"][0]
        if not isinstance(original.get("date"), str):
            raise ValueError("Identify the original order date")
        day = date.fromisoformat(original["date"]).isoformat()
        source_key = f"{day}:{_order_keys(payload)[0]}"
    card = create_card(
        connection, enterprise_id, kind, source_key, {"chat": destination, "role": assigned_role},
        {"request": payload, "destination": destination, "mode": settings["contact_mode"], **extras},
        now, parent_id=parent_id,
    )
    enqueue(connection, card["id"], "operation", destination, {"kind": "operation"}, now)
    return card


def submit_morning_operation(connection: Connection, checklist_id: str, kind: str,
                             payload: dict, actor: Actor, now: datetime):
    checklist = get_card(connection, checklist_id, lock=True)
    if checklist["kind"] != "morning_checklist" or checklist["status"] not in {"review", "accepted"}:
        raise ProcessConflict("Finish the morning checklist before operational updates")
    settings = settings_for(connection, checklist["enterprise_id"])
    if not has_role(settings, actor, route(settings, "enterprise"), "service_account", now):
        raise ProcessPermissionError("Morning update belongs to the enterprise service account")
    if kind not in MORNING_SEQUENCE:
        raise ValueError("Invalid morning update")
    existing = set(connection.execute(select(cards.c.kind).where(
        cards.c.parent_id == checklist_id).with_for_update()).scalars())
    if not set(MORNING_SEQUENCE[:MORNING_SEQUENCE.index(kind)]) <= existing:
        raise ProcessConflict("Complete previous morning updates")
    return start_operation(connection, checklist["enterprise_id"], kind, f"morning:{checklist_id}",
                           payload, actor, now, parent_id=checklist_id)


def take_operation(connection: Connection, card_id: str, command_id: str, revision: int,
                   actor: Actor, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    if card["kind"] not in OPERATIONS:
        raise ProcessConflict("Expected an operational card")
    settings = settings_for(connection, card["enterprise_id"])
    destination = operation_destination(settings)
    if destination != card["data"]["destination"] or settings["contact_mode"] != card["data"]["mode"]:
        raise ProcessConflict("Operational center changed; transfer this card first")
    role = _worker_role(settings, actor, destination, now)
    assignment = {"chat": destination, "role": role, "user": actor.user_id}
    orders = card["data"].get("order_results", {})
    ready = {}
    if card["kind"] == "order_delay" and len(orders) > 1:
        ready = {order: now.isoformat() for order in orders
                 if order not in card["data"].get("waiting_for_details", [])}
    if card["status"] != "open":
        ready = card["data"].get("first_call_initial", {})
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("take_operation", frozenset({"open"}), "working", frozenset({role}), assignee_only=False),
        actor, destination, role, now, assignment=assignment, updates={"first_call_ready": ready, "first_call_initial": ready})
    if card["status"] == "open":
        for order in ready:
            _start_first_call(connection, card_id, order, settings, destination, now)
    if card["kind"] == "stop_list":
        enqueue(connection, card_id, f"stop-work:{updated}", destination, {"kind": "stop_work"}, now)
    return updated


def _start_first_call(connection, card_id, order, settings, chat, now):
    limits = effective(settings, "operation.mass_first_call")
    for key, deadline in zip(first_call_keys(order), work_deadlines(settings, chat, now, limits)):
        start_timer(connection, card_id, key, now, deadline - now)


def mark_order_ready(connection: Connection, card_id: str, command_id: str, revision: int,
                     order: str, actor: Actor, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    results = card["data"].get("order_results", {})
    if card["kind"] != "order_delay" or len(results) < 2 or order not in results or results[order] is not None:
        raise ProcessConflict("Expected an uncalled order in a mass delay")
    ready = card["data"].get("first_call_ready", {})
    assigned = card["assignment"]
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("order_ready", frozenset({"working"}), None, frozenset({assigned["role"]})),
        actor, assigned["chat"], assigned["role"], now,
        updates={"first_call_ready": ready | {order: ready.get(order, now.isoformat())}})
    if order not in ready:
        _start_first_call(connection, card_id, order, settings_for(connection, card["enterprise_id"]),
                          assigned["chat"], now)
    return updated


def record_order_result(connection: Connection, card_id: str, command_id: str, revision: int,
                        order: str, result: str, actor: Actor, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    current = card["data"].get("order_results", {})
    if order not in current or result not in CONTACT_RESULTS:
        raise ValueError("Unknown order or guest contact result")
    assigned = card["assignment"]
    first_misses = card["data"].get("first_misses", {})
    first_attempt = result == "not_reached" and order not in first_misses
    if first_attempt:
        first_misses = first_misses | {order: now.isoformat()}
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("record_order_result", frozenset({"working"}), None, frozenset({assigned["role"]})),
        actor, assigned["chat"], assigned["role"], now,
        updates={"order_results": current | {order: result}, "first_misses": first_misses})
    if first_attempt:
        settings = settings_for(connection, card["enterprise_id"])
        limits = effective(settings, "operation.guest_contact")
        for key, deadline in zip(guest_timer_keys(order), work_deadlines(settings, assigned["chat"], now, limits)):
            start_timer(connection, card_id, key, now, deadline - now)
    return updated


def close_operation(connection: Connection, card_id: str, command_id: str, revision: int,
                    result: str, actor: Actor, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    if card["kind"] not in OPERATIONS or not result.strip():
        raise ValueError("Describe the operational result")
    if any(item not in {"warned", "resolved"} for item in card["data"].get("order_results", {}).values()):
        raise ProcessConflict("Some orders still need a decision")
    assigned = card["assignment"]
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("close_operation", frozenset({"working"}), "closed", frozenset({assigned["role"]})),
        actor, assigned["chat"], assigned["role"], now, updates={"result": result})
    finish_request_control(connection, card_id, now)
    return updated
