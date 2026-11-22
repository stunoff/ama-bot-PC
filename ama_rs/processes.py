"""Transactional process cards, action receipts, timers, and delivery records."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import (
    JSON, Column, DateTime, ForeignKey, Index, Integer, MetaData, String, Table,
    UniqueConstraint, insert, select, update,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import Connection

from ama_rs.guest_contact import pending_first_calls, pending_guest_orders
from ama_rs.enterprises import Actor, chat_is_open, enterprises, has_role, refund_delivery_close_hour

metadata = MetaData()

cards = Table(
    "process_cards", metadata,
    Column("id", String(32), primary_key=True),
    Column("enterprise_id", String(64), ForeignKey(enterprises.c.id), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("source_key", String(128), nullable=False),
    Column("status", String(40), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("assignment", JSON, nullable=False),
    Column("data", JSON, nullable=False),
    Column("parent_id", String(32), ForeignKey("process_cards.id")),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("enterprise_id", "kind", "source_key", name="uq_card_source"),
)
actions = Table(
    "process_actions", metadata,
    Column("card_id", String(32), ForeignKey(cards.c.id), primary_key=True),
    Column("command_id", String(128), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("action", String(48), nullable=False),
    Column("actor", JSON, nullable=False),
    Column("input", JSON, nullable=False),
    Column("previous_status", String(40)),
    Column("status", String(40), nullable=False),
    Column("recorded_at", DateTime, nullable=False),
)
attachments = Table(
    "process_attachments", metadata,
    Column("card_id", String(32), ForeignKey(cards.c.id), primary_key=True),
    Column("command_id", String(128), primary_key=True),
    Column("item", String(64), nullable=False),
    Column("platform", String(16), nullable=False),
    Column("file_id", String(512), nullable=False),
    Column("metadata", JSON, nullable=False),
    Column("received_at", DateTime, nullable=False),
)
deliveries = Table(
    "process_deliveries", metadata,
    Column("id", String(32), primary_key=True),
    Column("card_id", String(32), ForeignKey(cards.c.id), nullable=False),
    Column("signal", String(128), nullable=False),
    Column("chat_key", String(100), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("state", String(12), nullable=False),
    Column("available_at", DateTime, nullable=False),
    Column("attempt_id", String(32)),
    Column("external_id", String(128)),
    Index("ix_delivery_due", "state", "available_at"),
    UniqueConstraint("card_id", "signal", "chat_key", name="uq_delivery_signal"),
)
timers = Table(
    "process_timers", metadata,
    Column("card_id", String(32), ForeignKey(cards.c.id), primary_key=True),
    Column("key", String(48), primary_key=True),
    Column("card_revision", Integer, nullable=False),
    Column("due_at", DateTime, nullable=False),
    Column("elapsed_at", DateTime),
    Index("ix_timer_due", "elapsed_at", "due_at"),
)


class ProcessConflict(ValueError):
    pass


class ProcessPermissionError(PermissionError):
    pass


@dataclass(frozen=True)
class Transition:
    name: str
    from_states: frozenset[str]
    to_state: str | None
    roles: frozenset[str]
    assignee_only: bool = True


def utc_value(instant: datetime) -> datetime:
    if instant.utcoffset() is None:
        raise ValueError("Expected a timezone-aware instant")
    return instant.astimezone(UTC).replace(tzinfo=None)


def _settings(connection: Connection, enterprise_id: str) -> dict:
    return connection.execute(select(enterprises.c.settings)
        .where(enterprises.c.id == enterprise_id).with_for_update(read=True)).scalar_one()


def get_card(connection: Connection, card_id: str, *, lock: bool = False):
    query = select(cards).where(cards.c.id == card_id)
    if lock:
        query = query.with_for_update()
    return connection.execute(query).mappings().one()


def create_card(
    connection: Connection, enterprise_id: str, kind: str, source_key: str,
    assignment: dict, data: dict, now: datetime, *, status: str = "open",
    parent_id: str | None = None,
):
    settings = _settings(connection, enterprise_id)
    if assignment["chat"] not in settings["chats"]:
        raise ProcessConflict("Assignment route is not configured")
    if parent_id is not None and get_card(connection, parent_id)["enterprise_id"] != enterprise_id:
        raise ProcessConflict("Related cards must belong to the same enterprise")
    statement = mysql_insert(cards).values(
        id=uuid4().hex, enterprise_id=enterprise_id, kind=kind, source_key=source_key,
        assignment=assignment, data=data, created_at=utc_value(now), status=status,
        parent_id=parent_id, revision=1,
    )
    connection.execute(statement.on_duplicate_key_update(id=cards.c.id))
    return connection.execute(select(cards).where(
        cards.c.enterprise_id == enterprise_id, cards.c.kind == kind, cards.c.source_key == source_key,
    ).with_for_update()).mappings().one()


def advance_system_card(connection: Connection, control, status: str,
                   action: str, now: datetime, **fields) -> int:
    revision = control["revision"] + 1
    connection.execute(update(cards).where(cards.c.id == control["id"]).values(
        revision=revision, status=status, **fields,
    ))
    connection.execute(insert(actions).values(
        card_id=control["id"], command_id=f"{action}:{revision}", revision=revision,
        action=action, actor={"source": "workflow"}, input={}, previous_status=control["status"],
        status=status, recorded_at=utc_value(now),
    ))
    return revision


def apply_action(
    connection: Connection, card_id: str, command_id: str, expected_revision: int,
    transition: Transition, actor: Actor, chat_key: str, role: str, now: datetime,
    *, updates: dict | None = None, assignment: dict | None = None,
) -> int:
    current = get_card(connection, card_id, lock=True)
    settings = _settings(connection, current["enterprise_id"])
    if role not in transition.roles or not has_role(settings, actor, chat_key, role, now):
        raise ProcessPermissionError("This actor cannot perform this action")
    identity = {"platform": actor.platform, "user": actor.user_id, "chat": chat_key,
                "topic": actor.topic_id, "role": role}
    payload = {"updates": updates, "assignment": assignment}
    receipt = connection.execute(select(actions).where(
        actions.c.card_id == card_id, actions.c.command_id == command_id
    ).with_for_update()).mappings().one_or_none()
    if receipt is not None:
        if (receipt["actor"], receipt["action"], receipt["input"]) != (identity, transition.name, payload):
            raise ProcessConflict("Command already used for a different action")
        return receipt["revision"]
    if current["revision"] != expected_revision or current["status"] not in transition.from_states:
        raise ProcessConflict("Card changed; reload the current version")
    assigned_to = current["assignment"]
    if transition.assignee_only and (
        assigned_to["chat"] != chat_key or assigned_to["role"] != role
        or assigned_to.get("user") not in (None, actor.user_id)
    ):
        raise ProcessPermissionError("Work is assigned to another recipient")
    if assignment is not None and assignment["chat"] not in settings["chats"]:
        raise ProcessConflict("Next assignment route is not configured")
    revision = current["revision"] + 1
    status = transition.to_state or current["status"]
    connection.execute(update(cards).where(cards.c.id == card_id).values(
        status=status, revision=revision, assignment=assignment or assigned_to,
        data=current["data"] | (updates or {}),
    ))
    clock_keys = None
    if current["kind"] == "refund" and status not in {"executed", "not_required", "closed"}:
        clock_keys = ("refund_reminder_1", "refund_reminder_2", "refund_reminder_3",
                      "financial_deadline", "refund_followup")
    elif current["kind"] == "task" and current["status"] in {"open", "working"} and status == "working":
        clock_keys = ("execution", "execution_warning", "execution_escalation")
    elif (current["kind"] == "technical" and transition.name == "take"
            and status == "working" and not (updates or {}).get("next_due")):
        clock_keys = ("reminder", "breach")
    elif current["kind"] == "cash_shift" and status != "accepted":
        clock_keys = ("cash_review_due", "cash_review_escalation")
    elif current["data"].get("mode") in {"central", "independent"} and status == "working":
        clock_keys = ("operation_work_reminder", "operation_work_repeat", "operation_work_escalation",
                      "operation_work_control_due")
        next_data = current["data"] | (updates or {})
        clock_keys += tuple(pending_guest_orders({"data": next_data}))
        clock_keys += tuple(pending_first_calls({"data": next_data}))
    if ("accounting" not in (updates or {})
            and current["data"].get("accounting", {}).get("status") in {"pending", "working", "clarification"}):
        clock_keys = (*(clock_keys or ()), "accounting_due")
    if clock_keys:
        carry_clocks(connection, card_id, current["revision"], revision, clock_keys)
    connection.execute(insert(actions).values(
        card_id=card_id, command_id=command_id, revision=revision, actor=identity,
        action=transition.name, input=payload, previous_status=current["status"],
        status=status, recorded_at=utc_value(now),
    ))
    return revision


def carry_clocks(connection: Connection, card_id: str, previous: int,
                 revision: int, keys: tuple[str, ...]) -> None:
    connection.execute(update(timers).where(
        timers.c.card_id == card_id, timers.c.card_revision == previous,
        timers.c.key.in_(keys),
    ).values(card_revision=revision))
    pending = connection.execute(select(deliveries.c.id, deliveries.c.payload).where(
        deliveries.c.card_id == card_id, deliveries.c.state == "pending",
        deliveries.c.signal.startswith("timer:"),
    ).with_for_update()).all()
    for record in pending:
        if record.payload.get("timer") in keys:
            connection.execute(update(deliveries).where(deliveries.c.id == record.id).values(
                payload=record.payload | {"card_revision": revision}))


def attach(
    connection: Connection, card_id: str, command_id: str, item: str, file_id: str,
    actor: Actor, chat_key: str, role: str, now: datetime, metadata: dict,
    *, assignee_only: bool = True,
) -> None:
    current = get_card(connection, card_id, lock=True)
    assigned_to = current["assignment"]
    assigned = (assigned_to["chat"], assigned_to["role"]) == (chat_key, role) and (
        assigned_to.get("user") in (None, actor.user_id)
    )
    if (assignee_only and not assigned) or not has_role(
        _settings(connection, current["enterprise_id"]), actor, chat_key, role, now
    ):
        raise ProcessPermissionError("Cannot attach to this card")
    existing = connection.execute(select(attachments).where(
        attachments.c.card_id == card_id, attachments.c.command_id == command_id
    ).with_for_update()).mappings().one_or_none()
    values = {"item": item, "platform": actor.platform, "file_id": file_id,
              "metadata": metadata | {"uploader": actor.user_id}}
    if existing is not None:
        if any(existing[key] != value for key, value in values.items()):
            raise ProcessConflict("Attachment command already used")
        return
    connection.execute(insert(attachments).values(
        card_id=card_id, command_id=command_id, **values, received_at=utc_value(now),
    ))


def enqueue(connection: Connection, card_id: str, signal: str, chat_key: str,
            payload: dict, now: datetime) -> None:
    get_card(connection, card_id, lock=True)
    existing = connection.execute(select(deliveries).where(
        deliveries.c.card_id == card_id, deliveries.c.signal == signal,
        deliveries.c.chat_key == chat_key,
    ).with_for_update()).mappings().one_or_none()
    values = {"payload": payload, "available_at": utc_value(now)}
    if existing is None:
        connection.execute(insert(deliveries).values(
            id=uuid4().hex, card_id=card_id, signal=signal, chat_key=chat_key,
            state="pending", **values,
        ))
    elif existing["state"] == "pending":
        connection.execute(update(deliveries).where(deliveries.c.id == existing["id"]).values(**values))


def claim_delivery(connection: Connection, delivery_id: str, now: datetime):
    record = connection.execute(select(deliveries).where(deliveries.c.id == delivery_id)
        .with_for_update()).mappings().one()
    if record["state"] != "pending" or record["available_at"] > utc_value(now):
        return None
    card = get_card(connection, record["card_id"])
    settings = _settings(connection, card["enterprise_id"])
    chat = settings["chats"].get(record["chat_key"])
    if chat is None or not chat_is_open(
        settings, record["chat_key"], now,
        refund_close_hour=refund_delivery_close_hour(card["kind"], chat["kind"]),
    ):
        return None
    guests = record["payload"].get("guest_orders")
    if guests is not None:
        pending = pending_first_calls if record["payload"]["kind"] == "first_call" else pending_guest_orders
        outstanding = set(pending(card).values())
        guests = [order for order in guests if order in outstanding]
        if card["status"] != "working" or not guests:
            connection.execute(update(deliveries).where(deliveries.c.id == delivery_id)
                               .values(state="superseded"))
            return None
        payload = record["payload"] | {"guest_orders": guests}
        connection.execute(update(deliveries).where(deliveries.c.id == delivery_id).values(payload=payload))
        record = dict(record) | {"payload": payload}
    expected = record["payload"].get("card_revision")
    expected_chat = record["payload"].get("expected_chat")
    question = record["payload"].get("question_round")
    stale_shift_summary = record["payload"].get("kind") == "operator_shift_summary" and card["status"] == "closed"
    stale_question = question is not None and (
        card["status"] != "clarification" or question != card["data"].get("question_round")
    )
    accounting = card["data"].get("accounting", {})
    reference = record["payload"].get("accounting_request")
    stale_accounting = reference is not None and (
        reference != accounting.get("request_id")
        or record["payload"].get("accounting_executor") != accounting.get("executor_user")
        or record["payload"].get("accounting_state") != accounting.get("status")
    )
    expired_probe = card["kind"] == "route_probe" and (
        card["status"] != "open" or utc_value(datetime.fromisoformat(card["data"]["expires_at"])) <= utc_value(now)
    )
    if stale_accounting or expired_probe or stale_shift_summary or stale_question or (expected is not None and card["revision"] != expected) or (
        expected_chat is not None and any(chat.get(field) != value for field, value in expected_chat.items())
    ):
        connection.execute(update(deliveries).where(deliveries.c.id == delivery_id)
            .values(state="superseded"))
        return None
    # Commit before sending. A process crash leaves an uncertain attempt and
    # requires reconciliation; it must never be blindly retried.
    attempt_id = uuid4().hex
    identity = {field: chat.get(field) for field in ("platform", "id", "topic_id")}
    payload = record["payload"] | {"delivery_chat": identity}
    connection.execute(update(deliveries).where(deliveries.c.id == delivery_id)
        .values(state="uncertain", attempt_id=attempt_id, payload=payload))
    return dict(record) | {"attempt_id": attempt_id, "chat": chat, "payload": payload}


def finish_delivery(connection: Connection, delivery_id: str, attempt_id: str,
                    *, external_id: str | None = None, retry_at: datetime | None = None) -> bool:
    if (external_id is None) == (retry_at is None):
        raise ValueError("Provide a confirmed delivery or a confirmed safe retry")
    values = {"state": "sent", "external_id": external_id} if retry_at is None else {
        "state": "pending", "available_at": utc_value(retry_at), "attempt_id": None,
    }
    result = connection.execute(update(deliveries).where(
        deliveries.c.id == delivery_id, deliveries.c.attempt_id == attempt_id,
        deliveries.c.state == "uncertain",
    ).values(**values))
    return result.rowcount == 1


def start_timer(connection: Connection, card_id: str, key: str,
                now: datetime, duration: timedelta, *, delivery_id: str | None = None) -> None:
    card = get_card(connection, card_id, lock=True)
    if duration <= timedelta(0):
        raise ValueError("Timer duration must be positive")
    if delivery_id is not None:
        state = connection.execute(select(deliveries.c.state).where(
            deliveries.c.id == delivery_id, deliveries.c.card_id == card_id,
        )).scalar_one_or_none()
        if state != "sent":
            raise ProcessConflict("Timer requires confirmed delivery")
    existing = connection.execute(select(timers).where(
        timers.c.card_id == card_id, timers.c.key == key,
    ).with_for_update()).mappings().one_or_none()
    values = {"card_revision": card["revision"], "due_at": utc_value(now + duration), "elapsed_at": None}
    if existing is None:
        connection.execute(insert(timers).values(card_id=card_id, key=key, **values))
    elif existing["card_revision"] != card["revision"]:
        connection.execute(update(timers).where(timers.c.card_id == card_id, timers.c.key == key)
            .values(**values))


def elapse_timer(connection: Connection, card_id: str, key: str, now: datetime) -> bool:
    card = get_card(connection, card_id, lock=True)
    instant = utc_value(now)
    result = connection.execute(update(timers).where(
        timers.c.card_id == card_id, timers.c.key == key,
        timers.c.card_revision == card["revision"], timers.c.elapsed_at.is_(None),
        timers.c.due_at <= instant,
    ).values(elapsed_at=instant))
    return result.rowcount == 1
