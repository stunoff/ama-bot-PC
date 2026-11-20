"""Configuration and iiko evidence required for scenario activation."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ama_rs.checklist_setup import SCENARIO_ROUTES, route_issues
from ama_rs.reports import coverage

SCENARIOS = {
    "scheduled_reports": ("Плановые сводки", ("orders",)),
    "attendance_monitoring": ("Контроль рабочего времени", ("schedule", "attendance")),
}


def scenario_issues(connection: Connection, enterprise_id: str, settings: dict,
                    field: str, now: datetime) -> list[str]:
    _, expected_sources = SCENARIOS[field]
    issues = route_issues(connection, enterprise_id, settings, now, required=SCENARIO_ROUTES[field])
    rows = connection.execute(select(coverage.c.source, coverage.c.state).where(
        coverage.c.enterprise_id == enterprise_id, coverage.c.source.in_(expected_sources),
    ).order_by(coverage.c.updated_at.desc(), coverage.c.day.desc())).all()
    latest = {}
    for source, state in rows:
        latest.setdefault(source, state)
    for source in expected_sources:
        if latest.get(source) not in {"complete", "partial"}:
            issues.append(f"{source}: нужна подтверждённая выгрузка iiko.")
    return issues
