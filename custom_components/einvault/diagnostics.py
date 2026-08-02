"""Diagnostics for EinVault.

Companion records carry a lot of genuinely sensitive material — microchip
numbers, a vet's phone number, an emergency contact, free-text notes left for a
sitter. Diagnostics downloads get pasted into public issue trackers, so
everything of that kind is redacted here, alongside both credentials.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EinVaultConfigEntry
from .const import CONF_API_TOKEN, CONF_CALENDAR_FEED_URL

TO_REDACT_CONFIG = {
    CONF_API_TOKEN,
    # The calendar feed URL embeds a token as a path segment, so the whole URL
    # is a credential.
    CONF_CALENDAR_FEED_URL,
}

TO_REDACT_COMPANION = {
    "microchip",
    "emergency_contact_name",
    "emergency_contact_phone",
    "vet_name",
    "vet_phone",
    "vet_clinic",
    "notes_for_sitter",
    # Free text the owner wrote about the animal; not ours to publish.
    "bio",
    "feeding_schedule",
    "walk_schedule",
    "medication_schedule",
}

TO_REDACT_USER = {"username", "display_name"}


def _serialise(value: Any) -> Any:
    """Convert dataclasses and datetimes into JSON-friendly values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _serialise(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _serialise(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_serialise(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EinVaultConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data

    companions = [
        async_redact_data(_serialise(companion), TO_REDACT_COMPANION)
        for companion in data.companions.values()
    ]
    users = [async_redact_data(_serialise(user), TO_REDACT_USER) for user in data.users]

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT_CONFIG),
            "options": async_redact_data(dict(entry.options), TO_REDACT_CONFIG),
        },
        "instance": {
            "base_url": coordinator.client.base_url,
            "calendar_configured": entry.runtime_data.calendar_coordinator is not None,
            "calendar_last_update_success": (
                entry.runtime_data.calendar_coordinator.last_update_success
                if entry.runtime_data.calendar_coordinator is not None
                else None
            ),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds() if coordinator.update_interval else None
            ),
            "mood_sensor_enabled": coordinator.mood_sensor_enabled,
            "include_archived": coordinator.include_archived,
            # The number the call-budget conversation turns on.
            "calls_last_refresh": data.calls_last_refresh,
            "total_requests": coordinator.client.request_count,
        },
        "data": {
            "companions": companions,
            "latest_events": {
                companion_id: {
                    event_type: _serialise(event) for event_type, event in events.items()
                }
                for companion_id, events in data.latest_events.items()
            },
            "latest_weight": {
                companion_id: _serialise(entry_value)
                for companion_id, entry_value in data.latest_weight.items()
            },
            "journals": {
                companion_id: _serialise(journal) for companion_id, journal in data.journals.items()
            },
            "reminders": [_serialise(reminder) for reminder in data.reminders],
            "shifts": [_serialise(shift) for shift in data.shifts],
            "quick_logs": [_serialise(quick_log) for quick_log in data.quick_logs],
            "users": users,
        },
    }
