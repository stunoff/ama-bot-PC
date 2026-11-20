"""Versioned enterprise overrides for implemented SLA controls."""

DEFAULTS = {
    "technical.normal": (15, 30, 60),
    "technical.high": (10, 20, 30),
    "technical.critical": (5, 10, 15),
    "technical.vipart": (5, 15, 25),
    "refund.execution": (30, 60, 720, 1440),
    "refund.control.branch": (10, 20, 40),
    "refund.control.final": (10, 20),
    "cash.question": (10, 25, 40),
    "cash.discrepancy": (30, 45),
    "cash.review_overdue": (120,),
    "operation.take": (5, 10),
    "operation.handoff": (10,),
    "operation.mass_first_call": (5, 10, 15),
    "operation.guest_contact": (5, 10),
    "operation.order_take": (3, 5),
    "operation.work": (10, 20, 30),
    "operation.stop_work": (10, 20),
    "task.review": (30, 45, 60),
    "control.branch": (10, 20),
    "control.partner": (10,),
    "control.general": (10,),
    "control.technical.normal": (30, 60),
    "control.technical.high": (15, 30),
    "control.technical.critical": (10, 20),
    "control.executive.normal": (60, 120),
    "control.executive.high": (30, 60),
    "control.executive.critical": (15, 30),
    "technical.review": (30, 45),
}


def validate_overrides(overrides: dict) -> None:
    if not isinstance(overrides, dict):
        raise ValueError("Expected timing overrides by key")
    for key, minutes in overrides.items():
        default = DEFAULTS.get(key)
        lengths = {len(default), 1} if key == "refund.execution" else {len(default)} if default else set()
        if (default is None or not isinstance(minutes, list) or len(minutes) not in lengths
                or any(type(value) is not int or not 0 < value <= 10080 for value in minutes)
                or minutes != sorted(set(minutes))):
            raise ValueError(f"Invalid ordered timer thresholds for {key}")


def effective(settings: dict, key: str) -> tuple[int, ...]:
    value = tuple(settings.get("timing_overrides", {}).get(key, DEFAULTS[key]))
    if key == "refund.execution" and len(value) == 1:
        deadline = value[0]
        return (*[reminder for reminder in DEFAULTS[key][:-1] if reminder < deadline], deadline)
    return value
