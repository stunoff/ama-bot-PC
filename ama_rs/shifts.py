"""Morning and evening checklists and the linked cash-shift workflow."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, chat_is_open, get_enterprise, has_role, save_enterprise
from ama_rs.processes import (
    ProcessConflict, ProcessPermissionError, Transition, actions, apply_action, attach,
    attachments, cards, create_card, deliveries, enqueue, get_card, utc_value,
)


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    photos: tuple[str, ...] = ()
    optional: bool = False


MORNING = (
    Step("internet", "Интернет: замер не ниже 5 Мбит/с", ("speed",)),
    Step("iiko_open", "Открытие кассовой смены и индикатор обмена", ("iiko",)),
    Step("morning_cash", "Подписанный чек утреннего пересчёта", ("receipt",)),
    Step("terminal", "Стационарный терминал", ("terminal_receipt",)),
    Step("mangust", "Мангуст", ("screen",), True),
    Step("call", "Контрольный звонок", ("call",)),
    Step("guest_entrance", "Вход для гостей"),
    Step("ready", "Готовность к обслуживанию"),
    Step("opening", "Фактическое открытие или задержка"),
    Step("continue", "Продолжить утренний чек-лист"),
    Step("packing", "Зона комплектации", ("packing",)),
    Step("courier_bags", "Курьерские сумки", ("bags",), True),
    Step("kitchen", "Кухня и рабочие станции", ("overview", "stations", "passages")),
    Step("wash", "Моечная зона", ("wash",)),
    Step("cold", "Холодильное и морозильное оборудование", ("kitchen_cold", "stock_cold")),
    Step("stockroom", "Склад и проходы", ("stock", "shelves", "passages")),
    Step("attendance", "План и явка"),
    Step("team_ready", "Готовность команды"),
    Step("supplies", "Запас упаковки и расходников", ("supplies",)),
    Step("production", "Производственная готовность и стоп-лист"),
    Step("courier_terminals", "Курьерские терминалы", ("terminals",), True),
    Step("sber_sound", "СберЗвук", ("screen",), True),
    Step("queue_display", "Телевизор электронной очереди", ("screen",), True),
    Step("cash_display", "Экранный кассовый повторитель", ("screen",), True),
    Step("video_menu", "Телевизоры с видеоменю", ("screen",), True),
    Step("facilities", "Гостевые и служебные зоны"),
    Step("coffee", "Кофеаппарат", ("coffee",), True),
    Step("drinks", "Холодильник с напитками", ("fridge",), True),
    Step("facade", "Территория, фасад и входная группа", ("facade", "territory")),
)
EVENING = (
    Step("attendance", "Состояние смены по персоналу"),
    Step("packing", "Зона комплектации, упаковка и расходники", ("packing",)),
    Step("facilities", "Гостевые и служебные зоны"),
    Step("evening_cash", "Вечерний комплект кассы и передача смены"),
    Step("storage", "Продукция и хранение"),
    Step("stations", "Рабочие станции, оборудование и инвентарь"),
    Step("sanitation", "Санитарное состояние производственных зон"),
    Step("production_photos", "Фотоблок производства",
         ("overview", "stations", "wash", "kitchen_cold", "stock_cold")),
    Step("stockroom", "Склад", ("stock",)),
    Step("finish", "Финальная проверка и завершение смены"),
)
FACILITIES = {"guest_hall", "reception", "guest_restroom", "staff_restroom"}
CASH_OUTCOMES = {"accepted", "explained", "correction_required"}


def route(settings: dict, kind: str) -> str:
    matches = [key for key, chat in settings["chats"].items() if chat["kind"] == kind]
    if len(matches) != 1:
        raise ProcessConflict(f"Expected one configured route for {kind}")
    return matches[0]


def _settings(connection: Connection, enterprise_id: str) -> dict:
    enterprise = get_enterprise(connection, enterprise_id)
    if enterprise is None:
        raise ProcessConflict("Unknown enterprise")
    return enterprise["settings"]


def _step_list(settings: dict, phase: str, local_day: date) -> list[dict]:
    template = {"morning": MORNING, "evening": EVENING}[phase]
    facilities = set(settings.get("facilities", ()))
    steps = []
    for step in template:
        if (step.key == "guest_entrance" and "guest_entrance" not in facilities
                or step.key == "facilities" and not facilities & FACILITIES):
            continue
        hidden = settings.get("hidden_shift_items", {}).get(step.key)
        if step.optional and hidden and local_day < date.fromisoformat(hidden):
            continue
        photos = sorted(facilities & FACILITIES) if step.key == "facilities" else list(step.photos)
        steps.append({"key": step.key, "title": step.title, "photos": photos, "optional": step.optional})
    return steps


def checklist_schedule(settings: dict, business_day: date) -> dict[str, datetime]:
    shift = settings["shift_schedule"]
    zone = ZoneInfo(settings["timezone"])
    service_start = datetime.combine(business_day, time.fromisoformat(shift["service_starts_at"]), zone)
    service_end = datetime.combine(business_day, time.fromisoformat(shift["service_ends_at"]), zone)
    if service_end <= service_start:
        service_end += timedelta(days=1)
    schedule = {
        "morning": service_start - timedelta(minutes=shift["preparation_minutes"]),
        "evening": service_end,
    }
    chat = route(settings, "enterprise")
    if any(not chat_is_open(settings, chat, instant) for instant in schedule.values()):
        raise ProcessConflict("Checklist schedule falls outside the enterprise calendar")
    return schedule


def begin_checklist(connection: Connection, enterprise_id: str, phase: str, shift_id: str,
                    now: datetime, *, business_day: date | None = None):
    settings = _settings(connection, enterprise_id)
    local_day = business_day or now.astimezone(ZoneInfo(settings["timezone"])).date()
    management = route(settings, "management")
    card = create_card(
        connection, enterprise_id, f"{phase}_checklist", shift_id,
        {"chat": route(settings, "enterprise"), "role": "service_account"},
        {"day": local_day.isoformat(), "steps": _step_list(settings, phase, local_day),
         "current": 0, "results": {}, "management": management},
        now, status="filling",
    )
    identity = _chat_identity(settings["chats"][management])
    enqueue(connection, card["id"], "management-card", management,
            {"kind": "card", "expected_chat": identity}, now)
    enqueue(connection, card["id"], "start-checklist", card["assignment"]["chat"], {"kind": "card"}, now)
    return card


def _step(card, key: str) -> dict:
    if card["kind"] not in ("morning_checklist", "evening_checklist") or card["status"] != "filling":
        raise ProcessConflict("Checklist is not open")
    step = card["data"]["steps"][card["data"]["current"]]
    if key != step["key"]:
        raise ProcessConflict("Unexpected checklist step")
    return step


def _cash_card(connection: Connection, checklist, settings: dict, now: datetime):
    return create_card(
        connection, checklist["enterprise_id"], "cash_shift", checklist["source_key"],
        {"chat": route(settings, "enterprise"), "role": "service_account"},
        {"day": checklist["data"]["day"], "cash_chat": route(settings, "cash"),
         "required": sorted(set(["morning:receipt", "evening:iiko_report", "evening:stationary",
                                  "evening:signed_count"] + [
             f"evening:courier:{terminal}" for terminal in settings.get("courier_terminals", ())
         ]))},
        now, status="collecting",
    )


def add_checklist_photo(connection: Connection, card_id: str, command_id: str, step_key: str,
                        slot: str, file_id: str, actor: Actor, now: datetime) -> None:
    card = get_card(connection, card_id, lock=True)
    step = _step(card, step_key)
    if slot not in step["photos"] and slot != "problem":
        raise ValueError("Unknown photo slot")
    settings = _settings(connection, card["enterprise_id"])
    target = card
    destination = card["data"]["management"]
    if step_key == "morning_cash" and slot == "receipt":
        target = _cash_card(connection, card, settings, now)
        destination = target["data"]["cash_chat"]
    elif slot == "problem":
        target = connection.execute(select(cards).where(
            cards.c.enterprise_id == card["enterprise_id"], cards.c.parent_id == card_id,
            cards.c.kind == "problem", cards.c.source_key == f"{card_id}:{step_key}",
        ).with_for_update()).mappings().one_or_none()
        if target is None:
            raise ProcessConflict("Record the problem description before adding photos")
    item = "morning:receipt" if target["kind"] == "cash_shift" else f"{step_key}:{slot}"
    attach(connection, target["id"], command_id, item, file_id, actor, route(settings, "enterprise"),
           "service_account", now, {}, assignee_only=target["kind"] != "problem")
    enqueue(connection, target["id"], f"photo:{command_id}", destination,
            {"kind": "attachment", "command_id": command_id}, now)


def checklist_problem(connection: Connection, card_id: str, step_key: str, description: str,
                      actor: Actor, now: datetime):
    card = get_card(connection, card_id, lock=True)
    _step(card, step_key)
    settings = _settings(connection, card["enterprise_id"])
    if not has_role(settings, actor, route(settings, "enterprise"), "service_account", now):
        raise ProcessPermissionError("Only the service account can report a checklist problem")
    if not description.strip():
        raise ValueError("A problem description is required")
    management = card["data"]["management"]
    problem = create_card(connection, card["enterprise_id"], "problem", f"{card_id}:{step_key}",
        {"chat": management, "role": "manager"}, {"step": step_key, "description": description},
        now, parent_id=card_id)
    enqueue(connection, problem["id"], "problem", management, {"kind": "card"}, now)
    return problem


def complete_step(
    connection: Connection, card_id: str, command_id: str, revision: int, key: str,
    outcome: str, actor: Actor, now: datetime, *, details: dict | None = None,
) -> int:
    card = get_card(connection, card_id, lock=True)
    existing = connection.execute(select(actions.c.input, actions.c.revision, actions.c.actor).where(
        actions.c.card_id == card_id, actions.c.command_id == command_id
    )).mappings().one_or_none()
    settings = _settings(connection, card["enterprise_id"])
    chat = route(settings, "enterprise")
    if not has_role(settings, actor, chat, "service_account", now):
        raise ProcessPermissionError("Only the service account can complete a checklist step")
    result = {"outcome": outcome, "details": details or {}}
    if existing is not None:
        recorded = (existing["input"]["updates"] or {}).get("results", {}).get(key)
        recorded_input = {k: recorded.get(k) for k in ("outcome", "details")} if recorded else None
        if recorded_input != result or existing["actor"]["user"] != actor.user_id:
            raise ProcessConflict("Command already used")
        return existing["revision"]
    if card["revision"] != revision:
        raise ProcessConflict("Card changed; reload the current version")
    step = _step(card, key)
    if outcome == "absent":
        if not step["optional"]:
            raise ProcessConflict("This step is mandatory")
        entry = get_enterprise(connection, card["enterprise_id"])
        today = now.astimezone(ZoneInfo(settings["timezone"])).date()
        year, month = divmod(today.year * 12 + today.month + 2, 12)
        until = today.replace(year=year, month=month + 1, day=min(today.day, monthrange(year, month + 1)[1]))
        settings["hidden_shift_items"] = settings.get("hidden_shift_items", {}) | {key: until.isoformat()}
        save_enterprise(connection, settings, entry["revision"], f"{actor.platform}:{actor.user_id}")
    elif outcome == "problem":
        problem = checklist_problem(connection, card_id, key, result["details"].get("description", ""), actor, now)
        if key not in {"storage", "stations", "sanitation", "attendance", "team_ready"}:
            _require_documents(connection, problem["id"], {f"{key}:problem"})
        result["problem_card"] = problem["id"]
    elif key == "opening":
        if outcome not in {"opened", "delayed"}:
            raise ValueError("Expected opening or delay confirmation")
        if outcome == "delayed" and not {"reason", "expected_at"} <= result["details"].keys():
            raise ValueError("Opening delay requires a reason and expected time")
        result["recorded_at"] = now.isoformat()
        operational = route(settings, "contact_center") if settings["contact_mode"] == "central" else card["data"]["management"]
        enqueue(connection, card_id, "opening", operational, {"kind": "opening", **result}, now)
    elif outcome == "ok":
        if key in {"morning_cash", "evening_cash"}:
            cash = _cash_card(connection, card, settings, now)
            if key == "morning_cash":
                _require_documents(connection, cash["id"], {"morning:receipt"})
            elif cash["status"] == "collecting":
                raise ProcessConflict("Cash documents have not been submitted")
            result["cash_card"] = cash["id"]
        else:
            _require_documents(connection, card_id, {f"{key}:{slot}" for slot in step["photos"]})
    else:
        raise ValueError("Unknown checklist outcome")
    data = card["data"]
    next_step = data["current"] + 1
    finished = next_step == len(data["steps"])
    management = data["management"]
    new_revision = apply_action(
        connection, card_id, command_id, revision,
        Transition(f"step:{key}", frozenset({"filling"}), "review" if finished else None,
                   frozenset({"service_account"})),
        actor, chat, "service_account", now,
        updates={"current": next_step, "results": data["results"] | {key: result}},
        assignment={"chat": management, "role": "manager"} if finished else None,
    )
    refresh_management_card(connection, card, new_revision, now)
    if finished:
        enqueue(connection, card_id, "review", management, {"kind": "review"}, now)
    return new_revision


def _require_documents(connection: Connection, card_id: str, required: set[str]) -> None:
    if not required:
        return
    present = set(connection.execute(select(attachments.c.item)
        .where(attachments.c.card_id == card_id)).scalars())
    if missing := required - present:
        raise ProcessConflict(f"Missing documents: {', '.join(sorted(missing))}")


def accept_checklist(connection: Connection, card_id: str, command_id: str, revision: int,
                     actor: Actor, now: datetime) -> int:
    card = get_card(connection, card_id, lock=True)
    if card["kind"] not in ("morning_checklist", "evening_checklist"):
        raise ProcessConflict("Expected a checklist")
    accepted = apply_action(connection, card_id, command_id, revision,
        Transition("accept", frozenset({"review"}), "accepted", frozenset({"manager"})),
        actor, card["data"]["management"], "manager", now)
    refresh_management_card(connection, card, accepted, now)
    return accepted


def _chat_identity(chat: dict) -> dict:
    return {key: chat.get(key) for key in ("platform", "id", "topic_id")}


def refresh_management_card(connection: Connection, card, revision: int, now: datetime) -> None:
    target = card["data"]["management"]
    original = connection.execute(select(deliveries).where(
        deliveries.c.card_id == card["id"], deliveries.c.chat_key == target,
        deliveries.c.signal == "management-card",
    ).with_for_update()).mappings().one_or_none()
    if original is None:
        return
    chat = original["payload"].get("expected_chat")
    if not chat or not original["external_id"]:
        return
    enqueue(connection, card["id"], f"management-edit:{revision}", target,
            {"kind": "card", "expected_chat": chat, "edit": original["external_id"],
             "card_revision": revision}, now)


def add_cash_document(connection: Connection, checklist_id: str, command_id: str, document: str,
                      file_id: str, actor: Actor, now: datetime) -> str:
    checklist = get_card(connection, checklist_id, lock=True)
    _step(checklist, "evening_cash")
    settings = _settings(connection, checklist["enterprise_id"])
    cash = _cash_card(connection, checklist, settings, now)
    item = f"evening:{document}"
    if cash["status"] != "collecting" or (item not in cash["data"]["required"] and not document.startswith("expense:")):
        raise ProcessConflict("Document does not belong to the open cash package")
    attach(connection, cash["id"], command_id, item, file_id, actor, route(settings, "enterprise"),
           "service_account", now, {})
    enqueue(connection, cash["id"], f"photo:{command_id}", cash["data"]["cash_chat"],
            {"kind": "attachment", "command_id": command_id}, now)
    return cash["id"]


def submit_cash(connection: Connection, cash_id: str, command_id: str, revision: int,
                actor: Actor, now: datetime, *, no_pending_operations: bool,
                expense_documents: list[str]) -> int:
    cash = get_card(connection, cash_id, lock=True)
    if cash["kind"] != "cash_shift" or no_pending_operations is not True:
        raise ProcessConflict("Confirm that no cash operations are pending")
    required = set(cash["data"]["required"]) | {f"evening:expense:{item}" for item in expense_documents}
    _require_documents(connection, cash_id, required)
    settings = _settings(connection, cash["enterprise_id"])
    updated = apply_action(connection, cash_id, command_id, revision,
        Transition("submit_cash", frozenset({"collecting"}), "awaiting_iiko", frozenset({"service_account"})),
        actor, route(settings, "enterprise"), "service_account", now,
        updates={"expenses": expense_documents, "no_pending_operations": True})
    enqueue(connection, cash_id, "package", cash["data"]["cash_chat"], {"kind": "card"}, now)
    return updated


def confirm_iiko_closure(connection: Connection, enterprise_id: str, shift_id: str, event_id: str,
                         now: datetime) -> bool:
    """Call only after a verified iiko event for this enterprise and shift."""
    cash = connection.execute(select(cards).where(
        cards.c.enterprise_id == enterprise_id, cards.c.kind == "cash_shift", cards.c.source_key == shift_id,
    ).with_for_update()).mappings().one_or_none()
    if cash is None or cash["status"] not in {"awaiting_iiko", "clarification"} or "closed_in_iiko_at" in cash["data"]:
        return False
    revision = cash["revision"] + 1
    chat = cash["data"]["cash_chat"]
    status = "clarification" if cash["status"] == "clarification" else "review"
    assignment = cash["assignment"] if status == "clarification" else {"chat": chat, "role": "cash_reviewer"}
    connection.execute(update(cards).where(cards.c.id == cash["id"]).values(
        status=status, revision=revision, assignment=assignment,
        data=cash["data"] | {"closed_in_iiko_at": now.isoformat()},
    ))
    connection.execute(actions.insert().values(
        card_id=cash["id"], command_id=f"iiko:{event_id}", revision=revision, action="iiko_closed",
        actor={"source": "iiko"}, input={}, previous_status=cash["status"], status=status,
        recorded_at=utc_value(now),
    ))
    if status == "review":
        enqueue(connection, cash["id"], "review", chat, {"kind": "review"}, now)
    return True


def accept_cash(connection: Connection, cash_id: str, command_id: str, revision: int,
                outcome: str, actor: Actor, now: datetime) -> int:
    cash = get_card(connection, cash_id, lock=True)
    if cash["kind"] != "cash_shift" or outcome not in CASH_OUTCOMES:
        raise ValueError("Invalid cash acceptance")
    return apply_action(connection, cash_id, command_id, revision,
        Transition("accept_cash", frozenset({"review"}), "accepted", frozenset({"cash_reviewer"})),
        actor, cash["data"]["cash_chat"], "cash_reviewer", now, updates={"outcome": outcome})


def request_cash_clarification(connection: Connection, cash_id: str, command_id: str, revision: int,
                               question: str, actor: Actor, now: datetime) -> int:
    cash = get_card(connection, cash_id, lock=True)
    if cash["kind"] != "cash_shift" or not question.strip():
        raise ValueError("Expected a cash shift and a question")
    settings = _settings(connection, cash["enterprise_id"])
    enterprise_chat = route(settings, "enterprise")
    new_revision = apply_action(connection, cash_id, command_id, revision,
        Transition("request_cash_clarification", frozenset({"awaiting_iiko", "review"}),
                   "clarification", frozenset({"cash_reviewer"}), assignee_only=False),
        actor, cash["data"]["cash_chat"], "cash_reviewer", now, updates={"question": question},
        assignment={"chat": enterprise_chat, "role": "service_account"})
    enqueue(connection, cash_id, f"clarification:{new_revision}", enterprise_chat,
            {"kind": "clarification", "question": question}, now)
    return new_revision


def answer_cash_clarification(connection: Connection, cash_id: str, command_id: str, revision: int,
                              answer: str, actor: Actor, now: datetime) -> int:
    cash = get_card(connection, cash_id, lock=True)
    if cash["kind"] != "cash_shift" or not answer.strip():
        raise ValueError("Expected a cash shift and an answer")
    settings = _settings(connection, cash["enterprise_id"])
    reviewer_chat = cash["data"]["cash_chat"]
    new_revision = apply_action(connection, cash_id, command_id, revision,
        Transition("answer_cash_clarification", frozenset({"clarification"}),
                   "review" if "closed_in_iiko_at" in cash["data"] else "awaiting_iiko",
                   frozenset({"service_account"})),
        actor, route(settings, "enterprise"), "service_account", now, updates={"answer": answer},
        assignment={"chat": reviewer_chat, "role": "cash_reviewer"})
    enqueue(connection, cash_id, f"clarification-answer:{new_revision}", reviewer_chat,
            {"kind": "clarification_answer", "answer": answer}, now)
    return new_revision
