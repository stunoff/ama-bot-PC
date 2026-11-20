"""Explanations and management review for existing iiko sales documents."""

from datetime import datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, get_enterprise
from ama_rs.processes import ProcessConflict, Transition, apply_action, attach, attachments, enqueue, get_card
from ama_rs.requests import make_task
from ama_rs.shifts import route

SALES_DOCUMENTS = {"sales.writeoff", "sales.return"}
WRITEOFF_REASONS = ("Брак при приготовлении", "Истёк срок хранения", "Порча продукта",
                    "Нарушение хранения / разморозка", "Производственная ошибка")
RETURN_COMMON = ("Ошибка в заказе", "Ошибка оплаты", "Отказ гостя", "Отказ гостя — долгое ожидание")
RETURN_REASONS = {
    "money": (*RETURN_COMMON, "Дублирование оплаты / заказа"),
    "food": (*RETURN_COMMON, "Не устроило качество блюда", "Не тот состав / комплектация"),
}


def _sale(connection: Connection, card_id: str):
    card = get_card(connection, card_id, lock=True)
    if card["kind"] not in SALES_DOCUMENTS:
        raise ProcessConflict("Expected a write-off or iiko return")
    return card, get_enterprise(connection, card["enterprise_id"])["settings"]


def _submit(connection, card, command_id, revision, name, states, status, actor, chat, role,
            now, *, updates, assignment=None):
    return apply_action(connection, card["id"], command_id, revision,
        Transition(name, frozenset(states), status, frozenset({role})),
        actor, chat, role, now, updates=updates, assignment=assignment)


def attach_sale_photo(connection: Connection, card_id: str, command_id: str, file_id: str,
                      actor: Actor, now: datetime) -> None:
    card, settings = _sale(connection, card_id)
    if card["status"] not in {"open", "clarification"}:
        raise ProcessConflict("This operation is not accepting evidence")
    round_id = card["data"].get("sales_round", "initial")
    attach(connection, card_id, command_id, f"evidence:{round_id}", file_id,
           actor, route(settings, "enterprise"), "service_account", now, {})
    enqueue(connection, card_id, f"sales-photo:{command_id}", route(settings, "management"),
            {"kind": "attachment", "command_id": command_id}, now)


def submit_sale_explanation(connection: Connection, card_id: str, command_id: str, revision: int,
                            reason: str, comment: str, actor: Actor, now: datetime, *,
                            return_type: str = "", photo: str = "") -> int:
    card, settings = _sale(connection, card_id)
    if card["kind"] == "sales.return":
        if return_type not in RETURN_REASONS or not comment.strip():
            raise ValueError("Select a return type and provide a comment")
        allowed = RETURN_REASONS[return_type]
    else:
        allowed = settings.get("writeoff_reasons", WRITEOFF_REASONS)
        if return_type:
            raise ValueError("A write-off has no return type")
    if reason not in allowed:
        raise ValueError("Select a configured reason")
    required = card["kind"] == "sales.writeoff" or return_type == "food"
    if required:
        if photo not in {"attached", "unavailable"}:
            raise ValueError("Declare whether photo evidence is attached or unavailable")
        if photo == "attached" and connection.scalar(select(attachments.c.command_id).where(
            attachments.c.card_id == card_id,
            attachments.c.item == f"evidence:{card['data'].get('sales_round', 'initial')}"
        ).limit(1)) is None:
            raise ProcessConflict("No photo evidence for this request")
    manager = route(settings, "management")
    updated = _submit(connection, card, command_id, revision, "sales_explanation",
        {"open", "clarification"}, "review", actor, route(settings, "enterprise"), "service_account",
        now, updates={"sale_explanation": {"reason": reason, "comment": comment,
            "return_type": return_type, "missing_photo": required and photo == "unavailable"}},
        assignment={"chat": manager, "role": "manager"})
    enqueue(connection, card_id, f"sales-review:{updated}", manager,
            {"kind": "sales_review", "card_revision": updated}, now)
    return updated


def ask_sale_question(connection: Connection, card_id: str, command_id: str, revision: int,
                      question: str, actor: Actor, now: datetime) -> int:
    card, settings = _sale(connection, card_id)
    enterprise = route(settings, "enterprise")
    if not question.strip():
        raise ValueError("Provide an additional question")
    updated = _submit(connection, card, command_id, revision, "sales_question", {"review"}, "clarification",
        actor, route(settings, "management"), "manager", now,
        updates={"sales_question": question, "sales_round": command_id},
        assignment={"chat": enterprise, "role": "service_account"})
    enqueue(connection, card_id, f"sales-question:{updated}", enterprise,
            {"kind": "sales_explanation", "card_revision": updated, "question": question}, now)
    return updated


def accept_sale_explanation(connection: Connection, card_id: str, command_id: str, revision: int,
                            actor: Actor, now: datetime) -> int:
    card, settings = _sale(connection, card_id)
    task_id = card["data"].get("linked_sale_task")
    if card["status"] == "task" and (not task_id or get_card(connection, task_id)["status"] != "closed"):
        raise ProcessConflict("Accept the linked task before closing the sales review")
    return _submit(connection, card, command_id, revision, "sales_accept", {"review", "task"}, "closed",
                   actor, route(settings, "management"), "manager", now, updates={"reviewed": True})


def assign_sale_task(connection: Connection, card_id: str, command_id: str, revision: int,
                     title: str, expected_result: str, recipient: dict, due_at: datetime,
                     actor: Actor, now: datetime) -> str:
    card, settings = _sale(connection, card_id)
    manager = route(settings, "management")
    if card["status"] != "review" or card["revision"] != revision:
        raise ProcessConflict("Only a current sales review can be assigned to a task")
    source_key = sha256(f"sales:{card_id}:{command_id}".encode()).hexdigest()
    task = make_task(connection, card["enterprise_id"], source_key, title,
                     expected_result, due_at, recipient, {"chat": manager, "role": "manager"},
                     actor, now, parent_id=card_id)
    _submit(connection, card, command_id, revision, "sales_assign_task", {"review"}, "task",
            actor, manager, "manager", now, updates={"linked_sale_task": task["id"]})
    return task["id"]
