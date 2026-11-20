"""Attendance observations based on confirmed iiko plans and actual intervals."""

from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, enterprises, get_enterprise, has_role
from ama_rs.personnel import people, work_time
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, advance_system_card, apply_action,
    cards, create_card, enqueue, get_card, utc_value,
)
from ama_rs.reports import coverage

REASONS = {"Транспорт", "Семейные обстоятельства", "Плохое самочувствие", "Ошибка графика", "Другое"}


def _chat(settings, kind):
    matches = [key for key, chat in settings["chats"].items() if chat["kind"] == kind]
    return matches[0] if len(matches) == 1 else None


def _overlaps(plan, actual, end):
    return actual["starts_at"] < plan["ends_at"] and (actual["ends_at"] or end) > plan["starts_at"]


def _observations(plan, plans, actuals, verified, instant):
    start, end = plan["starts_at"], plan["ends_at"]
    matching = [row for row in actuals if _overlaps(plan, row, instant)]
    if any(sum(_overlaps(other, actual, instant) for other in plans) != 1 for actual in matching):
        return {}
    if not matching:
        due = start + timedelta(minutes=5)
        return {"no_arrival": (start, None)} if not actuals and verified and verified >= due and instant >= due else {}
    found = {}
    first = min(row["starts_at"] for row in matching)
    if first > start:
        found["late"] = (start, first)
    if any(row["ends_at"] is None for row in matching):
        due = end + timedelta(minutes=15)
        if verified and verified >= due and instant >= due:
            found["unclosed"] = (end, None)
    else:
        last = max(row["ends_at"] for row in matching)
        if verified and verified >= end and instant >= end and last < end:
            found["early_leave"] = (end, last)
    return found


def scan_attendance(connection: Connection, now: datetime, *, enterprise_id: str | None = None) -> None:
    instant = utc_value(now)
    query = select(enterprises)
    if enterprise_id is not None:
        query = query.where(enterprises.c.id == enterprise_id)
    for entry in connection.execute(query).mappings().all():
        settings = entry["settings"]
        management = _chat(settings, "management")
        if not settings.get("attendance_monitoring") or not management:
            continue
        zone = ZoneInfo(settings["timezone"])
        local_day = now.astimezone(zone).date()
        since = utc_value(datetime.combine(local_day - timedelta(days=2), time.min, zone))
        until = utc_value(datetime.combine(local_day + timedelta(days=1), time.min, zone))
        confirmed = {(row.source, row.day): row.updated_at for row in connection.execute(
            select(coverage).where(coverage.c.enterprise_id == entry["id"],
            coverage.c.day >= (local_day - timedelta(days=2)).isoformat(),
            coverage.c.state == "complete"))}
        records = connection.execute(select(work_time, people.c.name).join(people).where(
            people.c.enterprise_id == entry["id"], people.c.state == "active",
            work_time.c.starts_at < until, work_time.c.starts_at >= since - timedelta(days=1)
        )).mappings().all()
        grouped = {}
        for record in records:
            grouped.setdefault(record["person_id"], []).append(record)
        for person_id, events in grouped.items():
            plans = [row for row in events if row["kind"] == "planned" and row["ends_at"]]
            actuals = [row for row in events if row["kind"] == "actual"]
            for plan in plans:
                start = plan["starts_at"]
                if not since <= start <= instant:
                    continue
                day = start.replace(tzinfo=UTC).astimezone(zone).date().isoformat()
                schedule = confirmed.get(("schedule", day))
                # Match intervals only inside a confirmed schedule. Do not infer absence
                # from an unrelated or partially uploaded attendance interval.
                if not schedule:
                    continue
                candidates = [row for row in actuals if (
                    row["starts_at"].replace(tzinfo=UTC).astimezone(zone).date().isoformat() == day
                    or _overlaps(plan, row, instant)
                )]
                end_day = plan["ends_at"].replace(tzinfo=UTC).astimezone(zone).date().isoformat()
                verified = confirmed.get(("attendance", day))
                if end_day != day:
                    second = confirmed.get(("attendance", end_day))
                    verified = max(verified, second) if second and verified else None
                found = _observations(plan, plans, candidates, verified, instant)
                _sync(connection, entry["id"], management, person_id, plan, found, now,
                      complete=verified is not None, has_fact=bool(candidates))


