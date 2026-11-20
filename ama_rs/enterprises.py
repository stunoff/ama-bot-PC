"""Enterprise configuration, calendars, and exact-scope permissions."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, insert, select, update
from sqlalchemy.engine import Connection

from ama_rs.timing import validate_overrides

metadata = MetaData()
enterprises = Table(
    "enterprises", metadata,
    Column("id", String(64), primary_key=True),
    Column("iiko_id", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("settings", JSON, nullable=False),
)
history = Table(
    "enterprise_configuration_history", metadata,
    Column("enterprise_id", String(64), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("operator", String(128), nullable=False),
    Column("settings", JSON, nullable=False),
)

ROLE_CHATS = {
    "service_account": {"enterprise"},
    "employee": {"personal"},
    "manager": {"management", "enterprise"},
    "branch_lead": {"branch", "management"},
    "cash_reviewer": {"cash"},
    "maintenance_manager": {"maintenance"},
    "technician": {"technical"},
    "vipart_specialist": {"vipart"},
    "technical_director": {"technical_management"},
    "operations_director": {"headquarters"},
    "general_director": {"headquarters"},
    "system_admin": {"technical"},
    "franchise_partner": {"partner"},
    "operator": {"contact_center"},
    "senior_operator": {"contact_center", "contact_management"},
    "contact_center_lead": {"contact_center", "contact_management"},
    "finance_executor": {"refunds"},
}

CONTROL_ONLY = {
    ("manager", "enterprise"),
    ("branch_lead", "management"),
    ("contact_center_lead", "contact_center"),
}

# Each workflow can require a role on its own actions. Setting names are
# configuration data and cannot grant workflow capabilities.
CENTRAL_CHATS = {
    "technical", "vipart", "technical_management", "headquarters",
    "contact_center", "contact_management",
}


class ConfigurationConflict(ValueError):
    pass


def _required_text(data: dict, key: str, limit: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key} must be a non-empty string up to {limit} characters")
    return value


def _wall_time(value: str) -> time:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("Expected a local time in HH:MM format")
    return time.fromisoformat(value)


def _validate_windows(windows: list, *, weekly: bool) -> None:
    if not isinstance(windows, list):
        raise ValueError("Calendar intervals must be a list")
    for window in windows:
        if weekly and (type(window["weekday"]) is not int or not 0 <= window["weekday"] <= 6):
            raise ValueError("weekday must be between 0 (Monday) and 6")
        start, end = _wall_time(window["start"]), _wall_time(window["end"])
        if start == end or (not weekly and start > end):
            raise ValueError("Invalid interval. Split overnight exceptions between dates.")


def validate_settings(settings: dict) -> None:
    for name, limit in (
        ("id", 64), ("iiko_id", 128), ("name", 160), ("timezone", 64),
        ("format", 64), ("server_address", 255), ("lm_address", 255),
    ):
        _required_text(settings, name, limit)
    try:
        ZoneInfo(settings["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Unknown IANA timezone") from exc
    if settings.get("contact_mode") not in ("central", "independent"):
        raise ValueError("contact_mode must be central or independent")
    if settings.get("final_route") not in ("headquarters", "partner"):
        raise ValueError("final_route must be headquarters or partner")
    brands = settings["brands"]
    if not isinstance(brands, list) or not brands or any(
        not isinstance(brand, str) or not brand.strip() for brand in brands
    ) or len(set(brands)) != len(brands):
        raise ValueError("At least one distinct brand is required")

    validate_overrides(settings.get("timing_overrides", {}))
    shifts = settings.get("shift_schedule")
    if shifts is not None:
        _wall_time(shifts["service_starts_at"])
        _wall_time(shifts["service_ends_at"])
        for key in ("preparation_minutes", "closing_minutes"):
            if type(shifts[key]) is not int or not 0 <= shifts[key] < 1440:
                raise ValueError(f"{key} must be an integer from 0 to 1439")

    for key, chat in settings["chats"].items():
        _required_text({"key": key}, "key", 100)
        _required_text(chat, "id", 128)
        if chat.get("platform") not in ("telegram", "max"):
            raise ValueError(f"Unsupported chat platform for {key}")
        if chat.get("kind") not in set().union(*ROLE_CHATS.values()):
            raise ValueError(f"Unsupported chat kind for {key}")
        if "general_topic_id" in chat:
            topic = chat["general_topic_id"]
            if chat["platform"] != "telegram" or chat["kind"] != "technical" or (
                topic is not None and (not isinstance(topic, str) or not topic.isdecimal())
            ):
                raise ValueError("General topic requires an explicit Telegram technical forum topic")
        if "topic_id" in chat:
            _required_text(chat, "topic_id", 128)
        if "timezone" in chat:
            try:
                ZoneInfo(chat["timezone"])
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"Unknown chat timezone for {key}") from exc
        if "timezone" in chat and chat["kind"] not in CENTRAL_CHATS | {"refunds"}:
            raise ValueError(f"{key} uses the enterprise timezone")
        calendar = chat.get("calendar")
        if calendar is not None and calendar != "always":
            _validate_windows(calendar, weekly=True)
        for day, windows in chat.get("calendar_exceptions", {}).items():
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError("Calendar exception must use YYYY-MM-DD")
            _validate_windows(windows, weekly=False)

    for grant in settings["grants"]:
        chat = settings["chats"].get(grant["chat"])
        if chat is None or chat["kind"] not in ROLE_CHATS.get(grant["role"], ()):
            raise ValueError("Role and chat are incompatible")
        _required_text(grant, "user", 128)
        for bound in ("starts_at", "ends_at"):
            if bound in grant and datetime.fromisoformat(grant[bound]).utcoffset() is None:
                raise ValueError(f"{bound} must specify a UTC offset")
        if "starts_at" in grant and "ends_at" in grant:
            if datetime.fromisoformat(grant["starts_at"]) >= datetime.fromisoformat(grant["ends_at"]):
                raise ValueError("Role interval must have positive duration")


def get_enterprise(connection: Connection, enterprise_id: str):
    return connection.execute(
        select(enterprises).where(enterprises.c.id == enterprise_id)
    ).mappings().one_or_none()


def save_enterprise(
    connection: Connection, settings: dict, expected_revision: int, operator: str
) -> int:
    """Write configuration and history in the caller's transaction."""
    validate_settings(settings)
    _required_text({"operator": operator}, "operator", 128)
    if expected_revision < 0:
        raise ValueError("Revision cannot be negative")
    revision = expected_revision + 1
    values = {"iiko_id": settings["iiko_id"], "revision": revision, "settings": settings}
    if expected_revision == 0:
        connection.execute(insert(enterprises).values(id=settings["id"], **values))
    else:
        result = connection.execute(
            update(enterprises)
            .where(enterprises.c.id == settings["id"], enterprises.c.revision == expected_revision)
            .values(**values)
        )
        if result.rowcount != 1:
            raise ConfigurationConflict("Configuration changed; reload the current revision")
    connection.execute(insert(history).values(
        enterprise_id=settings["id"], revision=revision, operator=operator, settings=settings
    ))
    return revision


