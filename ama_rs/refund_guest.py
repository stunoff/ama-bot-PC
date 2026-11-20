"""Independent follow-up after a standalone online refund."""

from datetime import datetime

from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, get_enterprise
from ama_rs.processes import (
    ProcessConflict, Transition, apply_action, create_card, enqueue, get_card, start_timer, utc_value,
)

KIND = "refund_guest"
GUEST_CLOCKS = ("refund_guest_reminder", "refund_guest_escalation", "refund_guest_next")


def publish_result(connection: Connection, refund, outcome: str, now: datetime) -> None:
    requester = refund["data"]["requester"]
    if refund["parent_id"]:
        enqueue(connection, refund["id"], "financial-result", requester["chat"],
                {"kind": "financial_result", "outcome": outcome, "source_card": refund["parent_id"]}, now)
        return
    central = requester["role"] in {"operator", "senior_operator"}
    card = create_card(connection, refund["enterprise_id"], KIND, refund["id"], requester,
        {"outcome": outcome, "mode": "central" if central else "independent"},
        now, status="working", parent_id=refund["id"])
    if card["revision"] == 1:
        enqueue(connection, card["id"], "notify-guest", requester["chat"],
                {"kind": KIND}, now)


def record_contact(connection: Connection, card_id: str, command_id: str, revision: int,
                   outcome: str, note: str, actor: Actor, now: datetime, *,
                   next_action: str = "", next_due: datetime | None = None) -> int:
    card = get_card(connection, card_id, lock=True)
    if card["kind"] != KIND or outcome not in {"informed", "unreachable", "no_channel"}:
        raise ValueError("Record the guest contact result")
    if outcome != "informed" and (
        not note.strip() or not next_action.strip() or next_due is None or utc_value(next_due) <= utc_value(now)
    ):
        raise ProcessConflict("Provide the reason, next action and its future deadline")
    assigned = card["assignment"]
    payload = {"guest_contact": outcome, "note": note}
    if outcome != "informed":
        payload |= {"next_action": next_action, "next_due": next_due.isoformat()}
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("refund_guest_contact", frozenset({"working"}), "closed" if outcome == "informed" else None,
                   frozenset({assigned["role"]})),
        actor, assigned["chat"], assigned["role"], now, updates=payload)
    if outcome != "informed" and updated > card["revision"]:
        start_timer(connection, card_id, "refund_guest_next", now, next_due - now)
    return updated


def escalation_chat(connection: Connection, card) -> str | None:
    settings = get_enterprise(connection, card["enterprise_id"])["settings"]
    kind = "contact_management" if card["data"]["mode"] == "central" else "management"
    matches = [key for key, chat in settings["chats"].items() if chat["kind"] == kind]
    return matches[0] if len(matches) == 1 else None
