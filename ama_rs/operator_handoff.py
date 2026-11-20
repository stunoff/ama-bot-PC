"""Explicit transfer of an in-progress contact-center assignment."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, get_enterprise, has_role
from ama_rs.guest_contact import pending_first_calls, pending_guest_orders
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, advance_system_card, apply_action,
    cards, carry_clocks, create_card, enqueue, get_card,
)


def _pending(connection: Connection, source_id: str):
    return connection.execute(select(cards).where(
        cards.c.parent_id == source_id, cards.c.kind == "operator_handoff", cards.c.status == "open",
    ).with_for_update()).mappings().one_or_none()


def offer_handoff(connection: Connection, card_id: str, command_id: str, revision: int,
                  recipient_id: str, note: str, actor: Actor, now: datetime) -> str:
    source = get_card(connection, card_id, lock=True)
    prior = source["assignment"]
    if source["data"].get("mode") != "central" or source["status"] != "working" or source["revision"] != revision:
        raise ProcessConflict("Expected the current active contact-center assignment")
    settings = get_enterprise(connection, source["enterprise_id"])["settings"]
    if prior.get("user") != actor.user_id or not has_role(settings, actor, prior["chat"], prior["role"], now):
        raise ProcessPermissionError("Only the assigned available operator can transfer this task")
    if recipient_id == actor.user_id or not note.strip():
        raise ValueError("Choose another operator and provide the handoff notes")
    chat = settings["chats"][prior["chat"]]
    recipient = Actor(chat["platform"], recipient_id, chat["id"], chat.get("topic_id"))
    role = next((role for role in ("operator", "senior_operator")
                 if has_role(settings, recipient, prior["chat"], role, now)), None)
    if role is None:
        raise ProcessPermissionError("Recipient is not an active operator for this enterprise")
    pending = _pending(connection, card_id)
    source_key = f"{card_id}:{command_id}"
    if pending is not None and pending["source_key"] != source_key:
        raise ProcessConflict("Another transfer already awaits a response")
    target = {"chat": prior["chat"], "role": role, "user": recipient_id}
    offer = create_card(connection, source["enterprise_id"], "operator_handoff", source_key, target,
        {"previous": prior, "notes": note}, now, parent_id=card_id)
    enqueue(connection, offer["id"], "handoff-offer", target["chat"],
            {"kind": "handoff_offer", "card_revision": 1}, now)
    return offer["id"]


def respond_handoff(connection: Connection, card_id: str, command_id: str, revision: int,
                    accepted: bool, reason: str, actor: Actor, now: datetime) -> int:
    offer = get_card(connection, card_id)
    if offer["kind"] != "operator_handoff" or (not accepted and not reason.strip()):
        raise ValueError("A declined handoff requires a reason")
    source = get_card(connection, offer["parent_id"], lock=True)
    offer = get_card(connection, card_id, lock=True)
    target = offer["assignment"]
    if offer["status"] == "open" and (
        source["status"] != "working" or source["assignment"] != offer["data"]["previous"]
    ):
        raise ProcessConflict("The original assignment has changed")
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("respond_handoff", frozenset({"open"}), "accepted" if accepted else "closed",
                   frozenset({target["role"]})), actor, target["chat"], target["role"], now,
        updates={"accepted": accepted, "reason": reason})
    if offer["status"] != "open":
        return updated
    if accepted:
        next_revision = advance_system_card(connection, source, source["status"], "operator_handoff", now,
                                            assignment=target)
        keys = ("operation_work_reminder", "operation_work_repeat", "operation_work_escalation",
                "operation_work_control_due")
        keys += tuple(pending_guest_orders(source))
        keys += tuple(pending_first_calls(source))
        carry_clocks(connection, source["id"], source["revision"], next_revision, keys)
    else:
        previous = offer["data"]["previous"]
        enqueue(connection, card_id, "handoff-declined", previous["chat"],
                {"kind": "handoff_declined", "reason": reason, "recipient": previous["user"]}, now)
    return updated


def cancel_pending_handoff(connection: Connection, source_id: str, now: datetime) -> None:
    offer = _pending(connection, source_id)
    if offer is not None:
        advance_system_card(connection, offer, "closed", "operation_completed", now)