@dataclass(frozen=True)
class Actor:
    platform: str
    user_id: str
    chat_id: str
    topic_id: str | None = None


def has_role(
    settings: dict, actor: Actor, chat_key: str, role: str, instant: datetime, *,
    work: bool = True,
) -> bool:
    """Eligible roles must be combined with each workflow's state and assignment checks."""
    if instant.utcoffset() is None:
        raise ValueError("Authorization time must be timezone-aware")
    chat = settings["chats"].get(chat_key)
    if chat is None or (chat["platform"], chat["id"]) != (actor.platform, actor.chat_id):
        return False
    if role == "system_admin":
        if actor.platform != "telegram" or "general_topic_id" not in chat or chat["general_topic_id"] != actor.topic_id:
            return False
    elif chat.get("topic_id") is not None and chat["topic_id"] != actor.topic_id:
        return False
    if work and (role, chat["kind"]) in CONTROL_ONLY:
        return False
    current_roles = {
        grant["role"] for grant in settings["grants"]
        if grant["chat"] == chat_key and grant["user"] == actor.user_id
        and ("starts_at" not in grant or datetime.fromisoformat(grant["starts_at"]) <= instant)
        and ("ends_at" not in grant or instant < datetime.fromisoformat(grant["ends_at"]))
    }
    if work and chat["kind"] == "contact_center" and "contact_center_lead" in current_roles:
        return False
    return role in current_roles


def chat_is_open(settings: dict, chat_key: str, instant: datetime) -> bool:
    if instant.utcoffset() is None:
        raise ValueError("Calendar time must be timezone-aware")
    chat = settings["chats"][chat_key]
    calendar = chat.get("calendar")
    local = instant.astimezone(ZoneInfo(chat.get("timezone", settings["timezone"])))
    weekday, clock = local.weekday(), local.time()
    exceptions = chat.get("calendar_exceptions", {})
    today = local.date()
    if today.isoformat() in exceptions:
        return any(
            _wall_time(window["start"]) <= clock < _wall_time(window["end"])
            for window in exceptions[today.isoformat()]
        )
    if calendar == "always":
        return True
    yesterday_overridden = (today - timedelta(days=1)).isoformat() in exceptions
    for window in calendar or ():
        start, end = _wall_time(window["start"]), _wall_time(window["end"])
        day = window["weekday"]
        if start < end and weekday == day and start <= clock < end:
            return True
        if start > end and (
            (weekday == day and clock >= start)
            or (not yesterday_overridden and weekday == (day + 1) % 7 and clock < end)
        ):
            return True
    return False
