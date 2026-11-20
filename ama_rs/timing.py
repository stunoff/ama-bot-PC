"""Versioned enterprise overrides for implemented SLA controls."""

DEFAULTS = {
    "technical.normal": (15, 30, 60),
    "technical.high": (10, 20, 30),
    "technical.critical": (5, 10, 15),
    "technical.vipart": (5, 15, 25),
    "refund.execution": (1440,),
}


def validate_overrides(overrides: dict) -> None:
    if not isinstance(overrides, dict):
        raise ValueError("Expected timing overrides by key")
    for key, minutes in overrides.items():
        default = DEFAULTS.get(key)
        if (default is None or not isinstance(minutes, list) or len(minutes) != len(default)
                or any(type(value) is not int or not 0 < value <= 10080 for value in minutes)
                or minutes != sorted(set(minutes))):
            raise ValueError(f"Invalid ordered timer thresholds for {key}")


def effective(settings: dict, key: str) -> tuple[int, ...]:
    return tuple(settings.get("timing_overrides", {}).get(key, DEFAULTS[key]))
