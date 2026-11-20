"""Personnel matching, planned and actual working time, and onboarding."""

import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import (
    JSON, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, UniqueConstraint,
    insert, select, update,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, enterprises, has_role
from ama_rs.processes import ProcessConflict, ProcessPermissionError, utc_value
from ama_rs.requests import settings_for

metadata = MetaData()
people = Table(
    "personnel", metadata,
    Column("id", String(32), primary_key=True),
    Column("enterprise_id", String(64), ForeignKey(enterprises.c.id), nullable=False),
    Column("iiko_id", String(128), nullable=False),
    Column("name", String(160), nullable=False),
    Column("profile", String(40), nullable=False),
    Column("platform", String(16)),
    Column("user_id", String(128)),
    Column("invite_hash", String(64), unique=True),
    Column("invite_expiry", DateTime),
    Column("state", String(20), nullable=False),
    Column("training", JSON, nullable=False),
    Column("revision", Integer, nullable=False),
    UniqueConstraint("enterprise_id", "iiko_id", name="uq_person_iiko"),
    UniqueConstraint("enterprise_id", "platform", "user_id", name="uq_person_chat_user"),
)
work_time = Table(
    "personnel_work_time", metadata,
    Column("person_id", String(32), ForeignKey(people.c.id), primary_key=True),
    Column("kind", String(16), primary_key=True),
    Column("source_key", String(128), primary_key=True),
    Column("observed_at", DateTime, nullable=False),
    Column("starts_at", DateTime, nullable=False),
    Column("ends_at", DateTime),
)
training_history = Table(
    "personnel_training_history", metadata,
    Column("person_id", String(32), ForeignKey(people.c.id), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("actor", String(128), nullable=False),
    Column("event", JSON, nullable=False),
    Column("recorded_at", DateTime, nullable=False),
)

PROFILES = {
    "kitchen": ("safety", "production", "sanitation"),
    "front": ("safety", "guest_service", "cash_rules"),
    "courier": ("safety", "handoff", "delivery"),
    "shift_lead": ("safety", "shift", "problems"),
    "manager": ("roles", "shifts", "audit", "staff", "escalations"),
}


def synchronize_person(connection: Connection, enterprise_id: str, iiko_id: str,
                       name: str, profile: str):
    """Ingest a known enterprise's personnel directory entry by stable iiko identity."""
    if not iiko_id.strip() or not name.strip() or profile not in PROFILES:
        raise ValueError("Invalid iiko person record")
    stmt = mysql_insert(people).values(id=uuid4().hex, enterprise_id=enterprise_id,
        iiko_id=iiko_id, name=name, profile=profile, training={}, state="unlinked", revision=1)
    connection.execute(stmt.on_duplicate_key_update(name=stmt.inserted.name))
    return connection.execute(select(people).where(
        people.c.enterprise_id == enterprise_id, people.c.iiko_id == iiko_id
    ).with_for_update()).mappings().one()


def _person(connection: Connection, person_id: str, *, lock: bool = False):
    query = select(people).where(people.c.id == person_id)
    if lock:
        query = query.with_for_update()
    return connection.execute(query).mappings().one()


def _manager(connection: Connection, person, actor: Actor, now: datetime) -> None:
    settings = settings_for(connection, person["enterprise_id"])
    role = "branch_lead" if person["profile"] == "manager" else "manager"
    if not any(has_role(settings, actor, key, role, now) for key in settings["chats"]):
        raise ProcessPermissionError("Personnel record is outside the actor's scope")


def _record(connection: Connection, person, actor: Actor, now: datetime, event: dict, **values) -> int:
    revision = person["revision"] + 1
    connection.execute(update(people).where(people.c.id == person["id"])
                       .values(revision=revision, **values))
    connection.execute(insert(training_history).values(
        person_id=person["id"], revision=revision, actor=f"{actor.platform}:{actor.user_id}",
        event=event, recorded_at=utc_value(now),
    ))
    return revision


def issue_invite(connection: Connection, person_id: str, actor: Actor, now: datetime) -> str:
    person = _person(connection, person_id, lock=True)
    _manager(connection, person, actor, now)
    if person["state"] != "unlinked":
        raise ProcessConflict("Account is already linked or awaiting confirmation")
    token = secrets.token_urlsafe(24)
    _record(connection, person, actor, now, {"kind": "invited"},
            invite_hash=hashlib.sha256(token.encode()).hexdigest(),
            invite_expiry=utc_value(now + timedelta(days=1)))
    return token


def claim_invite(connection: Connection, token: str, actor: Actor, now: datetime) -> str:
    if actor.platform not in {"telegram", "max"} or actor.chat_id != actor.user_id:
        raise ProcessPermissionError("Use a verified private account to link personnel")
    digest = hashlib.sha256(token.encode()).hexdigest()
    person = connection.execute(select(people).where(
        people.c.invite_hash == digest).with_for_update()).mappings().one_or_none()
    if person is None or person["invite_expiry"] <= utc_value(now) or person["state"] != "unlinked":
        raise ProcessConflict("Invitation is invalid or expired")
    _record(connection, person, actor, now, {"kind": "claimed"}, platform=actor.platform,
            user_id=actor.user_id, state="pending", invite_hash=None, invite_expiry=None)
    return person["id"]


def confirm_link(connection: Connection, person_id: str, revision: int, approve: bool,
                 actor: Actor, now: datetime) -> int:
    person = _person(connection, person_id, lock=True)
    _manager(connection, person, actor, now)
    if person["revision"] != revision or person["state"] != "pending":
        raise ProcessConflict("Personnel link has changed")
    if (person["platform"], person["user_id"]) == (actor.platform, actor.user_id):
        raise ProcessPermissionError("Self-confirmation is forbidden")
    values = {"state": "active"} if approve else {"state": "unlinked", "platform": None, "user_id": None}
    return _record(connection, person, actor, now, {"kind": "link_review", "approved": approve}, **values)


def record_work_time(connection: Connection, person_id: str, kind: str, source_key: str,
                     starts_at: datetime, ends_at: datetime | None, observed_at: datetime) -> bool:
    if kind not in {"planned", "actual"} or not source_key.strip():
        raise ValueError("Work time requires a kind and source ID")
    start = utc_value(starts_at)
    end = utc_value(ends_at) if ends_at else None
    seen = utc_value(observed_at)
    if end is not None and end <= start:
        raise ValueError("Work interval must have positive duration")
    _person(connection, person_id, lock=True)
    where = (work_time.c.person_id == person_id, work_time.c.kind == kind, work_time.c.source_key == source_key)
    previous = connection.execute(select(work_time.c.observed_at).where(*where).with_for_update()).scalar_one_or_none()
    if previous is not None and previous >= seen:
        return False
    values = {"starts_at": start, "ends_at": end, "observed_at": seen}
    if previous is None:
        connection.execute(insert(work_time).values(person_id=person_id, kind=kind, source_key=source_key, **values))
    else:
        connection.execute(update(work_time).where(*where).values(**values))
    return True


def personal_schedule(connection: Connection, person_id: str, actor: Actor):
    person = _person(connection, person_id)
    _check_self(person, actor)
    return connection.execute(select(work_time).where(work_time.c.person_id == person_id)
                              .order_by(work_time.c.starts_at, work_time.c.kind)).mappings().all()


def _check_self(person, actor: Actor) -> None:
    if person["state"] != "active" or actor.chat_id != actor.user_id or (
        person["platform"], person["user_id"]
    ) != (actor.platform, actor.user_id):
        raise ProcessPermissionError("Personal data belongs to another account")


def acknowledge_training(connection: Connection, person_id: str, module: str,
                         actor: Actor, now: datetime) -> int:
    person = _person(connection, person_id, lock=True)
    _check_self(person, actor)
    modules = PROFILES[person["profile"]]
    if module not in modules:
        raise ValueError("Unknown training module")
    training = person["training"]
    seen = training.get("acknowledged", [])
    if module in seen:
        return person["revision"]
    if module != modules[len(seen)]:
        raise ProcessConflict("Complete the previous module first")
    return _record(connection, person, actor, now, {"kind": "acknowledged", "module": module},
                   training=training | {"acknowledged": [*seen, module]})


def record_skill_check(connection: Connection, person_id: str, actor: Actor, now: datetime,
                       result: str, note: str) -> int:
    person = _person(connection, person_id, lock=True)
    _manager(connection, person, actor, now)
    if not note.strip() or result not in {"approved", "restricted", "retraining"}:
        raise ValueError("Record the in-person decision and its rationale")
    if person["state"] != "active" or set(person["training"].get("acknowledged", [])) != set(PROFILES[person["profile"]]):
        raise ProcessConflict("Complete introductory training before the skill check")
    event = {"kind": "skill_check", "result": result, "note": note}
    return _record(connection, person, actor, now, event, training=person["training"] | {"decision": event})
