"""Operational problems, assignments, and first-line technical support."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, get_enterprise, has_role
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, apply_action,
    create_card, enqueue, get_card, start_timer, utc_value,
)
from ama_rs.request_control import finish_request_control
from ama_rs.shifts import route
from ama_rs.timing import effective

WORK_ROLES = {
    "task": {"service_account", "employee", "manager"},
    "technical": {"technician", "vipart_specialist"},
    "maintenance_request": {"maintenance_manager"},
}
REVIEW_ROLES = {"service_account", "employee", "manager", "branch_lead", "cash_reviewer",
                "operator", "senior_operator", "contact_center_lead", "technician"}
CHECKS = {
    "internet": ("power", "connection", "speed"),
    "iiko": ("power", "connection", "exchange_indicator"),
    "terminal": ("power", "connection", "test_receipt"),
    "mangust": ("power", "connection", "restart_application"),
    "equipment": ("power", "connection"),
    "other": ("inspect",),
}
PRIORITIES = {"normal", "high", "critical"}


def settings_for(connection: Connection, enterprise_id: str) -> dict:
    entry = get_enterprise(connection, enterprise_id)
    if entry is None:
        raise ProcessConflict("Unknown enterprise")
    return entry["settings"]


def _authorize(settings: dict, identity: dict, actor: Actor, now: datetime) -> None:
    if not has_role(settings, actor, identity["chat"], identity["role"], now):
        raise ProcessPermissionError("Actor is outside the configured role")


def report_problem(connection: Connection, enterprise_id: str, source_key: str,
                   description: str, actor: Actor, now: datetime):
    settings = settings_for(connection, enterprise_id)
    _authorize(settings, {"chat": route(settings, "enterprise"), "role": "service_account"}, actor, now)
    if not description.strip():
        raise ValueError("Describe the problem")
    manager = route(settings, "management")
    card = create_card(connection, enterprise_id, "problem", source_key,
        {"chat": manager, "role": "manager"}, {"description": description}, now)
    enqueue(connection, card["id"], "problem", manager, {"kind": "card"}, now)
    return card


def make_task(connection: Connection, enterprise_id: str, source_key: str, title: str,
              expected_result: str, due_at: datetime, recipient: dict, reviewer: dict,
              actor: Actor, now: datetime, *, parent_id: str | None = None):
    settings = settings_for(connection, enterprise_id)
    if reviewer["role"] not in {"manager", "branch_lead"}:
        raise ProcessPermissionError("Only enterprise or branch management can issue a task")
    _authorize(settings, reviewer, actor, now)
    if not title.strip() or not expected_result.strip() or utc_value(due_at) <= utc_value(now):
        raise ValueError("Task requires a title, expected result and future deadline")
    _validate_recipient(settings, recipient, WORK_ROLES["task"], now)
    reviewer = reviewer | {"user": actor.user_id}
    if (settings["chats"][recipient["chat"]]["platform"], recipient.get("user")) == (actor.platform, actor.user_id):
        raise ProcessConflict("A person cannot accept their own result")
    task = create_card(connection, enterprise_id, "task", source_key, recipient,
        {"title": title, "expected_result": expected_result, "due_at": due_at.isoformat(),
         "reviewer": reviewer, "worker": recipient},
        now, parent_id=parent_id)
    enqueue(connection, task["id"], "assignment", recipient["chat"], {"kind": "card"}, now)
    if task["revision"] == 1:
        duration = due_at - now
        advance = timedelta(minutes=15 if duration <= timedelta(hours=2) else 60)
        if duration > advance:
            start_timer(connection, task["id"], "execution_warning", now, duration - advance)
        start_timer(connection, task["id"], "execution", now, duration)
        start_timer(connection, task["id"], "execution_escalation", now, duration + timedelta(minutes=30))
    return task


def _validate_recipient(settings: dict, recipient: dict, roles: set[str], now: datetime) -> None:
    if recipient["role"] not in roles:
        raise ProcessPermissionError("Role cannot execute this request")
    chat = settings["chats"].get(recipient["chat"])
    if chat is None:
        raise ProcessConflict("Recipient chat is missing")
    if not any(
        has_role(settings, Actor(chat["platform"], grant["user"], chat["id"], chat.get("topic_id")),
                 recipient["chat"], recipient["role"], now)
        and recipient.get("user") in (None, grant["user"])
        for grant in settings["grants"]
    ):
        raise ProcessConflict("No current recipient for this assignment")


def resolve_problem(connection: Connection, problem_id: str, command_id: str, revision: int,
                    resolution: str, actor: Actor, now: datetime, *,
                    note: str, linked_card_id: str | None = None) -> int:
    problem = get_card(connection, problem_id, lock=True)
    if problem["kind"] != "problem" or not note.strip():
        raise ValueError("Expected a problem and a resolution note")
    if resolution not in {"closed", "task", "technical", "maintenance_request", "escalation", "self_diagnosed"}:
        raise ValueError("Unknown problem outcome")
    if resolution in {"closed", "self_diagnosed"}:
        if linked_card_id is not None:
            raise ValueError("This resolution has no linked workflow")
    else:
        if linked_card_id is None:
            raise ProcessConflict("A linked workflow is required")
        linked = get_card(connection, linked_card_id)
        if linked["parent_id"] != problem_id or linked["kind"] != resolution:
            raise ProcessConflict("Linked card does not resolve this problem")
    settings = settings_for(connection, problem["enterprise_id"])
    manager = route(settings, "management")
    return apply_action(connection, problem_id, command_id, revision,
        Transition("resolve_problem", frozenset({"open"}), "waiting" if linked_card_id else "closed",
                   frozenset({"manager"})),
        actor, manager, "manager", now,
        updates={"resolution": resolution, "note": note, "linked_card_id": linked_card_id})


def open_technical_request(
    connection: Connection, enterprise_id: str, source_key: str, category: str,
    checks: dict[str, str], description: str, priority: str, requester: dict, actor: Actor,
    now: datetime, *, parent_id: str | None = None,
):
    settings = settings_for(connection, enterprise_id)
    if requester["role"] not in REVIEW_ROLES:
        raise ProcessPermissionError("Role cannot request technical help")
    _authorize(settings, requester, actor, now)
    if category not in CHECKS or set(checks) != set(CHECKS[category]) or any(
        value not in {"done", "unavailable"} for value in checks.values()
    ):
        raise ProcessConflict("Complete the category's diagnostic checks")
    if priority not in PRIORITIES or not description.strip():
        raise ValueError("Technical request requires a priority and description")
    hour = now.astimezone(ZoneInfo("Europe/Moscow")).hour
    night = not 9 <= hour < 20
    destination = route(settings, "vipart" if night else "technical")
    worker = {"chat": destination, "role": "vipart_specialist" if night else "technician"}
    _validate_recipient(settings, worker, WORK_ROLES["technical"], now)
    request = create_card(connection, enterprise_id, "technical", source_key, worker,
        {"category": category, "checks": checks, "description": description, "priority": priority,
         "reviewer": requester | {"user": actor.user_id}, "worker": worker, "night": night,
         "server_address": settings["server_address"], "lm_address": settings["lm_address"]},
        now, parent_id=parent_id)
    enqueue(connection, request["id"], "assignment", destination, {"kind": "card"}, now)
    if request["revision"] == 1:
        names = ("acceptance", "reminder", "breach")
        minutes = effective(settings, f"technical.{'vipart' if night else priority}")
        for name, delay in zip(names, minutes):
            start_timer(connection, request["id"], name, now, timedelta(minutes=delay))
    return request


def take_request(connection: Connection, card_id: str, command_id: str, revision: int,
                 actor: Actor, now: datetime, *, next_action: str = "",
                 next_due: datetime | None = None) -> int:
    card = get_card(connection, card_id, lock=True)
    worker = card["data"]["worker"]
    if card["kind"] == "technical":
        if bool(next_action.strip()) != (next_due is not None):
            raise ValueError("Provide a next action and its deadline together")
        if worker["role"] == "vipart_specialist" and next_due is None:
            raise ValueError("VIPART requires a next action and a future deadline")
    if next_due is not None and utc_value(next_due) <= utc_value(now):
        raise ValueError("The next deadline must be in the future")
    updates = {"worker": worker | {"user": actor.user_id}, "next_action": next_action,
               "next_due": next_due.isoformat() if next_due else None}
    new_revision = apply_action(connection, card_id, command_id, revision,
        Transition("take", frozenset({"open"}), "working", frozenset(WORK_ROLES[card["kind"]])),
        actor, worker["chat"], worker["role"], now,
        updates=updates, assignment=updates["worker"])
    if next_due is not None:
        start_timer(connection, card_id, "next_action", now, next_due - now)
    elif card["kind"] == "task":
        due_at = datetime.fromisoformat(card["data"]["due_at"])
        if utc_value(due_at) > utc_value(now):
            start_timer(connection, card_id, "execution", now, due_at - now)
    return new_revision


def set_technical_plan(connection: Connection, card_id: str, command_id: str, revision: int,
                       next_action: str, next_due: datetime, actor: Actor, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    if card["kind"] != "technical" or not next_action.strip() or utc_value(next_due) <= utc_value(now):
        raise ValueError("Provide a technical next action and a future deadline")
    worker = card["data"]["worker"]
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("set_technical_plan", frozenset({"working"}), None, frozenset(WORK_ROLES["technical"])),
        actor, worker["chat"], worker["role"], now,
        updates={"next_action": next_action, "next_due": next_due.isoformat()})
    if get_card(connection, card_id)["revision"] == updated:
        start_timer(connection, card_id, "next_action", now, next_due - now)
    return updated


def submit_result(connection: Connection, card_id: str, command_id: str, revision: int,
                  actor: Actor, result: str, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    worker, reviewer = card["data"]["worker"], card["data"]["reviewer"]
    if not result.strip():
        raise ValueError("A result is required")
    settings = settings_for(connection, card["enterprise_id"])
    if settings["chats"][reviewer["chat"]]["platform"] == actor.platform and reviewer.get("user") == actor.user_id:
        raise ProcessPermissionError("A person cannot verify their own result")
    new_revision = apply_action(connection, card_id, command_id, revision,
        Transition("submit_result", frozenset({"working"}), "review", frozenset(WORK_ROLES[card["kind"]])),
        actor, worker["chat"], worker["role"], now,
        updates={"result": result}, assignment=reviewer)
    enqueue(connection, card_id, f"review:{new_revision}", reviewer["chat"],
            {"kind": "review", "card_revision": new_revision}, now)
    return new_revision


def review_result(connection: Connection, card_id: str, command_id: str, revision: int,
                  actor: Actor, accepted: bool, note: str, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    worker, reviewer = card["data"]["worker"], card["data"]["reviewer"]
    if type(accepted) is not bool or (not accepted and not note.strip()):
        raise ValueError("Returning a result requires a note")
    new_revision = apply_action(connection, card_id, command_id, revision,
        Transition("review_result", frozenset({"review"}), "closed" if accepted else "working",
                   frozenset({reviewer["role"]})),
        actor, reviewer["chat"], reviewer["role"], now,
        updates={"accepted": accepted, "review_note": note}, assignment=None if accepted else worker)
    if accepted:
        finish_request_control(connection, card_id, now)
    else:
        enqueue(connection, card_id, f"returned:{new_revision}", worker["chat"],
                {"kind": "card", "card_revision": new_revision}, now)
    return new_revision


def finish_linked_problem(connection: Connection, problem_id: str, command_id: str, revision: int,
                          actor: Actor, now: datetime) -> int:
    problem = get_card(connection, problem_id, lock=True)
    if problem["kind"] != "problem" or not problem["data"].get("linked_card_id"):
        raise ValueError("Expected a problem with linked work")
    work = get_card(connection, problem["data"]["linked_card_id"])
    if work["status"] != "closed":
        raise ProcessConflict("Linked work is still open")
    manager = route(settings_for(connection, problem["enterprise_id"]), "management")
    return apply_action(connection, problem_id, command_id, revision,
        Transition("finish_linked_problem", frozenset({"waiting"}), "closed", frozenset({"manager"})),
        actor, manager, "manager", now, updates={"linked_result": work["data"].get("result")})
