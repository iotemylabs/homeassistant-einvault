"""Actions (services) for EinVault.

Every action:

* targets a companion device, which is resolved back to a config entry and a
  companion id through the device registry;
* validates locally before sending, because the API rejects unknown fields
  outright (``additionalProperties: false``) and answers ``400 invalidSubtype``
  for a type/subtype mismatch — a local error can name the offending value;
* sends an ``Idempotency-Key``, so a retried call replays rather than
  duplicates;
* refreshes the coordinator on success, so sensors reflect the change without
  waiting for the next poll.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
import logging
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .api import (
    EinVaultAuthError,
    EinVaultConflictError,
    EinVaultConnectionError,
    EinVaultError,
    EinVaultForbiddenError,
    EinVaultNotFoundError,
    EinVaultRateLimitError,
    EinVaultResponseError,
    EinVaultValidationError,
    validate_subtypes,
)
from .const import (
    DAILY_EVENT_TYPES,
    DOMAIN,
    ERROR_ALREADY_COMPLETED,
    ERROR_NO_ACTIVE_SHIFT,
    ERROR_NOT_ASSIGNED,
    ERROR_NOT_RECURRING,
    HEALTH_EVENT_TYPES,
    JOURNAL_MOODS,
    MAX_DURATION_MINUTES,
    WEIGHT_UNITS,
)
from .coordinator import EinVaultDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_LOG_ACTIVITY = "log_activity"
SERVICE_LOG_WEIGHT = "log_weight"
SERVICE_LOG_HEALTH_EVENT = "log_health_event"
SERVICE_SET_JOURNAL = "set_journal"
SERVICE_COMPLETE_REMINDER = "complete_reminder"
SERVICE_SKIP_REMINDER = "skip_reminder"

ATTR_DEVICE_ID = "device_id"
ATTR_TYPE = "type"
ATTR_SUBTYPES = "subtypes"
ATTR_DURATION_MINUTES = "duration_minutes"
ATTR_NOTES = "notes"
ATTR_LOGGED_AT = "logged_at"
ATTR_WEIGHT = "weight"
ATTR_UNIT = "unit"
ATTR_RECORDED_AT = "recorded_at"
ATTR_TITLE = "title"
ATTR_OCCURRED_AT = "occurred_at"
ATTR_VET_NAME = "vet_name"
ATTR_VET_CLINIC = "vet_clinic"
ATTR_DATE = "date"
ATTR_BODY = "body"
ATTR_MOOD = "mood"
ATTR_REMINDER_ID = "reminder_id"

_TARGET_SCHEMA: dict[Any, Any] = {
    vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string])
}

LOG_ACTIVITY_SCHEMA = vol.Schema(
    {
        **_TARGET_SCHEMA,
        vol.Required(ATTR_TYPE): vol.In(DAILY_EVENT_TYPES),
        vol.Optional(ATTR_SUBTYPES): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_DURATION_MINUTES): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_DURATION_MINUTES)
        ),
        vol.Optional(ATTR_NOTES): cv.string,
        vol.Optional(ATTR_LOGGED_AT): cv.datetime,
    }
)

LOG_WEIGHT_SCHEMA = vol.Schema(
    {
        **_TARGET_SCHEMA,
        vol.Required(ATTR_WEIGHT): vol.All(vol.Coerce(float), vol.Range(min=0, min_included=False)),
        vol.Required(ATTR_UNIT): vol.In(WEIGHT_UNITS),
        vol.Optional(ATTR_NOTES): cv.string,
        vol.Optional(ATTR_RECORDED_AT): cv.datetime,
    }
)

LOG_HEALTH_EVENT_SCHEMA = vol.Schema(
    {
        **_TARGET_SCHEMA,
        vol.Required(ATTR_TYPE): vol.In(HEALTH_EVENT_TYPES),
        vol.Required(ATTR_TITLE): cv.string,
        vol.Optional(ATTR_NOTES): cv.string,
        vol.Optional(ATTR_OCCURRED_AT): cv.datetime,
        vol.Optional(ATTR_VET_NAME): cv.string,
        vol.Optional(ATTR_VET_CLINIC): cv.string,
    }
)

SET_JOURNAL_SCHEMA = vol.Schema(
    {
        **_TARGET_SCHEMA,
        vol.Optional(ATTR_DATE): cv.date,
        vol.Optional(ATTR_BODY): cv.string,
        vol.Optional(ATTR_MOOD): vol.In(JOURNAL_MOODS),
    }
)

REMINDER_SCHEMA = vol.Schema(
    {
        **_TARGET_SCHEMA,
        vol.Required(ATTR_REMINDER_ID): cv.string,
    }
)


def _resolve_targets(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[EinVaultDataUpdateCoordinator, str]]:
    """Map targeted devices to (coordinator, companion id) pairs.

    Raises:
        ServiceValidationError: if a device is not an EinVault companion, or
            its config entry is not loaded.
    """
    registry = dr.async_get(hass)
    resolved: list[tuple[EinVaultDataUpdateCoordinator, str]] = []

    for device_id in call.data[ATTR_DEVICE_ID]:
        device = registry.async_get(device_id)
        if device is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders={"device_id": device_id},
            )

        identifier = next((ident for domain, ident in device.identifiers if domain == DOMAIN), None)
        if identifier is None or "_" not in identifier:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_companion",
                translation_placeholders={"device": device.name or device_id},
            )

        entry_id, _, companion_id = identifier.partition("_")
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or not hasattr(entry, "runtime_data") or entry.runtime_data is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"device": device.name or device_id},
            )

        resolved.append((entry.runtime_data.coordinator, companion_id))

    if not resolved:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_target")
    return resolved


def _translate_error(err: EinVaultError) -> HomeAssistantError:  # noqa: PLR0911
    """Turn an API error into something a user can act on.

    Branching is on ``code`` only — ``message`` is localized by the server.
    """
    if isinstance(err, EinVaultForbiddenError):
        if err.code == ERROR_NO_ACTIVE_SHIFT:
            return ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_active_shift"
            )
        if err.code == ERROR_NOT_ASSIGNED:
            return ServiceValidationError(translation_domain=DOMAIN, translation_key="not_assigned")
    if isinstance(err, EinVaultConflictError):
        if err.code == ERROR_ALREADY_COMPLETED:
            return ServiceValidationError(
                translation_domain=DOMAIN, translation_key="already_completed"
            )
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"code": err.code, "message": err.api_message},
        )
    if isinstance(err, EinVaultValidationError):
        if err.code == ERROR_NOT_RECURRING:
            return ServiceValidationError(
                translation_domain=DOMAIN, translation_key="not_recurring"
            )
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"code": err.code, "message": err.api_message},
        )
    if isinstance(err, EinVaultNotFoundError):
        return ServiceValidationError(
            translation_domain=DOMAIN, translation_key="reminder_not_found"
        )
    if isinstance(err, EinVaultAuthError):
        return HomeAssistantError(translation_domain=DOMAIN, translation_key="invalid_token")
    if isinstance(err, EinVaultRateLimitError):
        return HomeAssistantError(translation_domain=DOMAIN, translation_key="rate_limited")
    if isinstance(err, EinVaultConnectionError):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        )
    if isinstance(err, EinVaultResponseError):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"code": err.code, "message": err.api_message},
        )
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="api_error",
        translation_placeholders={"code": "unknown", "message": str(err)},
    )


def _as_datetime(value: Any) -> datetime | None:
    """Normalise an optional datetime field."""
    return value if isinstance(value, datetime) else None


def _as_date(value: Any) -> date_type | None:
    """Normalise an optional date field."""
    return value if isinstance(value, date_type) and not isinstance(value, datetime) else None


async def _async_run(
    hass: HomeAssistant,
    call: ServiceCall,
    action: Any,
) -> None:
    """Run an action against every targeted companion, then refresh once each.

    Calls are issued one companion at a time; the client serialises them
    anyway, and a burst is the fastest way to hit the rate limit.
    """
    targets = _resolve_targets(hass, call)
    touched: set[int] = set()

    for coordinator, companion_id in targets:
        try:
            await action(coordinator, companion_id)
        except EinVaultError as err:
            raise _translate_error(err) from err
        touched.add(id(coordinator))

    for coordinator, _ in targets:
        if id(coordinator) in touched:
            await coordinator.async_request_refresh()
            touched.discard(id(coordinator))


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the EinVault actions once for the whole integration."""

    async def _log_activity(call: ServiceCall) -> None:
        event_type = call.data[ATTR_TYPE]
        subtypes = call.data.get(ATTR_SUBTYPES)

        # Validate before any HTTP call so a typo names itself, rather than
        # coming back as a bare 400 from the server.
        try:
            validate_subtypes(event_type, subtypes)
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_subtypes",
                translation_placeholders={"error": str(err)},
            ) from err

        async def action(coordinator: EinVaultDataUpdateCoordinator, companion_id: str) -> None:
            await coordinator.client.async_log_activity(
                event_type=event_type,
                companion_id=companion_id,
                subtypes=subtypes,
                duration_minutes=call.data.get(ATTR_DURATION_MINUTES),
                notes=call.data.get(ATTR_NOTES),
                logged_at=_as_datetime(call.data.get(ATTR_LOGGED_AT)),
                idempotency_key=str(uuid4()),
            )

        await _async_run(hass, call, action)

    async def _log_weight(call: ServiceCall) -> None:
        async def action(coordinator: EinVaultDataUpdateCoordinator, companion_id: str) -> None:
            await coordinator.client.async_log_weight(
                companion_id=companion_id,
                weight=call.data[ATTR_WEIGHT],
                unit=call.data[ATTR_UNIT],
                notes=call.data.get(ATTR_NOTES),
                recorded_at=_as_datetime(call.data.get(ATTR_RECORDED_AT)),
                idempotency_key=str(uuid4()),
            )

        await _async_run(hass, call, action)

    async def _log_health_event(call: ServiceCall) -> None:
        async def action(coordinator: EinVaultDataUpdateCoordinator, companion_id: str) -> None:
            await coordinator.client.async_log_health_event(
                companion_id=companion_id,
                event_type=call.data[ATTR_TYPE],
                title=call.data[ATTR_TITLE],
                notes=call.data.get(ATTR_NOTES),
                occurred_at=_as_datetime(call.data.get(ATTR_OCCURRED_AT)),
                vet_name=call.data.get(ATTR_VET_NAME),
                vet_clinic=call.data.get(ATTR_VET_CLINIC),
                idempotency_key=str(uuid4()),
            )

        await _async_run(hass, call, action)

    async def _set_journal(call: ServiceCall) -> None:
        async def action(coordinator: EinVaultDataUpdateCoordinator, companion_id: str) -> None:
            # Omitted keys are never sent as null: the server preserves the
            # stored value for an absent field, which is not the same thing.
            await coordinator.client.async_set_journal(
                companion_id=companion_id,
                entry_date=_as_date(call.data.get(ATTR_DATE)),
                body_text=call.data.get(ATTR_BODY),
                mood=call.data.get(ATTR_MOOD),
                idempotency_key=str(uuid4()),
            )

        await _async_run(hass, call, action)

    async def _complete_reminder(call: ServiceCall) -> None:
        reminder_id = call.data[ATTR_REMINDER_ID]

        async def action(coordinator: EinVaultDataUpdateCoordinator, companion_id: str) -> None:
            await coordinator.client.async_complete_reminder(
                reminder_id, idempotency_key=str(uuid4())
            )

        await _async_run(hass, call, action)

    async def _skip_reminder(call: ServiceCall) -> None:
        reminder_id = call.data[ATTR_REMINDER_ID]

        async def action(coordinator: EinVaultDataUpdateCoordinator, companion_id: str) -> None:
            await coordinator.client.async_skip_reminder(reminder_id, idempotency_key=str(uuid4()))

        await _async_run(hass, call, action)

    for name, handler, schema in (
        (SERVICE_LOG_ACTIVITY, _log_activity, LOG_ACTIVITY_SCHEMA),
        (SERVICE_LOG_WEIGHT, _log_weight, LOG_WEIGHT_SCHEMA),
        (SERVICE_LOG_HEALTH_EVENT, _log_health_event, LOG_HEALTH_EVENT_SCHEMA),
        (SERVICE_SET_JOURNAL, _set_journal, SET_JOURNAL_SCHEMA),
        (SERVICE_COMPLETE_REMINDER, _complete_reminder, REMINDER_SCHEMA),
        (SERVICE_SKIP_REMINDER, _skip_reminder, REMINDER_SCHEMA),
    ):
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(DOMAIN, name, handler, schema=schema)
