"""Scoped financial reporting for online refunds."""

from collections import Counter
from decimal import Decimal
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.enterprises import Actor, enterprises, has_role
from ama_rs.processes import ProcessPermissionError, actions, cards
from ama_rs.reports import _period

REPORT_ROLES = {"finance_executor", "branch_lead", "operations_director", "general_director"}
FILTERS = {"enterprise", "brand", "reason", "requester", "executor", "status", "request"}


def _scoped_chats(settings: dict, actor: Actor, now: datetime) -> set[str]:
    return {
        grant["chat"] for grant in settings["grants"] if grant["role"] in REPORT_ROLES
        and has_role(settings, actor, grant["chat"], grant["role"], now)
    }


def _matches(record, filters: dict) -> bool:
    data = record["data"]
    requester = data["requester"]["role"]
    flow = "enterprise" if requester == "service_account" else "operator"
    actual = {
        "enterprise": record["enterprise_id"], "brand": data["brand"], "reason": data["reason"],
        "requester": flow, "executor": data["worker"].get("user"), "status": record["status"],
        "request": record["id"],
    }
    return all(actual[key] == value for key, value in filters.items())


def refund_report(connection: Connection, actor: Actor, now: datetime, begin: date,
                  until: date, filters: dict) -> str:
    if not isinstance(filters, dict) or filters.keys() - FILTERS or any(
        not isinstance(value, str) or not value.strip() for value in filters.values()
    ):
        raise ValueError("Invalid refund report filters")
    if not timedelta(0) < until - begin <= timedelta(days=366):
        raise ValueError("Invalid report period")
    eligible = []
    for entry in connection.execute(select(enterprises).order_by(enterprises.c.id)).mappings():
        if filters.get("enterprise", entry["id"]) != entry["id"]:
            continue
        scope = _scoped_chats(entry["settings"], actor, now)
        if scope:
            eligible.append((entry, scope))
    if not eligible:
        raise ProcessPermissionError("Refund reporting is outside the actor's scope")
    output = [f"Онлайн-возвраты · {begin} — {until - timedelta(days=1)}"]
    for entry, scope in eligible:
        settings = entry["settings"]
        start, end = _period(begin, until, settings["timezone"])
        query = select(cards).where(
            cards.c.enterprise_id == entry["id"], cards.c.kind == "refund",
            cards.c.created_at >= start, cards.c.created_at < end,
        )
        management = any(settings["chats"][key]["kind"] != "refunds" for key in scope)
        records = [row for row in connection.execute(query).mappings()
                   if _matches(row, filters) and (management or row["data"]["worker"]["chat"] in scope)]
        states = Counter(row["status"] for row in records)
        amounts = sum(row["data"]["requested_kopecks"] for row in records)
        confirmations = {}
        for row in records:
            if row["status"] != "executed":
                continue
            data = row["data"]
            reference = (data.get("payment_reference"), data.get("financial_reference"))
            key = reference if all(reference) else row["id"]
            confirmations.setdefault(key, set()).add(data["requested_kopecks"])
        executed = sum(next(iter(values)) for values in confirmations.values() if len(values) == 1)
        conflicts = sum(len(values) > 1 for values in confirmations.values())
        partial = sum(bool(row["data"].get("positions")) for row in records)
        dates = [row["id"] for row in records]
        acceptance, execution = [], []
        escalated = 0
        if dates:
            escalated = len(set(connection.execute(select(cards.c.parent_id).where(
                cards.c.kind == "refund_control", cards.c.parent_id.in_(dates),
            )).scalars()))
            by_card = {row["id"]: row for row in records}
            seen = set()
            activity = connection.execute(select(actions).where(
                actions.c.card_id.in_(dates), actions.c.action.in_(("take_refund", "record_refund_result"))
            ).order_by(actions.c.recorded_at, actions.c.revision)).mappings()
            for row in activity:
                key = (row["card_id"], row["action"])
                if key in seen or (row["action"] == "record_refund_result" and row["status"] != "executed"):
                    continue
                seen.add(key)
                minutes = max(0, int((row["recorded_at"] - by_card[row["card_id"]]["created_at"]).total_seconds() / 60))
                (acceptance if row["action"] == "take_refund" else execution).append(minutes)

        def average(items):
            return f"{sum(items) / len(items):.0f} мин" if items else "—"

        output += [f"{settings['name']} · {settings['timezone']}",
                   f"Запросов: {len(records)}. Полных: {len(records) - partial}. Частичных: {partial}.",
                   f"Запрошено: {Decimal(amounts) / 100:.2f} ₽. Исполнено: {Decimal(executed) / 100:.2f} ₽.",
                   f"Эскалировано: {escalated}.",
                   f"Среднее до принятия: {average(acceptance)}. До исполнения: {average(execution)}."]
        if conflicts:
            output.append(f"Требуют сверки суммы подтверждённых операций: {conflicts}.")
        if filters.get("request") and records:
            record = records[0]
            data = record["data"]
            output += [f"Карточка: {record['id']}", f"Бренд: {data['brand']}", f"Причина: {data['reason']}"]
        if states:
            labels = {"open": "Новые", "checking": "На проверке", "clarification": "Уточнение",
                      "reserved": "Сумма зарезервирована", "uncertain": "Результат неизвестен",
                      "executed": "Выполнены", "not_required": "Возврат не требуется"}
            output.append(", ".join(f"{labels.get(state, state)}: {amount}" for state, amount in sorted(states.items())))
    return "\n".join(output)[:3800]
