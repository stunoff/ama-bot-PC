"""Manual iiko correction requests attached to their original workflow card."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, get_enterprise, has_role
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, advance_system_card, apply_action, cards, carry_clocks,
    enqueue, get_card, start_timer, timers, utc_value,
)

KINDS = {"sales.writeoff", "sales.return", "cash_shift", "inventory"}
EXECUTION = {"pending", "working", "clarification"}
ACTIVE = EXECUTION | {"review"}


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


def _executor_active(settings: dict, key: str, user: str, now: datetime) -> bool:
    chat = settings["chats"].get(key)
    return bool(chat and has_role(settings, Actor(chat["platform"], user, chat["id"], chat.get("topic_id")),
                                  key, "accounting_executor", now))


def _notify_executor(connection, card_id, record, signal, now):
    target = record["executor_chat"]
    enqueue(connection, card_id, signal, target, {
        "kind": "accounting", "accounting_executor": record["executor_user"],
        "accounting_request": record["request_id"], "accounting_state": record["status"],
    }, now)


def request_correction(connection: Connection, card_id: str, command_id: str, revision: int,
                       document_id: str, current_value: str, expected_value: str, reason: str,
                       executor_chat: str, executor_user: str, due_at: datetime,
                       actor: Actor, now: datetime, *,
                       reserve_chat: str = "", reserve_user: str = "") -> int:
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
    if not _executor_active(settings, executor_chat, executor_user, now):
        raise ProcessPermissionError("Choose an assigned accounting executor")
    if reserve_chat or reserve_user:
        if not reserve_chat or not reserve_user or executor_user == reserve_user or not _executor_active(
            settings, reserve_chat, reserve_user, now
        ):
            raise ProcessPermissionError("Choose a different active reserve executor")
    old = card["data"].get("accounting", {})
    if old.get("status") in ACTIVE and old.get("request_id") != command_id:
        raise ProcessConflict("A correction is already open in this source card")
    record = {"request_id": command_id, "status": "pending", "document_id": document_id,
              "current_value": current_value, "expected_value": expected_value, "reason": reason,
              "executor_chat": executor_chat, "executor_user": executor_user, "manager": manager,
              "due_at": due_at.isoformat(), "reserve_chat": reserve_chat, "reserve_user": reserve_user}
    updated = _write(connection, card, command_id, revision, "accounting_request",
                     record, actor, manager, "manager", now)
    if get_card(connection, card_id)["revision"] == updated:
        start_timer(connection, card_id, "accounting_due", now, due_at - now)
    _notify_executor(connection, card_id, record, f"accounting-request:{updated}", now)
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
    if outcome not in {"working", "corrected", "refused", "clarification"} or not result.strip():
        raise ValueError("Provide the decision and details")
    if outcome == "corrected" and (not confirmation.strip() or not corrected_by.strip()):
        raise ValueError("Record verification and who corrected the iiko document")
    if record["status"] not in {"pending", "working"}:
        raise ProcessConflict("This correction has already been submitted")
    status = "review" if outcome == "corrected" else outcome
    record |= {"status": status, "result": result, "confirmation": confirmation, "corrected_by": corrected_by}
    if status == "clarification":
        record["question"] = result
    updated = _write(connection, card, command_id, revision, "accounting_submit",
        record, actor, record["executor_chat"], "accounting_executor", now)
    if status in EXECUTION:
        carry_clocks(connection, card_id, card["revision"], updated, ("accounting_due",))
    if status != "working":
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
        _notify_executor(connection, card_id, record | {"status": "pending"},
                         f"accounting-return:{updated}", now)
    return updated


def answer_correction(connection: Connection, card_id: str, command_id: str, revision: int,
                      answer: str, actor: Actor, now: datetime) -> int:
    card, _ = _context(connection, card_id)
    record = card["data"].get("accounting", {})
    if record.get("status") != "clarification" or not answer.strip():
        raise ProcessConflict("Answer an active correction question")
    record |= {"status": "pending", "answer": answer}
    updated = _write(connection, card, command_id, revision, "accounting_answer", record,
                     actor, record["manager"], "manager", now)
    carry_clocks(connection, card_id, card["revision"], updated, ("accounting_due",))
    _notify_executor(connection, card_id, record, f"accounting-answer:{updated}", now)
    return updated


def switch_to_reserve(connection: Connection, card_id: str, now: datetime, *,
                      failed_payload: dict | None = None) -> bool:
    card, settings = _context(connection, card_id)
    record = card["data"].get("accounting", {})
    if record.get("status") not in EXECUTION or record.get("using_reserve"):
        return False
    if failed_payload is None:
        if _executor_active(settings, record["executor_chat"], record["executor_user"], now):
            return False
    elif (record["request_id"] != failed_payload.get("accounting_request")
          or record["executor_user"] != failed_payload.get("accounting_executor")
          or record["status"] != failed_payload.get("accounting_state")):
        return False
    key, user = record.get("reserve_chat"), record.get("reserve_user")
    if not key or not user or not _executor_active(settings, key, user, now):
        return False
    record |= {"executor_chat": key, "executor_user": user, "using_reserve": True}
    keys = tuple(connection.execute(select(timers.c.key).where(
        timers.c.card_id == card_id, timers.c.card_revision == card["revision"],
    )).scalars())
    revision = advance_system_card(connection, card, card["status"], "accounting_reserve", now,
                                   data=card["data"] | {"accounting": record})
    if keys:
        carry_clocks(connection, card_id, card["revision"], revision, keys)
    if record["status"] != "clarification":
        _notify_executor(connection, card_id, record, f"accounting-reserve:{revision}", now)
    else:
        enqueue(connection, card_id, f"accounting-question-reserve:{revision}", record["manager"],
                {"kind": "accounting", "card_revision": revision}, now)
    return True


def check_reserves(connection: Connection, now: datetime) -> None:
    ids = connection.execute(select(cards.c.id).where(
        cards.c.kind.in_(KINDS), cards.c.data["accounting"]["status"].as_string().in_(EXECUTION),
        cards.c.data["accounting"]["reserve_user"].as_string().not_in(("", "null")),
    )).scalars().all()
    for card_id in ids:
        switch_to_reserve(connection, card_id, now)


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
