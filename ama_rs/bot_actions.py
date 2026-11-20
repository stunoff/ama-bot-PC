"""Explicit allowlist of authenticated messenger actions."""

import inspect
import json
from datetime import datetime

from sqlalchemy.engine import Connection

from ama_rs import contact_center, operations, personnel, refunds, requests, shifts
from ama_rs.enterprises import Actor
from ama_rs.processes import ProcessConflict

ACTIONS = {
    "cash.document": shifts.add_cash_document,
    "cash.submit": shifts.submit_cash,
    "cash.ask": shifts.request_cash_clarification,
    "cash.answer": shifts.answer_cash_clarification,
    "cash.accept": shifts.accept_cash,
    "checklist.problem": shifts.checklist_problem,
    "problem.resolve": requests.resolve_problem,
    "problem.finish": requests.finish_linked_problem,
    "task.create": requests.make_task,
    "request.take": requests.take_request,
    "request.result": requests.submit_result,
    "request.review": requests.review_result,
    "technical.create": requests.open_technical_request,
    "person.confirm": personnel.confirm_link,
    "person.training": personnel.acknowledge_training,
    "person.skill_check": personnel.record_skill_check,
    "audit.start": operations.begin_audit,
    "audit.section": operations.record_audit_section,
    "audit.submit": operations.submit_audit,
    "audit.review": operations.review_audit,
    "zones.plan": operations.plan_zones,
    "zone.submit": operations.submit_zone,
    "zone.review": operations.review_zone,
    "zones.finish": operations.finish_zones,
    "maintenance.plan": operations.plan_regular_work,
    "maintenance.finish": operations.finish_regular_work,
    "operation.start": contact_center.start_operation,
    "operation.morning": contact_center.submit_morning_operation,
    "operation.take": contact_center.take_operation,
    "operation.order_result": contact_center.record_order_result,
    "operation.close": contact_center.close_operation,
    "refund.request": refunds.request_refund,
    "refund.take": refunds.take_refund,
    "refund.reserve": refunds.reserve_refund,
    "refund.result": refunds.record_refund_result,
    "refund.ask": refunds.ask_refund_question,
    "refund.answer": refunds.answer_refund_question,
}

DATE_FIELDS = {"due_at", "next_due", "deadline"}
SERVER_FIELDS = {"connection", "actor", "now", "command_id", "file_id"}


def workflow_action(connection: Connection, expression: str, command_id: str, actor: Actor,
                    now: datetime, *, file_id: str | None = None) -> str:
    name, _, document = expression.strip().partition(" ")
    if not name:
        return "Доступные формы:\n" + "\n".join(ACTIONS)
    handler = ACTIONS.get(name)
    if handler is None:
        raise ValueError("Unknown workflow action")
    signature = inspect.signature(handler)
    if not document:
        fields = [parameter.name for parameter in signature.parameters.values()
                  if parameter.name not in SERVER_FIELDS]
        return f"/workflow {name} {{…}}\nПоля: {', '.join(fields)}"
    params = json.loads(document)
    if not isinstance(params, dict) or SERVER_FIELDS & params.keys():
        raise ValueError("Only action fields are accepted")
    for field, value in params.items():
        parameter = signature.parameters.get(field)
        if parameter is None:
            raise ValueError("Unknown action field")
        if parameter.annotation in (bool, int, str) and type(value) is not parameter.annotation:
            raise ValueError(f"Invalid type for {field}")
    for field in DATE_FIELDS & params.keys():
        params[field] = datetime.fromisoformat(params[field])
        if params[field].utcoffset() is None:
            raise ValueError("Date values must include a UTC offset")
    server = {"connection": connection, "actor": actor, "now": now, "command_id": command_id}
    if "file_id" in signature.parameters:
        if not file_id:
            raise ProcessConflict("Send the command as a photo caption")
        server["file_id"] = file_id
    bound = signature.bind(**params, **{key: value for key, value in server.items()
                                        if key in signature.parameters})
    result = handler(*bound.args, **bound.kwargs)
    if isinstance(result, (int, str)):
        return f"Готово · {result}"
    return f"Карточка {result['id']} · версия {result['revision']}"
