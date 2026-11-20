"""Online refund requests and manual financial execution controls."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.contact_center import operation_destination
from ama_rs.enterprises import Actor, enterprises, has_role
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, apply_action, cards,
    create_card, enqueue, get_card, start_timer,
)
from ama_rs.requests import _validate_recipient, settings_for
from ama_rs.shifts import route
from ama_rs.timing import effective

SUBMISSION_ROLES = {"service_account", "operator", "senior_operator"}


def _amount(amount: int, *, allow_zero: bool = False) -> None:
    if type(amount) is not int or not (0 if allow_zero else 1) <= amount <= 10**12:
        raise ValueError("Supply a valid amount in integer kopecks")


def request_refund(connection: Connection, enterprise_id: str, source_key: str,
                   brand: str, order_reference: str, reason: str, amount: int,
                   actor: Actor, now: datetime, *, positions: list[str] | None = None,
                   parent_id: str | None = None):
    settings = settings_for(connection, enterprise_id)
    if settings["contact_mode"] == "central":
        origin = operation_destination(settings)
        roles = {"operator", "senior_operator"}
    else:
        origin = route(settings, "enterprise")
        roles = {"service_account"}
    role = next((role for role in roles if has_role(settings, actor, origin, role, now)), None)
    if role is None:
        raise ProcessPermissionError("Refund entry does not match the enterprise service mode")
    _amount(amount)
    if brand not in settings["brands"] or not reason.strip():
        raise ValueError("Provide a configured brand and a reason")
    if positions is not None and (not positions or any(not position.strip() for position in positions)):
        raise ValueError("Specify the items involved in a partial refund")
    financial_chat = route(settings, "refunds")
    worker = {"chat": financial_chat, "role": "finance_executor"}
    _validate_recipient(settings, worker, {"finance_executor"}, now)
    management = route(settings, "management")
    requester = {"chat": origin, "role": role, "user": actor.user_id}
    card = create_card(connection, enterprise_id, "refund", source_key, worker,
        {"brand": brand, "order_reference": order_reference, "reason": reason,
         "requested_kopecks": amount, "positions": positions, "worker": worker, "requester": requester},
        now, parent_id=parent_id)
    enqueue(connection, card["id"], "financial-request", financial_chat, {"kind": "card"}, now)
    enqueue(connection, card["id"], "management-info", management, {"kind": "financial_status"}, now)
    if card["revision"] == 1:
        start_timer(connection, card["id"], "financial_deadline", now, timedelta(minutes=effective(settings, "refund.execution")[0]))
    return card


def _refund(connection: Connection, card_id: str, *, lock: bool = True):
    card = get_card(connection, card_id, lock=lock)
    if card["kind"] != "refund":
        raise ProcessConflict("Expected a refund request")
    return card


def take_refund(connection: Connection, card_id: str, command_id: str, revision: int,
                actor: Actor, now: datetime) -> int:
    card = _refund(connection, card_id)
    worker = card["data"]["worker"] | {"user": actor.user_id}
    return apply_action(connection, card_id, command_id, revision,
        Transition("take_refund", frozenset({"open"}), "checking", frozenset({"finance_executor"})),
        actor, worker["chat"], "finance_executor", now, updates={"worker": worker}, assignment=worker)


def reserve_refund(
    connection: Connection, card_id: str, command_id: str, revision: int,
    payment_reference: str, paid_kopecks: int, prior_external_refunds_kopecks: int,
    actor: Actor, now: datetime,
) -> int:
    """Record independent verification and reserve an amount before manual execution.

    The external amount covers operations absent from this enterprise's AMA RS
    ledger. Pending and uncertain reservations continue to consume the balance.
    """
    _amount(paid_kopecks)
    _amount(prior_external_refunds_kopecks, allow_zero=True)
    payment_reference = payment_reference.strip()
    if not payment_reference or len(payment_reference) > 128:
        raise ValueError("A verified payment reference is mandatory")
    unlocked = _refund(connection, card_id, lock=False)
    connection.execute(select(enterprises.c.id).where(
        enterprises.c.id == unlocked["enterprise_id"]).with_for_update()).scalar_one()
    card = _refund(connection, card_id)
    active = connection.execute(select(cards.c.data).where(
        cards.c.enterprise_id == card["enterprise_id"], cards.c.kind == "refund",
        cards.c.id != card_id, cards.c.status.in_(("reserved", "uncertain", "executed")),
    ).with_for_update()).scalars()
    reused = sum(item["requested_kopecks"] for item in active
                 if item.get("payment_reference") == payment_reference)
    if reused + prior_external_refunds_kopecks + card["data"]["requested_kopecks"] > paid_kopecks:
        raise ProcessConflict("The available payment balance does not cover this request")
    worker = card["data"]["worker"]
    return apply_action(connection, card_id, command_id, revision,
        Transition("reserve_refund", frozenset({"checking"}), "reserved", frozenset({"finance_executor"})),
        actor, worker["chat"], "finance_executor", now,
        updates={"payment_reference": payment_reference, "paid_kopecks": paid_kopecks,
                 "external_refunds_kopecks": prior_external_refunds_kopecks})


def record_refund_result(connection: Connection, card_id: str, command_id: str, revision: int,
                         outcome: str, reference: str, actor: Actor, now: datetime) -> int:
    if outcome not in {"executed", "not_required", "uncertain"} or not reference.strip():
        raise ValueError("Provide a documented financial outcome")
    card = _refund(connection, card_id)
    worker = card["data"]["worker"]
    allowed = {"reserved", "uncertain"} if outcome == "executed" else {"checking", "reserved", "uncertain"}
    if outcome == "uncertain":
        allowed = {"reserved"}
    new_revision = apply_action(connection, card_id, command_id, revision,
        Transition("record_refund_result", frozenset(allowed), outcome, frozenset({"finance_executor"})),
        actor, worker["chat"], "finance_executor", now, updates={"financial_reference": reference})
    if outcome != "uncertain":
        enqueue(connection, card_id, "financial-result", card["data"]["requester"]["chat"],
                {"kind": "financial_result", "outcome": outcome}, now)
    return new_revision


def ask_refund_question(connection: Connection, card_id: str, command_id: str, revision: int,
                        question: str, actor: Actor, now: datetime) -> int:
    card = _refund(connection, card_id)
    if not question.strip():
        raise ValueError("Provide a question")
    worker, requester = card["data"]["worker"], card["data"]["requester"]
    new_revision = apply_action(connection, card_id, command_id, revision,
        Transition("refund_question", frozenset({"checking"}), "clarification", frozenset({"finance_executor"})),
        actor, worker["chat"], "finance_executor", now,
        updates={"question": question}, assignment=requester)
    enqueue(connection, card_id, f"question:{new_revision}", requester["chat"], {"kind": "clarification"}, now)
    return new_revision


def answer_refund_question(connection: Connection, card_id: str, command_id: str, revision: int,
                           answer: str, actor: Actor, now: datetime) -> int:
    card = _refund(connection, card_id)
    if not answer.strip():
        raise ValueError("Provide an answer")
    worker, requester = card["data"]["worker"], card["data"]["requester"]
    new_revision = apply_action(connection, card_id, command_id, revision,
        Transition("refund_answer", frozenset({"clarification"}), "checking", frozenset(SUBMISSION_ROLES)),
        actor, requester["chat"], requester["role"], now, updates={"answer": answer}, assignment=worker)
    enqueue(connection, card_id, f"answer:{new_revision}", worker["chat"], {"kind": "clarification_answer"}, now)
    return new_revision
