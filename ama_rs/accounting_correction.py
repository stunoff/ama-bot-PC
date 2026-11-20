"""Manual iiko correction requests attached to their original workflow card."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, get_enterprise, has_role
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, apply_action, carry_clocks,
    enqueue, get_card, start_timer, timers, utc_value,
)

KINDS = {"sales.writeoff", "sales.return", "cash_shift", "inventory"}
ACTIVE = {"pending", "working", "review"}


def _context(connection: Connection, card_id: str):
    card = get_card(connection, card_id, lock=True)
    if card["kind"] not in KINDS:
        raise ProcessConflict("This workflow has no iiko correction action")
    settings = get_enterprise(connection, card["enterprise_id"])["settings"]
    return card, settings


def _actor_chat(settings, actor, role, now):
    chats = [chat for chat in settings["chats"] if has_role(settings, actor, chat, role, now)]
    if len(chats) != 1:
        raise ProcessPermissionError("The actor must use the assigned workflow chat")
    return chats[0]


def _write(connection, card, command_id, revision, operation, record, actor, chat, role, now):
    existing = connection.execute(select(timers.c.key).where(
        timers.c.card_id == card["id"], timers.c.card_revision == card["revision"],
        timers.c.key != "accounting_due",
    )).scalars().all()
    updated = apply_action(connection, card["id"], command_id, revision,
        Transition(operation, frozenset({card["status"]}), None, frozenset({role}), assignee_only=False),
        actor, chat, role, now, updates={"accounting": record})
    if updated > card["revision"] and existing:
        carry_clocks(connection, card["id"], card["revision"], updated, tuple(existing))
    return updated


def request_correction(connection: Connection, card_id: str, command_id: str, revision: int,
                       document_id: str, current_value: str, expected_value: str, reason: str,
                       executor_chat: str, executor_user: str, due_at: datetime,
                       actor: Actor, now: datetime) -> int:
    card, settings = _context(connection, card_id)
    if card["status"] not in {"review", "closed", "accepted", "discrepancy"}:
        raise ProcessConflict("Review the source before requesting a correction")
    if (not all(item.strip() for item in (document_id, current_value, expected_value, reason))
            or current_value == expected_value or utc_value(due_at) <= utc_value(now)):
        raise ValueError("Provide document, differing values, reason and a future deadline")
    if card["kind"].startswith("sales.") and document_id != card["source_key"]:
        raise ValueError("Use the iiko document of the source card")
    if card["kind"] == "inventory" and document_id != card["data"].get("posted_document"):
        raise ValueError("Use the posted inventory document")
    manager = _actor_chat(settings, actor, "manager", now)
    chat = settings["chats"].get(executor_chat)
    if (not chat or not has_role(settings, Actor(chat["platform"], executor_user, chat["id"], chat.get("topic_id")),
                                 executor_chat, "accounting_executor", now)):
        raise ProcessPermissionError("Choose an assigned accounting executor")
    old = card["data"].get("accounting", {})
    if old.get("status") in ACTIVE and old.get("request_id") != command_id:
        raise ProcessConflict("A correction is already open in this source card")
    record = {"request_id": command_id, "status": "pending", "document_id": document_id,
              "current_value": current_value, "expected_value": expected_value, "reason": reason,
              "executor_chat": executor_chat, "executor_user": executor_user, "manager": manager,
              "due_at": due_at.isoformat()}
    updated = _write(connection, card, command_id, revision, "accounting_request",
                     record, actor, manager, "manager", now)
    if get_card(connection, card_id)["revision"] == updated:
        start_timer(connection, card_id, "accounting_due", now, due_at - now)
    enqueue(connection, card_id, f"accounting-request:{updated}", executor_chat,
            {"kind": "accounting", "card_revision": updated}, now)
    return updated


def submit_correction(connection: Connection, card_id: str, command_id: str, revision: int,
                      outcome: str, result: str, actor: Actor, now: datetime, *,
                      confirmation: str = "", corrected_by: str = "") -> int:
    card, settings = _context(connection, card_id)
    record = card["data"].get("accounting", {})
    if record.get("executor_user") != actor.user_id or not has_role(
        settings, actor, record.get("executor_chat", ""), "accounting_executor", now
    ):
        raise ProcessPermissionError("This correction has another executor")
    if outcome not in {"working", "corrected", "refused"} or not result.strip():
        raise ValueError("Provide the decision and details")
    if outcome == "corrected" and (not confirmation.strip() or not corrected_by.strip()):
        raise ValueError("Record verification and who corrected the iiko document")
    if record["status"] not in {"pending", "working"}:
        raise ProcessConflict("This correction has already been submitted")
    status = "review" if outcome == "corrected" else outcome
    updated = _write(connection, card, command_id, revision, "accounting_submit",
        record | {"status": status, "result": result, "confirmation": confirmation, "corrected_by": corrected_by},
        actor, record["executor_chat"], "accounting_executor", now)
    if status == "working":
        carry_clocks(connection, card_id, card["revision"], updated, ("accounting_due",))
    else:
        enqueue(connection, card_id, f"accounting-result:{updated}", record["manager"],
                {"kind": "accounting", "card_revision": updated}, now)
    return updated


def review_correction(connection: Connection, card_id: str, command_id: str, revision: int,
                      confirmed: bool, note: str, actor: Actor, now: datetime) -> int:
    card, _ = _context(connection, card_id)
    record = card["data"].get("accounting", {})
    if record.get("status") != "review" or (not confirmed and not note.strip()):
        raise ProcessConflict("Review a submitted correction or give a return reason")
    if actor.user_id == record["executor_user"]:
        raise ProcessPermissionError("The executor cannot verify their own correction")
    state = "confirmed" if confirmed else "pending"
    updated = _write(connection, card, command_id, revision, "accounting_review",
                     record | {"status": state, "review_note": note},
                     actor, record["manager"], "manager", now)
    if not confirmed:
        due_at = datetime.fromisoformat(record["due_at"])
        # An already elapsed source deadline stays elapsed.
        moment = max(now + timedelta(seconds=1), due_at)
        start_timer(connection, card_id, "accounting_due", now, moment - now)
        enqueue(connection, card_id, f"accounting-return:{updated}", record["executor_chat"],
                {"kind": "accounting", "card_revision": updated}, now)
    return updated


def read_correction(connection: Connection, card_id: str, actor: Actor, now: datetime) -> dict:
    if actor.user_id != actor.chat_id or actor.topic_id is not None:
        raise ProcessPermissionError("Use a private dialog to view accounting correction details")
    card, settings = _context(connection, card_id)
    record = card["data"].get("accounting")
    if not record:
        raise ProcessConflict("No correction exists on this source")
    for key, role in ((record["manager"], "manager"), (record["executor_chat"], "accounting_executor")):
        chat = settings["chats"].get(key)
        if not chat or (role == "accounting_executor" and actor.user_id != record["executor_user"]):
            continue
        subject = Actor(actor.platform, actor.user_id, chat["id"], chat.get("topic_id"))
        if has_role(settings, subject, key, role, now):
            return dict(record) | {"revision": card["revision"]}
    raise ProcessPermissionError("Correction details are outside this user's assignments")
