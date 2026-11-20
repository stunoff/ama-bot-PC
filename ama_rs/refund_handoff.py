"""Transfer a financial assignment only after the new executor accepts."""

from datetime import datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, get_enterprise, has_role
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, advance_system_card, apply_action,
    cards, carry_clocks, create_card, enqueue, get_card, timers,
)

KIND = "refund_handoff"
ACTIVE = {"checking", "clarification", "reserved", "uncertain"}
ROLE = "finance_executor"


def _pending(connection: Connection, card_id: str):
    return connection.execute(select(cards).where(
        cards.c.parent_id == card_id, cards.c.kind == KIND, cards.c.status == "open",
    ).with_for_update()).mappings().one_or_none()


def offer_refund_handoff(connection: Connection, card_id: str, command_id: str, revision: int,
                         recipient_id: str, note: str, actor: Actor, now: datetime) -> str:
    source = get_card(connection, card_id, lock=True)
    if source["kind"] != "refund" or source["status"] not in ACTIVE or source["revision"] != revision:
        raise ProcessConflict("Expected the current version of an assigned refund")
    prior = source["data"]["worker"]
    settings = get_enterprise(connection, source["enterprise_id"])["settings"]
    if prior.get("user") != actor.user_id or not has_role(settings, actor, prior["chat"], ROLE, now):
        raise ProcessPermissionError("Only the assigned financial executor can transfer this refund")
    if recipient_id == actor.user_id or not note.strip():
        raise ValueError("Provide another executor and the reason for the handoff")
    chat = settings["chats"][prior["chat"]]
    recipient = Actor(chat["platform"], recipient_id, chat["id"], chat.get("topic_id"))
    if not has_role(settings, recipient, prior["chat"], ROLE, now):
        raise ProcessPermissionError("Choose an executor in the assigned financial route")
    source_key = sha256(f"{card_id}:{command_id}".encode()).hexdigest()
    pending = _pending(connection, card_id)
    if pending is not None and pending["source_key"] != source_key:
        raise ProcessConflict("A transfer already awaits a response")
    offer = create_card(connection, source["enterprise_id"], KIND, source_key,
        prior | {"user": recipient_id}, {"previous": prior, "note": note}, now, parent_id=card_id)
    enqueue(connection, offer["id"], "refund-handoff", prior["chat"],
            {"kind": KIND, "card_revision": 1}, now)
    return offer["id"]


def respond_refund_handoff(connection: Connection, card_id: str, command_id: str, revision: int,
                           accepted: bool, reason: str, actor: Actor, now: datetime) -> int:
    offer = get_card(connection, card_id)
    if offer["kind"] != KIND or type(accepted) is not bool or (not accepted and not reason.strip()):
        raise ValueError("Provide a decision and the reason for declining")
    source = get_card(connection, offer["parent_id"], lock=True)
    offer = get_card(connection, card_id, lock=True)
    target = offer["assignment"]
    if offer["status"] == "open" and (
        source["kind"] != "refund" or source["status"] not in ACTIVE
        or source["data"]["worker"] != offer["data"]["previous"]
    ):
        raise ProcessConflict("The financial assignment is no longer current")
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("refund_handoff_response", frozenset({"open"}), "accepted" if accepted else "closed",
                   frozenset({ROLE})), actor, target["chat"], ROLE, now,
        updates={"accepted": accepted, "reason": reason})
    if offer["status"] != "open":
        return updated
    if not accepted:
        enqueue(connection, card_id, "refund-handoff-declined", target["chat"],
                {"kind": "refund_handoff_declined"}, now)
        return updated
    keys = tuple(connection.execute(select(timers.c.key).where(
        timers.c.card_id == source["id"], timers.c.card_revision == source["revision"],
    )).scalars())
    assignment = source["assignment"] if source["status"] == "clarification" else target
    next_revision = advance_system_card(connection, source, source["status"], "refund_handoff", now,
        assignment=assignment, data=source["data"] | {"worker": target})
    if keys:
        carry_clocks(connection, source["id"], source["revision"], next_revision, keys)
    return updated


def cancel_refund_handoff(connection: Connection, card_id: str, now: datetime) -> None:
    pending = _pending(connection, card_id)
    if pending is not None:
        advance_system_card(connection, pending, "closed", "refund_completed", now)