def _sync(connection, enterprise_id, management, person_id, plan, found, now, *, complete, has_fact):
    prefix = sha256(f"{person_id}:{plan['source_key']}".encode()).hexdigest()
    for kind in ("no_arrival", "late", "unclosed", "early_leave"):
        key = f"{prefix}:{kind}"
        existing = connection.execute(select(cards).where(
            cards.c.enterprise_id == enterprise_id, cards.c.kind == "attendance", cards.c.source_key == key,
        ).with_for_update()).mappings().one_or_none()
        if kind not in found:
            if not (complete or (kind == "no_arrival" and has_fact)):
                continue
            if existing is not None and existing["status"] != "closed":
                advance_system_card(connection, existing, "closed", "attendance_reconciled", now,
                                    data=existing["data"] | {"reconciled": True})
            continue
        planned, actual = found[kind]
        observation = {"person_id": person_id, "employee": plan["name"], "deviation": kind,
                       "planned_at": planned.replace(tzinfo=UTC).isoformat(),
                       "actual_at": actual.replace(tzinfo=UTC).isoformat() if actual else None,
                       "delta_minutes": abs(int((actual - planned).total_seconds() / 60)) if actual else None}
        if existing is not None:
            if any(existing["data"].get(key) != value for key, value in observation.items()):
                advance_system_card(connection, existing, existing["status"], "attendance_updated", now,
                                    data=existing["data"] | observation)
            continue
        card = create_card(connection, enterprise_id, "attendance", key,
                           {"chat": management, "role": "manager"}, observation, now)
        enqueue(connection, card["id"], "attendance", management, {"kind": "attendance", "card_revision": card["revision"]}, now)


def request_explanation(connection: Connection, card_id: str, command_id: str, revision: int,
                        question: str, actor: Actor, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    if card["kind"] != "attendance" or not question.strip():
        raise ValueError("Provide an attendance question")
    person = connection.execute(select(people).where(people.c.id == card["data"]["person_id"])).mappings().one()
    settings = get_enterprise(connection, card["enterprise_id"])["settings"]
    if person["state"] != "active":
        raise ProcessConflict("The employee has no confirmed private account")
    recipient = Actor(person["platform"], person["user_id"], person["user_id"])
    targets = [key for key, chat in settings["chats"].items() if chat["kind"] == "personal"
               and has_role(settings, recipient, key, "employee", now)]
    if len(targets) != 1:
        raise ProcessConflict("A single verified personal chat is required")
    management = _chat(settings, "management")
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("attendance_question", frozenset({"open"}), "clarification", frozenset({"manager"})),
        actor, management, "manager", now, updates={"question": question},
        assignment={"chat": targets[0], "role": "employee", "user": person["user_id"]})
    enqueue(connection, card_id, f"attendance-question:{updated}", targets[0],
            {"kind": "attendance_question", "question": question, "card_revision": updated}, now)
    return updated


def answer_explanation(connection: Connection, card_id: str, command_id: str, revision: int,
                       reason: str, comment: str, actor: Actor, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    if card["kind"] != "attendance" or reason not in REASONS or (reason == "Другое" and not comment.strip()):
        raise ValueError("Choose a reason and explain an unlisted reason")
    person = connection.execute(select(people).where(people.c.id == card["data"]["person_id"])).mappings().one()
    if (actor.user_id != actor.chat_id or person["state"] != "active"
            or (person["platform"], person["user_id"]) != (actor.platform, actor.user_id)):
        raise ProcessPermissionError("Use your confirmed personal chat")
    settings = get_enterprise(connection, card["enterprise_id"])["settings"]
    management = _chat(settings, "management")
    private = next((key for key, chat in settings["chats"].items()
                    if chat["kind"] == "personal" and has_role(settings, actor, key, "employee", now)), None)
    updated = apply_action(connection, card_id, command_id, revision,
        Transition("attendance_answer", frozenset({"clarification"}), "review", frozenset({"employee"})),
        actor, private, "employee", now, updates={"reason": reason, "comment": comment},
        assignment={"chat": management, "role": "manager"})
    enqueue(connection, card_id, f"attendance-answer:{updated}", management,
            {"kind": "attendance_answer", "card_revision": updated}, now)
    return updated


def acknowledge_deviation(connection: Connection, card_id: str, command_id: str, revision: int,
                          note: str, actor: Actor, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    if card["kind"] != "attendance" or not note.strip():
        raise ValueError("Record the known reason or review decision")
    management = _chat(get_enterprise(connection, card["enterprise_id"])["settings"], "management")
    return apply_action(connection, card_id, command_id, revision,
        Transition("attendance_close", frozenset({"open", "review"}), "closed", frozenset({"manager"})),
        actor, management, "manager", now, updates={"review_note": note})
