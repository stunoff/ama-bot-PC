"""Convert expired workflow timers into deduplicated delivery obligations."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from ama_rs.enterprises import get_enterprise
from ama_rs.processes import cards, deliveries, elapse_timer, enqueue, get_card, timers, utc_value

TIMER_RANK = {"acceptance": 1, "reminder": 2, "breach": 3, "next_action": 4}


def _timer_target(card, key: str, settings: dict) -> str | None:
    if key == "acceptance" and card["kind"] == "technical" and not card["data"].get("night"):
        return None
    if key == "breach" and card["kind"] == "technical" and not card["data"].get("night"):
        matches = [name for name, chat in settings["chats"].items() if chat["kind"] == "technical_management"]
        return matches[0] if len(matches) == 1 else card["assignment"]["chat"]
    return card["assignment"]["chat"]


def queue_due_reminders(connection: Connection, now: datetime) -> None:
    due = connection.execute(select(timers.c.card_id, timers.c.key).join(cards).where(
        timers.c.elapsed_at.is_(None), timers.c.due_at <= utc_value(now),
        timers.c.card_revision == cards.c.revision,
        cards.c.status.not_in(("closed", "accepted", "executed", "not_required")),
    ).order_by(timers.c.due_at, timers.c.card_id, timers.c.key).limit(100)).all()
    grouped = {}
    for card_id, key in due:
        grouped.setdefault(card_id, []).append(key)
    for card_id, keys in grouped.items():
        card = get_card(connection, card_id, lock=True)
        if card["status"] in {"closed", "accepted", "executed", "not_required"}:
            continue
        entry = get_enterprise(connection, card["enterprise_id"])
        selected = sorted(keys, key=lambda key: TIMER_RANK.get(key, 0), reverse=True)
        emitted = set()
        for key in selected:
            if not elapse_timer(connection, card_id, key, now):
                continue
            target = _timer_target(card, key, entry["settings"])
            if not target or target in emitted or target not in entry["settings"]["chats"]:
                continue
            connection.execute(update(deliveries).where(
                deliveries.c.card_id == card_id, deliveries.c.chat_key == target,
                deliveries.c.signal.startswith("timer:"), deliveries.c.state == "pending",
            ).values(state="superseded"))
            enqueue(connection, card_id, f"timer:{card['revision']}:{key}", target,
                    {"kind": "reminder", "timer": key, "card_revision": card["revision"]}, now)
            emitted.add(target)
