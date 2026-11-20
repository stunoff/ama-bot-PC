"""Route a cash discrepancy to management while keeping one cash shift."""

from datetime import datetime

from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, get_enterprise
from ama_rs.processes import ProcessConflict, Transition, apply_action, create_card, enqueue, get_card
from ama_rs.shifts import route


def refer_discrepancy(connection: Connection, cash_id: str, command_id: str, revision: int,
                      amount_kopecks: int, comment: str, actor: Actor, now: datetime) -> str:
    cash = get_card(connection, cash_id, lock=True)
    if cash["kind"] != "cash_shift" or type(amount_kopecks) is not int or not amount_kopecks or not comment.strip():
        raise ValueError("Provide a nonzero discrepancy amount and explanation")
    target = route(get_enterprise(connection, cash["enterprise_id"])["settings"], "management")
    apply_action(connection, cash_id, command_id, revision,
        Transition("refer_cash_discrepancy", frozenset({"review"}), "discrepancy",
                   frozenset({"cash_reviewer"})),
        actor, cash["data"]["cash_chat"], "cash_reviewer", now,
        updates={"discrepancy": comment, "discrepancy_amount": amount_kopecks})
    child = create_card(connection, cash["enterprise_id"], "cash_discrepancy", f"{cash_id}:{command_id}",
        {"chat": target, "role": "manager"}, {"note": comment, "amount_kopecks": amount_kopecks},
        now, parent_id=cash_id)
    enqueue(connection, child["id"], "cash-discrepancy", target, {"kind": "cash_discrepancy"}, now)
    return child["id"]


def explain_discrepancy(connection: Connection, card_id: str, command_id: str, revision: int,
                        explanation: str, actor: Actor, now: datetime) -> int:
    child = get_card(connection, card_id)
    if child["kind"] != "cash_discrepancy" or not explanation.strip():
        raise ValueError("Explain the discrepancy")
    cash = get_card(connection, child["parent_id"], lock=True)
    if cash["status"] != "discrepancy":
        raise ProcessConflict("Cash discrepancy is no longer active")
    child = get_card(connection, card_id, lock=True)
    target = child["assignment"]
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("explain_discrepancy", frozenset({"open"}), "closed", frozenset({"manager"})),
        actor, target["chat"], "manager", now, updates={"explanation": explanation})
    apply_action(connection, cash["id"], f"explain:{child['id']}", cash["revision"],
        Transition("explain_cash_discrepancy", frozenset({"discrepancy"}), "review",
                   frozenset({"manager"}), assignee_only=False),
        actor, target["chat"], "manager", now, updates={"manager_explanation": explanation})
    enqueue(connection, cash["id"], f"discrepancy-answer:{child['id']}", cash["data"]["cash_chat"],
            {"kind": "cash_review", "answer": explanation}, now)
    return updated
