"""Audits, inventory and cleaning zones, meetings, and maintenance plans."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, has_role
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, apply_action, attachments,
    cards, create_card, enqueue, get_card, start_timer, utc_value,
)
from ama_rs.requests import _validate_recipient, settings_for
from ama_rs.shifts import route

AUDIT_SECTIONS = ("premises", "production", "sanitation", "team", "cooking", "equipment")
ZONE_PROCESSES = {"inventory", "cleaning"}
MAINTENANCE_PROCESSES = {"inspection", "preventive_service", "seasonal_transition", "maintenance_request"}


def _role_route(settings: dict, actor: Actor, role: str, now: datetime) -> str:
    for key in settings["chats"]:
        if has_role(settings, actor, key, role, now):
            return key
    raise ProcessPermissionError(f"Role {role} is required in the appropriate chat")


def begin_audit(connection: Connection, enterprise_id: str, source_key: str,
                level: str, actor: Actor, now: datetime):
    if level not in {"manager", "branch"}:
        raise ValueError("Invalid audit level")
    settings = settings_for(connection, enterprise_id)
    role = "manager" if level == "manager" else "branch_lead"
    source_chat = _role_route(settings, actor, role, now)
    reviewer = route(settings, "branch") if level == "manager" else None
    audit = create_card(connection, enterprise_id, "audit", source_key,
        {"chat": source_chat, "role": role, "user": actor.user_id},
        {"level": level, "reviewer_chat": reviewer, "author": [actor.platform, actor.user_id],
         "sections": list(AUDIT_SECTIONS), "findings": {}}, now, status="filling")
    if reviewer:
        enqueue(connection, audit["id"], "progress", reviewer, {"kind": "card"}, now)
    return audit


def record_audit_section(connection: Connection, audit_id: str, command_id: str, revision: int,
                         section: str, finding: dict, actor: Actor, now: datetime) -> int:
    audit = get_card(connection, audit_id, lock=True)
    if audit["kind"] != "audit" or section not in audit["data"]["sections"]:
        raise ValueError("Unknown audit section")
    if finding.get("outcome") not in {"ok", "problem", "inapplicable"}:
        raise ValueError("Record a valid outcome")
    if finding["outcome"] == "problem" and not finding.get("description", "").strip():
        raise ValueError("Describe the issue found")
    assignment = audit["assignment"]
    findings = audit["data"]["findings"] | {section: finding}
    return apply_action(connection, audit_id, command_id, revision,
        Transition(f"section:{section}", frozenset({"filling"}), None, frozenset({assignment["role"]})),
        actor, assignment["chat"], assignment["role"], now, updates={"findings": findings})


def submit_audit(connection: Connection, audit_id: str, command_id: str, revision: int,
                 actor: Actor, now: datetime) -> int:
    audit = get_card(connection, audit_id, lock=True)
    if audit["kind"] != "audit" or set(audit["data"]["sections"]) != set(audit["data"]["findings"]):
        raise ProcessConflict("Complete all audit sections")
    reviewer = audit["data"]["reviewer_chat"]
    settings = settings_for(connection, audit["enterprise_id"])
    role = "manager" if reviewer else "branch_lead"
    source_chat = route(settings, "management" if reviewer else "branch")
    revision = apply_action(connection, audit_id, command_id, revision,
        Transition("submit_audit", frozenset({"filling"}), "review" if reviewer else "closed",
                   frozenset({role})),
        actor, source_chat, role, now,
        assignment={"chat": reviewer, "role": "branch_lead"} if reviewer else None)
    if reviewer:
        enqueue(connection, audit_id, f"review:{revision}", reviewer, {"kind": "review"}, now)
    return revision


def review_audit(connection: Connection, audit_id: str, command_id: str, revision: int,
                 accepted: bool, note: str, actor: Actor, now: datetime) -> int:
    audit = get_card(connection, audit_id, lock=True)
    if audit["kind"] != "audit" or audit["data"]["level"] != "manager":
        raise ProcessConflict("Only a manager audit has a separate reviewer")
    if audit["data"]["author"] == [actor.platform, actor.user_id]:
        raise ProcessPermissionError("A person cannot accept their own audit")
    if not accepted and not note.strip():
        raise ValueError("A returned audit requires a note")
    reviewer = audit["data"]["reviewer_chat"]
    settings = settings_for(connection, audit["enterprise_id"])
    source = {"chat": route(settings, "management"), "role": "manager", "user": audit["data"]["author"][1]}
    return apply_action(connection, audit_id, command_id, revision,
        Transition("review_audit", frozenset({"review"}), "closed" if accepted else "filling",
                   frozenset({"branch_lead"})),
        actor, reviewer, "branch_lead", now, updates={"review_note": note},
        assignment=None if accepted else source)


def plan_zones(connection: Connection, enterprise_id: str, kind: str, source_key: str,
               zones: dict[str, dict], deadline: datetime, actor: Actor, now: datetime):
    if kind not in ZONE_PROCESSES or not zones or utc_value(deadline) <= utc_value(now):
        raise ValueError("Specify zones and a future deadline")
    settings = settings_for(connection, enterprise_id)
    management = _role_route(settings, actor, "manager", now)
    reviewer = {"chat": management, "role": "manager", "user": actor.user_id}
    for worker in zones.values():
        _validate_recipient(settings, worker, {"employee", "service_account"}, now)
    plan = create_card(connection, enterprise_id, kind, source_key, reviewer,
        {"zones": sorted(zones), "deadline": deadline.isoformat()}, now)
    if plan["data"]["zones"] != sorted(zones):
        raise ProcessConflict("The existing plan has a different zone list")
    for name, worker in zones.items():
        zone = create_card(connection, enterprise_id, f"{kind}_zone", f"{plan['id']}:{name}",
            worker, {"name": name, "worker": worker, "reviewer": reviewer}, now, parent_id=plan["id"])
        enqueue(connection, zone["id"], "assignment", worker["chat"], {"kind": "card"}, now)
        if zone["revision"] == 1:
            start_timer(connection, zone["id"], "zone_deadline", now, deadline - now)
    return plan


def submit_zone(connection: Connection, zone_id: str, command_id: str, revision: int,
                method: str, actor: Actor, now: datetime, *, reference: str = "") -> int:
    zone = get_card(connection, zone_id, lock=True)
    if zone["kind"] not in {"inventory_zone", "cleaning_zone"}:
        raise ProcessConflict("Expected a zone")
    if method not in {"form", "iiko", "photos"} or (zone["kind"] == "cleaning_zone" and method != "photos"):
        raise ValueError("Unsupported zone completion method")
    if method == "iiko":
        if not reference.strip():
            raise ValueError("The iiko count reference is required")
    elif connection.scalar(select(attachments.c.command_id).where(
        attachments.c.card_id == zone_id).limit(1)) is None:
        raise ProcessConflict("Upload the required photo or count sheet")
    worker, reviewer = zone["data"]["worker"], zone["data"]["reviewer"]
    revision = apply_action(connection, zone_id, command_id, revision,
        Transition("submit_zone", frozenset({"open"}), "review", frozenset({worker["role"]})),
        actor, worker["chat"], worker["role"], now, updates={"method": method, "iiko_reference": reference},
        assignment=reviewer)
    enqueue(connection, zone_id, f"review:{revision}", reviewer["chat"],
            {"kind": "review", "card_revision": revision}, now)
    return revision


def review_zone(connection: Connection, zone_id: str, command_id: str, revision: int,
                accepted: bool, actor: Actor, now: datetime, *, note: str = "") -> int:
    zone = get_card(connection, zone_id, lock=True)
    if zone["kind"] not in {"inventory_zone", "cleaning_zone"} or (not accepted and not note.strip()):
        raise ValueError("Returning a zone requires an explanation")
    reviewer = zone["data"]["reviewer"]
    return apply_action(connection, zone_id, command_id, revision,
        Transition("review_zone", frozenset({"review"}), "closed" if accepted else "open", frozenset({"manager"})),
        actor, reviewer["chat"], "manager", now, updates={"review_note": note},
        assignment=None if accepted else zone["data"]["worker"])


def finish_zones(connection: Connection, plan_id: str, command_id: str, revision: int,
                 actor: Actor, now: datetime, *, posted_document: str = "") -> int:
    plan = get_card(connection, plan_id, lock=True)
    if plan["kind"] not in ZONE_PROCESSES:
        raise ProcessConflict("Expected a zone-based plan")
    children = connection.execute(select(cards.c.status).where(
        cards.c.parent_id == plan_id, cards.c.kind == f"{plan['kind']}_zone"
    ).with_for_update()).scalars().all()
    if len(children) != len(plan["data"]["zones"]) or any(state != "closed" for state in children):
        raise ProcessConflict("Every zone must be accepted")
    if plan["kind"] == "inventory" and not posted_document.strip():
        raise ValueError("Record the document posted in iiko")
    return apply_action(connection, plan_id, command_id, revision,
        Transition("finish_plan", frozenset({"open"}), "closed", frozenset({"manager"})),
        actor, plan["assignment"]["chat"], "manager", now, updates={"posted_document": posted_document})


def plan_regular_work(connection: Connection, enterprise_id: str, kind: str, source_key: str,
                      title: str, due_at: datetime, actor: Actor, now: datetime,
                      *, equipment_id: str | None = None):
    if kind not in MAINTENANCE_PROCESSES | {"meeting"} or utc_value(due_at) <= utc_value(now):
        raise ValueError("Invalid work type or deadline")
    if not title.strip() or (kind == "preventive_service" and not equipment_id):
        raise ValueError("Work requires a title and equipment for scheduled maintenance")
    settings = settings_for(connection, enterprise_id)
    management = _role_route(settings, actor, "manager", now)
    reviewer = {"chat": management, "role": "manager", "user": actor.user_id}
    worker = reviewer if kind == "meeting" else {"chat": route(settings, "maintenance"), "role": "maintenance_manager"}
    _validate_recipient(settings, worker, {worker["role"]}, now)
    work = create_card(connection, enterprise_id, kind, source_key, worker,
        {"title": title, "due_at": due_at.isoformat(), "equipment_id": equipment_id,
         "worker": worker, "reviewer": reviewer}, now)
    enqueue(connection, work["id"], "assignment", worker["chat"], {"kind": "card"}, now)
    start_timer(connection, work["id"], "execution", now, due_at - now)
    return work


def finish_regular_work(connection: Connection, card_id: str, command_id: str, revision: int,
                        result: str, actor: Actor, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    if card["kind"] not in MAINTENANCE_PROCESSES | {"meeting"} or not result.strip():
        raise ValueError("Record the work result or meeting minutes")
    worker = card["data"]["worker"]
    return apply_action(connection, card_id, command_id, revision,
        Transition("finish_regular_work", frozenset({"open"}), "closed", frozenset({worker["role"]})),
        actor, worker["chat"], worker["role"], now, updates={"result": result})
