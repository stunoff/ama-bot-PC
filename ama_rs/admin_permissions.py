"""Administrative capabilities for a single enterprise and General topic."""

EDITABLE = frozenset({
    "name", "timezone", "format", "shift_schedule", "facilities", "contact_mode",
    "final_route", "scheduled_reports", "scheduled_checklists", "timing_overrides", "chat_calendar", "writeoff_reasons", "attendance_monitoring",
})
ADMIN_ROLES = {"system_admin", "delegated_admin"}
